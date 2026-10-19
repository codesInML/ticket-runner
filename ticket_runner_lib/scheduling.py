"""Concurrent ticket scheduling and interruption handling."""

import os
import queue
import threading

from .display import say
from .github import ref
from .graph import classify, ready, render_graph
from .processes import attempt, command, kill
from .settings import BOLD, DIM, GREEN, YELLOW


class Scheduling:
    def preflight(self, selected):
        pending, external = [], {}
        for n in selected:
            record = self.data.get(str(n), {})
            branch, wt = self.branch(n), self.worktrees / str(n)
            if self.complete(n):
                say(f"#{n} complete — skipping", DIM)
                if record.get("status") == "done":
                    self.cleanup(n)
                elif not attempt(self.root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode and \
                        attempt(self.root, "merge-base", "--is-ancestor", branch, self.epic).returncode:
                    say(f"#{n} is closed, but {branch} never merged into {self.epic}; dependents will run "
                        "without its work", YELLOW, BOLD)
                continue
            if record.get("status") == "awaiting-human" and not (self.a.resume and self.a.only == n):
                say(f"#{n} awaiting a human — skipping; answer on the issue, then --resume --only {n}", YELLOW)
                self.parked.append(n)
                continue
            if self.a.resume and record.get("status") not in (None, "done") and "branch" not in record:
                raise RuntimeError(
                    f"#{n} was left {record.get('status')} by the previous in-place runner. Finish it with that "
                    f"version, or commit/stash its work and delete the \"{n}\" entry in {os.path.relpath(self.path)}.")
            leftover = wt.exists() or not attempt(self.root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode
            if leftover and not self.a.resume:
                raise RuntimeError(f"#{n} has a worktree or branch from an earlier run; continue it with --resume, "
                                   f"or remove it: git worktree remove {os.path.relpath(wt)}; git branch -D {branch}")
            unmet = list(self.tickets[n].get("foreign", []))
            for blocker in self.tickets[n]["blockers"]:
                if blocker in self.tickets:
                    continue
                if blocker not in external:
                    external[blocker] = command("gh", "issue", "view", str(blocker), "--json", "state",
                                                "-q", ".state") == "CLOSED"
                if not external[blocker]:
                    unmet.append(blocker)
            if unmet:
                say(f"#{n} deferred: unfinished blockers outside the epic: {' '.join(map(ref, unmet))}", YELLOW)
                self.deferred[n] = unmet
                continue
            pending.append(n)
        return pending

    def schedule(self, pending):
        done = {n for n in self.order if self.complete(n)}
        failed, changed = self.failed, True
        while True:
            if self.fatal:
                raise self.fatal
            for n in ready(pending, self.blockers, done, self.threads)[:self.a.jobs - len(self.threads)]:
                thread = threading.Thread(target=self.work, args=(n,), name=f"ticket-{n}", daemon=True)
                self.threads[n] = thread
                thread.start()
            if changed:
                self.show()
                changed = False
            if not self.threads:
                break
            try:
                n = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            self.threads.pop(n).join()
            pending.remove(n)
            if self.complete(n):
                done.add(n)
            else:
                failed.append(n)
            changed = True
        if self.fatal:
            raise self.fatal
        return self.summarize(pending, failed, done)

    def summarize(self, pending, failed, done):
        remaining = [n for n in self.order if not self.complete(n)]
        if failed or pending or self.parked or self.deferred:
            say("\nSummary", BOLD)
            for n in failed + self.parked:
                record = self.data.get(str(n), {})
                where = f"  {os.path.relpath(record['worktree'])}" if record.get("worktree") else ""
                detail = f"  {record['reason']}" if record.get("reason") else ""
                say(f"  #{n} {record.get('status')}{where}{detail}", YELLOW)
            for n in pending:
                waiting = [b for b in self.blockers[n] if b in self.tickets and b not in done]
                say(f"  #{n} held: waiting on {' '.join(f'#{b}' for b in waiting)}", DIM)
            for n, unmet in self.deferred.items():
                say(f"  #{n} deferred: needs {' '.join(map(ref, unmet))} closed outside the epic", DIM)
        if remaining and not self.a.only:
            say(f"Epic incomplete: {remaining}. The epic stages wait for it.", YELLOW)
            return 1
        if self.a.only and (failed or pending or self.parked or self.deferred):
            return 1
        if self.a.only:
            say("Ticket run finished; epic review, verify and ship skipped (--only).", GREEN)
            return 0
        return None

    def show(self):
        done = {n for n in self.order if self.complete(n)}
        stuck = self.failed + self.parked
        labels = {n: self.data.get(str(n), {}).get("status") for n in stuck}
        for n in self.threads:
            phase = self.data.get(str(n), {}).get("phase")
            labels[n] = {"integrate": "merging", "review": "reviewing", "fix": "fixing"}.get(phase, "implementing")
        labels.update({n: "needs " + " ".join(map(ref, unmet)) for n, unmet in self.deferred.items()})
        skipped = set(self.order) - set(self.selected) - done
        kinds = classify(self.blockers, done, self.threads, stuck, self.deferred, skipped)
        say("\n" + render_graph(self.levels, self.tickets, kinds, labels, self.spent_total()) + "\n")

    def stop(self):
        self.cancel_children()
        for thread in self.threads.values():
            if thread is not threading.current_thread():
                thread.join(timeout=30)
        with self.state:
            for n in self.threads:
                record = self.data.get(str(n), {})
                if record.get("status") in {"in-progress", "in-review", "reported", "error"}:
                    record["status"] = "interrupted"
            self.persist()
