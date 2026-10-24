"""Offline subscription backend checks: temporary repositories, fake CLI and fake operator home."""

import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import test_ticket_runner as base


CODEX = """import json, os, re, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
prompt = args[-1]
root = Path(os.environ['ROOT'])
resumed = args[-2] if args[1:2] == ['resume'] else None
ticket = Path.cwd().name
session = resumed or ('thread-' + ticket)
mode = os.environ.get('MODE', 'success')
with open(os.environ['AGENT_LOG'], 'a') as f:
    __import__('fcntl').flock(f, 2)
    f.write(json.dumps(args) + '\\n')
with open(os.environ['ENV_LOG'], 'a') as f:
    __import__('fcntl').flock(f, 2)
    f.write(json.dumps({k:v for k,v in os.environ.items() if k.startswith(('OPENAI_', 'CODEX_'))}) + '\\n')
home = Path(os.environ['CODEX_HOME'])
assert (home / 'auth.json').is_symlink()
assert (home / 'auth.json').resolve() == Path(os.environ['OPERATOR_AUTH'])
def emit(frame): print(json.dumps(frame), flush=True)
def done(text):
    emit({'type':'item.completed', 'item':{'id':'message', 'type':'agent_message', 'text':text}})
    emit({'type':'turn.completed', 'usage':{'input_tokens':100, 'cached_input_tokens':40, 'output_tokens':20}})
    sys.exit(0)
def fail(error):
    emit({'type':'error', **error})
    emit({'type':'turn.failed', 'error':error})
    sys.exit(1)
emit({'type':'thread.started', 'thread_id':session})
emit({'type':'turn.started'})
if 'You are an epic reviewer' in prompt: done('## FINDINGS\\nNone.\\nVERDICT=SHIP')
if 'Self-check' in prompt or 'Cross-examination' in prompt: done('## ANSWERS\\nCHECK=DONE')
if 'You are the ticket reviewer' in prompt: done('## BLOCKING\\nNone.\\n## DEFERRED\\nNone.\\nVERDICT=PASS')
if 'You write the launch plan' in prompt: done('## Pre-launch checklist\\n- [x] ok\\n## Rollback plan\\n- Revert')
if 'You are the verifier' in prompt:
    checks = re.findall(r'^- (C\\d+):', Path(re.search(r'(\\S+/checks.md)', prompt)[1]).read_text(), re.M)
    done('## RESULTS\\n' + '\\n'.join('VERIFIED %s ok' % c for c in checks) + '\\nVERIFY=PASS')
if mode == 'fatal-parallel':
    (root / 'fake-bin' / ('started-' + ticket)).write_text('started')
    if ticket == '210': time.sleep(30)
    deadline = time.monotonic() + 10
    while not (root / 'fake-bin/started-210').exists() and time.monotonic() < deadline: time.sleep(.02)
    fail({'message':'You’ve hit your usage limit. Try again at 14:00.'})
if mode == 'signed-out': fail({'message':'Your access token could not be refreshed because your refresh token was revoked.'})
if mode == 'transient' and ticket == '209': fail({'message':'Request failed: 503 Service Unavailable'})
if mode == 'worktree-protected':
    (root / 'fake-bin' / ('started-' + ticket)).write_text('started')
    if ticket == '210': time.sleep(3)
    else:
        deadline = time.monotonic() + 10
        while not (root / 'fake-bin/started-210').exists() and time.monotonic() < deadline: time.sleep(.02)
        Path('.codex').mkdir(exist_ok=True)
        Path('.codex/config.toml').write_text('injected')
        emit({'type':'item.completed', 'item':{'id':'inject', 'type':'command_execution',
              'command':'write ignored config', 'status':'completed', 'exit_code':0}})
if mode == 'max-turns' and ticket == '209' and not resumed:
    for i in range(100):
        emit({'type':'item.completed', 'item':{'id':str(i), 'type':'command_execution',
              'command':'pwd', 'status':'completed', 'exit_code':0}})
        time.sleep(.1)
    (root / 'fake-bin/escaped-cap').write_text('bad')
if mode.startswith('edit:'):
    path = root / mode.split(':', 1)[1]
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text('injected')
if mode.startswith('commit:'):
    path = Path(mode.split(':', 1)[1]); path.parent.mkdir(parents=True, exist_ok=True); path.write_text('injected')
    subprocess.run(['git', 'add', str(path)], check=True)
    subprocess.run(['git', 'commit', '-qm', 'Change instructions'], check=True)
Path('work.txt').write_text(ticket + ' done')
subprocess.run(['git','add','work.txt'],check=True)
subprocess.run(['git','commit','-qm','Implement ticket'],check=True)
done('The usage limit and rate limit tests pass.\\nSTATUS=COMPLETE')
"""


class CodexBackendTests(base.RunnerTests):
    def setUp(self):
        super().setUp()
        self.fake('codex', CODEX)
        self.operator = (self.bin / 'operator').resolve()
        self.operator.mkdir()
        self.auth = self.operator / 'auth.json'
        self.auth.write_text('{"auth_mode":"chatgpt","tokens":{"access_token":"fake-secret"}}')
        self.env.update(CODEX_BIN=str(self.bin / 'codex'), CODEX_HOME=str(self.operator),
                        OPERATOR_AUTH=str(self.auth), ENV_LOG=str(self.root / 'env.log'))
        self.env.pop('BACKEND', None)

    def run_codex(self, *args, mode='success'):
        return self.run_script(*args, mode=mode, script='ticket-runner-codex')

    def test_preflight_defaults_and_same_model_warning(self):
        self.plan_osmani()
        result = self.run_codex('--dry-run', '--workflow', 'osmani')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(f'Backend: codex, ChatGPT login from {self.auth}', result.stdout)
        for text in ('implement     gpt-6-luna @ high', 'ticket review gpt-6-luna @ high',
                     'epic review   gpt-6.1-sol @ high', 'launch plan   gpt-6-luna @ low',
                     'escalate      gpt-6.1-sol @ xhigh'):
            self.assertIn(text, result.stdout)
        self.assertIn('ticket review shares gpt-6-luna with the implementer', result.stderr)

    def test_every_stage_uses_the_isolated_home_and_tokens_only(self):
        self.plan_osmani()
        for name in ('OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID',
                     'OPENAI_ORGANIZATION', 'OPENAI_PROJECT', 'CODEX_ACCESS_TOKEN', 'CODEX_REFRESH_TOKEN_URL_OVERRIDE'):
            self.env[name] = 'must-not-leak'
        self.env['IMPL_MAX_BUDGET_USD'] = 'invalid-ignored'
        gh = (self.bin / 'gh').read_text()
        (self.bin / 'gh').write_text(gh.replace('args=sys.argv[1:]',
            "assert not any(k.startswith(('OPENAI_', 'CODEX_')) for k in os.environ)\nargs=sys.argv[1:]"))
        self.configure(lockfiles=['work.txt'],
                       install='python3 -c "import os; assert not any(k.startswith((\'OPENAI_\', \'CODEX_\')) for k in os.environ); print(\'INSTALL_ENV_CLEAN\')"',
                       gate=['python3 -c "import os; assert not any(k.startswith((\'OPENAI_\', \'CODEX_\')) for k in os.environ)"'])
        result = self.run_codex('--workflow', 'osmani', '--post-costs')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(result.stdout.count('INSTALL_ENV_CLEAN\n'), 2)
        calls = self.calls()
        models = [c[c.index('-m') + 1] for c in calls]
        self.assertEqual(models.count('gpt-6.1-sol'), 1)
        self.assertTrue(all(m in ('gpt-6-luna', 'gpt-6.1-sol') for m in models))
        for line in (self.root / 'env.log').read_text().splitlines():
            self.assertEqual(json.loads(line), {'CODEX_HOME': str((self.root / '.scratch/codex-home').resolve())})
        state = json.dumps(self.state())
        self.assertNotIn('costUsd', state)
        self.assertNotIn('fake-secret', state)
        self.assertIn('inputTokens', state)
        self.assertNotIn('$', result.stdout + result.stderr)
        table = self.root / '.scratch/run-tickets-commandcode/207/costs.md'
        self.assertIn('Tokens', table.read_text())
        self.assertNotIn('$', table.read_text())
        self.assertEqual(self.state()['209']['backend'], 'codex')

    def test_turn_cap_kills_the_session_and_resume_uses_its_thread(self):
        result = self.run_codex('--max-turns', '2', '--no-epic-review', mode='max-turns')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.state()['209']['status'], 'turn-cap')
        self.assertEqual(self.state()['209']['sessionId'], 'thread-209')
        self.assertEqual(self.state()['209']['costs'][0]['numTurns'], 2)
        self.assertFalse((self.bin / 'escaped-cap').exists())
        result = self.run_codex('--resume', '--no-epic-review', mode='max-turns')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        resumed = [c for c in self.calls() if c[1:2] == ['resume']]
        self.assertEqual(resumed[0][-2], 'thread-209')

    def test_main_instruction_changes_stop_the_run(self):
        (self.root / 'src').mkdir()
        (self.root / 'src/AGENTS.md').write_text('original')
        self.git('add', 'src/AGENTS.md')
        self.git('commit', '-qm', 'Add nested instructions')
        for path in ('src/AGENTS.md', 'src/AGENTS.override.md', 'AGENTS.override.md', '.codex/config.toml', '.agents/new.md'):
            with self.subTest(path=path):
                result = self.run_codex('--only', '209', mode='edit:' + path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Agent modified', result.stderr + result.stdout)
                if path == 'src/AGENTS.md':
                    (self.root / path).write_text('original')
                else:
                    (self.root / path).unlink()
                self.git('worktree', 'remove', '--force', str(self.root / '.scratch/worktrees/209'))
                self.git('branch', '-D', 'cc/207/209')
                state = self.root / '.scratch/run-tickets-commandcode/207/state.json'
                state.unlink()

    def test_instruction_diffs_never_integrate(self):
        before = self.git('rev-parse', 'HEAD')
        for path in ('src/AGENTS.md', 'src/AGENTS.override.md', 'AGENTS.override.md', '.codex/config.toml', '.agents/new.md'):
            with self.subTest(path=path):
                result = self.run_codex('--only', '209', mode='commit:' + path)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.git('rev-parse', 'HEAD'), before)
                self.git('worktree', 'remove', '--force', str(self.root / '.scratch/worktrees/209'))
                self.git('branch', '-D', 'cc/207/209')
                (self.root / '.scratch/run-tickets-commandcode/207/state.json').unlink()

    def test_ignored_instruction_changes_in_a_session_stop_parallel_tickets(self):
        self.issues[1]['body'] = 'Parent #207'
        with (self.root / '.gitignore').open('a') as file:
            file.write('.codex/\n')
        self.git('add', '.gitignore')
        self.git('commit', '-qm', 'Ignore local config')
        result = self.run_codex('--no-epic-review', mode='worktree-protected')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Agent modified', result.stderr + result.stdout)
        self.assertEqual(self.state()['210']['status'], 'interrupted')

    def test_preflight_refuses_wrong_models_missing_binary_and_wrong_login(self):
        for args in (('--model', 'glm'), ('--verify-model', 'claude-opus-5-5'),
                     ('--launch-plan-model', 'bad'), ('--ticket-review-model', 'sol,flash')):
            result = self.run_codex('--dry-run', *args)
            self.assertEqual(result.returncode, 1)
            self.assertIn('not a gpt-* model', result.stderr)
        self.env['CODEX_BIN'] = str(self.bin / 'missing')
        result = self.run_codex('--dry-run')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Codex binary not found', result.stderr)
        self.env['CODEX_BIN'] = str(self.bin / 'codex')
        for contents in (None, '{}', '{"auth_mode":"apikey"}', 'invalid json'):
            if contents is None:
                self.auth.unlink()
            else:
                self.auth.write_text(contents)
            result = self.run_codex('--dry-run')
            self.assertEqual(result.returncode, 1)
            self.assertIn('ChatGPT login required', result.stderr)
        self.assertFalse((self.root / 'agents.log').exists())

    def test_usage_limit_stops_parallel_tickets_and_signed_out_gives_login_hint(self):
        self.issues[1]['body'] = 'Parent #207'
        result = self.run_codex(mode='fatal-parallel')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ChatGPT usage limit was reached', result.stderr)
        self.assertIn('14:00', result.stderr)
        self.assertIn('then --resume', result.stderr)
        self.assertEqual(self.state()['210']['status'], 'interrupted')
        result = self.run_codex('--resume', mode='signed-out')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('run `codex login`, then --resume', result.stderr)

    def test_transient_failure_leaves_other_tickets_runnable(self):
        self.issues[1]['body'] = 'Parent #207'
        result = self.run_codex('--no-epic-review', mode='transient')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['209']['status'], 'provider-error')
        self.assertEqual(self.state()['210']['status'], 'done')

    def test_saved_backend_cannot_be_switched_mid_ticket_or_left_implicitly(self):
        result = self.run_codex('--max-turns', '1', mode='max-turns')
        self.assertEqual(result.returncode, 1)
        result = self.run_script('--backend', 'commandcode', '--resume')
        self.assertIn('part-way through', result.stderr)
        self.assertEqual(self.run_codex('--resume', '--only', '209', '--no-epic-review').returncode, 0)
        result = self.run_script('--only', '209')
        self.assertIn('runs on the codex backend; pass --backend codex', result.stderr)

    def test_verifier_gets_browser_override_and_default_model(self):
        self.configure(serve='python3 -c ' + __import__('shlex').quote(base.SERVE),
                       serveEnv='verify.env', ready=base.READY, browser=True, gate=['true'])
        (self.root / 'verify.env').write_text(f'VERIFY_TOKEN=s3cret-token\nPORT={base.free_port()}\n')
        self.git('add', 'verify.env')
        self.git('commit', '-qm', 'Configure runtime')
        result = self.run_codex()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        verifier = next(c for c in calls if 'You are the verifier' in c[-1])
        self.assertEqual(verifier[verifier.index('-m') + 1], 'gpt-6-luna')
        override = 'mcp_servers.chrome-devtools.command="npx"'
        self.assertIn(override, verifier)
        self.assertTrue(all(override not in c for c in calls if c is not verifier))



for _name in dir(base.RunnerTests):
    if _name.startswith('test_') and _name not in CodexBackendTests.__dict__:
        setattr(CodexBackendTests, _name, None)


if __name__ == '__main__':
    unittest.main()
