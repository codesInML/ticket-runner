"""Shared runner state and workflow orchestration."""

from pathlib import Path
import datetime
import os
import queue
import subprocess
import threading
import time

from .agents import AgentProcesses
from .backends import ClaudeCode, Codex, CommandCode
from .display import say
from .epic_branch import tree_path
from .epic_review import EpicReview
from .github import gh_json
from .processes import attempt, dirty, git, save
from .prompts import Prompts
from .review import Reviews
from .scheduling import Scheduling
from .settings import (BOLD, CLAUDE_PROTECTED, CODEX_HOME_DIR, CODEX_PROTECTED, CONFIG,
                       Fatal, GREEN, TICKET_COLORS, YELLOW)
from .shipping import Shipping
from .sync import GitHubSync
from .text import section
from .tickets import TicketImplementation
from .verification import Verification
from .worktrees import WorktreeIntegration


class Runner(
    AgentProcesses, Scheduling, TicketImplementation, WorktreeIntegration,
    Prompts, Reviews, EpicReview, Verification, Shipping, GitHubSync,
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
        # The epic branch lives in its own worktree, so the folder the operator ran this from is never touched.
        self.tree = tree_path(root, a.parent) if tree_path(root, a.parent).is_dir() else root
        self.claude = getattr(a, "backend", "commandcode") == "claude"
        factories = {"commandcode": CommandCode, "claude": ClaudeCode,
                     "codex": lambda: Codex(root / CODEX_HOME_DIR, a.codex_auth, a.codex_binary)}
        self.backend = factories[getattr(a, "backend", "commandcode")]()
        if hasattr(self.backend, "prepare"):
            self.backend.prepare(skills_dir)
        self.binary = self.backend.binary
        self.spent = False
        self.gate_cmd = os.getenv("GATE_CMDS") or " && ".join(cfg["gate"]) or "true"
        self.install_cmd = os.getenv("INSTALL_CMD") or cfg["install"]
        # state guards data/state.json; integration serializes every change to the epic branch,
        # worktree creation and main-worktree installs.
        self.state, self.integration, self.spawn = threading.RLock(), threading.Lock(), threading.Lock()
        self.children, self.threads, self.events = set(), {}, queue.Queue()
        self.stopping, self.fatal, self.finished = threading.Event(), None, False
        self.epic_unblocked, self.no_wait = False, bool(os.getenv("TICKET_RUNNER_NO_WAIT"))
        self.sync_setup()
        self.costs()  # migrate legacy costs before any replaceable stage records are reset
        home = Path(__file__).resolve().parents[1]
        project = ["AGENTS.md", CONFIG, *cfg["linkFiles"], *([cfg["serveEnv"]] if cfg["serveEnv"] else []),
                   *([cfg["appVerification"]] if cfg["appVerification"] else []),
                   *(CLAUDE_PROTECTED if self.claude else ()),
                   *(CODEX_PROTECTED if self.backend.name == "codex" else ())]
        project = list(dict.fromkeys(project))
        # Under .claude/ only tracked files are watched: it may hold untracked worktrees and local settings
        # that the operator's own sessions keep changing while the runner works.
        claude_dir = attempt(self.tree, "ls-files", "-z", "--", ".claude").stdout.split("\0") if self.claude else []
        own = [home / "ticket_runner.py", home / "bin/ticket-runner",
               *sorted((home / "ticket_runner_lib").glob("*.py"))]
        # Agents read the epic worktree's copies. In the operator's folder only what they can reach through a link
        # is watched (and an uncommitted config, which then is the config): that folder may change branch freely.
        local = [*cfg["linkFiles"], *([cfg["serveEnv"]] if cfg["serveEnv"] else []),
                 *([] if (self.tree / CONFIG).exists() else [CONFIG])]
        protected = [*own, *(self.tree / f for f in project if not f.endswith("/") and not f.startswith(":(glob)")),
                     *(self.tree / f for f in claude_dir if f), *(root / f for f in local)]
        # A protected file that is missing now must stay missing: an agent creating one counts as changing it.
        self.instruction_paths = self.claude_instructions() if self.claude else set()
        if self.backend.name == "codex":
            self.instruction_paths = self.codex_instructions()
        self.snapshots = {p: p.read_bytes() if p.exists() else None for p in [*protected, *self.instruction_paths]}
        self.protected = list(dict.fromkeys([*project, *(str(p.relative_to(root)) for p in own
                                                       if p.is_relative_to(root))]))
        self.hands_off = ", ".join(self.protected)
        notes = (self.tree / "AGENTS.md").read_text(errors="replace") if (self.tree / "AGENTS.md").exists() else ""
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

    def continuing(self, n):
        """Whether this ticket picks up where it stopped: --resume, or a person unblocking it."""
        return self.a.resume or bool(self.data.get(str(n), {}).get("unblocked"))

    @property
    def resuming(self):
        """The same for the epic stages."""
        return self.a.resume or self.epic_unblocked

    def complete(self, n):
        return self.tickets[n]["state"] == "CLOSED" or self.data.get(str(n), {}).get("status") == "done"

    def color(self, n):
        return TICKET_COLORS[self.order.index(n) % len(TICKET_COLORS)]

    def stage_color(self, i):
        """Epic stage agents count down the palette: reviewers 0-1, verifier 2, repair 3, repair review 4,
        launch plan 5."""
        return TICKET_COLORS[-1 - i % len(TICKET_COLORS)]

    def claude_instructions(self):
        """Discover instruction inputs without traversing disposable worktrees or dependencies."""
        found = {self.tree / "CLAUDE.md", self.tree / "CLAUDE.local.md"}
        excluded = {self.tree / name for name in (".git", ".scratch", *self.cfg["dependencyDirs"])}
        for folder, dirs, files in os.walk(self.tree):
            dirs[:] = [d for d in dirs if Path(folder) / d not in excluded and
                       not (Path(folder).name == ".claude" and d == "worktrees")]
            found.update(Path(folder) / name for name in files if name in ("CLAUDE.md", "CLAUDE.local.md"))
        return found

    def guard(self):
        changed = [p for p, contents in self.snapshots.items() if (p.read_bytes() if p.exists() else None) != contents]
        if self.claude:
            changed.extend(sorted(self.claude_instructions() - self.instruction_paths))
        if self.backend.name == "codex":
            changed.extend(sorted(self.codex_instructions() - self.instruction_paths))
        if changed:
            raise Fatal(f"Agent modified {', '.join(map(str, changed))}; inspect and restore before resuming")
        if git(self.tree, "branch", "--show-current") != self.epic:
            raise Fatal(f"{os.path.relpath(self.tree)} left {self.epic}; check it out again, then --resume")

    def codex_instructions(self, root=None):
        root = Path(root or self.tree)
        found = {root / "AGENTS.md", root / "AGENTS.override.md"}
        excluded = {root / name for name in (".git", ".scratch", *self.cfg["dependencyDirs"])}
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if Path(folder) / d not in excluded]
            inside = Path(folder).relative_to(root).parts
            found.update(Path(folder) / name for name in files if name in ("AGENTS.md", "AGENTS.override.md")
                         or inside and inside[0] in (".codex", ".agents"))
        return found

    def codex_snapshot(self, root):
        return {p: p.read_bytes() if p.exists() else None for p in self.codex_instructions(root)}

    def guard_codex(self, root, snapshots):
        changed = {p for p, contents in snapshots.items() if (p.read_bytes() if p.exists() else None) != contents}
        changed.update(self.codex_instructions(root) - snapshots.keys())
        if changed:
            raise Fatal(f"Agent modified {', '.join(map(str, sorted(changed)))}; inspect and restore before resuming")

    def check_main(self):
        if git(self.tree, "branch", "--show-current") != self.epic:
            raise Fatal(f"{os.path.relpath(self.tree)} left {self.epic}; check it out again, then --resume")
        if dirty(self.tree):
            raise Fatal(f"{os.path.relpath(self.tree)} has uncommitted changes, so the epic branch cannot "
                        "fast-forward. Commit or stash them, then --resume. No files discarded.")

    def execute(self):
        ended = False
        try:
            outcome = self.run()
            ended = True
            return outcome
        finally:
            try:
                self.report_costs()
            except Exception as exc:  # a cost report must never mask how the run ended
                say(f"Warning: could not report costs ({exc})", YELLOW, err=True)
            self.stop_sync(flush=ended)
            if self.finished:
                self.retire_tree()

    def run(self):
        try:
            self.persist()
            attempt(self.root, "worktree", "prune")
            self.adopt_hold()
            self.selected = [] if self.a.epic_only else ([self.a.only] if self.a.only else self.order)
            rv = self.data.get("_review", {})
            remaining = [n for n in self.order if not self.complete(n)]
            if self.a.epic_only and not self.a.no_epic_review and rv.get("phase") in ("file", "fix") and \
                    set(remaining) <= self.review_fix_tickets(rv) and \
                    self.review_resumable(rv, git(self.tree, "rev-parse", "HEAD")):
                self.start_sync()
                return self.ended_stages()
            pending = self.preflight(self.selected)
            self.start_sync()
            outcome = self.schedule(pending)
            return self.ended_stages() if outcome is None else outcome
        except BaseException:
            self.stop()
            raise

    def ended_stages(self):
        outcome = self.settled_stages()
        self.finished = outcome == 0
        return outcome

    def settled_stages(self):
        """The epic stages, again each time a person unblocks the epic; 0 once they all pass."""
        while True:
            if not self.wait_for_epic():
                return 1
            outcome = self.stages()
            if outcome == 0 or self.no_wait or "_hold" not in self.data:
                return outcome

    def wait_for_epic(self):
        """Hold the stages while the epic issue says needs-human; False only when told not to wait."""
        if self.no_wait and "_hold" in self.data:
            self.sync_tick(watch=True)
        while "_hold" in self.data:
            if self.data["_hold"].get("unblocked"):
                self.release_epic()
                say(f"Epic #{self.a.parent} unblocked; running its stages again", GREEN, BOLD)
                return True
            if self.no_wait:
                say(f"Epic #{self.a.parent} needs-human: {self.data['_hold']['reason']}", YELLOW, BOLD)
                return False
            self.remind()
            time.sleep(.5)  # Bounded so Ctrl-C lands at once.
        return True

    def adopt_hold(self):
        """An epic stuck before holds were saved, and --epic-only --force, which goes ahead whatever its label says."""
        with self.state:
            stuck = next((rec for rec in map(self.data.get, ("_review", "_verify", "_ship"))
                          if rec and rec.get("status") == "needs-human"), None)
            if stuck and "_hold" not in self.data:
                self.data["_hold"] = {"reason": stuck.get("reason", "it needs a person")}
        if getattr(self.a, "force", False) and "_hold" in self.data:
            self.release_epic()

    def release_epic(self):
        with self.state:
            self.data.pop("_hold", None)
            self.persist()
        self.epic_unblocked = True
        self.__dict__.pop("_parent", None)  # The person may have changed the requirements while it waited.

    def retire_tree(self):
        """A finished run frees the epic branch for the operator; its commits stay on the branch."""
        if self.tree == self.root or not self.tree.exists():
            return
        try:
            if dirty(self.tree):
                return say(f"{os.path.relpath(self.tree)} has uncommitted changes; left in place", YELLOW)
            git(self.root, "worktree", "remove", "--force", str(self.tree))
        except (subprocess.CalledProcessError, OSError) as exc:
            say(f"Could not remove {os.path.relpath(self.tree)} ({exc}); remove it by hand", YELLOW)

    def stages(self):
        """Epic review, verify, then ship, each skipped while its result still holds for the epic head."""
        if dirty(self.tree):
            self.hold_epic(f"{os.path.relpath(self.tree)} has uncommitted changes; commit or discard them")
            say(f"Epic #{self.a.parent} needs-human: {self.data['_hold']['reason']}", YELLOW, BOLD)
            return 1
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
            self.hold_epic(reason)
        say(f"Epic #{self.a.parent} {status}: {reason}", YELLOW, BOLD)
        return 1

    def hold_epic(self, reason):
        """The epic issue shows needs-human until a person unblocks it."""
        with self.state:
            self.data["_hold"] = {"reason": reason}
            self.github().get("labels", {}).pop(str(self.a.parent), None)
            self.persist()
        self.nudge()
