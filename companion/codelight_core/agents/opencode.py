"""OpenCode integration (foundation slice: detection + cost meter).

OpenCode is BYOK — it has no provider quota, so unlike the other agents there
is no "% of a limit" to show; the only meaningful usage metric is cumulative
cost in $. OpenCode records `cost` per session in its SQLite store, so we sum
that for the current calendar month and render it against a user-set monthly
budget (opt-in `agents.opencode.monthly_budget_usd`). No pricing table needed.

Status, remote permission approval, and remote question answering come from
OpenCode's HTTP server SSE event bus, not from installed hooks — that
background-listener component is the next slice (see PLAN.md, OpenCode).
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from codelight_core.agents import base
from codelight_core.timefmt import format_epoch_countdown


DEFAULT_SERVER_URL = "http://127.0.0.1:4096"
ACTIVE_POLL_INTERVAL = 2.0

# working / idle come from the authoritative active-session set (OpenCode
# v1.17 emits no session.idle/session.status event — verified 2026-07-13), so
# the SSE bus is used only for the waiting edge: a permission/question prompt
# blocks the turn (routed to codelight's remote control), and its reply or
# rejection resumes it.
_PERMISSION_ASK_EVENTS = frozenset({"permission.asked", "permission.v2.asked"})
_QUESTION_ASK_EVENTS = frozenset({"question.asked", "question.v2.asked"})
_RESUME_EVENTS = frozenset({
    "permission.replied", "permission.v2.replied",
    "question.replied", "question.v2.replied",
    "question.rejected", "question.v2.rejected",
})


def _to_codelight_questions(oc_questions: list) -> list:
    """OpenCode QuestionV2Info[] → codelight's AskUserQuestion shape."""
    out = []
    for q in oc_questions or []:
        options = []
        for opt in q.get("options") or []:
            options.append({"label": opt.get("label", "")} if isinstance(opt, dict)
                           else {"label": str(opt)})
        out.append({
            "question": q.get("question", ""),
            "header": q.get("header", ""),
            "options": options,
            "multiSelect": bool(q.get("multiple")),
        })
    return out


def messages_to_lines(messages: list, max_msgs: int = 60) -> list[dict]:
    """OpenCode `GET /api/session/{id}/message` payload → codelight conversation
    lines ({"role", "text"}). Users carry `text`; assistants carry typed
    `content` blocks (text prose + tool calls); system messages are skipped.
    Pure for testing."""
    # The API returns messages newest-first; show them oldest-first.
    ordered = sorted(
        (m for m in (messages or []) if isinstance(m, dict)),
        key=lambda m: (m.get("time") or {}).get("created", 0))
    out: list[dict] = []
    for msg in ordered:
        mtype = msg.get("type")
        if mtype == "user":
            text = str(msg.get("text") or "").strip()
            if text:
                out.append({"role": "user", "text": text[:2000]})
        elif mtype == "assistant":
            prose: list[str] = []
            tail: list[dict] = []
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    prose.append(str(block.get("text") or ""))
                elif bt == "tool":
                    name = (block.get("tool") or block.get("name")
                            or (block.get("state") or {}).get("title") or "tool")
                    tail.append({"role": "tool", "text": f"⚙ {name}"})
            prose_text = "\n".join(p for p in prose if p).strip()
            if prose_text:
                out.append({"role": "assistant", "text": prose_text[:2000]})
            out.extend(tail)
        # system / other → skipped
    return out[-max_msgs:]


def _to_opencode_answers(oc_questions: list, answers: dict) -> list:
    """codelight answers ({question_text: answer_string}) → OpenCode's
    QuestionV2Reply.answers ([[selected labels] per question, in order])."""
    result = []
    for q in oc_questions or []:
        text = str(answers.get(q.get("question", ""), "") or "")
        if bool(q.get("multiple")) and ", " in text:
            result.append([a.strip() for a in text.split(",") if a.strip()])
        else:
            result.append([text] if text else [])
    return result


# OpenCode's official mark rendered single-color (currentColor SVG +
# matching 48x48 1-bit bitmap for the screen): outer frame + filled lower
# block, derived from the 240x300 two-tone logo.
_LOGO_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 300" fill="currentColor"><path d="M0 0H240V60H0ZM0 0H60V300H0ZM180 0H240V300H180ZM0 240H240V300H0ZM60 120H180V240H60Z"/></svg>'
_LOGO_BITMAP = "B//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB/wAAD/gB/wAAD/gB/wAAD/gB/wAAD/gB/wAAD/gB/wAAD/gB/wAAD/gB/wAAD/gB/wAAD/gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////gB//////g"
SPEC = base.AgentSpec(
    agent_id="opencode",
    display="OpenCode",
    executables=("opencode",),
    color="#F1ECEC",
    logo_svg=_LOGO_SVG,
    logo_bitmap=_LOGO_BITMAP,
)


def default_db_path() -> str:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(data_home, "opencode", "opencode.db")


def _month_bounds(now: datetime) -> tuple[int, int]:
    """(start-of-this-month in ms, start-of-next-month in seconds), UTC.

    The ms value windows the SQLite query (OpenCode stores ms epochs); the
    seconds value is the meter's reset timestamp (clients/`format_epoch_
    countdown` use seconds)."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start.replace(year=start.year + 1, month=1) if start.month == 12
           else start.replace(month=start.month + 1))
    return int(start.timestamp() * 1000), int(nxt.timestamp())


def month_cost_usd(db_path: str, *, now: datetime | None = None) -> float | None:
    """Sum of `session.cost` for the current calendar month, or None if the
    store can't be read. OpenCode books cost per session, so this needs no
    model pricing table."""
    if not os.path.isfile(db_path):
        return None
    start_ms, _ = _month_bounds(now or datetime.now(timezone.utc))
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM session "
                "WHERE time_created >= ?",
                (start_ms,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    try:
        return float(row[0] or 0.0)
    except (TypeError, ValueError):
        return 0.0


def get_usage(db_path: str, monthly_budget_usd: float,
              log: Callable[[str], None] | None = None) -> dict | None:
    """Monthly spend vs a self-set budget as a `monthly_pct` meter.

    Returns None when no budget is configured (nothing to meter) or the store
    can't be read — never raises. This is a tracking meter, not enforcement:
    codelight cannot cap OpenCode spend (the real bill is at the provider)."""
    if monthly_budget_usd <= 0:
        return None
    spent = month_cost_usd(db_path)
    if spent is None:
        return None
    _, reset_at = _month_bounds(datetime.now(timezone.utc))
    pct = max(0.0, min(1.0, spent / monthly_budget_usd))
    if log:
        log(f"[opencode-usage] ${spent:.2f}/${monthly_budget_usd:.2f} ({pct:.0%})")
    return {
        "monthly_pct": pct,
        "monthly_reset": format_epoch_countdown(reset_at),
        "monthly_reset_at": reset_at,
        "spent_usd": round(spent, 2),
        "budget_usd": monthly_budget_usd,
    }


class OpenCodeAgent:
    def __init__(self, db_path: str, monthly_budget_usd: float,
                 server_url: str = DEFAULT_SERVER_URL,
                 username: str = "", password: str = "",
                 log: Callable[[str], None] | None = None) -> None:
        self.db_path = db_path
        self.monthly_budget_usd = monthly_budget_usd
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.log = log

    def get_usage(self) -> dict | None:
        return get_usage(self.db_path, self.monthly_budget_usd, self.log)

    def set_budget(self, monthly_budget_usd: float) -> None:
        self.monthly_budget_usd = max(0.0, float(monthly_budget_usd))

    def _headers(self, accept: str = "") -> dict:
        headers = {}
        if accept:
            headers["Accept"] = accept
        if self.password:
            token = base64.b64encode(
                f"{self.username or 'opencode'}:{self.password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        return headers

    def _get_data(self, path: str, timeout: float = 5.0):
        """GET a JSON endpoint and return its `data` field (the server wraps
        responses as {"data": ...})."""
        req = urllib.request.Request(self.server_url + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read() or "null")
        return body.get("data") if isinstance(body, dict) else None

    def _fetch_active_sids(self) -> set[str]:
        """The server's authoritative set of currently-working sessions
        (`GET /api/session/active` → {data: {sid: {type: running}}})."""
        data = self._get_data("/api/session/active")
        return set(data.keys()) if isinstance(data, dict) else set()

    def conversation(self) -> tuple[str, list[dict]] | None:
        """The active (or most-recently-updated) session's messages as codelight
        conversation lines, fetched from the server. None when unavailable."""
        try:
            active = self._fetch_active_sids()
            sid = next(iter(active), "")
            if not sid:
                sessions = self._get_data("/api/session")
                if isinstance(sessions, dict):
                    sessions = list(sessions.values())
                if not isinstance(sessions, list) or not sessions:
                    return None
                sid = max(
                    sessions,
                    key=lambda s: (s.get("time") or {}).get("updated", 0)
                    if isinstance(s, dict) else 0,
                ).get("id", "")
            if not sid:
                return None
            messages = self._get_data(f"/api/session/{sid}/message", timeout=8.0)
            return sid, messages_to_lines(messages if isinstance(messages, list) else [])
        except Exception:
            return None

    def _post(self, ctx: base.ListenerContext, path: str, body: dict) -> None:
        try:
            req = urllib.request.Request(
                self.server_url + path, method="POST",
                data=json.dumps(body).encode(),
                headers={**self._headers(), "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as exc:
            ctx.log(f"[opencode] reply POST {path} failed ({exc})")

    def _permission_responder(self, ctx, sid, req_id):
        def respond(payload: dict) -> None:
            decision = payload.get("decision")
            if decision == "deny":
                reply = "reject"
            elif decision == "allow":
                persistence = payload.get("persistence") or {}
                forever = (bool(persistence.get("requested"))
                           and persistence.get("kind") in ("tool", "command", "folder"))
                reply = "always" if forever else "once"
            else:
                return  # no remote decision — let OpenCode use its own prompt
            self._post(ctx, f"/api/session/{sid}/permission/{req_id}/reply",
                       {"reply": reply})
        return respond

    def _question_responder(self, ctx, sid, req_id, oc_questions):
        def respond(payload: dict) -> None:
            answers = payload.get("answers")
            if not answers:
                return  # no remote answer — OpenCode asks in its own UI
            self._post(ctx, f"/api/session/{sid}/question/{req_id}/reply",
                       {"answers": _to_opencode_answers(oc_questions, answers)})
        return respond

    def _route_event(self, event: dict, ctx: base.ListenerContext,
                     pending: set, lock) -> None:
        etype = str(event.get("type") or "")
        props = event.get("properties") or {}
        sid = str(props.get("sessionID") or "")
        if not sid:
            return
        if etype in _PERMISSION_ASK_EVENTS:
            req_id = str(props.get("id") or "")
            if not req_id:
                return
            action = str(props.get("action") or "tool")
            resources = props.get("resources") or []
            summary = action + (": " + ", ".join(str(r) for r in resources)
                                if resources else "")
            with lock:
                pending.add(sid)
            ctx.report_status(sid, "waiting", "opencode")
            ctx.submit_permission({
                "prompt_id": req_id, "session_id": sid, "agent_id": "opencode",
                "tool_name": action, "summary": summary,
                "tool_input": {"action": action, "resources": resources},
                "policy_command": "", "cwd": str(props.get("cwd") or ""),
            }, self._permission_responder(ctx, sid, req_id))
        elif etype in _QUESTION_ASK_EVENTS:
            req_id = str(props.get("id") or "")
            oc_questions = props.get("questions") or []
            if not req_id or not oc_questions:
                return
            with lock:
                pending.add(sid)
            ctx.report_status(sid, "waiting", "opencode")
            ctx.submit_question({
                "prompt_id": req_id, "session_id": sid, "agent_id": "opencode",
                "questions": _to_codelight_questions(oc_questions), "cwd": "",
            }, self._question_responder(ctx, sid, req_id, oc_questions))
        elif etype in _RESUME_EVENTS:
            with lock:
                pending.discard(sid)
            # If the user answered in OpenCode's own TUI, clear codelight's
            # still-open prompt on the phone/GNOME card.
            ctx.cancel_session_prompts(sid)

    def run_listener(self, ctx: base.ListenerContext) -> None:
        """Report OpenCode status into codelight.

        Working/idle come from polling the authoritative active-session set
        (OpenCode emits no idle event); the SSE bus supplies the waiting edge
        (a permission/question prompt) which overrides working until answered.
        Both loops reconnect/retry until shutdown."""
        pending: set[str] = set()   # sessions blocked on a permission/question
        tracked: set[str] = set()   # sessions we've reported as working
        lock = threading.Lock()

        def poll_active() -> None:
            while not ctx.shutdown.is_set():
                try:
                    active = self._fetch_active_sids()
                    with lock:
                        for sid in active:
                            if sid not in pending:
                                ctx.report_status(sid, "working", "opencode")
                                tracked.add(sid)
                        for sid in list(tracked):
                            if sid not in active and sid not in pending:
                                ctx.report_status(sid, "idle", "opencode")
                                tracked.discard(sid)
                except Exception:
                    pass  # server down / transient — next tick retries
                ctx.shutdown.wait(ACTIVE_POLL_INTERVAL)

        threading.Thread(target=poll_active, daemon=True).start()

        url = f"{self.server_url}/event"
        backoff = 1.0
        while not ctx.shutdown.is_set():
            try:
                req = urllib.request.Request(
                    url, headers=self._headers("text/event-stream"))
                with urllib.request.urlopen(req, timeout=120) as stream:
                    backoff = 1.0
                    ctx.log(f"[opencode] listening on {self.server_url}")
                    for raw in stream:
                        if ctx.shutdown.is_set():
                            return
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        self._route_event(event, ctx, pending, lock)
            except Exception as exc:
                if ctx.shutdown.is_set():
                    return
                ctx.log(f"[opencode] listener reconnecting in {backoff:.0f}s ({exc})")
                ctx.shutdown.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                backoff = min(backoff * 2, 30.0)


def build_integration(config: dict, *,
                      log: Callable[[str], None] | None = None) -> base.AgentIntegration:
    """Config keys (~/.config/codelight/config.json, agents.opencode):
    db_path (SQLite store; default ~/.local/share/opencode/opencode.db);
    monthly_budget_usd (opt-in cost meter — this calendar month's spend vs this
    budget; the meter is hidden when unset)."""
    db_path = (os.path.expanduser(str(config.get("db_path") or ""))
               or default_db_path())
    try:
        budget = float(config.get("monthly_budget_usd") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    agent = OpenCodeAgent(
        db_path, budget,
        server_url=str(config.get("server_url") or DEFAULT_SERVER_URL),
        username=str(config.get("username") or ""),
        password=str(config.get("password") or ""),
        log=log,
    )
    return base.AgentIntegration(
        spec=SPEC,
        agent=agent,
        # Always wired: get_usage returns None while the budget is 0 (meter
        # hidden), so setting a budget from the app takes effect without a
        # restart.
        usage_fetcher=agent.get_usage,
        # No install_hooks: OpenCode has no hooks. Status + remote permission/
        # question answering come from the server's SSE bus via this listener.
        background_listener=agent.run_listener,
        # The BYOK $-budget is user-settable and daemon-persisted.
        budget_getter=lambda: agent.monthly_budget_usd,
        budget_setter=agent.set_budget,
        # Conversation comes from the server API, not a JSONL file.
        conversation_provider=agent.conversation,
    )
