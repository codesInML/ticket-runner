"""The claude backend end to end: real temporary git repo, fake gh and fake claude."""

import json
import os
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).parent))  # the CommandCode suite's harness, however this file is loaded
import test_ticket_runner as base  # noqa: E402  (a module, so its own tests are not collected twice)

# Claude Code in stream-json: system/init, assistant text and tool_use, a failed tool_result, an api_retry, then
# the result. A resumed session's totals include its earlier processes', as Claude Code restores them.
CLAUDE = """import json, os, re, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ['ROOT'])
if args[:2] == ['auth', 'status']:
    assert not os.environ.get('ANTHROPIC_API_KEY') and not os.environ.get('CLAUDE_CONFIG_DIR')
    print(os.environ.get('FAKE_AUTH', json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'email': 'private@example.com'})))
    sys.exit(0)
prompt = args[args.index('-p') + 1]
with open(os.environ['AGENT_LOG'], 'a') as f:
    __import__('fcntl').flock(f, 2)
    f.write(json.dumps(args) + '\\n')
with open(os.environ['ENV_LOG'], 'a') as f:
    __import__('fcntl').flock(f, 2)
    f.write(json.dumps({k: os.environ.get(k) for k in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
        'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
        'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY')}) + '\\n')
mode = os.environ.get('MODE', 'success')
resumed = args[args.index('--resume') + 1] if '--resume' in args else None
model = args[args.index('--model') + 1]
ticket = Path.cwd().name
LIMIT = "You've hit your session limit · resets 3pm"
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
# A session that found an API key instead of the subscription login, as Claude Code reports it.
source = 'ANTHROPIC_API_KEY' if mode == 'api-key' else 'none'
emit({'type': 'system', 'subtype': 'init', 'session_id': session, 'apiKeySource': source,
      'model': model, 'tools': ['Task', 'Bash', 'Read', 'Edit', 'Write', 'Skill']})
if mode == 'api-key':
    time.sleep(30)  # stopped by the runner before any API call
    (root / 'fake-bin' / 'spent-on-api-key').write_text('spent')
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
    fail(LIMIT, 'rate_limit', 429, session)
if mode == 'usage-limit':
    emit({'type': 'rate_limit_event', 'session_id': session, 'rate_limit_info': {'status': 'rejected', 'resetsAt': 1893456000}})
    fail(LIMIT, 'rate_limit', 429, session)
if mode == 'usage-words': fail(LIMIT, 'rate_limit', 429, session)
if mode == 'signed-out': fail('Not logged in · Please run /login', 'authentication_failed', 401, session)
if mode == '429': fail('API Error: 429 rate_limit_error', 'rate_limit', 429, session)
if mode == 'empty': sys.exit(1)
if mode == 'edit-claude-md': (root / 'CLAUDE.md').write_text('changed by the agent')
if mode == 'edit-claude-settings': (root / '.claude/settings.json').write_text('{"changed": true}')
if mode == 'operator-claude-dir':  # what the operator's own sessions do there while the runner works
    (root / '.claude/settings.local.json').write_text('{"local": 2}')
    (root / '.claude/worktrees/other').mkdir(parents=True, exist_ok=True)
if mode == 'commit-nested-claude-md': commit('src/CLAUDE.md', 'injected instructions')
if mode == 'commit-local-claude-md': commit('CLAUDE.local.md', 'injected instructions')
if mode == 'edit-nested-claude-md': (root / 'src').mkdir(exist_ok=True); (root / 'src/CLAUDE.md').write_text('injected')
if mode == 'commit-claude-md': commit('CLAUDE.md', 'changed in a ticket')
if mode == 'commit-claude-dir': commit('.claude/agents/new.md', 'changed in a ticket')
capped = mode == 'max-turns' and not resumed and ticket == '209'
Path('work.txt').write_text(ticket + ' partial' if capped else ticket + ' done')
if not capped:
    subprocess.run(['git', 'add', 'work.txt'], check=True)
    subprocess.run(['git', 'commit', '-qm', 'Implement ticket'], check=True)
if capped:
    done('', session, 'error_max_turns', True, 1, extra={'errors': []})
done('Done; the Advance rate limit and credit balance checks pass.\\nSTATUS=COMPLETE', session)
"""


class ClaudeBackendTests(base.RunnerTests):
    def setUp(self):
        super().setUp()
        self.fake("claude", CLAUDE)
        self.env.update(CLAUDE_BIN=str(self.bin / "claude"), ENV_LOG=str(self.root / "env.log"),
                        ANTHROPIC_API_KEY="sk-personal",
                        ANTHROPIC_AUTH_TOKEN="oauth-personal", ANTHROPIC_BASE_URL="http://proxy.invalid",
                        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-personal", CLAUDE_CONFIG_DIR="/personal/config",
                        CLAUDE_CODE_USE_BEDROCK="1")
        self.env.pop("BACKEND", None)

    def run_claude(self, *args, mode="success"):
        return self.run_script("--backend", "claude", *args, mode=mode)

    @staticmethod
    def model(call):
        return call[call.index('--model') + 1]

    def envs(self):
        return [json.loads(line) for line in (self.root / "env.log").read_text().splitlines()]

    def test_every_stage_runs_on_the_subscription_with_no_other_credential(self):
        self.configure(gate=['python3 -c "import os; open(os.environ[\'ROOT\'] + \'/gate-env.log\', \'a\')'
                             '.write(str(os.environ.get(\'ANTHROPIC_API_KEY\')) + chr(10))"'])
        result = self.run_claude()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'epic review', 'launch plan'])
        calls = self.calls()
        for call in calls:
            self.assertEqual(call[0], '-p')  # never --bare: it withholds sub-agents, Write and Skill
            for flag in ('--output-format', '--verbose', '--max-turns', '--dangerously-skip-permissions',
                         '--strict-mcp-config', '--settings'):
                self.assertIn(flag, call)
            self.assertEqual(call[call.index('--output-format') + 1], 'stream-json')
            self.assertEqual(call[call.index('--setting-sources') + 1], 'user')
            self.assertNotIn('--append-system-prompt-file', call)  # no CLAUDE.md in this project
        self.assertEqual([(self.model(c), c[c.index('--effort') + 1]) for c in calls],
                         [('claude-sonnet-5-5', 'medium')] * 2 + [('claude-opus-5-5', 'medium'), ('claude-haiku-5-5', 'low')])
        self.assertIn('Use only your own tools and sub-agents', self.prompt(calls[0]))
        self.assertNotIn('Use only CommandCode', self.prompt(calls[2]))
        for env in self.envs():
            self.assertEqual(env.pop('CLAUDE_CODE_DISABLE_AUTO_MEMORY'), '1')
            self.assertEqual(list(env.values()), [None] * 8)
        self.assertFalse((self.root / '.scratch/claude-config').exists())
        self.assertEqual((self.root / 'gate-env.log').read_text().split(), ['sk-personal'] * 2)  # the runner's own env
        state = self.state()
        self.assertEqual(state['_meta']['backend'], 'claude')
        self.assertEqual(state['209']['backend'], 'claude')
        self.assertEqual(state['209']['sessionId'], 'session-209')
        # Narration: the same feed as CommandCode's, from Claude's events.
        for line in ('AGENT Looking around', '▸ Bash List files', '✗ Bash boom',
                     '↻ api retry overloaded (529), attempt 1/10', '── turn 1 ──',
                     '✓ #209 implement · 120 tokens · 3 turns · 1m', '✓ launch plan · 120 tokens'):
            self.assertIn(line, result.stdout)
        self.assertNotIn('private@example.com', result.stdout + result.stderr)

    def test_tokens_are_recorded_per_attempt_and_no_dollars_anywhere(self):
        failed = self.run_claude(mode='max-turns')
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(self.state()['209']['status'], 'turn-cap')
        result = self.run_claude('--resume', mode='max-turns')
        self.assertEqual(result.returncode, 0, result.stderr)
        resumed = self.calls()[1]
        self.assertEqual(resumed[resumed.index('--resume') + 1], 'session-209')
        costs = self.state()['209']['costs']
        self.assertEqual(costs[0]['usage'], {'inputTokens': 100, 'outputTokens': 20, 'cacheReadInputTokens': 300,
                                             'cacheCreationInputTokens': 40})
        self.assertEqual(set(costs[1]['usage'].values()), {0})  # a resume's totals repeat what was counted
        self.assertEqual((costs[0]['model'], costs[0]['effort'], costs[0]['numTurns'], costs[0]['durationMs']),
                         ('claude-sonnet-5-5', 'medium', 3, 61000))
        self.assertRegex(result.stdout, r'Ticket graph: .* · [\d,]+ tokens spent')
        self.assertIn('Usage of epic #207 so far: 480 tokens', result.stdout)
        self.assertNotIn('$', result.stdout)
        self.assertNotIn('costUsd', json.dumps(self.state()))
        self.assertNotIn(['issue', 'comment', '207'], [c[:3] for c in self.gh_calls()])  # --post-costs is off

    def test_post_costs_comments_tokens_on_the_epic_but_never_the_pull_request(self):
        result = self.run_claude('--post-costs')
        self.assertEqual(result.returncode, 0, result.stderr)
        closes = {c[2]: c[c.index('--comment') + 1] for c in self.gh_calls() if c[:2] == ['issue', 'close']}
        self.assertNotIn('Cost', closes['209'])
        self.assertEqual(len([c for c in self.gh_calls() if c[:3] == ['issue', 'comment', '207']]), 1)
        table = (self.root / '.scratch/run-tickets-commandcode/207/costs.md').read_text()
        self.assertIn('| implement | 2 | 240 |', table)
        self.assertNotIn('$', table)
        self.assertNotIn('Usage of', self.prs()[0]['body'])

    def test_budget_variables_are_ignored_and_never_reach_a_session(self):
        self.env['IMPL_MAX_BUDGET_USD'] = 'not-a-number'
        result = self.run_claude()
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in self.calls():
            self.assertNotIn('--max-budget-usd', call)

    def test_transient_and_empty_failures_keep_the_ticket_retryable(self):
        for mode, status in (('429', 'provider-error'), ('empty', 'no-output')):
            with self.subTest(mode=mode):
                self.setUp()
                result = self.run_claude(mode=mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.state()['209']['status'], status)
                self.assertEqual(len(self.calls()), 1)

    def test_account_failures_stop_the_whole_run(self):
        for mode, message in (('usage-limit', 'usage limit was reached. Reset time: '),
                              ('usage-words', 'usage limit was reached. Wait for it to reset, then --resume'),
                              ('signed-out', 'run `claude auth login`, then --resume')):
            with self.subTest(mode=mode):
                self.setUp()
                result = self.run_claude(mode=mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
                self.assertEqual(len(self.calls()), 1)
                self.assertIn(self.state()['209']['status'], ('provider-error', 'interrupted'))

    def test_an_account_failure_in_one_ticket_stops_the_others(self):
        self.issues[1]['body'] = 'Parent #207'  # independent, so both run at once
        started = time.monotonic()
        result = self.run_claude(mode='fatal-parallel')
        self.assertEqual(result.returncode, 1)
        self.assertLess(time.monotonic() - started, 25)
        self.assertIn('usage limit was reached', result.stderr)
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

    def test_claude_md_is_protected_with_the_tracked_claude_dir(self):
        self.claude_files()
        result = self.run_claude(mode='operator-claude-dir')
        self.assertEqual(result.returncode, 0, result.stderr)
        implement = self.calls()[0]
        self.assertNotIn('--append-system-prompt-file', implement)  # Claude Code discovers it itself
        self.assertIn('CLAUDE.md, .claude/', self.prompt(implement))
        for mode, expected in (('edit-claude-md', 'CLAUDE.md'), ('edit-claude-settings', '.claude/settings.json'),
                               ('edit-nested-claude-md', 'src/CLAUDE.md')):
            with self.subTest(mode=mode):
                self.setUp()
                self.claude_files()
                result = self.run_claude(mode=mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn('Agent modified', result.stderr)
                self.assertIn(expected, result.stderr)

    def test_a_ticket_touching_claude_files_is_not_integrated(self):
        for mode, expected in (('commit-claude-md', 'CLAUDE.md'), ('commit-claude-dir', '.claude/agents/new.md'),
                               ('commit-nested-claude-md', 'src/CLAUDE.md'), ('commit-local-claude-md', 'CLAUDE.local.md')):
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
        for auth, message in (('{"loggedIn": false, "authMethod": "none"}', 'not logged in: run `claude auth login`'),
                              ('{"loggedIn": true, "authMethod": "console"}', 'not a claude.ai subscription'),
                              ('oops', 'could not read the Claude Code login')):
            self.env['FAKE_AUTH'] = auth
            for args in (('--dry-run',), ()):
                with self.subTest(auth=auth, args=args):
                    result = self.run_claude(*args)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(message, result.stderr)
        self.env.pop('FAKE_AUTH')
        self.env['CLAUDE_BIN'] = str(self.root / 'no-such-claude')
        self.env['COMMANDCODE_BIN'] = str(self.root / 'no-such-command-code')
        result = self.run_claude()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Claude Code binary not found', result.stderr)
        self.assertNotIn('CommandCode binary', result.stderr)
        self.assertFalse((self.root / 'agents.log').exists())

    def test_dry_run_names_the_backend_and_every_stage_default(self):
        result = self.run_claude('--dry-run', '--workflow', 'osmani', '--skip-plan-check')
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in ('Backend: claude, your claude.ai subscription',
                     'implement     claude-sonnet-5-5 @ medium, 500 turns',
                     'ticket review claude-sonnet-5-5 @ high, 500 turns',
                     'epic review   claude-opus-5-5 @ medium, 500 turns',
                     'verify        claude-sonnet-5-5 @ medium, 500 turns',
                     'launch plan   claude-haiku-5-5 @ low, 500 turns',
                     'escalate      claude-opus-5-5 @ medium, 500 turns',
                     'epic review  claude-opus-5-5, self-checked'):
            self.assertIn(line, result.stdout)
        self.assertIn('ticket review shares claude-sonnet-5-5 with the implementer', result.stderr)
        self.assertNotIn('$', result.stdout)
        result = self.run_claude('--dry-run', '--launch-plan-model', 'sonnet')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('launch plan (claude-sonnet-5-5)', result.stdout)
        self.assertFalse((self.root / '.scratch').exists())

    def test_a_session_on_anything_but_the_subscription_stops_the_run_before_it_spends(self):
        result = self.run_claude(mode='api-key')
        self.assertEqual(result.returncode, 1)
        self.assertIn("authenticated with 'ANTHROPIC_API_KEY', not the subscription login", result.stdout + result.stderr)
        time.sleep(0.5)
        self.assertFalse((self.bin / 'spent-on-api-key').exists())
        self.assertEqual(self.state()['209']['status'], 'interrupted')  # --resume once the login is fixed

    def test_later_runs_reuse_the_saved_backend_and_leave_it_only_when_one_is_named(self):
        self.assertEqual(self.run_claude('--only', '209').returncode, 0)
        result = self.run_script('--dry-run')  # no --backend: the epic's own
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Backend: claude, your claude.ai subscription', result.stdout)
        self.env['BACKEND'] = 'commandcode'
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.state()['_meta']['backend'], 'commandcode')

    def saved_on_the_removed_backend(self, **record):
        folder = self.root / '.scratch/run-tickets-commandcode/207'
        folder.mkdir(parents=True)
        (folder / 'state.json').write_text(json.dumps({
            '_meta': {'branch': self.git('branch', '--show-current'), 'epicBase': self.git('rev-parse', 'HEAD'),
                      'workflow': 'pocock', 'backend': 'claude-api'}, **record}))

    def test_the_removed_backend_is_refused_by_name(self):
        result = self.run_script('--backend', 'claude-api', '--dry-run')
        self.assertEqual(result.returncode, 2)
        self.assertIn('the claude-api backend was removed; use --backend claude', result.stderr)

    def test_an_epic_on_the_removed_backend_moves_only_when_a_backend_is_named(self):
        self.saved_on_the_removed_backend()
        result = self.run_script('--dry-run')  # its account changes, so the operator has to say so
        self.assertEqual(result.returncode, 2)
        self.assertIn('ran on the removed claude-api backend, billed to a company API key; pass --backend claude',
                      result.stderr)
        result = self.run_claude()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()['_meta']['backend'], 'claude')

    def test_a_part_way_ticket_on_the_removed_backend_starts_a_fresh_session(self):
        self.assertEqual(self.run_claude('--only', '209', mode='max-turns').returncode, 1)
        path = self.root / '.scratch/run-tickets-commandcode/207/state.json'
        state = json.loads(path.read_text())
        state['_meta']['backend'] = 'claude-api'
        state['209']['backend'] = 'claude-api'
        path.write_text(json.dumps(state))
        result = self.run_claude('--resume', '--only', '209')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('--resume', self.calls()[-1])  # the old config directory's session cannot be resumed
        self.assertEqual(self.state()['209']['status'], 'done')

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
        self.assertEqual(self.state()['_meta']['backend'], 'claude')
        self.assertEqual(self.state()['_meta']['workflow'], 'osmani')
        self.assertEqual(self.model(self.call('epic review')), 'claude-opus-5-5')

    def test_osmani_ticket_review_runs_on_sonnet_at_high(self):
        self.plan_osmani()
        result = self.run_claude('--workflow', 'osmani')
        self.assertEqual(result.returncode, 0, result.stderr)
        review = self.call('ticket review')
        self.assertEqual((self.model(review), review[review.index('--effort') + 1]), ('claude-sonnet-5-5', 'high'))
        self.assertIn('✓ #209 ticket review · 120 tokens', result.stdout)

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
        self.assertIn('--strict-mcp-config', verifier)


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
