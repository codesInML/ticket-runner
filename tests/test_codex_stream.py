"""Subscription backend contracts, tested without a real login or CLI."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ticket_runner_lib.cli import parse_args
from ticket_runner_lib.backends import Codex, CodexStream
from ticket_runner_lib.settings import Fatal
import test_claude_regressions as ledger


class CodexStreamTests(unittest.TestCase):
    def test_top_level_error_without_a_failed_turn_is_classified(self):
        stream = CodexStream()
        stream.feed({'type': 'error', 'message': 'You’ve hit your usage limit. Try again later.'})
        self.assertIn('ChatGPT usage limit', stream.outcome(1)[3])

    def test_account_failures_and_transient_errors(self):
        cases = [({'code': 'usage_limit_reached', 'resets_at': 12345}, 'usage limit', '12345'),
                 ({'message': 'You’ve hit your usage limit. Try again at 14:00.'}, 'usage limit', '14:00'),
                 ({'code': 'refresh_token_expired'}, 'codex login', None),
                 ({'code': 'refresh_token_reused'}, 'codex login', None),
                 ({'message': 'Your access token could not be refreshed because your refresh token was revoked.'},
                  'codex login', None),
                 ({'message': 'plan type is required for chatgpt authentication'}, 'codex login', None)]
        for error, hint, reset in cases:
            with self.subTest(error=error):
                stream = CodexStream()
                stream.feed({'type': 'turn.failed', 'error': error})
                status, _, _, fatal = stream.outcome(1)
                self.assertEqual(status, 'provider-error')
                self.assertIn(hint, fatal)
                self.assertTrue(fatal.endswith('then --resume'))
                if reset:
                    self.assertIn(reset, fatal)
        for error in ({'code': 'rate_limit_exceeded'}, {'http_status_code': 503},
                      {'message': 'Rate limit reached for requests'}, {'code': 'invalid_request',
                       'message': 'You’ve hit your usage limit.'}):
            stream = CodexStream()
            stream.feed({'type': 'turn.failed', 'error': error})
            self.assertEqual((stream.outcome(1)[0], stream.outcome(1)[3]), ('provider-error', None))
        self.assertEqual(CodexStream().outcome(1)[0], 'no-output')

    def test_frames_report_text_tokens_and_completed_tools(self):
        stream = CodexStream(2)
        self.assertEqual(stream.feed({'type': 'thread.started', 'thread_id': 'thread-1'}), ('thread-1', []))
        self.assertEqual(stream.feed({'type': 'turn.started'})[1][0]['type'], 'turn_start')
        item = {'id': 'cmd', 'type': 'command_execution', 'command': 'pwd', 'status': 'completed', 'exit_code': 0}
        self.assertEqual(stream.feed({'type': 'item.started', 'item': item})[1][0]['type'], 'tool_queued')
        stream.feed({'type': 'item.completed', 'item': item})
        stream.feed({'type': 'item.completed', 'item': item})
        self.assertFalse(stream.stop_at_cap)
        stream.feed({'type': 'item.completed', 'item': {'id': 'text', 'type': 'agent_message',
                    'text': 'The usage limit and rate limit tests pass. STATUS=COMPLETE'}})
        stream.feed({'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 40,
                                                      'output_tokens': 12}})
        status, text, spent, fatal = stream.outcome(0)
        self.assertEqual((status, fatal), ('reported', None))
        self.assertIn('STATUS=COMPLETE', text)
        self.assertEqual(spent['usage'], {'inputTokens': 100, 'cacheReadInputTokens': 40, 'outputTokens': 12})
        self.assertEqual(spent['numTurns'], 1)
        self.assertNotIn('costUsd', spent)
        stream.feed({'type': 'item.completed', 'item': {'id': 'patch', 'type': 'file_change',
                                                     'status': 'completed', 'changes': []}})
        self.assertTrue(stream.stop_at_cap)
        self.assertEqual(stream.outcome(-15)[0], 'turn-cap')


class CodexAdapterTests(unittest.TestCase):
    def test_isolated_home_symlinks_auth_and_refuses_a_regular_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            operator = root / 'operator'
            operator.mkdir()
            (operator / 'auth.json').write_text('{"auth_mode":"chatgpt"}')
            skills = root / 'skills'
            skills.mkdir()
            with patch.dict(os.environ, {'CODEX_HOME': str(operator)}):
                backend = Codex(root / 'runner')
                backend.prepare(skills)
                auth = root / 'runner/auth.json'
                self.assertTrue(auth.is_symlink())
                self.assertEqual(auth.resolve(), (operator / 'auth.json').resolve())
                auth.unlink()
                auth.symlink_to(root / 'stale')
                backend.prepare(skills)
                self.assertEqual(auth.resolve(), (operator / 'auth.json').resolve())
                auth.unlink()
                auth.write_text('operator owned')
                with self.assertRaises(Fatal):
                    backend.prepare(skills)
                self.assertEqual(auth.read_text(), 'operator owned')

    def test_argv_resume_browser_and_environment(self):
        leaked = {k: 'fake' for k in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID', 'OPENAI_ORGANIZATION',
                  'OPENAI_PROJECT', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN', 'CODEX_REFRESH_TOKEN_URL_OVERRIDE')}
        with patch.dict(os.environ, {**leaked, 'CODEX_HOME': '/operator'}):
            backend = Codex(Path('/runner'))
            env = backend.env()
        self.assertEqual(env['CODEX_HOME'], '/runner')
        self.assertFalse(set(leaked) & set(env))
        for session in (None, 'saved-thread'):
            argv = backend.argv('gpt-6-luna', 'high', 3, 'prompt', session, browser=True)
            self.assertEqual(argv[1:3], ['exec', 'resume'] if session else ['exec', '--json'])
            self.assertIn('--dangerously-bypass-approvals-and-sandbox', argv)
            self.assertIn('model_reasoning_effort="high"', argv)
            self.assertIn('mcp_servers.chrome-devtools.command="npx"', argv)
            self.assertEqual(argv[-1], 'prompt')
            self.assertNotIn('--max-budget-usd', argv)
            if session:
                self.assertEqual(argv[-2], session)
        self.assertNotIn('mcp_servers.chrome-devtools.command="npx"', backend.argv('gpt-6-luna', 'low', 3, 'prompt'))


class CodexArguments(unittest.TestCase):
    def test_defaults_aliases_and_efforts(self):
        with patch.dict(os.environ, {}, clear=True), patch('sys.argv', ['ticket-runner', '207', '--backend', 'codex']):
            a = parse_args()
        self.assertEqual((a.model, a.effort), ('gpt-6-luna', 'high'))
        self.assertEqual(a.ticket_review_models, ['gpt-6-luna'])
        self.assertEqual(a.epic_review_models, ['gpt-6.1-sol'])
        self.assertEqual((a.verify_model, a.verify_effort), ('gpt-6-luna', 'high'))
        self.assertEqual((a.launch_plan_model, a.launch_plan_effort), ('gpt-6-luna', 'low'))
        with patch.dict(os.environ, {}, clear=True), patch('sys.argv', [
                'ticket-runner', '207', '--backend', 'codex', '--model', 'sol', '--effort', 'ultra']):
            self.assertEqual(parse_args().model, 'gpt-6.1-sol')


class SubscriptionLedgerTests(unittest.TestCase):
    def test_a_capped_attempt_without_usage_keeps_the_last_thread_baseline(self):
        runner = ledger.Harness()
        record = {'sessionId': 'thread'}
        runner.data['209'] = record
        runner.spend(record, 'implement', 'gpt-6-luna', 'high',
                     {'usage': {'inputTokens': 100, 'outputTokens': 20}}, None, 'reported', '#209')
        runner.spend(record, 'implement', 'gpt-6-luna', 'high', {'usage': {}, 'numTurns': 2},
                     'thread', 'turn-cap', '#209')
        runner.spend(record, 'implement', 'gpt-6-luna', 'high',
                     {'usage': {'inputTokens': 160, 'outputTokens': 30}}, 'thread', 'reported', '#209')
        self.assertEqual(runner.spent_total(), '190 tokens')

    def test_resume_subtracts_thread_totals_without_fabricating_cost(self):
        runner = ledger.Harness()
        record = {'sessionId': 'thread'}
        runner.data['209'] = record
        runner.spend(record, 'implement', 'gpt-6-luna', 'high',
                     {'usage': {'inputTokens': 100, 'cacheReadInputTokens': 40, 'outputTokens': 20}, 'numTurns': 2},
                     None, 'turn-cap', '#209')
        runner.spend(record, 'implement', 'gpt-6-luna', 'high',
                     {'usage': {'inputTokens': 160, 'cacheReadInputTokens': 50, 'outputTokens': 30}, 'numTurns': 1},
                     'thread', 'reported', '#209')
        self.assertEqual(record['costs'][1]['usage'],
                         {'inputTokens': 60, 'cacheReadInputTokens': 10, 'outputTokens': 10})
        self.assertEqual(runner.spent_total(), '190 tokens')
        self.assertNotIn('costUsd', record)
        stages, tickets, total = runner.cost_tables()
        self.assertEqual(total, '190 tokens')
        self.assertIn('| implement | 2 | 190 |', stages)
        runner.data['_review'] = {}
        self.assertEqual(runner.spent_total(), '190 tokens')

    def test_mixed_legacy_costs_keep_unpriced_sessions_visible_as_tokens(self):
        runner = ledger.Harness()
        runner.data.update({'209': {'costs': [{'stage': 'implement', 'costUsd': 1.25}]},
                            '210': {'costs': [{'stage': 'implement', 'usage': {'inputTokens': 100, 'outputTokens': 20}}]}})
        stages, tickets, total = runner.cost_tables()
        self.assertIn('| #210 | 120 tokens |', tickets)
        self.assertIn('$1.25', str(total))
        self.assertIn('120 tokens', str(total))


if __name__ == '__main__':
    unittest.main()
