"""Ticket implementation and per-ticket lifecycle."""

import os

from .display import say
from .processes import attempt, dirty, git
from .settings import BOLD, Fatal, RED, RETRYABLE, Stop, YELLOW
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
            phase = record.get("phase") if self.a.resume else None
            if phase not in ("review", "fix", "integrate"):
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
        self.update(record, status=status, reason=reason)
        self.label(n, "needs-human" if status in {"needs-human", "awaiting-human"} else "ready-for-agent",
                   "in-progress")
        say(f"#{n} {status}: {reason}", YELLOW, BOLD)

    def implement(self, n, record, tdir):
        branch, wt = self.branch(n), self.worktrees / str(n)
        answered = record.get("status") == "awaiting-human"
        session = record.get("sessionId") if self.a.resume and (
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
        self.label(n, "in-progress", "ready-for-agent")
        color = self.color(n)
        say(f"\n━━ #{n}: {'resuming' if session else 'implementing'} with {self.a.model} "
            f"in {os.path.relpath(wt)} ━━", BOLD, color)
        say(f"#{n} {self.tickets[n]['title']}", BOLD, color)
        prompt = self.osmani_brief(n, tdir) if self.osmani else f"""Read AGENTS.md and {self.skill}, then follow the implementation workflow for issue #{n}.
Read the full ticket at {tdir / 'ticket.md'}. Satisfy its acceptance criteria without expanding scope.
Use TDD at appropriate seams; decide and document seams yourself in this unattended run.
Run typechecking, targeted tests, the full suite once, and the workflow's code review before finishing.
"""
        prompt += f"""Use only CommandCode for any agent/review work; do not invoke codex or claude CLIs.
You are in a dedicated git worktree for this ticket. Other tickets of this epic run in parallel in
their own worktrees; the runner integrates them, so ignore them.
Commit buildable work onto the current branch {branch}, referencing #{n}.
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
        if record["status"] != "reported":
            return self.fail(n, record, record["status"], f"partial work kept in {os.path.relpath(wt)}; retry with "
                             f"--resume --max-turns {self.a.max_turns * 2}")
        status = verdict(text)
        commits = int(git(wt, "rev-list", "--count", f"{base}..HEAD"))
        problem = self.check_worktree(wt, branch)
        if status == "COMPLETE" and commits and not dirty(wt) and not problem and self.osmani:
            self.update(record, status="ready-to-review", phase="review", drop=("reason",))
            say(f"#{n} implemented ({commits} commit{'s' * (commits != 1)}); handing to ticket review", BOLD, color)
        elif status == "COMPLETE" and commits and not dirty(wt) and not problem:
            self.update(record, status="ready-to-merge", phase="integrate", drop=("reason",))
            say(f"#{n} implemented ({commits} commit{'s' * (commits != 1)}); waiting to integrate", BOLD, color)
        elif status == "NEEDS_HUMAN" and not dirty(wt) and not problem:
            self.fail(n, record, "awaiting-human", f"agent needs a decision; see {tdir / 'final.txt'}")
        else:
            self.fail(n, record, "needs-human", problem or f"status={status}, commits={commits}, "
                      f"dirty={dirty(wt)}; see {tdir}")
