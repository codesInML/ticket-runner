"""The claude backend's reading of Claude Code's stream-json: narration, outcome, spend and argv."""

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from ticket_runner_lib import display
from ticket_runner_lib.backends import ClaudeCode, ClaudeStream
from ticket_runner_lib.settings import BROWSER_TOOLS, CLAUDE_SETTINGS


def ended(*frames, rc=1):
    stream = ClaudeStream()
    for frame in frames:
        stream.feed(frame)
    return stream.outcome(rc)


def result(**fields):
    return {"type": "result", "subtype": "success", "is_error": True, "session_id": "s", **fields}


def failed(text, error=None):
    """The assistant message Claude Code writes for a failed API call, with its `error` category."""
    frame = {"type": "assistant", "session_id": "s", "parent_tool_use_id": None,
             "message": {"id": "m0", "content": [{"type": "text", "text": text}]}}
    return {**frame, "error": error} if error else frame


class OutcomeTests(unittest.TestCase):
    def test_success_with_text_is_reported_with_tokens_and_no_dollar_figure(self):
        status, text, spent, fatal = ended(result(
            is_error=False, result="STATUS=COMPLETE", total_cost_usd=1.84, num_turns=62, duration_ms=840000,
            usage={"input_tokens": 1}, modelUsage={
                "claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 30,
                                      "cacheCreationInputTokens": 40, "costUSD": 1.5},
                "claude-haiku-5-5": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3,
                                     "cacheCreationInputTokens": 4, "costUSD": 0.34}}), rc=0)
        self.assertEqual((status, text, fatal), ("reported", "STATUS=COMPLETE", None))
        self.assertEqual(spent, {"numTurns": 62, "durationMs": 840000,
                                 "usage": {"inputTokens": 11, "outputTokens": 22, "cacheReadInputTokens": 33,
                                           "cacheCreationInputTokens": 44}})
        status, _, spent, _ = ended(result(is_error=False, result="STATUS=COMPLETE", total_cost_usd=1.84,
                                           usage={"input_tokens": 5, "output_tokens": 6}), rc=0)
        self.assertEqual((spent["usage"]["inputTokens"], spent["usage"]["outputTokens"]), (5, 6))
        self.assertNotIn("costUsd", spent)

    def test_the_turn_cap_maps_to_its_own_status(self):
        self.assertEqual(ended(result(subtype="error_max_turns", errors=[]))[0], "turn-cap")

    def test_no_result_or_empty_text_is_no_output(self):
        self.assertEqual(ended()[0:2], ("no-output", ""))
        self.assertEqual(ended(result(is_error=False, result="  "), rc=0)[0], "no-output")
        self.assertEqual(ended(result(subtype="error_during_execution", errors=["boom"]))[0], "no-output")

    def test_transient_api_failures_are_provider_errors(self):
        for frames in ([failed("API Error: 429", "rate_limit"), result(result="API Error: 429")],
                       [result(result="Overloaded", api_error_status=529)],
                       [result(result="API Error: Request rejected (429) · this may be a temporary capacity issue.")],
                       [result(result='API Error: 500 {"type":"error","error":{"type":"api_error"}}')]):
            with self.subTest(frames=frames):
                status, _, _, fatal = ended(*frames)
                self.assertEqual((status, fatal), ("provider-error", None))

    def test_account_failures_are_run_wide_fatal(self):
        cases = [([failed("Credit balance is too low", "billing_error"), result(result="Credit balance is too low")],
                  "a billing problem"),
                 ([result(result="Your credit balance is too low to access the Anthropic API.", api_error_status=400)],
                  "a billing problem"),
                 ([failed("Invalid API key · Fix external API key", "authentication_failed"), result(result="x")],
                  "run `claude auth login`"),
                 ([result(result="Failed", api_error_status=401)], "run `claude auth login`"),
                 ([result(result='{"type":"error","error":{"type":"authentication_error"}}')], "run `claude auth login`"),
                 ([result(result="Authentication required · Sign in again")], "run `claude auth login`"),
                 ([result(result="Forbidden", api_error_status=403)], "not permitted"),
                 ([result(result="You have reached your specified API usage limits.")], "not permitted")]
        for frames, message in cases:
            with self.subTest(frames=frames):
                status, _, _, fatal = ended(*frames)
                self.assertEqual(status, "provider-error")
                self.assertIn(message, fatal)

    def test_a_spent_subscription_stops_the_run_though_it_arrives_as_a_rate_limit(self):
        limit = {"type": "rate_limit_event", "session_id": "s",
                 "rate_limit_info": {"status": "rejected", "resetsAt": 1893456000, "rateLimitType": "five_hour"}}
        status, _, _, fatal = ended(limit, failed("You've hit your session limit · resets 3pm", "rate_limit"),
                                    result(result="You've hit your session limit · resets 3pm", api_error_status=429))
        self.assertEqual(status, "provider-error")
        self.assertIn("usage limit was reached. Reset time: 20", fatal)
        self.assertIn("then --resume", fatal)
        # The wording alone is enough, with no reset time to name; a curly apostrophe reads the same.
        for words in ("You've hit your weekly limit", "You’ve reached your Fable limit."):
            status, _, _, fatal = ended(failed(words, "rate_limit"), result(result=words, api_error_status=429))
            self.assertEqual(status, "provider-error")
            self.assertIn("usage limit was reached. Wait for it to reset", fatal)

    def test_a_rejected_window_means_nothing_when_the_session_succeeds(self):
        limit = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1}}
        status, _, _, fatal = ended(limit, result(is_error=False, result="STATUS=COMPLETE"), rc=0)
        self.assertEqual((status, fatal), ("reported", None))
        warning = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}}
        status, _, _, fatal = ended(warning, failed("API Error: 429", "rate_limit"), result(result="API Error: 429"))
        self.assertEqual((status, fatal), ("provider-error", None))

    def test_a_reset_time_that_cannot_be_read_is_left_out(self):
        for resets in (True, "soon", 1e30):
            limit = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": resets}}
            self.assertIn("reached. Wait", ended(limit, result(result="failed"))[3])

    def test_the_projects_own_words_never_read_as_an_account_or_api_failure(self):
        # A credit-decisioning codebase talks about credit and rate limits in its own sense.
        prose = ("Raised the Advance rate limit to 80%; the borrower's credit balance is too low for a top-up.\n"
                 "rate limit exceeded for the credit bureau mock\nSTATUS=COMPLETE")
        status, _, _, fatal = ended(result(is_error=False, result=prose), rc=0)
        self.assertEqual((status, fatal), ("reported", None))
        for text in ("Tests failed: Advance rate limit validation", "credit limit too low in fixture",
                     "the rate limit middleware returned 429 to the client"):
            with self.subTest(text=text):
                self.assertEqual(ended(result(subtype="error_during_execution", errors=[text]))[0:4:3],
                                 ("no-output", None))
        # The model's own text, even quoting an API error, is not the CLI's error report.
        talk = {"type": "assistant", "session_id": "s", "parent_tool_use_id": None,
                "message": {"id": "m1", "content": [{"type": "text", "text": "Docs say: credit balance is too low"}]}}
        self.assertEqual(ended(talk, result(subtype="error_during_execution", errors=[]))[0:4:3], ("no-output", None))

    def test_structured_fields_outrank_the_wording(self):
        status, _, _, fatal = ended(failed("API Error: 429 rate_limit_error", "invalid_request"),
                                    result(result="API Error: 429", api_error_status=400))
        self.assertEqual((status, fatal), ("no-output", None))


class NarrationTests(unittest.TestCase):
    def test_claude_events_become_the_same_feed(self):
        stream = ClaudeStream()
        sid, shown = stream.feed({"type": "system", "subtype": "init", "session_id": "abc",
                                  "apiKeySource": "none"})
        self.assertEqual((sid, shown, stream.refusal), ("abc", [], None))
        message = {"id": "m1", "content": [{"type": "text", "text": "Looking"},
                                          {"type": "tool_use", "id": "t1", "name": "Bash",
                                           "input": {"command": "ls", "description": "List files"}}]}
        _, shown = stream.feed({"type": "assistant", "session_id": "abc", "parent_tool_use_id": None,
                                "message": message})
        self.assertEqual([e["type"] for e in shown], ["turn_start", "message_end", "tool_queued"])
        _, again = stream.feed({"type": "assistant", "session_id": "abc", "parent_tool_use_id": None,
                                "message": {"id": "m1", "content": []}})
        self.assertEqual(again, [])  # one API turn, several messages: one turn marker
        _, shown = stream.feed({"type": "user", "session_id": "abc", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": [{"type": "text", "text": "no"}]},
            {"type": "tool_result", "tool_use_id": "t1", "content": "fine"}]}})
        self.assertEqual(shown, [{"type": "tool_errored", "toolName": "Bash", "error": "no"}])
        _, shown = stream.feed({"type": "system", "subtype": "api_retry", "attempt": 2, "max_retries": 10,
                                "error_status": 529, "error": "overloaded", "session_id": "abc"})
        self.assertEqual(shown[0]["error"], "overloaded (529), attempt 2/10")
        _, shown = stream.feed({"type": "rate_limit_event", "session_id": "abc"})  # unknown: ignored
        self.assertEqual(shown, [])

    def test_describe_and_colour_claude_tools(self):
        colors = display.TOOL_COLORS
        self.assertEqual(display.describe("Read", {"file_path": os.path.abspath("x.py")}), "x.py")
        self.assertEqual(display.describe("Bash", {"command": "npm test"}), "npm test")
        self.assertEqual(display.describe("Grep", {"pattern": "TODO", "path": "src"}), "TODO  src")
        for tool, kind in (("Read", "read"), ("Glob", "read"), ("WebFetch", "read"), ("Edit", "write"),
                           ("Write", "write"), ("MultiEdit", "write"), ("NotebookEdit", "write"), ("Bash", "shell"),
                           ("mcp__chrome-devtools__click", "mcp")):
            self.assertEqual(display.tool_color(tool), colors[kind], tool)


class AuthTests(unittest.TestCase):
    def test_init_on_anything_but_the_subscription_login_is_refused(self):
        for source in ("ANTHROPIC_API_KEY", "/login managed key", "ANTHROPIC_AUTH_TOKEN", "apiKeyHelper"):
            stream = ClaudeStream()
            stream.feed({"type": "system", "subtype": "init", "session_id": "s", "apiKeySource": source})
            self.assertIn(repr(source), stream.refusal)
        stream = ClaudeStream()
        stream.feed({"type": "system", "subtype": "init", "session_id": "s"})  # no field: nothing to judge
        self.assertIsNone(stream.refusal)

    def test_not_logged_in_is_an_account_failure(self):
        # Claude Code 2.1.300's own report of a session with no credentials at all.
        status, _, _, fatal = ended(failed("Not logged in · Please run /login", "authentication_failed"),
                                    result(result="Not logged in · Please run /login", api_error_status=None), rc=0)
        self.assertEqual(status, "provider-error")
        self.assertIn("run `claude auth login`", fatal)
        self.assertIn("run `claude auth login`", ended(result(result="Not logged in · Please run /login"), rc=0)[3])

    def fake_status(self, folder, body, rc=0):
        path = Path(folder) / "claude"
        path.write_text(f"#!/bin/sh\necho '{body}'\nexit {rc}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_login_problem_accepts_only_a_claude_ai_subscription(self):
        with tempfile.TemporaryDirectory() as folder:
            for body, rc, expected in (('{"loggedIn": true, "authMethod": "claude.ai"}', 0, None),
                                       ('{"loggedIn": false, "authMethod": "none"}', 1, "not logged in"),
                                       ('{"loggedIn": true, "authMethod": "console"}', 0, "not a claude.ai subscription"),
                                       ('not json', 0, "could not read"), ('[]', 0, "not logged in")):
                with self.subTest(body=body), patch.dict(os.environ, {"CLAUDE_BIN": self.fake_status(folder, body, rc)}):
                    problem = ClaudeCode().login_problem()
                    self.assertIsNone(problem) if expected is None else self.assertIn(expected, problem)
        with patch.dict(os.environ, {"CLAUDE_BIN": "/no/such/claude"}):
            self.assertIn("could not read", ClaudeCode().login_problem())

    def test_the_login_check_sees_what_a_session_would(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "claude"
            script.write_text('#!/bin/sh\nif [ -n "$ANTHROPIC_API_KEY$CLAUDE_CONFIG_DIR" ]; then echo "{}"; else '
                              'echo \'{"loggedIn": true, "authMethod": "claude.ai"}\'; fi\n')
            script.chmod(0o755)
            with patch.dict(os.environ, {"CLAUDE_BIN": str(script), "ANTHROPIC_API_KEY": "x", "CLAUDE_CONFIG_DIR": "/x"}):
                self.assertIsNone(ClaudeCode().login_problem())


class ArgvTests(unittest.TestCase):
    def test_argv_never_bare_and_never_appends_claude_md(self):
        backend = ClaudeCode()
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "CLAUDE.md").write_text("notes")
            argv = backend.argv("claude-opus-5-5", "none", 9, "do it", None, folder, False)
            self.assertEqual(argv[1:4], ["-p", "do it", "--model"])
            self.assertNotIn("--bare", argv)
            self.assertNotIn("--append-system-prompt-file", argv)
            self.assertNotIn("--effort", argv)
            self.assertNotIn("--max-budget-usd", argv)
            self.assertIn("--strict-mcp-config", argv)
            self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), CLAUDE_SETTINGS)
            self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")
            self.assertNotIn("--mcp-config", argv)
            argv = backend.argv("claude-opus-5-5", "high", 9, "do it", "sess", folder, True)
            self.assertEqual(argv[argv.index("--resume") + 1], "sess")
            self.assertEqual(argv[argv.index("--effort") + 1], "high")
            self.assertLess(argv.index("--strict-mcp-config"), argv.index("--mcp-config"))
            self.assertEqual(argv[-1], ",".join(BROWSER_TOOLS))  # list-taking flags go last

    def test_settings_turn_off_attribution_and_auto_memory(self):
        self.assertEqual(CLAUDE_SETTINGS, {"attribution": {"commit": "", "pr": ""}, "autoMemoryEnabled": False})

    def test_child_env_leaves_only_the_operators_login_reachable(self):
        leaked = {"ANTHROPIC_API_KEY": "personal", "ANTHROPIC_AUTH_TOKEN": "oauth", "ANTHROPIC_BASE_URL": "http://x",
                  "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat", "CLAUDE_CONFIG_DIR": "/personal",
                  "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
                  "TICKET_RUNNER_ANTHROPIC_API_KEY": "stale"}
        with patch.dict(os.environ, leaked):
            env = ClaudeCode().env()
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "personal")  # the runner's own env is untouched
        self.assertFalse(set(leaked) & set(env))
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")

    def test_nothing_is_seeded(self):
        self.assertFalse(hasattr(ClaudeCode(), "prepare"))


if __name__ == "__main__":
    unittest.main()
