"""Offline integration checks: real temporary git repo, fake gh and CommandCode."""

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[1]
OSMANI_SKILLS = ("incremental-implementation", "test-driven-development", "code-simplification",
                 "security-and-hardening", "api-and-interface-design", "debugging-and-error-recovery",
                 "performance-optimization", "doubt-driven-development", "code-review-and-quality",
                 "git-workflow-and-versioning", "frontend-ui-engineering", "observability-and-instrumentation",
                 "documentation-and-adrs", "deprecation-and-migration", "ci-cd-and-automation",
                 "browser-testing-with-devtools", "shipping-and-launch")
# A tiny app for the runtime checks: it refuses to start without the serveEnv token.
SERVE = """import http.server, os
assert os.environ.get('VERIFY_TOKEN') == 's3cret-token', 'serveEnv not loaded'
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'hello from the app')
    def log_message(self, *args):
        pass
http.server.HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
"""
READY = "python3 -c \"import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['PORT'])\""


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def planned(body, blocked_by=""):
    """An Osmani-planned sub-ticket in the shared ticket template."""
    return (f"## Parent\n#207\n\n## What to build\n{body}\n\n## Acceptance criteria\n- [ ] It works\n\n"
            f"## Verification\n- [ ] Tests: npm test\n\n## Files likely touched\n- src/a.ts\n\n## Scope\nS\n"
            + (f"\n## Blocked by\n- {blocked_by}\n" if blocked_by else ""))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "README.md").write_text("Project\n")
        self.git("init", "-b", "test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        self.bin = self.root / "fake-bin"
        self.bin.mkdir()
        self.issues = [{"number": 209, "title": "First slice", "body": "Parent #207", "state": "OPEN"},
                       {"number": 210, "title": "Second slice", "body": "Parent #207\n## Blocked by\n#209", "state": "OPEN"}]
        self.fake("gh", """import json, os, re, sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as f: f.write(json.dumps(args)+'\\n')
none={'totalCount':0, 'nodes':[]}
if args[:2] == ['issue', 'list']:
    if os.environ.get('DISCOVERY_DELAY'):
        Path(os.environ['ROOT'], 'discovery.log').write_text('started')
        __import__('time').sleep(float(os.environ['DISCOVERY_DELAY']))
    print(os.environ['ISSUES'])
elif args[:2] == ['issue', 'view'] and any('body' in a for a in args):
    print(json.dumps({'title': 'Epic title', 'body': os.environ.get('PARENT_BODY', '')}))
elif args[:2] == ['issue', 'view']: print('CLOSED')
elif args[:2] == ['repo', 'view']: print('main')
elif args[0] == 'pr':
    store = Path(os.environ['PR_STATE'])
    prs = json.loads(store.read_text()) if store.exists() else []
    opt = lambda name: args[args.index(name)+1]
    if args[1] == 'list':
        print(json.dumps([{'number': p['number'], 'url': p['url']} for p in prs
                          if p['head'] == opt('--head') and p['state'] == 'OPEN']))
    elif args[1] == 'view':
        p = next(p for p in prs if p['number'] == int(args[2]))
        print(json.dumps({'state': p['state'], 'headRefOid': p['oid'], 'headRefName': p['head']}))
    elif args[1] == 'create':
        n = 300 + len(prs)
        prs.append({'number': n, 'url': 'https://github.com/acme/engine/pull/%d' % n, 'head': opt('--head'),
                    'base': opt('--base'), 'title': opt('--title'), 'body': Path(opt('--body-file')).read_text(),
                    'state': 'OPEN', 'oid': __import__('subprocess').check_output(['git','rev-parse','HEAD'], text=True).strip()})
        print(prs[-1]['url'])
    elif args[1] == 'edit':
        p = next(p for p in prs if p['number'] == int(args[2]))
        p['body'] = Path(opt('--body-file')).read_text()
        p['oid'] = __import__('subprocess').check_output(['git','rev-parse','HEAD'], text=True).strip()
    elif args[1] == 'checks':
        sys.exit(int(os.environ.get('CHECKS_RC', '0')))
    elif args[1] == 'merge':
        next(p for p in prs if p['number'] == int(args[2]))['state'] = 'MERGED'
    store.write_text(json.dumps(prs))
elif args[:2] == ['api', 'graphql']:
    query=next(a for a in args if a.startswith('query='))
    repo={'nameWithOwner':'acme/engine', 'parent':{'subIssues':none}}
    repo.update({alias:{'blockedBy':none} for alias in re.findall(r'(i\\d+): issue', query)})
    print(json.dumps({'data':{'repository':repo}}))
""")
        self.fake("command-code", """import json, os, subprocess, sys
from pathlib import Path
args=sys.argv[1:]
if args[:2] == ['mcp', 'list']:
    print(os.environ.get('MCP_LIST', 'No MCP servers configured'))
    sys.exit(0)
with open(os.environ['AGENT_LOG'], 'a') as f: f.write(json.dumps(args)+'\\n')
prompt=args[args.index('-p')+1]
review='You are the final reviewer' in prompt
ticket_review='You are the ticket reviewer' in prompt
mode=os.environ.get('MODE', 'success')
cap=mode in ('cap', 'cap-commit') and '--resume' not in args
def done(text, session, subtype='success', rc=0):
    print(json.dumps({'type':'result', 'subtype':subtype, 'sessionId':session, 'finalText':text}))
    sys.exit(rc)
def commit(name, text):
    Path(name).write_text(text)
    subprocess.run(['git', 'add', name], check=True)
    subprocess.run(['git', 'commit', '-qm', 'Change ' + name], check=True)
if 'You are the verifier' in prompt:
    import urllib.request
    port = os.environ.get('TEST_PORT')
    seen = urllib.request.urlopen('http://127.0.0.1:' + port).read().decode().strip() if port else 'no app'
    if mode == 'verifier-edit': Path('work.txt').write_text('verifier edit')
    if mode == 'verifier-new-file': Path('new-runtime-output.txt').write_text('created during checks')
    if mode == 'verifier-ignored-file': Path('runtime-output.log').write_text('created during checks')
    if mode == 'verifier-cap' and '--resume' not in args: done('', 'verify-session', 'max_turns', 8)
    fail = mode in ('verify-fail', 'verify-fail-always') and (mode == 'verify-fail-always' or not Path('fixed.txt').exists())
    import re
    path = re.search(r'(\S+/checks.md)', prompt)[1]
    ids = re.findall(r'^- (C\d+):', Path(path).read_text(), re.M)
    rows = [('FAILED' if fail else 'VERIFIED') + ' ' + ids[0] + ' GET / answered ' + seen]
    rows += ['UNVERIFIABLE ' + cid + ' needs production data' for cid in ids[1:]]
    if mode == 'verifier-contradictory': rows[0] = rows[0].replace('VERIFIED', 'FAILED')
    if mode == 'verifier-missing': rows = []
    done('## RESULTS\\n' + '\\n'.join(rows) + '\\n' + ('VERIFY=FAIL' if fail else 'VERIFY=PASS'), 'verify-session')
if 'You write the launch plan' in prompt:
    if mode == 'plan-cap' and '--resume' not in args: done('', 'plan-session', 'max_turns', 8)
    done('## Pre-launch checklist\\n- [x] Tests pass (verify report)\\n## Residual risk\\n- UNVERIFIABLE: needs '
         'production data\\n## Rollback plan\\n- Revert the merge commit', 'plan-session')
if 'verify stage failed' in prompt:
    if mode == 'repair-cap' and '--resume' not in args: done('', 'repair-session', 'max_turns', 8)
    if mode == 'repair-protected': commit('AGENTS.md', 'changed by the repair')
    commit('other.txt' if mode == 'repair-noop' else 'fixed.txt', 'fixed')
    done('STATUS=NEEDS_HUMAN' if mode == 'repair-human' else 'STATUS=COMPLETE', 'repair-session')
if ticket_review:
    if mode == 'review-edit': Path('work.txt').write_text('reviewer edit')
    if mode == 'review-cap' and '--resume' not in args:
        print(json.dumps({'type':'result', 'subtype':'max_turns', 'sessionId':'ticket-review-session', 'finalText':''}))
        sys.exit(8)
    fail=mode == 'review-fail-always' or (mode == 'review-fail-once' and 'after one fix round' not in prompt)
    print(json.dumps({'type':'result', 'subtype':'success', 'sessionId':'ticket-review-session',
                      'finalText':'## BLOCKING\\n' + ('- a.ts:1 wrong' if fail else 'None.') + '\\n## DEFERRED\\n'
                                   '- old bug in b.ts:2\\n' + ('VERDICT=FAIL' if fail else 'VERDICT=PASS')}))
    sys.exit(0)
if not review and mode in ('parallel', 'parallel-conflict'):
    if 'stopped with conflicts' in prompt:
        Path('shared.txt').write_text('209 and 210\\n')
        subprocess.run(['git', 'add', 'shared.txt'], check=True)
        subprocess.run(['git', '-c', 'core.editor=true', 'rebase', '--continue'], check=True)
        done('STATUS=COMPLETE', 'ticket-session')
    number = Path.cwd().name
    import time
    root = Path(os.environ['ROOT'])
    (root / 'fake-bin' / ('parallel-ready-' + number)).write_text('ready')
    deadline = time.monotonic() + 10
    while not all((root / 'fake-bin' / ('parallel-ready-' + n)).exists() for n in ('209', '210')):
        if time.monotonic() >= deadline: raise RuntimeError('parallel start barrier timed out')
        time.sleep(.02)
    if number == '210':
        state = root / '.scratch/run-tickets-commandcode/207/state.json'
        while json.loads(state.read_text()).get('209', {}).get('status') != 'done':
            if time.monotonic() >= deadline: raise RuntimeError('first integration timed out')
            time.sleep(.02)
    commit('shared.txt' if mode == 'parallel-conflict' else 'ticket-' + number + '.txt', number)
    done('STATUS=COMPLETE', 'ticket-session')
if not review:
    if os.environ.get('PROBE'):
        with open(os.environ['PROBE'], 'a') as f:
            f.write(json.dumps({'link': os.path.islink('local.cfg'), 'deps': os.path.exists('deps/lib.txt')}) + '\\n')
    if mode == 'edit-config': Path(os.environ['ROOT'], '.agents/ticket-runner.json').write_text('{}')
    if mode == 'commit-config':
        Path('.agents/ticket-runner.json').write_text('{"gate": ["false"]}')
        subprocess.run(['git', 'add', '.agents/ticket-runner.json'], check=True)
    Path('work.txt').write_text('partial' if cap else str(len(Path(os.environ['AGENT_LOG']).read_text())))
    if not cap or mode == 'cap-commit':
        subprocess.run(['git', 'add', 'work.txt'], check=True)
        subprocess.run(['git', 'commit', '-qm', 'Implement ticket'], check=True)
if mode == 'empty': sys.exit(0)
if mode == 'error':
    print(json.dumps({'type':'result', 'subtype':'error', 'finalText':'STATUS=COMPLETE'}))
    sys.exit(6)
text=('## DEFERRED\\n- old bug in b.ts:2\\n' + ('VERDICT=FIX-FIRST' if mode == 'fix' else 'VERDICT=SHIP')) if review else 'STATUS=COMPLETE'
if mode == 'missing': text='Finished the work'
if mode == 'human' and not review: text='STATUS=NEEDS_HUMAN'
print(json.dumps({'type':'event', 'event':{'type':'tool_running'}}))
print(json.dumps({'type':'result', 'subtype':'max_turns' if cap else 'success',
                  'sessionId':'review-session' if review else 'ticket-session', 'finalText':text}))
sys.exit(8 if cap else 0)
""")
        skill = self.root / "skill.md"
        skill.write_text("Implement the ticket.")
        self.skills = self.root / "skills"
        for name in OSMANI_SKILLS:
            (self.skills / name).mkdir(parents=True)
            (self.skills / name / "SKILL.md").write_text(f"# {name}\nSee ../../references/definition-of-done.md\n")
        (self.root / "references").mkdir()
        (self.root / "references" / "definition-of-done.md").write_text("Done means done.\n")
        (self.root / ".gitignore").write_text("fake-bin/\n*.log\nskill.md\nskills/\nreferences/\nprs.json\n")
        (self.root / "AGENTS.md").write_text("# Agents\n")
        self.git("add", ".gitignore", "AGENTS.md")
        self.git("commit", "-qm", "Test setup")
        remote = tempfile.TemporaryDirectory()
        self.addCleanup(remote.cleanup)
        self.origin = Path(remote.name) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        self.git("remote", "add", "origin", str(self.origin))
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        COMMANDCODE_BIN=str(self.bin / "command-code"), IMPLEMENT_SKILL=str(skill),
                        AGENT_SKILLS_DIR=str(self.skills), PARENT_BODY="## Checkpoint: after #210\n- [ ] Tests pass",
                        CALL_LOG=str(self.root / "gh.log"), AGENT_LOG=str(self.root / "agents.log"),
                        ROOT=str(self.root), PR_STATE=str(self.root / "prs.json"))
        for name in ('IMPL_MODEL', 'IMPL_EFFORT', 'IMPL_MAX_TURNS', 'FINAL_MODEL', 'FINAL_EFFORT', 'FINAL_MAX_TURNS',
                     'WORKFLOW', 'TICKET_REVIEW_MODEL', 'TICKET_REVIEW_EFFORT', 'TICKET_REVIEW_MAX_TURNS',
                     'VERIFY_MODEL', 'VERIFY_EFFORT', 'VERIFY_MAX_TURNS', 'GATE_CMDS', 'INSTALL_CMD'):
            self.env.pop(name, None)
        self.configure(gate=['true'])

    def fake(self, name, source):
        path = self.bin / name
        path.write_text("#!/usr/bin/env python3\n" + source)
        path.chmod(0o755)

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True, stderr=subprocess.DEVNULL).strip()

    def run_script(self, *args, mode="success", script="ticket-runner"):
        env = dict(self.env, ISSUES=json.dumps(self.issues), MODE=mode)
        return subprocess.run([str(RUNNER / "bin" / script), "207", *args],
                              cwd=self.root, env=env, capture_output=True, text=True)

    def run_osmani(self, *args, mode="success"):
        return self.run_script(*args, mode=mode, script="ticket-runner-osmani")

    def plan_osmani(self):
        self.issues[0]["body"] = planned("First slice")
        self.issues[1]["body"] = planned("Second slice", blocked_by="#209")

    def configure(self, commit=True, **config):
        (self.root / ".agents").mkdir(exist_ok=True)
        config.setdefault('gate', ['true'])
        (self.root / ".agents/ticket-runner.json").write_text(json.dumps(config))
        if commit:
            self.git("add", ".agents/ticket-runner.json")
            if self.git("diff", "--cached", "--name-only"):
                self.git("commit", "-qm", "Configure the runner")

    def gh_calls(self):
        return [json.loads(line) for line in (self.root / "gh.log").read_text().splitlines()]

    @staticmethod
    def model(call):
        return call[call.index('-m')+1]

    @staticmethod
    def prompt(call):
        return call[call.index('-p')+1]

    def state(self):
        return json.loads((self.root / ".scratch/run-tickets-commandcode/207/state.json").read_text())

    def calls(self):
        return [json.loads(line) for line in (self.root / "agents.log").read_text().splitlines()]

    def kinds(self):
        return ['verifier' if 'You are the verifier' in self.prompt(c) else
                'repair' if 'verify stage failed' in self.prompt(c) else
                'launch plan' if 'You write the launch plan' in self.prompt(c) else
                'epic review' if 'You are the final reviewer' in self.prompt(c) else
                'ticket review' if 'You are the ticket reviewer' in self.prompt(c) else 'implement'
                for c in self.calls()]

    def call(self, kind, index=-1):
        return [c for c, k in zip(self.calls(), self.kinds()) if k == kind][index]

    def prs(self):
        return json.loads((self.root / "prs.json").read_text())

    def test_default_implements_in_order_then_sonnet_reviews(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(self.kinds(), ['implement', 'implement', 'epic review', 'launch plan'])
        self.assertEqual(calls[0][calls[0].index('-m')+1], 'deepseek/deepseek-v4.1-flash')
        self.assertEqual(self.model(self.call('epic review')), 'claude-sonnet-5-5')
        self.assertEqual(self.model(self.call('launch plan')), 'claude-sonnet-5-5')
        self.assertEqual(calls[0][calls[0].index('--max-turns')+1], '500')
        self.assertEqual(self.state()['210']['status'], 'done')
        self.assertEqual(self.state()['_review']['status'], 'ship')
        self.assertEqual(self.run_script('--resume').returncode, 0)
        self.assertEqual(len(self.calls()), 4)  # Every stage still holds for this head.

    def check_cap_resume(self, mode):
        failed = self.run_script(mode=mode)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(self.state()['209']['status'], 'turn-cap')
        self.assertTrue((self.root / '.scratch/worktrees/209/work.txt').exists())
        base = self.state()['209']['base']
        result = self.run_script('--resume', '--max-turns', '1000')
        self.assertEqual(result.returncode, 0, result.stderr)
        resumed = self.calls()[1]
        self.assertEqual(resumed[resumed.index('--resume')+1], 'ticket-session')
        self.assertEqual(resumed[resumed.index('--max-turns')+1], '1000')
        self.assertEqual(self.state()['209']['base'], base)

    def test_cap_preserves_work_and_resumes_exact_session(self):
        self.check_cap_resume('cap')

    def test_cap_after_commit_still_resumes_incomplete_ticket(self):
        self.check_cap_resume('cap-commit')

    def check_failure(self, mode, expected):
        result = self.run_script(mode=mode)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['209']['status'], expected)
        self.assertEqual(len(self.calls()), 1)

    def test_missing_verdict_halts_downstream(self):
        self.check_failure('missing', 'needs-human')

    def test_empty_output_halts_downstream(self):
        self.check_failure('empty', 'no-output')

    def test_provider_error_cannot_pass_even_with_complete_text(self):
        self.check_failure('error', 'provider-error')

    def test_model_aliases_select_supported_effort(self):
        for alias, model, effort in [('flash', 'deepseek/deepseek-v4.1-flash', 'max'),
                                     ('flash-fast', 'deepseek/deepseek-v4.1-flash-fast', 'max'),
                                     ('muse', 'meta/muse-spark-1.3-contributor', 'xhigh'),
                                     ('luna', 'gpt-6-luna', 'max'),
                                     ('glm', 'z-ai/glm-5.3-flash', 'max')]:
            with self.subTest(model=alias):
                result = self.run_script('--dry-run', '--model', alias)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'{model}/{effort}', result.stdout)
        self.assertEqual(self.run_script('--dry-run', '--model', 'muse', '--effort', 'max').returncode, 2)

    def test_dry_run_does_not_write_or_edit(self):
        result = self.run_script('--dry-run', '--model', 'glm')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('z-ai/glm-5.3-flash', result.stdout)
        self.assertFalse((self.root / '.scratch').exists())
        self.assertFalse((self.root / 'agents.log').exists())
        self.assertEqual(len((self.root / 'gh.log').read_text().splitlines()), 2)  # issue list + native links

    def test_dependency_cycle_fails_before_agents(self):
        self.issues[0]['body'] += '\n## Blocked by\n#210'
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Dependency cycle', result.stderr)
        self.assertFalse((self.root / 'agents.log').exists())

    def test_human_gate_defers_dependents_and_final_review(self):
        result = self.run_script(mode='human')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['209']['status'], 'awaiting-human')
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn('_review', self.state())

    def test_final_only_rejects_unfinished_epic_and_fix_first_fails(self):
        result = self.run_script('--final-only')
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / 'agents.log').exists())
        result = self.run_script(mode='fix')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['_review']['status'], 'fix-first')

    def test_dirty_tree_is_preserved_and_invalid_budget_rejected(self):
        (self.root / 'unrelated.txt').write_text('user work')
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Dirty main worktree', result.stderr)
        self.assertEqual((self.root / 'unrelated.txt').read_text(), 'user work')
        self.assertFalse((self.root / 'agents.log').exists())
        self.assertEqual(self.run_script('--max-turns', '0').returncode, 2)

    # ── osmani workflow ──────────────────────────────────────────────────

    def test_osmani_reviews_each_ticket_on_alternating_models_then_posts_deferred_once(self):
        self.plan_osmani()
        result = self.run_osmani()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(self.kinds(), ['implement', 'ticket review', 'implement', 'ticket review', 'epic review',
                                        'launch plan'])
        # Odd #209 gets the second model (glm), even #210 the first (luna); both review at high.
        self.assertEqual(self.model(calls[1]), 'z-ai/glm-5.3-flash')
        self.assertEqual(self.model(calls[3]), 'gpt-6-luna')
        self.assertEqual(calls[1][calls[1].index('--effort')+1], 'high')
        implement = self.prompt(calls[0])
        self.assertIn(str(self.skills / 'incremental-implementation' / 'SKILL.md'), implement)
        self.assertIn('Do not review your own work', implement)
        self.assertIn('ASSUMPTIONS', implement)
        for name in ('frontend-ui-engineering', 'observability-and-instrumentation', 'documentation-and-adrs',
                     'deprecation-and-migration', 'ci-cd-and-automation'):
            self.assertIn(str(self.skills / name / 'SKILL.md'), implement)
        self.assertIn('zombie code', implement)
        self.assertIn('outside\nthe files this ticket changes is DEFERRED', self.prompt(calls[1]))
        self.assertIn('old bug in b.ts:2', (self.root / '.scratch/run-tickets-commandcode/207/209/review/round-1/final.txt').read_text())
        self.assertIn('209/review/round-1/final.txt', self.prompt(self.call('epic review')))
        state = self.state()
        self.assertEqual(state['_meta']['workflow'], 'osmani')
        self.assertEqual(state['209']['ticketReview']['status'], 'pass')
        self.assertEqual(state['_review']['status'], 'ship')
        comments = [c for c in self.gh_calls() if c[:2] == ['issue', 'comment']]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0][2], '207')
        self.assertIn('old bug in b.ts:2', (self.root / '.scratch/run-tickets-commandcode/207/review/deferred.md').read_text())
        self.assertEqual(self.run_osmani('--final-only').returncode, 0)
        self.assertEqual(len([c for c in self.gh_calls() if c[:2] == ['issue', 'comment']]), 1)  # Same head: not reposted.

    def test_osmani_blocking_findings_get_one_fix_round_and_the_same_reviewer(self):
        self.plan_osmani()
        result = self.run_osmani(mode='review-fail-once')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()[:4]
        self.assertIn('You are the ticket reviewer', self.prompt(calls[1]))
        fix = calls[2]
        self.assertIn('found blocking problems', self.prompt(fix))
        self.assertEqual(fix[fix.index('--resume')+1], 'ticket-session')
        self.assertEqual(self.model(fix), 'deepseek/deepseek-v4.1-flash')
        self.assertIn('after one fix round', self.prompt(calls[3]))
        self.assertEqual(self.model(calls[3]), self.model(calls[1]))
        self.assertNotIn('--resume', calls[3])  # The re-review is a fresh session.
        self.assertEqual(self.state()['209']['status'], 'done')
        self.assertEqual(self.state()['209']['ticketReview']['round'], 2)

    def test_osmani_still_blocking_after_the_fix_round_holds_dependents(self):
        self.plan_osmani()
        result = self.run_osmani(mode='review-fail-always')
        self.assertEqual(result.returncode, 1)
        state = self.state()
        self.assertEqual(state['209']['status'], 'needs-human')
        self.assertIn('after one fix round', state['209']['reason'])
        self.assertNotIn('210', state)
        self.assertNotIn('_review', state)
        self.assertEqual(len(self.calls()), 4)  # implement, review, fix, re-review

    def test_osmani_interrupted_ticket_review_resumes_its_own_session(self):
        self.plan_osmani()
        result = self.run_osmani(mode='review-cap')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['209']['status'], 'turn-cap')
        self.assertEqual(self.state()['209']['phase'], 'review')
        result = self.run_osmani('--resume')
        self.assertEqual(result.returncode, 0, result.stderr)
        resumed = self.calls()[2]
        self.assertIn('You are the ticket reviewer', self.prompt(resumed))
        self.assertEqual(resumed[resumed.index('--resume')+1], 'ticket-review-session')
        self.assertEqual(self.model(resumed), self.model(self.calls()[1]))
        self.assertEqual(self.state()['209']['sessionId'], 'ticket-session')  # The implementer's session survives.

    def test_osmani_ticket_review_must_stay_read_only(self):
        self.plan_osmani()
        result = self.run_osmani(mode='review-edit')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['209']['status'], 'needs-human')
        self.assertIn('read-only', self.state()['209']['reason'])

    def test_osmani_plan_check_refuses_before_any_agent(self):
        self.issues[0]["body"] = "## Parent\n#207\n\n## Acceptance criteria\n- [ ] It works\n"
        self.issues[1]["body"] = planned("Second slice") + "\n**Dependencies:** Task 1\n"
        dry = self.run_osmani('--dry-run')
        self.assertEqual(dry.returncode, 1)
        self.assertIn('#209 has no `## Verification` section', dry.stderr)
        self.assertIn('#210 names dependencies without issue numbers', dry.stderr)
        self.assertFalse((self.root / '.scratch').exists())
        result = self.run_osmani()
        self.assertEqual(result.returncode, 1)
        self.assertIn('plan check refused', result.stderr)
        self.assertFalse((self.root / 'agents.log').exists())
        self.assertEqual(self.run_osmani('--dry-run', '--skip-plan-check').returncode, 0)

    def test_osmani_plan_check_catches_a_dependency_missing_from_blocked_by(self):
        self.plan_osmani()
        self.issues[1]["body"] = planned("Second slice") + "\nThis depends on #209.\n"
        result = self.run_osmani('--dry-run')
        self.assertEqual(result.returncode, 1)
        self.assertIn('#210 depends on #209 without a Blocked by entry', result.stderr)

    def test_osmani_refuses_while_a_linked_reference_is_missing(self):
        self.plan_osmani()
        (self.root / "references" / "definition-of-done.md").unlink()
        result = self.run_osmani()
        self.assertEqual(result.returncode, 1)
        self.assertIn('definition-of-done.md (linked from', result.stderr)
        self.assertIn('sparse-checkout set references', result.stderr)
        self.assertFalse((self.root / 'agents.log').exists())

    def test_osmani_refuses_while_a_new_implementer_skill_is_missing(self):
        self.plan_osmani()
        shutil.rmtree(self.skills / "deprecation-and-migration")
        result = self.run_osmani('--dry-run')
        self.assertEqual(result.returncode, 1)
        self.assertIn('deprecation-and-migration/SKILL.md', result.stderr)

    def test_workflow_switch_is_refused_while_a_ticket_is_part_way(self):
        self.plan_osmani()
        self.assertEqual(self.run_script(mode='human').returncode, 1)
        result = self.run_osmani('--resume')
        self.assertEqual(result.returncode, 1)
        self.assertIn('runs the pocock workflow and #209 is part-way through', result.stderr)
        self.assertEqual(self.state()['_meta']['workflow'], 'pocock')

    def test_workflow_switch_is_allowed_once_nothing_is_part_way(self):
        self.plan_osmani()
        self.assertEqual(self.run_script().returncode, 0)
        result = self.run_osmani('--final-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()['_meta']['workflow'], 'osmani')
        self.assertIn('Every ticket passed its own ticket review', self.prompt(self.call('epic review')))


    # ── project config + generic prompts ─────────────────────────────────

    def test_config_refuses_unknown_keys_bad_types_and_unsafe_paths(self):
        for config, expected in [({"gat": ["true"]}, "unknown key(s) gat"),
                                 ({"gate": "npm test"}, "gate must be a list of shell commands"),
                                 ({"mergeMethod": "fast"}, "mergeMethod must be one of merge, squash, rebase"),
                                 ({"linkFiles": ["../secrets"]}, "linkFiles must be a list of relative paths"),
                                 ({"readyTimeout": 0}, "readyTimeout must be a positive integer"),
                                 ({"serve": "npm start"}, "serve needs a ready command"),
                                 ({"browser": True}, "browser needs serve")]:
            with self.subTest(config=config):
                self.configure(commit=False, **config)
                result = self.run_script('--dry-run')
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected, result.stderr)
                self.assertFalse((self.root / 'agents.log').exists())

    def test_config_provisions_dependency_dirs_and_link_files(self):
        (self.root / "deps").mkdir()
        (self.root / "deps/lib.txt").write_text("lib")
        (self.root / "local.cfg").write_text("secret=1")
        with (self.root / ".gitignore").open("a") as f:
            f.write("deps/\nlocal.cfg\nprobe.jsonl\n")
        self.git("add", ".gitignore")
        self.configure(dependencyDirs=["deps"], linkFiles=["local.cfg"])
        self.env["PROBE"] = str(self.root / "probe.jsonl")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        probes = [json.loads(line) for line in (self.root / "probe.jsonl").read_text().splitlines()]
        self.assertEqual(probes, [{'link': True, 'deps': True}] * 2)

    def test_agent_changing_the_config_stops_the_run(self):
        self.configure(gate=["true"])
        result = self.run_script(mode='edit-config')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Agent modified', result.stderr)
        self.assertIn('ticket-runner.json', result.stderr)

    def test_ticket_touching_the_config_is_not_integrated(self):
        self.configure(gate=["true"])
        result = self.run_script(mode='commit-config')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['209']['status'], 'needs-human')
        self.assertIn('protected files (.agents/ticket-runner.json)', self.state()['209']['reason'])

    def test_prompts_are_project_agnostic_and_point_at_agents_md(self):
        self.plan_osmani()
        self.assertEqual(self.run_osmani().returncode, 0)
        prompts = [self.prompt(c) for c in self.calls()]
        for prompt in prompts:
            for word in ('BVN', 'bureau', 'statement', 'Mongo', '/v1', 'model-governance', 'PebbleScore'):
                self.assertNotIn(word, prompt)
        self.assertIn('AGENTS.md has no `## Ticket Runner` section, so use general judgement', prompts[0])
        (self.root / "AGENTS.md").write_text("# Agents\n\n## Ticket Runner\n- Personal data: emails\n")
        self.git("commit", "-qam", "Runner notes")
        self.issues.append({"number": 211, "title": "Third", "body": planned("Third slice"), "state": "OPEN"})
        self.assertEqual(self.run_osmani().returncode, 0)
        self.assertIn('The `## Ticket Runner` section of AGENTS.md lists', self.prompt(self.calls()[-3]))


    # ── verify stage ─────────────────────────────────────────────────────

    def test_successful_integration_gate_cannot_change_tested_commit(self):
        self.configure(gate=['echo changed >> README.md; git add README.md; git commit -qm gate-change'])
        result = self.run_script('--no-final-review')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Integration gate changed HEAD', result.stderr)
        self.assertEqual((self.root / 'README.md').read_text(), 'Project\n')
        self.assertNotEqual(self.state()['209']['status'], 'done')

    def test_failed_verify_gate_preserves_new_files_and_refuses_resume(self):
        self.configure(verify=['test -f fixed.txt'], gate=['echo unintended > stray-source.py; false'])
        self.env['GATE_CMDS'] = 'true'
        self.assertEqual(self.run_script('--no-verify', '--no-final-review').returncode, 0)
        self.env.pop('GATE_CMDS')
        self.assertNotEqual(self.run_script('--final-only', '--no-ship').returncode, 0)
        self.assertEqual(self.state()['_verify']['unexpectedUntracked'], ['stray-source.py'])
        self.assertNotEqual(self.run_script('--final-only', '--no-ship').returncode, 0)
        self.assertTrue((self.root / '.scratch/worktrees/verify/stray-source.py').exists())
        self.assertIn('unexpected uncommitted files remain', self.state()['_verify']['reason'])

    def test_human_commits_in_kept_verify_worktree_require_gate(self):
        self.serve()
        config = json.loads((self.root / '.agents/ticket-runner.json').read_text())
        config['gate'] = ['false']
        self.configure(**config)
        self.env['GATE_CMDS'] = 'true'
        self.assertEqual(self.run_script('--no-verify', '--no-final-review').returncode, 0)
        self.env.pop('GATE_CMDS')
        self.assertNotEqual(self.run_script('--final-only', '--no-ship', mode='verifier-missing').returncode, 0)
        wt = self.root / '.scratch/worktrees/verify'
        with (wt / 'README.md').open('a') as out: out.write('human fix\n')
        subprocess.run(['git', 'add', 'README.md'], cwd=wt, check=True)
        subprocess.run(['git', 'commit', '-qm', 'Human fix'], cwd=wt, check=True)
        result = self.run_script('--final-only', '--no-ship')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('gate fails', self.state()['_verify']['reason'])
        self.assertEqual((self.root / 'README.md').read_text(), 'Project\n')

    def test_existing_verify_worktree_link_tampering_is_not_repaired_silently(self):
        with (self.root / '.gitignore').open('a') as out: out.write('.env\n')
        (self.root / '.env').write_text('PRIVATE=value')
        self.git('add', '.gitignore'); self.git('commit', '-qm', 'Ignore local env')
        self.configure(verify=['false'])
        self.assertNotEqual(self.run_script('--no-ship').returncode, 0)
        link = self.root / '.scratch/worktrees/verify/.env'
        link.unlink(); link.write_text('tampered')
        self.assertNotEqual(self.run_script('--final-only', '--no-ship').returncode, 0)
        self.assertEqual(link.read_text(), 'tampered')
        self.assertIn('no longer links', self.state()['_verify']['reason'])

    def test_cached_pull_request_on_different_commit_is_rejected(self):
        self.assertEqual(self.run_script().returncode, 0)
        prs = self.prs(); prs[0]['oid'] = '0' * 40
        (self.root / 'prs.json').write_text(json.dumps(prs))
        self.assertNotEqual(self.run_script('--final-only').returncode, 0)
        self.assertIn('no longer matches', self.state()['_ship']['reason'])

    def test_parallel_independent_tickets_rebase_and_gate_before_integrating(self):
        self.issues[1]['body'] = 'Parent #207'
        result = self.run_script('--jobs', '2', '--no-final-review', mode='parallel')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rebasing onto', result.stdout)
        self.assertEqual((self.root / 'ticket-209.txt').read_text(), '209')
        self.assertEqual((self.root / 'ticket-210.txt').read_text(), '210')
        self.assertEqual([self.state()[str(n)]['status'] for n in (209, 210)], ['done', 'done'])
        logs = list((self.root / '.scratch/run-tickets-commandcode/207').glob('*/gate-*.log'))
        self.assertEqual(len(logs), 2)

    def test_parallel_conflict_repair_preserves_both_slices_and_runs_gate(self):
        self.issues[1]['body'] = 'Parent #207'
        result = self.run_script('--jobs', '2', '--no-final-review', mode='parallel-conflict')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('rebase conflicted', result.stdout)
        self.assertEqual((self.root / 'shared.txt').read_text(), '209 and 210\n')
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual([self.state()[str(n)]['status'] for n in (209, 210)], ['done', 'done'])

    def test_checkout_lock_precedes_discovery_and_state_snapshot(self):
        env = dict(self.env, ISSUES=json.dumps(self.issues), MODE='success', DISCOVERY_DELAY='1')
        slow = subprocess.Popen([str(RUNNER / 'bin/ticket-runner'), '207', '--no-final-review'],
                                cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: slow.kill() if slow.poll() is None else None)
        import time
        deadline = time.monotonic() + 5
        while not (self.root / 'discovery.log').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        fast = self.run_script('--no-final-review')
        self.assertNotEqual(fast.returncode, 0)
        self.assertIn('Another ticket runner is active', fast.stderr)
        _, errors = slow.communicate(timeout=20)
        self.assertEqual(slow.returncode, 0, errors)
        self.assertEqual(self.kinds(), ['implement', 'implement'])

    def test_interrupt_during_verification_stops_shell_and_descendants(self):
        self.configure(verify=['echo $$ > command.pid; sleep 60'])
        env = dict(self.env, ISSUES=json.dumps(self.issues), MODE='success')
        runner = subprocess.Popen([str(RUNNER / 'bin/ticket-runner'), '207', '--no-ship'],
                                  cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: runner.kill() if runner.poll() is None else None)
        import signal, time
        marker = self.root / '.scratch/worktrees/verify/command.pid'
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(marker.exists())
        child = int(marker.read_text())
        runner.send_signal(signal.SIGINT)
        runner.communicate(timeout=10)
        self.assertEqual(runner.returncode, 130)
        with self.assertRaises(ProcessLookupError): os.kill(child, 0)

    def test_verify_commands_cannot_discard_new_unignored_files(self):
        self.configure(verify=['echo new > new-uncommitted.txt'])
        result = self.run_script('--no-ship')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / '.scratch/worktrees/verify/new-uncommitted.txt').exists())
        self.assertEqual(self.state()['_verify']['status'], 'needs-human')
        self.assertNotEqual(self.run_script('--final-only', '--no-ship').returncode, 0)
        self.assertTrue((self.root / '.scratch/worktrees/verify/new-uncommitted.txt').exists())

    def test_verify_commands_cannot_mutate_tracked_code(self):
        self.configure(verify=["echo changed >> README.md"])
        result = self.run_script('--no-ship')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()['_verify']['status'], 'needs-human')
        self.assertTrue((self.root / '.scratch/worktrees/verify/README.md').read_text().endswith('changed\n'))
        self.assertEqual((self.root / 'README.md').read_text(), 'Project\n')

    def test_failed_verify_repair_gate_is_retried_on_resume(self):
        self.configure(verify=["test -f fixed.txt"], gate=["false"])
        # Finish tickets with a working gate before testing the verify repair gate.
        self.env['GATE_CMDS'] = 'true'
        self.assertEqual(self.run_script('--no-verify', '--no-final-review').returncode, 0)
        self.env.pop('GATE_CMDS')
        self.assertNotEqual(self.run_script('--final-only', '--no-ship').returncode, 0)
        self.assertNotEqual(self.run_script('--final-only', '--no-ship').returncode, 0)
        self.assertEqual(self.state()['_verify']['status'], 'needs-human')
        self.assertGreaterEqual(len(list(self.verify_dir().glob('gate-*.log'))), 2)
        self.assertFalse((self.root / 'fixed.txt').exists())

    def test_new_ignored_runtime_artifacts_are_tolerated(self):
        self.serve()
        result = self.run_script('--no-ship', mode='verifier-ignored-file')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()['_verify']['status'], 'pass')

    def test_new_unignored_runtime_files_are_preserved_and_refused(self):
        self.serve()
        result = self.run_script('--no-ship', mode='verifier-new-file')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()['_verify']['status'], 'needs-human')
        self.assertEqual(self.state()['_verify']['unexpectedUntracked'], ['new-runtime-output.txt'])
        artifact = self.root / '.scratch/worktrees/verify/new-runtime-output.txt'
        self.assertEqual(artifact.read_text(), 'created during checks')
        self.assertNotEqual(self.run_script('--final-only', '--no-ship').returncode, 0)
        self.assertTrue(artifact.exists())
        self.assertNotIn('_review', self.state())

    def test_verifier_contradictory_and_missing_results_are_rejected(self):
        self.serve()
        for mode in ('verifier-contradictory', 'verifier-missing'):
            with self.subTest(mode=mode):
                result = self.run_script('--no-ship', mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.state()['_verify']['status'], 'needs-human')
                self.assertNotIn('_review', self.state())

    def test_new_verify_worktree_provisions_links(self):
        with (self.root / '.gitignore').open('a') as out: out.write('.env\n')
        (self.root / '.env').write_text('PRIVATE=value')
        self.git('add', '.gitignore'); self.git('commit', '-qm', 'Ignore local env')
        self.configure(verify=["test -L .env"])
        result = self.run_script('--no-ship')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_runtime_inputs_invalidate_all_stages(self):
        self.serve()
        self.assertEqual(self.run_script().returncode, 0)
        before = len(self.calls())
        with (self.root / '.env.verify').open('a') as out: out.write('NEW_SECRET=secret-regression-value\n')
        self.assertEqual(self.run_script('--final-only').returncode, 0)
        self.assertEqual(self.kinds()[before:], ['verifier', 'epic review', 'launch plan'])
        self.assertNotIn('secret-regression-value', json.dumps(self.state()))

    def test_closed_cached_pull_request_requires_attention(self):
        self.assertEqual(self.run_script().returncode, 0)
        prs = self.prs(); prs[0]['state'] = 'CLOSED'
        (self.root / 'prs.json').write_text(json.dumps(prs))
        result = self.run_script('--final-only')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('closed', self.state()['_ship']['reason'].lower())
        self.assertNotEqual(self.run_script('--final-only').returncode, 0)
        self.assertEqual(len(self.prs()), 1)

    def test_gate_runs_for_every_ticket_without_a_rebase(self):
        self.configure(gate=["echo checked >> gate.log"])
        result = self.run_script('--no-final-review')
        self.assertEqual(result.returncode, 0, result.stderr)
        logs = list((self.root / '.scratch/run-tickets-commandcode/207').glob('*/gate-*.log'))
        self.assertEqual(len(logs), 2)

    def verify_dir(self):
        return self.root / ".scratch/run-tickets-commandcode/207/verify"

    def serve(self, **config):
        port = free_port()
        (self.root / "serve.py").write_text(SERVE)
        (self.root / ".env.verify").write_text(f"# local only\nPORT={port}\nexport VERIFY_TOKEN=\"s3cret-token\"\n")
        with (self.root / ".gitignore").open("a") as f:
            f.write(".env.verify\n")
        self.git("add", "serve.py", ".gitignore")
        self.configure(serve="python3 serve.py", ready=READY, serveEnv=".env.verify", readyTimeout=20,
                       gate=["true"], **config)
        self.env["TEST_PORT"] = str(port)
        return port

    def test_verify_commands_pass_and_the_epic_review_gets_the_report(self):
        self.configure(verify=["echo first", "echo second"])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state()
        self.assertEqual(state['_verify']['status'], 'pass')
        self.assertEqual(state['_verify']['head'], self.git('rev-parse', 'HEAD'))
        self.assertIn('first', (self.verify_dir() / 'cmd-1.log').read_text())
        self.assertIn('second', (self.verify_dir() / 'cmd-2.log').read_text())
        report = (self.verify_dir() / 'report.md').read_text()
        self.assertIn('PASS `echo first`', report)
        self.assertIn('skipped: no `serve`', report)
        self.assertIn(str(self.verify_dir() / 'report.md'), self.prompt(self.call('epic review')))
        self.assertEqual(self.kinds(), ['implement', 'implement', 'epic review', 'launch plan'])
        self.assertFalse((self.root / '.scratch/worktrees/verify').exists())
        self.assertEqual(self.git('branch', '--list', 'cc/207/verify'), '')

    def test_verify_failure_gets_one_repair_then_re_verifies_and_fast_forwards(self):
        self.configure(verify=["test -f fixed.txt"], gate=["true"])
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'repair', 'epic review', 'launch plan'])
        repair = self.calls()[2]
        self.assertIn(str(self.skills / 'debugging-and-error-recovery' / 'SKILL.md'), self.prompt(repair))
        self.assertIn('$ test -f fixed.txt', self.prompt(repair))
        self.assertEqual(self.model(repair), 'deepseek/deepseek-v4.1-flash')
        self.assertTrue((self.root / 'fixed.txt').exists())  # The epic branch fast-forwarded to the fix.
        state = self.state()
        self.assertEqual(state['_verify']['head'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(state['_verify']['status'], 'pass')
        self.assertTrue(state['_verify']['repaired'])
        self.assertTrue((self.verify_dir() / 'gate-1.log').exists())
        self.assertIn('one repair round ran', (self.verify_dir() / 'report.md').read_text())

    def test_verify_still_failing_needs_a_human_and_skips_the_epic_review(self):
        self.configure(verify=["test -f fixed.txt"], gate=["true"])
        result = self.run_script(mode='repair-noop')
        self.assertEqual(result.returncode, 1)
        state = self.state()
        self.assertEqual(state['_verify']['status'], 'needs-human')
        self.assertIn('still fails after one repair', state['_verify']['reason'])
        self.assertNotIn('_review', state)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'repair'])
        wt = self.root / '.scratch/worktrees/verify'
        self.assertTrue((wt / 'other.txt').exists())
        self.assertFalse((self.root / 'other.txt').exists())
        self.assertIn(['issue', 'edit', '207', '--add-label', 'needs-human', '--remove-label', 'in-progress'],
                      self.gh_calls())
        # A person fixes it in the kept worktree; the next run re-verifies without another repair.
        (wt / 'fixed.txt').write_text('by hand')
        subprocess.run(['git', 'add', 'fixed.txt'], cwd=wt, check=True)
        subprocess.run(['git', 'commit', '-qm', 'Fix by hand'], cwd=wt, check=True)
        result = self.run_script('--final-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'repair', 'epic review', 'launch plan'])
        self.assertEqual((self.root / 'fixed.txt').read_text(), 'by hand')

    def test_verify_repair_touching_a_protected_file_is_not_merged(self):
        self.configure(verify=["test -f fixed.txt"], gate=["true"])
        result = self.run_script(mode='repair-protected')
        self.assertEqual(result.returncode, 1)
        self.assertIn('protected files (AGENTS.md)', self.state()['_verify']['reason'])
        self.assertFalse((self.root / 'fixed.txt').exists())

    def test_runtime_checks_serve_the_app_with_serve_env_and_stop_it(self):
        port = self.serve()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'verifier', 'epic review', 'launch plan'])
        verifier = self.calls()[2]
        self.assertEqual(self.model(verifier), 'gpt-6-luna')
        self.assertEqual(verifier[verifier.index('--effort')+1], 'high')
        self.assertNotIn('--tools-enable', verifier)
        self.assertIn('checks.md', self.prompt(verifier))
        checks = (self.verify_dir() / 'checks.md').read_text()
        self.assertIn('## Parent issue #207', checks)
        self.assertIn('Checkpoint: after #210', checks)
        self.assertIn('answered hello from the app', (self.verify_dir() / 'check/final.txt').read_text())
        report = (self.verify_dir() / 'report.md').read_text()
        self.assertIn('VERIFY=PASS (1 VERIFIED, 0 FAILED, 0 UNVERIFIABLE)', report)
        self.assertNotIn('s3cret-token', result.stdout + result.stderr)
        with socket.socket() as sock:  # The app was stopped.
            self.assertNotEqual(sock.connect_ex(('127.0.0.1', port)), 0)

    def test_failed_runtime_check_is_repaired_then_the_whole_stage_reruns(self):
        self.serve(verify=["echo built"])
        result = self.run_script(mode='verify-fail')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'verifier', 'repair', 'verifier', 'epic review',
                                        'launch plan'])
        self.assertIn('FAILED runtime checks', self.prompt(self.calls()[3]))
        self.assertTrue((self.root / 'fixed.txt').exists())

    def test_an_app_that_never_gets_ready_is_repaired_once_then_needs_a_human(self):
        (self.root / ".env.verify").write_text("PORT=1\n")
        with (self.root / ".gitignore").open("a") as f:
            f.write(".env.verify\n")
        self.git("add", ".gitignore")
        self.configure(serve="sleep 300 & echo $! > app.pid; wait", ready="false", readyTimeout=1,
                       serveEnv=".env.verify", gate=["true"])
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'repair'])
        self.assertIn('The app did not start for the runtime checks', self.prompt(self.call('repair')))
        self.assertEqual(self.state()['_verify']['status'], 'needs-human')
        self.assertIn('still fails after one repair', self.state()['_verify']['reason'])
        pid = int((self.root / '.scratch/worktrees/verify/app.pid').read_text())
        with self.assertRaises(ProcessLookupError):  # The whole process group was stopped.
            os.kill(pid, 0)

    def test_runtime_checks_refuse_without_a_separate_serve_env(self):
        (self.root / ".env").write_text("SECRET=1\n")
        for config, expected in [({}, "sets serve but no serveEnv"),
                                 ({"serveEnv": ".env.missing"}, "serveEnv .env.missing does not exist"),
                                 ({"serveEnv": ".env"}, "must not be the developer's .env")]:
            with self.subTest(config=config):
                self.configure(commit=False, serve="python3 serve.py", ready="true", **config)
                result = self.run_script('--dry-run')
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected, result.stderr)
        self.configure(commit=False, serve="python3 serve.py", ready="true")
        self.assertEqual(self.run_script('--dry-run', '--no-verify').returncode, 0)

    def test_browser_refuses_until_the_devtools_server_is_registered(self):
        self.serve(browser=True)
        result = self.run_script('--dry-run')
        self.assertEqual(result.returncode, 1)
        self.assertIn('command-code mcp add --scope user --transport stdio chrome-devtools -- '
                      'npx -y chrome-devtools-mcp@latest --isolated', result.stderr)
        self.assertIn('"browser": false', result.stderr)
        self.assertFalse((self.root / 'agents.log').exists())
        self.env["MCP_LIST"] = "chrome-devtools: npx -y chrome-devtools-mcp@latest --isolated - Connected"
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        verifier = self.calls()[2]
        self.assertIn('mcp__chrome-devtools__navigate_page', verifier[verifier.index('--tools-enable')+1])
        self.assertIn(str(self.skills / 'browser-testing-with-devtools' / 'SKILL.md'), self.prompt(verifier))
        self.assertNotIn('--tools-enable', self.calls()[0])

    def test_verifier_must_stay_read_only(self):
        self.serve()
        result = self.run_script(mode='verifier-edit')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['_verify']['status'], 'needs-human')
        self.assertIn('must be read-only', self.state()['_verify']['reason'])
        self.assertNotIn('repair', self.kinds())

    def test_interrupted_verifier_resumes_its_session(self):
        self.serve()
        self.assertEqual(self.run_script(mode='verifier-cap').returncode, 1)
        self.assertEqual(self.state()['_verify']['status'], 'turn-cap')
        result = self.run_script('--resume', '--final-only', mode='verifier-cap')
        self.assertEqual(result.returncode, 0, result.stderr)
        resumed = self.calls()[3]
        self.assertEqual(resumed[resumed.index('--resume')+1], 'verify-session')

    def test_interrupted_verify_repair_resumes_its_session(self):
        self.configure(verify=["test -f fixed.txt"], gate=["true"])
        self.assertEqual(self.run_script(mode='repair-cap').returncode, 1)
        self.assertEqual(self.state()['_verify']['status'], 'turn-cap')
        result = self.run_script('--resume', '--final-only', mode='repair-cap')
        self.assertEqual(result.returncode, 0, result.stderr)
        resumed = self.calls()[3]
        self.assertIn('verify stage failed', self.prompt(resumed))
        self.assertEqual(resumed[resumed.index('--resume')+1], 'repair-session')
        self.assertTrue((self.root / 'fixed.txt').exists())


    # ── ship stage ───────────────────────────────────────────────────────

    def remote_head(self, branch='test'):
        out = subprocess.run(['git', 'ls-remote', '--heads', str(self.origin), branch], capture_output=True, text=True)
        return out.stdout.split()[0] if out.stdout.strip() else None

    def test_ship_pushes_and_opens_a_ready_pull_request_with_a_runner_written_body(self):
        self.plan_osmani()
        self.configure(verify=["echo first"])
        result = self.run_osmani()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_head(), self.git('rev-parse', 'HEAD'))
        create = next(c for c in self.gh_calls() if c[:2] == ['pr', 'create'])
        self.assertNotIn('--draft', create)
        [pr] = self.prs()
        self.assertEqual((pr['base'], pr['head'], pr['title']), ('main', 'test', 'Epic title (#207)'))
        for text in ('## Tickets', '- #209 First slice', '## Verify', '- PASS `echo first`', '## Launch plan',
                     '## Rollback plan', '## Deferred findings', 'old bug in b.ts:2'):
            self.assertIn(text, pr['body'])
        plan = self.prompt(self.call('launch plan'))
        self.assertIn(str(self.skills / 'shipping-and-launch' / 'SKILL.md'), plan)
        self.assertIn('definition-of-done.md', plan)
        self.assertIn(str(self.verify_dir() / 'report.md'), plan)
        self.assertIn('no deploy section', plan)
        state = self.state()['_ship']
        self.assertEqual((state['status'], state['pr'], state['head']), ('pr-open', 300, self.git('rev-parse', 'HEAD')))

    def test_ship_edits_the_open_pull_request_for_a_new_head(self):
        self.configure(verify=["true"], prBase="develop")
        self.assertEqual(self.run_script().returncode, 0)
        self.issues.append({"number": 211, "title": "Third slice", "body": "Parent #207", "state": "OPEN"})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([c for c in self.gh_calls() if c[:2] == ['pr', 'create']]), 1)
        self.assertEqual(len([c for c in self.gh_calls() if c[:2] == ['pr', 'edit']]), 1)
        [pr] = self.prs()
        self.assertEqual(pr['base'], 'develop')
        self.assertIn('#211 Third slice', pr['body'])
        self.assertEqual(self.remote_head(), self.git('rev-parse', 'HEAD'))

    def test_ship_refuses_to_push_over_foreign_commits(self):
        self.git("push", "-q", "origin", "test")
        other = self.origin.parent / "other"
        subprocess.run(['git', 'clone', '-q', '-b', 'test', str(self.origin), str(other)], check=True)
        (other / 'foreign.txt').write_text('someone else')
        for args in (['add', 'foreign.txt'], ['-c', 'user.email=x@y', '-c', 'user.name=X', 'commit', '-qm', 'Foreign'],
                     ['push', '-q', 'origin', 'test']):
            subprocess.run(['git', *args], cwd=other, check=True)
        foreign = self.remote_head()
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['_ship']['status'], 'needs-human')
        self.assertIn('never force-pushes', self.state()['_ship']['reason'])
        self.assertEqual(self.remote_head(), foreign)
        self.assertFalse((self.root / 'prs.json').exists())
        self.assertEqual(self.run_script('--final-only').returncode, 1)
        self.assertEqual(self.kinds().count('launch plan'), 1)  # The plan for this head is kept.

    def test_merge_refusals(self):
        result = self.run_script('--merge', '--no-verify')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--merge needs the verify stage', result.stderr)
        result = self.run_script('--merge')  # Default config: verify has nothing to run.
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['_ship']['status'], 'merge-refused')
        self.assertIn('verify stage ran nothing', self.state()['_ship']['reason'])
        self.assertFalse([c for c in self.gh_calls() if c[:2] in (['pr', 'checks'], ['pr', 'merge'])])

    def test_merge_waits_for_green_ci_then_merges_without_deleting_branches(self):
        self.configure(verify=["true"], mergeMethod="squash")
        result = self.run_script('--merge')
        self.assertEqual(result.returncode, 0, result.stderr)
        head = self.git('rev-parse', 'HEAD')
        self.assertIn(['pr', 'checks', '300', '--watch'], self.gh_calls())
        merge = next(c for c in self.gh_calls() if c[:2] == ['pr', 'merge'])
        self.assertEqual(merge, ['pr', 'merge', '300', '--squash', '--match-head-commit', head])
        self.assertEqual(self.state()['_ship']['status'], 'merged')
        self.assertEqual(self.prs()[0]['state'], 'MERGED')
        self.assertEqual(self.git('branch', '--list', 'test'), '* test')

    def test_red_ci_needs_a_human_and_never_merges(self):
        self.configure(verify=["true"])
        self.env["CHECKS_RC"] = "1"
        result = self.run_script('--merge')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.state()['_ship']['status'], 'needs-human')
        self.assertIn('CI is red', self.state()['_ship']['reason'])
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ['pr', 'merge']])
        self.assertEqual(self.kinds(), ['implement', 'implement', 'epic review', 'launch plan'])

    def test_no_ship_stops_after_the_epic_review(self):
        result = self.run_script('--no-ship')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'epic review'])
        self.assertIsNone(self.remote_head())

    def test_no_verify_is_named_in_the_launch_plan_and_the_pull_request(self):
        self.configure(verify=["false"])
        result = self.run_script('--no-verify')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('_verify', self.state())
        self.assertIn('verify skipped', self.prompt(self.call('launch plan')))
        self.assertIn('was skipped for this run', self.prompt(self.call('epic review')))
        self.assertIn('Verify skipped (--no-verify).', self.prs()[0]['body'])

    def test_interrupted_launch_plan_resumes_its_session(self):
        self.assertEqual(self.run_script(mode='plan-cap').returncode, 1)
        self.assertEqual(self.state()['_ship']['status'], 'turn-cap')
        result = self.run_script('--resume', '--final-only', mode='plan-cap')
        self.assertEqual(result.returncode, 0, result.stderr)
        resumed = self.call('launch plan')
        self.assertEqual(resumed[resumed.index('--resume')+1], 'plan-session')
        self.assertEqual(self.state()['_ship']['status'], 'pr-open')


    # ── stage bookkeeping ────────────────────────────────────────────────

    def test_stages_skip_while_valid_and_rerun_for_a_new_head(self):
        self.configure(verify=["echo verified"])
        self.assertEqual(self.run_script().returncode, 0)
        result = self.run_script('--final-only')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 4)
        self.assertIn('Verify stage already passed', result.stdout)
        self.assertIn('Epic review already said SHIP', result.stdout)
        self.assertIn('already up to date', result.stdout)
        first = self.state()
        self.issues.append({"number": 211, "title": "Third slice", "body": "Parent #207", "state": "OPEN"})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'epic review', 'launch plan',
                                        'implement', 'epic review', 'launch plan'])
        head = self.git('rev-parse', 'HEAD')
        state = self.state()
        self.assertNotEqual(first['_verify']['head'], head)
        self.assertEqual([state[k]['head'] for k in ('_verify', '_review', '_ship')], [head] * 3)

    def test_a_verify_result_that_arrives_later_invalidates_an_unverified_review(self):
        self.configure(verify=["true"])
        self.assertEqual(self.run_script('--no-verify', '--no-ship').returncode, 0)
        self.assertIsNone(self.state()['_review']['verified'])
        self.assertEqual(self.run_script('--final-only', '--no-ship').returncode, 0)
        self.assertEqual(self.kinds(), ['implement', 'implement', 'epic review', 'epic review'])
        self.assertEqual(self.state()['_review']['verified'], self.state()['_verify']['fingerprint'])

    def test_skip_flag_combinations(self):
        result = self.run_script('--final-only', '--no-verify', '--no-final-review')
        self.assertEqual(result.returncode, 2)
        self.assertIn('leaves nothing to run', result.stderr)
        self.configure(verify=["true"])
        self.assertEqual(self.run_script('--no-final-review').returncode, 0)
        self.assertEqual(self.kinds(), ['implement', 'implement'])
        self.assertEqual(self.state()['_verify']['status'], 'pass')
        self.assertNotIn('_ship', self.state())

    def test_dry_run_lists_the_stages(self):
        self.configure(verify=["true", "true"], prBase="master")
        result = self.run_script('--dry-run', '--merge')
        self.assertEqual(result.returncode, 0, result.stderr)
        for line in ('tickets      2 to run', 'verify       2 command(s); runtime checks off (no serve)',
                     'epic review  claude-sonnet-5-5',
                     'ship         launch plan, push, pull request into master; then --merge (merge)'):
            self.assertIn(line, result.stdout)
        result = self.run_script('--dry-run', '--no-verify', '--no-ship')
        self.assertIn('verify       skipped (--no-verify)', result.stdout)
        self.assertIn('ship         skipped (--no-ship)', result.stdout)
        self.assertIn('verify, epic review, ship: skipped (--only)', self.run_script('--dry-run', '--only', '209').stdout)

    def test_pocock_checks_the_skills_of_its_enabled_stages(self):
        shutil.rmtree(self.skills / "shipping-and-launch")
        result = self.run_script('--dry-run')
        self.assertEqual(result.returncode, 1)
        self.assertIn('shipping-and-launch/SKILL.md', result.stderr)
        self.assertEqual(self.run_script('--dry-run', '--no-ship').returncode, 0)
        shutil.rmtree(self.skills / "debugging-and-error-recovery")
        self.assertEqual(self.run_script('--dry-run', '--no-ship').returncode, 0)  # Verify has nothing to run.
        self.configure(verify=["true"])
        result = self.run_script('--dry-run', '--no-ship')
        self.assertEqual(result.returncode, 1)
        self.assertIn('debugging-and-error-recovery/SKILL.md', result.stderr)


if __name__ == '__main__':
    unittest.main()
