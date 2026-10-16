"""Each backend's agent CLI: its argv and environment, its event stream, and how a session ended.

run_agent owns the process, interrupts and records; an adapter only translates. Narration events are
CommandCode-shaped on both backends, so the colour feed looks the same whichever CLI runs.
"""

from pathlib import Path
import json
import os
import re
import subprocess

from .settings import BROWSER_MCP, BROWSER_TOOLS, FOREIGN_AUTH, KEYCHAIN_SERVICE


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


class CommandCode:
    name = "commandcode"

    def __init__(self):
        self.binary = os.getenv("COMMANDCODE_BIN", "command-code")

    def argv(self, model, effort, turns, prompt, session=None, cwd=None, budget=None, browser=False):
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

    def stream(self):
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
    """Claude Code in bare mode, authenticated only by the company key (docs/adr/0002-*)."""
    name = "claude-api"

    def __init__(self, key):
        self.binary = os.getenv("CLAUDE_BIN", "claude")
        self.key = key

    def argv(self, model, effort, turns, prompt, session=None, cwd=None, budget=None, browser=False):
        argv = [self.binary, "--bare", "-p", prompt, "--model", model, "--output-format", "stream-json",
                "--verbose", "--max-turns", str(turns), "--dangerously-skip-permissions"]
        if effort and effort != "none":
            argv.extend(["--effort", effort])
        if session:
            argv.extend(["--resume", session])
        # Bare mode skips CLAUDE.md discovery, so the session's own copy is appended. Not redundant.
        notes = Path(cwd or ".") / "CLAUDE.md"
        if notes.is_file():
            argv.extend(["--append-system-prompt-file", str(notes)])
        if budget:
            argv.extend(["--max-budget-usd", f"{budget:g}"])
        if browser:  # bare mode reads no user MCP config; both flags take lists, so they go last
            argv.extend(["--mcp-config", json.dumps(BROWSER_MCP), "--allowedTools", ",".join(BROWSER_TOOLS)])
        return argv

    def env(self):
        """The runner's environment with every other credential removed and the company key added."""
        env = {k: v for k, v in os.environ.items() if k not in FOREIGN_AUTH}
        env["ANTHROPIC_API_KEY"] = self.key
        return env

    def stream(self):
        return ClaudeStream()


def company_key():
    """The company Anthropic API key and where it came from, or (None, None). Never printed or stored."""
    key = os.environ.get("TICKET_RUNNER_ANTHROPIC_API_KEY", "").strip()
    if key:
        return key, "env"
    service = os.environ.get("TICKET_RUNNER_KEYCHAIN_SERVICE") or KEYCHAIN_SERVICE
    try:  # the keychain may ask the operator to allow access, so wait for them
        found = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                               capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    key = found.stdout.strip() if found.returncode == 0 else ""
    return (key, f"keychain:{service}") if key else (None, None)


ACCOUNT = {"billing": "company API key: credit balance too low — top up, then --resume",
           "auth": "company API key: rejected as invalid or revoked — replace it in the keychain (or "
                   "TICKET_RUNNER_ANTHROPIC_API_KEY), then --resume",
           "access": "company API key: not permitted, or over its organization's spend limit — fix its access "
                     "or raise the limit, then --resume"}
# Claude Code's assistant `error` categories and the result's api_error_status, per backend failure kind.
ACCOUNT_FLAGS = {"billing": ({"billing_error"}, {402}), "auth": ({"authentication_failed"}, {401}),
                 "access": ({"account_on_hold", "oauth_org_not_allowed"}, {403})}
TRANSIENT_FLAGS = {"rate_limit", "overloaded", "server_error"}
# Only when no structured field says: the API's and the CLI's own error wording, never a bare word. This runner
# works on codebases whose docs say "rate limit" and "credit" in their own sense.
ACCOUNT_WORDING = {
    "billing": re.compile(r"\bbilling_error\b|\bcredit balance is too low\b", re.I),
    "auth": re.compile(r"\bauthentication_error\b|\binvalid x-api-key\b|^\W*(?:API Error: 401\b|Invalid API key\b)",
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

    def feed(self, frame):
        kind, events = frame.get("type"), []
        message = frame.get("message") if isinstance(frame.get("message"), dict) else {}
        if kind == "result":
            self.result = frame
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
        """'billing', 'auth' or 'access' for an account-level failure, 'transient' for one worth retrying."""
        status = result.get("api_error_status") if isinstance(result.get("api_error_status"), int) else None
        for kind, (flags, statuses) in ACCOUNT_FLAGS.items():
            if self.flags & flags or status in statuses:
                return kind
        if self.flags & TRANSIENT_FLAGS or status in (408, 429) or (status or 0) >= 500:
            return "transient"
        # The API has answered a spent balance with a plain 400, so its wording is read even then.
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        said = "\n".join([str(result.get("result") or ""), *map(str, errors), *self.alerts])
        kind = next((k for k, pattern in ACCOUNT_WORDING.items() if pattern.search(said)), None)
        structured = self.flags or status  # then neither account nor transient: invalid_request and the like
        return kind or ("transient" if not structured and TRANSIENT_WORDING.search(said) else None)

    def outcome(self, rc):
        result = self.result if isinstance(self.result, dict) else {}
        text, subtype = str(result.get("result") or ""), result.get("subtype")
        fatal = None
        if subtype == "error_max_turns":
            status = "turn-cap"
        elif subtype == "error_max_budget_usd":
            status = "budget-cap"
        elif subtype == "success" and not result.get("is_error") and rc == 0:
            status = "reported" if text.strip() else "no-output"
        else:  # an error result, or none: only a failed session's own error text is ever read
            kind = self.failure(result)
            status = "provider-error" if kind else "no-output"
            fatal = ACCOUNT.get(kind)
        return status, text, self.spend(result), fatal

    @staticmethod
    def spend(result):
        """Cost, tokens, turns and time from the result. modelUsage covers what total_cost_usd covers
        (subagents included); usage covers only the main loop, so it is the fallback."""
        if not isinstance(result.get("total_cost_usd"), (int, float)):
            return None
        models = [m for m in (result.get("modelUsage") or {}).values() if isinstance(m, dict)] \
            if isinstance(result.get("modelUsage"), dict) else []
        if models:
            usage = {key: sum(number(m.get(field)) for m in models) for key, field in (
                ("input", "inputTokens"), ("output", "outputTokens"), ("cacheRead", "cacheReadInputTokens"),
                ("cacheCreation", "cacheCreationInputTokens"))}
        else:
            raw = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            usage = {key: number(raw.get(field)) for key, field in (
                ("input", "input_tokens"), ("output", "output_tokens"), ("cacheRead", "cache_read_input_tokens"),
                ("cacheCreation", "cache_creation_input_tokens"))}
        return {"costUsd": float(result["total_cost_usd"]), "usage": usage,
                "numTurns": number(result.get("num_turns")), "durationMs": number(result.get("duration_ms"))}
