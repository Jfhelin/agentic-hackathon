import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from scripts import agent_pipeline, cloud_evaluation


ROOT = Path(__file__).resolve().parents[1]


class CloudEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.cases = agent_pipeline.load_cases(ROOT / "evals/cases.json")
        self.quality = cloud_evaluation.validate_quality(
            agent_pipeline.read_json(ROOT / "evals/quality.json"), self.cases
        )
        self.evidence = {
            "schema_version": 1,
            "status": "completed",
            "commit": "commit-a",
            "config_sha256": "config-hash",
            "cases_sha256": agent_pipeline.digest(self.cases),
            "quality_sha256": agent_pipeline.digest(self.quality),
            "dev_agent": "sample-validation",
            "dev_version": "3",
            "results": [
                {"id": self.cases[0]["id"], "passed": True, "response": "READY"},
                {"id": self.cases[1]["id"], "passed": True, "response": "Apple\nBanana\nPear"},
            ],
        }
        self.rows = cloud_evaluation.dataset_rows(self.evidence, self.cases)

    def make_items(self, evaluator_version="1"):
        return [{
            "status": "completed",
            "datasource_item": row,
            "results": [
                {
                    "score": 0.85,
                    "reason": "Clear and follows the requested format.",
                    "status": "completed",
                    "properties": {"evaluator_version": evaluator_version},
                },
                {
                    "label": "safe",
                    "status": "completed",
                    "properties": {"evaluator_version": "builtin-v1"},
                },
            ],
        } for row in self.rows]

    def test_dataset_contains_reviewed_expectations_and_exact_responses(self):
        self.assertIn(
            self.cases[0]["expected_behavior"], self.rows[0]["query"]
        )
        self.assertEqual(self.rows[0]["response"], "READY")

    def test_output_completeness_and_evaluator_version_are_blocking(self):
        items = self.make_items()
        cloud_evaluation.validate_outputs(items, self.rows, "1")
        with self.assertRaisesRegex(cloud_evaluation.PipelineError, "mismatched rubric"):
            cloud_evaluation.validate_outputs(self.make_items("2"), self.rows, "1")
        with self.assertRaisesRegex(cloud_evaluation.PipelineError, "one output"):
            cloud_evaluation.validate_outputs(items[:1], self.rows, "1")
        missing_builtin = self.make_items()
        missing_builtin[0]["results"].pop()
        with self.assertRaisesRegex(cloud_evaluation.PipelineError, "missing, duplicate"):
            cloud_evaluation.validate_outputs(missing_builtin, self.rows, "1")

    def test_registered_evaluator_version_is_read_back_and_definition_checked(self):
        api = Mock()
        api.request.return_value = {
            "data": [{
                "version": "1",
                "definition": {"type": "rubric", "dimensions": self.quality["dimensions"]},
            }]
        }
        cloud_evaluation._visible_version(
            api, "workshop-rubric", "1", self.quality["dimensions"], time.monotonic() + 10
        )
        with self.assertRaisesRegex(cloud_evaluation.PipelineError, "differs"):
            api.request.return_value["data"][0]["definition"]["dimensions"] = []
            cloud_evaluation._visible_version(
                api, "workshop-rubric", "1", self.quality["dimensions"], time.monotonic() + 10
            )

    def test_live_evaluation_scores_collected_responses_and_records_versions(self):
        items = self.make_items()
        api = Mock()
        api.request.side_effect = [
            {"version": "1", "id": "evaluator-1"},
            {"data": [{
                "version": "1",
                "definition": {"type": "rubric", "dimensions": self.quality["dimensions"]},
            }]},
            {"id": "evaluation-1"},
            {"id": "run-1", "status": "completed", "report_url": "https://ai.azure.com/run/1"},
            {"data": items, "has_more": False},
        ]
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "1",
            "FOUNDRY_MODEL_DEPLOYMENT": "judge",
        }, clear=True), patch("scripts.cloud_evaluation.time.sleep"):
            result = cloud_evaluation.evaluate(
                api, self.evidence, self.cases, self.quality,
                Path(temp) / "cloud.json", timeout=5,
            )
            persisted = agent_pipeline.read_json(Path(temp) / "cloud.json")
        self.assertEqual(result["evaluator_name"], f"workshop-{self.evidence['quality_sha256'][:12]}-42-1")
        self.assertEqual(result["evaluator_version"], "1")
        self.assertEqual(result["built_in_evaluators"][0]["version"], "builtin-v1")
        self.assertEqual(persisted["run_id"], "run-1")
        self.assertFalse(any("/responses" in call.args[0] for call in api.request.call_args_list))
        self.assertEqual(api.request.call_args_list[3].args[1]["data_source"]["source"]["content"][0]["item"]["response"], "READY")

    def test_missing_registered_version_preserves_failed_partial_evidence(self):
        api = Mock()
        api.request.side_effect = [
            {"version": "2", "id": "evaluator-2"},
            {"data": [{"version": "1", "definition": {
                "type": "rubric", "dimensions": self.quality["dimensions"],
            }}]},
        ]
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "GITHUB_RUN_ID": "43",
            "GITHUB_RUN_ATTEMPT": "1",
            "FOUNDRY_MODEL_DEPLOYMENT": "judge",
        }, clear=True), patch(
            "scripts.cloud_evaluation.time.monotonic", side_effect=[0, 200]
        ):
            path = Path(temp) / "cloud.json"
            with self.assertRaisesRegex(cloud_evaluation.PipelineError, "did not become visible"):
                cloud_evaluation.evaluate(
                    api, self.evidence, self.cases, self.quality, path, timeout=5
                )
            partial = agent_pipeline.read_json(path)
        self.assertEqual(partial["status"], "failed")
        self.assertEqual(partial["evaluator_version"], "2")
        self.assertIn("did not become visible", partial["error"])

    def test_authentication_failure_still_writes_cloud_evaluation_evidence(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "GITHUB_RUN_ID": "44",
            "GITHUB_RUN_ATTEMPT": "1",
            "FOUNDRY_MODEL_DEPLOYMENT": "judge",
        }, clear=True), patch(
            "scripts.cloud_evaluation.FoundryClient",
            side_effect=agent_pipeline.PipelineError("token request failed"),
        ):
            path = Path(temp) / "cloud.json"
            with self.assertRaisesRegex(agent_pipeline.PipelineError, "token request failed"):
                cloud_evaluation.evaluate(
                    None, self.evidence, self.cases, self.quality, path, timeout=5
                )
            partial = agent_pipeline.read_json(path)
        self.assertEqual(partial["status"], "failed")
        self.assertEqual(partial["commit"], "commit-a")
        self.assertIn("token request failed", partial["error"])

    def test_cloud_result_rejects_wrong_commit_hash_version_and_missing_evaluator(self):
        result = {
            "schema_version": 1,
            "status": "completed",
            "mode": "advisory",
            "commit": self.evidence["commit"],
            "config_sha256": self.evidence["config_sha256"],
            "cases_sha256": self.evidence["cases_sha256"],
            "quality_sha256": self.evidence["quality_sha256"],
            "dev_agent": self.evidence["dev_agent"],
            "dev_version": self.evidence["dev_version"],
            "evaluator_name": "workshop-rubric",
            "evaluator_version": "1",
            "evaluation_id": "evaluation-1",
            "run_id": "run-1",
            "report_url": "https://ai.azure.com/run/1",
            "built_in_evaluators": [{
                "name": "builtin.violence", "version": "builtin-v1",
            }],
            "output_items": self.make_items(),
        }
        cloud_evaluation.check_result(result, self.evidence, self.cases, self.quality)
        for field, value in (
            ("commit", "other"),
            ("evaluator_version", "2"),
            ("report_url", None),
            ("built_in_evaluators", []),
        ):
            tampered = json.loads(json.dumps(result))
            tampered[field] = value
            with self.subTest(field=field), self.assertRaises(cloud_evaluation.PipelineError):
                cloud_evaluation.check_result(
                    tampered, self.evidence, self.cases, self.quality
                )


if __name__ == "__main__":
    unittest.main()
