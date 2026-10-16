# Ticket runner

Runs a GitHub epic's sub-tickets in parallel through coding agents, then takes the integrated epic through
its review, verification and shipping before a person merges it.

## Work

**Epic**:
The parent GitHub issue and the branch its tickets integrate into; the unit the runner reviews, verifies and
ships.
_Avoid_: Spec (for the branch), feature

**Ticket**:
A sub-issue of the epic that one agent implements in its own worktree and the runner integrates into the
epic branch.
_Avoid_: Task, slice, sub-ticket (in prose)

**Fix ticket**:
A ticket the epic review files to fix its confirmed blocking findings; it runs through the same pipeline as
any other ticket.
_Avoid_: Repair ticket, follow-up ticket

**Workflow**:
The skill set an epic's prompts follow: pocock or osmani.

**Backend**:
The agent CLI and account every session of a run uses: commandcode, or claude-api (Claude Code billed to a
company Anthropic API key).
_Avoid_: Engine, harness, provider

## Stages

**Epic stages**:
Everything after the tickets: epic review, verify and ship, each pinned to the epic head it judged.
_Avoid_: Final stages, post-ticket stages

**Ticket review**:
A read-only review of one ticket's diff by a different model family, before it integrates (osmani only). On
the claude-api backend it is a different model of the same family.

**Epic review**:
The review of the whole epic diff by one or two reviewers, ending in SHIP or in fix tickets.
_Avoid_: Final review

**Cross-examination**:
The epic review round in which each reviewer answers every one of the other reviewer's findings with
evidence; with one reviewer, the reviewer re-checks its own.

**Follow-up review**:
The epic review round after the fix tickets integrate: it reads only the fix diff and marks each confirmed
finding fixed or not.

**Ticket writer**:
The epic reviewer that turns confirmed blocking findings into fix ticket drafts; the runner files them.

**Verify**:
The stage that runs the project's verify commands and runtime checks against the epic tip.

**Verify repair**:
The single agent that fixes a failed verify in the verify worktree.

**Repair review**:
A one-reviewer review of only the verify repair's diff, extending the epic review's SHIP to the repaired
head.

**Launch plan**:
The runner-pasted section of the pull request covering the pre-launch checklist, risk and rollback.

**Ship**:
The stage that writes the launch plan, pushes the epic branch and opens its pull request.

## Findings

**Finding**:
One problem an epic reviewer reports, with a stable ID (such as D1 or G2) and a file and line.

**Confirmed**:
A finding the other reviewer (or, alone, the reviewer itself) backed with evidence; a confirmed blocking
finding becomes part of a fix ticket.

**Disputed**:
A finding the other reviewer rejected with evidence; it never blocks and is reported for a person to read.

**Deferred**:
A real defect the epic does not own; it never blocks and is posted on the epic.

**Withdrawn**:
A finding its own reviewer retracted during cross-examination.
