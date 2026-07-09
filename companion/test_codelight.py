import json
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import codelight


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
            usage = codelight._usage_from_codex_rollout(path)
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

        with mock.patch.object(codelight, "_github_api", side_effect=api):
            usage = codelight.get_copilot_usage(
                "Drivec-AB", "token",
                codelight.datetime(2026, 7, 9, tzinfo=codelight.timezone.utc))

        self.assertIsNotNone(usage)
        self.assertEqual(usage["used_credits"], 2100)
        self.assertEqual(usage["included_credits"], 21000)
        self.assertEqual(usage["monthly_pct"], 0.1)
        self.assertEqual(usage["limits"][0]["label"], "Monthly")

    def test_permission_failure_omits_detailed_usage(self):
        error = codelight.urllib.error.HTTPError(
            "https://api.github.com/test", 403, "Forbidden", {}, None)
        with mock.patch.object(codelight, "_github_api", side_effect=error):
            self.assertIsNone(codelight.get_copilot_usage(
                "Drivec-AB", "token",
                codelight.datetime(2026, 7, 9, tzinfo=codelight.timezone.utc)))


class PermissionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = os.path.join(self.tmp.name, "policy.json")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.repo, ".git"))
        self.policy_patch = mock.patch.object(codelight, "POLICY_PATH", self.policy)
        self.vscode_patch = mock.patch.object(
            codelight, "VSCODE_SETTINGS_CANDIDATES",
            [os.path.join(self.tmp.name, "missing-settings.json")])
        self.policy_patch.start()
        self.vscode_patch.start()

    def tearDown(self):
        self.vscode_patch.stop()
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
        digest = codelight.hmac.new(
            secret.encode(), nonce.encode(), codelight.hashlib.sha256
        ).hexdigest()

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
        with mock.patch.object(codelight.shutil, "which", side_effect=which), \
             mock.patch("subprocess.run", return_value=extension_result):
            detected = codelight.detect_installed_agents()

        self.assertEqual(detected, {"claude", "copilot", "codex"})

    def test_agent_set_ignores_unknown_values(self):
        self.assertEqual(
            codelight._parse_agent_set("codex, unknown, claude"),
            {"codex", "claude"},
        )


class DashboardTests(unittest.TestCase):
    def test_renders_every_agent_and_every_limit(self):
        payload = {
            "status": "idle",
            "sessions": 0,
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
        output = io.StringIO()
        with mock.patch.object(codelight.sys, "stdout", output), \
             mock.patch.object(codelight, "_dashboard_ready", False):
            codelight._render_dashboard(payload)

        rendered = output.getvalue()
        self.assertIn("Claude", rendered)
        self.assertIn("Copilot", rendered)
        self.assertIn("Codex", rendered)
        self.assertEqual(rendered.count("Weekly"), 2)
        self.assertEqual(rendered.count("Session"), 2)
        self.assertIn("Monthly", rendered)


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
