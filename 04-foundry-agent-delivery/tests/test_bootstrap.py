import copy
import unittest
from datetime import datetime, timezone

from scripts import bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "schema_version": 1,
            "repository": "workshop/participant",
            "subscription_id": "00000000-0000-0000-0000-000000000000",
            "identity_resource_group": "participant-identities",
            "oidc_subject_format": "auto",
            "agent_name": "workshop-agent",
            "environments": {
                "dev": {
                    "resource_group": "participant-dev",
                    "account_name": "dev-account",
                    "project_name": "dev-project",
                    "model_deployment": "dev-model",
                },
                "prod": {
                    "resource_group": "participant-prod",
                    "account_name": "prod-account",
                    "project_name": "prod-project",
                    "model_deployment": "prod-model",
                },
            },
        }
        self.repo = {
            "full_name": "workshop/participant",
            "name": "participant",
            "id": 456,
            "owner": {"login": "workshop", "id": 123},
            "created_at": "2026-07-16T00:00:00Z",
            "default_branch": "main",
        }

    def test_minimal_participant_config_is_valid(self):
        self.assertEqual(bootstrap.validate_config(self.config), self.config)

    def test_interactive_config_collects_only_explicit_resource_values(self):
        answers = iter([
            self.config["subscription_id"],
            self.config["identity_resource_group"],
            "",
            "participant-dev",
            "dev-account",
            "dev-project",
            "dev-model",
            "participant-prod",
            "prod-account",
            "prod-project",
            "prod-model",
        ])
        with unittest.mock.patch("builtins.input", side_effect=lambda _prompt: next(answers)):
            config = bootstrap.prompt_config("workshop/participant")
        self.assertEqual(config["repository"], "workshop/participant")
        self.assertEqual(config["agent_name"], "workshop-agent")
        self.assertEqual(config["environments"]["prod"]["model_deployment"], "prod-model")

    def test_unknown_or_same_environment_resource_values_are_rejected(self):
        config = copy.deepcopy(self.config)
        config["extra"] = "not allowed"
        with self.assertRaisesRegex(bootstrap.BootstrapError, "schema"):
            bootstrap.validate_config(config)
        config = copy.deepcopy(self.config)
        config["environments"]["prod"]["account_name"] = "dev-account"
        with self.assertRaisesRegex(bootstrap.BootstrapError, "distinct"):
            bootstrap.validate_config(config)

    def test_oidc_subject_uses_repository_and_owner_ids_for_immutable_default(self):
        self.assertEqual(bootstrap.subjects(self.config, self.repo), {
            "dev": "repo:workshop@123/participant@456:ref:refs/heads/main",
            "prod": "repo:workshop@123/participant@456:environment:production",
        })

    def test_oidc_subject_format_can_be_explicitly_overridden_for_existing_repos(self):
        repo = dict(self.repo, created_at="2025-01-01T00:00:00Z")
        self.assertNotIn("@123/", bootstrap.subjects(self.config, repo)["dev"])
        legacy = dict(self.config, oidc_subject_format="legacy")
        self.assertEqual(bootstrap.subjects(legacy, repo)["dev"], (
            "repo:workshop/participant:ref:refs/heads/main"
        ))
        immutable = dict(self.config, oidc_subject_format="immutable")
        self.assertEqual(bootstrap.subjects(immutable, repo)["prod"], (
            "repo:workshop@123/participant@456:environment:production"
        ))

    def test_plan_hash_changes_when_discovered_bindings_change(self):
        discovered = {
            "identity_prefix": "workshop-hash",
            "oidc_subjects": {"dev": "dev-sub", "prod": "prod-sub"},
            "bindings": {"dev": {"region": "region-a"}},
        }
        parameters = {"parameters": {"projectName": {"value": "dev-project"}}}
        what_if = {"changes": [{"resourceId": "/subscriptions/sub/roles/role"}]}
        original = bootstrap.plan_hash(self.config, discovered, parameters, what_if)
        changed = copy.deepcopy(discovered)
        changed["bindings"]["dev"]["region"] = "region-b"
        self.assertNotEqual(
            original, bootstrap.plan_hash(self.config, changed, parameters, what_if)
        )
        self.assertNotEqual(
            original,
            bootstrap.plan_hash(
                self.config, discovered, parameters, {"changes": []}
            ),
        )


if __name__ == "__main__":
    unittest.main()
