"""Ticket implementation and per-ticket lifecycle."""

from pathlib import Path
import os

from .display import say
from .processes import attempt, dirty, git
from .settings import BOLD, Fatal, GREEN, RED, RETRYABLE, Stop, YELLOW
from .text import verdict


class TicketImplementation:
    def branch(self, n):
        return f"cc/{self.a.parent}/{n}"

    def work(self, n):
        record = self.record(n)
        try:
            tdir = self.folder / str(n)
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "ticket.md").write_text(f"# #{n} — {self.tickets[n]['title']}\n\n{self.tickets[n]['body']}")
            phase = record.get("phase") if self.continuing(n) else None
            if phase == "escalate":
                self.resume_escalation(n, record, tdir)
            elif phase not in ("review", "fix", "integrate"):
                self.implement(n, record, tdir)
            if self.osmani and (record.get("status") == "ready-to-review" or phase in ("review", "fix")):
                self.review_ticket(n, record, tdir)
            if record.get("status") == "ready-to-merge" or phase == "integrate":
                with self.integration:
                    self.integrate(n, record, tdir)
        except Stop:
            if record.get("status") in {"in-progress", "in-review", "reported", "error"}:
                self.update(record, status="interrupted")
        except Fatal as exc:
            self.fatal = exc
        except Exception as exc:
            status = "interrupted" if self.stopping.is_set() else "error"
            self.update(record, status=status, reason=str(exc))
            if status == "error":
                say(f"#{n} error: {exc}", RED, BOLD)
        finally:
            self.events.put(n)

    def fail(self, n, record, status, reason):
        self.update(record, status=status, reason=reason, drop=("unblocked",))
        self.nudge()
        say(f"#{n} {status}: {reason}", YELLOW, BOLD)

    def implement(self, n, record, tdir):
        branch, wt = self.branch(n), self.worktrees / str(n)
        answered = record.get("status") == "awaiting-human"
        session = record.get("sessionId") if self.continuing(n) and (
            record.get("status") in RETRYABLE or answered) else None
        with self.integration:
            if self.stopping.is_set():
                raise Stop()
            if not wt.exists():
                self.worktrees.mkdir(parents=True, exist_ok=True)
                tip = git(self.root, "rev-parse", self.epic)
                if attempt(self.root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode:
                    git(self.root, "worktree", "add", "-q", "-b", branch, str(wt), tip)
                    base = tip
                else:
                    git(self.root, "worktree", "add", "-q", str(wt), branch)
                    base = record.get("base") or git(self.root, "merge-base", branch, tip)
            else:
                base = record.get("base") or git(wt, "merge-base", "HEAD", self.epic)
            self.provision(wt)
        head = git(wt, "rev-parse", "HEAD")
        if session and record.get("head") != head:
            return self.fail(n, record, "needs-human", f"HEAD in {os.path.relpath(wt)} changed since the saved "
                             "session; reconcile it, then --resume")
        if dirty(wt) and not session:
            return self.fail(n, record, "needs-human", f"{os.path.relpath(wt)} is dirty without a resumable "
                             "session; commit or discard there, then --resume")
        self.update(record, status="in-progress", phase="implement", base=base, branch=branch,
                    worktree=str(wt), drop=() if session else ("sessionId", "reason"))
        self.nudge()
        color = self.color(n)
        say(f"\n━━ #{n}: {'resuming' if session else 'implementing'} with {self.a.model} "
            f"in {os.path.relpath(wt)} ━━", BOLD, color)
        say(f"#{n} {self.tickets[n]['title']}", BOLD, color)
        prompt = self.osmani_brief(n, tdir) if self.osmani else f"""Read AGENTS.md and {self.skill}, then follow the implementation workflow for issue #{n}.
Read the full ticket at {tdir / 'ticket.md'}. Satisfy its acceptance criteria without expanding scope.
Use TDD at appropriate seams; decide and document seams yourself in this unattended run.
Run typechecking, targeted tests, the full suite once, and the workflow's code review before finishing.
{self.verification_rule()}"""
        prompt += self.agent_rule("any agent/review work")
        prompt += f"""You are in a dedicated git worktree for this ticket. Other tickets of this epic run in parallel in
their own worktrees; the runner integrates them, so ignore them.
Commit buildable work onto the current branch {branch}, referencing #{n}. A ticket whose acceptance
criteria need no repository change (tracker-only work, or work already on the epic branch) makes no
commit and still ends STATUS=COMPLETE.
Do not push, open PRs, change branches, rebase, merge, or add co-author trailers. Do not modify the
runner, {self.hands_off}, agent configuration, hooks, or anything under {self.folder}. Existing partial
edits/commits from this ticket are yours to finish when resuming. Pre-ticket commit: {base}.
If an acceptance criterion requires a human decision, commit all buildable work and report it.
End with exactly one line: STATUS=COMPLETE, STATUS=NEEDS_HUMAN, or STATUS=BLOCKED.
"""
        if answered:
            prompt += (f"\nYou stopped earlier to ask a human. Read their answer in the latest comments of "
                       f"gh issue view {n} --comments, then continue.\n")
        text = self.run_agent(self.a.model, self.a.effort, self.a.max_turns, prompt, tdir, record,
                              session, cwd=wt, tag=f"#{n}", color=color)
        self.guard()
        if record["status"] == "reported" and verdict(text) == "MISSING" and record.get("sessionId"):
            # One nudge into the same session; its own folder keeps the full report in final.txt.
            say(f"#{n} ended without a STATUS line; asking its session once for it", YELLOW)
            nudge = """Your final message did not end with the required STATUS line. Change nothing; reply with
exactly one line for the work as it stands: STATUS=COMPLETE, STATUS=NEEDS_HUMAN, or STATUS=BLOCKED.
"""
            text = self.run_agent(self.a.model, self.a.effort, self.a.max_turns, nudge, tdir / "status", record,
                                  record["sessionId"], cwd=wt, tag=f"#{n}", color=color)
            self.guard()
        if record["status"] == "budget-cap":
            return self.fail(n, record, "budget-cap", f"partial work kept in {os.path.relpath(wt)}; the budget ran "
                             "out, not the ticket: --resume continues it with a fresh budget")
        if record["status"] != "reported":
            return self.fail(n, record, record["status"], f"partial work kept in {os.path.relpath(wt)}; retry with "
                             f"--resume --max-turns {self.a.max_turns * 2}")
        status = verdict(text)
        commits = int(git(wt, "rev-list", "--count", f"{base}..HEAD"))
        problem = self.check_worktree(wt, branch)
        complete = status == "COMPLETE" and not dirty(wt) and not problem
        if complete:
            self.implemented(n, record)
        elif not problem and self.can_escalate(record, "implement"):
            why = f"its implementer ({self.a.model}) ended status={status}" + (", with uncommitted changes" if dirty(wt) else "")
            reports = [tdir / "final.txt", *([tdir / "status" / "final.txt"] if (tdir / "status").is_dir() else [])]
            if self.escalate(n, record, tdir, "implement", why, reports):
                self.escalated(n, record, tdir, "implement")
        elif status == "NEEDS_HUMAN" and not dirty(wt) and not problem:
            self.fail(n, record, "awaiting-human", f"agent needs a decision; see {tdir / 'final.txt'}")
        else:
            self.fail(n, record, "needs-human", problem or f"status={status}, commits={commits}, "
                      f"dirty={dirty(wt)}; see {tdir}")

    def implemented(self, n, record):
        """A complete ticket moves on: to ticket review (osmani), to integration, or straight to done without commits."""
        wt, color = Path(record["worktree"]), self.color(n)
        commits = int(git(wt, "rev-list", "--count", f"{record['base']}..HEAD"))
        if not commits:
            with self.integration:
                self.finish(n, record, git(self.tree, "rev-parse", "HEAD"))
                say(f"#{n} complete with no commits; nothing to review or integrate", GREEN, BOLD)
                self.cleanup(n)
        elif self.osmani:
            self.update(record, status="ready-to-review", phase="review", drop=("reason",))
            say(f"#{n} implemented ({commits} commit{'s' * (commits != 1)}); handing to ticket review", BOLD, color)
        else:
            self.update(record, status="ready-to-merge", phase="integrate", drop=("reason",))
            say(f"#{n} implemented ({commits} commit{'s' * (commits != 1)}); waiting to integrate", BOLD, color)

    def can_escalate(self, record, kind):
        """Each kind of stuck (implement, review) escalates once per ticket, and only when a model is set."""
        return bool(self.a.escalate_model) and kind not in record.get("escalation", {})

    def escalate(self, n, record, tdir, kind, why, reports, **review):
        """One session on the escalation model before a stuck ticket reaches a human; True when it completes."""
        branch, wt, color, model = self.branch(n), Path(record["worktree"]), self.color(n), self.a.escalate_model
        answered = record.get("status") == "awaiting-human"
        with self.state:
            rec = record.setdefault("escalation", {}).setdefault(kind, {})
        head = git(wt, "rev-parse", "HEAD")
        session = rec.get("sessionId") if self.continuing(n) and (rec.get("status") in RETRYABLE or answered) \
            and rec.get("head") == head else None
        self.update(rec, why=why, reports=[str(r) for r in reports], **review, drop=() if session else ("sessionId",))
        self.update(record, status="in-progress", phase="escalate", escalating=kind)
        say(f"\n━━ #{n}: {why}; {'resuming' if session else 'escalating to'} {model} ━━", BOLD, color)
        prompt = self.escalate_prompt(n, tdir, branch, record["base"], kind, why, reports)
        if answered:
            prompt += (f"\nYou stopped earlier to ask a human. Read their answer in the latest comments of "
                       f"gh issue view {n} --comments, then continue.\n")
        folder = tdir / "escalate" / kind
        text = self.run_agent(model, self.a.escalate_effort, self.a.escalate_max_turns, prompt, folder, rec, session,
                              cwd=wt, tag=f"#{n} escalate", color=color, stage="escalate")
        self.guard()
        if rec["status"] != "reported":
            self.fail(n, record, rec["status"], f"escalation to {model} ended {rec['status']}; partial work kept in "
                      f"{os.path.relpath(wt)}; --resume continues it")
            return False
        status = verdict(text)
        problem = "uncommitted changes remain" if dirty(wt) else self.check_worktree(wt, branch)
        if status == "NEEDS_HUMAN" and not problem:
            self.fail(n, record, "awaiting-human", f"{model} needs a decision; see {folder / 'final.txt'}")
            return False
        if status != "COMPLETE" or problem:
            self.fail(n, record, "needs-human", f"escalation to {model} ended status={status}"
                      f"{'; ' + problem if problem else ''}; see {folder}")
            return False
        say(f"#{n} {model} unblocked it", GREEN, BOLD)
        return True

    def escalated(self, n, record, tdir, kind):
        """After an escalation (or a person's fix in its worktree): back to the stage it left."""
        self.update(record, drop=("escalating", "reason"))
        if kind == "implement":
            return self.implemented(n, record)
        esc = record["escalation"]["review"]
        with self.state:
            rec = record.setdefault("ticketReview", {})
        self.update(rec, round=esc["nextRound"], previousReport=esc["previousReport"],
                    fixReport=str(tdir / "escalate" / "review" / "final.txt"), drop=("sessionId",))
        self.update(record, status="ready-to-review", phase="review")

    def resume_escalation(self, n, record, tdir):
        kind = record.get("escalating", "implement")
        rec = record.get("escalation", {}).get(kind, {})
        if record.get("status") == "needs-human":
            return self.escalated(n, record, tdir, kind)  # a person finished it in the worktree
        extra = {k: rec[k] for k in ("nextRound", "previousReport") if k in rec}
        if self.escalate(n, record, tdir, kind, rec.get("why", "an earlier escalation was interrupted"),
                         [Path(p) for p in rec.get("reports", [])], **extra):
            self.escalated(n, record, tdir, kind)
