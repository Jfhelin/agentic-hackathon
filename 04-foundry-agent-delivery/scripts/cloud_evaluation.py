"""Score exact validation responses with a Foundry-managed rubric evaluator."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote

from scripts.agent_pipeline import (
    FoundryClient,
    PipelineError,
    digest,
    identifier,
    load_cases,
    read_json,
    required_env,
    validate_config,
    write_json,
)


def validate_quality(quality, cases):
    if (
        not isinstance(quality, dict)
        or set(quality) != {"schema_version", "mode", "dimensions"}
        or type(quality.get("schema_version")) is not int
        or quality["schema_version"] != 1
        or quality.get("mode") != "advisory"
        or not isinstance(quality.get("dimensions"), list)
        or not quality["dimensions"]
    ):
        raise PipelineError("Quality definition must be a schema-v1 advisory rubric.")
    identifiers = []
    for dimension in quality["dimensions"]:
        if (
            not isinstance(dimension, dict)
            or set(dimension) != {
                "id", "description", "weight", "always_applicable",
            }
            or not isinstance(dimension["description"], str)
            or not dimension["description"].strip()
            or type(dimension["weight"]) is not int
            or not 1 <= dimension["weight"] <= 10
            or type(dimension["always_applicable"]) is not bool
        ):
            raise PipelineError("Invalid rubric dimension.")
        identifiers.append(identifier(dimension["id"], "rubric dimension id"))
    if len(set(identifiers)) != len(identifiers):
        raise PipelineError("Rubric dimension IDs must be unique.")
    if not cases:
        raise PipelineError("Rubric evaluation requires at least one case.")
    return quality


def dataset_rows(evidence, cases):
    if len(evidence.get("results", [])) != len(cases):
        raise PipelineError("Verification responses are incomplete.")
    rows = []
    for case, result in zip(cases, evidence["results"], strict=True):
        if result.get("id") != case["id"] or not result.get("response", "").strip():
            raise PipelineError("Verification response IDs or values do not match the dataset.")
        rows.append({
            "case_id": case["id"],
            "query": (
                case["prompt"] + "\n\nReviewed expected behavior for evaluation:\n"
                + case["expected_behavior"]
            ),
            "response": result["response"],
        })
    return rows


def validate_outputs(items, rows, evaluator_version):
    if not isinstance(items, list) or len(items) != len(rows):
        raise PipelineError("Foundry evaluation did not return one output for every case.")
    expected = {row["case_id"]: row for row in rows}
    seen = set()
    for item in items:
        source = item.get("datasource_item", {})
        case_id = source.get("case_id")
        results = item.get("results", [])
        if (
            case_id not in expected
            or case_id in seen
            or source != expected[case_id]
            or item.get("status") not in {"completed", "pass", "fail"}
            or len(results) != 2
            or any(result.get("status") != "completed" for result in results)
        ):
            raise PipelineError("Foundry evaluation returned missing, duplicate, or changed rows.")
        rubric_result = results[0]
        score = rubric_result.get("score")
        if (
            type(score) not in (int, float)
            or not math.isfinite(score)
            or not 0 <= score <= 1
            or not isinstance(rubric_result.get("reason"), str)
            or not rubric_result["reason"].strip()
            or rubric_result.get("properties", {}).get("evaluator_version")
            != evaluator_version
        ):
            raise PipelineError("Foundry returned an incomplete or mismatched rubric result.")
        built_in_result = results[1]
        if not any(
            key in built_in_result
            for key in ("score", "label", "result", "violence")
        ):
            raise PipelineError("Foundry built-in violence evaluation returned no result.")
        seen.add(case_id)


def check_result(result, evidence, cases, quality):
    rows = dataset_rows(evidence, cases)
    if (
        result.get("schema_version") != 1
        or result.get("status") != "completed"
        or result.get("mode") != "advisory"
        or result.get("quality_sha256") != digest(quality)
        or any(
            result.get(key) != evidence.get(key)
            for key in (
                "commit", "config_sha256", "cases_sha256", "quality_sha256",
                "dev_agent", "dev_version",
            )
        )
        or not result.get("evaluation_id")
        or not result.get("run_id")
        or not result.get("evaluator_name")
        or not result.get("evaluator_version")
        or not result.get("report_url")
        or not isinstance(result.get("built_in_evaluators"), list)
        or not result["built_in_evaluators"]
        or any(
            not evaluator.get("name") or not evaluator.get("version")
            for evaluator in result["built_in_evaluators"]
        )
    ):
        raise PipelineError("Cloud scoring evidence does not match the verified release.")
    validate_outputs(result.get("output_items"), rows, result["evaluator_version"])
    return result


def _visible_version(api, name, expected, dimensions, deadline):
    path = f"/evaluators/{quote(name, safe='')}/versions?api-version=v1&limit=100"
    while time.monotonic() < deadline:
        response = api.request(path)
        versions = response.get("data")
        if not isinstance(versions, list):
            raise PipelineError("Evaluator version listing returned an invalid response.")
        match = next((item for item in versions if str(item.get("version")) == expected), None)
        if match:
            detail = match
            if "definition" not in detail:
                detail = api.request(
                    f"/evaluators/{quote(name, safe='')}/versions/"
                    f"{quote(expected, safe='')}?api-version=v1"
                )
            if (
                detail.get("definition", {}).get("type") != "rubric"
                or detail.get("definition", {}).get("dimensions") != dimensions
            ):
                raise PipelineError("Registered evaluator version differs from the reviewed rubric.")
            return
        time.sleep(5)
    raise PipelineError(
        f"Registered evaluator version {expected} did not become visible within the bound."
    )


def evaluate(api, evidence, cases, quality, output, timeout=1800):
    quality_hash = digest(quality)
    run_id = required_env("GITHUB_RUN_ID")
    attempt = required_env("GITHUB_RUN_ATTEMPT")
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[0-9]+", attempt):
        raise PipelineError("GitHub run ID and attempt must be numeric.")
    evaluator_name = (
        f"workshop-{quality_hash[:12]}-{run_id}-{attempt}"
    )
    result = {
        "schema_version": 1,
        "status": "pending",
        "mode": "advisory",
        "commit": evidence["commit"],
        "config_sha256": evidence["config_sha256"],
        "cases_sha256": evidence["cases_sha256"],
        "quality_sha256": quality_hash,
        "dev_agent": evidence["dev_agent"],
        "dev_version": evidence["dev_version"],
        "judge_deployment": required_env("FOUNDRY_MODEL_DEPLOYMENT"),
        "evaluator_name": evaluator_name,
        "evaluator_version": None,
        "built_in_evaluators": [{
            "name": "builtin.violence",
            "version": "service-managed (version not exposed before run)",
        }],
        "output_items": [],
    }
    write_json(output, result)
    try:
        if api is None:
            api = FoundryClient()
        evaluator = api.request(
            f"/evaluators/{quote(evaluator_name, safe='')}/versions?api-version=v1",
            {
                "name": evaluator_name,
                "display_name": "Workshop agent quality (advisory)",
                "description": "Content-addressed rubric for the reviewed workshop dataset.",
                "categories": ["quality"],
                "definition": {"type": "rubric", "dimensions": quality["dimensions"]},
            },
        )
        result["evaluator_version"] = identifier(
            str(evaluator.get("version")), "evaluator version"
        )
        result["evaluator_id"] = evaluator.get("id")
        write_json(output, result)
        _visible_version(
            api, evaluator_name, result["evaluator_version"],
            quality["dimensions"], time.monotonic() + 120,
        )

        evaluation = api.request("/openai/v1/evals", {
            "name": f"Workshop validation {evidence['commit'][:12]}",
            "data_source_config": {
                "type": "custom",
                "item_schema": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "query": {"type": "string"},
                        "response": {"type": "string"},
                    },
                    "required": ["case_id", "query", "response"],
                },
            },
            "testing_criteria": [{
                "type": "azure_ai_evaluator",
                "name": "Workshop quality",
                "evaluator_name": evaluator_name,
                "initialization_parameters": {
                    "deployment_name": result["judge_deployment"],
                },
                "data_mapping": {
                    "query": "{{item.query}}",
                    "response": "{{item.response}}",
                },
            }, {
                "type": "azure_ai_evaluator",
                "name": "Violence safety",
                "evaluator_name": "builtin.violence",
                "data_mapping": {
                    "query": "{{item.query}}",
                    "response": "{{item.response}}",
                },
            }],
        })
        result["evaluation_id"] = evaluation["id"]
        path = f"/openai/v1/evals/{quote(evaluation['id'], safe='')}/runs"
        run = api.request(path, {
            "name": f"{evidence['dev_agent']}:{evidence['dev_version']}",
            "metadata": {
                "commit": evidence["commit"],
                "config_sha256": evidence["config_sha256"],
                "cases_sha256": evidence["cases_sha256"],
                "quality_sha256": quality_hash,
                "agent_version": evidence["dev_version"],
                "evaluator_name": evaluator_name,
                "evaluator_version": result["evaluator_version"],
            },
            "data_source": {
                "type": "jsonl",
                "source": {
                    "type": "file_content",
                    "content": [{"item": row} for row in dataset_rows(evidence, cases)],
                },
            },
        })
        result["run_id"] = run["id"]
        write_json(output, result)
        run_path = f"{path}/{quote(run['id'], safe='')}"
        deadline = time.monotonic() + timeout
        while run.get("status") not in {"completed", "failed", "canceled"}:
            if time.monotonic() >= deadline:
                raise PipelineError("Foundry evaluation polling timed out.")
            time.sleep(10)
            run = api.request(run_path)
        result["run_status"] = run.get("status")
        result["report_url"] = run.get("report_url")
        if run.get("status") != "completed":
            raise PipelineError(
                f"Foundry evaluation ended in {run.get('status')}: {run.get('error')}"
            )
        items = []
        after = None
        while True:
            suffix = "?limit=100" if after is None else f"?limit=100&after={quote(after, safe='')}"
            page = api.request(run_path + "/output_items" + suffix)
            data = page.get("data")
            if not isinstance(data, list):
                raise PipelineError("Foundry output-items response was incomplete.")
            items.extend(data)
            if not page.get("has_more"):
                break
            after = page.get("last_id")
            if not after:
                raise PipelineError("Foundry pagination omitted the continuation ID.")
        result["output_items"] = items
        observed_builtin_versions = {
            str(item["results"][1].get("properties", {}).get("evaluator_version"))
            for item in items
            if item["results"][1].get("properties", {}).get("evaluator_version")
        }
        if len(observed_builtin_versions) > 1:
            raise PipelineError("Foundry returned inconsistent built-in evaluator versions.")
        if observed_builtin_versions:
            result["built_in_evaluators"][0]["version"] = next(iter(observed_builtin_versions))
        result["status"] = "completed"
        check_result(result, evidence, cases, quality)
        write_json(output, result)
        return result
    except (
        PipelineError, OSError, ValueError, KeyError, subprocess.CalledProcessError,
    ) as error:
        result["status"] = "failed"
        result["error"] = str(error)
        write_json(output, result)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--quality", default="evals/quality.json")
    parser.add_argument("--cases", default="evals/cases.json")
    parser.add_argument("--config", default="agent/config.json")
    parser.add_argument("--evidence", default="artifacts/verification.json")
    parser.add_argument("--output", default="artifacts/cloud-evaluation.json")
    args = parser.parse_args()
    try:
        cases = load_cases(args.cases)
        quality = validate_quality(read_json(args.quality), cases)
        if args.validate:
            print("Cloud evaluation rubric is valid.")
            return 0
        config = validate_config(read_json(args.config))
        evidence = read_json(args.evidence)
        from scripts.agent_pipeline import check_evidence
        check_evidence(config, cases, quality, evidence, required_env("GITHUB_SHA"))
        if args.check:
            check_result(read_json(args.output), evidence, cases, quality)
            print("Cloud-evaluation evidence matches the verified commit and inputs.")
            return 0
        result = evaluate(None, evidence, cases, quality, args.output)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as stream:
                stream.write(
                    "\n### Foundry quality evaluation (advisory)\n"
                    f"- Agent: `{result['dev_agent']}:{result['dev_version']}`\n"
                    f"- Evaluator: `{result['evaluator_name']}:{result['evaluator_version']}`\n"
                    f"- Run: `{result['run_id']}`\n"
                    f"- Report: {result['report_url']}\n\n"
                    "Scores are uncalibrated and do not establish release approval.\n\n"
                    "| Case | Advisory score (0-1) | Reason |\n|---|---:|---|\n"
                )
                for item in result["output_items"]:
                    scored = item["results"][0]
                    reason = scored["reason"].replace("|", "\\|").replace("\n", " ")
                    stream.write(
                        f"| {item['datasource_item']['case_id']} | "
                        f"{scored['score']:.3f} | {reason} |\n"
                    )
        print(f"Foundry evaluation completed: {result['report_url']}")
    except (
        PipelineError, OSError, ValueError, KeyError, json.JSONDecodeError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
