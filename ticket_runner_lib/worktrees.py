"""Worktree provisioning, integration, gates, and conflict repair."""

from pathlib import Path
import os
import shutil
import subprocess

from .display import say
from .processes import attempt, dirty, git, rebasing
from .settings import BOLD, CLONES, Fatal, GREEN, RED, RETRYABLE, Stop, YELLOW
from .text import verdict


class WorktreeIntegration:
    def provision(self, wt):
        for name in self.cfg["dependencyDirs"]:
            source, target = self.root / name, wt / name
            if not source.is_dir() or target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            for clone in CLONES:
                if not subprocess.run([*clone, str(source), str(target)], capture_output=True).returncode:
                    break
                shutil.rmtree(target, ignore_errors=True)
            else:
                raise RuntimeError(f"Could not copy {name} into {wt}")
        for name in self.cfg["linkFiles"]:
            source, target = self.root / name, wt / name
            if source.exists() and not os.path.lexists(target):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source)

    def check_worktree(self, wt, branch):
        if git(wt, "branch", "--show-current") != branch:
            return f"worktree left {branch}"
        for name in self.cfg["linkFiles"]:
            source, target = self.root / name, wt / name
            if source.exists() and (not target.is_symlink() or Path(os.readlink(target)) != source):
                return f"{name} in the worktree no longer links to the main {name}"
        return None

    def lock_changed(self, cwd, old, new):
        return bool(self.cfg["lockfiles"]) and bool(attempt(cwd, "diff", "--quiet", old, new, "--",
                                                            *self.cfg["lockfiles"]).returncode)

    def integrate(self, n, record, tdir):
        # A ticket queued on the lock must not start a rebase once Ctrl-C has landed.
        if self.stopping.is_set():
            raise Stop()
        branch, wt = self.branch(n), Path(record["worktree"])
        try:
            self.merge(n, record, tdir, branch, wt)
        except Stop:
            # Undo only the runner's own rebase: the ticket's commits come back untouched, and
            # --resume redoes the rebase (and any conflict repair) from scratch.
            if wt.exists() and rebasing(wt):
                attempt(wt, "rebase", "--abort")
            raise

    def merge(self, n, record, tdir, branch, wt):
        color = self.color(n)
        self.check_main()
        if not wt.exists():
            return self.fail(n, record, "needs-human", f"worktree {wt} is missing; recreate it from {branch}")
        if rebasing(wt):
            return self.fail(n, record, "needs-human", f"a rebase is in progress in {os.path.relpath(wt)}; "
                             "finish or abort it, then --resume")
        if dirty(wt):
            return self.fail(n, record, "needs-human", f"{os.path.relpath(wt)} has uncommitted changes; "
                             "commit or discard them, then --resume")
        problem = self.check_worktree(wt, branch)
        if problem:
            return self.fail(n, record, "needs-human", problem)
        tip = git(self.root, "rev-parse", self.epic)
        if attempt(wt, "merge-base", "--is-ancestor", tip, "HEAD").returncode:
            say(f"#{n} rebasing onto {self.epic} {tip[:8]}", BOLD, color)
            before = git(wt, "rev-parse", "HEAD")
            repaired = False
            if attempt(wt, "rebase", tip).returncode:
                conflicts = git(wt, "diff", "--name-only", "--diff-filter=U") or "(see git status)"
                say(f"#{n} rebase conflicted in {', '.join(conflicts.split())}; handing back to its agent",
                    YELLOW, BOLD)
                if not self.repair(n, record, tdir, tip, f"""The runner is integrating #{n} into epic branch {self.epic}.
Rebasing {branch} onto the epic tip {tip} stopped with conflicts in:
{conflicts}
Those changes come from tickets merged while you worked. Resolve every conflict so both sides' intent
survives, run typechecking and the tests covering the touched code, then `git add` the files and run
`GIT_EDITOR=true git -c core.commentChar=auto rebase --continue` until the rebase finishes (commentChar
keeps commit subjects that start with #{n}). Do not abort the rebase, change branches, push, or modify
the runner or {self.hands_off}.
{self.repair_guidance('conflict')}End with exactly one line: STATUS=COMPLETE, STATUS=NEEDS_HUMAN, or STATUS=BLOCKED.
"""):
                    return
                repaired = True
            if self.lock_changed(wt, before, "HEAD"):
                log = self.log(tdir, "install")
                say(f"#{n} a lockfile changed under it; running {self.install_cmd} in its worktree", BOLD, color)
                if self.shell(self.install_cmd, wt, log):
                    return self.fail(n, record, "needs-human", f"{self.install_cmd} failed in the worktree; see {log}")
            passed, log = self.gate(n, wt, tdir)
            if not passed and repaired:
                return self.fail(n, record, "needs-human", f"gate failed after the conflict repair; see {log}")
            if not passed:
                tail = "\n".join(log.read_text(errors="replace").splitlines()[-120:])
                if not self.repair(n, record, tdir, tip, f"""The runner rebased {branch} onto the epic tip {tip} cleanly, but the integration gate failed:
  $ {self.gate_cmd}
Last lines of output (full log: {log}):
```
{tail}
```
Tickets merged while you worked may have changed code yours relies on. Fix the failure, re-run the gate
command, and commit the fix on {branch} referencing #{n}. Do not rebase, merge, change branches, push,
or modify the runner or {self.hands_off}.
{self.repair_guidance('gate')}End with exactly one line: STATUS=COMPLETE, STATUS=NEEDS_HUMAN, or STATUS=BLOCKED.
"""):
                    return
                passed, log = self.gate(n, wt, tdir)
                if not passed:
                    return self.fail(n, record, "needs-human", f"gate still fails after one repair; see {log}")
        touched = git(wt, "diff", "--name-only", tip, "HEAD", "--", *self.protected) if self.protected else ""
        if touched:
            return self.fail(n, record, "needs-human", f"the ticket changed protected files ({', '.join(touched.split())}); "
                             "revert those commits, then --resume")
        self.check_main()
        git(self.root, "merge", "-q", "--ff-only", branch)
        head = git(self.root, "rev-parse", "HEAD")
        self.update(record, status="done", head=head, drop=("reason",))
        self.label(n, "agent-done", "in-progress")
        count = git(self.root, "rev-list", "--count", f"{tip}..{head}")
        say(f"#{n} merged into {self.epic} ({count} commit{'s' * (count != '1')}, now {head[:8]})", GREEN, BOLD)
        if self.lock_changed(self.root, tip, head):
            say(f"a lockfile changed; running {self.install_cmd} in the main worktree", BOLD)
            if subprocess.run(self.install_cmd, shell=True, cwd=self.root).returncode:
                raise Fatal(f"{self.install_cmd} failed in the main worktree after merging #{n}; "
                            "fix dependencies there, then --resume")
        self.cleanup(n)

    def cleanup(self, n):
        # Only merged work is removed: dirty worktrees stay, and -d refuses unmerged branches.
        # A failure never touches the ticket's done status; the next run tries again.
        branch, wt = self.branch(n), self.worktrees / str(n)
        try:
            if wt.exists():
                if dirty(wt):
                    return say(f"#{n} merged, but {os.path.relpath(wt)} has uncommitted changes; left in place", YELLOW)
                git(self.root, "worktree", "remove", "--force", str(wt))
            if not attempt(self.root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode:
                git(self.root, "branch", "-q", "-d", branch)
        except (subprocess.CalledProcessError, OSError) as exc:
            say(f"#{n} merged, but removing its worktree/branch failed ({exc}); the next run retries", YELLOW)

    def repair(self, n, record, tdir, tip, prompt):
        branch, wt = self.branch(n), Path(record["worktree"])
        text = self.run_agent(self.a.model, self.a.effort, self.a.max_turns, prompt, tdir, record,
                              record.get("sessionId"), cwd=wt, tag=f"#{n}", color=self.color(n))
        self.guard()
        if record["status"] != "reported":
            ended = record["status"]
            if rebasing(wt):
                attempt(wt, "rebase", "--abort")
            if ended in RETRYABLE and not dirty(wt):
                self.fail(n, record, ended, f"repair agent ended {ended}; --resume retries the integration")
            else:
                self.fail(n, record, "needs-human", f"repair agent ended {ended} with uncommitted edits in "
                          f"{os.path.relpath(wt)}; commit or discard them, then --resume")
            return False
        status = verdict(text)
        problem = ("a rebase is still in progress" if rebasing(wt) else
                   "uncommitted changes remain" if dirty(wt) else
                   self.check_worktree(wt, branch) or
                   (f"{branch} does not contain the epic tip" if attempt(
                       wt, "merge-base", "--is-ancestor", tip, "HEAD").returncode else None))
        if status != "COMPLETE" or problem:
            self.fail(n, record, "needs-human", f"repair ended status={status}"
                      f"{'; ' + problem if problem else ''}; see {tdir}")
            return False
        self.update(record, status="ready-to-merge")
        return True

    def gate(self, n, wt, tdir):
        log = self.log(tdir, "gate")
        say(f"#{n} gate: {self.gate_cmd}", BOLD, self.color(n))
        rc = self.shell(self.gate_cmd, wt, log)
        say(f"#{n} gate {'passed' if rc == 0 else 'failed'} ({os.path.relpath(log)})",
            BOLD, GREEN if rc == 0 else RED)
        return rc == 0, log
