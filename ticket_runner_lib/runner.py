"""Shared runner state and workflow orchestration."""

from pathlib import Path
import datetime
import os
import queue
import subprocess
import threading

from .agents import AgentProcesses
from .display import say
from .github import gh_json
from .processes import attempt, dirty, git, save
from .prompts import Prompts
from .review import Reviews
from .scheduling import Scheduling
from .settings import BOLD, CONFIG, Fatal, GREEN, TICKET_COLORS, YELLOW
from .shipping import Shipping
from .text import section
from .tickets import TicketImplementation
from .verification import Verification
from .worktrees import WorktreeIntegration


class Runner(
    AgentProcesses, Scheduling, TicketImplementation, WorktreeIntegration,
    Prompts, Reviews, Verification, Shipping,
):
    def __init__(self, a, root, folder, path, data, tickets, levels, skill, skills_dir, cfg):
        self.a, self.root, self.folder, self.path, self.data, self.cfg = a, root, folder, path, data, cfg
        self.tickets, self.levels, self.skill, self.skills_dir = tickets, levels, skill, skills_dir
        self.osmani = a.workflow == "osmani"
        self.order = [n for level in levels for n in level]
        self.selected, self.parked, self.deferred, self.failed = [], [], {}, []
        self.epic = data["_meta"]["branch"]
        self.blockers = {n: t["blockers"] for n, t in tickets.items()}
        self.worktrees = root / ".scratch" / "worktrees"
        self.binary = os.getenv("COMMANDCODE_BIN", "command-code")
        self.gate_cmd = os.getenv("GATE_CMDS") or " && ".join(cfg["gate"]) or "true"
        self.install_cmd = os.getenv("INSTALL_CMD") or cfg["install"]
        # state guards data/state.json; integration serializes every change to the epic branch,
        # worktree creation and main-worktree installs.
        self.state, self.integration, self.spawn = threading.RLock(), threading.Lock(), threading.Lock()
        self.children, self.threads, self.events = set(), {}, queue.Queue()
        self.stopping, self.fatal = threading.Event(), None
        home = Path(__file__).resolve().parents[1]
        project = ["AGENTS.md", CONFIG, *cfg["linkFiles"], *([cfg["serveEnv"]] if cfg["serveEnv"] else [])]
        project = list(dict.fromkeys(project))
        protected = [home / "ticket_runner.py", home / "bin/ticket-runner", home / "bin/ticket-runner-osmani",
                     *sorted((home / "ticket_runner_lib").glob("*.py")),
                     *(root / f for f in project)]
        # A protected file that is missing now must stay missing: an agent creating one counts as changing it.
        self.snapshots = {p: p.read_bytes() if p.exists() else None for p in protected}
        self.protected = project
        self.hands_off = ", ".join(project)
        notes = (root / "AGENTS.md").read_text(errors="replace") if (root / "AGENTS.md").exists() else ""
        self.notes = section(notes, "Ticket Runner") is not None

    def persist(self):
        with self.state:
            self.data["_meta"]["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            save(self.path, self.data)

    def update(self, record, drop=(), **fields):
        with self.state:
            record.update(fields)
            for key in drop:
                record.pop(key, None)
            self.persist()

    def record(self, n):
        with self.state:
            return self.data.setdefault(str(n), {})

    def complete(self, n):
        return self.tickets[n]["state"] == "CLOSED" or self.data.get(str(n), {}).get("status") == "done"

    def label(self, n, add, remove):
        # Labels are convenience metadata; local state is authoritative.
        result = subprocess.run(["gh", "issue", "edit", str(n), "--add-label", add, "--remove-label", remove],
                                capture_output=True, text=True)
        if result.returncode:
            say(f"Warning: could not update labels for #{n}: {result.stderr.strip()}", YELLOW, err=True)

    def color(self, n):
        return TICKET_COLORS[self.order.index(n) % len(TICKET_COLORS)]

    def guard(self):
        changed = [p for p, contents in self.snapshots.items() if (p.read_bytes() if p.exists() else None) != contents]
        if changed:
            raise Fatal(f"Agent modified {', '.join(map(str, changed))}; inspect and restore before resuming")
        if git(self.root, "branch", "--show-current") != self.epic:
            raise Fatal(f"Main worktree left {self.epic}; check it out again, then --resume")

    def check_main(self):
        if git(self.root, "branch", "--show-current") != self.epic:
            raise Fatal(f"Main worktree left {self.epic}; check it out again, then --resume")
        if dirty(self.root):
            raise Fatal("Main worktree has uncommitted changes, so the epic branch cannot fast-forward. "
                        "Commit or stash them, then --resume. No files discarded.")

    def execute(self):
        self.persist()
        attempt(self.root, "worktree", "prune")
        self.selected = [] if self.a.final_only else ([self.a.only] if self.a.only else self.order)
        pending = self.preflight(self.selected)
        try:
            outcome = self.schedule(pending)
        except BaseException:
            self.stop()
            raise
        return self.stages() if outcome is None else outcome

    def stages(self):
        """Verify, then the epic review, each skipped while its result still holds for the epic head."""
        if dirty(self.root):
            raise RuntimeError("The post-ticket stages require a clean main worktree")
        if not self.a.no_verify:
            outcome = self.verify()
            if outcome:
                return outcome
        if self.a.no_final_review:
            say("Epic review skipped (--no-final-review), and with it the ship stage.", GREEN)
            return 0
        outcome = self.review()
        if outcome or self.a.no_ship:
            if not outcome:
                say("Ship stage skipped (--no-ship).", GREEN)
            return outcome
        return self.ship()

    def parent_issue(self):
        if not hasattr(self, "_parent"):
            self._parent = gh_json("issue", "view", str(self.a.parent), "--json", "title,body")
        return self._parent

    def epic_fail(self, rec, status, reason):
        """The epic itself needs a person: stop before the stages that follow."""
        self.update(rec, status=status, reason=reason)
        if status == "needs-human":
            self.label(self.a.parent, "needs-human", "in-progress")
        say(f"Epic #{self.a.parent} {status}: {reason}", YELLOW, BOLD)
        return 1
