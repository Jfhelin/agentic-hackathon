"""Validate validation-run provenance before accessing the production identity."""

import json
import os
import re
import subprocess
import sys


WORKFLOW_PATH = ".github/workflows/validate-agent.yml"


def api(path):
    result = subprocess.run(
        ["gh", "api", path], check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def validate_run(run, workflow, repository, commit, branch, latest_sha, artifacts):
    run_id = str(run.get("id", ""))
    attempt = run.get("run_attempt")
    expected_artifact = f"validation-{run_id}-{attempt}"
    artifact_names = {
        item.get("name") for item in artifacts
        if not item.get("expired") and item.get("workflow_run", {}).get("id") == int(run_id)
    }
    if (
        not run_id.isdigit()
        or type(attempt) is not int
        or run.get("repository", {}).get("full_name") != repository
        or run.get("head_repository", {}).get("full_name") != repository
        or run.get("head_sha") != commit
        or run.get("head_sha") != latest_sha
        or run.get("head_branch") != branch
        or run.get("workflow_id") != workflow.get("id")
        or workflow.get("path") != WORKFLOW_PATH
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or expected_artifact not in artifact_names
    ):
        raise ValueError(
            "Run must be the successful latest attempt of this repository's manual "
            "validation workflow for the current default-branch commit, with its evidence artifact."
        )
    return expected_artifact


def main():
    try:
        run_id = os.environ["VERIFICATION_RUN_ID"]
        if not re.fullmatch(r"[0-9]+", run_id):
            raise ValueError("Validation run ID must be numeric.")
        repository = os.environ["GITHUB_REPOSITORY"]
        commit = os.environ["GITHUB_SHA"]
        workflow = api(f"repos/{repository}/actions/workflows/validate-agent.yml")
        run_info = api(f"repos/{repository}/actions/runs/{run_id}")
        repo = api(f"repos/{repository}")
        branch = repo["default_branch"]
        latest_sha = api(
            f"repos/{repository}/commits/{branch}"
        )["sha"]
        artifacts = api(
            f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"
        ).get("artifacts", [])
        artifact_name = validate_run(
            run_info, workflow, repository, commit, branch, latest_sha, artifacts
        )
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"artifact_name={artifact_name}\n")
        print(f"Accepted validation run {run_id} for current commit {commit}.")
        return 0
    except (
        ValueError, KeyError, OSError, subprocess.CalledProcessError, json.JSONDecodeError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
