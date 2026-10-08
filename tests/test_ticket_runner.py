"""Offline integration checks: real temporary git repo, fake gh and CommandCode."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[1]
OSMANI_SKILLS = ("incremental-implementation", "test-driven-development", "code-simplification",
                 "security-and-hardening", "api-and-interface-design", "debugging-and-error-recovery",
                 "performance-optimization", "doubt-driven-development", "code-review-and-quality",
                 "git-workflow-and-versioning")


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
if args[:2] == ['issue', 'list']: print(os.environ['ISSUES'])
elif args[:2] == ['issue', 'view'] and 'body' in args: print(json.dumps({'body': os.environ.get('PARENT_BODY', '')}))
elif args[:2] == ['issue', 'view']: print('CLOSED')
elif args[:2] == ['api', 'graphql']:
    query=next(a for a in args if a.startswith('query='))
    repo={'nameWithOwner':'acme/engine', 'parent':{'subIssues':none}}
    repo.update({alias:{'blockedBy':none} for alias in re.findall(r'(i\\d+): issue', query)})
    print(json.dumps({'data':{'repository':repo}}))
""")
        self.fake("command-code", """import json, os, subprocess, sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['AGENT_LOG'], 'a') as f: f.write(json.dumps(args)+'\\n')
prompt=args[args.index('-p')+1]
review='You are the final reviewer' in prompt
ticket_review='You are the ticket reviewer' in prompt
mode=os.environ.get('MODE', 'success')
cap=mode in ('cap', 'cap-commit') and '--resume' not in args
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
if not review:
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
        (self.root / ".gitignore").write_text("fake-bin/\n*.log\nskill.md\nskills/\nreferences/\n")
        (self.root / "AGENTS.md").write_text("# Agents\n")
        self.git("add", ".gitignore", "AGENTS.md")
        self.git("commit", "-qm", "Test setup")
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                        COMMANDCODE_BIN=str(self.bin / "command-code"), IMPLEMENT_SKILL=str(skill),
                        AGENT_SKILLS_DIR=str(self.skills), PARENT_BODY="## Checkpoint: after #210\n- [ ] Tests pass",
                        CALL_LOG=str(self.root / "gh.log"), AGENT_LOG=str(self.root / "agents.log"))
        for name in ('IMPL_MODEL', 'IMPL_EFFORT', 'IMPL_MAX_TURNS', 'FINAL_MODEL', 'FINAL_EFFORT', 'FINAL_MAX_TURNS',
                     'WORKFLOW', 'TICKET_REVIEW_MODEL', 'TICKET_REVIEW_EFFORT', 'TICKET_REVIEW_MAX_TURNS'):
            self.env.pop(name, None)

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

    def test_default_implements_in_order_then_sonnet_reviews(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][calls[0].index('-m')+1], 'deepseek/deepseek-v4.1-flash')
        self.assertEqual(calls[-1][calls[-1].index('-m')+1], 'claude-sonnet-5-5')
        self.assertEqual(calls[0][calls[0].index('--max-turns')+1], '500')
        self.assertEqual(self.state()['210']['status'], 'done')
        self.assertEqual(self.state()['_review']['status'], 'ship')
        self.assertEqual(self.run_script('--resume').returncode, 0)
        self.assertEqual(len(self.calls()), 4)  # Resume skips implementation.

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
        kinds = ['ticket review' if 'You are the ticket reviewer' in self.prompt(c) else
                 'epic review' if 'You are the final reviewer' in self.prompt(c) else 'implement' for c in calls]
        self.assertEqual(kinds, ['implement', 'ticket review', 'implement', 'ticket review', 'epic review'])
        # Odd #209 gets the second model (glm), even #210 the first (luna); both review at high.
        self.assertEqual(self.model(calls[1]), 'z-ai/glm-5.3-flash')
        self.assertEqual(self.model(calls[3]), 'gpt-6-luna')
        self.assertEqual(calls[1][calls[1].index('--effort')+1], 'high')
        implement = self.prompt(calls[0])
        self.assertIn(str(self.skills / 'incremental-implementation' / 'SKILL.md'), implement)
        self.assertIn('Do not review your own work', implement)
        self.assertIn('ASSUMPTIONS', implement)
        self.assertIn('old bug in b.ts:2', (self.root / '.scratch/run-tickets-commandcode/207/209/review/round-1/final.txt').read_text())
        self.assertIn('209/review/round-1/final.txt', self.prompt(calls[-1]))
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
        self.assertIn('Every ticket passed its own ticket review', self.prompt(self.calls()[-1]))


if __name__ == '__main__':
    unittest.main()
