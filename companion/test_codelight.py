import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import codelight
from codelight_core.agents import base as agents_base
from codelight_core.agents import codex as codex_agent
from codelight_core.agents import copilot as copilot_agent
from codelight_core.state import CodelightState
from codelight_core import auth as auth_core
from codelight_core import dashboard_client
from codelight_core import hooks as hooks_core
from codelight_core.conversation import ConversationRefresher
from codelight_core import discovery as discovery_core
from codelight_core import hook_commands
from codelight_core import hook_io
from codelight_core import hook_runtime
from codelight_core import lifecycle
from codelight_core import remote_control
from codelight_core import remote_payloads
from codelight_core import service as service_core
from codelight_core.agents.registry import AgentRegistry
from codelight_core.usage import UsagePoller, usage_summary


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
            self.assertTrue(codelight._is_allowed_command(
                tool, tool_input, self.repo))
        self.assertFalse(codelight._is_allowed_command(
            "Bash", {"command": command + " --dangerous"}, self.repo))

        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        self.assertFalse(codelight._is_allowed_command(
            "Bash", {"command": command}, other))

    def test_trusted_patch_cannot_escape_through_symlink(self):
        self.assertTrue(codelight._allow_folder(self.repo)[0])
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.repo, "linked"))
        patch = {
            "input": "*** Update File: linked/secret.txt\n@@\n-old\n+new\n",
        }
        self.assertFalse(codelight._is_safe_trusted_apply_patch(
            "apply_patch", patch, self.repo))


class AuthenticationTests(unittest.TestCase):
    def test_hmac_authentication_is_required(self):
        secret = "test-secret"
        nonce = "abc123"
        digest = auth_core.auth_hmac(secret, nonce)

        self.assertTrue(codelight._valid_auth_response(
            {"auth_hmac": digest}, secret, nonce))
        self.assertFalse(codelight._valid_auth_response(
            {"auth": secret}, secret, nonce))
        self.assertFalse(codelight._valid_auth_response(
            {"auth_hmac": "wrong"}, secret, nonce))


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


class StateSnapshotTests(unittest.TestCase):
    def make_state(self):
        return CodelightState(
            default_agent_id="claude",
            agent_registry=codelight.AGENT_REGISTRY,
            idle_window=600,
            idle_window_waiting=30,
        )

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
            self.assertEqual(set(registry.usage_fetchers()), {"claude", "codex", "copilot"})
            self.assertIsNotNone(usage)
            self.assertEqual(usage["used_credits"], 100)


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
    def test_completion_event_policy_matches_pending_prompt_lifecycle(self):
        self.assertTrue(remote_control.should_cancel_pending_for_hook(
            "working", "PostToolUse"))
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

    def test_completion_events_cancel_pending_question(self):
        for event, state in (
            ("PostToolUse", "working"),
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
