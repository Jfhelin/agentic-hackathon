"""Discover participant resources, preview OIDC access, and apply only on request."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_SUBJECTS_START = datetime(2026, 7, 15, tzinfo=timezone.utc)
MIN_PYTHON = (3, 12)
DEFAULT_AGENT_NAME = "workshop-agent"
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


class BootstrapError(RuntimeError):
    """An expected configuration, discovery, or bootstrap failure."""


def check_prerequisites():
    """Fail fast with actionable hints before prompting or calling Azure."""
    problems = []
    if sys.version_info < MIN_PYTHON:
        problems.append(
            f"Python {'.'.join(map(str, MIN_PYTHON))}+ is required; "
            f"found {sys.version.split()[0]}."
        )
    missing = [tool for tool in ("az", "gh") if shutil.which(tool) is None]
    for tool in missing:
        problems.append(
            "Azure CLI (az) is not installed." if tool == "az"
            else "GitHub CLI (gh) is not installed."
        )
    checks = [
        ("az", ["az", "account", "show", "-o", "none"], "Sign in with `az login`."),
        ("gh", ["gh", "auth", "status"], "Sign in with `gh auth login`."),
        (
            "az", ["az", "bicep", "version"],
            "The Bicep CLI is missing or built for another CPU architecture. "
            "Run `az bicep uninstall` and then `az bicep install` "
            "(Apple silicon: `az bicep install --target-platform osx-arm64`).",
        ),
    ]
    for tool, command, hint in checks:
        if tool in missing:
            continue
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            problems.append(f"`{' '.join(command)}` failed. {hint}")
    if problems:
        raise BootstrapError(
            "Prerequisite checks failed:\n- " + "\n- ".join(problems)
        )


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
    optional = {"production_reviewers"}
    if (
        not isinstance(config, dict)
        or not expected <= set(config) <= expected | optional
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
    reviewers = config.get("production_reviewers", [])
    if (
        not isinstance(reviewers, list)
        or len(set(reviewers)) != len(reviewers)
        or not all(isinstance(login, str) and GITHUB_LOGIN.fullmatch(login) for login in reviewers)
    ):
        raise BootstrapError("production_reviewers must be a list of unique GitHub logins.")
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


SUBSCRIPTION_ID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
PROJECT_ENDPOINT = re.compile(
    r"https://(?P<account>[A-Za-z0-9][A-Za-z0-9-]*)\.services\.ai\.azure\.com"
    r"/api/projects/(?P<project>[A-Za-z0-9][A-Za-z0-9_.-]*)/?"
)


def parse_project_endpoint(value):
    match = PROJECT_ENDPOINT.fullmatch(value.strip())
    if not match:
        raise BootstrapError(
            "Expected a Foundry project endpoint like "
            "https://<account>.services.ai.azure.com/api/projects/<project>."
        )
    return match["account"], match["project"]


class AzureLookup:
    """Read-only lookups scoped to the single subscription the participant provides."""

    def check_subscription(self, subscription_id):
        try:
            account = run([
                "az", "account", "show", "--subscription", subscription_id, "-o", "json",
            ])
        except subprocess.CalledProcessError as error:
            raise BootstrapError(
                "Your az login cannot see that subscription. Check the ID, or run "
                "`az login --tenant <tenant-id>` for the workshop tenant."
            ) from error
        if account.get("id", "").lower() != subscription_id.lower():
            raise BootstrapError("Azure CLI returned a different subscription than requested.")

    def account_resource_group(self, subscription_id, account_name):
        groups = run([
            "az", "resource", "list", "--subscription", subscription_id,
            "--resource-type", "Microsoft.CognitiveServices/accounts",
            "--name", account_name, "--query", "[].resourceGroup", "-o", "json",
        ]) or []
        if len(groups) != 1:
            raise BootstrapError(
                f"Foundry account {account_name!r} was not found in subscription "
                f"{subscription_id}. Check the endpoint and subscription."
            )
        return groups[0]

    def model_deployments(self, subscription_id, values):
        result = run([
            "az", "rest", "--subscription", subscription_id, "--method", "get", "--url",
            f"https://management.azure.com{account_resource_id(subscription_id, values)}"
            "/deployments?api-version=2025-06-01", "-o", "json",
        ]) or {}
        return {
            item["name"]: item.get("properties", {}).get("model", {}).get("name", "")
            for item in result.get("value", []) if isinstance(item.get("name"), str)
        }

    def prompt_agents(self, subscription_id, endpoint):
        return list_prompt_agents(subscription_id, endpoint)


def _ask(prompt, preset=None, default=None):
    if preset:
        print(f"{prompt}: {preset}")
        return preset
    suffix = f" [{default}]" if default else ""
    return input(f"{prompt}{suffix}: ").strip() or (default or "")


def _ask_valid(prompt, check, preset=None, default=None):
    """Re-ask only the failing answer instead of losing the whole setup."""
    while True:
        answer = _ask(prompt, preset, default)
        try:
            return check(answer)
        except BootstrapError as error:
            if preset:
                raise
            print(f"  {error} Try again.")


def _choose(prompt, options, preset=None, default=None, strict=False):
    if preset:
        print(f"{prompt}: {preset}")
        return preset
    for index, label in enumerate(options, start=1):
        print(f"  {index}. {label}")

    def check(answer):
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if answer in options or (answer and not strict):
            return answer
        raise BootstrapError(
            f"Choose one of the listed options (1-{len(options)})."
            if answer else "A value is required."
        )
    return _ask_valid(f"{prompt} (number or name)", check, default=default)


def choose_agent(available, preset=None):
    if preset:
        print(f"Foundry agent name: {preset}")
        return preset
    if not available:
        print(
            "No saved prompt agents were found in the dev project. Create and save "
            "one in the Foundry portal before running Capture."
        )
        return _ask("Foundry agent name", default=DEFAULT_AGENT_NAME)
    print("Prompt agents in the dev project:")
    return _choose(
        "Foundry agent to promote", available,
        default=available[0] if len(available) == 1 else None, strict=True,
    )


def choose_deployment(environment, deployments, preset=None, preferred=None):
    if preset:
        if preset not in deployments:
            raise BootstrapError(
                f"{environment} account has no model deployment named {preset!r}; "
                f"found {sorted(deployments) or 'none'}."
            )
        print(f"  Model deployment: {preset}")
        return preset
    chat = sorted(name for name, model in deployments.items() if "embedding" not in model)
    if not chat:
        raise BootstrapError(f"{environment} account has no chat model deployment.")
    default = preferred if preferred in chat else chat[0] if len(chat) == 1 else None
    print(f"  {environment.upper()} model deployments:")
    return _choose("  Approved model deployment", chat, default=default, strict=True)


def prompt_config(repository, presets=None, lookup=None, current_login=None):
    """Collect the minimum inputs; everything else is looked up in one subscription."""
    presets = presets or {}
    lookup = lookup or AzureLookup()
    print(f"Using GitHub repository: {repository}")
    print("Enter the Foundry details assigned to you by the instructor.")

    def check_subscription(value):
        if not SUBSCRIPTION_ID.fullmatch(value):
            raise BootstrapError("Subscription ID must be a GUID.")
        lookup.check_subscription(value)
        return value
    subscription = _ask_valid(
        "Azure subscription ID", check_subscription, presets.get("subscription_id"),
    )
    environments = {}
    endpoints = {}
    for environment in ("dev", "prod"):
        print(f"{environment.upper()} Foundry project")

        def check_endpoint(value, environment=environment):
            account, project = parse_project_endpoint(value)
            group = presets.get(f"{environment}_resource_group") or (
                lookup.account_resource_group(subscription, account)
            )
            return value.strip().rstrip("/"), {
                "resource_group": group, "account_name": account,
                "project_name": project, "model_deployment": "",
            }
        endpoints[environment], values = _ask_valid(
            "  Project endpoint", check_endpoint, presets.get(f"{environment}_endpoint"),
        )
        print(f"  Resource group: {values['resource_group']}")
        values["model_deployment"] = choose_deployment(
            environment, lookup.model_deployments(subscription, values),
            presets.get("model_deployment"),
            environments.get("dev", {}).get("model_deployment"),
        )
        environments[environment] = values
    identity_group = _ask(
        "Resource group for the two GitHub identities",
        presets.get("identity_resource_group"), environments["dev"]["resource_group"],
    )
    available = []
    if not presets.get("agent_name"):
        try:
            available = lookup.prompt_agents(subscription, endpoints["dev"])
        except BootstrapError as error:
            print(f"Could not list dev agents ({error}); enter the name manually.")
    agent_name = choose_agent(available, presets.get("agent_name"))
    reviewers = presets.get("production_reviewers")
    if reviewers is None:
        answer = _ask(
            "GitHub logins required to approve production deploys "
            "(comma-separated, 'none' to skip)",
            default=current_login,
        )
        reviewers = [] if answer.lower() in {"", "none"} else [
            login.strip() for login in answer.split(",") if login.strip()
        ]
    config = {
        "schema_version": 1,
        "repository": repository,
        "subscription_id": subscription,
        "identity_resource_group": identity_group,
        "oidc_subject_format": "auto",
        "agent_name": agent_name,
        "environments": environments,
    }
    if reviewers:
        config["production_reviewers"] = reviewers
    return validate_config(config)


def current_login():
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() or None


def check_repository_admin(repository):
    repo = run(["gh", "api", f"repos/{repository}"])
    if not repo.get("permissions", {}).get("admin"):
        raise BootstrapError(
            f"You need admin access to {repository} to set Actions variables and environments."
        )


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


def account_resource_id(subscription_id, values):
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{values['resource_group']}"
        f"/providers/Microsoft.CognitiveServices/accounts/{values['account_name']}"
    )


def project_endpoint(subscription_id, values, environment="dev"):
    project = run([
        "az", "rest", "--subscription", subscription_id, "--method", "get", "--url",
        f"https://management.azure.com{account_resource_id(subscription_id, values)}"
        f"/projects/{values['project_name']}?api-version=2025-06-01",
        "-o", "json",
    ])
    if project.get("properties", {}).get("provisioningState") != "Succeeded":
        raise BootstrapError(f"{environment} Foundry project is not ready.")
    endpoint = project.get("properties", {}).get("endpoints", {}).get("AI Foundry API")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise BootstrapError(f"{environment} project did not return a Foundry API endpoint.")
    return endpoint


def list_prompt_agents(subscription_id, endpoint):
    """Return saved prompt-agent names using the participant's own Entra sign-in."""
    token = subprocess.run(
        [
            "az", "account", "get-access-token", "--subscription", subscription_id,
            "--resource", "https://ai.azure.com", "--query", "accessToken", "-o", "tsv",
        ],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if not token:
        raise BootstrapError("could not get a Foundry access token")
    request = Request(
        f"{endpoint}/agents?api-version=v1&limit=100",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise BootstrapError(f"listing agents failed: {error}") from error
    return sorted(
        agent["name"] for agent in payload.get("data", [])
        if isinstance(agent, dict) and isinstance(agent.get("name"), str)
        and agent.get("versions", {}).get("latest", {}).get("definition", {}).get("kind")
        == "prompt"
    )


def discover_github(config, repo):
    """Return proposed GitHub settings (hashed) and read-only warnings."""
    warnings = []
    workflow = run([
        "gh", "api", f"repos/{config['repository']}/actions/permissions/workflow",
    ], check=False) or {}
    if not workflow.get("can_approve_pull_request_reviews"):
        warnings.append(
            "Capture cannot open PRs until a repository admin enables Settings -> Actions "
            "-> General -> 'Allow GitHub Actions to create and approve pull requests'."
        )
    reviewers = []
    for login in config.get("production_reviewers", []):
        user = run(["gh", "api", f"users/{login}"])
        reviewers.append({"login": user["login"], "id": user["id"]})
    environment = None
    if reviewers:
        environment = {
            "name": "production",
            "required_reviewers": reviewers,
            "deployment_branch": repo["default_branch"],
        }
    else:
        warnings.append(
            "No production reviewers configured: the production environment separates "
            "the OIDC subject but is not an approval gate."
        )
    return {"production_environment": environment}, warnings


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
        account_id = account_resource_id(sub, values)
        account_resource = run([
            "az", "rest", "--subscription", sub, "--method", "get", "--url",
            f"https://management.azure.com{account_id}?api-version=2025-06-01", "-o", "json",
        ])
        endpoint = project_endpoint(sub, values, environment)
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
    github, warnings = discover_github(config, repo)
    try:
        dev_agents = list_prompt_agents(sub, bindings["dev"]["FOUNDRY_PROJECT_ENDPOINT"])
        if config["agent_name"] not in dev_agents:
            warnings.append(
                f"Agent {config['agent_name']!r} has no saved prompt version in the dev "
                f"project yet; Capture needs one. Found: {dev_agents or 'none'}."
            )
    except BootstrapError as error:
        warnings.append(f"Could not check the dev agent name: {error}.")
    return {
        "repository": repo,
        "resource_groups": group_info,
        "identity_prefix": prefix,
        "oidc_subjects": oidc_subjects,
        "bindings": bindings,
        "github": github,
        "warnings": warnings,
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
        "github": discovered.get("github"),
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
        "github_changes": discovered.get("github"),
        "bicep_parameters": params["parameters"],
        "what_if": what_if,
        "plan_sha256": fingerprint,
        "mode": mode,
    }, indent=2))
    print("\nSummary of proposed changes:")
    for line in summarize_changes(what_if, discovered.get("github")):
        print(f"  {line}")
    for warning in discovered.get("warnings", []):
        print(f"WARNING: {warning}")


def summarize_changes(what_if, github=None):
    """One line per Azure/GitHub change, skipping resources that stay as they are."""
    lines = []
    rerun_noise = False
    for change in (what_if or {}).get("changes", []):
        kind = change.get("changeType")
        if kind in {"Ignore", "NoChange"}:
            continue
        resource = change.get("resourceId", "").split("/providers/", 1)[-1]
        lines.append(f"{kind:<8} {resource.replace('/providers/', ' -> ')}")
        rerun_noise = rerun_noise or (
            kind == "Modify" and "roleAssignments/" in resource
        )
    if rerun_noise:
        lines.append(
            "         (Modify on an existing role assignment is expected on reruns: what-if "
            "cannot resolve identity principal IDs until deployment.)"
        )
    environment = (github or {}).get("production_environment")
    if environment:
        logins = ", ".join(r["login"] for r in environment["required_reviewers"])
        lines.append(
            f"GitHub   environment {environment['name']}: required reviewers {logins}; "
            f"deploys only from {environment['deployment_branch']}"
        )
    lines.append("GitHub   DEV_* and PROD_* repository variables")
    return lines


def configure_production_environment(config, discovered):
    environment = (discovered.get("github") or {}).get("production_environment")
    if not environment:
        return
    repository = config["repository"]
    path = f"repos/{repository}/environments/{environment['name']}"
    body = {
        "reviewers": [
            {"type": "User", "id": reviewer["id"]}
            for reviewer in environment["required_reviewers"]
        ],
        "deployment_branch_policy": {
            "protected_branches": False, "custom_branch_policies": True,
        },
    }
    try:
        run(["gh", "api", "--method", "PUT", path, "--input", "-"], input_text=json.dumps(body))
        policies = run(["gh", "api", f"{path}/deployment-branch-policies"]) or {}
        names = {policy.get("name") for policy in policies.get("branch_policies", [])}
        if environment["deployment_branch"] not in names:
            run([
                "gh", "api", "--method", "POST", f"{path}/deployment-branch-policies",
                "-f", f"name={environment['deployment_branch']}", "-f", "type=branch",
            ])
    except subprocess.CalledProcessError as error:
        raise BootstrapError(
            "Azure access and repository variables were applied, but the production "
            "environment protection could not be set. Required reviewers may not be "
            "available for this repository's plan/visibility; remove production_reviewers "
            f"from the config or configure it manually. {error.stderr or ''}".strip()
        ) from error


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
    configure_production_environment(config, discovered)


SETUP_OPTIONS = {
    "subscription": ("subscription_id", "Azure subscription ID (never listed or searched)"),
    "dev_endpoint": ("dev_endpoint", "Dev Foundry project endpoint"),
    "prod_endpoint": ("prod_endpoint", "Prod Foundry project endpoint"),
    "dev_resource_group": ("dev_resource_group", "Dev resource group (looked up if omitted)"),
    "prod_resource_group": ("prod_resource_group", "Prod resource group (looked up if omitted)"),
    "model_deployment": ("model_deployment", "Approved model deployment name used in dev and prod"),
    "identity_resource_group": (
        "identity_resource_group", "Resource group for the GitHub identities (defaults to dev)",
    ),
    "agent_name": ("agent_name", "Foundry prompt agent to promote"),
}


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
    setup_group = parser.add_argument_group(
        "setup values", "Optional with --setup; anything omitted is prompted for."
    )
    for option, (_, help_text) in SETUP_OPTIONS.items():
        setup_group.add_argument(f"--{option.replace('_', '-')}", help=help_text)
    setup_group.add_argument(
        "--production-reviewer", action="append", metavar="LOGIN",
        help="GitHub login required to approve production deploys (repeatable).",
    )
    setup_group.add_argument(
        "--no-production-reviewers", action="store_true",
        help="Do not configure required reviewers on the production environment.",
    )
    args = parser.parse_args()
    presets = {
        key: getattr(args, option) for option, (key, _) in SETUP_OPTIONS.items()
        if getattr(args, option)
    }
    if args.no_production_reviewers:
        presets["production_reviewers"] = []
    elif args.production_reviewer:
        presets["production_reviewers"] = args.production_reviewer
    try:
        if args.setup and (args.apply or args.confirm_plan):
            raise BootstrapError("--setup cannot be combined with --apply or --confirm-plan.")
        if args.apply != bool(args.confirm_plan):
            raise BootstrapError("--apply requires --confirm-plan, and --confirm-plan requires --apply.")
        if presets and not args.setup:
            raise BootstrapError("Setup values can only be passed together with --setup.")
        if args.production_reviewer and args.no_production_reviewers:
            raise BootstrapError(
                "--production-reviewer cannot be combined with --no-production-reviewers."
            )
        check_prerequisites()
        config_path = Path(args.config)
        if args.setup:
            if config_path.exists():
                raise BootstrapError(
                    f"{config_path} already exists. Run without --setup to preview it, "
                    "edit it, or choose another --config path."
                )
            repository = current_repository()
            check_repository_admin(repository)
            config = prompt_config(repository, presets, current_login=current_login())
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
                f"To apply this exact plan and the GitHub settings above, "
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
