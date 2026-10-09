"""Git commands, child process cleanup, state writes, and runtime probes."""

from pathlib import Path
import json
import os
import re
import signal
import subprocess


def command(*args, cwd=None):
    return subprocess.check_output(args, text=True, cwd=cwd).strip()


def git(cwd, *args):
    return command("git", *args, cwd=cwd)


def attempt(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def dirty(cwd):
    return bool(git(cwd, "status", "--porcelain"))


def edited(cwd):
    """Changes to tracked files: what the app under test writes (logs, pids, uploads) is untracked."""
    return bool(git(cwd, "status", "--porcelain", "--untracked-files=no"))


def untracked(cwd):
    return set(git(cwd, "ls-files", "--others", "--exclude-standard").splitlines())


def rebasing(cwd):
    return any(Path(cwd, git(cwd, "rev-parse", "--git-path", d)).exists() for d in ("rebase-merge", "rebase-apply"))


def kill(child):
    # A shell may exit while background descendants keep its process group alive.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS can return EPERM for an already terminated orphan group.
        # A live leader must still surface a genuine cleanup failure.
        if child.poll() is None:
            raise
    child.wait()


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def probe(cmd, cwd, env, timeout=30):
    """One bounded readiness attempt, including cleanup of shell descendants."""
    child = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return child.wait(timeout=max(0.001, timeout)) == 0
    except subprocess.TimeoutExpired:
        return False
    finally:
        kill(child)


def read_env(path):
    """KEY=VALUE pairs of an env file, for the app's process only; values are never printed."""
    pairs = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if line.startswith("#") or not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            continue
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        pairs[key.strip()] = value
    return pairs
