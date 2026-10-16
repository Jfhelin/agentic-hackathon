import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import agent_pipeline


ROOT = Path(__file__).resolve().parents[1]


class AgentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = agent_pipeline.read_json(ROOT / "agent/config.json")
        self.cases = agent_pipeline.load_cases(ROOT / "evals/cases.json")
        self.quality = agent_pipeline.read_json(ROOT / "evals/quality.json")

    def test_repository_assets_are_valid_and_portable(self):
        self.assertEqual(agent_pipeline.validate_config(self.config), self.config)
        self.assertEqual([case["id"] for case in self.cases], [
            "exact-ready", "three-fruit-lines",
        ])

    def test_unknown_settings_are_rejected_instead_of_dropped(self):
        definition = dict(self.config["definition"], temperature=0.2)
        with self.assertRaisesRegex(agent_pipeline.PipelineError, "Unsupported definition"):
            agent_pipeline.portable_definition(definition)
        with self.assertRaisesRegex(agent_pipeline.PipelineError, "Unsupported web_search"):
            agent_pipeline.portable_definition({
                "kind": "prompt", "instructions": "help",
                "tools": [{"type": "web_search", "connection_id": "dev-search"}],
            })
        with self.assertRaisesRegex(agent_pipeline.PipelineError, "Only prompt agents"):
            agent_pipeline.portable_definition({
                "kind": "hosted", "instructions": "help", "tools": [],
            })

    def test_deterministic_smoke_checks_only_assert_declared_patterns(self):
        exact_ready = self.cases[0]
        self.assertTrue(agent_pipeline.evaluate_text("READY", exact_ready))
        self.assertFalse(agent_pipeline.evaluate_text("READY, here you go.", exact_ready))
        fruits = self.cases[1]
        self.assertTrue(agent_pipeline.evaluate_text("Apple\nBanana\nPear", fruits))
        self.assertFalse(agent_pipeline.evaluate_text("Apple\nBanana", fruits))

    def test_digest_is_stable_and_sensitive_to_changes(self):
        self.assertEqual(agent_pipeline.digest({"b": 2, "a": 1}),
                         agent_pipeline.digest({"a": 1, "b": 2}))
        self.assertNotEqual(agent_pipeline.digest({"a": 1}), agent_pipeline.digest({"a": 2}))

    def test_foundry_requests_use_the_azure_bearer_token(self):
        client = agent_pipeline.FoundryClient.__new__(agent_pipeline.FoundryClient)
        client.endpoint = "https://example.test/api/projects/project"
        client.token = "azure-access-token"
        with patch(
            "scripts.agent_pipeline.urlopen", return_value=io.BytesIO(b'{"ok": true}')
        ) as urlopen:
            self.assertEqual(client.request("/test"), {"ok": True})
        self.assertEqual(
            urlopen.call_args.args[0].get_header("Authorization"),
            "Bearer azure-access-token",
        )

    def test_model_policy_queries_only_explicit_subscription(self):
        deployment = {
            "properties": {
                "model": {"name": "model-a", "version": "1"},
                "versionUpgradeOption": "NoAutoUpgrade",
            },
            "sku": {"name": "DataZoneStandard"},
        }
        account = {"location": "region-a"}
        env = {
            "AZURE_SUBSCRIPTION_ID": "subscription-a",
            "FOUNDRY_ACCOUNT_ID": (
                "/subscriptions/subscription-a/resourceGroups/rg-a/"
                "providers/Microsoft.CognitiveServices/accounts/account-a"
            ),
            "FOUNDRY_MODEL_DEPLOYMENT": "deployment-a",
            "FOUNDRY_REGION": "region-a",
            "FOUNDRY_MODEL_NAME": "model-a",
            "FOUNDRY_MODEL_VERSION": "1",
            "FOUNDRY_DEPLOYMENT_TYPE": "DataZoneStandard",
            "FOUNDRY_VERSION_UPGRADE_OPTION": "NoAutoUpgrade",
        }
        outputs = [
            type("Result", (), {"stdout": json.dumps(deployment)})(),
            type("Result", (), {"stdout": json.dumps(account)})(),
        ]
        with patch.dict(os.environ, env, clear=True), patch(
            "scripts.agent_pipeline.subprocess.run", side_effect=outputs
        ) as run:
            policy = agent_pipeline.check_model_policy()
        self.assertEqual(policy["model_version"], "1")
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("--subscription", command)
            self.assertEqual(command[command.index("--subscription") + 1], "subscription-a")

    def test_model_policy_mismatch_blocks(self):
        with patch.dict(os.environ, {
            "AZURE_SUBSCRIPTION_ID": "sub",
            "FOUNDRY_ACCOUNT_ID": "/subscriptions/sub/resourceGroups/rg/providers/X",
            "FOUNDRY_MODEL_DEPLOYMENT": "deployment",
            "FOUNDRY_REGION": "expected-region",
            "FOUNDRY_MODEL_NAME": "model",
            "FOUNDRY_MODEL_VERSION": "1",
            "FOUNDRY_DEPLOYMENT_TYPE": "Standard",
            "FOUNDRY_VERSION_UPGRADE_OPTION": "NoAutoUpgrade",
        }, clear=True), patch(
            "scripts.agent_pipeline.subprocess.run",
            side_effect=[
                type("Result", (), {"stdout": json.dumps({
                    "properties": {
                        "model": {"name": "model", "version": "1"},
                        "versionUpgradeOption": "NoAutoUpgrade",
                    },
                    "sku": {"name": "Standard"},
                })})(),
                type("Result", (), {"stdout": json.dumps({"location": "different-region"})})(),
            ],
        ):
            with self.assertRaisesRegex(agent_pipeline.PipelineError, "differs from"):
                agent_pipeline.check_model_policy()

    def test_evidence_requires_exact_commit_hashes_and_complete_results(self):
        from scripts.cloud_evaluation import validate_quality

        quality = validate_quality(self.quality, self.cases)
        evidence = {
            "schema_version": 1,
            "status": "completed",
            "commit": "commit-a",
            "config_sha256": agent_pipeline.digest(self.config),
            "cases_sha256": agent_pipeline.digest(self.cases),
            "quality_sha256": agent_pipeline.digest(quality),
            "dev_agent": "sample-validation",
            "dev_version": "3",
            "results": [
                {"id": self.cases[0]["id"], "passed": True, "response": "READY"},
                {"id": self.cases[1]["id"], "passed": True, "response": "Apple\nBanana\nPear"},
            ],
        }
        agent_pipeline.check_evidence(
            self.config, self.cases, self.quality, evidence, "commit-a"
        )
        for change in ("commit", "hash", "missing", "failed", "partial"):
            tampered = json.loads(json.dumps(evidence))
            if change == "commit":
                tampered["commit"] = "other"
            elif change == "hash":
                tampered["config_sha256"] = "other"
            elif change == "missing":
                tampered["results"].pop()
            elif change == "failed":
                tampered["results"][0]["passed"] = False
            else:
                tampered["status"] = "running"
            with self.subTest(change=change), self.assertRaises(agent_pipeline.PipelineError):
                agent_pipeline.check_evidence(
                    self.config, self.cases, self.quality, tampered, "commit-a"
                )

    def test_failed_model_preflight_preserves_validation_artifact(self):
        args = SimpleNamespace(
            config=str(ROOT / "agent/config.json"),
            cases=str(ROOT / "evals/cases.json"),
            quality=str(ROOT / "evals/quality.json"),
            evidence="",
        )
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "FOUNDRY_AGENT_NAME": "sample",
            "GITHUB_SHA": "commit-a",
        }, clear=True), patch(
            "scripts.agent_pipeline.check_model_policy",
            side_effect=agent_pipeline.PipelineError("policy mismatch"),
        ):
            args.evidence = str(Path(temp) / "verification.json")
            with self.assertRaisesRegex(agent_pipeline.PipelineError, "policy mismatch"):
                agent_pipeline.verify(args)
            evidence = agent_pipeline.read_json(args.evidence)
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["commit"], "commit-a")
        self.assertEqual(evidence["results"], [])
        self.assertIn("policy mismatch", evidence["error"])


if __name__ == "__main__":
    unittest.main()
