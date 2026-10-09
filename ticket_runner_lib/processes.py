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
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def probe(cmd, cwd, env):
    """One run of the `ready` command; a hung probe counts as not ready."""
    try:
        return subprocess.run(cmd, shell=True, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except subprocess.TimeoutExpired:
        return False


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
