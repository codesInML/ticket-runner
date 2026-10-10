"""Project configuration and workflow preflight validation."""

from pathlib import Path
import json
import os
import re
import subprocess

from .display import paint
from .github import references
from .settings import (
    BOLD, BROWSER_ADD, BROWSER_SERVER, BROWSER_SKILL, CONFIG, DEFAULTS, EPIC_REVIEW_SKILLS,
    IMPLEMENT_SKILLS, INTEGRATION_SKILLS, MERGE_METHODS, SHIP_SKILLS, TICKET_REVIEW_SKILLS, VERIFY_SKILLS,
)
from .text import section


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


def stage_skills(a, cfg):
    """Exactly the skills the stages this run enables will name, in either workflow."""
    names, osmani, post = [], a.workflow == "osmani", not a.only
    if osmani and not a.epic_only:
        names += IMPLEMENT_SKILLS + TICKET_REVIEW_SKILLS + INTEGRATION_SKILLS
    if post and not a.no_verify and (cfg["verify"] or cfg["serve"]):
        names += VERIFY_SKILLS + ((BROWSER_SKILL,) if cfg["browser"] else ())
    if post and not a.no_epic_review and osmani:
        names += EPIC_REVIEW_SKILLS
    if post and not a.no_epic_review and not a.no_ship:
        names += SHIP_SKILLS
    return list(dict.fromkeys(names))


def plan_stages(a, cfg, data, open_tickets, head):
    """The stages a real run would go through, for --dry-run; a result still valid for HEAD is a skip."""
    def held(key, ok):
        rec = data.get(key, {})
        return rec.get("head") == head and rec.get("status") in ok
    lines = [paint("Stages:", BOLD)]
    if a.epic_only:
        lines.append("  tickets      skipped (--epic-only)")
    else:
        todo = [a.only] if a.only else open_tickets
        lines.append(f"  tickets      {len(todo)} to run" + (f" (--only #{a.only})" if a.only else ""))
    if a.only:
        lines.append("  verify, epic review, ship: skipped (--only)")
        return "\n".join(lines)
    runtime = (f"runtime checks: {cfg['serve']}" + (" + browser" if cfg["browser"] else "")) if cfg["serve"] \
        else "runtime checks off (no serve)"
    lines.append("  verify       " + ("skipped (--no-verify)" if a.no_verify else
                                      f"cached pass at {head[:8]}; inputs rechecked before skipping" if held("_verify", ("pass",)) else
                                      f"{len(cfg['verify'])} command(s); {runtime}"))
    if a.no_epic_review:
        lines.append("  epic review  skipped (--no-epic-review)")
        lines.append("  ship         skipped (no epic review)")
        return "\n".join(lines)
    lines.append("  epic review  " + (f"cached {data['_review']['status']} at {head[:8]}; evidence rechecked before skipping"
                                      if held("_review", ("ship", "fix-first")) else f"{a.epic_review_model}"))
    if a.no_ship:
        lines.append("  ship         skipped (--no-ship)")
    else:
        merge = f"; then --merge ({cfg['mergeMethod']}) once CI is green" if a.merge else ""
        into = cfg["prBase"] or "origin's default branch"
        lines.append("  ship         " + (f"pull request already open at {head[:8]}" if held("_ship", ("pr-open", "merged"))
                                          else f"launch plan, push, pull request into {into}") + merge +
                     " (only on a SHIP verdict)")
    return "\n".join(lines)


def stage_refusals(a, cfg, root):
    """Reasons the verify stage could not run as configured, found before any agent starts."""
    if a.only or a.no_verify or not cfg["serve"]:
        return []
    refusals = []
    env = cfg["serveEnv"]
    if not env:
        refusals.append(f"{CONFIG} sets serve but no serveEnv: point it at a local env file for the app "
                        "(never the developer's .env)")
    elif not (root / env).is_file():
        refusals.append(f"serveEnv {env} does not exist; create it, or remove serve from {CONFIG}")
    elif (root / ".env").exists() and (root / env).resolve() == (root / ".env").resolve():
        refusals.append("serveEnv must not be the developer's .env; give the app its own env file")
    if cfg["browser"]:
        listed = subprocess.run([os.getenv("COMMANDCODE_BIN", "command-code"), "mcp", "list"],
                                capture_output=True, text=True)
        if not re.search(rf"(?<![\w-]){BROWSER_SERVER}(?![\w-])", listed.stdout + listed.stderr):
            refusals.append(f"browser is on but CommandCode has no {BROWSER_SERVER} MCP server; add it with:\n"
                            f"  {BROWSER_ADD}\nor set \"browser\": false in {CONFIG}")
    return refusals


def skill_problems(skills_dir, names):
    """Skill files the prompts name, and the references they link, that are missing on disk."""
    missing = []
    for name in names:
        skill = skills_dir / name / "SKILL.md"
        if not skill.is_file():
            missing.append(str(skill))
            continue
        for linked in sorted(set(re.findall(r"\.\./\.\./references/[\w.-]+\.md", skill.read_text()))):
            if not (skill.parent / linked).resolve().is_file():
                missing.append(f"{os.path.normpath(skill.parent / linked)} (linked from {name})")
    return sorted(set(missing))
