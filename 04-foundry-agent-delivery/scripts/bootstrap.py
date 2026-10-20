"""Discover participant resources, preview OIDC access, and apply only on request."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_SUBJECTS_START = datetime(2026, 7, 15, tzinfo=timezone.utc)


class BootstrapError(RuntimeError):
    """An expected configuration, discovery, or bootstrap failure."""


def run(command, *, input_text=None, check=True):
    result = subprocess.run(
        command, input=input_text, text=True, capture_output=True, check=check,
    )
    if result.returncode and check:
        raise subprocess.CalledProcessError(
            result.returncode, command, output=result.stdout, stderr=result.stderr
        )
    return json.loads(result.stdout) if result.stdout.strip() else None


def validate_config(config):
    expected = {
        "schema_version", "repository", "subscription_id", "identity_resource_group",
        "oidc_subject_format", "agent_name", "environments",
    }
    if (
        not isinstance(config, dict)
        or set(config) != expected
        or type(config.get("schema_version")) is not int
        or config["schema_version"] != 1
        or not isinstance(config["environments"], dict)
        or set(config["environments"]) != {"dev", "prod"}
    ):
        raise BootstrapError("Unexpected participant configuration schema.")
    for key in ("repository", "subscription_id", "identity_resource_group", "agent_name"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise BootstrapError(f"Participant field {key} must be a nonempty string.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", config["repository"]):
        raise BootstrapError("Repository must use owner/repository format.")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", config["subscription_id"]):
        raise BootstrapError("subscription_id must be an explicit UUID.")
    if config["oidc_subject_format"] not in {"auto", "legacy", "immutable"}:
        raise BootstrapError("oidc_subject_format must be auto, legacy, or immutable.")
    for environment, values in config["environments"].items():
        required = {
            "resource_group", "account_name", "project_name", "model_deployment",
        }
        if (
            not isinstance(values, dict)
            or set(values) != required
            or not all(isinstance(value, str) and value.strip() for value in values.values())
        ):
            raise BootstrapError(
                f"{environment} must define exactly these nonempty values: "
                + ", ".join(sorted(required))
            )
    dev, prod = config["environments"]["dev"], config["environments"]["prod"]
    if (
        dev["account_name"] == prod["account_name"]
        or (dev["account_name"], dev["project_name"])
        == (prod["account_name"], prod["project_name"])
    ):
        raise BootstrapError("Dev and prod must be distinct Foundry accounts/projects.")
    return config


def prompt_config(repository):
    print(f"Using GitHub repository: {repository}")
    print("Enter the existing Foundry resources assigned to you by the instructor.")
    config = {
        "schema_version": 1,
        "repository": repository,
        "subscription_id": input("Azure subscription ID: ").strip(),
        "identity_resource_group": input(
            "Existing resource group for the two GitHub identities: "
        ).strip(),
        "oidc_subject_format": "auto",
        "agent_name": input("Foundry agent name [workshop-agent]: ").strip()
        or "workshop-agent",
        "environments": {},
    }
    for environment in ("dev", "prod"):
        print(f"{environment.upper()} Foundry project")
        config["environments"][environment] = {
            "resource_group": input("  Resource group: ").strip(),
            "account_name": input("  Foundry account: ").strip(),
            "project_name": input("  Foundry project: ").strip(),
            "model_deployment": input("  Approved model deployment: ").strip(),
        }
    return validate_config(config)


def current_repository():
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        check=True,
        capture_output=True,
        text=True,
    )
    repository = result.stdout.strip()
    if not repository:
        raise BootstrapError("GitHub CLI could not identify the current repository.")
    return repository


def _subject_format(config, repo):
    selected = config["oidc_subject_format"]
    if selected != "auto":
        return selected
    created = datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00"))
    return "immutable" if created >= IMMUTABLE_SUBJECTS_START else "legacy"


def subjects(config, repo):
    owner_name = repo["owner"]["login"]
    repository_name = repo["name"]
    if (
        repo.get("full_name") != config["repository"]
        or not str(repo.get("id", "")).isdigit()
        or not str(repo["owner"].get("id", "")).isdigit()
    ):
        raise BootstrapError("Use the exact repository casing and valid GitHub repository IDs.")
    base = f"{owner_name}/{repository_name}"
    if _subject_format(config, repo) == "immutable":
        base = (
            f"{owner_name}@{repo['owner']['id']}/"
            f"{repository_name}@{repo['id']}"
        )
    return {
        "dev": f"repo:{base}:ref:refs/heads/{repo['default_branch']}",
        "prod": f"repo:{base}:environment:production",
    }


def discover(config):
    sub = config["subscription_id"]
    account = run(["az", "account", "show", "--subscription", sub, "-o", "json"])
    if account.get("id", "").lower() != sub.lower():
        raise BootstrapError("Azure CLI returned a different subscription than requested.")
    groups = {config["identity_resource_group"]}
    groups.update(value["resource_group"] for value in config["environments"].values())
    group_info = {}
    for group_name in sorted(groups):
        group_info[group_name] = run([
            "az", "group", "show", "--subscription", sub, "--name", group_name, "-o", "json",
        ])
    repo = run(["gh", "api", f"repos/{config['repository']}"])
    if not repo.get("permissions", {}).get("admin"):
        raise BootstrapError("GitHub repository administrator access is required.")
    oidc_subjects = subjects(config, repo)
    bindings = {}
    for environment, values in config["environments"].items():
        account_id = (
            f"/subscriptions/{sub}/resourceGroups/{values['resource_group']}"
            f"/providers/Microsoft.CognitiveServices/accounts/{values['account_name']}"
        )
        account_resource = run([
            "az", "rest", "--subscription", sub, "--method", "get", "--url",
            f"https://management.azure.com{account_id}?api-version=2025-06-01", "-o", "json",
        ])
        project = run([
            "az", "rest", "--subscription", sub, "--method", "get", "--url",
            f"https://management.azure.com{account_id}/projects/{values['project_name']}"
            "?api-version=2025-06-01",
            "-o", "json",
        ])
        if project.get("properties", {}).get("provisioningState") != "Succeeded":
            raise BootstrapError(f"{environment} Foundry project is not ready.")
        endpoints = project.get("properties", {}).get("endpoints", {})
        endpoint = endpoints.get("AI Foundry API")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise BootstrapError(f"{environment} project did not return a Foundry API endpoint.")
        deployment = run([
            "az", "rest", "--subscription", sub, "--method", "get", "--url",
            f"https://management.azure.com{account_id}/deployments/"
            f"{values['model_deployment']}?api-version=2025-06-01",
            "-o", "json",
        ])
        model = deployment.get("properties", {}).get("model", {})
        actual_policy = {
            "region": account_resource.get("location"),
            "model_name": model.get("name"),
            "model_version": model.get("version"),
            "deployment_type": deployment.get("sku", {}).get("name"),
            "version_upgrade_option": deployment.get("properties", {}).get(
                "versionUpgradeOption"
            ),
        }
        bindings[environment] = {
            "AZURE_SUBSCRIPTION_ID": sub,
            "AZURE_TENANT_ID": account["tenantId"],
            "FOUNDRY_PROJECT_ENDPOINT": endpoint,
            "FOUNDRY_ACCOUNT_ID": account_id,
            "FOUNDRY_AGENT_NAME": config["agent_name"],
            "FOUNDRY_MODEL_DEPLOYMENT": values["model_deployment"],
            "FOUNDRY_MODEL_NAME": actual_policy["model_name"],
            "FOUNDRY_MODEL_VERSION": actual_policy["model_version"],
            "FOUNDRY_DEPLOYMENT_TYPE": actual_policy["deployment_type"],
            "FOUNDRY_VERSION_UPGRADE_OPTION": actual_policy["version_upgrade_option"],
            "FOUNDRY_REGION": actual_policy["region"],
            "OIDC_SUBJECT": oidc_subjects[environment],
        }
    prefix = "workshop-" + hashlib.sha256(
        config["repository"].encode("utf-8")
    ).hexdigest()[:12]
    return {
        "repository": repo,
        "resource_groups": group_info,
        "identity_prefix": prefix,
        "oidc_subjects": oidc_subjects,
        "bindings": bindings,
    }


def deployment_parameters(config, discovered):
    dev = config["environments"]["dev"]
    prod = config["environments"]["prod"]
    location = discovered["resource_groups"][config["identity_resource_group"]]["location"]
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "location": {"value": location},
            "identityResourceGroupName": {"value": config["identity_resource_group"]},
            "devResourceGroupName": {"value": dev["resource_group"]},
            "devAccountName": {"value": dev["account_name"]},
            "devProjectName": {"value": dev["project_name"]},
            "prodResourceGroupName": {"value": prod["resource_group"]},
            "prodAccountName": {"value": prod["account_name"]},
            "prodProjectName": {"value": prod["project_name"]},
            "identityPrefix": {"value": discovered["identity_prefix"]},
            "devOidcSubject": {"value": discovered["oidc_subjects"]["dev"]},
            "prodOidcSubject": {"value": discovered["oidc_subjects"]["prod"]},
        },
    }


def plan_hash(config, discovered, parameters, what_if):
    plan = {
        "repository": config["repository"],
        "subscription_id": config["subscription_id"],
        "identity_prefix": discovered["identity_prefix"],
        "oidc_subjects": discovered["oidc_subjects"],
        "bindings": discovered["bindings"],
        "parameters": parameters["parameters"],
        "what_if": what_if,
    }
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_template(output_path):
    run([
        "az", "bicep", "build", "--file", str(ROOT / "infra/github-oidc.bicep"),
        "--outfile", str(output_path),
    ])


def preview_plan(config):
    discovered = discover(config)
    params = deployment_parameters(config, discovered)
    preview = [
        "az", "deployment", "sub", "what-if", "--subscription",
        config["subscription_id"], "--location",
        discovered["resource_groups"][config["identity_resource_group"]]["location"],
        "--name", discovered["identity_prefix"], "--template-file",
        "", "--parameters",
    ]
    with tempfile.TemporaryDirectory() as directory:
        template = str(Path(directory) / "github-oidc.json")
        parameters = str(Path(directory) / "parameters.json")
        build_template(template)
        Path(parameters).write_text(json.dumps(params), encoding="utf-8")
        preview[preview.index("--template-file") + 1] = template
        preview.extend([f"@{parameters}", "--no-pretty-print", "-o", "json"])
        what_if = run(preview)
    return discovered, params, what_if, plan_hash(config, discovered, params, what_if)


def print_preview(config, discovered, params, what_if, fingerprint, mode):
    print(json.dumps({
        "repository": config["repository"],
        "default_branch": discovered["repository"]["default_branch"],
        "identity_prefix": discovered["identity_prefix"],
        "oidc_subjects": discovered["oidc_subjects"],
        "bindings": discovered["bindings"],
        "bicep_parameters": params["parameters"],
        "what_if": what_if,
        "plan_sha256": fingerprint,
        "mode": mode,
    }, indent=2))


def apply(config, discovered):
    subscription = config["subscription_id"]
    with tempfile.TemporaryDirectory() as directory:
        template = str(Path(directory) / "github-oidc.json")
        parameters = str(Path(directory) / "parameters.json")
        build_template(template)
        Path(parameters).write_text(
            json.dumps(deployment_parameters(config, discovered)), encoding="utf-8"
        )
        deployment = run([
            "az", "deployment", "sub", "create", "--subscription", subscription,
            "--location", discovered["resource_groups"][config["identity_resource_group"]]["location"],
            "--name", discovered["identity_prefix"], "--template-file", template,
            "--parameters", f"@{parameters}", "-o", "json",
        ])
    client_ids = deployment["properties"]["outputs"]["clientIds"]["value"]
    if len(client_ids) != 2 or not all(isinstance(client_id, str) for client_id in client_ids):
        raise BootstrapError("Azure deployment did not return both OIDC client IDs.")
    for index, environment in enumerate(("dev", "prod")):
        bindings = discovered["bindings"][environment]
        bindings["AZURE_CLIENT_ID"] = client_ids[index]
        for name, value in bindings.items():
            run([
                "gh", "variable", "set", f"{environment.upper()}_{name}",
                "--repo", config["repository"], "--body", value,
            ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="participant.json")
    parser.add_argument(
        "--setup", action="store_true",
        help="Prompt for participant resources, preview them, and apply after exact confirmation.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Create scoped Azure access and set GitHub repository variables.",
    )
    parser.add_argument(
        "--confirm-plan",
        help="Exact plan SHA-256 printed by a prior preview; required together with --apply.",
    )
    args = parser.parse_args()
    try:
        if args.setup and (args.apply or args.confirm_plan):
            raise BootstrapError("--setup cannot be combined with --apply or --confirm-plan.")
        if args.apply != bool(args.confirm_plan):
            raise BootstrapError("--apply requires --confirm-plan, and --confirm-plan requires --apply.")
        config_path = Path(args.config)
        if args.setup:
            if config_path.exists():
                raise BootstrapError(
                    f"{config_path} already exists; review it or choose another --config path."
                )
            config = prompt_config(current_repository())
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            print(f"Saved participant-only configuration to {config_path}.")
        else:
            config = validate_config(json.loads(config_path.read_text(encoding="utf-8")))
        discovered, params, what_if, fingerprint = preview_plan(config)
        print_preview(
            config, discovered, params, what_if, fingerprint,
            "setup and apply" if args.setup else "apply" if args.apply else "preview only",
        )
        if args.setup:
            print(
                "\nCompare both discovered model policies with the instructor-approved "
                "policy and review every proposed Azure change above."
            )
            confirmation = input(
                f"To apply this exact plan and set GitHub repository variables, "
                f"enter its SHA-256 ({fingerprint}): "
            ).strip()
            if confirmation != fingerprint:
                print("Confirmation did not match; no Azure or GitHub changes were applied.")
                return 1
            current, current_params, current_what_if, current_fingerprint = preview_plan(config)
            if current_fingerprint != fingerprint:
                raise BootstrapError(
                    "Plan changed after confirmation; no changes were applied. Review the new preview."
                )
            apply(config, current)
            print(
                "Scoped OIDC access and repository variables applied. "
                "Bootstrap is not transactional; inspect completed changes before retrying."
            )
            return 0
        if not args.apply:
            print(
                "Preview only. Compare the discovered model policy with the instructor-approved "
                "policy. No Azure permissions or GitHub variables were changed."
            )
            return 0
        if args.confirm_plan != fingerprint:
            raise BootstrapError(
                "Plan changed since preview or confirmation SHA is wrong; review the new preview."
            )
        current, _, _, current_fingerprint = preview_plan(config)
        if current_fingerprint != fingerprint:
            raise BootstrapError(
                "Plan changed after preview; no changes were applied. Review the new preview."
            )
        apply(config, current)
        print(
            "Scoped OIDC access and repository variables applied. "
            "Bootstrap is not transactional; inspect completed changes before retrying."
        )
        return 0
    except (
        BootstrapError, EOFError, ValueError, KeyError, OSError, subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        print("No automatic rollback was attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
