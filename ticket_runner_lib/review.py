"""Ticket review sessions and their one fix round."""

from pathlib import Path
import os

from .display import say
from .processes import dirty, git
from .settings import BOLD, GREEN, RETRYABLE, YELLOW
from .text import verdict

VERDICT = r"VERDICT=(PASS|FAIL)"


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
        if self.a.resume and record.get("status") == "needs-human" and rec.get("status") == "fail":
            # A person fixed the last review's findings in the worktree: the next round re-reviews against them.
            last = rec.get("round", 1)
            self.update(rec, round=last + 1, previousReport=str(tdir / "review" / f"round-{last}" / "final.txt"),
                        fixReport="", reviewedHead=rec.get("reviewedHead") or rec.get("head"), drop=("sessionId",))
        while True:
            round_, model = rec.get("round", 1), rec.get("model") or self.reviewer_for(n)
            head = git(wt, "rev-parse", "HEAD")
            session = rec.get("sessionId") if self.a.resume and rec.get("status") in RETRYABLE \
                and rec.get("head") == head else None
            since = rec.get("reviewedHead") if round_ > 1 else None
            self.update(record, status="in-review", phase="review")
            self.update(rec, round=round_, model=model, drop=() if session else ("sessionId",))
            say(f"\n━━ #{n}: ticket review round {round_} with {model} ━━", BOLD, color)
            folder = tdir / "review" / f"round-{round_}"
            text = self.run_agent(model, self.a.ticket_review_effort, self.a.ticket_review_max_turns,
                                  self.review_prompt(n, tdir, base, round_, rec.get("previousReport"),
                                                     rec.get("fixReport"), since), folder, rec, session, cwd=wt,
                                  tag=f"#{n} review", color=color, stage="ticket review")
            self.guard()
            if rec["status"] == "reported" and verdict(text, VERDICT) == "MISSING" and rec.get("sessionId"):
                # One nudge into the same session; its own folder keeps the full report in final.txt.
                say(f"#{n} ticket review ended without a VERDICT line; asking its session once for it", YELLOW)
                nudge = """Your final message did not end with the required VERDICT line. Change nothing; reply with
exactly one line for your review as it stands: VERDICT=PASS when nothing is BLOCKING, otherwise VERDICT=FAIL.
"""
                text = self.run_agent(model, self.a.ticket_review_effort, self.a.ticket_review_max_turns, nudge,
                                      folder / "verdict", rec, rec["sessionId"], cwd=wt, tag=f"#{n} review",
                                      color=color, stage="ticket review")
                self.guard()
            if dirty(wt) or git(wt, "rev-parse", "HEAD") != head or self.check_worktree(wt, branch):
                return self.fail(n, record, "needs-human", f"the ticket review changed {os.path.relpath(wt)}, "
                                 "but it must be read-only; inspect the worktree, then --resume")
            if rec["status"] != "reported":
                return self.fail(n, record, rec["status"], f"ticket review ended {rec['status']}; "
                                 f"--resume retries it (logs in {folder})")
            result = verdict(text, VERDICT)
            self.update(rec, status=result.lower(), reviewedHead=head)
            if result == "PASS":
                self.update(record, status="ready-to-merge", phase="integrate", drop=("reason",))
                return say(f"#{n} passed ticket review round {round_}; waiting to integrate", GREEN, BOLD)
            if result != "FAIL":
                return self.fail(n, record, "needs-human", f"ticket review gave no verdict; see {folder / 'final.txt'}")
            if round_ > 1 and self.can_escalate(record, "review"):
                fixed = Path(rec.get("fixReport") or tdir / "fix" / "final.txt")
                if not self.escalate(n, record, tdir, "review", "its ticket review still found blocking problems "
                                     "after a fix round", [folder / "final.txt", fixed], nextRound=round_ + 1,
                                     previousReport=str(folder / "final.txt")):
                    return
                self.escalated(n, record, tdir, "review")
                continue
            if round_ > 1:
                return self.fail(n, record, "needs-human", "ticket review still blocking after one fix round"
                                 f"{' and an escalation' if 'review' in record.get('escalation', {}) else ''}; "
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
                              record.get("sessionId"), cwd=wt, tag=f"#{n}", color=color, stage="fix")
        self.guard()
        if record["status"] != "reported":
            self.fail(n, record, record["status"], f"fix round ended {record['status']}; partial work kept in "
                      f"{os.path.relpath(wt)}; --resume continues it")
            return False
        status = verdict(text)
        problem = "uncommitted changes remain" if dirty(wt) else self.check_worktree(wt, branch)
        if status != "COMPLETE" and not self.check_worktree(wt, branch) and self.can_escalate(record, "review"):
            if not self.escalate(n, record, tdir, "review", f"its fix round ({self.a.model}) ended status={status}",
                                 [report, tdir / "fix" / "final.txt"], nextRound=2, previousReport=str(report)):
                return False
            self.escalated(n, record, tdir, "review")
            return True
        if status == "NEEDS_HUMAN" and not problem:
            self.fail(n, record, "awaiting-human", f"agent needs a decision; see {tdir / 'fix' / 'final.txt'}")
            return False
        if status != "COMPLETE" or problem:
            self.fail(n, record, "needs-human", f"fix round ended status={status}"
                      f"{'; ' + problem if problem else ''}; see {tdir / 'fix'}")
            return False
        self.update(record, status="ready-to-review", phase="review")
        return True
