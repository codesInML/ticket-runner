"""Skill guidance and stage prompt construction."""

from .settings import BROWSER_SERVER, BROWSER_SKILL, TICKET_WRITING_SKILLS


class Prompts:
    def skill_file(self, name):
        return self.skills_dir / name / "SKILL.md"

    def domain(self):
        """Where a prompt learns the project's sensitive areas: AGENTS.md's `## Ticket Runner` section."""
        if self.notes:
            return ("The `## Ticket Runner` section of AGENTS.md lists what this project counts as personal or "
                    "sensitive data,\ngoverned parameters, public contracts and performance-sensitive areas; "
                    "use it wherever this prompt names them.\n")
        return ("AGENTS.md has no `## Ticket Runner` section, so use general judgement for what counts as personal "
                "or\nsensitive data, a governed parameter, a public contract or a performance-sensitive area.\n")

    def osmani_brief(self, n, tdir):
        s = self.skill_file
        return f"""Read AGENTS.md, then implement issue #{n} by following these skills in order:
{s('incremental-implementation')}, then {s('test-driven-development')} for every slice, then
{s('code-simplification')} on your own diff only.
Read the full ticket at {tdir / 'ticket.md'}. Satisfy its acceptance criteria and its Verification
section without expanding scope. {self.domain()}Load a conditional skill only when the ticket needs it:
- {s('security-and-hardening')}: auth, untrusted input, or personal or sensitive data
- {s('api-and-interface-design')}: a public contract or module interface changes
- {s('performance-optimization')}: the ticket touches a performance-sensitive area
- {s('frontend-ui-engineering')}: the ticket builds or changes user interface
- {s('observability-and-instrumentation')}: a new critical path, background job or queue
- {s('documentation-and-adrs')}: a public interface changes, or you make a decision a future reader would question
- {s('deprecation-and-migration')}: removing or renaming anything public, or a schema or data migration
- {s('ci-cd-and-automation')}: CI, Docker or build configuration changes
- {s('debugging-and-error-recovery')}: a test or build breaks for a reason you do not yet understand
- {s('doubt-driven-development')}: only for claims a test cannot pin down (invariants, cross-module
  contracts, migrations, anything irreversible). One doubt cycle per claim in this unattended run; a
  claim that still has substantive findings after it is a reason to stop with STATUS=NEEDS_HUMAN.
Remove zombie code (dead, unreachable or unused code) only inside files this ticket already changes;
leave it everywhere else, and the ticket reviewer records it as DEFERRED.
Do not review your own work: a separate ticket reviewer reviews this ticket once you finish.
Run typechecking, the ticket's focused tests, and the full suite once before finishing.
Nobody can answer questions during this run. Decide and record anything that does not change what an
acceptance criterion means (seams, naming, file placement, which existing pattern to follow): pick
what the surrounding code does, and list each choice under an `## ASSUMPTIONS` heading in your final
report. Stop instead (commit buildable work, state the question, end with STATUS=NEEDS_HUMAN) when the
ticket contradicts the code or another ticket, when an acceptance criterion can be read two ways that
lead to different behavior, or when meeting it would change a governed parameter or a public contract the
ticket does not mention.
"""

    def repair_guidance(self, kind):
        if not self.osmani:
            return ""
        if kind == "conflict":
            return (f"Follow {self.skill_file('git-workflow-and-versioning')} for the resolution, and re-run the "
                    "focused tests from the ticket's Verification section.\n")
        return (f"Follow {self.skill_file('debugging-and-error-recovery')}: reproduce, localize, fix, then guard "
                "with a regression test, since nobody else will cover this interaction between tickets.\n")

    def review_prompt(self, n, tdir, base, round_):
        s = self.skill_file
        again = f"""This is the re-review after one fix round. Your earlier report is at
{tdir / 'review' / 'round-1' / 'final.txt'} and the fix round's report at {tdir / 'fix' / 'final.txt'}.
Confirm every BLOCKING finding there is fixed and raise anything the fix broke; no fix round follows.
""" if round_ > 1 else ""
        return f"""You are the ticket reviewer for issue #{n} of epic #{self.a.parent}. Someone else wrote this
code; review it with fresh eyes and take none of their claims on trust.
Read AGENTS.md and the ticket at {tdir / 'ticket.md'}, then follow {s('code-review-and-quality')}
on git diff {base}..HEAD and git log {base}..HEAD in this worktree. {self.domain()}Load these only when the diff needs them:
- {s('security-and-hardening')}: auth, untrusted input, or personal or sensitive data
- {s('performance-optimization')}: a performance-sensitive area
- {s('api-and-interface-design')}: a public contract or module interface
The implementer's final report is at {tdir / 'final.txt'}. Check every choice under its ASSUMPTIONS
heading and raise each wrong one as a finding.
{again}Report under these markdown headings, citing file and line for every finding:
## ACCEPTANCE CRITERIA: every criterion MET or UNMET, with evidence (test name, file:line, command output).
## BLOCKING: real defects this ticket owns, and every UNMET criterion. A pre-existing defect is BLOCKING
when this ticket's change depends on it or makes it worse.
## DEFERRED: real defects this ticket neither caused, depends on, nor worsened: pre-existing code, or
another ticket's job (name it when you know it). Zombie code (dead, unreachable or unused code) outside
the files this ticket changes is DEFERRED, never BLOCKING. Write "None." when there are none.
## REJECTED: findings you considered and dismissed (style, taste, a misread), one line each.
Read-only review: do not edit files, commit, change branches, push, or post comments.
Use only CommandCode for agent work; do not invoke codex or claude CLIs.
End with exactly one line: VERDICT=PASS when nothing is BLOCKING, otherwise VERDICT=FAIL.
"""

    def verifier_prompt(self, wt):
        cfg, s = self.cfg, self.skill_file
        browser = (f"Follow {s(BROWSER_SKILL)} for anything that renders in a browser; the {BROWSER_SERVER} "
                   "MCP tools are enabled for you.\n") if cfg["browser"] else ""
        return f"""You are the verifier for epic #{self.a.parent}. The epic's code is checked out in this worktree
({wt}), and the app is running: the runner started it with `{cfg['serve']}`, and `{cfg['ready']}` succeeds.
Read AGENTS.md, then exercise every check in {self.checks_file()} against the running app: each ticket's
`## Verification` items (above all its Manual checks) and the parent issue's checkpoints. The runner
already ran the build and test commands; verify behavior by doing (call the endpoint, run the command,
inspect the output), not by reading code or re-running the test suite.
{browser}Report under `## RESULTS`, one line per check ID: VERIFIED C001, FAILED C001 or UNVERIFIABLE C001, then the evidence: the
exact command and the output that decides it. Use the IDs from checks.md, each exactly once. UNVERIFIABLE means this running app cannot exercise the
check (it needs production data, a third party, or a human judgement); say why. A check you could run
but did not is not UNVERIFIABLE.
Read-only: do not edit files, commit, change branches, push, or post comments; stop only processes you started.
Use only CommandCode for agent work; do not invoke codex or claude CLIs.
End with exactly one line: VERIFY=FAIL when any check is FAILED, otherwise VERIFY=PASS.
"""

    READ_ONLY = """Read-only: do not edit files, commit, change branches, push, or post comments.
Use only CommandCode for agent work; do not invoke codex or claude CLIs.
"""

    def epic_review_prompt(self, rv, r):
        """Round one: one reviewer, on its own, over the whole epic diff."""
        s, reviewers, base = self.skill_file, rv["reviewers"], self.data["_meta"]["epicBase"]
        reports = sorted(self.folder.glob("*/review/round-*/final.txt")) if self.osmani else []
        tickets = (f"Every ticket passed its own ticket review; the reports are:\n"
                   + "\n".join(f"  {path}" for path in reports) + "\nTheir DEFERRED findings are real defects that "
                   "no ticket owned: carry each one that still holds into FINDINGS as DEFERRED.\n") if reports else ""
        checked = ("Another reviewer reviews the same diff on its own; afterwards each of you checks the other's\n"
                   "findings against the code, so report only what you can point at.\n") if len(reviewers) > 1 else \
            "Afterwards you re-check each finding against the code, so report only what you can point at.\n"
        return f"""You are an epic reviewer for epic #{self.a.parent}. Someone else wrote this code; take none of its claims on trust.
Read AGENTS.md, the parent spec with gh issue view {self.a.parent}, and each ticket in {self.order}.
Review git diff {base}..{rv['start']} and git log {base}..{rv['start']} as a whole.
{self.domain()}Follow {s('code-review-and-quality')} across that whole diff; load {s('security-and-hardening')}
only when the epic touches auth, untrusted input or personal or sensitive data, and
{s('performance-optimization')} only when it touches a performance-sensitive area.
{tickets}{checked}Report under these markdown headings, in order:
## INTEGRATION: how the tickets fit together.
## EPIC SPEC: every parent requirement MET or UNMET, with evidence.
## CHECKPOINTS: every checkpoint in the parent plan MET or UNMET, with evidence.
## FINDINGS: one line per finding, numbered {r['letter']}1, {r['letter']}2 and so on, in exactly this shape:
- {r['letter']}1 BLOCKING path/to/file.ts:42 — what is wrong — the evidence (the code quoted, or a command and its output)
BLOCKING is a real correctness or security defect in this epic, a broken integration between tickets, or
an UNMET requirement or checkpoint. DEFERRED is a real defect the epic does not own: pre-existing code, or
out of the epic's scope. Style and taste are not findings. Write "None." when there are none.
{self.READ_ONLY}End with exactly one line: VERDICT=SHIP when nothing is BLOCKING, otherwise VERDICT=FIX-FIRST.
"""

    def check_prompt(self, rv, r, other, targets, report, follow=False):
        """Cross-examination of the other reviewer's findings, or a lone reviewer's check of its own."""
        ids = ", ".join(targets)
        rules = """Evidence is file:line with the code quoted, or a command and its output; a verdict without
evidence does not count.
"""
        if other:
            own = ", ".join(i for i, f in rv["findings"].items() if f["by"] == r["name"]) or "none"
            return f"""Cross-examination{' of the follow-up review' if follow else ''} for epic #{self.a.parent}. The other reviewer ({other['name']})
reviewed the same code on its own; its report is at {report}.
Check each of its findings against the code yourself: CONFIRMED when the code shows the problem is real
and correctly classified, DISPUTED when it does not. {rules}Answer each of these IDs exactly once: {ids}
## ANSWERS: one line per ID: - {targets[0]} CONFIRMED <evidence>, or - {targets[0]} DISPUTED <evidence>
## WITHDRAWN: any of your own findings ({own}) that its report shows you got wrong, one line each:
- {r['letter']}1 WITHDRAWN <why>. Write "None." when there are none.
{self.READ_ONLY}End with exactly one line: CHECK=DONE
"""
        return f"""Self-check for epic #{self.a.parent}: no second reviewer checks your findings, so re-check each one
against the code now: CONFIRMED when the code bears it out, WITHDRAWN when it does not. {rules}Answer each of these IDs exactly once: {ids}
## ANSWERS: one line per ID: - {targets[0]} CONFIRMED <evidence>, or - {targets[0]} WITHDRAWN <why>
{self.READ_ONLY}End with exactly one line: CHECK=DONE
"""

    def ticket_writer_prompt(self, rv, drafts, asked):
        lines = "\n".join(f"- {i} {f['class']} {f['text']}" + (f" (confirmed: {f['answer']})" if f.get("answer") else "")
                          for i, f in rv["findings"].items() if self.blocking(f))
        skill = self.skill_file(TICKET_WRITING_SKILLS[self.a.workflow])
        again = (f"You stopped earlier to ask a human. Read their answer in the latest comments of gh issue view "
                 f"{self.a.parent} --comments, then continue.\n") if asked else ""
        return f"""You are the ticket writer for epic #{self.a.parent}. The epic review confirmed these blocking findings; each needs a fix:
{lines}
Follow {skill} to group them into fix tickets: findings that one change fixes belong in one ticket, and
independent fixes go in separate tickets so they can run in parallel. This run is unattended: skip every
step that asks the user to approve, quiz or iterate, and ignore the skill's instructions on where to save or
publish tickets (no tasks/ files, no local issue files, no issue tracker, no gh issue create). Write each fix
ticket as its own file {drafts}/NN-short-slug.md, numbered from 01, and nothing else; the runner files them.
Each file has exactly these sections:
# <title>
## What to build
## Acceptance criteria
- [ ] <specific, testable condition>
## Verification
- [ ] <the focused test command, and any manual check>
## Files likely touched
- <path>
## Scope
XS, S or M
## Blocked by
None, or the numbers of the drafts that must land first (for example: 01)
## Findings
The finding IDs this ticket fixes (for example: D1, G2)
Every finding ID above appears in exactly one ticket's Findings. Keep each ticket to at most 8 files.
{self.domain()}{again}Do not edit any other file, commit, change branches, push, or post comments.
Use only CommandCode for agent work; do not invoke codex or claude CLIs.
End with exactly one line: STATUS=COMPLETE, or STATUS=NEEDS_HUMAN when a fix needs a human decision (say which).
"""

    def follow_up_prompt(self, rv, r):
        targets = [i for i, f in rv["findings"].items() if self.blocking(f)]
        used = [int(i[1:]) for i in rv["findings"] if i[0] == r["letter"]]
        tickets = "\n".join(f"  #{n} {self.tickets[n]['title']}" for n in rv["fixTickets"])
        return f"""Follow-up review for epic #{self.a.parent}. These fix tickets for the confirmed blocking findings have integrated:
{tickets}
Review only the fix diff: git diff {rv['start']}..{rv['fixHead']} and git log {rv['start']}..{rv['fixHead']}. The epic review so
far is at {self.folder / 'review' / 'report.md'}.
## FIXES: each of these confirmed findings exactly once: - {targets[0]} FIXED <evidence>, or - {targets[0]} NOT FIXED <evidence>.
IDs: {', '.join(targets)}
## FINDINGS: only problems the fix diff itself introduced, numbered on from {r['letter']}{max(used, default=0) + 1}, in the
same shape as before: BLOCKING for a defect the fixes caused, DEFERRED for anything else you notice.
Anything outside the fix diff is out of scope. Write "None." when there are none.
{self.READ_ONLY}End with exactly one line: FOLLOW-UP=DONE
"""

    def repair_review_prompt(self, wt, tip, vdir):
        return f"""You are the repair reviewer for epic #{self.a.parent}. The epic review said SHIP at {tip}; the verify stage
then failed, and its repair committed a fix in this worktree ({wt}). Review only that repair: git diff
{tip}..HEAD and git log {tip}..HEAD. The verify report is at {vdir / 'report.md'} and the repair agent's report
at {vdir / 'repair' / 'final.txt'}. Follow {self.skill_file('code-review-and-quality')}: is the fix correct, and
does it break anything the epic needs?
{self.domain()}## FINDINGS: one line per finding: - R1 BLOCKING path/to/file.ts:42 — what is wrong — the evidence.
Write "None." when there are none.
{self.READ_ONLY}End with exactly one line: VERDICT=PASS when nothing is BLOCKING, otherwise VERDICT=FAIL.
"""
