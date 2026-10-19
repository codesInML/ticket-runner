"""Shared runner state and workflow orchestration."""

from pathlib import Path
import datetime
import os
import queue
import subprocess
import threading

from .agents import AgentProcesses
from .backends import ClaudeCode, CommandCode
from .display import say
from .epic_review import EpicReview
from .github import gh_json
from .processes import attempt, dirty, git, save
from .prompts import Prompts
from .review import Reviews
from .scheduling import Scheduling
from .settings import BOLD, CLAUDE_CONFIG_DIR, CLAUDE_PROTECTED, CONFIG, Fatal, GREEN, TICKET_COLORS, YELLOW
from .shipping import Shipping
from .text import section
from .tickets import TicketImplementation
from .verification import Verification
from .worktrees import WorktreeIntegration


class Runner(
    AgentProcesses, Scheduling, TicketImplementation, WorktreeIntegration,
    Prompts, Reviews, EpicReview, Verification, Shipping,
):
    def __init__(self, a, root, folder, path, data, tickets, levels, skill, skills_dir, cfg, key=None):
        self.a, self.root, self.folder, self.path, self.data, self.cfg = a, root, folder, path, data, cfg
        self.tickets, self.levels, self.skill, self.skills_dir = tickets, levels, skill, skills_dir
        self.osmani = a.workflow == "osmani"
        self.order = [n for level in levels for n in level]
        self.selected, self.parked, self.deferred, self.failed = [], [], {}, []
        self.epic = data["_meta"]["branch"]
        self.blockers = {n: t["blockers"] for n, t in tickets.items()}
        self.worktrees = root / ".scratch" / "worktrees"
        # The backend holds the company key for its children's environment only; it is never stored or shown.
        self.claude = getattr(a, "backend", "commandcode") == "claude-api"
        self.backend = ClaudeCode(key, root / CLAUDE_CONFIG_DIR) if self.claude else CommandCode()
        if self.claude:
            self.backend.prepare(skills_dir)
        self.binary = self.backend.binary
        self.budgets, self.spent = getattr(a, "budgets", {}), False
        self.gate_cmd = os.getenv("GATE_CMDS") or " && ".join(cfg["gate"]) or "true"
        self.install_cmd = os.getenv("INSTALL_CMD") or cfg["install"]
        # state guards data/state.json; integration serializes every change to the epic branch,
        # worktree creation and main-worktree installs.
        self.state, self.integration, self.spawn = threading.RLock(), threading.Lock(), threading.Lock()
        self.children, self.threads, self.events = set(), {}, queue.Queue()
        self.stopping, self.fatal = threading.Event(), None
        self.costs()  # migrate legacy costs before any replaceable stage records are reset
        home = Path(__file__).resolve().parents[1]
        project = ["AGENTS.md", CONFIG, *cfg["linkFiles"], *([cfg["serveEnv"]] if cfg["serveEnv"] else []),
                   *(CLAUDE_PROTECTED if self.claude else ())]
        project = list(dict.fromkeys(project))
        # Under .claude/ only tracked files are watched: it may hold untracked worktrees and local settings
        # that the operator's own sessions keep changing while the runner works.
        claude_dir = attempt(root, "ls-files", "-z", "--", ".claude").stdout.split("\0") if self.claude else []
        protected = [home / "ticket_runner.py", home / "bin/ticket-runner", home / "bin/ticket-runner-osmani",
                     home / "bin/ticket-runner-claude", *sorted((home / "ticket_runner_lib").glob("*.py")),
                     *(root / f for f in project if not f.endswith("/") and not f.startswith(":(glob)")), *(root / f for f in claude_dir if f)]
        # A protected file that is missing now must stay missing: an agent creating one counts as changing it.
        self.instruction_paths = self.claude_instructions() if self.claude else set()
        self.snapshots = {p: p.read_bytes() if p.exists() else None for p in [*protected, *self.instruction_paths]}
        self.protected = list(dict.fromkeys([*project, *(str(p.relative_to(root)) for p in protected
                                                       if p.is_relative_to(root)
                                                       and ".claude" not in p.relative_to(root).parts)]))
        self.hands_off = ", ".join(self.protected)
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

    def stage_color(self, i):
        """Epic stage agents count down the palette: reviewers 0-1, verifier 2, repair 3, repair review 4,
        launch plan 5."""
        return TICKET_COLORS[-1 - i % len(TICKET_COLORS)]

    def claude_instructions(self):
        """Discover instruction inputs without traversing disposable worktrees or dependencies."""
        found = {self.root / "CLAUDE.md", self.root / "CLAUDE.local.md"}
        excluded = {self.root / name for name in (".git", ".scratch", *self.cfg["dependencyDirs"])}
        for folder, dirs, files in os.walk(self.root):
            dirs[:] = [d for d in dirs if Path(folder) / d not in excluded and
                       not (Path(folder).name == ".claude" and d == "worktrees")]
            found.update(Path(folder) / name for name in files if name in ("CLAUDE.md", "CLAUDE.local.md"))
        return found

    def guard(self):
        changed = [p for p, contents in self.snapshots.items() if (p.read_bytes() if p.exists() else None) != contents]
        if self.claude:
            changed.extend(sorted(self.claude_instructions() - self.instruction_paths))
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
        try:
            return self.run()
        finally:
            try:
                self.report_costs()
            except Exception as exc:  # a cost report must never mask how the run ended
                say(f"Warning: could not report costs ({exc})", YELLOW, err=True)

    def run(self):
        try:
            self.persist()
            attempt(self.root, "worktree", "prune")
            self.selected = [] if self.a.epic_only else ([self.a.only] if self.a.only else self.order)
            rv = self.data.get("_review", {})
            remaining = [n for n in self.order if not self.complete(n)]
            if self.a.epic_only and not self.a.no_epic_review and rv.get("phase") in ("file", "fix") and \
                    set(remaining) <= self.review_fix_tickets(rv) and \
                    self.review_resumable(rv, git(self.root, "rev-parse", "HEAD")):
                return self.stages()
            pending = self.preflight(self.selected)
            outcome = self.schedule(pending)
            return self.stages() if outcome is None else outcome
        except BaseException:
            self.stop()
            raise

    def stages(self):
        """Epic review, verify, then ship, each skipped while its result still holds for the epic head."""
        if dirty(self.root):
            raise RuntimeError("The epic stages require a clean main worktree")
        if not self.a.no_epic_review:
            outcome = self.epic_review()
            if outcome:
                return outcome
        if not self.a.no_verify:
            outcome = self.verify()
            if outcome:
                return outcome
        if self.a.no_epic_review:
            say("Epic review skipped (--no-epic-review), and with it the ship stage.", GREEN)
            return 0
        if self.a.no_ship:
            say("Ship stage skipped (--no-ship).", GREEN)
            return 0
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
