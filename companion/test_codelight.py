import base64
import asyncio
import contextlib
import io
import importlib.util
import json
import os
import shlex
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from types import ModuleType
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import codelight
from codelight_core.agents import base as agents_base
from codelight_core.agents import codex as codex_agent
from codelight_core.agents import copilot as copilot_agent
from codelight_core.agents import cursor as cursor_agent
from codelight_core.agents import grok as grok_agent
from codelight_core.agents import opencode as opencode_agent
from codelight_core.state import CodelightState
from codelight_core import auth as auth_core
from codelight_core import dashboard_client
from codelight_core import hooks as hooks_core
from codelight_core import invocation as invocation_core
from codelight_core import conversation as conversation_core
from codelight_core.conversation import ConversationRefresher
from codelight_core import discovery as discovery_core
from codelight_core import hook_commands
from codelight_core import hook_io
from codelight_core import hook_runtime
from codelight_core import lifecycle
from codelight_core import policy as policy_core
from codelight_core import transcript as transcript_core
from codelight_core import remote_control
from codelight_core import remote_payloads
from codelight_core import service as service_core
from codelight_core.ws_server import CodelightWebsocketHub
from codelight_core.agents.registry import AgentRegistry, discover_agent_modules
from codelight_core.usage import UsagePoller, usage_summary


def load_logo_bitmap_tool():
    path = os.path.join(os.path.dirname(__file__), "tools", "logo_bitmap.py")
    spec = importlib.util.spec_from_file_location("logo_bitmap_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TranscriptParserTests(unittest.TestCase):
    def parse(self, records):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w") as stream:
                for record in records:
                    stream.write(json.dumps(record) + "\n")
            return codelight._parse_transcript(path)
        finally:
            os.unlink(path)

    def test_codex_messages_tools_and_outputs(self):
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix the test"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "I will inspect it."}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "pytest -q"}),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "output": (
                        "Chunk ID: abc123\n"
                        "Wall time: 0.1 seconds\n"
                        "Process exited with code 0\n"
                        "Original token count: 2\n"
                        "Final output:\n"
                        "2 passed"
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "hidden instructions"}],
                },
            },
        ]

        lines = self.parse(records)

        self.assertEqual([line["role"] for line in lines],
                         ["user", "assistant", "tool", "output"])
        self.assertEqual(lines[0]["text"], "Fix the test")
        self.assertEqual(lines[1]["text"], "I will inspect it.")
        self.assertIn("pytest -q", lines[2]["text"])
        self.assertEqual(lines[3]["text"], "↳ 2 passed")

    def test_codex_tool_summaries_hide_transport_arguments(self):
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({
                        "cmd": "git status --short",
                        "workdir": "/repo",
                        "yield_time_ms": 1000,
                    }),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "update_plan",
                    "arguments": json.dumps({
                        "explanation": "Conversation feed is ready.",
                        "plan": [{"step": "Build", "status": "completed"}],
                    }),
                },
            },
        ]

        lines = self.parse(records)

        self.assertEqual(lines[0]["text"], "exec_command: git status --short")
        self.assertEqual(lines[1]["text"],
                         "update_plan: Conversation feed is ready.")


class CodexUsageTests(unittest.TestCase):
    def test_reads_latest_rate_limit_snapshot(self):
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {
                            "used_percent": 36.0,
                            "window_minutes": 300,
                            "resets_at": 2_000_000_000,
                        },
                        "secondary": {
                            "used_percent": 6.0,
                            "window_minutes": 10_080,
                            "resets_at": 2_000_604_800,
                        },
                    },
                },
            },
        ]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w") as stream:
                for record in records:
                    stream.write(json.dumps(record) + "\n")
            usage = codex_agent.usage_from_rollout(path)
        finally:
            os.unlink(path)

        self.assertIsNotNone(usage)
        self.assertEqual(usage["session_pct"], 0.36)
        self.assertEqual(usage["weekly_pct"], 0.06)
        self.assertEqual(usage["session_reset_at"], 2_000_000_000)
        self.assertEqual(usage["weekly_reset_at"], 2_000_604_800)

    def test_app_server_usage_includes_rate_limit_reset_credits(self):
        def rpc(requests):
            self.assertEqual(requests[0]["method"], "account/rateLimits/read")
            return [{
                "id": 2,
                "result": {
                    "rateLimitsByLimitId": {
                        "codex": {
                            "primary": {
                                "usedPercent": 25,
                                "resetsAt": 2_000_000_000,
                            },
                            "secondary": {
                                "usedPercent": 50,
                                "resetsAt": 2_000_604_800,
                            },
                        },
                    },
                    "rateLimitResetCredits": {
                        "availableCount": 2,
                        "credits": [{
                            "id": "RateLimitResetCredit_1",
                            "resetType": "codexRateLimits",
                            "status": "available",
                        }],
                    },
                },
            }]

        usage = codex_agent.get_app_server_usage("/tmp/codex", rpc=rpc)

        self.assertIsNotNone(usage)
        self.assertEqual(usage["session_pct"], 0.25)
        self.assertEqual(usage["weekly_pct"], 0.5)
        self.assertEqual(
            usage["rateLimitResetCredits"]["availableCount"], 2)
        self.assertEqual(usage["rate_limit_reset_available_count"], 2)

    def test_consume_session_reset_returns_outcome_and_refreshed_usage(self):
        def rpc(requests):
            self.assertEqual(
                requests[0]["method"],
                "account/rateLimitResetCredit/consume",
            )
            self.assertTrue(requests[0]["params"]["idempotencyKey"])
            self.assertEqual(requests[1]["method"], "account/rateLimits/read")
            return [
                {"id": 2, "result": {"outcome": "reset"}},
                {
                    "id": 3,
                    "result": {
                        "rateLimits": {
                            "primary": {
                                "usedPercent": 0,
                                "resetsAt": 2_000_000_000,
                            },
                        },
                        "rateLimitResetCredits": {"availableCount": 1},
                    },
                },
            ]

        result = codex_agent.consume_session_reset("/tmp/codex", rpc=rpc)

        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "reset")
        self.assertEqual(result["usage"]["session_pct"], 0.0)
        self.assertEqual(
            result["usage"]["rateLimitResetCredits"]["availableCount"], 1)


class CopilotUsageTests(unittest.TestCase):
    def test_builds_monthly_company_pool_from_direct_api(self):
        def api(path, token):
            self.assertEqual(token, "token")
            if "/ai_credit/usage?" in path:
                return {
                    "usageItems": [
                        {
                            "product": "Copilot",
                            "unitType": "ai-credits",
                            "grossQuantity": 2100,
                        },
                        {
                            "product": "Actions",
                            "unitType": "minutes",
                            "grossQuantity": 999,
                        },
                    ],
                }
            return {
                "plan_type": "business",
                "seat_breakdown": {"total": 7},
            }

        usage = copilot_agent.get_usage(
                "Sensnology-AB", "token",
                datetime(2026, 7, 9, tzinfo=timezone.utc),
                api=api)

        self.assertIsNotNone(usage)
        self.assertEqual(usage["used_credits"], 2100)
        self.assertEqual(usage["included_credits"], 21000)
        self.assertEqual(usage["monthly_pct"], 0.1)
        self.assertEqual(usage["limits"][0]["label"], "Monthly")

    def test_permission_failure_omits_detailed_usage(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/test", 403, "Forbidden", {}, None)
        try:
            self.assertIsNone(copilot_agent.get_usage(
                "Sensnology-AB", "token",
                datetime(2026, 7, 9, tzinfo=timezone.utc),
                api=error))
        finally:
            error.close()

    def test_events_path_for_session_stays_inside_copilot_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "copilot")
            session_dir = os.path.join(home, "session-state", "session-1")
            os.makedirs(session_dir)
            events = os.path.join(session_dir, "events.jsonl")
            with open(events, "w") as stream:
                stream.write("{}\n")

            self.assertEqual(
                copilot_agent.events_path_for_session(home, "session-1"),
                events,
            )
            self.assertEqual(
                copilot_agent.events_path_for_session(home, "../outside"),
                "",
            )


class PermissionPolicyTests(unittest.TestCase):
    def test_allow_tool_roundtrip_with_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = os.path.join(tmp, "policy.json")
            lock = threading.Lock()

            self.assertFalse(policy_core.is_allowed_tool(policy_path, "WebFetch"))
            persisted, value = policy_core.allow_tool(policy_path, lock, "WebFetch")
            self.assertTrue(persisted)
            self.assertEqual(value, "WebFetch")
            self.assertTrue(policy_core.is_allowed_tool(policy_path, "WebFetch"))
            self.assertFalse(policy_core.is_allowed_tool(policy_path, "Bash"))
            self.assertFalse(policy_core.is_allowed_tool(policy_path, "?"))

            entry = policy_core.load_policy(policy_path)["allowed_tools"][0]
            self.assertGreater(entry["added_at"], 0)
            self.assertEqual(entry["added_at"], entry["last_used"])

            # touch: a stale last_used is refreshed, a fresh one is left alone
            # (no rewrite churn on every hook invocation).
            stale = entry["added_at"] - policy_core.TOUCH_INTERVAL_SECS - 1
            policy = policy_core.load_policy(policy_path)
            policy["allowed_tools"][0]["last_used"] = stale
            policy_core.write_policy(policy_path, policy)
            policy_core.touch_allowed_tool(policy_path, lock, "WebFetch")
            refreshed = policy_core.load_policy(policy_path)["allowed_tools"][0]
            self.assertGreater(refreshed["last_used"], stale)

    def test_trusted_auto_allow_tools_are_agent_scoped(self):
        registry = codelight._new_agent_registry()
        self.assertIn("read_file", registry.trusted_auto_allow_tools("copilot"))
        self.assertEqual(registry.trusted_auto_allow_tools("claude"), frozenset())
        self.assertEqual(registry.trusted_auto_allow_tools("unknown"), frozenset())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = os.path.join(self.tmp.name, "policy.json")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.policy_patch = mock.patch.object(codelight, "POLICY_PATH", self.policy)
        self.policy_patch.start()

    def tearDown(self):
        self.policy_patch.stop()
        self.tmp.cleanup()

    def test_folder_trust_uses_codelight_policy(self):
        persisted, folder = codelight._allow_folder(self.repo)

        self.assertTrue(persisted)
        self.assertEqual(folder, self.repo)
        self.assertTrue(codelight._is_trusted_repo_cwd(
            os.path.join(self.repo, "src")))
        with open(self.policy) as stream:
            policy = json.load(stream)
        self.assertEqual(policy["trusted_folders"], [self.repo])

    def test_exact_command_is_repository_scoped_and_agent_neutral(self):
        command = "npm test -- --runInBand"
        persisted, stored = codelight._allow_command(command, self.repo)

        self.assertTrue(persisted)
        self.assertEqual(stored, command)
        for tool, tool_input in [
            ("Bash", {"command": command}),
            ("exec_command", {"cmd": command}),
            ("run_in_terminal", {"command": command}),
        ]:
            self.assertTrue(policy_core.is_allowed_command(
                self.policy, tool, tool_input, self.repo))
        self.assertFalse(policy_core.is_allowed_command(
            self.policy, "Bash", {"command": command + " --dangerous"}, self.repo))

        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        self.assertFalse(policy_core.is_allowed_command(
            self.policy, "Bash", {"command": command}, other))

    def test_trusted_patch_cannot_escape_through_symlink(self):
        self.assertTrue(codelight._allow_folder(self.repo)[0])
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.repo, "linked"))
        patch = {
            "input": "*** Update File: linked/secret.txt\n@@\n-old\n+new\n",
        }
        self.assertFalse(policy_core.is_safe_trusted_apply_patch(
            self.policy, "apply_patch", patch, self.repo))


class AuthenticationTests(unittest.TestCase):
    def test_hmac_authentication_is_required(self):
        secret = "test-secret"
        nonce = "abc123"
        digest = auth_core.auth_hmac(secret, nonce)

        self.assertTrue(auth_core.valid_auth_response(
            {"auth_hmac": digest}, secret, nonce))
        self.assertFalse(auth_core.valid_auth_response(
            {"auth": secret}, secret, nonce))
        self.assertFalse(auth_core.valid_auth_response(
            {"auth_hmac": "wrong"}, secret, nonce))

    def make_hub(self):
        return CodelightWebsocketHub(
            websockets_module=None,
            shutdown=threading.Event(),
            remote_permissions=lambda: False,
            remote_questions=lambda: False,
            client_config=lambda client: {},
            status_snapshot=lambda: {},
            overall_status=lambda: (0, "idle", {}, ""),
            pending_payloads=lambda: [],
            conversation_payload=lambda: None,
            conversation_payload_for=lambda agent_id: None,
            notify_conversation_changed=lambda: None,
            note_question_client_gone=lambda: None,
            respond_permission=lambda request_id, decision, by: False,
            respond_question=lambda request_id, answers, by: False,
            consume_session_reset=lambda agent_id, request_id: {},
            set_budget=lambda agent_id, budget, request_id: {},
            extend_request=lambda request_id: False,
            announce_gnome=lambda features: False,
            log=lambda message: None,
            verbose_log=lambda message: None,
        )

    def test_invalid_hmac_sends_unauthorized(self):
        class FakeWebSocket:
            remote_address = ("192.168.178.190", 12345)

            def __init__(self):
                self.sent = []
                self.closed = None

            async def send(self, message):
                self.sent.append(message)

            async def recv(self):
                return json.dumps({"auth_hmac": "wrong"})

            async def close(self, code, reason):
                self.closed = (code, reason)

        ws = FakeWebSocket()
        ok = asyncio.run(self.make_hub()._authenticate(ws, "secret"))

        self.assertFalse(ok)
        self.assertEqual(ws.closed, (1008, "Unauthorized"))
        self.assertTrue(any("unauthorized" in message for message in ws.sent))

    def test_auth_timeout_does_not_send_sticky_unauthorized(self):
        class FakeWebSocket:
            remote_address = ("192.168.178.190", 12345)

            def __init__(self):
                self.sent = []
                self.closed = None

            async def send(self, message):
                self.sent.append(message)

            async def recv(self):
                raise asyncio.TimeoutError()

            async def close(self, code, reason):
                self.closed = (code, reason)

        ws = FakeWebSocket()
        ok = asyncio.run(self.make_hub()._authenticate(ws, "secret"))

        self.assertFalse(ok)
        self.assertEqual(ws.closed, (1011, "Authentication timeout"))
        self.assertFalse(any("unauthorized" in message for message in ws.sent))


class AgentDetectionTests(unittest.TestCase):
    def test_detects_cli_and_vscode_agents(self):
        executable_paths = {
            "claude": "/bin/claude",
            "code": "/bin/code",
        }

        def which(name):
            return executable_paths.get(name)

        extension_result = mock.Mock(
            stdout="openai.chatgpt\ngithub.copilot-chat\n",
            returncode=0,
        )
        with mock.patch.object(lifecycle.shutil, "which", side_effect=which), \
             mock.patch("subprocess.run", return_value=extension_result):
            detected = lifecycle.detect_installed_agents(codelight._new_agent_registry())

        self.assertEqual(detected, {"claude", "copilot", "codex"})

    def test_agent_set_ignores_unknown_values(self):
        self.assertEqual(
            lifecycle.parse_agent_set("codex, unknown, claude",
                                      set(codelight.AGENT_REGISTRY)),
            {"codex", "claude"},
        )


class ClientConfigTests(unittest.TestCase):
    def test_conversation_capability_flag_in_client_metadata(self):
        meta = codelight._new_agent_registry().client_metadata("vscode")
        # Agents with a transcript extractor advertise conversation support.
        self.assertTrue(meta["cursor"]["conversation"])
        self.assertTrue(meta["claude"]["conversation"])
        self.assertTrue(meta["grok"]["conversation"])

    def test_load_config_reads_agents_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w") as stream:
                json.dump({"agents": {"copilot": {"github_org": "Org"}}}, stream)
            with mock.patch.object(codelight, "CODELIGHT_CONFIG_HOME", tmp):
                config = codelight._load_config()

        self.assertEqual(config["agents"]["copilot"]["github_org"], "Org")

    def test_load_config_tolerates_missing_or_broken_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(codelight, "CODELIGHT_CONFIG_HOME", tmp):
                self.assertEqual(codelight._load_config(), {})
            with open(os.path.join(tmp, "config.json"), "w") as stream:
                stream.write("not json")
            with mock.patch.object(codelight, "CODELIGHT_CONFIG_HOME", tmp):
                self.assertEqual(codelight._load_config(), {})

    def test_client_config_carries_agent_branding(self):
        config = codelight._client_config()

        self.assertEqual(config["default_agent_id"], codelight.DEFAULT_AGENT_ID)
        self.assertEqual(set(config["agents"]), set(codelight.AGENT_REGISTRY))
        for agent_id, meta in config["agents"].items():
            self.assertTrue(meta["display"], agent_id)
            self.assertRegex(meta["color"], r"^#[0-9A-Fa-f]{6}$")
            self.assertIn("currentColor", meta["logo_svg"])
            self.assertTrue(meta["logo_svg"].startswith("<svg"))
        # The whole map rides in every connect handshake — keep it lean.
        self.assertLess(len(json.dumps(config)), 16384)

    def test_screen_client_gets_bitmap_logos(self):
        config = codelight._client_config("screen")

        self.assertLessEqual(len(config["agents"]),
                             AgentRegistry.MAX_SCREEN_AGENTS)
        for agent_id, meta in config["agents"].items():
            self.assertNotIn("logo_svg", meta)
            bitmap = base64.b64decode(meta["logo_bitmap"])
            self.assertEqual(len(bitmap), 48 * 48 // 8, agent_id)
            self.assertRegex(meta["color"], r"^#[0-9A-Fa-f]{6}$")
        # The ESP8266 parses this into a ~45 KB heap — keep it small.
        self.assertLess(len(json.dumps(config)), 4096)


class FakeAgentIntegrationTests(unittest.TestCase):
    """Prove the registry path is additive: a new agent registers one
    AgentIntegration and flows through every client surface unchanged."""

    def test_builtin_agent_modules_are_discovered(self):
        module_names = {
            module.__name__.rsplit(".", 1)[-1]
            for module in discover_agent_modules()
        }

        self.assertGreaterEqual(module_names, {"claude", "copilot", "codex"})
        self.assertNotIn("base", module_names)
        self.assertNotIn("registry", module_names)

    def test_module_with_build_integration_is_enough_to_register_agent(self):
        module = ModuleType("codelight_core.agents.pluggy")
        module.SPEC = agents_base.AgentSpec("pluggy", "Pluggy")
        seen = {}

        def build_integration(config, *, log=None):
            seen["config"] = config
            seen["log"] = log
            return agents_base.AgentIntegration(
                spec=module.SPEC,
            )

        module.build_integration = build_integration
        registry = AgentRegistry(
            agents_config={"pluggy": {"enabled": True}},
            modules=(module,),
            log=lambda message: None,
        )

        self.assertEqual(registry.supported_agent_ids(), {"pluggy"})
        self.assertEqual(seen["config"], {"enabled": True})
        self.assertIsNotNone(seen["log"])

    def make_fake_integration(self):
        return agents_base.AgentIntegration(
            spec=agents_base.AgentSpec(
                "fake",
                "Fake Agent",
                executables=("fake-cli",),
                vscode_extensions=frozenset({"acme.fake-agent"}),
            ),
            hook_modes=(
                agents_base.HookMode(
                    "question-fake", kind="question",
                    envelope=agents_base.CONTEXT, default_agent_id="fake"),
            ),
            usage_fetcher=lambda: {"session_pct": 0.25, "weekly_pct": 0.5},
        )

    def make_registry(self):
        return AgentRegistry(extra_agents=(self.make_fake_integration(),))

    def test_fake_agent_flows_through_registry_surfaces(self):
        registry = self.make_registry()

        self.assertIn("fake", registry.supported_agent_ids())
        self.assertEqual(registry.display_registry()["fake"],
                         {"display": "Fake Agent"})
        self.assertEqual(registry.client_metadata()["fake"]["display"],
                         "Fake Agent")
        self.assertEqual(registry.executables_by_agent()["fake"], ("fake-cli",))
        self.assertEqual(
            lifecycle.parse_agent_set("fake, unknown",
                                      registry.supported_agent_ids()),
            {"fake"},
        )
        self.assertEqual(registry.hook_modes()["question-fake"].kind, "question")

    def test_fake_agent_is_detected_and_polled(self):
        registry = self.make_registry()

        def which(name):
            return "/bin/fake-cli" if name == "fake-cli" else None

        with mock.patch.object(lifecycle.shutil, "which", side_effect=which), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="", returncode=1)):
            self.assertEqual(lifecycle.detect_installed_agents(registry), {"fake"})

        state = CodelightState(
            default_agent_id="claude",
            agent_registry=registry.display_registry(),
            idle_window=600,
            idle_window_waiting=30,
        )
        poller = UsagePoller(
            state=state,
            fetchers={"fake": registry.usage_fetchers()["fake"]},
            interval=60,
            shutdown=threading.Event(),
            log=lambda line: None,
            push=lambda: None,
        )
        poller.poll_once()

        snapshot = state.status_snapshot()
        self.assertEqual(snapshot["per_agent_usage"]["fake"]["session_pct"], 0.25)
        self.assertEqual(snapshot["per_agent_usage"]["fake"]["agent_display"],
                         "Fake Agent")

    def test_fake_question_mode_emits_context_envelope(self):
        registry = self.make_registry()
        mode = registry.hook_modes()["question-fake"]
        seen = {}

        def request_json(path, request, **kwargs):
            seen.update(request)
            return {"answers": {"Deploy?": "Yes"}}

        stdout = io.StringIO()
        with mock.patch.object(hook_commands.hook_io, "request_json",
                               side_effect=request_json), \
             contextlib.redirect_stdout(stdout):
            hook_commands.run_question_hook(
                mode=mode,
                agent_id="",
                socket_path="/nonexistent.sock",
                hook_wait_ceiling=1,
                normalize_agent_id=lambda a: a or "claude",
                agent_display_name=lambda a: "Fake Agent",
                input_text=json.dumps({
                    "session_id": "s1",
                    "tool_input": {"questions": [{"question": "Deploy?"}]},
                }),
            )

        self.assertEqual(seen["agent_id"], "fake")
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"],
                         "deny")
        self.assertIn("Deploy?: Yes",
                      payload["hookSpecificOutput"]["additionalContext"])

    def test_registry_rejects_duplicate_agent_ids_and_hook_modes(self):
        claude_clone = agents_base.AgentIntegration(
            spec=agents_base.AgentSpec("claude", "Claude Clone"))
        with self.assertRaises(ValueError):
            AgentRegistry(extra_agents=(claude_clone,))

        mode_clone = agents_base.AgentIntegration(
            spec=agents_base.AgentSpec("fake", "Fake Agent"),
            hook_modes=(
                agents_base.HookMode(
                    "permission", kind="permission",
                    envelope=agents_base.BEHAVIOR, default_agent_id="fake"),
            ),
        )
        with self.assertRaises(ValueError):
            AgentRegistry(extra_agents=(mode_clone,)).hook_modes()


class LogoBitmapToolTests(unittest.TestCase):
    def test_pack_bitmap_is_msb_first_and_fixed_size(self):
        tool = load_logo_bitmap_tool()
        pixels = [False] * (48 * 48)
        pixels[0] = True
        pixels[7] = True
        pixels[8] = True

        packed = tool.pack_bitmap(pixels)

        self.assertEqual(len(packed), 48 * 48 // 8)
        self.assertEqual(packed[0], 0b10000001)
        self.assertEqual(packed[1], 0b10000000)


class StateSnapshotTests(unittest.TestCase):
    def make_state(self):
        return CodelightState(
            default_agent_id="claude",
            agent_registry=codelight.AGENT_REGISTRY,
            idle_window=600,
            idle_window_waiting=30,
        )

    def test_active_transcript_carries_its_agent(self):
        state = self.make_state()
        # Cursor is the most-recently-active session with a transcript; a more
        # recent claude "working" WITHOUT a transcript must not steal the label.
        state.update_session("cur-1", "working",
                             transcript="/tmp/cursor.jsonl", agent_id="cursor")
        state.update_session("cla-1", "working", agent_id="claude")

        active = state.active_transcript()
        self.assertEqual(active.path, "/tmp/cursor.jsonl")
        self.assertEqual(active.agent_id, "cursor")

    def test_transcript_for_agent_returns_that_agents_latest(self):
        state = self.make_state()
        state.update_session("cur-1", "working",
                             transcript="/tmp/cursor.jsonl", agent_id="cursor")
        state.update_session("cla-1", "working",
                             transcript="/tmp/claude.jsonl", agent_id="claude")

        cur = state.transcript_for_agent("cursor")
        self.assertEqual(cur.path, "/tmp/cursor.jsonl")
        self.assertEqual(cur.agent_id, "cursor")
        # Ended session still resolves via the per-agent record.
        state.update_session("cur-1", "ended", agent_id="cursor")
        self.assertEqual(state.transcript_for_agent("cursor").path,
                         "/tmp/cursor.jsonl")
        self.assertEqual(state.transcript_for_agent("grok").path, "")

    def test_conversation_payload_label_matches_transcript_source(self):
        payload = conversation_core.build_payload(
            active_transcript=lambda: ("s1", "/tmp/x.jsonl", "cursor"),
            parse_transcript=lambda path: [{"role": "user", "text": "hi"}],
            normalize_agent_id=lambda a: a or "claude",
            agent_display_name=lambda a: {"cursor": "Cursor"}.get(a, "?"),
        )
        self.assertEqual(payload["agent_id"], "cursor")
        self.assertEqual(payload["agent_display"], "Cursor")
        self.assertEqual(payload["lines"][0]["agent_id"], "cursor")

    def test_enabled_agents_stay_visible_when_idle(self):
        state = self.make_state()
        state.set_enabled_agents({"claude", "cursor", "grok"})

        # No active sessions, no usage for cursor/grok — they must still appear.
        snapshot = state.status_snapshot()
        per_agent_status = snapshot["per_agent_status"]
        self.assertEqual(per_agent_status.get("cursor"), "idle")
        self.assertEqual(per_agent_status.get("grok"), "idle")

        # An active session overrides idle for that agent only.
        state.update_session("s1", "working", agent_id="cursor")
        per_agent_status = state.status_snapshot()["per_agent_status"]
        self.assertEqual(per_agent_status["cursor"], "working")
        self.assertEqual(per_agent_status["grok"], "idle")

    def test_status_snapshot_uses_last_active_agent_usage(self):
        state = self.make_state()
        state.update_usage(usages={
            "claude": {"session_pct": 0.1, "weekly_pct": 0.2},
            "codex": {"session_pct": 0.3, "weekly_pct": 0.4},
            "copilot": {"monthly_pct": 0.5, "monthly_reset": "20d"},
        })

        state.update_session("codex-session", "working", agent_id="codex")
        codex_payload = state.status_snapshot()
        self.assertEqual(codex_payload["agent_id"], "codex")
        self.assertEqual(codex_payload["session_pct"], 0.3)
        self.assertEqual(codex_payload["weekly_pct"], 0.4)

        state.update_session("copilot-session", "waiting", agent_id="copilot")
        copilot_payload = state.status_snapshot()
        self.assertEqual(copilot_payload["agent_id"], "copilot")
        self.assertEqual(copilot_payload["weekly_title"], "Copilot Monthly")
        self.assertEqual(copilot_payload["weekly_pct"], 0.5)
        self.assertEqual(copilot_payload["session_pct"], 0.0)

    def test_pending_session_is_not_pruned_by_waiting_timeout(self):
        state = self.make_state()
        state.update_session("question-session", "waiting", agent_id="claude")
        with state._lock:
            state._sessions["question-session"]["time"] = 0

        active, status, _, _ = state.overall_status({"question-session"})

        self.assertEqual(active, 1)
        self.assertEqual(status, "waiting")

    def test_status_snapshot_includes_session_reset_capability(self):
        state = self.make_state()
        state.set_agent_capability("codex", "session_reset_supported", True)
        state.update_usage(usages={
            "codex": {
                "session_pct": 0.2,
                "weekly_pct": 0.4,
                "rateLimitResetCredits": {"availableCount": 2},
            },
        })

        usage = state.status_snapshot()["per_agent_usage"]["codex"]

        self.assertTrue(usage["session_reset_supported"])
        self.assertEqual(usage["rateLimitResetCredits"]["availableCount"], 2)


class UsagePollerTests(unittest.TestCase):
    def make_state(self):
        return CodelightState(
            default_agent_id="claude",
            agent_registry=codelight.AGENT_REGISTRY,
            idle_window=600,
            idle_window_waiting=30,
        )

    def test_usage_summary_formats_available_agents(self):
        self.assertEqual(
            usage_summary(
                usages={
                    "claude": {"session_pct": 0.12, "weekly_pct": 0.34},
                    "codex": {"session_pct": 0.56, "weekly_pct": 0.78},
                    "copilot": {"monthly_pct": 0.9},
                },
            ),
            "Claude 12%/34%  Codex 56%/78%  Copilot 90%",
        )

    def test_poll_once_updates_state_and_clears_missing_optional_agents(self):
        state = self.make_state()
        logs: list[str] = []
        pushes: list[bool] = []
        state.update_usage(usages={
            "codex": {"session_pct": 0.9, "weekly_pct": 0.8},
            "copilot": {"monthly_pct": 0.7, "monthly_reset": "1d"},
        })

        poller = UsagePoller(
            state=state,
            fetchers={
                "claude": lambda: {"session_pct": 0.1, "weekly_pct": 0.2},
                "codex": lambda: None,
                "copilot": lambda: None,
            },
            interval=60,
            shutdown=threading.Event(),
            log=logs.append,
            push=lambda: pushes.append(True),
        )

        poller.poll_once()

        snapshot = state.status_snapshot()
        self.assertEqual(snapshot["per_agent_usage"]["claude"]["session_pct"], 0.1)
        self.assertNotIn("codex", snapshot["per_agent_usage"])
        self.assertNotIn("copilot", snapshot["per_agent_usage"])
        self.assertEqual(logs[-1], "[usage] Claude 10%/20%")
        self.assertEqual(pushes, [True])

    def test_agent_registry_composes_agent_specific_fetchers(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = os.path.join(tmp, "token")
            with open(token_file, "w") as stream:
                stream.write("token")

            def api(path, token):
                self.assertEqual(token, "token")
                if "/ai_credit/usage?" in path:
                    return {
                        "usageItems": [{
                            "product": "Copilot",
                            "unitType": "ai-credits",
                            "grossQuantity": 100,
                        }],
                    }
                return {
                    "plan_type": "business",
                    "seat_breakdown": {"total": 1},
                }

            registry = AgentRegistry(
                agents_config={
                    "claude": {
                        "settings_path": os.path.join(tmp, "settings.json"),
                        "credentials_path": os.path.join(tmp, "missing.json"),
                    },
                    "codex": {"home": tmp},
                    "copilot": {
                        "home": tmp,
                        "github_org": "Org",
                        "github_token_file": token_file,
                    },
                },
                claude_usage_api="https://example.invalid",
                github_api=api,
            )

            copilot = registry.agent("copilot")
            usage = copilot.get_usage(
                now=datetime(2026, 7, 9, tzinfo=timezone.utc)
            )

            self.assertEqual(copilot.token(), "token")
            self.assertEqual(set(registry.usage_fetchers()),
                             {"claude", "codex", "copilot", "cursor", "opencode"})
            self.assertIsNotNone(usage)
            self.assertEqual(usage["used_credits"], 100)

    def test_registry_session_reset_consumer_is_agent_scoped(self):
        integration = agents_base.AgentIntegration(
            spec=agents_base.AgentSpec("resetter", "Resetter"),
            session_reset_consumer=lambda: {"ok": True, "outcome": "reset"},
        )
        registry = AgentRegistry(modules=(), extra_agents=(integration,))

        self.assertTrue(registry.session_reset_supported("resetter"))
        self.assertEqual(
            registry.consume_session_reset("resetter")["outcome"], "reset")
        self.assertFalse(registry.session_reset_supported("missing"))
        self.assertEqual(
            registry.consume_session_reset("missing")["outcome"],
            "unsupported",
        )


class ConversationRefresherTests(unittest.TestCase):
    def test_refreshes_only_when_clients_have_changed_transcript(self):
        with tempfile.NamedTemporaryFile() as stream:
            path = stream.name
            broadcasts: list[bool] = []
            has_clients = False
            refresher = ConversationRefresher(
                active_path=lambda: path,
                has_clients=lambda: has_clients,
                broadcast=lambda: broadcasts.append(True),
                shutdown=threading.Event(),
            )

            os.utime(path, (1, 1))
            self.assertFalse(refresher.refresh_if_changed())
            self.assertEqual(broadcasts, [])

            has_clients = True
            self.assertTrue(refresher.refresh_if_changed())
            self.assertEqual(broadcasts, [True])

            self.assertFalse(refresher.refresh_if_changed())
            self.assertEqual(broadcasts, [True])

            os.utime(path, (2, 2))
            self.assertTrue(refresher.refresh_if_changed())
            self.assertEqual(broadcasts, [True, True])

    def test_refresh_detects_size_change_when_mtime_is_unchanged(self):
        with tempfile.NamedTemporaryFile() as stream:
            path = stream.name
            broadcasts: list[bool] = []
            refresher = ConversationRefresher(
                active_path=lambda: path,
                has_clients=lambda: True,
                broadcast=lambda: broadcasts.append(True),
                shutdown=threading.Event(),
            )

            stream.write(b"one")
            stream.flush()
            os.utime(path, (10, 10))
            self.assertTrue(refresher.refresh_if_changed())

            stream.write(b"two")
            stream.flush()
            os.utime(path, (10, 10))
            self.assertTrue(refresher.refresh_if_changed())
            self.assertEqual(broadcasts, [True, True])


class ServiceInstallTests(unittest.TestCase):
    def test_build_args_line_quotes_user_values_and_sorts_agents(self):
        args = service_core.build_args_line(
            name="henrik laptop",
            secret="secret value",
            ws_port=9999,
            verbose=True,
            remote_control=True,
            permission_timeout=42,
            agents={"codex", "claude"},
        )

        self.assertEqual(
            args,
            "--name 'henrik laptop' --secret 'secret value' --ws-port 9999 "
            "--verbose --remote-control --permission-timeout 42 "
            "--agents claude,codex",
        )

    def test_uninstall_service_removes_unit_and_reloads_systemd(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_dir = os.path.join(tmp, ".config", "systemd", "user")
            os.makedirs(service_dir)
            service_path = os.path.join(service_dir, "codelight.service")
            with open(service_path, "w") as stream:
                stream.write("unit")
            calls: list[list[str]] = []

            def run(cmd, **_kwargs):
                calls.append(cmd)
                return mock.Mock(returncode=0, stderr="")

            with mock.patch.dict(os.environ, {"HOME": tmp}):
                service_core.uninstall_service(run=run)

            self.assertFalse(os.path.exists(service_path))
            self.assertEqual(calls, [
                ["systemctl", "--user", "disable", "--now", "codelight"],
                ["systemctl", "--user", "daemon-reload"],
            ])


class DiscoveryTests(unittest.TestCase):
    def test_mdns_advertiser_registers_and_cleans_up(self):
        events: list[str] = []
        logs: list[str] = []

        class StopAfterOne:
            def __init__(self):
                self.count = 0

            def is_set(self):
                return self.count > 0

            def wait(self, _timeout):
                self.count += 1

        class FakeZeroconf:
            def __init__(self, interfaces):
                events.append(f"zc:{interfaces[0]}")

            def register_service(self, info):
                events.append(f"register:{info.name}:{info.port}")

            def unregister_service(self, info):
                events.append(f"unregister:{info.name}")

            def close(self):
                events.append("close")

        class FakeServiceInfo:
            def __init__(self, _type, name, addresses, port, properties):
                self.name = name
                self.addresses = addresses
                self.port = port
                self.properties = properties

        discovery_core.advertise_mdns(
            port=8765,
            name="laptop",
            shutdown=StopAfterOne(),
            zeroconf_cls=FakeZeroconf,
            service_info_cls=FakeServiceInfo,
            log=logs.append,
            local_ip=lambda: "192.168.1.2",
        )

        self.assertEqual(events, [
            "zc:192.168.1.2",
            "register:laptop._codelight._tcp.local.:8765",
            "unregister:laptop._codelight._tcp.local.",
            "close",
        ])
        self.assertEqual(logs, ["[mdns] advertising on 192.168.1.2:8765"])


class HookRuntimeTests(unittest.TestCase):
    def test_hook_input_helpers_accept_agent_field_variants(self):
        data = hook_runtime.parse_json_object(json.dumps({
            "sessionId": "s1",
            "hookEventName": "PreToolUse",
            "toolName": "Bash",
            "toolArgs": {"command": "npm test"},
        }))

        self.assertEqual(hook_runtime.session_id(data), "s1")
        self.assertEqual(hook_runtime.hook_event_name(data), "PreToolUse")
        self.assertEqual(hook_runtime.tool_name(data), "Bash")
        self.assertEqual(hook_runtime.tool_input(data), {"command": "npm test"})
        self.assertEqual(hook_runtime.parse_json_object("not json"), {})

    def test_question_helpers_extract_supported_shapes(self):
        self.assertTrue(hook_runtime.is_question_tool(
            "Bash", {"questions": [{"question": "Proceed?"}]}
        ))
        self.assertTrue(hook_runtime.is_question_tool("AskUserQuestion", {}))
        self.assertEqual(
            hook_runtime.questions_from_input({}, {"question": "  Continue? "}),
            [{"question": "Continue?"}],
        )
        self.assertEqual(
            hook_runtime.questions_from_input({"questions": [{"q": "x"}]}, {}),
            [{"q": "x"}],
        )

    def test_permission_decision_outputs_host_shapes(self):
        self.assertEqual(
            hook_runtime.permission_decision_output("allow"),
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                },
            },
        )
        self.assertEqual(
            hook_runtime.permission_decision_output(
                "deny", envelope=agents_base.BEHAVIOR, reason="nope"),
            {"behavior": "deny", "message": "nope"},
        )
        self.assertEqual(
            hook_runtime.permission_decision_output(
                "deny", envelope=agents_base.PRETOOL_DECISION, reason="nope"
            ),
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "nope",
                },
            },
        )

    def test_hook_mode_strings_are_stable(self):
        # These tokens are persisted in users' installed hook files.
        self.assertEqual(
            set(codelight._new_agent_registry().hook_modes()),
            {
                "permission", "permission-copilot", "permission-vscode",
                "permission-cursor",
                "question", "question-vscode", "question-codex",
            },
        )

    def test_question_updated_input_output_includes_answer_aliases(self):
        output = hook_runtime.question_updated_input_output(
            {"question": "Proceed?"}, {"Proceed?": "yes"}
        )
        hook = output["hookSpecificOutput"]

        self.assertEqual(hook["permissionDecision"], "allow")
        self.assertEqual(hook["updatedInput"]["answer"], "yes")
        self.assertEqual(hook["modifiedArgs"], hook["updatedInput"])


class HookIoTests(unittest.TestCase):
    def test_write_monitor_state_writes_and_removes_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            hook_io.write_monitor_state(
                tmp,
                session_id="s1",
                state="working",
                agent_id="claude",
                hook_event="PreToolUse",
            )
            path = os.path.join(tmp, "s1.json")
            with open(path) as stream:
                data = json.load(stream)

            self.assertEqual(data["state"], "working")
            self.assertEqual(data["agent_id"], "claude")
            self.assertEqual(data["hook_event"], "PreToolUse")

            hook_io.write_monitor_state(
                tmp,
                session_id="s1",
                state="ended",
                agent_id="claude",
            )
            self.assertFalse(os.path.exists(path))

    def test_read_json_message_reads_until_newline(self):
        class FakeConn:
            def __init__(self):
                self.chunks = [b'{"hello": "world"}\nignored']

            def recv(self, _size):
                return self.chunks.pop(0) if self.chunks else b""

        self.assertEqual(
            hook_io.read_json_message(FakeConn(), max_bytes=1024),
            {"hello": "world"},
        )


class RemotePayloadTests(unittest.TestCase):
    def display(self, agent_id):
        return {"claude": "Claude", "codex": "Codex"}.get(agent_id, "Unknown")

    def test_permission_payload_contains_policy_actions_and_agent_display(self):
        payload = remote_payloads.permission_request_payload(
            {
                "id": "p1",
                "tool_name": "Bash",
                "summary": "Bash: npm test",
                "tool_input": {"command": "npm test"},
                "session_id": "s1",
                "agent_id": "claude",
                "cwd": "/repo",
                "expires": 123.9,
            },
            agent_display_name=self.display,
            allow_folder_available=True,
            allow_command_available=False,
        )

        self.assertEqual(payload["type"], "permission_request")
        self.assertEqual(payload["agent_display"], "Claude")
        self.assertTrue(payload["allow_folder_available"])
        self.assertFalse(payload["allow_command_available"])
        self.assertEqual(payload["expires_at"], 123)

    def test_resolved_payload_includes_policy_persistence_metadata(self):
        payload = remote_payloads.permission_resolved_payload(
            {"id": "p1", "agent_id": "codex"},
            decision="allow_command",
            by="android",
            persistence={"kind": "command", "value": "npm test", "persisted": True},
            agent_display_name=self.display,
        )

        self.assertEqual(payload["type"], "permission_resolved")
        self.assertEqual(payload["agent_display"], "Codex")
        self.assertEqual(payload["policy_kind"], "command")
        self.assertTrue(payload["policy_persisted"])

    def test_question_payloads_include_agent_identity(self):
        request = remote_payloads.question_request_payload(
            {
                "id": "q1",
                "questions": [{"question": "Proceed?"}],
                "session_id": "s1",
                "agent_id": "claude",
                "cwd": "/repo",
                "expires": 456,
            },
            agent_display_name=self.display,
        )
        resolved = remote_payloads.question_resolved_payload(
            {"id": "q1", "agent_id": "claude"},
            by="vscode",
            agent_display_name=self.display,
        )

        self.assertEqual(request["type"], "question_request")
        self.assertEqual(request["agent_display"], "Claude")
        self.assertEqual(resolved["type"], "question_resolved")
        self.assertEqual(resolved["by"], "vscode")


class RemoteControlTests(unittest.TestCase):
    @staticmethod
    def make_manager(pending, allow_tool_calls):
        def allow_tool(tool):
            allow_tool_calls.append(tool)
            return True, tool

        return remote_control.RemoteRequestManager(
            pending=pending,
            permission_timeout=lambda: 60,
            remote_permissions=lambda: True,
            remote_questions=lambda: True,
            normalize_agent_id=lambda a: a or "claude",
            permission_payload=lambda e: {},
            question_payload=lambda e: {},
            permission_resolved_payload=lambda e, o, b, p: {},
            question_resolved_payload=lambda e, b: {},
            broadcast_remote=lambda p, s: None,
            update_session=lambda s, st, a: None,
            push_status=lambda: None,
            log=lambda m: None,
            allow_folder=lambda cwd: (True, cwd),
            allow_command=lambda c, cwd: (True, c),
            allow_tool=allow_tool,
            can_answer_questions=lambda: True,
            last_question_client_gone=lambda: 0.0,
            no_client_grace=6,
            reconnect_window=30,
        )

    @staticmethod
    def make_permission_entry(request_id, session_id="s1", tool_name="WebFetch"):
        return {
            "responder": lambda payload: None,
            "id": request_id,
            "session_id": session_id,
            "agent_id": "claude",
            "tool_name": tool_name,
            "summary": tool_name,
            "tool_input": {},
            "policy_command": "",
            "cwd": "/tmp",
            "event": threading.Event(),
            "decision": None,
            "by": None,
            "expires": 10 ** 12,
        }

    def test_session_tool_allowances_scope_and_cleanup(self):
        allowances = remote_control.SessionToolAllowances()

        self.assertTrue(allowances.allow("s1", "WebFetch"))
        self.assertTrue(allowances.is_allowed("s1", "WebFetch"))
        self.assertFalse(allowances.is_allowed("s1", "Bash"))
        self.assertFalse(allowances.is_allowed("s2", "WebFetch"))
        self.assertFalse(allowances.allow("unknown", "WebFetch"))
        self.assertFalse(allowances.allow("s1", "?"))

        allowances.clear("s1")
        self.assertFalse(allowances.is_allowed("s1", "WebFetch"))

        # Bounded: sessions beyond the cap evict the oldest entry.
        for index in range(remote_control.SessionToolAllowances.MAX_SESSIONS + 1):
            allowances.allow(f"session-{index}", "WebFetch")
        self.assertFalse(allowances.is_allowed("session-0", "WebFetch"))

    def test_allow_tool_session_resolves_and_auto_answers_next_request(self):
        pending = remote_control.PendingRequests()
        allow_tool_calls: list[str] = []
        manager = self.make_manager(pending, allow_tool_calls)

        entry = self.make_permission_entry("req1")
        pending.add_permission("req1", entry)
        self.assertTrue(
            manager.resolve_permission("req1", "allow_tool_session", "test"))
        self.assertEqual(entry["decision"], "allow")
        self.assertEqual(entry["persistence"]["kind"], "tool_session")
        self.assertTrue(manager.session_allowances.is_allowed("s1", "WebFetch"))
        self.assertEqual(allow_tool_calls, [])

        # The next request for the same session+tool is answered instantly,
        # without broadcasting to clients.
        class FakeConn:
            def __init__(self):
                self.sent = b""

            def sendall(self, data):
                self.sent += data

            def close(self):
                pass

        conn = FakeConn()
        manager.register_permission(conn, {
            "prompt_id": "req2",
            "session_id": "s1",
            "tool_name": "WebFetch",
            "tool_input": {},
        })
        self.assertEqual(json.loads(conn.sent.decode()), {"decision": "allow"})
        self.assertIsNone(pending.pop_permission("req2"))

        # SessionEnd clears the allowance.
        manager.clear_session_allowances("s1")
        self.assertFalse(manager.session_allowances.is_allowed("s1", "WebFetch"))

    def test_allow_tool_forever_persists_via_callback(self):
        pending = remote_control.PendingRequests()
        allow_tool_calls: list[str] = []
        manager = self.make_manager(pending, allow_tool_calls)

        entry = self.make_permission_entry("req1", tool_name="WebSearch")
        pending.add_permission("req1", entry)
        self.assertTrue(manager.resolve_permission("req1", "allow_tool", "test"))
        self.assertEqual(entry["decision"], "allow")
        self.assertEqual(entry["persistence"]["kind"], "tool")
        self.assertEqual(allow_tool_calls, ["WebSearch"])

    def test_completion_event_policy_matches_pending_prompt_lifecycle(self):
        self.assertFalse(remote_control.should_cancel_pending_for_hook(
            "working", "PostToolUse"))
        self.assertTrue(remote_control.should_cancel_pending_for_hook(
            "working", "PermissionDenied"))
        self.assertTrue(remote_control.should_cancel_pending_for_hook(
            "ended", ""))
        self.assertFalse(remote_control.should_cancel_pending_for_hook(
            "working", "PreToolUse"))

    def test_question_wait_remaining_uses_grace_only_when_nobody_can_answer(self):
        entry = {"expires": 200.0}

        self.assertEqual(
            remote_control.question_wait_remaining(
                entry,
                can_answer=True,
                last_client_gone=0.0,
                reconnect_window=30.0,
                grace_deadline=110.0,
                now=100.0,
            ),
            100.0,
        )
        self.assertEqual(
            remote_control.question_wait_remaining(
                entry,
                can_answer=False,
                last_client_gone=90.0,
                reconnect_window=30.0,
                grace_deadline=110.0,
                now=100.0,
            ),
            100.0,
        )
        self.assertEqual(
            remote_control.question_wait_remaining(
                entry,
                can_answer=False,
                last_client_gone=0.0,
                reconnect_window=30.0,
                grace_deadline=110.0,
                now=100.0,
            ),
            10.0,
        )


class OpenCodeIntegrationTests(unittest.TestCase):
    def _make_db(self, path, rows):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE session (id TEXT, cost REAL, time_created INTEGER)")
        conn.executemany(
            "INSERT INTO session (id, cost, time_created) VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()

    def _ms(self, dt):
        return int(dt.timestamp() * 1000)

    def test_month_cost_sums_only_current_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "opencode.db")
            now = datetime(2026, 7, 13, tzinfo=timezone.utc)
            self._make_db(db, [
                ("ses1", 1.50, self._ms(datetime(2026, 7, 2, tzinfo=timezone.utc))),
                ("ses2", 2.25, self._ms(datetime(2026, 7, 12, tzinfo=timezone.utc))),
                ("ses3", 9.99, self._ms(datetime(2026, 6, 20, tzinfo=timezone.utc))),
            ])
            self.assertAlmostEqual(opencode_agent.month_cost_usd(db, now=now), 3.75)

    def test_get_usage_meter_shape_and_budget_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "opencode.db")
            month_start = datetime.now(timezone.utc).replace(
                day=1, hour=12, minute=0, second=0, microsecond=0)
            self._make_db(db, [("ses1", 10.0, self._ms(month_start))])
            # No budget configured -> no meter.
            self.assertIsNone(opencode_agent.get_usage(db, 0))
            # Budget -> monthly_pct is a fraction, plus $ fields.
            usage = opencode_agent.get_usage(db, 40.0)
            self.assertAlmostEqual(usage["monthly_pct"], 0.25)
            self.assertEqual(usage["spent_usd"], 10.0)
            self.assertEqual(usage["budget_usd"], 40.0)
            self.assertIn("monthly_reset", usage)

    def test_get_usage_none_when_store_missing(self):
        self.assertIsNone(opencode_agent.get_usage("/no/such/opencode.db", 40.0))

    def test_registry_autodiscovers_opencode_listener_and_budget(self):
        registry = codelight._new_agent_registry()
        self.assertIn("opencode", registry.supported_agent_ids())
        integ = opencode_agent.build_integration({})
        self.assertIsNone(integ.install_hooks)          # no hooks
        self.assertIsNotNone(integ.background_listener)  # SSE listener instead
        # The usage fetcher is always wired; it returns None while the budget
        # is 0 so the meter stays hidden until one is set (from config or app).
        self.assertIsNotNone(integ.usage_fetcher)
        self.assertIn("opencode", registry.background_listeners())
        self.assertIn("opencode", registry.budget_agents())
        self.assertTrue(
            registry.client_metadata()["opencode"]["budget_settable"])

    def test_messages_to_lines_and_conversation_capable(self):
        messages = [
            {"type": "user", "text": "run echo hi"},
            {"type": "system", "text": "internal skills blurb"},
            {"type": "assistant", "content": [
                {"type": "tool", "tool": "bash"},
                {"type": "text", "text": "Done."},
            ]},
        ]
        lines = opencode_agent.messages_to_lines(messages)
        self.assertEqual(lines[0], {"role": "user", "text": "run echo hi"})
        # system dropped; assistant prose + tool line (prose first, then tail)
        self.assertEqual(lines[1], {"role": "assistant", "text": "Done."})
        self.assertEqual(lines[2], {"role": "tool", "text": "⚙ bash"})
        # OpenCode is conversation-capable via its provider (no extractor).
        registry = codelight._new_agent_registry()
        self.assertIn("opencode", registry.conversation_agents())
        self.assertIsNotNone(registry.conversation_provider_for("opencode"))

    def test_budget_set_get_roundtrip_and_meter_toggle(self):
        agent = opencode_agent.OpenCodeAgent(db_path="/no/such.db",
                                             monthly_budget_usd=0)
        self.assertIsNone(agent.get_usage())   # no budget → hidden
        agent.set_budget(25)
        self.assertEqual(agent.monthly_budget_usd, 25.0)
        agent.set_budget(-5)                    # clamped to 0
        self.assertEqual(agent.monthly_budget_usd, 0.0)

    def test_question_conversion_to_codelight_and_back(self):
        oc_q = [
            {"question": "Pick one", "header": "H",
             "options": [{"label": "A"}, {"label": "B"}], "multiple": False},
            {"question": "Pick many", "header": "M",
             "options": [{"label": "X"}, {"label": "Y"}], "multiple": True},
        ]
        cl = opencode_agent._to_codelight_questions(oc_q)
        self.assertEqual(cl[0]["options"], [{"label": "A"}, {"label": "B"}])
        self.assertFalse(cl[0]["multiSelect"])
        self.assertTrue(cl[1]["multiSelect"])
        # codelight answers ({question: answer_string}) → OpenCode [[labels]].
        oc_a = opencode_agent._to_opencode_answers(
            oc_q, {"Pick one": "A", "Pick many": "X, Y"})
        self.assertEqual(oc_a, [["A"], ["X", "Y"]])

    def test_permission_responder_maps_decisions_to_replies(self):
        posts = []
        agent = opencode_agent.OpenCodeAgent(db_path="", monthly_budget_usd=0)
        agent._post = lambda ctx, path, body: posts.append((path, body))
        respond = agent._permission_responder(object(), "ses1", "per1")
        respond({"decision": "allow"})                                   # once
        respond({"decision": "allow",
                 "persistence": {"requested": True, "kind": "tool"}})    # always
        respond({"decision": "deny"})                                    # reject
        respond({"decision": None})                                      # no POST
        self.assertEqual([b["reply"] for _, b in posts], ["once", "always", "reject"])
        self.assertTrue(all("/permission/per1/reply" in p for p, _ in posts))


class SelfInvocationTests(unittest.TestCase):
    def test_resolves_interpreter_and_checkout_script(self):
        interpreter, script = invocation_core.self_invocation()
        self.assertTrue(interpreter)
        self.assertTrue(script.endswith("codelight.py"))
        self.assertTrue(os.path.isfile(script))

    def test_hook_command_base_uses_self_invocation(self):
        # Single source of truth: hook commands are prefixed with the same
        # interpreter self_invocation() resolves (not a hard-coded "python3").
        interpreter, _ = invocation_core.self_invocation()
        base = hooks_core.hook_command_base("/x/codelight.py", "grok")
        self.assertTrue(base.startswith(shlex.quote(interpreter)))
        self.assertIn("/x/codelight.py", base)
        self.assertIn("--agent grok --hook", base)


class HookAgentResolutionTests(unittest.TestCase):
    def test_grok_env_retags_harness_hooks(self):
        # Grok runs Claude/Cursor compat hooks with GROK_* env set → re-tag.
        with mock.patch.dict(os.environ,
                             {"GROK_SESSION_ID": "grok-1", "GROK_HOOK_EVENT": "stop"},
                             clear=False):
            self.assertEqual(hook_commands.resolve_hook_agent("claude"),
                             ("grok", "grok-1"))
        with mock.patch.dict(os.environ, {"GROK_HOOK_EVENT": "pre_tool_use"},
                             clear=False):
            agent, sid = hook_commands.resolve_hook_agent("cursor")
            self.assertEqual(agent, "grok")
            self.assertEqual(sid, "")

    def test_real_claude_is_not_retagged(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GROK_")}
        with mock.patch.dict(os.environ, env, clear=True):
            os.environ["CLAUDE_CODE_SESSION_ID"] = "c1"
            self.assertEqual(hook_commands.resolve_hook_agent("claude"),
                             ("claude", ""))


class GrokIntegrationTests(unittest.TestCase):
    def test_usage_budget_meter_from_management_api(self):
        responses = {
            "/auth/teams": {"teams": [{"teamId": "team-1"}]},
            "/v1/billing/teams/team-1/postpaid/invoice/preview": {
                "coreInvoice": {"amountAfterVat": "2500"},   # $25.00 spent
                "effectiveSpendingLimit": "10000",           # $100.00 limit
                "billingCycle": {"year": 2026, "month": 7},
            },
        }

        def fake_get(key, path):
            return responses[path]

        with mock.patch.object(grok_agent, "_management_get", side_effect=fake_get):
            usage, team = grok_agent.get_usage("mgmt-key")
        self.assertEqual(team, "team-1")
        self.assertAlmostEqual(usage["monthly_pct"], 0.25, places=4)
        self.assertEqual(usage["spent_usd"], 25.0)
        self.assertEqual(usage["limit_usd"], 100.0)
        self.assertEqual(usage["limits"][0]["label"], "Monthly")

        # No spending limit → fall back to prepaid credits (used / purchased).
        responses["/v1/billing/teams/team-1/postpaid/invoice/preview"] = {
            "coreInvoice": {"amountAfterVat": "0",
                            "prepaidCredits": {"val": "500"},   # $5.00 bought
                            "prepaidCreditsUsed": {"val": "150"}},  # $1.50 used
            "effectiveSpendingLimit": "0",
            "billingCycle": {"year": 2026, "month": 7},
        }
        with mock.patch.object(grok_agent, "_management_get", side_effect=fake_get):
            usage, _ = grok_agent.get_usage("mgmt-key", team_id="team-1")
        self.assertAlmostEqual(usage["monthly_pct"], 0.30, places=4)
        self.assertEqual(usage["limit_usd"], 5.0)

        # Neither a limit nor prepaid credits → nothing to meter.
        responses["/v1/billing/teams/team-1/postpaid/invoice/preview"] = {
            "coreInvoice": {"amountAfterVat": "0"},
            "effectiveSpendingLimit": "0",
            "billingCycle": {"year": 2026, "month": 7},
        }
        with mock.patch.object(grok_agent, "_management_get", side_effect=fake_get):
            usage, _ = grok_agent.get_usage("mgmt-key", team_id="team-1")
        self.assertIsNone(usage)

        # No key → no meter, no call.
        self.assertEqual(grok_agent.get_usage(""), (None, ""))

    def test_transcript_extractor_parses_grok_chat_history(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w") as stream:
                for record in [
                    {"type": "system", "content": "ignore me"},
                    {"type": "user", "synthetic_reason": "ctx",
                     "content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"}]},
                    {"type": "user",
                     "content": [{"type": "text", "text": "<user_query>\nRun echo hej\n</user_query>"}]},
                    {"type": "reasoning", "content": None},
                    {"type": "assistant", "content": "",
                     "tool_calls": [{"id": "1", "name": "run_terminal_command",
                                     "arguments": '{"command":"echo hej"}'}]},
                    {"type": "tool_result", "tool_call_id": "1", "content": "exit: 0\nhej\n"},
                    {"type": "assistant", "content": "Done."},
                ]:
                    stream.write(json.dumps(record) + "\n")
            lines = transcript_core.parse_transcript(
                path, tool_summary=policy_core.tool_summary,
                extractors=(grok_agent.transcript_extractor,))
        finally:
            os.unlink(path)

        self.assertEqual([l["role"] for l in lines],
                         ["user", "tool", "output", "assistant"])
        self.assertEqual(lines[0]["text"], "Run echo hej")   # unwrapped, ctx dropped
        self.assertEqual(lines[1]["text"], "run_terminal_command: echo hej")
        self.assertEqual(lines[3]["text"], "Done.")

    def test_session_path_matches_id_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            sid = "019f5637-5c3d-72b2-84cc-39c01455eaac"
            d = os.path.join(tmp, "sessions", "%2Fhome%2Fx", sid)
            os.makedirs(d)
            path = os.path.join(d, "chat_history.jsonl")
            with open(path, "w") as stream:
                stream.write("{}")
            self.assertEqual(grok_agent.sessions_path_for_session(tmp, sid), path)
            self.assertEqual(grok_agent.sessions_path_for_session(tmp, "nope"), "")

    def test_install_hooks_writes_status_only_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_file = os.path.join(tmp, "hooks", "codelight.json")
            grok_agent.install_hooks(
                hooks_file, "/opt/codelight.py",
                hook_wait_ceiling=590,
                remote_permissions=True,   # ignored: Grok hooks are deny-only
            )
            with open(hooks_file) as stream:
                doc = json.load(stream)

        hooks = doc["hooks"]
        # Grok matcher-group format: event → [{hooks: [{type, command}]}].
        def command_of(event):
            return hooks[event][0]["hooks"][0]["command"]
        def state_of(event):
            return command_of(event).rsplit(" ", 1)[-1]

        self.assertEqual(state_of("UserPromptSubmit"), "working")
        self.assertEqual(state_of("Notification"), "waiting")
        self.assertEqual(state_of("Stop"), "ended")
        self.assertEqual(state_of("SessionEnd"), "ended")
        self.assertIn("--agent grok", command_of("Stop"))
        # SessionStart must NOT mark "working": Grok's leader session fires only
        # SessionStart, which would otherwise leave a phantom working session
        # (IDLE_WINDOW=600s) masking the real session's "waiting" state.
        self.assertNotIn("SessionStart", hooks)
        # No permission/question forwarding — Grok's PreToolUse cannot approve.
        self.assertNotIn("permission", json.dumps(doc))
        self.assertNotIn("question", json.dumps(doc))

    def test_ensure_compat_hooks_off(self):
        import tomllib
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.toml")
            with open(cfg, "w") as stream:
                stream.write('[cli]\ninstaller = "internal"\n\n'
                             '[compat.claude]\nhooks = true\nother = 1\n')
            grok_agent.ensure_compat_hooks_off(cfg)
            with open(cfg, "rb") as stream:
                parsed = tomllib.load(stream)
            # both harnesses disabled, unrelated keys preserved
            self.assertIs(parsed["compat"]["claude"]["hooks"], False)
            self.assertIs(parsed["compat"]["cursor"]["hooks"], False)
            self.assertEqual(parsed["compat"]["claude"]["other"], 1)
            self.assertEqual(parsed["cli"]["installer"], "internal")
            # idempotent: a second run leaves the file byte-identical
            before = open(cfg).read()
            grok_agent.ensure_compat_hooks_off(cfg)
            self.assertEqual(open(cfg).read(), before)

    def test_registry_exposes_grok_status_only(self):
        registry = codelight._new_agent_registry()
        self.assertIn("grok", registry.supported_agent_ids())
        self.assertEqual(registry.executables_by_agent()["grok"], ("grok",))
        # No remote-control hook modes and no VSCode extension.
        self.assertNotIn("grok", registry.vscode_extensions_by_agent())
        self.assertEqual(grok_agent.HOOK_MODES, ())


class CursorIntegrationTests(unittest.TestCase):
    def test_install_merges_with_user_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_file = os.path.join(tmp, "hooks.json")
            with open(hooks_file, "w") as stream:
                json.dump({
                    "version": 1,
                    "hooks": {
                        "beforeShellExecution": [
                            {"command": "/home/user/my-own-hook.sh"},
                            {"command": "python3 /old/codelight.py --agent cursor --hook permission-cursor"},
                        ],
                        "afterFileEdit": [{"command": "my-formatter --fix"}],
                    },
                }, stream)

            cursor_agent.install_hooks(
                hooks_file, "/opt/codelight.py",
                hook_wait_ceiling=590,
                remote_permissions=True,
                permission_timeout=60,
            )
            with open(hooks_file) as stream:
                doc = json.load(stream)

            self.assertEqual(doc["version"], 1)
            hooks = doc["hooks"]
            # The user's own hooks survive; the stale codelight entry is gone.
            shell_cmds = [e["command"] for e in hooks["beforeShellExecution"]]
            self.assertIn("/home/user/my-own-hook.sh", shell_cmds)
            self.assertNotIn(
                "python3 /old/codelight.py --agent cursor --hook permission-cursor",
                shell_cmds)
            self.assertTrue(any("permission-cursor" in cmd for cmd in shell_cmds))
            self.assertEqual(hooks["afterFileEdit"],
                             [{"command": "my-formatter --fix"}])
            self.assertTrue(any("--hook ended" in e["command"]
                                for e in hooks["stop"]))
            self.assertEqual(hooks["beforeShellExecution"][-1]["timeout"], 605)

            # Second install: no changes.
            before = json.dumps(doc, sort_keys=True)
            cursor_agent.install_hooks(
                hooks_file, "/opt/codelight.py",
                hook_wait_ceiling=590,
                remote_permissions=True,
                permission_timeout=60,
            )
            with open(hooks_file) as stream:
                self.assertEqual(json.dumps(json.load(stream), sort_keys=True),
                                 before)

    def test_uninstall_strips_flat_codelight_entries_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_file = os.path.join(tmp, "hooks.json")
            cursor_agent.install_hooks(
                hooks_file, "/opt/codelight.py",
                hook_wait_ceiling=590,
                remote_permissions=True,
                permission_timeout=60,
            )
            with open(hooks_file) as stream:
                doc = json.load(stream)
            doc["hooks"]["afterFileEdit"] = [{"command": "my-formatter --fix"}]
            with open(hooks_file, "w") as stream:
                json.dump(doc, stream)

            hooks_core.remove_matcher_group_hooks(hooks_file)
            with open(hooks_file) as stream:
                cleaned = json.load(stream)

            self.assertEqual(cleaned["hooks"],
                             {"afterFileEdit": [{"command": "my-formatter --fix"}]})

    def test_permission_cursor_envelope_and_ask_fallback(self):
        mode = codelight._new_agent_registry().hook_modes()["permission-cursor"]
        self.assertEqual(mode.envelope, agents_base.CURSOR_PERMISSION)
        self.assertEqual(mode.fallback_decision, "ask")

        self.assertEqual(
            hook_runtime.permission_decision_output(
                "deny", envelope=agents_base.CURSOR_PERMISSION, reason="nope"),
            {"permission": "deny", "agent_message": "nope"},
        )
        self.assertEqual(
            hook_runtime.permission_decision_output(
                "ask", envelope=agents_base.CURSOR_PERMISSION),
            {"permission": "ask"},
        )

        # Shell payload shim + daemon-unreachable → explicit ask fallback.
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stdout(stdout):
            hook_commands.run_permission_hook(
                mode=mode,
                agent_id="cursor",
                socket_path=os.path.join(tmp, "no-daemon.sock"),
                monitor_state_dir=os.path.join(tmp, "monitor"),
                policy_path=os.path.join(tmp, "policy.json"),
                policy_lock=threading.Lock(),
                hook_wait_ceiling=1,
                normalize_agent_id=lambda a: a or "cursor",
                agent_display_name=lambda a: "Cursor",
                input_text=json.dumps({
                    "conversation_id": "conv-1",
                    "hook_event_name": "beforeShellExecution",
                    "command": "rm -rf /tmp/x",
                    "cwd": "/tmp",
                }),
            )
        self.assertEqual(json.loads(stdout.getvalue()), {"permission": "ask"})

    def test_transcript_extractor_parses_role_message_shape(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w") as stream:
                for record in [
                    {"role": "user",
                     "message": {"content": [{"type": "text", "text": "Fix it"}]}},
                    {"role": "assistant",
                     "message": {"content": [
                         {"type": "text", "text": "On it."},
                         {"type": "tool_use", "name": "Shell",
                          "input": {"command": "echo hi"}},
                     ]}},
                    {"role": None, "message": None},
                ]:
                    stream.write(json.dumps(record) + "\n")
            lines = transcript_core.parse_transcript(
                path,
                tool_summary=lambda name, inp: f"{name}: {inp.get('command','')}",
                extractors=(cursor_agent.transcript_extractor,),
            )
        finally:
            os.unlink(path)

        self.assertEqual([l["role"] for l in lines],
                         ["user", "assistant", "tool"])
        self.assertEqual(lines[0]["text"], "Fix it")
        self.assertEqual(lines[2]["text"], "Shell: echo hi")

    def test_transcript_extractor_strips_cursor_wrappers_and_redacted(self):
        record = {"role": "user", "message": {"content": [
            {"type": "text",
             "text": "<timestamp>Sun, Jul 12</timestamp>\n"
                     "<user_query>\nfix the bug\n</user_query>"},
        ]}}
        _, content = cursor_agent.transcript_extractor(record, lambda n, i: "")
        self.assertEqual(content, [{"type": "text", "text": "fix the bug"}])

        # A trailing [REDACTED] reasoning marker is dropped; a standalone one
        # removes the block entirely.
        record = {"role": "assistant", "message": {"content": [
            {"type": "text", "text": "Done:\n\n```\nok\n```\n\n[REDACTED]"},
            {"type": "text", "text": "[REDACTED]"},
            {"type": "tool_use", "name": "Shell", "input": {"command": "ls"}},
        ]}}
        _, content = cursor_agent.transcript_extractor(record, lambda n, i: "")
        self.assertEqual(content, [
            {"type": "text", "text": "Done:\n\n```\nok\n```"},
            {"type": "tool_use", "name": "Shell", "input": {"command": "ls"}},
        ])

    def test_shell_tool_summarizes_to_command(self):
        self.assertEqual(
            policy_core.tool_summary("Shell", {"command": "echo test",
                                               "description": "run it"}),
            "Shell: echo test")
        self.assertEqual(
            policy_core.command_from_tool("Shell", {"command": "echo test"}),
            "echo test")

    def test_transcript_path_for_session_finds_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            sid = "conv-abc"
            d = os.path.join(tmp, "projects", "proj", "agent-transcripts", sid)
            os.makedirs(d)
            path = os.path.join(d, f"{sid}.jsonl")
            with open(path, "w") as stream:
                stream.write("{}")

            self.assertEqual(
                cursor_agent.transcript_path_for_session(tmp, sid),
                os.path.realpath(path))
            self.assertEqual(
                cursor_agent.transcript_path_for_session(tmp, "missing"), "")

    def test_usage_reads_local_token_and_parses_monthly_percent(self):
        import base64 as _b64
        import sqlite3 as _sqlite
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "state.vscdb")
            con = _sqlite.connect(db)
            con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            payload = _b64.urlsafe_b64encode(
                json.dumps({"sub": "google-oauth2|user_ABC"}).encode()
            ).rstrip(b"=").decode()
            con.execute("INSERT INTO ItemTable VALUES (?, ?)",
                        ("cursorAuth/accessToken", f"header.{payload}.sig"))
            con.commit()
            con.close()

            captured = {}

            class FakeResp:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self):
                    return json.dumps({
                        "membershipType": "free",
                        "billingCycleEnd": "2026-08-11T21:20:46.144Z",
                        "individualUsage": {"plan": {"totalPercentUsed": 10.5}},
                    }).encode()

            def fake_urlopen(req, timeout=0):
                captured["cookie"] = req.headers.get("Cookie", "")
                return FakeResp()

            with mock.patch.object(cursor_agent.urllib.request, "urlopen",
                                   side_effect=fake_urlopen):
                usage = cursor_agent.get_usage(db)

            self.assertIsNotNone(usage)
            self.assertAlmostEqual(usage["monthly_pct"], 0.105, places=4)
            from codelight_core import timefmt
            self.assertEqual(usage["monthly_reset_at"],
                             timefmt.epoch("2026-08-11T21:20:46.144Z"))
            self.assertEqual(usage["limits"][0]["label"], "Monthly")
            # Cookie carries the workos id from the JWT sub, not the raw prefix.
            self.assertIn("user_ABC%3A%3A", captured["cookie"])

        # No DB / no token → no meter, no crash.
        self.assertIsNone(cursor_agent.get_usage(os.path.join(tmp, "gone.vscdb")))

    def test_registry_exposes_cursor(self):
        registry = codelight._new_agent_registry()
        self.assertIn("cursor", registry.supported_agent_ids())
        self.assertEqual(registry.executables_by_agent()["cursor"],
                         ("cursor", "cursor-agent"))
        # Cursor has a usage fetcher (web API via the local auth token).
        self.assertIn("cursor", registry.usage_fetchers())

    def test_session_id_accepts_cursor_conversation_id(self):
        self.assertEqual(
            hook_runtime.session_id({"conversation_id": "c1"}), "c1")


class HookConfigTests(unittest.TestCase):
    def test_matcher_group_merge_removes_old_codelight_hooks_only(self):
        hooks = {
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": "python3 /x/codelight.py --hook working"},
                        {"type": "command", "command": "echo keep"},
                    ],
                },
            ],
        }

        changed = hooks_core.merge_matcher_group_hooks(hooks, [
            ("PreToolUse", "", {"type": "command", "command": "python3 /new/codelight.py --hook working"}),
        ])

        self.assertTrue(changed)
        commands = [h["command"] for h in hooks["PreToolUse"][0]["hooks"]]
        self.assertEqual(commands, [
            "echo keep",
            "python3 /new/codelight.py --hook working",
        ])

    def test_codex_question_hook_uses_request_user_input_matcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks_path = os.path.join(tmp, "hooks.json")
            codex_agent.install_hooks(
                hooks_path,
                "/repo/companion/codelight.py",
                hook_wait_ceiling=590,
                remote_permissions=True,
                remote_questions=True,
                permission_timeout=42,
            )
            with open(hooks_path) as stream:
                doc = json.load(stream)

        pre_tool = doc["hooks"]["PreToolUse"]
        question_slot = next(x for x in pre_tool if x["matcher"] == "^request_user_input$")
        command = question_slot["hooks"][0]["command"]
        self.assertIn("question-codex", command)
        self.assertIn("--permission-timeout 42", command)


class DashboardTests(unittest.TestCase):
    def test_renders_every_agent_and_every_limit(self):
        payload = {
            "status": "idle",
            "sessions": 0,
            "activity": ["[10:00:00] test event"],
            "clients": {"websocket": 1, "dbus": False},
            "per_agent_status": {
                "claude": "idle", "copilot": "working", "codex": "idle",
            },
            "per_agent_usage": {
                "claude": {"limits": [
                    {"label": "Weekly", "pct": 1.0, "reset": "2d"},
                    {"label": "Session", "pct": 0.1, "reset": "1h"},
                ]},
                "copilot": {"limits": [
                    {"label": "Monthly", "pct": 0.2, "reset": "20d"},
                ]},
                "codex": {"limits": [
                    {"label": "Weekly", "pct": 0.3, "reset": "3d"},
                    {"label": "Session", "pct": 0.4, "reset": "2h"},
                ]},
            },
        }
        rendered = dashboard_client.render_payload(
            payload,
            agent_registry=codelight.AGENT_REGISTRY,
            default_agent_id=codelight.DEFAULT_AGENT_ID,
            dashboard_ready=False,
        )
        self.assertIn("Claude", rendered)
        self.assertIn("Copilot", rendered)
        self.assertIn("Codex", rendered)
        self.assertEqual(rendered.count("Weekly"), 2)
        self.assertEqual(rendered.count("Session"), 2)
        self.assertIn("Monthly", rendered)
        self.assertIn("1 WebSocket", rendered)
        self.assertIn("test event", rendered)


class PendingRequestCancellationTests(unittest.TestCase):
    def tearDown(self):
        with codelight._lock:
            codelight._pending_questions.clear()
            codelight._pending_perms.clear()

    def add_question(self, session_id="session-1"):
        entry = {
            "session_id": session_id,
            "by": None,
            "event": threading.Event(),
        }
        with codelight._lock:
            codelight._pending_questions["question-1"] = entry
        return entry

    def test_concurrent_pre_tool_status_does_not_cancel_question(self):
        entry = self.add_question()

        cancelled = codelight._cancel_pending_for_hook(
            "session-1", "working", "PreToolUse")

        self.assertFalse(cancelled)
        self.assertIsNone(entry["by"])
        self.assertFalse(entry["event"].is_set())

    def test_unrelated_post_tool_status_does_not_cancel_question(self):
        entry = self.add_question()

        cancelled = codelight._cancel_pending_for_hook(
            "session-1", "working", "PostToolUse")

        self.assertFalse(cancelled)
        self.assertIsNone(entry["by"])
        self.assertFalse(entry["event"].is_set())

    def test_strong_completion_events_cancel_pending_question(self):
        for event, state in (
            ("PermissionDenied", "working"),
            ("Stop", "ended"),
            ("SessionEnd", "ended"),
        ):
            with self.subTest(event=event):
                self.tearDown()
                entry = self.add_question()

                cancelled = codelight._cancel_pending_for_hook(
                    "session-1", state, event)

                self.assertTrue(cancelled)
                self.assertEqual(entry["by"], "cancelled")
                self.assertTrue(entry["event"].is_set())

    def test_legacy_working_event_keeps_old_cancellation_behavior(self):
        entry = self.add_question()

        cancelled = codelight._cancel_pending_for_hook(
            "session-1", "working", "")

        self.assertTrue(cancelled)
        self.assertEqual(entry["by"], "cancelled")
        self.assertTrue(entry["event"].is_set())


if __name__ == "__main__":
    unittest.main()
