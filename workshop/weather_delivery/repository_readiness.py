"""Read-only GitHub lab preflight; never contacts Azure or prints variable values."""

import argparse
import json
import re
import subprocess
import sys
from urllib.parse import quote


REQUIRED_VARIABLES = (
    "WEATHER_AZURE_CLIENT_ID",
    "WEATHER_AZURE_TENANT_ID",
    "WEATHER_AZURE_SUBSCRIPTION_ID",
    "WEATHER_CAPTURE_OIDC_SUBJECT",
    "WEATHER_FOUNDRY_PROJECT_ENDPOINT",
    "WEATHER_DEV_AGENT_NAME",
    "WEATHER_PROD_AGENT_NAME",
    "WEATHER_PROD_STABLE_ENDPOINT",
)
REQUIRED_WORKFLOWS = (
    ".github/workflows/weather-delivery-check.yml",
    ".github/workflows/weather-delivery-cloud.yml",
    ".github/workflows/weather-delivery-deploy.yml",
)


def _read_api(path, *, paginate=False):
    command = ["gh", "api", path]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=30
        )
    except OSError:
        return "unknown: gh unavailable", None
    except subprocess.TimeoutExpired:
        return "unknown: GitHub request timed out", None
    if result.returncode:
        match = re.search(r"HTTP (\d{3})", result.stderr)
        reason = {
            "401": "sign-in required",
            "403": "denied",
            "404": "unavailable or hidden; not evidence of absence",
        }.get(match.group(1) if match else None, "request failed")
        return f"unknown: {reason}", None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "unknown: invalid API response", None
    if not isinstance(payload, list if paginate else dict):
        return "unknown: invalid API response shape", None
    return "read", payload


def _listed_items(pages, key):
    if any(
        not isinstance(page, dict)
        or not isinstance(page.get(key), list)
        or any(not isinstance(item, dict) for item in page[key])
        for page in pages
    ):
        raise ValueError(f"Invalid {key} listing; absence cannot be established")
    return [item for page in pages for item in page[key]]


def inspect_repository(repository, expected_default_branch, delivery_branch=None):
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository) is None
        or repository.rsplit("/", 1)[-1] in {".", ".."}
    ):
        raise ValueError("Use an explicit OWNER/REPOSITORY name")
    if not expected_default_branch.strip():
        raise ValueError("Confirm the intended default branch")
    if delivery_branch is not None and (
        not delivery_branch.strip() or delivery_branch != delivery_branch.strip()
    ):
        raise ValueError("Confirm the exact delivery branch")
    selected_branch = delivery_branch or expected_default_branch
    prefix = f"repos/{repository}"
    report = {
        "repository": repository,
        "expected_default_branch": expected_default_branch,
        "expected_delivery_branch": selected_branch,
        "scope": "GitHub metadata only; no Azure access, runtime test or deployment",
        "checks": {},
        "cloud_variables": {"status": "unknown: repository access not verified"},
        "live_readiness": "not-verified",
    }
    checks = report["checks"]
    state, metadata = _read_api(prefix)
    checks["repository_access"] = "passed" if state == "read" else state
    if state != "read":
        return report
    actual_branch = metadata.get("default_branch")
    report["actual_default_branch"] = actual_branch
    checks["default_branch"] = (
        "unknown: default branch not returned" if not actual_branch else
        "passed" if actual_branch == expected_default_branch else
        "blocked: default branch differs from intended workshop branch"
    )
    if delivery_branch is not None:
        state, branch = _read_api(f"{prefix}/branches/{quote(selected_branch, safe='')}")
        checks["delivery_branch_exists"] = (
            "passed" if state == "read" and branch.get("name") == selected_branch else
            "unknown: branch name not returned or differs" if state == "read" else state
        )
    admin = metadata.get("permissions", {}).get("admin")
    checks["administrator_access"] = (
        "passed" if admin is True else
        "blocked: caller needs the repository owner/facilitator for setup" if admin is False else
        "unknown: administrator permission not returned"
    )
    state, permissions = _read_api(f"{prefix}/actions/permissions")
    checks["actions_enabled"] = (
        "passed" if permissions.get("enabled") is True else
        "blocked: Actions disabled" if permissions.get("enabled") is False else
        "unknown: Actions enabled state not returned"
    ) if state == "read" else state
    if state == "read":
        report["actions_policy"] = permissions.get("allowed_actions", "unknown")
    state, pages = _read_api(f"{prefix}/actions/workflows?per_page=100", paginate=True)
    if state == "read":
        workflows = {
            item["path"]: item.get("state")
            for item in _listed_items(pages, "workflows")
        }
        for path in REQUIRED_WORKFLOWS:
            checks[path] = (
                "passed" if workflows.get(path) == "active" else
                "blocked: workflow absent from successful listing" if path not in workflows else
                f"blocked: workflow state is {workflows[path]}"
            )
    else:
        checks["workflow_availability"] = state
    for path in REQUIRED_WORKFLOWS:
        state, file_metadata = _read_api(
            f"{prefix}/contents/{path}?ref={quote(expected_default_branch, safe='')}"
        )
        checks[f"{path} on default branch"] = (
            "passed" if state == "read" and file_metadata.get("type") == "file" else
            "unknown: workflow file metadata not returned" if state == "read" else state
        )
    state, pages = _read_api(f"{prefix}/actions/variables?per_page=100", paginate=True)
    if state == "read":
        variables = {item["name"]: item.get("value") for item in _listed_items(pages, "variables")}
        names = set(variables)
        missing = sorted(set(REQUIRED_VARIABLES) - names)
        report["cloud_variables"] = {
            "status": "missing" if missing else "configured-not-authenticated",
            "missing_names": missing,
        }
        configured_branch = variables.get("WEATHER_DELIVERY_BRANCH") or actual_branch
        checks["delivery_branch_setting"] = (
            "unknown: default branch not returned" if not configured_branch else
            "passed" if configured_branch == selected_branch else
            "blocked: configured delivery branch differs from intended branch"
        )
        subject = variables.get("WEATHER_CAPTURE_OIDC_SUBJECT")
        checks["oidc_subject_setting"] = (
            "unknown: OIDC subject variable missing or unreadable" if not subject else
            "passed" if subject == f"repo:{repository}:ref:refs/heads/{selected_branch}" else
            "blocked: OIDC subject does not match intended repository and delivery branch"
        )
    else:
        report["cloud_variables"] = {"status": state}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--expected-default-branch", required=True)
    parser.add_argument("--delivery-branch", help="Exact intended branch; defaults to the expected default branch")
    args = parser.parse_args(argv)
    try:
        report = inspect_repository(args.repository, args.expected_default_branch, args.delivery_branch)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        print(f"BLOCKED: repository metadata could not be interpreted: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(
        any(value != "passed" for value in report["checks"].values())
        or report["cloud_variables"]["status"] != "configured-not-authenticated"
    )


if __name__ == "__main__":
    raise SystemExit(main())
