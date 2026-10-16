"""The claude-api backend's reading of Claude Code's stream-json: narration, outcome, spend and argv."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ticket_runner_lib import display
from ticket_runner_lib.backends import ClaudeCode, ClaudeStream, company_key
from ticket_runner_lib.settings import BROWSER_TOOLS


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
    def test_success_with_text_is_reported_and_its_spend_kept(self):
        status, text, spent, fatal = ended(result(
            is_error=False, result="STATUS=COMPLETE", total_cost_usd=1.84, num_turns=62, duration_ms=840000,
            usage={"input_tokens": 1}, modelUsage={
                "claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 30,
                                      "cacheCreationInputTokens": 40, "costUSD": 1.5},
                "claude-haiku-5-5": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3,
                                     "cacheCreationInputTokens": 4, "costUSD": 0.34}}), rc=0)
        self.assertEqual((status, text, fatal), ("reported", "STATUS=COMPLETE", None))
        self.assertEqual(spent, {"costUsd": 1.84, "numTurns": 62, "durationMs": 840000,
                                 "usage": {"input": 11, "output": 22, "cacheRead": 33, "cacheCreation": 44}})

    def test_caps_map_to_their_own_statuses(self):
        self.assertEqual(ended(result(subtype="error_max_turns", errors=[]))[0], "turn-cap")
        self.assertEqual(ended(result(subtype="error_max_budget_usd", errors=[]))[0], "budget-cap")

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
                  "credit balance too low — top up, then --resume"),
                 ([result(result="Your credit balance is too low to access the Anthropic API.", api_error_status=400)],
                  "credit balance too low"),
                 ([failed("Invalid API key · Fix external API key", "authentication_failed"), result(result="x")],
                  "rejected as invalid or revoked"),
                 ([result(result="Failed", api_error_status=401)], "rejected as invalid or revoked"),
                 ([result(result='{"type":"error","error":{"type":"authentication_error"}}')], "invalid or revoked"),
                 ([result(result="Forbidden", api_error_status=403)], "spend limit"),
                 ([result(result="You have reached your specified API usage limits.")], "spend limit")]
        for frames, message in cases:
            with self.subTest(frames=frames):
                status, _, _, fatal = ended(*frames)
                self.assertEqual(status, "provider-error")
                self.assertIn("company API key: ", fatal)
                self.assertIn(message, fatal)

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
                                  "apiKeySource": "ANTHROPIC_API_KEY"})
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
    def test_init_on_anything_but_the_company_key_is_refused(self):
        for source in ("none", "/login managed key", "ANTHROPIC_AUTH_TOKEN"):
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
        self.assertIn("invalid or revoked", fatal)
        self.assertIn("invalid or revoked", ended(result(result="Not logged in · Please run /login"), rc=0)[3])


class ArgvTests(unittest.TestCase):
    def test_argv_never_bare_and_never_appends_claude_md(self):
        backend = ClaudeCode("sk-test")
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "CLAUDE.md").write_text("notes")
            argv = backend.argv("claude-opus-5-5", "none", 9, "do it", None, folder, None, False)
            self.assertEqual(argv[1:4], ["-p", "do it", "--model"])
            self.assertNotIn("--bare", argv)
            self.assertNotIn("--append-system-prompt-file", argv)
            self.assertNotIn("--effort", argv)
            argv = backend.argv("claude-opus-5-5", "high", 9, "do it", "sess", folder, 2.5, True)
            self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.5")
            self.assertEqual(argv[argv.index("--resume") + 1], "sess")
            self.assertEqual(argv[argv.index("--effort") + 1], "high")
            self.assertEqual(argv[-1], ",".join(BROWSER_TOOLS))  # list-taking flags go last

    def test_child_env_carries_only_the_company_key_and_the_runners_config_dir(self):
        leaked = {"ANTHROPIC_API_KEY": "personal", "ANTHROPIC_AUTH_TOKEN": "oauth", "ANTHROPIC_BASE_URL": "http://x",
                  "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat", "CLAUDE_CONFIG_DIR": "/personal",
                  "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1"}
        with patch.dict(os.environ, leaked):
            env = ClaudeCode("sk-company", Path("/runner/config")).env()
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "personal")  # the runner's own env is untouched
        self.assertEqual((env.pop("ANTHROPIC_API_KEY"), env.pop("CLAUDE_CONFIG_DIR")), ("sk-company", "/runner/config"))
        self.assertFalse(set(leaked) & set(env))

    def test_prepare_seeds_skills_and_settings_and_follows_a_moved_skills_dir(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a").mkdir()
            (root / "b").mkdir()
            backend = ClaudeCode("sk", root / "config")
            backend.prepare(root / "a")
            self.assertEqual((root / "config/skills").resolve(), (root / "a").resolve())
            self.assertFalse(json.loads((root / "config/settings.json").read_text())["autoMemoryEnabled"])
            backend.prepare(root / "b")
            self.assertEqual((root / "config/skills").resolve(), (root / "b").resolve())

    def test_key_from_env_wins_and_an_empty_one_does_not_count(self):
        with patch.dict(os.environ, {"TICKET_RUNNER_ANTHROPIC_API_KEY": "sk-env"}):
            self.assertEqual(company_key(), ("sk-env", "env"))
        with patch.dict(os.environ, {"TICKET_RUNNER_ANTHROPIC_API_KEY": " ", "TICKET_RUNNER_KEYCHAIN_SERVICE": "svc",
                                     "PATH": "/nonexistent"}):
            self.assertEqual(company_key(), (None, None))  # no `security` on PATH: no key, never a guess


if __name__ == "__main__":
    unittest.main()
