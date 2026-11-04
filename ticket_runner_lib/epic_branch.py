"""The epic branch and the epic worktree it runs in, set up before the runner starts."""

from pathlib import Path
import subprocess

from .display import say
from .processes import attempt, git, remote
from .settings import YELLOW


def tree_path(root, parent):
    return Path(root) / ".scratch" / "epics" / str(parent)


def local_tip(root, branch):
    return attempt(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}^{{commit}}").stdout.strip() or None


def contains(root, newer, older):
    return not attempt(root, "merge-base", "--is-ancestor", older, newer).returncode


def checked_out_at(root, branch):
    """Every folder with this branch checked out; git allows only one, and the epic worktree needs it."""
    found, path = [], None
    for line in attempt(root, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            found.append(Path(path))
    return found


def blocked_by_checkout(root, branch, tree):
    """Why the epic worktree cannot take this branch, or None."""
    others = [p for p in checked_out_at(root, branch) if p.resolve() != Path(tree).resolve()]
    if not others:
        return None
    return (f"{branch} is checked out in {others[0]}, and the epic worktree needs it. Switch that folder to another "
            "branch (git switch <other>), then run this again; no work or agent progress is lost")


def origin_tip(root, name):
    """The branch's commit on origin, None when origin lacks it; raises when origin cannot be read."""
    found = remote(root, "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{name}")
    if found.returncode == 2:
        return None
    if found.returncode:
        raise RuntimeError(f"could not read origin: {found.stderr.strip()}")
    return found.stdout.split()[0]


def fetched(root, name):
    got = remote(root, "fetch", "-q", "origin", f"refs/heads/{name}")
    if got.returncode:
        raise RuntimeError(f"could not fetch origin/{name}: {got.stderr.strip()}")


def from_commit(root, name):
    """The latest commit of a --from branch, wherever it lives; two copies that both moved on are refused."""
    local, theirs = local_tip(root, name), origin_tip(root, name)
    if theirs:
        fetched(root, name)
    if not local and not theirs:
        raise RuntimeError(f"--from {name}: no such branch here or on origin")
    if not local or not theirs:
        return local or theirs
    if contains(root, theirs, local):
        return theirs
    if contains(root, local, theirs):
        return local
    raise RuntimeError(f"--from {name}: this checkout and origin have both moved on; reconcile them first")


def target_commit(root, target):
    """A new branch starts from the PR target as origin has it, however stale the local copy is."""
    theirs = origin_tip(root, target)
    if not theirs:
        raise RuntimeError(f"pull request target {target} is not on origin; push it, or name another with --pr-base")
    fetched(root, target)
    return theirs


def require_agents_md(root, commit, branch):
    if attempt(root, "cat-file", "-e", f"{commit}:AGENTS.md").returncode:
        raise RuntimeError(f"AGENTS.md is not committed on {branch}; ticket worktrees would not see it. "
                           "Commit it on this branch first.")


def prepare(root, branch, start, target):
    """Make the local branch: as it stands, from origin, or new from --from or the PR target."""
    local = local_tip(root, branch)
    try:
        theirs = origin_tip(root, branch)
    except RuntimeError as exc:
        if not local:
            raise
        say(f"Warning: using the local {branch} without checking origin ({exc})", YELLOW, err=True)
        return
    if local and theirs and local != theirs:
        fetched(root, branch)
        if contains(root, theirs, local):
            git(root, "branch", "-q", "-f", branch, theirs)
            say(f"{branch} fast-forwarded to origin at {theirs[:8]}")
        elif not contains(root, local, theirs):
            raise RuntimeError(f"{branch} here and on origin have both moved on; reconcile them, then run this again")
    elif theirs and not local:
        fetched(root, branch)
        git(root, "branch", "-q", "--no-track", branch, theirs)
        say(f"{branch} created from origin at {theirs[:8]}")
    elif not local:
        commit = from_commit(root, start) if start else target_commit(root, target)
        require_agents_md(root, commit, branch)
        git(root, "branch", "-q", "--no-track", branch, commit)
        say(f"{branch} created from {start or target} at {commit[:8]}")


def default_target(root):
    """Origin's default branch, or None when neither git nor GitHub can say right now."""
    found = attempt(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").stdout.strip()
    if found:
        return found.split("/", 1)[1]
    try:
        return subprocess.check_output(["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
                                       text=True, cwd=root, stderr=subprocess.DEVNULL, timeout=30).strip() or None
    except (subprocess.SubprocessError, OSError):
        return None
