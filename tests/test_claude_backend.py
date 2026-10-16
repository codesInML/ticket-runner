"""The claude-api backend end to end: real temporary git repo, fake gh, fake claude and a fake keychain."""

import json
import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))  # the CommandCode suite's harness, however this file is loaded
import test_ticket_runner as base  # noqa: E402  (a module, so its own tests are not collected twice)

KEY = "sk-ant-company-test-0000"
# Claude Code in stream-json: system/init, assistant text and tool_use, a failed tool_result, an api_retry, then
# the result. A resumed session's totals include its earlier processes', as Claude Code restores them.
CLAUDE = """import json, os, re, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ['ROOT'])
prompt = args[args.index('-p') + 1]
with open(os.environ['AGENT_LOG'], 'a') as f:
    __import__('fcntl').flock(f, 2)
    f.write(json.dumps(args) + '\\n')
with open(os.environ['ENV_LOG'], 'a') as f:
    __import__('fcntl').flock(f, 2)
    f.write(json.dumps({k: os.environ.get(k) for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
        'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')}) + '\\n')
mode = os.environ.get('MODE', 'success')
resumed = args[args.index('--resume') + 1] if '--resume' in args else None
model = args[args.index('--model') + 1]
ticket = Path.cwd().name
def emit(frame):
    print(json.dumps(frame), flush=True)
def done(text, session, subtype='success', is_error=False, rc=0, cost=1.25, extra=None):
    totals = root / 'fake-bin' / ('cost-' + session)
    total = cost + (float(totals.read_text()) if resumed and totals.exists() else 0)
    totals.write_text(str(total))
    emit({'type': 'result', 'subtype': subtype, 'is_error': is_error, 'result': text, 'session_id': session,
          'num_turns': 3, 'duration_ms': 61000, 'total_cost_usd': total,
          'usage': {'input_tokens': 5, 'output_tokens': 6},
          'modelUsage': {model: {'inputTokens': 100, 'outputTokens': 20, 'cacheReadInputTokens': 300,
                                 'cacheCreationInputTokens': 40, 'costUSD': total}}, **(extra or {})})
    sys.exit(rc)
def fail(text, error, status, session):
    emit({'type': 'assistant', 'session_id': session, 'parent_tool_use_id': None, 'error': error,
          'message': {'id': 'msg_err', 'content': [{'type': 'text', 'text': text}]}})
    done(text, session, is_error=True, rc=1, cost=0.01, extra={'api_error_status': status})
def commit(name, text):
    Path(name).parent.mkdir(parents=True, exist_ok=True)
    Path(name).write_text(text)
    subprocess.run(['git', 'add', name], check=True)
    subprocess.run(['git', 'commit', '-qm', 'Change ' + name], check=True)
session = resumed or ('session-' + ticket)
emit({'type': 'system', 'subtype': 'init', 'session_id': session, 'apiKeySource': 'ANTHROPIC_API_KEY',
      'model': model, 'tools': ['Bash', 'Read', 'Edit']})
emit({'type': 'assistant', 'session_id': session, 'parent_tool_use_id': None, 'message': {'id': 'msg_1', 'content': [
    {'type': 'text', 'text': 'Looking around'},
    {'type': 'tool_use', 'id': 'tu_1', 'name': 'Bash', 'input': {'command': 'ls', 'description': 'List files'}}]}})
emit({'type': 'user', 'session_id': session, 'message': {'content': [
    {'type': 'tool_result', 'tool_use_id': 'tu_1', 'is_error': True, 'content': 'boom'}]}})
emit({'type': 'system', 'subtype': 'api_retry', 'attempt': 1, 'max_retries': 10, 'retry_delay_ms': 1,
      'error_status': 529, 'error': 'overloaded', 'session_id': session})
emit({'type': 'rate_limit_event', 'session_id': session})
if 'You are an epic reviewer' in prompt:
    done('## INTEGRATION\\nFits.\\n## FINDINGS\\nNone.\\nVERDICT=SHIP', 'epic-' + model)
if 'You are the ticket reviewer' in prompt:
    done('## BLOCKING\\nNone.\\n## DEFERRED\\nNone.\\nVERDICT=PASS', 'review-' + ticket)
if 'You write the launch plan' in prompt:
    done('## Pre-launch checklist\\n- [x] ok\\n## Rollback plan\\n- Revert', 'plan')
if 'You are the verifier' in prompt:
    checks = re.findall(r'^- (C\\d+):', Path(re.search(r'(\\S+/checks.md)', prompt)[1]).read_text(), re.M)
    done('## RESULTS\\n' + '\\n'.join('VERIFIED %s ok' % c for c in checks) + '\\nVERIFY=PASS', 'verify')
if mode == 'fatal-parallel':
    (root / 'fake-bin' / ('started-' + ticket)).write_text('started')
    if ticket == '210':
        time.sleep(30)  # stopped by the runner when #209's account failure lands
    deadline = time.monotonic() + 10
    while not (root / 'fake-bin' / 'started-210').exists() and time.monotonic() < deadline:
        time.sleep(.02)
    fail('Credit balance is too low', 'billing_error', 400, session)
if mode == 'credit': fail('Credit balance is too low', 'billing_error', 400, session)
if mode == 'credit-words': fail('Your credit balance is too low to access the Anthropic API.', None, None, session)
if mode == 'invalid-key': fail('Invalid API key · Fix external API key', 'authentication_failed', 401, session)
if mode == '429': fail('API Error: 429 rate_limit_error', 'rate_limit', 429, session)
if mode == 'empty': sys.exit(1)
if mode == 'edit-claude-md': (root / 'CLAUDE.md').write_text('changed by the agent')
if mode == 'edit-claude-settings': (root / '.claude/settings.json').write_text('{"changed": true}')
if mode == 'operator-claude-dir':  # what the operator's own sessions do there while the runner works
    (root / '.claude/settings.local.json').write_text('{"local": 2}')
    (root / '.claude/worktrees/other').mkdir(parents=True, exist_ok=True)
if mode == 'commit-claude-md': commit('CLAUDE.md', 'changed in a ticket')
if mode == 'commit-claude-dir': commit('.claude/agents/new.md', 'changed in a ticket')
capped = mode in ('max-turns', 'budget') and not resumed and ticket == '209'
Path('work.txt').write_text(ticket + ' partial' if capped else ticket + ' done')
if not capped:
    subprocess.run(['git', 'add', 'work.txt'], check=True)
    subprocess.run(['git', 'commit', '-qm', 'Implement ticket'], check=True)
if capped:
    done('', session, 'error_max_turns' if mode == 'max-turns' else 'error_max_budget_usd', True, 1,
         extra={'errors': []})
done('Done; the Advance rate limit and credit balance checks pass.\\nSTATUS=COMPLETE', session)
"""
SECURITY = """import os, sys
args = sys.argv[1:]
service = args[args.index('-s') + 1]
if os.environ.get('FAKE_KEYCHAIN') and service == os.environ.get('FAKE_KEYCHAIN_SERVICE'):
    print(os.environ['FAKE_KEYCHAIN'])
    sys.exit(0)
sys.exit(44)
"""


class ClaudeBackendTests(base.RunnerTests):
    def setUp(self):
        super().setUp()
        self.fake("claude", CLAUDE)
        self.fake("security", SECURITY)  # never the real keychain
        self.env.update(CLAUDE_BIN=str(self.bin / "claude"), ENV_LOG=str(self.root / "env.log"),
                        TICKET_RUNNER_ANTHROPIC_API_KEY=KEY, ANTHROPIC_API_KEY="sk-personal",
                        ANTHROPIC_AUTH_TOKEN="oauth-personal", ANTHROPIC_BASE_URL="http://proxy.invalid",
                        CLAUDE_CODE_USE_BEDROCK="1")
        for name in ("BACKEND", "TICKET_RUNNER_KEYCHAIN_SERVICE", *(f"{s}_MAX_BUDGET_USD" for s in (
                "IMPL", "TICKET_REVIEW", "EPIC_REVIEW", "VERIFY", "LAUNCH_PLAN"))):
            self.env.pop(name, None)

    def run_claude(self, *args, mode="success"):
        return self.run_script(*args, mode=mode, script="ticket-runner-claude")

    @staticmethod
    def model(call):
        return call[call.index('--model') + 1]

    def envs(self):
        return [json.loads(line) for line in (self.root / "env.log").read_text().splitlines()]

    def assert_key_never_written(self, *outputs):
        for output in outputs:
            self.assertNotIn(KEY, output)
        for path in (self.root / ".scratch").rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text(errors="replace"), path)

    def test_every_stage_runs_bare_on_claude_with_only_the_company_key(self):
        self.configure(gate=['python3 -c "import os; open(os.environ[\'ROOT\'] + \'/gate-env.log\', \'a\')'
                             '.write(str(os.environ.get(\'ANTHROPIC_API_KEY\')) + chr(10))"'])
        result = self.run_claude()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'epic review', 'launch plan'])
        calls = self.calls()
        for call in calls:
            self.assertEqual(call[:2], ['--bare', '-p'])
            for flag in ('--output-format', '--verbose', '--max-turns', '--dangerously-skip-permissions'):
                self.assertIn(flag, call)
            self.assertEqual(call[call.index('--output-format') + 1], 'stream-json')
            self.assertNotIn('--append-system-prompt-file', call)  # no CLAUDE.md in this project
        self.assertEqual([(self.model(c), c[c.index('--effort') + 1]) for c in calls],
                         [('claude-sonnet-5-5', 'medium')] * 2 + [('claude-opus-5-5', 'high'), ('claude-haiku-5-5', 'low')])
        self.assertIn('Use only your own tools and sub-agents', self.prompt(calls[0]))
        self.assertNotIn('Use only CommandCode', self.prompt(calls[2]))
        for env in self.envs():
            self.assertEqual(env['ANTHROPIC_API_KEY'], KEY)
            self.assertEqual([v for k, v in env.items() if k != 'ANTHROPIC_API_KEY'], [None] * 5)
        gate = (self.root / 'gate-env.log').read_text().split()
        self.assertEqual(gate, ['sk-personal'] * 2)  # the runner's own commands never get the company key
        state = self.state()
        self.assertEqual(state['_meta']['backend'], 'claude-api')
        self.assertEqual(state['209']['backend'], 'claude-api')
        self.assertEqual(state['209']['sessionId'], 'session-209')
        # Narration: the same feed as CommandCode's, from Claude's events.
        for line in ('AGENT Looking around', '▸ Bash List files', '✗ Bash boom',
                     '↻ api retry overloaded (529), attempt 1/10', '── turn 1 ──',
                     '✓ #209 implement · $1.25 · 3 turns · 1m', '✓ launch plan · $1.25'):
            self.assertIn(line, result.stdout)
        self.assert_key_never_written(result.stdout, result.stderr)

    def test_cost_is_recorded_per_attempt_and_summed_across_a_resume(self):
        failed = self.run_claude(mode='max-turns')
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(self.state()['209']['status'], 'turn-cap')
        result = self.run_claude('--resume', mode='max-turns')
        self.assertEqual(result.returncode, 0, result.stderr)
        resumed = self.calls()[1]
        self.assertEqual(resumed[resumed.index('--resume') + 1], 'session-209')
        costs = self.state()['209']['costs']
        # The resumed process reported the session's $2.50; the runner counts only its own $1.25.
        self.assertEqual([(c['stage'], c['costUsd'], c['session']['costUsd']) for c in costs],
                         [('implement', 1.25, 1.25), ('implement', 1.25, 2.5)])
        self.assertEqual(self.state()['209']['costUsd'], 2.5)
        self.assertEqual(costs[0]['usage'], {'input': 100, 'output': 20, 'cacheRead': 300, 'cacheCreation': 40})
        self.assertEqual(costs[1]['usage'], {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheCreation': 0})
        self.assertEqual((costs[0]['model'], costs[0]['effort'], costs[0]['numTurns'], costs[0]['durationMs']),
                         ('claude-sonnet-5-5', 'medium', 3, 61000))
        self.assertIn('Ticket graph: ', result.stdout)
        self.assertRegex(result.stdout, r'Ticket graph: .* · \$2\.50 spent')
        self.assertIn('Cost of epic #207 so far (estimated): $6.25', result.stdout)  # 2.50 + 1.25 x 3
        self.assertNotIn(['issue', 'comment', '207'], [c[:3] for c in self.gh_calls()])  # --post-costs is off

    def test_post_costs_comments_on_the_tickets_and_the_epic_but_never_the_pull_request(self):
        result = self.run_claude('--post-costs')
        self.assertEqual(result.returncode, 0, result.stderr)
        closes = {c[2]: c[c.index('--comment') + 1] for c in self.gh_calls() if c[:2] == ['issue', 'close']}
        self.assertIn('Cost: $1.25 (estimated).', closes['209'])
        comments = [c for c in self.gh_calls() if c[:3] == ['issue', 'comment', '207']]
        self.assertEqual(len(comments), 1)
        table = (self.root / '.scratch/run-tickets-commandcode/207/costs.md').read_text()
        self.assertIn('| implement | 2 | $2.50 |', table)
        self.assertIn('| epic stages | $2.50 |', table)
        self.assertNotIn('Cost', self.prs()[0]['body'])

    def test_budget_cap_is_retryable_and_says_the_budget_ran_out(self):
        self.env['IMPL_MAX_BUDGET_USD'] = '3'
        self.env['EPIC_REVIEW_MAX_BUDGET_USD'] = '0.5'
        failed = self.run_claude(mode='budget')
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(self.state()['209']['status'], 'budget-cap')
        self.assertIn('the budget ran out, not the ticket', self.state()['209']['reason'])
        self.assertIn('spent its $3.00 budget (IMPL_MAX_BUDGET_USD)', failed.stdout)
        self.assertEqual(self.calls()[0][self.calls()[0].index('--max-budget-usd') + 1], '3')
        result = self.run_claude('--resume', mode='budget')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.call('epic review')[self.call('epic review').index('--max-budget-usd') + 1], '0.5')
        self.assertNotIn('--max-budget-usd', self.call('launch plan'))

    def test_transient_and_empty_failures_keep_the_ticket_retryable(self):
        for mode, status in (('429', 'provider-error'), ('empty', 'no-output')):
            with self.subTest(mode=mode):
                self.setUp()
                result = self.run_claude(mode=mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.state()['209']['status'], status)
                self.assertEqual(len(self.calls()), 1)

    def test_account_failures_stop_the_whole_run(self):
        for mode, message in (('credit', 'company API key: credit balance too low — top up, then --resume'),
                              ('credit-words', 'credit balance too low'),
                              ('invalid-key', 'rejected as invalid or revoked')):
            with self.subTest(mode=mode):
                self.setUp()
                result = self.run_claude(mode=mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(len(self.calls()), 1)
                self.assertIn(self.state()['209']['status'], ('provider-error', 'interrupted'))
                self.assert_key_never_written(result.stdout, result.stderr)

    def test_an_account_failure_in_one_ticket_stops_the_others(self):
        self.issues[1]['body'] = 'Parent #207'  # independent, so both run at once
        started = time.monotonic()
        result = self.run_claude(mode='fatal-parallel')
        self.assertEqual(result.returncode, 1)
        self.assertLess(time.monotonic() - started, 25)
        self.assertIn('credit balance too low', result.stderr)
        self.assertEqual(self.state()['210']['status'], 'interrupted')
        self.assertNotIn('epic review', self.kinds())

    def claude_files(self):
        """A committed CLAUDE.md and .claude/settings.json, beside untracked worktrees and local settings."""
        (self.root / 'CLAUDE.md').write_text('# Project notes\n')
        (self.root / '.claude/worktrees/old').mkdir(parents=True)
        (self.root / '.claude/settings.json').write_text('{}')
        (self.root / '.claude/settings.local.json').write_text('{"local": 1}')
        with (self.root / '.gitignore').open('a') as f:
            f.write('.claude/worktrees/\n.claude/settings.local.json\n')
        self.git('add', 'CLAUDE.md', '.claude/settings.json', '.gitignore')
        self.git('commit', '-qm', 'Claude notes')

    def test_claude_md_is_appended_and_protected_with_the_tracked_claude_dir(self):
        self.claude_files()
        result = self.run_claude(mode='operator-claude-dir')
        self.assertEqual(result.returncode, 0, result.stderr)
        implement, review = self.calls()[0], self.call('epic review')
        root = self.root.resolve()
        self.assertEqual(implement[implement.index('--append-system-prompt-file') + 1],
                         str(root / '.scratch/worktrees/209/CLAUDE.md'))
        self.assertEqual(review[review.index('--append-system-prompt-file') + 1], str(root / 'CLAUDE.md'))
        self.assertIn('CLAUDE.md, .claude/', self.prompt(implement))
        for mode, expected in (('edit-claude-md', 'CLAUDE.md'), ('edit-claude-settings', '.claude/settings.json')):
            with self.subTest(mode=mode):
                self.setUp()
                self.claude_files()
                result = self.run_claude(mode=mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn('Agent modified', result.stderr)
                self.assertIn(expected, result.stderr)

    def test_a_ticket_touching_claude_files_is_not_integrated(self):
        for mode, expected in (('commit-claude-md', 'CLAUDE.md'), ('commit-claude-dir', '.claude/agents/new.md')):
            with self.subTest(mode=mode):
                self.setUp()
                result = self.run_claude(mode=mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.state()['209']['status'], 'needs-human')
                self.assertIn(f'protected files ({expected})', self.state()['209']['reason'])
        # On commandcode CLAUDE.md is the project's own business.
        self.setUp()
        self.fake("command-code", base_commit_claude_md())
        self.assertEqual(self.run_script().returncode, 0)

    def test_preflight_refuses_before_anything_is_spent(self):
        result = self.run_claude('--dry-run', '--model', 'glm', '--epic-review-model', 'opus,flash')
        self.assertEqual(result.returncode, 1)
        self.assertIn('implement model glm is not a Claude model', result.stderr)
        self.assertIn('epic review model flash is not a Claude model', result.stderr)
        self.assertEqual(self.run_claude('--dry-run', '--effort', 'turbo').returncode, 2)
        self.env.pop('TICKET_RUNNER_ANTHROPIC_API_KEY')
        for args in (('--dry-run',), ()):
            result = self.run_claude(*args)
            self.assertEqual(result.returncode, 1)
            self.assertIn('security add-generic-password -s krim-anthropic-api-key -a "$USER" -w', result.stderr)
        self.env['TICKET_RUNNER_ANTHROPIC_API_KEY'] = KEY
        self.env['CLAUDE_BIN'] = str(self.root / 'no-such-claude')
        self.env['COMMANDCODE_BIN'] = str(self.root / 'no-such-command-code')
        result = self.run_claude()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Claude Code binary not found', result.stderr)
        self.assertNotIn('CommandCode binary', result.stderr)
        self.assertFalse((self.root / 'agents.log').exists())

    def test_dry_run_names_the_backend_models_budgets_and_key_source_but_never_the_key(self):
        self.env['IMPL_MAX_BUDGET_USD'] = '5'
        result = self.run_claude('--dry-run', '--workflow', 'osmani', '--skip-plan-check')
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in ('Backend: claude-api, company key from env',
                     'implement     claude-sonnet-5-5 @ medium, 500 turns, budget $5.00',
                     'ticket review claude-opus-5-5 @ medium, 500 turns',
                     'epic review   claude-opus-5-5 @ high, 500 turns',
                     'verify        claude-sonnet-5-5 @ medium, 500 turns',
                     'launch plan   claude-haiku-5-5 @ low, 500 turns',
                     'epic review  claude-opus-5-5, self-checked'):
            self.assertIn(line, result.stdout)
        self.env.pop('TICKET_RUNNER_ANTHROPIC_API_KEY')
        self.env.update(FAKE_KEYCHAIN=KEY, FAKE_KEYCHAIN_SERVICE='team-key', TICKET_RUNNER_KEYCHAIN_SERVICE='team-key')
        result = self.run_claude('--dry-run', '--launch-plan-model', 'sonnet')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('company key from keychain:team-key', result.stdout)
        self.assertIn('launch plan (claude-sonnet-5-5)', result.stdout)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        self.assertFalse((self.root / '.scratch').exists())
        result = self.run_claude()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({e['ANTHROPIC_API_KEY'] for e in self.envs()}, {KEY})

    def test_backend_switch_is_refused_while_a_ticket_is_part_way(self):
        self.assertEqual(self.run_script(mode='human').returncode, 1)
        result = self.run_claude('--resume')
        self.assertEqual(result.returncode, 1)
        self.assertIn('runs the commandcode backend and #209 is part-way through; finish them with --backend '
                      'commandcode', result.stderr)
        self.assertEqual(self.state()['_meta']['backend'], 'commandcode')
        self.assertFalse((self.root / 'env.log').exists())

    def test_backend_switch_is_allowed_once_nothing_is_part_way_and_composes_with_osmani(self):
        self.plan_osmani()
        self.assertEqual(self.run_script().returncode, 0)
        result = self.run_claude('--epic-only', '--workflow', 'osmani')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()['_meta']['backend'], 'claude-api')
        self.assertEqual(self.state()['_meta']['workflow'], 'osmani')
        self.assertEqual(self.model(self.call('epic review')), 'claude-opus-5-5')

    def test_osmani_ticket_review_runs_on_opus(self):
        self.plan_osmani()
        result = self.run_claude('--workflow', 'osmani')
        self.assertEqual(result.returncode, 0, result.stderr)
        review = self.call('ticket review')
        self.assertEqual((self.model(review), review[review.index('--effort') + 1]), ('claude-opus-5-5', 'medium'))
        self.assertIn('✓ #209 ticket review · $1.25', result.stdout)

    def test_browser_verifier_gets_the_devtools_server_inline(self):
        self.serve(browser=True)
        result = self.run_claude()  # no `command-code mcp list` check on this backend
        self.assertEqual(result.returncode, 0, result.stderr)
        verifier = self.call('verifier')
        config = json.loads(verifier[verifier.index('--mcp-config') + 1])
        self.assertEqual(config['mcpServers']['chrome-devtools']['args'],
                         ['-y', 'chrome-devtools-mcp@latest', '--isolated'])
        self.assertIn('mcp__chrome-devtools__navigate_page', verifier[verifier.index('--allowedTools') + 1])
        self.assertEqual((self.model(verifier), verifier[verifier.index('--effort') + 1]), ('claude-sonnet-5-5', 'medium'))
        self.assertNotIn('--mcp-config', self.calls()[0])


def base_commit_claude_md():
    """A CommandCode fake whose ticket commits CLAUDE.md alongside its work."""
    return """import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['AGENT_LOG'], 'a') as f: f.write(json.dumps(args) + '\\n')
prompt = args[args.index('-p') + 1]
def done(text):
    print(json.dumps({'type': 'result', 'subtype': 'success', 'sessionId': 's', 'finalText': text}))
    sys.exit(0)
if 'You are an epic reviewer' in prompt: done('## FINDINGS\\nNone.\\nVERDICT=SHIP')
if 'Cross-examination' in prompt or 'Self-check' in prompt: done('## ANSWERS\\n\\nCHECK=DONE')
if 'You write the launch plan' in prompt: done('## Rollback plan\\n- Revert')
Path('CLAUDE.md').write_text(Path.cwd().name)
subprocess.run(['git', 'add', 'CLAUDE.md'], check=True)
subprocess.run(['git', 'commit', '-qm', 'Notes'], check=True)
done('STATUS=COMPLETE')
"""


# Only this file's tests: the inherited CommandCode ones already run from test_ticket_runner.
for _name in dir(base.RunnerTests):
    if _name.startswith("test_") and _name not in ClaudeBackendTests.__dict__:
        setattr(ClaudeBackendTests, _name, None)


if __name__ == "__main__":
    unittest.main()
