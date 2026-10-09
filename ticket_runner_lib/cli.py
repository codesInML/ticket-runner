"""Parallel GitHub ticket implementation, then verify, epic review and ship stages, via CommandCode.

Every unblocked ticket runs in its own git worktree (.scratch/worktrees/<n>) on
branch cc/<parent>/<n>, cut from the epic tip when it starts; up to --jobs run
at once. Finished tickets integrate one at a time: rebase onto the epic tip,
run the gate before every integration, fast-forward the epic branch, then remove the
worktree and branch. A rebase conflict or gate failure resumes the ticket's own
session once; if that does not fix it the ticket needs a human. A failed ticket
holds back only the tickets that depend on it. Once every ticket has merged, the
post-ticket stages run in order, each skipped while its result still holds for the
epic head (a new epic commit invalidates the stages after it):
  verify       the project's verify commands in a worktree at the epic tip, then, when the
               config sets serve, the app started with serveEnv and a read-only verifier that
               marks each Verification item and checkpoint VERIFIED, FAILED or UNVERIFIABLE.
               A failure gets one repair agent, the gate, and the whole stage again; a pass
               fast-forwards the epic branch to any repair commits.
  epic review  reads the whole epic diff once, with the verify report as evidence.
  ship         on a SHIP verdict: a launch plan, a push of the epic branch (never forced),
               and a pull request whose body the runner writes. --merge then waits for CI
               and merges when green.

A workflow names the skills each prompt follows:
  pocock  one implement skill (IMPLEMENT_SKILL) whose own code review runs in sub-agents.
  osmani  Addy Osmani's agent skills. The implementer does not review itself; a ticket
          review — a fresh read-only session on another model family — sorts findings
          into BLOCKING, DEFERRED and REJECTED. BLOCKING findings get one fix round in the
          implementer's session, then a re-review by the same model; DEFERRED findings
          are consolidated by the epic review and posted once on the parent issue.
In both workflows the verify repair follows debugging-and-error-recovery, the launch plan
follows shipping-and-launch, and the verifier uses browser-testing-with-devtools when the
project config turns the browser on.
"""

from pathlib import Path
import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys

from .config import load_config, plan_problems, plan_stages, skill_problems, stage_refusals, stage_skills
from .display import say
from .github import discover, gh_json
from .graph import classify, render_graph
from .processes import attempt, command, dirty
from .runner import Runner
from .settings import BOLD, DIM, EFFORTS, EPIC_REVIEW_MODEL, MODELS, OSMANI_SOURCE, RED, WORKFLOWS, YELLOW


def positive(value):
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise argparse.ArgumentTypeError("expected a positive integer")
    return int(value)


def parse_args():
    p = argparse.ArgumentParser(prog="ticket-runner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Model aliases: flash (default), flash-fast, muse, luna, glm.
Full CommandCode model IDs also work. Sonnet 5.5 runs the epic review and the launch
plan by default; luna runs the verifier; verify repairs use the implementation model.
Effort defaults: max for implementation (xhigh for Muse contributor), high for reviews.
Ticket review (osmani only) alternates by ticket number over --ticket-review-model:
even tickets get the first model, odd tickets the second; a re-review keeps its model.

Examples:
  ticket-runner 207 --dry-run
  ticket-runner 207 --jobs 2 --model muse
  ticket-runner 207 --resume --max-turns 1000
  ticket-runner 207 --only 209
  ticket-runner 207 --final-only               (verify, epic review, ship)
  ticket-runner 207 --final-only --merge
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
  gate            ["npx tsc --noEmit", "npm test"]   run before every ticket integration
  prBase, mergeMethod, verify, serve, ready, readyTimeout, serveEnv, browser
The config, AGENTS.md, linkFiles and serveEnv are protected: an agent changing one stops the run,
and a ticket whose diff touches one is not integrated. AGENTS.md may carry a `## Ticket Runner`
section naming the project's personal data, governed parameters, public contracts and
performance-sensitive areas; the prompts point agents at it.

Post-ticket stages: --final-only runs only them; --no-verify, --no-final-review and
--no-ship skip one each. No epic review means no ship; --merge refuses unless verify ran
and passed and the epic review said SHIP. --resume also continues an interrupted verifier,
verify repair or launch plan session. A needs-human verify keeps .scratch/worktrees/verify
(branch cc/<parent>/verify): fix it there, commit, then --final-only.

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
Env: COMMANDCODE_BIN, WORKFLOW, IMPLEMENT_SKILL (pocock), AGENT_SKILLS_DIR (default
     ~/.agents/skills), IMPL_MODEL, IMPL_EFFORT, IMPL_MAX_TURNS, IMPL_JOBS,
     VERIFY_MODEL, VERIFY_EFFORT, VERIFY_MAX_TURNS (the verifier),
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
    p.add_argument("--verify-model", default=os.getenv("VERIFY_MODEL", "luna"),
                   help="model of the verify stage's read-only verifier (default luna)")
    p.add_argument("--verify-effort", default=os.getenv("VERIFY_EFFORT", "high"))
    p.add_argument("--verify-max-turns", type=positive, default=os.getenv("VERIFY_MAX_TURNS", "500"))
    p.add_argument("--no-verify", action="store_true", help="skip the verify stage")
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
    group.add_argument("--no-final-review", action="store_true", help="skip the epic review (and so the ship stage)")
    group.add_argument("--final-review", action="store_true", help="run the epic review; already the default")
    p.add_argument("--no-ship", action="store_true", help="skip the ship stage (launch plan, push, pull request)")
    p.add_argument("--merge", action="store_true", help="after the ship stage, wait for CI on the pull request "
                   "and merge it when green (needs the verify stage and a SHIP verdict)")
    a = p.parse_args()
    if a.final_only and a.only:
        p.error("--final-only cannot be combined with --only")
    if a.final_only and a.no_verify and a.no_final_review:
        p.error("--final-only with --no-verify and --no-final-review leaves nothing to run")
    if a.merge and (a.only or a.no_verify or a.no_final_review or a.no_ship):
        p.error("--merge needs the verify stage, the epic review and the ship stage: drop --only, --no-verify, "
                "--no-final-review and --no-ship")
    a.model = MODELS.get(a.model, a.model)
    a.final_model = MODELS.get(a.final_model, a.final_model)
    a.verify_model = MODELS.get(a.verify_model, a.verify_model)
    a.ticket_review_models = [MODELS.get(m, m) for m in a.ticket_review_model.replace(" ", "").split(",") if m]
    if not a.ticket_review_models:
        p.error("--ticket-review-model needs at least one model")
    if a.effort is None:
        a.effort = "xhigh" if a.model == MODELS["muse"] else "max"
    for model, effort in [(a.model, a.effort), (a.final_model, a.final_effort), (a.verify_model, a.verify_effort),
                          *((m, a.ticket_review_effort) for m in a.ticket_review_models)]:
        if model in EFFORTS and effort != "none" and effort not in EFFORTS[model]:
            p.error(f"{model} effort must be one of {sorted(EFFORTS[model])}, or none")
    return a


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
    folder = root / ".scratch" / "run-tickets-commandcode" / str(a.parent)
    if a.dry_run:
        return run_checkout(a, root, folder)
    # Acquire before reading state, discovery, or branch validation. An invocation
    # must never execute a snapshot observed before another runner completed.
    folder.mkdir(parents=True, exist_ok=True)
    ignore = root / ".scratch/.gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    with (folder.parent / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another ticket runner is active in this checkout")
        return run_checkout(a, root, folder)


def run_checkout(a, root, folder):
    cfg = load_config(root)
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
    missing = skill_problems(skills_dir, stage_skills(a, cfg))
    for item in missing:
        say(f"Missing skill file: {item}", RED, err=True)
    refusals = [f"{len(missing)} skill file(s) missing; install the skills from {OSMANI_SOURCE}, and restore "
                f"their references with:\n  git clone --depth 1 --filter=blob:none --sparse {OSMANI_SOURCE} "
                f"/tmp/agent-skills && git -C /tmp/agent-skills sparse-checkout set references && "
                f"cp -R /tmp/agent-skills/references {skills_dir.parent}/"] if missing else []
    refusals += stage_refusals(a, cfg, root)
    if osmani:
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
        say(plan_stages(a, cfg, data, [n for n in order if n not in done], command("git", "rev-parse", "HEAD")))
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
    return Runner(a, root, folder, path, data, tickets, levels, skill, skills_dir, cfg).execute()


def entrypoint():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("Interrupted; partial work preserved. Re-run with --resume.", YELLOW, err=True)
        sys.exit(130)
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        say(f"Error: {exc}", RED, BOLD, err=True)
        sys.exit(1)
