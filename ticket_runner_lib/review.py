"""Ticket and epic review sessions and deferred findings."""

from pathlib import Path
import os
import re
import subprocess

from .display import say
from .processes import dirty, git
from .settings import BLUE, BOLD, DIM, GREEN, RED, RETRYABLE, YELLOW
from .text import section, verdict


class Reviews:
    def reviewer_for(self, n):
        # Even tickets get the first model, odd the second: balanced, and readable from the number.
        pool = self.a.ticket_review_models
        return pool[n % len(pool)]

    def review_ticket(self, n, record, tdir):
        branch, wt, base, color = self.branch(n), Path(record["worktree"]), record["base"], self.color(n)
        with self.state:
            rec = record.setdefault("ticketReview", {})
        if record.get("phase") == "fix" and not self.fix(n, record, rec, tdir):
            return
        while True:
            round_, model = rec.get("round", 1), rec.get("model") or self.reviewer_for(n)
            head = git(wt, "rev-parse", "HEAD")
            session = rec.get("sessionId") if self.a.resume and rec.get("status") in RETRYABLE \
                and rec.get("head") == head else None
            self.update(record, status="in-review", phase="review")
            self.update(rec, round=round_, model=model, drop=() if session else ("sessionId",))
            say(f"\n━━ #{n}: ticket review round {round_} with {model} ━━", BOLD, color)
            folder = tdir / "review" / f"round-{round_}"
            text = self.run_agent(model, self.a.ticket_review_effort, self.a.ticket_review_max_turns,
                                  self.review_prompt(n, tdir, base, round_), folder, rec, session, cwd=wt,
                                  tag=f"#{n} review", color=color)
            self.guard()
            if dirty(wt) or git(wt, "rev-parse", "HEAD") != head or self.check_worktree(wt, branch):
                return self.fail(n, record, "needs-human", f"the ticket review changed {os.path.relpath(wt)}, "
                                 "but it must be read-only; inspect the worktree, then --resume")
            if rec["status"] != "reported":
                return self.fail(n, record, rec["status"], f"ticket review ended {rec['status']}; "
                                 f"--resume retries it (logs in {folder})")
            result = verdict(text, r"VERDICT=(PASS|FAIL)")
            self.update(rec, status=result.lower())
            if result == "PASS":
                self.update(record, status="ready-to-merge", phase="integrate", drop=("reason",))
                return say(f"#{n} passed ticket review round {round_}; waiting to integrate", GREEN, BOLD)
            if result != "FAIL":
                return self.fail(n, record, "needs-human", f"ticket review gave no verdict; see {folder / 'final.txt'}")
            if round_ > 1:
                return self.fail(n, record, "needs-human", "ticket review still blocking after one fix round; "
                                 f"see {folder / 'final.txt'}; fix it in the worktree, then --resume re-reviews")
            say(f"#{n} ticket review found blocking problems; one fix round", YELLOW, BOLD)
            self.update(record, status="in-progress", phase="fix")
            self.update(rec, round=2, drop=("sessionId",))
            if not self.fix(n, record, rec, tdir):
                return

    def fix(self, n, record, rec, tdir):
        """One fix round in the implementer's own session, scoped to the ticket review's BLOCKING list."""
        branch, wt, color = self.branch(n), Path(record["worktree"]), self.color(n)
        answered = record.get("status") == "awaiting-human"
        report = tdir / "review" / "round-1" / "final.txt"
        say(f"#{n} fixing ticket review findings with {self.a.model}", BOLD, color)
        prompt = f"""A separate ticket reviewer found blocking problems in your work on #{n}; read the report at
{report}. Fix every finding under BLOCKING and every UNMET acceptance criterion, and nothing else: DEFERRED
and REJECTED findings are not this ticket's to act on. Follow {self.skill_file('test-driven-development')}
for each fix that changes behavior. If you disagree with a finding, say why in your final report; the same
reviewer re-reviews once. Run typechecking, the focused tests, and the full suite once, then commit onto
{branch} referencing #{n}. Do not push, rebase, merge, change branches, or add co-author trailers. Do not
modify the runner, {self.hands_off}, agent configuration, hooks, or anything under {self.folder}.
End with exactly one line: STATUS=COMPLETE, STATUS=NEEDS_HUMAN, or STATUS=BLOCKED.
"""
        if answered:
            prompt += (f"\nYou stopped earlier to ask a human. Read their answer in the latest comments of "
                       f"gh issue view {n} --comments, then continue.\n")
        text = self.run_agent(self.a.model, self.a.effort, self.a.max_turns, prompt, tdir / "fix", record,
                              record.get("sessionId"), cwd=wt, tag=f"#{n}", color=color)
        self.guard()
        if record["status"] != "reported":
            self.fail(n, record, record["status"], f"fix round ended {record['status']}; partial work kept in "
                      f"{os.path.relpath(wt)}; --resume continues it")
            return False
        status = verdict(text)
        problem = "uncommitted changes remain" if dirty(wt) else self.check_worktree(wt, branch)
        if status == "NEEDS_HUMAN" and not problem:
            self.fail(n, record, "awaiting-human", f"agent needs a decision; see {tdir / 'fix' / 'final.txt'}")
            return False
        if status != "COMPLETE" or problem:
            self.fail(n, record, "needs-human", f"fix round ended status={status}"
                      f"{'; ' + problem if problem else ''}; see {tdir / 'fix'}")
            return False
        self.update(record, status="ready-to-review", phase="review")
        return True

    def evidence(self, head):
        """The verify head the epic review saw: the head itself when verify passed there, else None."""
        verify = self.data.get("_verify", {})
        return head if not self.a.no_verify and verify.get("status") == "pass" and verify.get("head") == head \
            else None

    def review_valid(self, review, head):
        # A verdict holds for its head, workflow and evidence; with verify skipped now, any evidence will do.
        return review.get("status") in ("ship", "fix-first") and review.get("head") == head and \
            review.get("workflow") == self.a.workflow and \
            (self.a.no_verify or review.get("verified") == self.evidence(head))

    def review(self):
        a = self.a
        if dirty(self.root):
            raise RuntimeError("Epic review requires a clean tree")
        base = self.data["_meta"]["epicBase"]
        head = git(self.root, "rev-parse", "HEAD")
        review = self.data.setdefault("_review", {})
        if self.review_valid(review, head):
            say(f"Epic review already said {review['status'].upper()} at {head[:8]}; skipping. "
                f"Report: {self.folder / 'review/final.txt'}", GREEN if review["status"] == "ship" else RED, BOLD)
            return 0 if review["status"] == "ship" else 1
        session = review.get("sessionId") if a.resume and review.get("status") in RETRYABLE and review.get("head") == head else None
        self.update(review, status="in-progress", head=head, workflow=a.workflow, verified=self.evidence(head),
                    drop=() if session else ("sessionId",))
        say(f"\n━━ Epic review of #{a.parent} with {a.final_model} ━━", BOLD, BLUE)
        checks = self.epic_brief() if self.osmani else """Check integration between slices, every parent requirement (MET/UNMET with evidence),
and real correctness/security defects. Cite file and line for each actionable finding.
Read-only review: do not edit files, commit, change branches, push, or post comments.
Use only CommandCode for agent work. Report INTEGRATION, EPIC SPEC, DEFECTS, and VERDICT.
"""
        report = self.folder / "verify" / "report.md"
        evidence = (f"The verify stage's report is evidence for your review: {report}\n" if not a.no_verify
                    and report.exists() else "The verify stage was skipped for this run (--no-verify).\n")
        text = self.run_agent(a.final_model, a.final_effort, a.final_max_turns, f"""You are the final reviewer for epic #{a.parent}.
Read AGENTS.md, the parent spec with gh issue view {a.parent}, and each ticket in {self.order}.
Review git diff {base}..{head} and git log {base}..{head} as a whole.
{evidence}{checks}End with exactly one line: VERDICT=SHIP or VERDICT=FIX-FIRST.
""", self.folder / "review", review, session)
        self.guard()
        if dirty(self.root) or git(self.root, "rev-parse", "HEAD") != head:
            self.update(review, status="modified-tree")
        elif review["status"] == "reported":
            self.update(review, status="ship" if text.strip().splitlines()[-1] == "VERDICT=SHIP" else "fix-first")
            if self.osmani:
                self.post_deferred(text, review)
        print(text)
        say(f"Epic review: {review['status']}. Report: {self.folder / 'review/final.txt'}",
            GREEN if review["status"] == "ship" else RED, BOLD)
        return 0 if review["status"] == "ship" else 1

    def post_deferred(self, text, review):
        """The runner, not the reviewer, posts the epic's consolidated DEFERRED findings: once per reviewed head."""
        found = section(text, "DEFERRED")
        if not found or re.fullmatch(r"none\.?", found, re.I) or review.get("deferredPosted") == review["head"]:
            return
        body = self.folder / "review" / "deferred.md"
        body.write_text(f"Deferred findings from the epic review of #{self.a.parent} at `{review['head'][:8]}`: "
                        f"real defects that no ticket in this epic owned.\n\n{found}\n")
        result = subprocess.run(["gh", "issue", "comment", str(self.a.parent), "--body-file", str(body)],
                                capture_output=True, text=True)
        if result.returncode:
            return say(f"Warning: could not post deferred findings on #{self.a.parent} ({result.stderr.strip()}); "
                       f"they are in {body}", YELLOW, err=True)
        self.update(review, deferredPosted=review["head"])
        say(f"Posted the epic's deferred findings on #{self.a.parent}", DIM)
