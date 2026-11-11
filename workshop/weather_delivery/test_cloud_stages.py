import copy
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from workshop.weather_delivery.cloud_stages import (
    FoundryApi,
    StageBlocked,
    azure_cli_token,
    capture_agent,
    continue_tool_call,
    deploy_captured_agent,
    prepare_candidate,
    prepare_release_version,
    start_tool_call,
    test_stable_endpoint,
    validate_delivery_context,
    verify_delivery_oidc,
    verify_github_oidc,
    write_capture_manifest,
)
from workshop.weather_delivery.contract import ContractError, validate_capture_files
from workshop.weather_delivery.test_contract import manifest


SUBJECT = "repo:example/workshop:ref:refs/heads/FY27"
IMMUTABLE_SUBJECT = "repo:example@123/workshop@456:ref:refs/heads/FY27"
EVENT = {"repository": {"full_name": "example/workshop", "id": 456, "owner": {
    "login": "example", "id": 123,
}}}
CLAIMS = {
    "repository": "example/workshop", "repository_id": "456",
    "repository_owner_id": "123", "ref": "refs/heads/FY27",
}
PROJECT_ENDPOINT = "https://project.example.invalid/api/projects/lab"
PROD_ENDPOINT = (
    "https://project.example.invalid/api/projects/lab/agents/weather-prod/"
    "endpoint/protocols/openai/responses"
)
DEFINITION = {
    "kind": "prompt",
    "model": "participant-selected-model",
    "instructions": "Weather instructions.",
    "tools": [{
        "type": "mcp",
        "server_label": "weather_toolbox",
        "server_url": "https://project.example.invalid/api/projects/lab/toolboxes/weather-toolbox/versions/3/mcp?api-version=v1",
        "project_connection_id": "weather-toolbox-auth",
        "allowed_tools": ["weather_tools___current_weather", "weather_tools___weather_forecast"],
        "require_approval": "always",
    }],
}


def pending_response(response_id="resp_1", conversation_id="conv_1"):
    return {
        "id": response_id,
        "status": "requires_action",
        "conversation": {"id": conversation_id},
        "output": [{
            "type": "mcp_approval_request",
            "id": "mcpr_1",
            "server_label": "weather_toolbox",
            "name": "weather_tools___weather_forecast",
            "arguments": '{"latitude":40.7,"longitude":-74.0}',
        }],
    }


class FakeFoundryApi:
    def __init__(self):
        self.endpoint = "https://project.example.invalid/api/projects/lab"
        self.definition = copy.deepcopy(DEFINITION)
        self.requests = []
        self.pending = pending_response()
        self.responses = []
        self.agent_readback = {
            "name": "weather-release",
            "endpoint": "https://project.example.invalid/api/projects/lab/agents/weather-release/endpoint/protocols/openai/responses",
            "agent_endpoint": {"version_selector": {"version_selection_rules": [{
                "type": "FixedRatio", "agent_version": "8", "traffic_percentage": 100,
            }]}},
        }
        self.existing_validation_agent = {
            "name": "weather-validation",
            "versions": {"latest": {"version": "7", "definition": copy.deepcopy(DEFINITION)}},
        }

    def get_existing_agent(self, agent_name):
        self.requests.append(("get_existing_agent", agent_name))
        return copy.deepcopy(self.existing_validation_agent)

    def create_agent(self, agent_name, definition):
        self.requests.append(("create_agent", agent_name, copy.deepcopy(definition)))
        self.definition = copy.deepcopy(definition)
        return {"name": agent_name, "versions": {"latest": {"version": "8"}}}

    def get_version(self, agent_name, version):
        self.requests.append(("get_version", agent_name, str(version)))
        return {"version": str(version), "definition": copy.deepcopy(self.definition)}

    def get_agent(self, agent_name):
        self.requests.append(("get_agent", agent_name))
        return copy.deepcopy(self.agent_readback)

    def create_version(self, agent_name, definition):
        self.requests.append(("create_version", agent_name, copy.deepcopy(definition)))
        self.definition = copy.deepcopy(definition)
        return {"version": "8"}

    def create_conversation(self):
        self.requests.append(("create_conversation",))
        return {"id": "conv_1"}

    def response(self, body):
        self.requests.append(("response", copy.deepcopy(body)))
        return self.responses.pop(0)

    def get_response(self, response_id):
        self.requests.append(("get_response", response_id))
        return copy.deepcopy(self.pending)


class OidcTests(unittest.TestCase):
    def test_delivery_context_requires_exact_ref_repository_and_subject(self):
        for branch in ("FY27", "rehearsal/test"):
            ref = f"refs/heads/{branch}"
            subject = f"repo:example/workshop:ref:{ref}"
            immutable = f"repo:example@123/workshop@456:ref:{ref}"
            for candidate in (subject, immutable):
                validate_delivery_context("example/workshop", ref, branch, candidate, EVENT, "456", "123")
            for repo, actual_ref, selected, expected in (
                ("example/workshop", ref.upper(), branch, subject),
                ("example/workshop", ref, branch + "-other", subject),
                ("example/other", ref, branch, subject),
                ("example/workshop", ref, branch, SUBJECT + "-other"),
            ):
                with self.assertRaises(StageBlocked):
                    validate_delivery_context(repo, actual_ref, selected, expected, EVENT, "456", "123")

    def test_delivery_context_rejects_missing_or_mismatched_event_identity(self):
        for event, repo_id, owner_id in (
            ({}, "456", "123"),
            ({"repository": {}}, "456", "123"),
            ({"repository": {"full_name": "example/workshop", "id": 456,
                             "owner": {"login": "other", "id": 123}}}, "456", "123"),
            (EVENT, "457", "123"),
            (EVENT, "456", "124"),
            (EVENT, "", "123"),
        ):
            with self.assertRaises(StageBlocked):
                validate_delivery_context("example/workshop", CLAIMS["ref"], "FY27",
                                          IMMUTABLE_SUBJECT, event, repo_id, owner_id)
        for subject in (
            "repo:example@124/workshop@456:ref:refs/heads/FY27",
            "repo:example@123/workshop@457:ref:refs/heads/FY27",
        ):
            with self.assertRaises(StageBlocked):
                validate_delivery_context("example/workshop", CLAIMS["ref"], "FY27",
                                          subject, EVENT, "456", "123")

    @patch("workshop.weather_delivery.cloud_stages.verify_github_oidc")
    def test_delivery_oidc_checks_context_before_request(self, verify):
        context = {
            "GITHUB_REPOSITORY": "example/workshop",
            "GITHUB_REF": "refs/heads/FY27",
            "DELIVERY_BRANCH": "rehearsal",
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(EVENT))
            context.update({"GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY_ID": "456",
                            "GITHUB_REPOSITORY_OWNER_ID": "123"})
            with patch.dict("os.environ", context, clear=True):
                with self.assertRaises(StageBlocked):
                    verify_delivery_oidc(SUBJECT)
        verify.assert_not_called()

    @patch("workshop.weather_delivery.cloud_stages.verify_github_oidc")
    def test_delivery_oidc_passes_exact_validated_context_to_token_check(self, verify):
        context = {
            "GITHUB_REPOSITORY": "example/workshop",
            "GITHUB_REF": "refs/heads/FY27",
            "DELIVERY_BRANCH": "FY27",
            "GITHUB_REPOSITORY_ID": "456",
            "GITHUB_REPOSITORY_OWNER_ID": "123",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.githubusercontent.com/oidc",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(EVENT))
            context["GITHUB_EVENT_PATH"] = str(path)
            with patch.dict("os.environ", context, clear=True):
                for subject in (SUBJECT, IMMUTABLE_SUBJECT):
                    verify_delivery_oidc(subject)
                    self.assertEqual(verify.call_args.args[2], subject)
                    self.assertEqual(verify.call_args.kwargs["expected_claims"], CLAIMS)

    def test_exact_github_subject_is_verified(self):
        token = "header." + __import__("base64").urlsafe_b64encode(json.dumps({
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "api://AzureADTokenExchange",
            "sub": SUBJECT,
            "exp": 9999999999,
            **CLAIMS,
        }).encode()).decode().rstrip("=") + ".signature"
        opener = Mock(return_value=io.BytesIO(json.dumps({"value": token}).encode()))
        self.assertEqual(
            verify_github_oidc(
                "https://token.actions.githubusercontent.com/oidc?sig=x",
                "request-token",
                SUBJECT,
                expected_claims=CLAIMS,
                opener=opener,
            ),
            SUBJECT,
        )
        request = opener.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {
            "sig": ["x"],
            "audience": ["api://AzureADTokenExchange"],
        })
        self.assertEqual(request.get_header("Authorization"), "Bearer request-token")

    def test_wrong_github_subject_blocks(self):
        token = "header." + __import__("base64").urlsafe_b64encode(json.dumps({
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "api://AzureADTokenExchange",
            "sub": "repo:attacker/other:ref:refs/heads/main",
            "exp": 9999999999,
        }).encode()).decode().rstrip("=") + ".signature"
        opener = Mock(return_value=io.BytesIO(json.dumps({"value": token}).encode()))
        with self.assertRaisesRegex(StageBlocked, "configured subject"):
            verify_github_oidc(
                "https://pipelines.actions.githubusercontent.com/oidc",
                "token",
                SUBJECT,
                expected_claims=CLAIMS,
                opener=opener,
            )

    def test_untrusted_oidc_request_host_is_rejected_before_fetch(self):
        opener = Mock()
        with self.assertRaisesRegex(StageBlocked, "GitHub Actions HTTPS host"):
            verify_github_oidc("https://actions.example/oidc", "token", SUBJECT,
                               expected_claims=CLAIMS, opener=opener)
        opener.assert_not_called()

    def test_immutable_subject_and_exact_token_claims(self):
        claims = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "api://AzureADTokenExchange",
            "sub": IMMUTABLE_SUBJECT, "exp": 9999999999, **CLAIMS,
        }
        def verify(payload):
            token = "header." + __import__("base64").urlsafe_b64encode(
                json.dumps(payload).encode()
            ).decode().rstrip("=") + ".signature"
            opener = Mock(return_value=io.BytesIO(json.dumps({"value": token}).encode()))
            return verify_github_oidc("https://token.actions.githubusercontent.com/oidc",
                                      "token", IMMUTABLE_SUBJECT,
                                      expected_claims=CLAIMS, opener=opener)
        self.assertEqual(verify(claims), IMMUTABLE_SUBJECT)
        for key in CLAIMS:
            for value in (None, "incorrect"):
                with self.subTest(key=key, value=value), self.assertRaises(StageBlocked):
                    altered = dict(claims)
                    if value is None:
                        del altered[key]
                    else:
                        altered[key] = value
                    verify(altered)

    def test_azure_cli_token_must_match_configured_tenant_and_client(self):
        payload = {
            "tid": "tenant-1",
            "appid": "client-1",
        }
        token = "header." + __import__("base64").urlsafe_b64encode(
            json.dumps(payload).encode(),
        ).decode().rstrip("=") + ".signature"
        command = Mock(return_value=SimpleNamespace(stdout=token))
        self.assertEqual(azure_cli_token("subscription-1", "client-1", "tenant-1", command), token)
        command.assert_called_once()
        self.assertIn("https://ai.azure.com", command.call_args.args[0])

    def test_azure_cli_token_rejects_wrong_tenant(self):
        payload = {"tid": "other-tenant", "azp": "client-1"}
        token = "header." + __import__("base64").urlsafe_b64encode(
            json.dumps(payload).encode(),
        ).decode().rstrip("=") + ".signature"
        command = Mock(return_value=SimpleNamespace(stdout=token))
        with self.assertRaisesRegex(StageBlocked, "identity differs"):
            azure_cli_token("subscription-1", "client-1", "tenant-1", command)


class CloudStageTests(unittest.TestCase):
    def test_create_candidate_uses_documented_agents_collection_api(self):
        api = FoundryApi("https://project.example.invalid/api/projects/lab", "token")
        api.request = Mock(return_value={"name": "weather-validation"})
        result = api.create_agent("weather-validation", DEFINITION)
        self.assertEqual(result["name"], "weather-validation")
        api.request.assert_called_once_with(
            "/agents?api-version=v1",
            {"name": "weather-validation", "definition": DEFINITION},
        )

    def test_capture_reads_exact_service_version_and_hashes_artifact(self):
        api = FakeFoundryApi()
        with TemporaryDirectory() as directory:
            target = Path(directory) / "agent-definition.json"
            record = capture_agent(api, "weather-release", "7", target)
            self.assertEqual(api.requests, [("get_version", "weather-release", "7")])
            self.assertEqual(record["agent_version"], "7")
            self.assertEqual(record["configuration_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(json.loads(target.read_text())["definition"], DEFINITION)
            self.assertTrue(record["review_required"])

    def test_capture_rejects_credential_fields_without_writing_definition(self):
        api = FakeFoundryApi()
        api.definition["tools"][0]["headers"] = {"Authorization": "Bearer example"}
        with TemporaryDirectory() as directory:
            target = Path(directory) / "agent-definition.json"
            with self.assertRaisesRegex(StageBlocked, "credential-like field"):
                capture_agent(api, "weather-release", "7", target)
            self.assertFalse(target.exists())

    def test_capture_package_records_source_and_prod_target(self):
        config = {"schema_version": 1, "definition": copy.deepcopy(DEFINITION)}
        with TemporaryDirectory() as directory:
            definition = Path(directory) / "agent-definition.json"
            manifest_path = Path(directory) / "manifest.json"
            definition.write_text(json.dumps(config), encoding="utf-8")
            result = write_capture_manifest(
                "weather-dev", "7", "weather-prod", PROD_ENDPOINT, definition,
                manifest_path, PROJECT_ENDPOINT,
            )
            checked, captured = validate_capture_files(manifest_path, definition)
        self.assertEqual(result["source_agent_name"], "weather-dev")
        self.assertEqual(result["production_agent_name"], "weather-prod")
        self.assertEqual(checked, result)
        self.assertEqual(captured, config)

    def test_capture_package_rejects_same_dev_and_prod_agent(self):
        config = {"schema_version": 1, "definition": copy.deepcopy(DEFINITION)}
        with TemporaryDirectory() as directory:
            definition = Path(directory) / "agent-definition.json"
            definition.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(StageBlocked, "must be separate"):
                write_capture_manifest(
                    "weather-agent", "7", "weather-agent", PROD_ENDPOINT,
                    definition, Path(directory) / "manifest.json", PROJECT_ENDPOINT,
                )

    def test_deploy_creates_and_pins_reviewed_definition_on_existing_prod_agent(self):
        api = FakeFoundryApi()
        api.get_existing_agent = Mock(return_value={"name": "weather-prod"})
        api.create_version = Mock(return_value={"version": "9"})
        api.get_version = Mock(return_value={
            "version": "9", "definition": copy.deepcopy(DEFINITION),
        })
        api.pin_agent_version = Mock(return_value={
            "name": "weather-prod",
            "endpoint": "https://project.example.invalid/api/projects/lab/agents/weather-prod/endpoint/protocols/openai/responses",
        })
        config = {"schema_version": 1, "definition": copy.deepcopy(DEFINITION)}
        with TemporaryDirectory() as directory:
            definition_path = Path(directory) / "agent-definition.json"
            manifest_path = Path(directory) / "manifest.json"
            definition_path.write_text(json.dumps(config), encoding="utf-8")
            write_capture_manifest(
                "weather-dev", "7", "weather-prod", PROD_ENDPOINT,
                definition_path, manifest_path, PROJECT_ENDPOINT,
            )
            result = deploy_captured_agent(
                api, manifest_path, definition_path, "weather-prod", PROD_ENDPOINT,
            )
        self.assertEqual(result["production_version"], "9")
        self.assertEqual(result["source_agent_version"], "7")
        self.assertEqual(api.create_version.call_args.args[0], "weather-prod")
        self.assertEqual(api.pin_agent_version.call_args.args, ("weather-prod", "9"))

    def test_deploy_rejects_manifest_for_other_prod_agent_before_write(self):
        api = FakeFoundryApi()
        config = {"schema_version": 1, "definition": copy.deepcopy(DEFINITION)}
        with TemporaryDirectory() as directory:
            definition_path = Path(directory) / "agent-definition.json"
            manifest_path = Path(directory) / "manifest.json"
            definition_path.write_text(json.dumps(config), encoding="utf-8")
            write_capture_manifest(
                "weather-dev", "7", "weather-prod", PROD_ENDPOINT,
                definition_path, manifest_path, PROJECT_ENDPOINT,
            )
            with self.assertRaisesRegex(ContractError, "different configured Prod agent"):
                deploy_captured_agent(
                    api, manifest_path, definition_path, "other-prod", PROD_ENDPOINT,
                )
        self.assertEqual(api.requests, [])

    def test_deploy_rejects_mismatched_prod_endpoint_before_write(self):
        api = FakeFoundryApi()
        config = {"schema_version": 1, "definition": copy.deepcopy(DEFINITION)}
        with TemporaryDirectory() as directory:
            definition_path = Path(directory) / "agent-definition.json"
            manifest_path = Path(directory) / "manifest.json"
            definition_path.write_text(json.dumps(config), encoding="utf-8")
            write_capture_manifest(
                "weather-dev", "7", "weather-prod", PROD_ENDPOINT,
                definition_path, manifest_path, PROJECT_ENDPOINT,
            )
            with self.assertRaisesRegex(StageBlocked, "differs from the facilitator-verified endpoint"):
                deploy_captured_agent(
                    api, manifest_path, definition_path, "weather-prod",
                    PROD_ENDPOINT.replace("weather-prod", "other-prod"),
                )
        self.assertEqual(api.requests, [])

    def test_prepare_creates_separate_candidate_and_reads_it_back(self):
        api = FakeFoundryApi()
        config = {"schema_version": 1, "definition": copy.deepcopy(DEFINITION)}
        with TemporaryDirectory() as directory:
            source = Path(directory) / "definition.json"
            source.write_text(json.dumps(config), encoding="utf-8")
            result = prepare_candidate(
                api, manifest(), source, binding_verified=True, approved=True,
            )
        self.assertEqual(result["candidate_agent"], "weather-validation")
        self.assertEqual(result["candidate_version"], "7")
        self.assertFalse(result["created_agent"])
        self.assertEqual(
            [request[0] for request in api.requests],
            ["get_existing_agent", "get_version"],
        )

    def test_prepare_creates_missing_validation_agent_once(self):
        api = FakeFoundryApi()
        api.existing_validation_agent = None
        config = {"schema_version": 1, "definition": copy.deepcopy(DEFINITION)}
        with TemporaryDirectory() as directory:
            source = Path(directory) / "definition.json"
            source.write_text(json.dumps(config), encoding="utf-8")
            result = prepare_candidate(
                api, manifest(), source, binding_verified=True, approved=True,
            )
        self.assertTrue(result["created_agent"])
        self.assertEqual(
            [request[0] for request in api.requests],
            ["get_existing_agent", "create_agent", "get_version"],
        )

    def test_prepare_does_not_change_an_existing_different_candidate(self):
        api = FakeFoundryApi()
        api.existing_validation_agent["versions"]["latest"]["definition"]["instructions"] = "Unreviewed"
        config = {"schema_version": 1, "definition": copy.deepcopy(DEFINITION)}
        with TemporaryDirectory() as directory:
            source = Path(directory) / "definition.json"
            source.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(StageBlocked, "already exists with another configuration"):
                prepare_candidate(
                    api, manifest(), source, binding_verified=True, approved=True,
                )
        self.assertEqual([request[0] for request in api.requests], ["get_existing_agent"])

    def test_prepare_requires_observed_toolbox_auth_binding(self):
        api = FakeFoundryApi()
        with self.assertRaisesRegex(StageBlocked, "Verify the current-project Toolbox-authentication"):
            prepare_candidate(
                api, manifest(), "unused.json", binding_verified=False, approved=True,
            )
        self.assertEqual(api.requests, [])

    def test_prepare_requires_explicit_agent_creation_approval(self):
        api = FakeFoundryApi()
        with self.assertRaisesRegex(StageBlocked, "requires explicit current-step approval"):
            prepare_candidate(
                api, manifest(), "unused.json", binding_verified=True, approved=False,
            )
        self.assertEqual(api.requests, [])

    def test_release_version_creation_requires_explicit_approval_before_write(self):
        api = FakeFoundryApi()
        with self.assertRaisesRegex(StageBlocked, "requires explicit current-step approval"):
            prepare_release_version(api, manifest(), "unused.json", {}, approved=False)
        self.assertEqual(api.requests, [])

    def test_prepare_rejects_unverified_tool_binding_before_write(self):
        api = FakeFoundryApi()
        config = {"schema_version": 1, "definition": copy.deepcopy(DEFINITION)}
        config["definition"]["tools"][0]["server_url"] = "https://weather.datakoot.com/mcp"
        with TemporaryDirectory() as directory:
            source = Path(directory) / "definition.json"
            source.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(StageBlocked, "differs from the manifest"):
                prepare_candidate(
                    api, manifest(), source, binding_verified=True, approved=True,
                )
        self.assertEqual(api.requests, [])

    def test_start_creates_one_managed_conversation_and_waits_for_approval(self):
        api = FakeFoundryApi()
        api.responses = [pending_response()]
        result = start_tool_call(api, manifest(), "12", "approved", spend_approved=True)
        self.assertEqual(result["conversation_id"], "conv_1")
        self.assertEqual(result["approval_request_id"], "mcpr_1")
        self.assertEqual(result["tool_name"], "weather_tools___weather_forecast")
        self.assertEqual(result["candidate_version"], "12")
        self.assertNotIn("arguments", result)
        self.assertEqual(
            [request[0] for request in api.requests],
            ["get_version", "create_conversation", "response"],
        )
        self.assertEqual(api.requests[2][1]["conversation"], "conv_1")
        self.assertTrue(api.requests[2][1]["store"])

    def test_nonapproval_start_preserves_only_sanitized_partial_ids(self):
        api = FakeFoundryApi()
        api.responses = [{
            "id": "resp_no_approval",
            "status": "completed",
            "conversation": {"id": "conv_1"},
            "output": [{"type": "message", "content": "private text"}],
        }]
        with self.assertRaises(StageBlocked) as raised:
            start_tool_call(api, manifest(), "12", "approved", spend_approved=True)
        self.assertEqual(raised.exception.partial_evidence, {
            "response_id": "resp_no_approval",
            "conversation_id": "conv_1",
            "response_status": "completed",
            "output_types": ["message"],
        })

    def test_continue_uses_exact_pending_request_and_same_conversation(self):
        api = FakeFoundryApi()
        api.responses = [{
            "id": "resp_2",
            "status": "completed",
            "conversation": {"id": "conv_1"},
            "output": [{"type": "mcp_call", "id": "mcp_call_1"}],
        }]
        result = continue_tool_call(
            api, manifest(), "12", "resp_1", "mcpr_1", True, spend_approved=True,
        )
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(result["execution_id"], "mcp_call_1")
        request_body = api.requests[-1][1]
        self.assertEqual(request_body["previous_response_id"], "resp_1")
        self.assertEqual(request_body["conversation"], "conv_1")
        self.assertTrue(request_body["input"][0]["approve"])

    def test_denial_has_no_tool_execution(self):
        api = FakeFoundryApi()
        api.responses = [{
            "id": "resp_2",
            "status": "completed",
            "conversation": "conv_1",
            "output": [{"type": "mcp_approval_response", "approve": False}],
        }]
        result = continue_tool_call(
            api, manifest(), "12", "resp_1", "mcpr_1", False, spend_approved=True,
        )
        self.assertEqual(result["decision"], "denied")
        self.assertIsNone(result["execution_id"])

    def test_incomplete_approval_continuation_does_not_count_as_denial(self):
        api = FakeFoundryApi()
        api.responses = [{
            "id": "resp_2",
            "status": "in_progress",
            "conversation": {"id": "conv_1"},
            "output": [],
        }]
        with self.assertRaisesRegex(StageBlocked, "did not complete"):
            continue_tool_call(
                api, manifest(), "12", "resp_1", "mcpr_1", False, spend_approved=True,
            )

    def test_wrong_approval_id_stops_before_continuation(self):
        api = FakeFoundryApi()
        with self.assertRaisesRegex(StageBlocked, "request ID differs"):
            continue_tool_call(
                api, manifest(), "12", "resp_1", "mcpr_wrong", True, spend_approved=True,
            )
        self.assertEqual([request[0] for request in api.requests], ["get_version", "get_response"])

    def test_start_requires_spend_approval_before_creating_conversation(self):
        api = FakeFoundryApi()
        with self.assertRaisesRegex(StageBlocked, "spend approval"):
            start_tool_call(api, manifest(), "12", "approved", spend_approved=False)
        self.assertEqual([request[0] for request in api.requests], ["get_version"])

    def test_continue_requires_spend_approval_before_response_call(self):
        api = FakeFoundryApi()
        with self.assertRaisesRegex(StageBlocked, "spend approval"):
            continue_tool_call(
                api, manifest(), "12", "resp_1", "mcpr_1", True, spend_approved=False,
            )
        self.assertEqual([request[0] for request in api.requests], ["get_version", "get_response"])

    def test_pin_uses_documented_merge_patch_and_verifies_selector(self):
        api = FoundryApi("https://project.example.invalid/api/projects/lab", "token")
        calls = []

        def request(path, body=None, *, method=None, content_type="application/json"):
            calls.append((path, body, method, content_type))
            if method == "PATCH":
                return {}
            return {
                "agent_endpoint": {"version_selector": {"version_selection_rules": [{
                    "type": "FixedRatio", "agent_version": "12", "traffic_percentage": 100,
                }]}},
            }

        api.request = request
        api.pin_agent_version("weather-release", "12")
        self.assertEqual(calls[0][2], "PATCH")
        self.assertEqual(calls[0][3], "application/merge-patch+json")
        self.assertEqual(calls[0][1]["agent_endpoint"]["version_selector"]["version_selection_rules"][0]["agent_version"], "12")

    def test_response_body_never_replaces_service_returned_endpoint(self):
        endpoint = "https://project.example.invalid/api/projects/lab/agents/weather-release/endpoint/protocols/openai/responses"
        api = FoundryApi("https://project.example.invalid/api/projects/lab", "token")
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "id": "resp_consumer",
                    "conversation": {"id": "conv_consumer"},
                }).encode()

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["authorization"] = request.get_header("Authorization")
            return FakeResponse()

        api.opener = opener
        result = api.invoke_stable_endpoint(
            endpoint,
            "weather-release",
            {"input": "public synthetic test"},
        )
        self.assertEqual(captured["url"], endpoint)
        self.assertEqual(result["id"], "resp_consumer")
        self.assertEqual(captured["body"]["input"], "public synthetic test")
        self.assertEqual(captured["authorization"], "Bearer token")

    def test_consumer_test_reads_back_endpoint_and_pinned_version(self):
        api = FakeFoundryApi()
        config = manifest()
        config["released_agent"]["stable_endpoint"] = api.agent_readback["endpoint"]
        api.responses = [{
            "id": "resp_consumer",
            "conversation": {"id": "conv_consumer"},
        }]
        api.invoke_stable_endpoint = Mock(return_value=api.responses[0])
        result = test_stable_endpoint(api, config, "8", "public synthetic test")
        self.assertEqual(result["agent_version"], "8")
        api.invoke_stable_endpoint.assert_called_once()
        self.assertEqual(api.invoke_stable_endpoint.call_args.args[0], api.agent_readback["endpoint"])

    def test_consumer_test_uses_reviewed_manifest_endpoint_without_invented_api_field(self):
        api = FakeFoundryApi()
        del api.agent_readback["endpoint"]
        config = manifest()
        config["released_agent"]["stable_endpoint"] = (
            "https://project.example.invalid/api/projects/lab/agents/weather-release/"
            "endpoint/protocols/openai/responses"
        )
        api.invoke_stable_endpoint = Mock(return_value={
            "id": "resp_consumer",
            "conversation": {"id": "conv_consumer"},
        })
        result = test_stable_endpoint(api, config, "8", "synthetic")
        self.assertEqual(result["response_id"], "resp_consumer")
        api.invoke_stable_endpoint.assert_called_once()

    def test_project_and_toolbox_endpoints_are_rejected_for_consumer_call(self):
        api = FoundryApi("https://project.example.invalid/api/projects/lab", "token")
        for endpoint in (
            "https://project.example.invalid/api/projects/lab",
            "https://project.example.invalid/api/projects/lab/toolboxes/weather/versions/3/mcp",
            "https://project.example.invalid/api/projects/other/agents/weather-release/endpoint/protocols/openai/responses",
            "https://project.example.invalid/api/projects/lab/agents/other/endpoint/protocols/openai/responses",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(StageBlocked):
                api.invoke_stable_endpoint(endpoint, "weather-release", {"input": "synthetic"})


if __name__ == "__main__":
    unittest.main()
