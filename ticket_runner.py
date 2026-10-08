#!/usr/bin/env python3
"""Parallel GitHub ticket implementation and one epic review via CommandCode.

Every unblocked ticket runs in its own git worktree (.scratch/worktrees/<n>) on
branch cc/<parent>/<n>, cut from the epic tip when it starts; up to --jobs run
at once. Finished tickets integrate one at a time: rebase onto the epic tip,
run the gate when the tip moved, fast-forward the epic branch, then remove the
worktree and branch. A rebase conflict or gate failure resumes the ticket's own
session once; if that does not fix it the ticket needs a human. A failed ticket
holds back only the tickets that depend on it. Once every ticket has merged, the
epic review reads the whole epic diff once.

A workflow names the skills each prompt follows:
  pocock  one implement skill (IMPLEMENT_SKILL) whose own code review runs in sub-agents.
  osmani  Addy Osmani's agent skills. The implementer does not review itself; a ticket
          review — a fresh read-only session on another model family — sorts findings
          into BLOCKING, DEFERRED and REJECTED. BLOCKING findings get one fix round in the
          implementer's session, then a re-review by the same model; DEFERRED findings
          are consolidated by the epic review and posted once on the parent issue.
"""

import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time

MODELS = {
    "flash": "deepseek/deepseek-v4.1-flash",
    "flash-fast": "deepseek/deepseek-v4.1-flash-fast",
    "muse": "meta/muse-spark-1.3-contributor",
    "luna": "gpt-6-luna",
    "glm": "z-ai/glm-5.3-flash",
}
EPIC_REVIEW_MODEL = "claude-sonnet-5-5"
EFFORTS = {
    MODELS["flash"]: {"off", "low", "high", "max"},
    MODELS["flash-fast"]: {"off", "low", "high", "max"},
    MODELS["muse"]: {"low", "medium", "high", "xhigh"},
    MODELS["luna"]: {"low", "medium", "high", "xhigh", "max"},
    MODELS["glm"]: {"low", "high", "max"},
    EPIC_REVIEW_MODEL: {"low", "medium", "high", "xhigh", "max"},
}
WORKFLOWS = ("pocock", "osmani")
# Osmani skills the runner names in its prompts. Each links ../../references/*.md, which the
# skills installer leaves out; the preflight refuses to run while any of those files is missing.
OSMANI_SKILLS = ("incremental-implementation", "test-driven-development", "code-simplification",
                 "security-and-hardening", "api-and-interface-design", "debugging-and-error-recovery",
                 "performance-optimization", "doubt-driven-development", "code-review-and-quality",
                 "git-workflow-and-versioning")
OSMANI_SOURCE = "https://github.com/addyosmani/agent-skills.git"
RETRYABLE = {"turn-cap", "provider-error", "interrupted", "in-progress", "error"}
# Optional project config, relative to the project root. Every key is optional; these are the defaults.
CONFIG = ".agents/ticket-runner.json"
DEFAULTS = {"install": "npm ci", "lockfiles": ["package-lock.json"], "dependencyDirs": ["node_modules"],
            "linkFiles": [".env"], "gate": ["npx tsc --noEmit", "npm test"], "verify": [], "serve": None,
            "ready": None, "readyTimeout": 120, "serveEnv": None, "browser": False, "prBase": None,
            "mergeMethod": "merge"}
MERGE_METHODS = ("merge", "squash", "rebase")
# Copy-on-write clones keep each worktree's dependency directories isolated at near-zero cost.
CLONES = (["cp", "-cR"], ["cp", "-R"]) if sys.platform == "darwin" else (["cp", "-R", "--reflink=auto"],)
# NO_COLOR / FORCE_COLOR follow https://no-color.org conventions.
COLOR = bool(os.getenv("FORCE_COLOR")) or (
    not os.getenv("NO_COLOR") and os.getenv("TERM") != "dumb" and sys.stdout.isatty())
BOLD, DIM, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "1", "2", "31", "32", "33", "34", "35", "36"
TICKET_COLORS = ("36", "35", "33", "32", "34", "96", "95", "93", "92", "94")
FILE_TOOLS = {"read_file", "edit_file", "write_file"}
WRITE_TOOLS = {"edit_file", "write_file"}
PRINT = threading.Lock()


class Fatal(RuntimeError):
    """Run-wide problem: stop every ticket and exit."""


class Stop(Exception):
    """The run is being interrupted; unwind the ticket without touching its work."""


def paint(text, *codes):
    return f"\033[{';'.join(codes)}m{text}\033[0m" if COLOR and codes else text


def say(text, *codes, err=False):
    with PRINT:
        print(paint(text, *codes), file=sys.stderr if err else sys.stdout, flush=True)


def fit(text, used=0):
    width = shutil.get_terminal_size((120, 20)).columns - used
    text = " ".join(text.split())
    return text if len(text) <= width else text[:max(width - 1, 10)] + "…"


def describe(tool, args):
    if not isinstance(args, dict):
        return str(args)
    if tool in FILE_TOOLS:
        return os.path.relpath(args["file_path"]) if args.get("file_path") else ""
    if tool == "shell_command":
        return args.get("description") or args.get("command", "")
    if tool in {"grep", "glob"}:
        return f"{args.get('pattern', '')}  {os.path.relpath(args.get('path') or '.')}"
    return os.path.relpath(args["path"]) if args.get("path") else json.dumps(args)[:200]


def message(value):
    if isinstance(value, dict):
        return str(value.get("message") or json.dumps(value))
    return "" if value is None else str(value)


def narrate(event, started, tag, color):
    # Live progress feed; the full NDJSON stream still goes to attempt-N.jsonl.
    kind = event.get("type")
    elapsed = int(time.monotonic() - started)
    head = paint(f"[{elapsed // 60:02d}:{elapsed % 60:02d}]", DIM) + " " + paint(tag, BOLD, color)
    used = 9 + len(tag)
    if kind == "turn_start":
        say(f"{head} " + paint(f"── turn {event.get('turnNumber')} ──", DIM, color))
    elif kind == "message_end":
        content = event.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": message(content)}]
        text = " ".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            say(f"{head} {paint('AGENT', BOLD, color)} {fit(text, used + 6)}")
    elif kind == "tool_queued":
        tool = str(event.get("toolName", "?"))
        tint = MAGENTA if tool in WRITE_TOOLS else CYAN
        say(f"{head}   {paint('▸ ' + tool, tint)} {fit(describe(tool, event.get('input') or {}), used + 6 + len(tool))}")
    elif kind == "tool_errored":
        say(f"{head}   {paint('✗ ' + str(event.get('toolName', '?')), RED, BOLD)} {fit(message(event.get('error')), used + 24)}")
    elif kind in {"api_retry", "stream_restart"}:
        detail = message(event.get("error")) or f"attempt {event.get('attempt')}/{event.get('maxAttempts')}"
        say(f"{head} {paint('↻ ' + kind.replace('_', ' '), YELLOW)} {fit(detail, used + 16)}")
    elif kind == "run_error":
        say(f"{head} {paint('run error', RED, BOLD)} {message(event.get('error'))}")


def command(*args, cwd=None):
    return subprocess.check_output(args, text=True, cwd=cwd).strip()


def git(cwd, *args):
    return command("git", *args, cwd=cwd)


def attempt(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def dirty(cwd):
    return bool(git(cwd, "status", "--porcelain"))


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


def positive(value):
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise argparse.ArgumentTypeError("expected a positive integer")
    return int(value)


def waves(blockers):
    """Group tickets into dependency levels; blockers outside the map are ignored."""
    levels, pending = [], set(blockers)
    while pending:
        level = sorted(n for n in pending if not set(blockers[n]) & pending)
        if not level:
            raise RuntimeError(f"Dependency cycle among tickets: {sorted(pending)}")
        levels.append(level)
        pending.difference_update(level)
    return levels


def ready(pending, blockers, done, busy):
    """Pending tickets, in order, whose in-epic blockers have all merged."""
    return [n for n in pending if n not in busy and all(b in done for b in blockers[n] if b in blockers)]


def held_back(stuck, blockers):
    """Tickets that depend, directly or transitively, on a stuck ticket."""
    held = set()
    while True:
        new = {n for n, bs in blockers.items() if n not in held | set(stuck) and set(bs) & (held | set(stuck))}
        if not new:
            return held
        held |= new


# Graph glyph and colour per ticket kind, in the order the header counts them.
KINDS = {"done": ("✓", GREEN), "running": ("●", CYAN), "ready": ("○", BLUE), "waiting": ("·", DIM),
         "failed": ("✗", RED), "held": ("⊘", YELLOW), "deferred": ("⊘", YELLOW), "skipped": ("–", DIM)}


def classify(blockers, done, running=(), failed=(), deferred=(), skipped=()):
    """Kind of every ticket: done, running, failed, deferred, skipped, held, ready or waiting."""
    held = held_back(set(failed) | set(deferred), blockers)
    return {n: "done" if n in done else "running" if n in running else "failed" if n in failed else
               "deferred" if n in deferred else "skipped" if n in skipped else "held" if n in held else
               "ready" if all(b in done for b in bs if b in blockers) else "waiting"
            for n, bs in blockers.items()}


def render_graph(levels, tickets, kinds, labels=None):
    """Wave-by-wave picture of the epic; a wave that is all done collapses to one line."""
    labels = labels or {}
    counts = [f"{sum(k == kind for k in kinds.values())} {kind}" for kind in KINDS
              if any(k == kind for k in kinds.values())]
    lines = [paint("Ticket graph: ", BOLD) + " · ".join(counts)]
    for i, level in enumerate(levels, 1):
        wave = f"  wave {i:<2} "
        if all(kinds[n] == "done" for n in level):
            lines.append(paint(f"{wave}✓ all {len(level)} done: " + " ".join(f"#{n}" for n in level), DIM))
            continue
        for j, n in enumerate(level):
            kind = kinds[n]
            glyph, color = KINDS[kind]
            label = labels.get(n) or kind
            open_blockers = [b for b in tickets[n]["blockers"] if b in kinds and kinds[b] != "done"]
            if kind in {"waiting", "held"} and open_blockers:
                label += (" on " if kind == "waiting" else " by ") + " ".join(f"#{b}" for b in open_blockers)
            plain = f"{wave if j == 0 else ' ' * len(wave)}{glyph} #{n} {label:<22} "
            title = fit(tickets[n]["title"], len(plain))
            lines.append(paint(plain[:len(wave)], DIM) + paint(plain[len(wave):], color) +
                         paint(title, DIM) if kind == "done" else
                         paint(plain[:len(wave)], DIM) + paint(plain[len(wave):], color, BOLD) + title)
    return "\n".join(lines)


def verdict(text, pattern=r"STATUS=(COMPLETE|NEEDS_HUMAN|BLOCKED)"):
    lines = text.strip().splitlines()
    found = re.fullmatch(pattern + r"\s*", lines[-1]) if lines else None
    return found[1] if found else "MISSING"


def section(body, name):
    """Text under a `## <name>` heading up to the next heading, or None when there is no such heading."""
    found = re.search(rf"^#+\s*{name}\b[^\n]*$(.*?)(?=^#+\s|^VERDICT=|\Z)", body, re.M | re.S | re.I)
    return found[1].strip() if found else None


def relative(value):
    return isinstance(value, str) and bool(value.strip()) and not Path(value).is_absolute() \
        and ".." not in Path(value).parts


def load_config(root):
    """The project config over the generic defaults; refuses unknown keys, wrong types and unsafe paths."""
    path = root / CONFIG
    try:
        raw = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{CONFIG} is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        raise RuntimeError(f"{CONFIG} must hold a JSON object")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise RuntimeError(f"{CONFIG} has unknown key(s) {', '.join(unknown)}; allowed: {', '.join(DEFAULTS)}")
    cfg = {**DEFAULTS, **raw}
    text = lambda v: isinstance(v, str) and bool(v.strip())
    paths = lambda v: isinstance(v, list) and all(map(relative, v))
    rules = {"install": (text, "a non-empty string"),
             "lockfiles": (paths, "a list of relative paths"),
             "dependencyDirs": (paths, "a list of relative paths"),
             "linkFiles": (paths, "a list of relative paths"),
             "gate": (lambda v: isinstance(v, list) and all(map(text, v)), "a list of shell commands"),
             "verify": (lambda v: isinstance(v, list) and all(map(text, v)), "a list of shell commands"),
             "serve": (lambda v: v is None or text(v), "a shell command or null"),
             "ready": (lambda v: v is None or text(v), "a shell command or null"),
             "readyTimeout": (lambda v: type(v) is int and v > 0, "a positive integer (seconds)"),
             "serveEnv": (lambda v: v is None or relative(v), "a relative path or null"),
             "browser": (lambda v: type(v) is bool, "true or false"),
             "prBase": (lambda v: v is None or text(v), "a branch name or null"),
             "mergeMethod": (lambda v: v in MERGE_METHODS, f"one of {', '.join(MERGE_METHODS)}")}
    problems = [f"{key} must be {what}" for key, (ok, what) in rules.items() if not ok(cfg[key])]
    if not problems and cfg["serve"] and not cfg["ready"]:
        problems.append("serve needs a ready command to poll")
    if not problems and cfg["browser"] and not cfg["serve"]:
        problems.append("browser needs serve: the verifier can only browse a running app")
    if problems:
        raise RuntimeError(f"{CONFIG}: " + "; ".join(problems))
    return cfg


def plan_problems(tickets, parent_body):
    """Refusals and warnings for an Osmani plan: every ticket must carry the shared ticket template.

    The worst one is a dependency the runner cannot see: Osmani's own `Dependencies: Task 2` format
    names no issue, so without a Blocked by entry the dependent ticket would start in parallel.
    """
    refusals, warnings = [], []
    for n, ticket in sorted(tickets.items()):
        if ticket["state"] == "CLOSED":
            continue
        body = ticket.get("body") or ""
        for name in ("Acceptance criteria", "Verification"):
            if not section(body, name):
                refusals.append(f"#{n} has no `## {name}` section")
        scope = section(body, "Scope")
        size = re.search(r"\b(XS|S|M|L|XL)\b", scope or "")
        if not size:
            warnings.append(f"#{n} has no `## Scope` (XS, S, M, L)")
        elif size[1] == "XL":
            refusals.append(f"#{n} is XL; split it before running")
        elif size[1] == "L":
            warnings.append(f"#{n} is L; Osmani suggests splitting it")
        files = section(body, "Files likely touched")
        count = len(re.findall(r"^\s*[-*]\s+\S", files or "", re.M))
        if files is None:
            warnings.append(f"#{n} has no `## Files likely touched`")
        elif count > 8:
            refusals.append(f"#{n} likely touches {count} files; split it before running")
        clauses = re.findall(r"^[\s>*_-]*Dependencies\**\s*:\**(.*)$", body, re.M | re.I)
        clauses += [section(body, "Dependencies") or ""]
        clauses += re.findall(r"\bdepends on\s+((?:#\d+[\s,]*(?:and\s+)?)+)", body, re.I)
        for clause in filter(str.strip, clauses):
            named = references(clause)
            if not named and not re.match(r"[\s*_:-]*(none|nothing|n/a)\b", clause, re.I):
                refusals.append(f"#{n} names dependencies without issue numbers ({clause.strip()[:60]}); "
                                "list them under `## Blocked by`")
            missing = sorted(set(named) - set(ticket["blockers"]))
            if missing:
                refusals.append(f"#{n} depends on {' '.join(f'#{m}' for m in missing)} without a Blocked by "
                                "entry or native link, so it would start in parallel")
    if not re.search(r"checkpoint", parent_body, re.I):
        warnings.append("the parent issue has no checkpoints; the epic review will have none to check")
    return refusals, warnings


def skill_problems(skills_dir):
    """Osmani skill files the prompts name, and the references they link, that are missing on disk."""
    missing = []
    for name in OSMANI_SKILLS:
        skill = skills_dir / name / "SKILL.md"
        if not skill.is_file():
            missing.append(str(skill))
            continue
        for linked in sorted(set(re.findall(r"\.\./\.\./references/[\w.-]+\.md", skill.read_text()))):
            if not (skill.parent / linked).resolve().is_file():
                missing.append(f"{os.path.normpath(skill.parent / linked)} (linked from {name})")
    return sorted(set(missing))


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Model aliases: flash (default), flash-fast, muse, luna, glm.
Full CommandCode model IDs also work. Sonnet 5.5 runs the epic review by default.
Effort defaults: max for implementation (xhigh for Muse contributor), high for reviews.
Ticket review (osmani only) alternates by ticket number over --ticket-review-model:
even tickets get the first model, odd tickets the second; a re-review keeps its model.

Examples:
  ticket-runner 207 --dry-run
  ticket-runner 207 --jobs 2 --model muse
  ticket-runner 207 --resume --max-turns 1000
  ticket-runner 207 --only 209
  ticket-runner 207 --final-only
  ticket-runner-osmani 207 --dry-run       (same runner, --workflow osmani)
  ticket-runner-osmani 207 --ticket-review-model glm

Osmani plans: every open sub-ticket needs `## Acceptance criteria` and `## Verification`,
must not be XL or likely touch more than 8 files, and must list every dependency under
`## Blocked by` (or a native link). The plan check refuses otherwise, on --dry-run too;
--skip-plan-check overrides. The workflow is saved with the epic's state; switching it
is refused while any ticket is part-way through.

Requirements: a clean main worktree on the epic branch, and AGENTS.md committed
on it (ticket worktrees only see committed files). Leave the main worktree alone
while the runner works; it fast-forwards the epic branch there.

A ticket graph (done, running, ready, waiting, failed, held per wave) prints
before the run starts and again whenever a ticket finishes; --dry-run shows it too.

Project config (.agents/ticket-runner.json, optional JSON; every key optional):
  install         "npm ci"            runs when a merge changes a lockfile
  lockfiles       ["package-lock.json"]
  dependencyDirs  ["node_modules"]    cloned copy-on-write into each worktree
  linkFiles       [".env"]            symlinked into each worktree
  gate            ["npx tsc --noEmit", "npm test"]   run after a rebase onto a moved epic tip
  prBase, mergeMethod, verify, serve, ready, readyTimeout, serveEnv, browser
The config, AGENTS.md, linkFiles and serveEnv are protected: an agent changing one stops the run,
and a ticket whose diff touches one is not integrated. AGENTS.md may carry a `## Ticket Runner`
section naming the project's personal data, governed parameters, public contracts and
performance-sensitive areas; the prompts point agents at it.

Merged (done) and closed tickets are always skipped. --resume continues the saved
CommandCode session of an unfinished ticket in its kept worktree, and retries
integration of a ticket that finished but did not merge. Fix a needs-human ticket
in its worktree (commit, finish any rebase), then --resume. For awaiting-human,
answer on the issue, then --resume --only <n>. No changes are discarded.
Sub-tickets are the parent's native GitHub sub-issues plus any issue naming the parent
in a "## Parent" section or a "Parent…: #N" line. Blockers are the union of native
"blocked by" links, a "## Blocked by" section, and labelled "Blocked by:" lines;
prose that just says "blocked by" counts for nothing.
State/logs: .scratch/run-tickets-commandcode/<parent>/
Worktrees:  .scratch/worktrees/<n> on branch cc/<parent>/<n>
Env: COMMANDCODE_BIN, WORKFLOW, IMPLEMENT_SKILL (pocock), AGENT_SKILLS_DIR (osmani, default
     ~/.agents/skills), IMPL_MODEL, IMPL_EFFORT, IMPL_MAX_TURNS, IMPL_JOBS,
     TICKET_REVIEW_MODEL, TICKET_REVIEW_EFFORT, TICKET_REVIEW_MAX_TURNS,
     FINAL_MODEL, FINAL_EFFORT, FINAL_MAX_TURNS (the epic review),
     GATE_CMDS and INSTALL_CMD (override the config's gate and install).
""")
    p.add_argument("parent", type=positive)
    p.add_argument("--workflow", choices=WORKFLOWS, default=os.getenv("WORKFLOW"),
                   help="skills the prompts follow (default: the epic's saved workflow, else pocock)")
    p.add_argument("--model", default=os.getenv("IMPL_MODEL", "flash"))
    p.add_argument("--effort", default=os.getenv("IMPL_EFFORT"),
                   help="implementation effort; use 'none' to omit the flag")
    p.add_argument("--max-turns", type=positive, default=os.getenv("IMPL_MAX_TURNS", "500"))
    p.add_argument("--jobs", type=positive, default=os.getenv("IMPL_JOBS", "3"),
                   help="tickets implemented at once (default 3)")
    p.add_argument("--ticket-review-model", default=os.getenv("TICKET_REVIEW_MODEL", "luna,glm"),
                   help="comma-separated models ticket reviews alternate over (osmani; default luna,glm)")
    p.add_argument("--ticket-review-effort", default=os.getenv("TICKET_REVIEW_EFFORT", "high"))
    p.add_argument("--ticket-review-max-turns", type=positive, default=os.getenv("TICKET_REVIEW_MAX_TURNS", "500"))
    p.add_argument("--skip-plan-check", action="store_true", help="run an osmani epic despite plan check refusals")
    p.add_argument("--final-model", default=os.getenv("FINAL_MODEL", EPIC_REVIEW_MODEL))
    p.add_argument("--final-effort", default=os.getenv("FINAL_EFFORT", "high"))
    p.add_argument("--final-max-turns", type=positive, default=os.getenv("FINAL_MAX_TURNS", "500"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--only", type=positive)
    p.add_argument("--final-only", action="store_true")
    p.add_argument("--epic-base", help="commit the epic started from (default: saved state, else HEAD; "
                   "--final-only without saved state defaults to the merge-base with origin's default branch)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--no-final-review", action="store_true", help="skip the epic review")
    group.add_argument("--final-review", action="store_true", help="run the epic review; already the default")
    a = p.parse_args()
    if a.final_only and (a.only or a.no_final_review):
        p.error("--final-only cannot be combined with --only or --no-final-review")
    a.model = MODELS.get(a.model, a.model)
    a.final_model = MODELS.get(a.final_model, a.final_model)
    a.ticket_review_models = [MODELS.get(m, m) for m in a.ticket_review_model.replace(" ", "").split(",") if m]
    if not a.ticket_review_models:
        p.error("--ticket-review-model needs at least one model")
    if a.effort is None:
        a.effort = "xhigh" if a.model == MODELS["muse"] else "max"
    for model, effort in [(a.model, a.effort), (a.final_model, a.final_effort),
                          *((m, a.ticket_review_effort) for m in a.ticket_review_models)]:
        if model in EFFORTS and effort != "none" and effort not in EFFORTS[model]:
            p.error(f"{model} effort must be one of {sorted(EFFORTS[model])}, or none")
    return a


def references(text):
    """Issue numbers in a blocker clause: link targets and parenthetical asides don't count."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*/issues/(\d+)[^)]*\)", r"\1 #\2", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\([^)]*\)", "", text)
    if re.match(r"[\s*_:-]*(none|nothing)\b", text, re.I):
        return []
    return [int(n) for n in re.findall(r"#(\d+)", text)]


def parent_of(body):
    """The parent named by a `## Parent` section or a `Parent…: #N` line; a passing mention is not one."""
    section = re.search(r"^#+\s*Parent\s*$(.*?)(?=^#+\s|\Z)", body, re.M | re.S | re.I)
    line = re.search(r"^[\s>*_-]*Parent\b[^\n#]*?#(\d+)", body, re.M | re.I)
    first = re.search(r"#(\d+)", section[1]) if section else None
    return int(first[1]) if first else int(line[1]) if line else None


def blockers_of(body):
    """Blockers from a `## Blocked by` section and every labelled `Blocked by:` line or `· Blocked by:` field.

    Prose that merely says "blocked by" ("blocked by the vendor decision, see #12") names no blocker.
    """
    found = []
    for section in re.findall(r"^#+\s*Blocked by\s*$(.*?)(?=^#+\s|\Z)", body, re.M | re.S | re.I):
        for line in section.splitlines():
            found += references(re.sub(r"^[\s*-]*(Blocked by)?", "", line, flags=re.I))
    for clause in re.findall(r"(?:^|·)[ \t>*_-]*Blocked by\s*\**\s*:\**(.*)", body, re.M | re.I):
        found += references(clause.split(" · ")[0])
    return sorted(set(found))


def ref(blocker):
    """Display form of a blocker: #N in this repo, owner/repo#N elsewhere."""
    return f"#{blocker}" if isinstance(blocker, int) else blocker


def gh_json(*args):
    for tries in range(4):
        try:
            return json.loads(command("gh", *args))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            if tries == 3:
                raise RuntimeError(f"gh {' '.join(args[:2])} failed after four attempts")
            time.sleep(tries + 1)


EDGES = "blockedBy(first: 100) { totalCount nodes { number state repository { nameWithOwner } } }"


def native_links(parent, numbers):
    """GitHub's own sub-issue and blocked-by links: the parent's sub-issues, plus every ticket's blockers.

    /to-tickets prefers these native relationships, so a ticket may record its blockers nowhere else.
    """
    aliases = "\n".join(f"i{n}: issue(number: {n}) {{ {EDGES} }}" for n in sorted(numbers))
    query = f"""query($owner: String!, $name: String!, $parent: Int!) {{
  repository(owner: $owner, name: $name) {{
    nameWithOwner
    parent: issue(number: $parent) {{
      subIssues(first: 100) {{ totalCount nodes {{ number title body state repository {{ nameWithOwner }} {EDGES} }} }}
    }}
    {aliases}
  }}
}}"""
    repo = gh_json("api", "graphql", "-F", "owner={owner}", "-F", "name={repo}", "-F", f"parent={parent}",
                   "-f", f"query={query}")["data"]["repository"]
    children = repo["parent"]["subIssues"]
    if children["totalCount"] > len(children["nodes"]):
        raise RuntimeError(f"#{parent} has more than 100 sub-issues; split the epic before running")
    edges = {int(key[1:]): node["blockedBy"] for key, node in repo.items() if re.fullmatch(r"i\d+", key)}
    edges.update({c["number"]: c["blockedBy"] for c in children["nodes"]})
    for n, linked in edges.items():
        if linked["totalCount"] > len(linked["nodes"]):
            raise RuntimeError(f"#{n} has more than 100 blocked-by links; trim them before running")
    return repo["nameWithOwner"], children["nodes"], edges


def discover(parent):
    """Sub-tickets and their blockers, from issue bodies and GitHub's native links together; fails on cycles."""
    issues = gh_json("issue", "list", "--state", "all", "--limit", "1000", "--json", "number,title,body,state")
    if len(issues) == 1000:
        raise RuntimeError("Issue listing reached its limit; narrow discovery before running")
    tickets = {i["number"]: i for i in issues if i["number"] != parent and parent_of(i.get("body") or "") == parent}
    home, children, edges = native_links(parent, tickets)
    for child in children:
        if child["repository"]["nameWithOwner"] != home:
            say(f"Skipping sub-issue {child['repository']['nameWithOwner']}#{child['number']}: "
                f"it lives outside {home}", YELLOW, err=True)
            continue
        tickets.setdefault(child["number"], {k: child[k] for k in ("number", "title", "body", "state")})
    for n, issue in tickets.items():
        local, foreign = set(blockers_of(issue.get("body") or "")), []
        for node in edges.get(n, {"nodes": []})["nodes"]:
            where = node["repository"]["nameWithOwner"]
            if where == home:
                local.add(node["number"])
            elif node["state"] != "CLOSED":
                foreign.append(f"{where}#{node['number']}")
        # A ticket naming its own parent as a blocker would wait on the open epic forever.
        issue["blockers"] = sorted(local - {parent, n})
        issue["foreign"] = sorted(foreign)
    if not tickets:
        raise RuntimeError(f"No sub-tickets found for #{parent}")
    levels = waves({n: t["blockers"] for n, t in tickets.items()})
    return tickets, levels


def epic_base(a):
    if a.epic_base:
        return command("git", "rev-parse", "--verify", f"{a.epic_base}^{{commit}}")
    head = command("git", "rev-parse", "HEAD")
    if not a.final_only:
        return head
    # A fresh --final-only has no recorded start; review everything the branch adds to the default branch.
    default = subprocess.run(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
                             capture_output=True, text=True).stdout.strip() or "main"
    base = command("git", "merge-base", "HEAD", default)
    if base == head:
        raise RuntimeError(f"Nothing to review: HEAD has no commits beyond {default}. Pass --epic-base")
    return base


def main():
    a = parse_args()
    root = Path(command("git", "rev-parse", "--show-toplevel"))
    os.chdir(root)
    cfg = load_config(root)
    folder = root / ".scratch" / "run-tickets-commandcode" / str(a.parent)
    path = folder / "state.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    tickets, levels = discover(a.parent)
    order = [n for level in levels for n in level]
    if a.only and a.only not in tickets:
        raise RuntimeError(f"#{a.only} is not a sub-ticket of #{a.parent}")
    saved = data.get("_meta", {}).get("workflow")
    a.workflow = a.workflow or saved or "pocock"
    if saved and saved != a.workflow:
        # A part-way ticket's saved session was started under the other workflow's prompts.
        busy = [n for n in order if tickets[n]["state"] != "CLOSED"
                and data.get(str(n), {}).get("status") not in (None, "done")]
        if busy:
            raise RuntimeError(f"#{a.parent} runs the {saved} workflow and {' '.join(f'#{n}' for n in busy)} "
                               f"{'is' if len(busy) == 1 else 'are'} part-way through; finish them with "
                               f"--workflow {saved}, or reset them, before switching to {a.workflow}")
    osmani = a.workflow == "osmani"
    review = "off" if a.no_final_review else f"{a.final_model}/{a.final_effort}, {a.final_max_turns} turns"
    ticket_review = (f"; ticket review: {'/'.join(a.ticket_review_models)} @ {a.ticket_review_effort}, "
                     f"{a.ticket_review_max_turns} turns") if osmani else ""
    say(f"Workflow: {a.workflow}. Implement: {a.model}/{a.effort}, {a.max_turns} turns, {a.jobs} at once"
        f"{ticket_review}; epic review: {review}", DIM)
    if osmani and a.model in a.ticket_review_models:
        say(f"Warning: ticket review shares {a.model} with the implementer, and with it the same blind spots",
            YELLOW, err=True)
    skills_dir = Path(os.getenv("AGENT_SKILLS_DIR", str(Path.home() / ".agents/skills")))
    refusals = []
    if osmani:
        missing = skill_problems(skills_dir)
        for item in missing:
            say(f"Missing skill file: {item}", RED, err=True)
        if missing:
            refusals.append(f"{len(missing)} skill file(s) missing; restore the references with:\n"
                            f"  git clone --depth 1 --filter=blob:none --sparse {OSMANI_SOURCE} /tmp/agent-skills && "
                            f"git -C /tmp/agent-skills sparse-checkout set references && "
                            f"cp -R /tmp/agent-skills/references {skills_dir.parent}/")
        if not a.final_only:
            parent_body = gh_json("issue", "view", str(a.parent), "--json", "body")["body"] or ""
            problems, warnings = plan_problems(tickets, parent_body)
            for item in warnings:
                say(f"Plan check warning: {item}", YELLOW, err=True)
            for item in problems:
                say(f"Plan check refusal: {item}", RED, err=True)
            if problems and not a.skip_plan_check:
                refusals.append(f"plan check refused {len(problems)} problem(s); fix the tickets, "
                                "or pass --skip-plan-check")
    if a.dry_run:
        done = {n for n in order if tickets[n]["state"] == "CLOSED" or data.get(str(n), {}).get("status") == "done"}
        say(render_graph(levels, tickets, classify({n: t["blockers"] for n, t in tickets.items()}, done)))
        say("Dry run: no agents, file writes or GitHub edits.", DIM)
        for item in refusals:
            say(f"A real run would refuse: {item}", RED, err=True)
        return 1 if refusals else 0
    if refusals:
        raise RuntimeError("; ".join(refusals))
    if not shutil.which(os.getenv("COMMANDCODE_BIN", "command-code")):
        raise RuntimeError("CommandCode binary not found; set COMMANDCODE_BIN")
    branch = command("git", "branch", "--show-current")
    if not branch:
        raise RuntimeError("Check out a branch before running")
    if "_meta" not in data or a.epic_base:
        data["_meta"] = {**data.get("_meta", {}), "branch": data.get("_meta", {}).get("branch", branch),
                         "epicBase": epic_base(a)}
    meta = data["_meta"]
    meta["workflow"] = a.workflow
    if meta["branch"] != branch:
        raise RuntimeError(f"Saved state belongs to branch {meta['branch']}")
    if command("git", "merge-base", meta["epicBase"], "HEAD") != meta["epicBase"]:
        raise RuntimeError("Saved epic base is not an ancestor of HEAD")
    if attempt(root, "cat-file", "-e", "HEAD:AGENTS.md").returncode:
        raise RuntimeError(f"AGENTS.md is not committed on {branch}; ticket worktrees would not see it. "
                           "Commit it on this branch first.")
    if dirty(root):
        raise RuntimeError("Dirty main worktree; commit or stash first. No files discarded.")
    skill = Path(os.getenv("IMPLEMENT_SKILL", str(Path.home() / ".agents/skills/implement/SKILL.md")))
    if not osmani and not a.final_only and not skill.is_file():
        raise RuntimeError(f"Implementation skill not found: {skill}")
    folder.mkdir(parents=True, exist_ok=True)
    ignore = root / ".scratch/.gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    # One runner per checkout: different epics must not race the main worktree.
    with (folder.parent / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another ticket runner is active in this checkout")
        return Runner(a, root, folder, path, data, tickets, levels, skill, skills_dir, cfg).execute()


class Runner:
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
        home = Path(__file__).resolve().parent
        project = ["AGENTS.md", CONFIG, *cfg["linkFiles"], *([cfg["serveEnv"]] if cfg["serveEnv"] else [])]
        project = list(dict.fromkeys(project))
        protected = [Path(__file__).resolve(), home / "bin/ticket-runner", home / "bin/ticket-runner-osmani",
                     *(root / f for f in project)]
        # A protected file that is missing now must stay missing: an agent creating one counts as changing it.
        self.snapshots = {p: p.read_bytes() if p.exists() else None for p in protected}
        self.protected = project
        self.hands_off = ", ".join(project)
        notes = (root / "AGENTS.md").read_text(errors="replace") if (root / "AGENTS.md").exists() else ""
        self.notes = section(notes, "Ticket Runner") is not None

    # ── state ──────────────────────────────────────────────────────────────

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

    # ── processes ──────────────────────────────────────────────────────────

    def spawn_child(self, argv, **kwargs):
        with self.spawn:
            if self.stopping.is_set():
                raise Stop()
            child = subprocess.Popen(argv, start_new_session=True, **kwargs)
            self.children.add(child)
        return child

    def reap(self, child):
        with self.spawn:
            self.children.discard(child)

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

    def run_agent(self, model, effort, turns, prompt, folder, record, session=None, cwd=None,
                  tag="review", color=BLUE):
        cwd = cwd or self.root
        folder.mkdir(parents=True, exist_ok=True)
        number = record.get("attempts", 0) + 1
        self.update(record, attempts=number, model=model, effort=effort, maxTurns=turns)
        argv = [self.binary, "-p", prompt, "-m", model, "--max-turns", str(turns),
                "--output-format", "json", "--yolo", "--trust", "--skip-onboarding", "--no-auto-update"]
        if effort and effort != "none":
            argv.extend(["--effort", effort])
        if session:
            argv.extend(["--resume", session])
        # Separate stdout NDJSON from stderr, and preserve every attempt's logs.
        events = folder / f"attempt-{number}.jsonl"
        result = None
        started = time.monotonic()
        say(f"{tag} streaming to {os.path.relpath(events)}", DIM)
        with events.open("w") as output, (folder / f"attempt-{number}.log").open("w") as errors:
            child = self.spawn_child(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=errors, text=True)
            try:
                for line in child.stdout:
                    output.write(line)
                    output.flush()
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    event = frame.get("event") if isinstance(frame.get("event"), dict) else {}
                    # Session IDs may arrive before the final result, allowing interrupt recovery.
                    sid = frame.get("sessionId") or event.get("sessionId")
                    if isinstance(sid, str) and sid and sid != record.get("sessionId"):
                        self.update(record, sessionId=sid)
                    if frame.get("type") == "result":
                        result = frame
                    elif event:
                        try:
                            narrate(event, started, tag, color)
                        except Exception as exc:  # the feed is cosmetic; never let it kill the agent
                            say(f"{tag} unreadable progress event ({type(exc).__name__}); see {events.name}", DIM)
                rc = child.wait()
            except BaseException:
                kill(child)
                self.update(record, status="interrupted", head=git(cwd, "rev-parse", "HEAD"))
                raise
            finally:
                self.reap(child)
        head = git(cwd, "rev-parse", "HEAD")
        if self.stopping.is_set():
            self.update(record, status="interrupted", head=head)
            raise Stop()
        text = str((result or {}).get("finalText") or "")
        (folder / "final.txt").write_text(text)
        if rc == 8 or (result or {}).get("subtype") == "max_turns":
            status = "turn-cap"
        elif rc or not result or result.get("subtype") != "success" or not text.strip():
            status = "provider-error" if rc in {3, 5, 6, 7, 10} else "no-output"
        else:
            status = "reported"
        self.update(record, exitCode=rc, head=head, status=status)
        return text

    # ── scheduling ─────────────────────────────────────────────────────────

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
        return self.review() if outcome is None else outcome

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
            say(f"Epic incomplete: {remaining}. Epic review deferred.", YELLOW)
            return 1
        if self.a.only and (failed or pending or self.parked or self.deferred):
            return 1
        if self.a.only or self.a.no_final_review:
            say("Ticket run finished; epic review skipped.", GREEN)
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
        say("\n" + render_graph(self.levels, self.tickets, kinds, labels) + "\n")

    def stop(self):
        with self.spawn:
            self.stopping.set()
            children = list(self.children)
        for child in children:
            kill(child)
        for thread in self.threads.values():
            thread.join(timeout=30)
        with self.state:
            for n in self.threads:
                record = self.data.get(str(n), {})
                if record.get("status") in {"in-progress", "in-review", "reported", "error"}:
                    record["status"] = "interrupted"
            self.persist()

    # ── one ticket ─────────────────────────────────────────────────────────

    def branch(self, n):
        return f"cc/{self.a.parent}/{n}"

    def work(self, n):
        record = self.record(n)
        try:
            tdir = self.folder / str(n)
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "ticket.md").write_text(f"# #{n} — {self.tickets[n]['title']}\n\n{self.tickets[n]['body']}")
            phase = record.get("phase") if self.a.resume else None
            if phase not in ("review", "fix", "integrate"):
                self.implement(n, record, tdir)
            if self.osmani and (record.get("status") == "ready-to-review" or phase in ("review", "fix")):
                self.review_ticket(n, record, tdir)
            if record.get("status") == "ready-to-merge" or phase == "integrate":
                with self.integration:
                    self.integrate(n, record, tdir)
        except Stop:
            if record.get("status") in {"in-progress", "in-review", "reported", "error"}:
                self.update(record, status="interrupted")
        except Fatal as exc:
            self.fatal = exc
        except Exception as exc:
            status = "interrupted" if self.stopping.is_set() else "error"
            self.update(record, status=status, reason=str(exc))
            if status == "error":
                say(f"#{n} error: {exc}", RED, BOLD)
        finally:
            self.events.put(n)

    def fail(self, n, record, status, reason):
        self.update(record, status=status, reason=reason)
        self.label(n, "needs-human" if status in {"needs-human", "awaiting-human"} else "ready-for-agent",
                   "in-progress")
        say(f"#{n} {status}: {reason}", YELLOW, BOLD)

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

    def implement(self, n, record, tdir):
        branch, wt = self.branch(n), self.worktrees / str(n)
        answered = record.get("status") == "awaiting-human"
        session = record.get("sessionId") if self.a.resume and (
            record.get("status") in RETRYABLE or answered) else None
        with self.integration:
            if self.stopping.is_set():
                raise Stop()
            if not wt.exists():
                self.worktrees.mkdir(parents=True, exist_ok=True)
                tip = git(self.root, "rev-parse", self.epic)
                if attempt(self.root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode:
                    git(self.root, "worktree", "add", "-q", "-b", branch, str(wt), tip)
                    base = tip
                else:
                    git(self.root, "worktree", "add", "-q", str(wt), branch)
                    base = record.get("base") or git(self.root, "merge-base", branch, tip)
            else:
                base = record.get("base") or git(wt, "merge-base", "HEAD", self.epic)
            self.provision(wt)
        head = git(wt, "rev-parse", "HEAD")
        if session and record.get("head") != head:
            return self.fail(n, record, "needs-human", f"HEAD in {os.path.relpath(wt)} changed since the saved "
                             "session; reconcile it, then --resume")
        if dirty(wt) and not session:
            return self.fail(n, record, "needs-human", f"{os.path.relpath(wt)} is dirty without a resumable "
                             "session; commit or discard there, then --resume")
        self.update(record, status="in-progress", phase="implement", base=base, branch=branch,
                    worktree=str(wt), drop=() if session else ("sessionId", "reason"))
        self.label(n, "in-progress", "ready-for-agent")
        color = self.color(n)
        say(f"\n━━ #{n}: {'resuming' if session else 'implementing'} with {self.a.model} "
            f"in {os.path.relpath(wt)} ━━", BOLD, color)
        say(f"#{n} {self.tickets[n]['title']}", BOLD, color)
        prompt = self.osmani_brief(n, tdir) if self.osmani else f"""Read AGENTS.md and {self.skill}, then follow the implementation workflow for issue #{n}.
Read the full ticket at {tdir / 'ticket.md'}. Satisfy its acceptance criteria without expanding scope.
Use TDD at appropriate seams; decide and document seams yourself in this unattended run.
Run typechecking, targeted tests, the full suite once, and the workflow's code review before finishing.
"""
        prompt += f"""Use only CommandCode for any agent/review work; do not invoke codex or claude CLIs.
You are in a dedicated git worktree for this ticket. Other tickets of this epic run in parallel in
their own worktrees; the runner integrates them, so ignore them.
Commit buildable work onto the current branch {branch}, referencing #{n}.
Do not push, open PRs, change branches, rebase, merge, or add co-author trailers. Do not modify the
runner, {self.hands_off}, agent configuration, hooks, or anything under {self.folder}. Existing partial
edits/commits from this ticket are yours to finish when resuming. Pre-ticket commit: {base}.
If an acceptance criterion requires a human decision, commit all buildable work and report it.
End with exactly one line: STATUS=COMPLETE, STATUS=NEEDS_HUMAN, or STATUS=BLOCKED.
"""
        if answered:
            prompt += (f"\nYou stopped earlier to ask a human. Read their answer in the latest comments of "
                       f"gh issue view {n} --comments, then continue.\n")
        text = self.run_agent(self.a.model, self.a.effort, self.a.max_turns, prompt, tdir, record,
                              session, cwd=wt, tag=f"#{n}", color=color)
        self.guard()
        if record["status"] != "reported":
            return self.fail(n, record, record["status"], f"partial work kept in {os.path.relpath(wt)}; retry with "
                             f"--resume --max-turns {self.a.max_turns * 2}")
        status = verdict(text)
        commits = int(git(wt, "rev-list", "--count", f"{base}..HEAD"))
        problem = self.check_worktree(wt, branch)
        if status == "COMPLETE" and commits and not dirty(wt) and not problem and self.osmani:
            self.update(record, status="ready-to-review", phase="review", drop=("reason",))
            say(f"#{n} implemented ({commits} commit{'s' * (commits != 1)}); handing to ticket review", BOLD, color)
        elif status == "COMPLETE" and commits and not dirty(wt) and not problem:
            self.update(record, status="ready-to-merge", phase="integrate", drop=("reason",))
            say(f"#{n} implemented ({commits} commit{'s' * (commits != 1)}); waiting to integrate", BOLD, color)
        elif status == "NEEDS_HUMAN" and not dirty(wt) and not problem:
            self.fail(n, record, "awaiting-human", f"agent needs a decision; see {tdir / 'final.txt'}")
        else:
            self.fail(n, record, "needs-human", problem or f"status={status}, commits={commits}, "
                      f"dirty={dirty(wt)}; see {tdir}")

    def skill_file(self, name):
        return self.skills_dir / name / "SKILL.md"

    def domain(self):
        """Where a prompt learns the project's sensitive areas: AGENTS.md's `## Ticket Runner` section."""
        if self.notes:
            return ("The `## Ticket Runner` section of AGENTS.md lists what this project counts as personal or "
                    "sensitive data,\ngoverned parameters, public contracts and performance-sensitive areas; "
                    "use it for the triggers below.\n")
        return ("AGENTS.md has no `## Ticket Runner` section, so use general judgement for what counts as personal "
                "or\nsensitive data, a governed parameter, a public contract or a performance-sensitive area.\n")

    def osmani_brief(self, n, tdir):
        s = self.skill_file
        return f"""Read AGENTS.md, then implement issue #{n} by following these skills in order:
{s('incremental-implementation')}, then {s('test-driven-development')} for every slice, then
{s('code-simplification')} on your own diff only.
Read the full ticket at {tdir / 'ticket.md'}. Satisfy its acceptance criteria and its Verification
section without expanding scope. {self.domain()}Load a conditional skill only when the ticket needs it:
- {s('security-and-hardening')}: auth, untrusted input, or personal or sensitive data
- {s('api-and-interface-design')}: a public contract or module interface changes
- {s('performance-optimization')}: the ticket touches a performance-sensitive area
- {s('debugging-and-error-recovery')}: a test or build breaks for a reason you do not yet understand
- {s('doubt-driven-development')}: only for claims a test cannot pin down (invariants, cross-module
  contracts, migrations, anything irreversible). One doubt cycle per claim in this unattended run; a
  claim that still has substantive findings after it is a reason to stop with STATUS=NEEDS_HUMAN.
Do not review your own work: a separate ticket reviewer reviews this ticket once you finish.
Run typechecking, the ticket's focused tests, and the full suite once before finishing.
Nobody can answer questions during this run. Decide and record anything that does not change what an
acceptance criterion means (seams, naming, file placement, which existing pattern to follow): pick
what the surrounding code does, and list each choice under an `## ASSUMPTIONS` heading in your final
report. Stop instead (commit buildable work, state the question, end with STATUS=NEEDS_HUMAN) when the
ticket contradicts the code or another ticket, when an acceptance criterion can be read two ways that
lead to different behavior, or when meeting it would change a governed parameter or a public contract the
ticket does not mention.
"""

    def repair_guidance(self, kind):
        if not self.osmani:
            return ""
        if kind == "conflict":
            return (f"Follow {self.skill_file('git-workflow-and-versioning')} for the resolution, and re-run the "
                    "focused tests from the ticket's Verification section.\n")
        return (f"Follow {self.skill_file('debugging-and-error-recovery')}: reproduce, localize, fix, then guard "
                "with a regression test, since nobody else will cover this interaction between tickets.\n")

    # ── ticket review (osmani) ─────────────────────────────────────────────

    def reviewer_for(self, n):
        # Even tickets get the first model, odd the second: balanced, and readable from the number.
        pool = self.a.ticket_review_models
        return pool[n % len(pool)]

    def review_prompt(self, n, tdir, base, round_):
        s = self.skill_file
        again = f"""This is the re-review after one fix round. Your earlier report is at
{tdir / 'review' / 'round-1' / 'final.txt'} and the fix round's report at {tdir / 'fix' / 'final.txt'}.
Confirm every BLOCKING finding there is fixed and raise anything the fix broke; no fix round follows.
""" if round_ > 1 else ""
        return f"""You are the ticket reviewer for issue #{n} of epic #{self.a.parent}. Someone else wrote this
code; review it with fresh eyes and take none of their claims on trust.
Read AGENTS.md and the ticket at {tdir / 'ticket.md'}, then follow {s('code-review-and-quality')}
on git diff {base}..HEAD and git log {base}..HEAD in this worktree. {self.domain()}Load these only when the diff needs them:
- {s('security-and-hardening')}: auth, untrusted input, or personal or sensitive data
- {s('performance-optimization')}: a performance-sensitive area
- {s('api-and-interface-design')}: a public contract or module interface
The implementer's final report is at {tdir / 'final.txt'}. Check every choice under its ASSUMPTIONS
heading and raise each wrong one as a finding.
{again}Report under these markdown headings, citing file and line for every finding:
## ACCEPTANCE CRITERIA: every criterion MET or UNMET, with evidence (test name, file:line, command output).
## BLOCKING: real defects this ticket owns, and every UNMET criterion. A pre-existing defect is BLOCKING
when this ticket's change depends on it or makes it worse.
## DEFERRED: real defects this ticket neither caused, depends on, nor worsened: pre-existing code, or
another ticket's job (name it when you know it). Write "None." when there are none.
## REJECTED: findings you considered and dismissed (style, taste, a misread), one line each.
Read-only review: do not edit files, commit, change branches, push, or post comments.
Use only CommandCode for agent work; do not invoke codex or claude CLIs.
End with exactly one line: VERDICT=PASS when nothing is BLOCKING, otherwise VERDICT=FAIL.
"""

    def review_ticket(self, n, record, tdir):
        branch, wt, base, color = self.branch(n), Path(record["worktree"]), record["base"], self.color(n)
        with self.state:
            rec = record.setdefault("ticketReview", {})
        if record.get("phase") == "fix" and not self.fix(n, record, rec, tdir):
            return
        while True:
            round_, model = rec.get("round", 1), rec.get("model") or self.reviewer_for(n)
            head = git(wt, "rev-parse", "HEAD")
            session = rec.get("sessionId") if self.a.resume and rec.get("status") in RETRYABLE \
                and rec.get("head") == head else None
            self.update(record, status="in-review", phase="review")
            self.update(rec, round=round_, model=model, drop=() if session else ("sessionId",))
            say(f"\n━━ #{n}: ticket review round {round_} with {model} ━━", BOLD, color)
            folder = tdir / "review" / f"round-{round_}"
            text = self.run_agent(model, self.a.ticket_review_effort, self.a.ticket_review_max_turns,
                                  self.review_prompt(n, tdir, base, round_), folder, rec, session, cwd=wt,
                                  tag=f"#{n} review", color=color)
            self.guard()
            if dirty(wt) or git(wt, "rev-parse", "HEAD") != head or self.check_worktree(wt, branch):
                return self.fail(n, record, "needs-human", f"the ticket review changed {os.path.relpath(wt)}, "
                                 "but it must be read-only; inspect the worktree, then --resume")
            if rec["status"] != "reported":
                return self.fail(n, record, rec["status"], f"ticket review ended {rec['status']}; "
                                 f"--resume retries it (logs in {folder})")
            result = verdict(text, r"VERDICT=(PASS|FAIL)")
            self.update(rec, status=result.lower())
            if result == "PASS":
                self.update(record, status="ready-to-merge", phase="integrate", drop=("reason",))
                return say(f"#{n} passed ticket review round {round_}; waiting to integrate", GREEN, BOLD)
            if result != "FAIL":
                return self.fail(n, record, "needs-human", f"ticket review gave no verdict; see {folder / 'final.txt'}")
            if round_ > 1:
                return self.fail(n, record, "needs-human", "ticket review still blocking after one fix round; "
                                 f"see {folder / 'final.txt'}; fix it in the worktree, then --resume re-reviews")
            say(f"#{n} ticket review found blocking problems; one fix round", YELLOW, BOLD)
            self.update(record, status="in-progress", phase="fix")
            self.update(rec, round=2, drop=("sessionId",))
            if not self.fix(n, record, rec, tdir):
                return

    def fix(self, n, record, rec, tdir):
        """One fix round in the implementer's own session, scoped to the ticket review's BLOCKING list."""
        branch, wt, color = self.branch(n), Path(record["worktree"]), self.color(n)
        answered = record.get("status") == "awaiting-human"
        report = tdir / "review" / "round-1" / "final.txt"
        say(f"#{n} fixing ticket review findings with {self.a.model}", BOLD, color)
        prompt = f"""A separate ticket reviewer found blocking problems in your work on #{n}; read the report at
{report}. Fix every finding under BLOCKING and every UNMET acceptance criterion, and nothing else: DEFERRED
and REJECTED findings are not this ticket's to act on. Follow {self.skill_file('test-driven-development')}
for each fix that changes behavior. If you disagree with a finding, say why in your final report; the same
reviewer re-reviews once. Run typechecking, the focused tests, and the full suite once, then commit onto
{branch} referencing #{n}. Do not push, rebase, merge, change branches, or add co-author trailers. Do not
modify the runner, {self.hands_off}, agent configuration, hooks, or anything under {self.folder}.
End with exactly one line: STATUS=COMPLETE, STATUS=NEEDS_HUMAN, or STATUS=BLOCKED.
"""
        if answered:
            prompt += (f"\nYou stopped earlier to ask a human. Read their answer in the latest comments of "
                       f"gh issue view {n} --comments, then continue.\n")
        text = self.run_agent(self.a.model, self.a.effort, self.a.max_turns, prompt, tdir / "fix", record,
                              record.get("sessionId"), cwd=wt, tag=f"#{n}", color=color)
        self.guard()
        if record["status"] != "reported":
            self.fail(n, record, record["status"], f"fix round ended {record['status']}; partial work kept in "
                      f"{os.path.relpath(wt)}; --resume continues it")
            return False
        status = verdict(text)
        problem = "uncommitted changes remain" if dirty(wt) else self.check_worktree(wt, branch)
        if status == "NEEDS_HUMAN" and not problem:
            self.fail(n, record, "awaiting-human", f"agent needs a decision; see {tdir / 'fix' / 'final.txt'}")
            return False
        if status != "COMPLETE" or problem:
            self.fail(n, record, "needs-human", f"fix round ended status={status}"
                      f"{'; ' + problem if problem else ''}; see {tdir / 'fix'}")
            return False
        self.update(record, status="ready-to-review", phase="review")
        return True

    # ── integration (caller holds self.integration) ───────────────────────

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

    def log(self, tdir, kind):
        return tdir / f"{kind}-{len(list(tdir.glob(f'{kind}-*.log'))) + 1}.log"

    def shell(self, cmd, cwd, log):
        with log.open("w") as out:
            child = self.spawn_child(cmd, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
            try:
                rc = child.wait()
            finally:
                self.reap(child)
        if self.stopping.is_set():
            raise Stop()
        return rc

    def gate(self, n, wt, tdir):
        log = self.log(tdir, "gate")
        say(f"#{n} gate: {self.gate_cmd}", BOLD, self.color(n))
        rc = self.shell(self.gate_cmd, wt, log)
        say(f"#{n} gate {'passed' if rc == 0 else 'failed'} ({os.path.relpath(log)})",
            BOLD, GREEN if rc == 0 else RED)
        return rc == 0, log

    # ── epic review ────────────────────────────────────────────────────────

    def review(self):
        a = self.a
        if dirty(self.root):
            raise RuntimeError("Epic review requires a clean tree")
        base = self.data["_meta"]["epicBase"]
        head = git(self.root, "rev-parse", "HEAD")
        review = self.data.setdefault("_review", {})
        session = review.get("sessionId") if a.resume and review.get("status") in RETRYABLE and review.get("head") == head else None
        self.update(review, status="in-progress", head=head, drop=() if session else ("sessionId",))
        say(f"\n━━ Epic review of #{a.parent} with {a.final_model} ━━", BOLD, BLUE)
        checks = self.epic_brief() if self.osmani else """Check integration between slices, every parent requirement (MET/UNMET with evidence),
and real correctness/security defects. Cite file and line for each actionable finding.
Read-only review: do not edit files, commit, change branches, push, or post comments.
Use only CommandCode for agent work. Report INTEGRATION, EPIC SPEC, DEFECTS, and VERDICT.
"""
        text = self.run_agent(a.final_model, a.final_effort, a.final_max_turns, f"""You are the final reviewer for epic #{a.parent}.
Read AGENTS.md, the parent spec with gh issue view {a.parent}, and each ticket in {self.order}.
Review git diff {base}..{head} and git log {base}..{head} as a whole.
{checks}End with exactly one line: VERDICT=SHIP or VERDICT=FIX-FIRST.
""", self.folder / "review", review, session)
        self.guard()
        if dirty(self.root) or git(self.root, "rev-parse", "HEAD") != head:
            self.update(review, status="modified-tree")
        elif review["status"] == "reported":
            self.update(review, status="ship" if text.strip().splitlines()[-1] == "VERDICT=SHIP" else "fix-first")
            if self.osmani:
                self.post_deferred(text, review)
        print(text)
        say(f"Epic review: {review['status']}. Report: {self.folder / 'review/final.txt'}",
            GREEN if review["status"] == "ship" else RED, BOLD)
        return 0 if review["status"] == "ship" else 1

    def epic_brief(self):
        s = self.skill_file
        reports = sorted(self.folder.glob("*/review/round-*/final.txt"))
        listed = "\n".join(f"  {r}" for r in reports) or "  (none found)"
        return f"""{self.domain()}Follow {s('code-review-and-quality')} across that whole diff; load {s('security-and-hardening')}
only when the epic touches auth, untrusted input or personal or sensitive data, and
{s('performance-optimization')} only when it touches a performance-sensitive area.
Every ticket passed its own ticket review; the reports are:
{listed}
Their DEFERRED findings are real defects that no ticket owned.
Report under these markdown headings, in order, citing file and line for each actionable finding:
## INTEGRATION: how the slices fit together.
## EPIC SPEC: every parent requirement MET or UNMET, with evidence.
## CHECKPOINTS: every checkpoint in the parent plan MET or UNMET, with evidence.
## DEFECTS: real correctness and security defects.
## DEFERRED: the ticket reviews' DEFERRED findings consolidated. Merge duplicates, drop any the epic has
since fixed, confirm the rest still hold, and add any of your own that no ticket owns. Write "None." when
nothing remains. The runner posts this section on #{self.a.parent} as written.
Read-only review: do not edit files, commit, change branches, push, or post comments.
Use only CommandCode for agent work.
"""

    def post_deferred(self, text, review):
        """The runner, not the reviewer, posts the epic's consolidated DEFERRED findings: once per reviewed head."""
        found = section(text, "DEFERRED")
        if not found or re.fullmatch(r"none\.?", found, re.I) or review.get("deferredPosted") == review["head"]:
            return
        body = self.folder / "review" / "deferred.md"
        body.write_text(f"Deferred findings from the epic review of #{self.a.parent} at `{review['head'][:8]}`: "
                        f"real defects that no ticket in this epic owned.\n\n{found}\n")
        result = subprocess.run(["gh", "issue", "comment", str(self.a.parent), "--body-file", str(body)],
                                capture_output=True, text=True)
        if result.returncode:
            return say(f"Warning: could not post deferred findings on #{self.a.parent} ({result.stderr.strip()}); "
                       f"they are in {body}", YELLOW, err=True)
        self.update(review, deferredPosted=review["head"])
        say(f"Posted the epic's deferred findings on #{self.a.parent}", DIM)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("Interrupted; partial work preserved. Re-run with --resume.", YELLOW, err=True)
        sys.exit(130)
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        say(f"Error: {exc}", RED, BOLD, err=True)
        sys.exit(1)