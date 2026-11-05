"""Keeping GitHub in step with the run's saved state.

The saved state already says what GitHub should show, so nothing is queued: each pass compares what should be
there with what GitHub last confirmed and sends the difference. Only the current status is ever sent, a restart
needs nothing special, and a person's edit on GitHub is never treated as a mistake to undo.
"""

import hashlib
import json
import os
import threading
import time

from .display import dollars, say
from .github import LABELS, STATUS_LABELS, gh, missing_labels, transient
from .settings import DIM, FIX_LABEL, YELLOW


# A status of the ticket's saved record that a person, or --resume, has to act on before it runs again.
STOPPED = {"turn-cap", "budget-cap", "provider-error", "no-output", "interrupted", "error"}
WAITING = {"needs-human", "awaiting-human"}


def seconds(name, default):
    return float(os.getenv(name) or default)


class GitHubSync:
    def sync_setup(self):
        self.poll = seconds("TICKET_RUNNER_POLL_SECONDS", 30)
        self.flush_limit = seconds("TICKET_RUNNER_FLUSH_SECONDS", 120)
        self.sync_wake, self.sync_stop, self.sync_thread = threading.Event(), threading.Event(), None
        self.sync_problems, self.sync_down, self.sync_lock = set(), False, threading.Lock()
        # What the scheduler is waiting on a person for, and when to look at the labels next.
        self.watching, self.next_watch = set(), 0

    def nudge(self):
        self.sync_wake.set()

    def github(self):
        with self.state:
            return self.data.setdefault("_github", {})

    def start_sync(self):
        self.sync_thread = threading.Thread(target=self.sync_loop, name="github-sync", daemon=True)
        self.sync_thread.start()

    def sync_loop(self):
        while not self.sync_stop.is_set():
            try:
                self.sync_tick()
            except Exception as exc:  # GitHub is convenience metadata: it must never end a run.
                self.report_once("sync", f"Warning: the GitHub sync hit {type(exc).__name__}: {exc}")
            self.sync_wake.wait(self.poll)
            self.sync_wake.clear()

    def sync_tick(self, watch=False):
        with self.sync_lock:
            self.sync_pass()
            if watch or time.monotonic() >= self.next_watch:
                self.next_watch = time.monotonic() + self.poll
                self.watch_pass()

    def watch_pass(self):
        """Removing needs-human unblocks. A label the runner never saw on GitHub, or an unreadable issue, says nothing."""
        for n in sorted(self.watching):
            self.watch(n, self.data.get(str(n)), lambda view: self.tickets[n].update(
                {k: view[k] for k in ("title", "body", "state") if k in view}))
        if self.data.get("_hold"):
            self.watch(self.a.parent, self.data["_hold"], lambda view: None)

    def watch(self, n, record, unblocked):
        if not record or record.get("unblocked"):
            return
        view = self.issue_view(n)
        if view is None:
            return
        with self.state:
            confirmed = self.github().setdefault("labels", {})
            if view.get("state") == "CLOSED" and n != self.a.parent:
                self.tickets[n]["state"] = "CLOSED"  # Closed by hand: nothing left to wait for.
            elif "needs-human" in [label.get("name") for label in view.get("labels", [])]:
                confirmed[str(n)] = "needs-human"
            elif confirmed.get(str(n)) == "needs-human":
                unblocked(view)
                record["unblocked"] = True
                del confirmed[str(n)]
            self.persist()

    @staticmethod
    def issue_view(n):
        found = gh("issue", "view", str(n), "--json", "labels,state,title,body")
        try:
            return None if found.returncode else json.loads(found.stdout)
        except ValueError:
            return None

    def sync_pass(self):
        """Send what GitHub lacks; stop at the first sign GitHub is down, as the next pass tries everything again."""
        for step in (self.sync_labels, self.sync_epic_label, self.sync_links, self.sync_closes, self.sync_findings, self.sync_costs):
            if not step():
                return
        if self.sync_down:
            self.sync_down = False
            say("GitHub is reachable again; its updates caught up", DIM)

    def stop_sync(self, flush):
        """A run that ended on its own gives GitHub a last chance; Ctrl-C and run-wide failures just leave."""
        if self.sync_thread is None:
            return
        deadline = time.monotonic() + (self.flush_limit if flush else 0)
        next_wake = 0
        while flush and self.sync_pending() and time.monotonic() < deadline:
            if time.monotonic() >= next_wake:
                self.nudge()
                next_wake = time.monotonic() + min(5, self.poll)
            time.sleep(.05)
        self.sync_stop.set()
        self.nudge()
        self.sync_thread.join(1)
        left = self.sync_pending()
        if flush and left:
            say(f"Not on GitHub yet: {'; '.join(left)}. The next run of this epic sends them.", YELLOW, err=True)

    def report_once(self, key, message):
        if key not in self.sync_problems:
            self.sync_problems.add(key)
            say(message, YELLOW, err=True)

    def undelivered(self, key, what, result):
        """A failed update: True when GitHub is down and the pass should stop, False when it is a real error."""
        if transient(result.stderr):
            if not self.sync_down:
                self.sync_down = True
                say(f"Could not reach GitHub to {what} ({result.stderr.strip() or 'no response'}); "
                    f"retrying about every {self.poll:g}s", YELLOW, err=True)
            return True
        self.report_once(key, f"Warning: could not {what}: {result.stderr.strip()}")
        return False

    def expected_label(self, n):
        record = self.data.get(str(n)) or {}
        status = record.get("status")
        if status is None:
            return None
        if status == "done":
            return "agent-done"
        if status in WAITING:
            return "in-progress" if record.get("unblocked") else "needs-human"
        return "ready-for-agent" if status in STOPPED else "in-progress"

    def expected_labels(self):
        with self.state:
            found = {int(key): self.expected_label(int(key)) for key in self.data if key.isdigit()}
            hold = self.data.get("_hold")
            if hold and not hold.get("unblocked"):
                found[self.a.parent] = "needs-human"
            return {n: label for n, label in sorted(found.items()) if label}

    def wanted_labels(self):
        """Issue → label, for every issue whose label GitHub has not confirmed."""
        with self.state:
            confirmed = self.github().get("labels", {})
            return {n: label for n, label in self.expected_labels().items()
                    if confirmed.get(str(n)) != label and f"label:{n}" not in self.sync_problems}

    def sync_labels(self):
        for n, label in self.wanted_labels().items():
            result = self.apply_label(n, label, [name for name in STATUS_LABELS if name != label])
            if result.returncode:
                if self.undelivered(f"label:{n}", f"label #{n} {label}", result):
                    return False
                continue
            with self.state:
                self.github().setdefault("labels", {})[str(n)] = label
                self.persist()
        return True

    def stale_epic_label(self):
        """--force reran the epic stages without a person removing its label, so the label is the runner's to clear."""
        with self.state:
            return "_hold" not in self.data and "epic label" not in self.sync_problems \
                and self.github().get("labels", {}).get(str(self.a.parent)) == "needs-human"

    def sync_epic_label(self):
        if not self.stale_epic_label():
            return True
        result = gh("issue", "edit", str(self.a.parent), "--remove-label", "needs-human")
        if result.returncode and not missing_labels(result.stderr):
            return not self.undelivered("epic label", f"remove needs-human from #{self.a.parent}", result)
        with self.state:
            self.github()["labels"].pop(str(self.a.parent), None)
            self.persist()
        return True

    def apply_label(self, n, add, remove):
        """Add one label and remove others. A label missing from the repo is created if it is being added, and dropped
        if it is being removed: there is nothing to remove, and no reason to clutter the repo with it."""
        remove = list(remove)
        for _ in range(len(LABELS) + 2):
            result = gh("issue", "edit", str(n), "--add-label", add,
                        *(["--remove-label", ",".join(remove)] if remove else []))
            if not result.returncode:
                return result
            progressed = False
            for name in missing_labels(result.stderr):
                if name in remove:
                    remove.remove(name)
                    progressed = True
                elif name == add:
                    made = self.create_label(name)
                    if made.returncode:
                        return made
                    progressed = True
            if not progressed:
                return result
        return result

    def create_label(self, name):
        """Never --force: a label that exists keeps the colour and description the repo gave it."""
        colour, description = LABELS.get(name, ("EDEDED", "Used by the ticket runner"))
        made = gh("label", "create", name, "--color", colour, "--description", description)
        if made.returncode and "already exists" in made.stderr.lower():
            made.returncode = 0
        return made

    def link_fix_ticket(self, n):
        """Label the fix ticket and make it a native sub-issue; the `## Parent` section already makes it one."""
        with self.state:
            self.github().setdefault("links", {}).setdefault(str(n), {})
            self.persist()
        self.nudge()

    def sync_links(self):
        with self.state:
            links = {int(key): dict(value) for key, value in self.github().get("links", {}).items()}
        for n, link in sorted(links.items()):
            if f"link:{n}" in self.sync_problems or (link.get("label") and link.get("linked")):
                continue
            if not link.get("label"):
                result = self.apply_label(n, FIX_LABEL, [])
                if result.returncode:
                    if self.undelivered(f"link:{n}", f"label fix ticket #{n}", result):
                        return False
                    continue
                self.mark_link(n, "label")
            if not link.get("linked"):
                result = gh("api", f"repos/{{owner}}/{{repo}}/issues/{n}", "--jq", ".id")
                if not result.returncode:
                    result = gh("api", "--method", "POST", f"repos/{{owner}}/{{repo}}/issues/{self.a.parent}/sub_issues",
                                "-F", f"sub_issue_id={result.stdout.strip()}")
                if result.returncode and "already" not in result.stderr.lower():
                    if self.undelivered(f"link:{n}", f"link fix ticket #{n} to #{self.a.parent}", result):
                        return False
                    continue
                self.mark_link(n, "linked")
        return True

    def mark_link(self, n, part):
        with self.state:
            self.github()["links"][str(n)][part] = True
            self.persist()

    def closes_owed(self):
        with self.state:
            rv = self.data.get("_review", {})
            if not rv.get("closeAt"):
                return []
            closed = rv.get("closed", [])
            return [n for n in self.order if n not in closed and self.tickets.get(n, {}).get("state") != "CLOSED"
                    and f"close:{n}" not in self.sync_problems]

    def sync_closes(self):
        for n in self.closes_owed():
            rv = self.data["_review"]
            result = gh("issue", "close", str(n), "--comment", self.close_comment(rv, n))
            if result.returncode:
                if self.undelivered(f"close:{n}", f"close #{n}", result):
                    return False
                continue
            with self.state:
                rv.setdefault("closed", []).append(n)
                self.persist()
            say(f"Closed #{n}", DIM)
        return True

    def close_comment(self, rv, n):
        spent = [c["costUsd"] for owner, c in self.costs() if owner == f"#{n}" and "costUsd" in c]
        cost = f" Cost: {dollars(sum(spent))} (estimated)." if spent and getattr(self.a, "post_costs", False) else ""
        return (f"Integrated into `{self.epic}` at `{rv['head'][:8]}`; the epic review of #{self.a.parent} said "
                f"SHIP.{cost}")

    def findings_owed(self):
        with self.state:
            rv = self.data.get("_review", {})
            return bool(rv.get("start")) and rv.get("findingsAt") == rv["start"] and rv.get("posted") != rv["start"] \
                and "findings" not in self.sync_problems and (self.review_dir() / "findings.md").exists()

    def sync_findings(self):
        if not self.findings_owed():
            return True
        rv, body = self.data["_review"], self.review_dir() / "findings.md"
        result = gh("issue", "comment", str(self.a.parent), "--body-file", str(body))
        if result.returncode:
            return not self.undelivered("findings", f"post the epic review's findings on #{self.a.parent} (they are "
                                        f"in {body})", result)
        self.update(rv, posted=rv["start"])
        say(f"Posted the epic review's deferred and disputed findings on #{self.a.parent}", DIM)
        return True

    def queue_costs(self, body):
        self.update(self.github(), costs=hashlib.sha256(body.read_bytes()).hexdigest())
        self.nudge()

    def costs_owed(self):
        with self.state:
            gh_state = self.github()
            return bool(gh_state.get("costs")) and gh_state["costs"] != gh_state.get("costsPosted") \
                and "costs" not in self.sync_problems and (self.folder / "costs.md").exists()

    def sync_costs(self):
        if not self.costs_owed():
            return True
        body, sent = self.folder / "costs.md", self.github()["costs"]
        result = gh("issue", "comment", str(self.a.parent), "--body-file", str(body))
        if result.returncode:
            return not self.undelivered("costs", f"post the cost table on #{self.a.parent} (it is in {body})", result)
        self.update(self.github(), costsPosted=sent)
        return True

    def sync_pending(self):
        """What GitHub still lacks, in words; real errors were already reported and are not retried this run."""
        found = [f"#{n} {label}" for n, label in self.wanted_labels().items()]
        found += [f"removing needs-human from #{self.a.parent}"] * self.stale_epic_label()
        with self.state:
            found += [f"fix ticket #{key} link" for key, link in self.github().get("links", {}).items()
                      if f"link:{key}" not in self.sync_problems and not (link.get("label") and link.get("linked"))]
        found += [f"closing #{n}" for n in self.closes_owed()]
        found += ["the epic review's findings"] * self.findings_owed()
        found += ["the cost table"] * self.costs_owed()
        return found
