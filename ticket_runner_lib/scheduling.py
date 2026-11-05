"""Concurrent ticket scheduling and interruption handling."""

import os
import queue
import threading
import time

from .display import say
from .github import ref
from .graph import classify, ready, render_graph
from .processes import attempt, command, kill
from .settings import BOLD, DIM, GREEN, YELLOW
from .sync import WAITING, seconds


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
            if record.get("status") in WAITING and not record.get("unblocked"):
                if not (self.a.resume and self.a.only == n):
                    say(f"#{n} {record['status']} — waiting for needs-human to come off the issue; "
                        f"--resume --only {n} continues it now", YELLOW)
                    self.parked.append(n)
                    continue
                self.update(record, unblocked=True)  # The operator named this ticket: its label no longer decides.
            if self.a.resume and record.get("status") not in (None, "done") and "branch" not in record:
                raise RuntimeError(
                    f"#{n} was left {record.get('status')} by the previous in-place runner. Finish it with that "
                    f"version, or commit/stash its work and delete the \"{n}\" entry in {os.path.relpath(self.path)}.")
            leftover = wt.exists() or not attempt(self.root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode
            if leftover and not self.continuing(n):
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
        self.watching = self.stuck()
        return pending

    def schedule(self, pending):
        done = {n for n in self.order if self.complete(n)}
        failed, changed = self.failed, True
        if self.no_wait:  # Nothing will wait for the sync, so what GitHub says now has to count at once.
            self.sync_tick(watch=True)
        while True:
            if self.fatal:
                raise self.fatal
            changed = self.release(pending, done) or changed
            for n in ready(pending, self.blockers, done, self.threads)[:self.a.jobs - len(self.threads)]:
                thread = threading.Thread(target=self.work, args=(n,), name=f"ticket-{n}", daemon=True)
                self.threads[n] = thread
                thread.start()
            if changed:
                self.show()
                changed = False
            self.watching = self.stuck()
            if not self.threads:
                if self.no_wait or not self.watching:
                    break
                self.remind()
            try:
                n = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            self.threads.pop(n).join()
            pending.remove(n)
            self.nudge()
            if self.complete(n):
                done.add(n)
            else:
                failed.append(n)
            changed = True
        if self.fatal:
            raise self.fatal
        return self.summarize(pending, failed, done)

    def stuck(self):
        """The tickets only a person can move, which a removed label or a closed issue sets free."""
        return {n for n in [*self.failed, *self.parked] if self.data.get(str(n), {}).get("status") in WAITING}

    def release(self, pending, done):
        moved = False
        for n in sorted(self.stuck()):
            closed = self.tickets[n]["state"] == "CLOSED"
            if not closed and not self.data[str(n)].get("unblocked"):
                continue
            for group in (self.failed, self.parked):
                if n in group:
                    group.remove(n)
            if closed:
                done.add(n)
                say(f"#{n} was closed on GitHub; no longer waiting on it", YELLOW)
            else:
                pending.append(n)
                say(f"#{n} unblocked; continuing it", GREEN, BOLD)
            moved = True
        return moved

    def remind(self):
        """Say what is waiting on a person when that changes, and every so often while it does not."""
        waiting = sorted(self.watching)
        hold = self.data.get("_hold")
        confirmed = self.github().get("labels", {})
        subjects = [*waiting, *([self.a.parent] if hold else [])]
        shown = [n for n in subjects if confirmed.get(str(n)) == "needs-human"]
        if len(shown) < len(subjects) and not self.sync_down and \
                not any(f"label:{n}" in self.sync_problems for n in subjects):
            return  # The label is on its way; say what to do once there is a label to remove.
        key = (tuple(subjects), tuple(shown))
        last_key, last_time = getattr(self, "reminded", (None, 0))
        if key == last_key and time.monotonic() - last_time < seconds("TICKET_RUNNER_REMIND_SECONDS", 1800):
            return
        self.reminded = key, time.monotonic()
        say("\nWaiting on you. Ctrl-C exits safely, and --resume continues later.", BOLD)
        for n in waiting:
            record = self.data[str(n)]
            say(f"  #{n} {record['status']}: {record.get('reason', '')}", YELLOW)
            self.label_hint(n, confirmed, f"--resume --only {n}")
        if hold:
            say(f"  Epic #{self.a.parent}: {hold['reason']}", YELLOW)
            self.label_hint(self.a.parent, confirmed, "--epic-only --force")

    def label_hint(self, n, confirmed, force):
        if confirmed.get(str(n)) == "needs-human":
            return say(f"    Remove needs-human from #{n} to continue.", DIM)
        say(f"    GitHub has not confirmed the needs-human label on #{n} yet. Add it by hand and remove it to continue, "
            f"or press Ctrl-C and use {force}.", DIM)

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
            labels[n] = {"integrate": "merging", "review": "reviewing", "fix": "fixing", "escalate": "escalating"}.get(phase, "implementing")
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
