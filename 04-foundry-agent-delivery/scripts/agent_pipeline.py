"""Capture, verify, and promote a portable Microsoft Foundry prompt agent."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class PipelineError(RuntimeError):
    """An expected pipeline or remote-service failure."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise PipelineError(f"Missing environment variable: {name}")
    return value


def identifier(value, label="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        raise PipelineError(f"Invalid {label}: {value!r}")
    return value


def portable_definition(definition):
    if not isinstance(definition, dict):
        raise PipelineError("Foundry agent definition must be an object.")
    unknown = set(definition) - {"kind", "model", "instructions", "tools"}
    if unknown:
        raise PipelineError(f"Unsupported definition settings: {sorted(unknown)}")
    if definition.get("kind") != "prompt":
        raise PipelineError("Only prompt agents are supported by this lab.")
    instructions = definition.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise PipelineError("Prompt-agent instructions must be a nonempty string.")
    tools = definition.get("tools", [])
    if not isinstance(tools, list):
        raise PipelineError("Agent tools must be a list.")
    portable_tools = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "web_search":
            raise PipelineError(
                "Only built-in web_search is supported; connection-backed or "
                "custom tools need explicit environment bindings."
            )
        unsupported = set(tool) - {
            "type", "search_context_size", "filters", "user_location",
        }
        if unsupported:
            raise PipelineError(f"Unsupported web_search settings: {sorted(unsupported)}")
        portable_tools.append(tool)
    portable = {"kind": "prompt", "instructions": instructions}
    if "tools" in definition:
        portable["tools"] = portable_tools
    return portable


def validate_config(config):
    if (
        not isinstance(config, dict)
        or set(config) != {"schema_version", "definition"}
        or type(config["schema_version"]) is not int
        or config["schema_version"] != 1
    ):
        raise PipelineError("Unexpected agent configuration schema.")
    definition = portable_definition(config["definition"])
    if definition != config["definition"]:
        raise PipelineError("Canonical agent configuration is not portable.")
    return config


def load_cases(path):
    cases = read_json(path)
    if not isinstance(cases, list) or not cases:
        raise PipelineError("At least one evaluation case is required.")
    seen = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or set(case) != {
                "id", "prompt", "expected_behavior", "must_match", "must_not_match",
            }
        ):
            raise PipelineError("Unexpected evaluation case schema.")
        case_id = identifier(case["id"], "evaluation case id")
        if case_id in seen:
            raise PipelineError(f"Duplicate evaluation case id: {case_id}")
        seen.add(case_id)
        for field in ("prompt", "expected_behavior"):
            if not isinstance(case[field], str) or not case[field].strip():
                raise PipelineError(f"Evaluation {field} must be a nonempty string.")
        for field in ("must_match", "must_not_match"):
            patterns = case[field]
            if not isinstance(patterns, list) or not all(
                isinstance(pattern, str) for pattern in patterns
            ):
                raise PipelineError(f"{field} must be a list of regular expressions.")
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as error:
                    raise PipelineError(f"Invalid {field} expression: {error}") from error
        if not case["must_match"]:
            raise PipelineError(f"Evaluation {case_id} needs a positive smoke assertion.")
    return cases


def evaluate_text(text, case):
    return (
        all(re.search(pattern, text, re.I) for pattern in case["must_match"])
        and not any(re.search(pattern, text, re.I) for pattern in case["must_not_match"])
    )


class FoundryClient:
    def __init__(self):
        self.subscription = required_env("AZURE_SUBSCRIPTION_ID")
        self.endpoint = required_env("FOUNDRY_PROJECT_ENDPOINT").rstrip("/")
        if not re.fullmatch(
            r"https://[a-zA-Z0-9.-]+/api/projects/[A-Za-z0-9_.-]+",
            self.endpoint,
        ):
            raise PipelineError("FOUNDRY_PROJECT_ENDPOINT must be a Foundry project endpoint.")
        token = subprocess.run(
            [
                "az", "account", "get-access-token", "--subscription", self.subscription,
                "--resource", "https://ai.azure.com", "--query", "accessToken", "-o", "tsv",
            ],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if not token:
            raise PipelineError("Azure CLI returned an empty Foundry access token.")
        self.token = token

    def request(self, path, body=None, *, method=None):
        request = Request(
            self.endpoint + path,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method=method or ("GET" if body is None else "POST"),
        )
        for attempt in range(3):
            try:
                with urlopen(request, timeout=120) as response:
                    return json.load(response)
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                retryable = (
                    request.method == "GET" and (error.code == 429 or 500 <= error.code < 600)
                ) or (
                    error.code == 429 and path == "/openai/v1/responses"
                )
                if not retryable or attempt == 2:
                    raise PipelineError(f"Foundry HTTP {error.code}: {detail}") from error
                retry_after = error.headers.get("Retry-After", "10")
                delay = int(retry_after) if retry_after.isdigit() else 2 ** attempt
                if delay > 60:
                    raise PipelineError(
                        f"Foundry retry delay exceeds the 60-second bound: {detail}"
                    ) from error
                print(f"Foundry rate limit; retrying in {max(delay, 1)} seconds.", file=sys.stderr)
                time.sleep(max(delay, 1))
            except (TimeoutError, URLError) as error:
                if request.method != "GET" or attempt == 2:
                    raise PipelineError(f"Foundry request failed: {error}") from error
                time.sleep(2 ** attempt)
        raise PipelineError("Foundry request retry budget exhausted.")

    def latest_saved_version(self, name):
        result = self.request(
            f"/agents/{quote(identifier(name, 'agent name'))}/versions"
            "?api-version=v1&order=desc&limit=100&include_drafts=false"
        )
        versions = result.get("data")
        if not isinstance(versions, list):
            raise PipelineError("Foundry returned no version list.")
        saved = [item for item in versions if item.get("is_draft") is not True]
        if not saved:
            raise PipelineError("No saved, non-draft dev agent version was found.")
        saved.sort(key=lambda item: int(item["version"]), reverse=True)
        return saved[0]

    def create_version(self, name, definition):
        return self.request(
            f"/agents/{quote(identifier(name, 'agent name'))}/versions?api-version=v1",
            {"definition": definition},
        )

    def respond(self, name, version, prompt):
        started = time.monotonic()
        result = self.request("/openai/v1/responses", {
            "agent_reference": {
                "type": "agent_reference",
                "name": identifier(name, "agent name"),
                "version": identifier(str(version), "agent version"),
            },
            "input": prompt,
            "store": False,
            "max_output_tokens": 4096,
        })
        if result.get("status") != "completed" or result.get("error"):
            raise PipelineError(f"Explicit agent version did not complete: {result.get('status')}")
        text = "\n".join(
            part["text"]
            for item in result.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        if not text.strip():
            raise PipelineError("Foundry returned an empty response.")
        return {
            "response": text,
            "latency_seconds": round(time.monotonic() - started, 3),
            "usage": result.get("usage"),
        }


def check_model_policy():
    subscription = required_env("AZURE_SUBSCRIPTION_ID")
    account_id = required_env("FOUNDRY_ACCOUNT_ID")
    if not account_id.startswith(f"/subscriptions/{subscription}/resourceGroups/"):
        raise PipelineError("Foundry account is outside the configured subscription.")
    deployment = quote(required_env("FOUNDRY_MODEL_DEPLOYMENT"), safe="")
    api_version = "2025-06-01"
    result = subprocess.run(
        [
            "az", "rest", "--subscription", subscription, "--method", "get", "--url",
            f"https://management.azure.com{account_id}/deployments/{deployment}"
            f"?api-version={api_version}",
            "-o", "json",
        ],
        check=True, capture_output=True, text=True,
    )
    model = json.loads(result.stdout)
    account_result = subprocess.run(
        [
            "az", "rest", "--subscription", subscription, "--method", "get", "--url",
            f"https://management.azure.com{account_id}?api-version={api_version}",
            "-o", "json",
        ],
        check=True, capture_output=True, text=True,
    )
    region = json.loads(account_result.stdout).get("location")
    props = model.get("properties", {})
    expected = {
        "region": required_env("FOUNDRY_REGION"),
        "model_name": required_env("FOUNDRY_MODEL_NAME"),
        "model_version": required_env("FOUNDRY_MODEL_VERSION"),
        "deployment_type": required_env("FOUNDRY_DEPLOYMENT_TYPE"),
        "version_upgrade_option": required_env("FOUNDRY_VERSION_UPGRADE_OPTION"),
    }
    actual = {
        "region": region,
        "model_name": props.get("model", {}).get("name"),
        "model_version": props.get("model", {}).get("version"),
        "deployment_type": model.get("sku", {}).get("name"),
        "version_upgrade_option": props.get("versionUpgradeOption"),
    }
    if actual != expected:
        raise PipelineError(
            "Model deployment differs from the approved participant policy: "
            + json.dumps({"expected": expected, "actual": actual}, sort_keys=True)
        )
    return actual


def capture(args):
    name = required_env("FOUNDRY_AGENT_NAME")
    expected_model = required_env("FOUNDRY_MODEL_DEPLOYMENT")
    api = FoundryClient()
    version = api.latest_saved_version(name)
    definition = version.get("definition")
    if not isinstance(definition, dict) or definition.get("model") != expected_model:
        raise PipelineError("Latest saved dev model binding differs from participant policy.")
    config = validate_config({
        "schema_version": 1,
        "definition": portable_definition(definition),
    })
    target = Path(args.config)
    before = target.read_text(encoding="utf-8") if target.exists() else ""
    after = json.dumps(config, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="reviewed agent configuration",
        tofile=f"saved dev version {version['version']}",
    ))
    write_json(target, config)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    report = (
        "# Development agent capture\n\n"
        f"- Agent: `{name}`\n- Saved dev version: `{version['version']}`\n"
        f"- Canonical configuration SHA-256: `{digest(config)}`\n"
        "- Model binding was verified separately; conversations and user data were not exported.\n\n"
        + ("```diff\n" + diff + "```\n" if diff else "No configuration changes.\n")
    )
    Path(args.report).write_text(report, encoding="utf-8")
    print(f"Captured {name}:{version['version']} ({digest(config)}).")


def verify(args):
    config = validate_config(read_json(args.config))
    cases = load_cases(args.cases)
    quality = read_json(args.quality)
    from scripts.cloud_evaluation import validate_quality
    validate_quality(quality, cases)
    name = identifier(required_env("FOUNDRY_AGENT_NAME") + "-validation", "agent name")
    evidence = {
        "schema_version": 1,
        "status": "running",
        "commit": required_env("GITHUB_SHA"),
        "config_sha256": digest(config),
        "cases_sha256": digest(cases),
        "quality_sha256": digest(quality),
        "dev_agent": name,
        "dev_version": None,
        "model_policy": None,
        "results": [],
    }
    write_json(args.evidence, evidence)
    try:
        evidence["model_policy"] = check_model_policy()
        write_json(args.evidence, evidence)
        api = FoundryClient()
        definition = {
            **config["definition"],
            "model": required_env("FOUNDRY_MODEL_DEPLOYMENT"),
        }
        created = api.create_version(name, definition)
        version = identifier(str(created.get("version")), "agent version")
        evidence["dev_version"] = version
        write_json(args.evidence, evidence)
        for case in cases:
            item = {"id": case["id"], "passed": False}
            try:
                response = api.respond(name, version, case["prompt"])
                item.update(response)
                item["passed"] = evaluate_text(response["response"], case)
            except PipelineError as error:
                item["error"] = str(error)
            evidence["results"].append(item)
            write_json(args.evidence, evidence)
        if len(evidence["results"]) != len(cases) or not all(
            result.get("passed") is True for result in evidence["results"]
        ):
            raise PipelineError("One or more agent invocations or deterministic checks failed.")
        evidence["status"] = "completed"
        write_json(args.evidence, evidence)
        print(f"All {len(cases)} deterministic checks passed for {name}:{version}.")
    except (
        PipelineError, ValueError, KeyError, OSError, subprocess.CalledProcessError,
    ) as error:
        evidence["status"] = "failed"
        evidence["error"] = str(error)
        write_json(args.evidence, evidence)
        raise


def check_evidence(config, cases, quality, evidence, commit):
    expected_ids = [case["id"] for case in cases]
    results = evidence.get("results", [])
    if (
        evidence.get("schema_version") != 1
        or evidence.get("status") != "completed"
        or evidence.get("commit") != commit
        or evidence.get("config_sha256") != digest(config)
        or evidence.get("cases_sha256") != digest(cases)
        or evidence.get("quality_sha256") != digest(quality)
        or [result.get("id") for result in results] != expected_ids
        or not all(
            result.get("passed") is True
            and isinstance(result.get("response"), str)
            and result["response"].strip()
            for result in results
        )
    ):
        raise PipelineError(
            "Verification evidence is incomplete or does not match this commit and inputs."
        )
    return evidence


def deploy(args):
    config = validate_config(read_json(args.config))
    cases = load_cases(args.cases)
    quality = read_json(args.quality)
    from scripts.cloud_evaluation import check_result, validate_quality
    validate_quality(quality, cases)
    evidence = check_evidence(
        config, cases, quality, read_json(args.evidence), required_env("GITHUB_SHA")
    )
    check_result(read_json(args.cloud_evidence), evidence, cases, quality)
    check_model_policy()
    api = FoundryClient()
    name = identifier(required_env("FOUNDRY_AGENT_NAME"), "agent name")
    version_info = api.create_version(name, {
        **config["definition"],
        "model": required_env("FOUNDRY_MODEL_DEPLOYMENT"),
    })
    version = identifier(str(version_info.get("version")), "agent version")
    release = {
        "schema_version": 1,
        "commit": required_env("GITHUB_SHA"),
        "config_sha256": digest(config),
        "validation_run_id": required_env("VERIFICATION_RUN_ID"),
        "agent": name,
        "version": version,
        "smoke_passed": False,
    }
    write_json(args.release, release)
    try:
        response = api.respond(name, version, cases[0]["prompt"])
        release["smoke_response"] = response["response"]
        release["smoke_passed"] = evaluate_text(response["response"], cases[0])
    except PipelineError as error:
        release["smoke_error"] = str(error)
        write_json(args.release, release)
        raise
    write_json(args.release, release)
    if not release["smoke_passed"]:
        raise PipelineError("Production smoke check failed; do not route clients to this version.")
    print(f"Created and smoke-tested production version {name}:{version}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["validate", "capture", "verify", "evidence", "deploy"]
    )
    parser.add_argument("--config", default="agent/config.json")
    parser.add_argument("--cases", default="evals/cases.json")
    parser.add_argument("--quality", default="evals/quality.json")
    parser.add_argument("--report", default="artifacts/capture.md")
    parser.add_argument("--evidence", default="artifacts/verification.json")
    parser.add_argument("--cloud-evidence", default="artifacts/cloud-evaluation.json")
    parser.add_argument("--release", default="artifacts/release.json")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            validate_config(read_json(args.config))
            load_cases(args.cases)
            from scripts.cloud_evaluation import validate_quality
            validate_quality(read_json(args.quality), load_cases(args.cases))
            print("Portable agent configuration and evaluation assets are valid.")
        elif args.command == "capture":
            capture(args)
        elif args.command == "verify":
            verify(args)
        elif args.command == "evidence":
            from scripts.cloud_evaluation import check_result, validate_quality
            config = validate_config(read_json(args.config))
            cases = load_cases(args.cases)
            quality = read_json(args.quality)
            validate_quality(quality, cases)
            evidence = check_evidence(
                config, cases, quality, read_json(args.evidence), required_env("GITHUB_SHA")
            )
            check_result(read_json(args.cloud_evidence), evidence, cases, quality)
            print("Both deterministic and cloud-evaluation evidence match the release.")
        else:
            deploy(args)
    except (
        PipelineError, ValueError, KeyError, OSError, subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
