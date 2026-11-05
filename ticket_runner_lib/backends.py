"""Each backend's agent CLI: its argv and environment, its event stream, and how a session ended.

run_agent owns the process, interrupts and records; an adapter only translates. Narration events are
CommandCode-shaped on every backend, so the colour feed looks the same whichever CLI runs.
"""

from pathlib import Path
import datetime
import json
import os
import re
import subprocess
import time

from .settings import BROWSER_MCP, BROWSER_TOOLS, CLAUDE_SETTINGS, CODEX_CONFIG, FOREIGN_AUTH, Fatal


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def codex_auth():
    return Path(os.getenv("CODEX_HOME") or str(Path.home() / ".codex")).expanduser().absolute() / "auth.json"


def strip_codex_auth():
    # Capture CODEX_HOME before calling this; no gate, install or GitHub child should inherit credentials.
    for key in list(os.environ):
        if key.startswith(("OPENAI_", "CODEX_")):
            os.environ.pop(key)


class Codex:
    name = "codex"

    def __init__(self, home, auth=None, binary=None):
        self.binary = binary or os.getenv("CODEX_BIN", "codex")
        self.home, self.auth = home, auth or codex_auth()

    def prepare(self, skills_dir):
        self.home.mkdir(parents=True, exist_ok=True)
        auth = self.home / "auth.json"
        if auth.is_symlink():
            if auth.resolve() != self.auth.resolve():
                auth.unlink()
        elif auth.exists():
            raise Fatal(f"Refusing to overwrite {auth}: expected a symlink to the operator's auth.json")
        if not auth.is_symlink():
            auth.symlink_to(self.auth)
        (self.home / "config.toml").write_text(CODEX_CONFIG)
        # Codex discovers ~/.agents/skills itself; an explicit skills directory needs its legacy home link.
        skills = self.home / "skills"
        if skills.is_symlink():
            skills.unlink()
        if skills_dir.resolve() != (Path.home() / ".agents/skills").resolve():
            if skills.exists():
                raise Fatal(f"Refusing to overwrite {skills}: expected a skills symlink")
            skills.symlink_to(skills_dir, target_is_directory=True)

    def argv(self, model, effort, turns, prompt, session=None, cwd=None, browser=False):
        argv = [self.binary, "exec", *(["resume"] if session else []), "--json", "--strict-config",
                "--dangerously-bypass-approvals-and-sandbox", "-m", model]
        if effort and effort != "none":
            argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
        if browser:
            for name, server in BROWSER_MCP["mcpServers"].items():
                for key, value in server.items():
                    argv.extend(["-c", f"mcp_servers.{name}.{key}={json.dumps(value)}"])
        return [*argv, *([session] if session else []), prompt]

    def env(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "CODEX_"))}
        return {**env, "CODEX_HOME": str(self.home)}

    def stream(self, turns=500):
        return CodexStream(turns)


class CodexStream:
    """exec JSONL; usage is cumulative across a thread, including after resume."""

    TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "collab_tool_call", "web_search"}
    USAGE_CODES = {"usage_limit_reached", "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached",
                   "workspace_owner_credits_depleted", "workspace_member_credits_depleted"}
    AUTH_CODES = {"refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated", "unauthorized",
                  "authentication_error", "not_authenticated"}
    USAGE_WORDING = re.compile(r"\bYou[’']ve hit your usage limit\b|\bYour workspace is out of credits\b|"
                               r"\bYou hit your spend cap set\b", re.I)
    AUTH_WORDING = re.compile(r"\bYour access token could not be refreshed\b|"
                              r"\bplan type is required for chatgpt authentication\b|"
                              r"\b(?:Missing bearer or basic authentication|Not logged in|ChatGPT login is required)\b|"
                              r"\bPlease (?:log in|sign in) (?:to|again)\b|\b401 Unauthorized\b", re.I)

    def __init__(self, turns=500):
        self.cap, self.completed, self.stop_at_cap = turns, set(), False
        self.text, self.usage, self.errors = "", {}, []
        self.finished, self.failed, self.turns = False, False, 0
        self.started = time.monotonic()

    def feed(self, frame):
        kind, events = frame.get("type"), []
        if kind == "thread.started":
            return frame.get("thread_id"), []
        if kind == "turn.started":
            self.turns += 1
            events.append({"type": "turn_start", "turnNumber": self.turns})
        elif kind == "turn.completed":
            self.finished = True
            raw = frame.get("usage") or {}
            self.usage = {name: number(raw.get(key)) for key, name in (
                ("input_tokens", "inputTokens"), ("cached_input_tokens", "cacheReadInputTokens"),
                ("output_tokens", "outputTokens"))}
        elif kind in {"error", "turn.failed"}:
            error = frame.get("error", {k: v for k, v in frame.items() if k != "type"})
            error = error if isinstance(error, dict) else {"message": str(error)}
            self.errors.append(error)
            self.failed = self.failed or kind == "turn.failed"
            events.append({"type": "run_error", "error": error.get("message", "")})
        elif kind in {"item.started", "item.completed"}:
            item = frame.get("item") or {}
            tool = item.get("type")
            if kind == "item.completed" and tool == "agent_message":
                self.text = str(item.get("text") or "")
                events.append({"type": "message_end", "content": [{"type": "text", "text": self.text}]})
            elif tool in self.TOOL_ITEMS:
                name = f"mcp__{item.get('server')}__{item.get('tool')}" if tool == "mcp_tool_call" else tool
                if kind == "item.started" or tool == "file_change":
                    events.append({"type": "tool_queued", "toolName": name,
                                   "input": {"command": item.get("command")} if tool == "command_execution" else item})
                if kind == "item.completed":
                    self.completed.add(item.get("id"))
                    self.stop_at_cap = len(self.completed) >= self.cap
                    if item.get("status") in {"failed", "declined"} or item.get("exit_code") not in (None, 0):
                        events.append({"type": "tool_errored", "toolName": name,
                                       "error": item.get("error") or item.get("aggregated_output", "")})
        return None, events

    def outcome(self, rc):
        spent = {"usage": self.usage, "numTurns": len(self.completed),
                 "durationMs": int((time.monotonic() - self.started) * 1000)}
        if self.stop_at_cap:
            return "turn-cap", self.text, spent, None
        if self.finished and not self.failed and rc == 0:
            return ("reported" if self.text.strip() else "no-output"), self.text, spent, None
        fatal = self.account_failure()
        return ("provider-error" if self.failed or self.errors else "no-output"), self.text, spent, fatal

    def account_failure(self):
        # Only CLI errors are eligible; agent messages and tool output often discuss limits in the project.
        for error in reversed(self.errors):
            code = error.get("code") or error.get("type")
            status = error.get("http_status_code") or error.get("status")
            message = str(error.get("message") or "")
            usage, auth = code in self.USAGE_CODES, code in self.AUTH_CODES or status == 401
            if not code and not status:
                usage, auth = bool(self.USAGE_WORDING.search(message)), bool(self.AUTH_WORDING.search(message))
            if usage:
                reset = error.get("resets_at")
                detail = f" Reset time: {reset}." if reset is not None else f" {message}" if message else ""
                return f"ChatGPT usage limit was reached.{detail} Wait for access to reset, then --resume"
            if auth:
                return "ChatGPT login expired or signed out: run `codex login`, then --resume"
        return None


class CommandCode:
    name = "commandcode"

    def __init__(self):
        self.binary = os.getenv("COMMANDCODE_BIN", "command-code")

    def argv(self, model, effort, turns, prompt, session=None, cwd=None, browser=False):
        argv = [self.binary, "-p", prompt, "-m", model, "--max-turns", str(turns),
                "--output-format", "json", "--yolo", "--trust", "--skip-onboarding", "--no-auto-update"]
        if effort and effort != "none":
            argv.extend(["--effort", effort])
        if session:
            argv.extend(["--resume", session])
        if browser:  # a headless run withholds MCP tools unless named
            argv.extend(["--tools-enable", ",".join(BROWSER_TOOLS)])
        return argv

    def env(self):
        return None

    def stream(self, turns=500):
        return CommandCodeStream()


class CommandCodeStream:
    """NDJSON with `event` frames and a final `result` frame carrying finalText."""

    def __init__(self):
        self.result = None

    def feed(self, frame):
        event = frame.get("event") if isinstance(frame.get("event"), dict) else {}
        # Session IDs may arrive before the final result, allowing interrupt recovery.
        sid = frame.get("sessionId") or event.get("sessionId")
        if frame.get("type") == "result":
            self.result = frame
            return sid, []
        return sid, [event] if event else []

    def outcome(self, rc):
        """(status, final text, spend or None, run-wide failure or None)."""
        result = self.result or {}
        text = str(result.get("finalText") or "")
        if rc == 8 or result.get("subtype") == "max_turns":
            status = "turn-cap"
        elif rc or not self.result or result.get("subtype") != "success" or not text.strip():
            status = "provider-error" if rc in {3, 5, 6, 7, 10} else "no-output"
        else:
            status = "reported"
        # CommandCode's result has carried no cost so far: record one only if it ever does, never a fake $0.
        cost = next((result[k] for k in ("totalCostUsd", "costUsd", "total_cost_usd")
                     if isinstance(result.get(k), (int, float))), None)
        return status, text, ({"costUsd": float(cost)} if cost is not None else None), None


class ClaudeCode:
    """Claude Code on the operator's normal login and subscription (docs/adr/0004-*). That login lives in the
    Keychain, tied to their config directory, so sessions run there with every other credential removed and
    the runner's settings passed inline."""
    name = "claude"

    def __init__(self):
        self.binary = os.getenv("CLAUDE_BIN", "claude")

    def argv(self, model, effort, turns, prompt, session=None, cwd=None, browser=False):
        # Settings sources do not control CLAUDE.md, skills or normal-mode tools. Project env/settings must not
        # replace the account or endpoint after the child environment is constructed.
        argv = [self.binary, "-p", prompt, "--model", model, "--output-format", "stream-json",
                "--verbose", "--max-turns", str(turns), "--dangerously-skip-permissions",
                "--setting-sources", "user", "--settings", json.dumps(CLAUDE_SETTINGS), "--strict-mcp-config"]
        if effort and effort != "none":
            argv.extend(["--effort", effort])
        if session:
            argv.extend(["--resume", session])
        if browser:  # only servers named here load; both flags take lists, so they go last
            argv.extend(["--mcp-config", json.dumps(BROWSER_MCP), "--allowedTools", ",".join(BROWSER_TOOLS)])
        return argv

    def env(self):
        """The runner's environment with every other credential and provider switch removed, so a session
        can only reach the operator's own login."""
        env = {k: v for k, v in os.environ.items() if k not in FOREIGN_AUTH
               and not k.startswith(("CLAUDE_CODE_USE_", "ANTHROPIC_"))}
        env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
        return env

    def stream(self, turns=500):
        return ClaudeStream()

    def login_problem(self):
        """Why a session here could not run on a subscription, or None. Runs as a session would, and costs nothing."""
        try:
            found = subprocess.run([self.binary, "auth", "status"], capture_output=True, text=True, timeout=60,
                                   env=self.env())
            status = json.loads(found.stdout)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return "could not read the Claude Code login from `claude auth status`"
        if not isinstance(status, dict) or not status.get("loggedIn"):
            return "Claude Code is not logged in: run `claude auth login`, then --resume"
        if status.get("authMethod") != "claude.ai":
            return (f"Claude Code is logged in with {status.get('authMethod')!r}, not a claude.ai subscription: "
                    "run `claude auth login`, then --resume")
        return None


ACCOUNT = {"usage": "Claude subscription usage limit was reached.{detail} Wait for it to reset, then --resume",
           "billing": "Claude subscription: a billing problem on the account — fix it, then --resume",
           "auth": "Claude login expired or signed out: run `claude auth login`, then --resume",
           "access": "Claude account: not permitted or on hold — fix its access, then --resume"}
# Claude Code's assistant `error` categories and the result's api_error_status, per backend failure kind.
ACCOUNT_FLAGS = {"billing": ({"billing_error"}, {402}), "auth": ({"authentication_failed"}, {401}),
                 "access": ({"account_on_hold", "oauth_org_not_allowed"}, {403})}
TRANSIENT_FLAGS = {"rate_limit", "overloaded", "server_error"}
# Only when no structured field says: the API's and the CLI's own error wording, never a bare word. This runner
# works on codebases whose docs say "rate limit" and "credit" in their own sense.
USAGE_WORDING = re.compile(r"^\W*You[’']ve (?:hit|reached) your\b", re.I | re.M)
ACCOUNT_WORDING = {
    "billing": re.compile(r"\bbilling_error\b|\bcredit balance is too low\b", re.I),
    "auth": re.compile(r"\bauthentication_error\b|\binvalid x-api-key\b|"
                       r"^\W*(?:API Error: 401\b|Invalid API key\b|Not logged in\b|Authentication required\b)",
                       re.I | re.M),
    "access": re.compile(r"\bpermission_error\b|\breached your specified API usage limits\b|"
                         r"\bThis organization has been disabled\b|\bYour account is on hold\b|^\W*API Error: 403\b",
                         re.I | re.M)}
TRANSIENT_WORDING = re.compile(r"\b(?:rate_limit_error|overloaded_error)\b|"
                               r"^\W*API Error: (?:(?:429|5\d\d|Connection error|Request timed out)\b|Request rejected \(429\))",
                               re.I | re.M)


def blocks(message):
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def flatten(content):
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return "" if content is None else str(content)


class ClaudeStream:
    """stream-json: system/init, assistant and user messages, system/api_retry, and a final result."""

    def __init__(self):
        self.result, self.flags, self.alerts, self.tools, self.turns = None, set(), [], {}, set()
        self.refusal, self.limit = None, None

    def feed(self, frame):
        kind, events = frame.get("type"), []
        message = frame.get("message") if isinstance(frame.get("message"), dict) else {}
        if kind == "system" and frame.get("subtype") == "init" and "apiKeySource" in frame \
                and frame["apiKeySource"] != "none":
            # init arrives before the first API call. A subscription login reads "none"; any other source
            # would bill someone else, so the run stops before it spends anything.
            self.refusal = (f"a Claude Code session authenticated with {frame['apiKeySource']!r}, not the subscription "
                            "login; stopped before its first API call")
        if kind == "system" and frame.get("subtype") == "init" and frame.get("apiProvider") not in (None, "anthropic", "firstParty"):
            self.refusal = "a Claude Code session selected a different API provider; stopped before its first API call"
        if kind == "result":
            self.result = frame
        elif kind == "rate_limit_event":
            info = frame.get("rate_limit_info")
            if isinstance(info, dict) and info.get("status") == "rejected":
                self.limit = info  # read only if the session then fails: one window can fall back to another
        elif kind == "assistant":
            # One API turn arrives as several messages sharing an id; a new main-thread id is a new turn.
            mid = message.get("id")
            if frame.get("parent_tool_use_id") is None and mid and mid not in self.turns:
                self.turns.add(mid)
                events.append({"type": "turn_start", "turnNumber": len(self.turns)})
            text = [b for b in blocks(message) if b.get("type") == "text"]
            if frame.get("error"):  # the CLI's report of a failed API call, not the model's words
                self.flags.add(str(frame["error"]))
                self.alerts.append(flatten(text))
                events.append({"type": "run_error", "error": flatten(text) or str(frame["error"])})
            elif text:
                events.append({"type": "message_end", "content": text})
            for b in blocks(message):
                if b.get("type") == "tool_use":
                    self.tools[b.get("id")] = str(b.get("name", "?"))
                    events.append({"type": "tool_queued", "toolName": b.get("name", "?"), "input": b.get("input")})
        elif kind == "user":
            for b in blocks(message):
                if b.get("type") == "tool_result" and b.get("is_error"):
                    events.append({"type": "tool_errored", "toolName": self.tools.get(b.get("tool_use_id"), "?"),
                                   "error": flatten(b.get("content"))})
        elif kind == "system" and frame.get("subtype") == "api_retry":
            status = frame.get("error_status")
            events.append({"type": "api_retry", "error": f"{frame.get('error') or 'error'}"
                           f"{f' ({status})' if status else ''}, attempt {frame.get('attempt')}/{frame.get('max_retries')}"})
        sid = frame.get("session_id")
        return (sid if isinstance(sid, str) else None), events

    def failure(self, result):
        """'usage', 'billing', 'auth' or 'access' for an account-level failure, 'transient' for one worth retrying."""
        status = result.get("api_error_status") if isinstance(result.get("api_error_status"), int) else None
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        said = "\n".join([str(result.get("result") or ""), *map(str, errors), *self.alerts])
        # A spent plan arrives as a rate limit too, so it is told apart from one before transient failures are.
        if self.limit or USAGE_WORDING.search(said):
            return "usage"
        for kind, (flags, statuses) in ACCOUNT_FLAGS.items():
            if self.flags & flags or status in statuses:
                return kind
        if self.flags & TRANSIENT_FLAGS or status in (408, 429) or (status or 0) >= 500:
            return "transient"
        # The API has answered a spent balance with a plain 400, so its wording is read even then.
        kind = next((k for k, pattern in ACCOUNT_WORDING.items() if pattern.search(said)), None)
        structured = self.flags or status  # then neither account nor transient: invalid_request and the like
        return kind or ("transient" if not structured and TRANSIENT_WORDING.search(said) else None)

    def outcome(self, rc):
        result = self.result if isinstance(self.result, dict) else {}
        text, subtype = str(result.get("result") or ""), result.get("subtype")
        fatal = None
        if subtype == "error_max_turns":
            status = "turn-cap"
        elif subtype == "success" and not result.get("is_error") and rc == 0:
            status = "reported" if text.strip() else "no-output"
        else:  # an error result, or none: only a failed session's own error text is ever read
            kind = self.failure(result)
            status = "provider-error" if kind else "no-output"
            fatal = ACCOUNT.get(kind)
            if kind == "usage":
                fatal = fatal.format(detail=self.reset())
        return status, text, self.spend(result), fatal

    def reset(self):
        """The usage window's reset time, as far as the session said."""
        resets = (self.limit or {}).get("resetsAt")
        if not isinstance(resets, (int, float)) or isinstance(resets, bool):
            return ""
        try:
            return f" Reset time: {datetime.datetime.fromtimestamp(resets).isoformat(sep=' ', timespec='minutes')}."
        except (OverflowError, OSError, ValueError):
            return ""

    @staticmethod
    def spend(result):
        """Tokens, turns and time from the result; a subscription has no per-session bill, so never its dollar
        estimate. modelUsage covers sub-agents too; usage covers only the main loop, so it is the fallback."""
        models = [m for m in (result.get("modelUsage") or {}).values() if isinstance(m, dict)] \
            if isinstance(result.get("modelUsage"), dict) else []
        raw = result.get("usage") if isinstance(result.get("usage"), dict) else None
        if not models and raw is None:
            return None
        if models:
            usage = {key: sum(number(m.get(key)) for m in models) for key in (
                "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")}
        else:
            usage = {key: number(raw.get(field)) for key, field in (
                ("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                ("cacheReadInputTokens", "cache_read_input_tokens"),
                ("cacheCreationInputTokens", "cache_creation_input_tokens"))}
        return {"usage": usage, "numTurns": number(result.get("num_turns")), "durationMs": number(result.get("duration_ms"))}
