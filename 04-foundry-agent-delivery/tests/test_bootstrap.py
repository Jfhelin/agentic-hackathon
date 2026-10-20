import copy
import subprocess
import unittest
import unittest.mock
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

    def _lookup(self, agents=("agent-a", "agent-b"), agent_error=None):
        lookup = unittest.mock.Mock()
        lookup.account_resource_group.side_effect = lambda _sub, account: (
            "participant-dev" if account == "dev-account" else "participant-prod"
        )
        lookup.model_deployments.return_value = {
            "gpt-model": "gpt-5-mini", "embed": "text-embedding-3-small",
        }
        if agent_error:
            lookup.prompt_agents.side_effect = agent_error
        else:
            lookup.prompt_agents.return_value = list(agents)
        return lookup

    DEV = "https://dev-account.services.ai.azure.com/api/projects/dev-project"
    PROD = "https://prod-account.services.ai.azure.com/api/projects/prod-project/"

    def test_interactive_setup_needs_only_subscription_and_endpoints(self):
        lookup = self._lookup()
        answers = iter([
            "not-a-guid",
            self.config["subscription_id"],
            self.DEV, "", self.PROD, "", "", "unlisted-agent", "2", "none",
        ])
        with unittest.mock.patch("builtins.input", side_effect=lambda _prompt: next(answers)):
            config = bootstrap.prompt_config("workshop/participant", lookup=lookup)
        lookup.check_subscription.assert_called_once_with(self.config["subscription_id"])
        self.assertEqual(config["environments"]["dev"], {
            "resource_group": "participant-dev", "account_name": "dev-account",
            "project_name": "dev-project", "model_deployment": "gpt-model",
        })
        self.assertEqual(config["environments"]["prod"]["project_name"], "prod-project")
        self.assertEqual(config["environments"]["prod"]["model_deployment"], "gpt-model")
        self.assertEqual(config["identity_resource_group"], "participant-dev")
        self.assertEqual(config["agent_name"], "agent-b")
        self.assertNotIn("production_reviewers", config)

    def test_setup_presets_run_without_prompts(self):
        presets = {
            "subscription_id": self.config["subscription_id"],
            "dev_endpoint": self.DEV, "prod_endpoint": self.PROD,
            "model_deployment": "gpt-model", "identity_resource_group": "shared-group",
            "agent_name": "workshop-agent", "production_reviewers": ["participant"],
        }
        with unittest.mock.patch("builtins.input", side_effect=AssertionError("prompted")):
            config = bootstrap.prompt_config(
                "workshop/participant", presets, lookup=self._lookup(),
            )
        self.assertEqual(config["identity_resource_group"], "shared-group")
        self.assertEqual(config["production_reviewers"], ["participant"])

    def test_preset_model_deployment_must_exist(self):
        presets = {
            "subscription_id": self.config["subscription_id"],
            "dev_endpoint": self.DEV, "model_deployment": "missing",
        }
        with self.assertRaisesRegex(bootstrap.BootstrapError, "missing"):
            bootstrap.prompt_config("workshop/participant", presets, lookup=self._lookup())

    def test_agent_listing_failure_falls_back_to_manual_name(self):
        presets = {
            "subscription_id": self.config["subscription_id"],
            "dev_endpoint": self.DEV, "prod_endpoint": self.PROD,
            "model_deployment": "gpt-model", "production_reviewers": [],
        }
        lookup = self._lookup(agent_error=bootstrap.BootstrapError("no access"))
        answers = iter(["", "my-agent"])
        with unittest.mock.patch("builtins.input", side_effect=lambda _prompt: next(answers)):
            config = bootstrap.prompt_config("workshop/participant", presets, lookup=lookup)
        self.assertEqual(config["agent_name"], "my-agent")

    def test_project_endpoint_is_parsed_strictly(self):
        self.assertEqual(
            bootstrap.parse_project_endpoint(self.PROD), ("prod-account", "prod-project"),
        )
        for bad in ("https://evil.example/api/projects/p", "dev-account/dev-project"):
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.parse_project_endpoint(bad)

    def test_production_reviewers_are_optional_but_validated(self):
        config = dict(self.config, production_reviewers=["octo-cat"])
        self.assertEqual(bootstrap.validate_config(config), config)
        for bad in (["-bad"], ["dup", "dup"], "octo-cat"):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "production_reviewers"):
                bootstrap.validate_config(dict(self.config, production_reviewers=bad))

    def test_prerequisite_failures_explain_the_fix(self):
        def fake_run(command, **_kwargs):
            code = 1 if command[:2] == ["az", "bicep"] else 0
            return subprocess.CompletedProcess(command, code, "", "")
        with unittest.mock.patch.object(bootstrap.shutil, "which", return_value="/bin/tool"), \
                unittest.mock.patch.object(bootstrap.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "az bicep install"):
                bootstrap.check_prerequisites()
        with unittest.mock.patch.object(bootstrap.shutil, "which", return_value=None):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "GitHub CLI"):
                bootstrap.check_prerequisites()

    def test_what_if_requests_machine_readable_json(self):
        discovered = {
            "resource_groups": {"participant-identities": {"location": "region"}},
            "identity_prefix": "workshop-hash",
            "oidc_subjects": {"dev": "dev-sub", "prod": "prod-sub"},
            "bindings": {},
        }
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            return {"changes": []}
        with unittest.mock.patch.object(bootstrap, "discover", return_value=discovered), \
                unittest.mock.patch.object(bootstrap, "build_template"), \
                unittest.mock.patch.object(bootstrap, "run", side_effect=fake_run):
            bootstrap.preview_plan(self.config)
        self.assertIn("--no-pretty-print", commands[-1])

    def test_summary_lists_only_real_changes_and_github_settings(self):
        what_if = {"changes": [
            {"changeType": "Create", "resourceId": "/subscriptions/s/providers/Microsoft.X/a"},
            {"changeType": "Ignore", "resourceId": "/subscriptions/s/providers/Microsoft.X/b"},
        ]}
        github = {"production_environment": {
            "name": "production", "deployment_branch": "main",
            "required_reviewers": [{"login": "octo", "id": 1}],
        }}
        lines = bootstrap.summarize_changes(what_if, github)
        self.assertTrue(lines[0].startswith("Create") and lines[0].endswith("Microsoft.X/a"))
        self.assertFalse(any("Microsoft.X/b" in line for line in lines))
        self.assertTrue(any("required reviewers octo" in line for line in lines))
        self.assertFalse(any("expected on reruns" in line for line in lines))
        what_if["changes"].append({
            "changeType": "Modify",
            "resourceId": "/subscriptions/s/providers/Microsoft.C/accounts/a"
            "/providers/Microsoft.Authorization/roleAssignments/r",
        })
        lines = bootstrap.summarize_changes(what_if, github)
        self.assertTrue(any("expected on reruns" in line for line in lines))

    def test_production_gate_is_part_of_the_plan_hash(self):
        discovered = {
            "identity_prefix": "p", "oidc_subjects": {}, "bindings": {},
            "github": {"production_environment": None},
        }
        gated = copy.deepcopy(discovered)
        gated["github"]["production_environment"] = {"name": "production"}
        parameters = {"parameters": {}}
        self.assertNotEqual(
            bootstrap.plan_hash(self.config, discovered, parameters, {}),
            bootstrap.plan_hash(self.config, gated, parameters, {}),
        )

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
