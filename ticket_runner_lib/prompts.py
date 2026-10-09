"""Skill guidance and stage prompt construction."""

from .settings import BROWSER_SERVER, BROWSER_SKILL


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

    def epic_brief(self):
        s = self.skill_file
        reports = sorted(self.folder.glob("*/review/round-*/final.txt"))
        listed = "\n".join(f"  {r}" for r in reports) or "  (none found)"
        return f"""{self.domain()}Follow {s('code-review-and-quality')} across that whole diff; load {s('security-and-hardening')}
only when the epic touches auth, untrusted input or personal or sensitive data, and
{s('performance-optimization')} only when it touches a performance-sensitive area.
Every ticket passed its own ticket review; the reports are:
{listed}
Their DEFERRED findings are real defects that no ticket owned.
Report under these markdown headings, in order, citing file and line for each actionable finding:
## INTEGRATION: how the slices fit together.
## EPIC SPEC: every parent requirement MET or UNMET, with evidence.
## CHECKPOINTS: every checkpoint in the parent plan MET or UNMET, with evidence.
## DEFECTS: real correctness and security defects.
## DEFERRED: the ticket reviews' DEFERRED findings consolidated. Merge duplicates, drop any the epic has
since fixed, confirm the rest still hold, and add any of your own that no ticket owns. Write "None." when
nothing remains. The runner posts this section on #{self.a.parent} as written.
Read-only review: do not edit files, commit, change branches, push, or post comments.
Use only CommandCode for agent work.
"""
