"""Parallel GitHub ticket implementation, then epic review, verify and ship stages, via CommandCode or
Claude Code.

Every unblocked ticket runs in its own git worktree (.scratch/worktrees/<n>) on
branch cc/<parent>/<n>, cut from the epic tip when it starts; up to --jobs run
at once. Finished tickets integrate one at a time: rebase onto the epic tip,
run the gate before every integration, fast-forward the epic branch, then remove the
worktree and branch. A rebase conflict or gate failure resumes the ticket's own
session once; if that does not fix it the ticket needs a human. A failed ticket
holds back only the tickets that depend on it. Once every ticket has merged, the
epic stages run in order, each skipped while its result still holds for the
epic head (a new epic commit invalidates the stages after it):
  epic review  one or two reviewers (default deepseek and glm) each read the whole epic
               diff on their own and number their findings (D1, G1…) BLOCKING or DEFERRED.
               Two reviewers then cross-examine: each answers every one of the other's
               findings CONFIRMED or DISPUTED with evidence; a lone reviewer re-checks its
               own. Confirmed BLOCKING findings become fix tickets: the first reviewer drafts
               them with the workflow's skill, the runner files them as sub-issues labelled
               epic-review-fix, and they run like any ticket. One follow-up review of only
               the fix diff marks each finding FIXED or NOT FIXED; anything still blocking
               needs a human. On SHIP every sub-ticket closes (the epic stays open), and the
               deferred and disputed findings are posted on the epic.
  verify       the project's verify commands in a worktree at the epic tip, then, when the
               config sets serve, the app started with serveEnv and a read-only verifier that
               marks each Verification item and checkpoint VERIFIED, FAILED or UNVERIFIABLE.
               A failure gets one repair agent, the gate, and the whole stage again; a repair
               review by the last epic reviewer reads only the repair, then a pass
               fast-forwards the epic branch to the repair commits.
  ship         on a SHIP verdict: a launch plan, a push of the epic branch (never forced),
               and a pull request whose body the runner writes. --merge then waits for CI
               and merges when green.

A workflow names the skills each prompt follows:
  pocock  one implement skill (IMPLEMENT_SKILL) whose own code review runs in sub-agents;
          the epic review's ticket writer follows to-tickets.
  osmani  Addy Osmani's agent skills. The implementer does not review itself; a ticket
          review — a fresh read-only session on another model family — sorts findings
          into BLOCKING, DEFERRED and REJECTED. BLOCKING findings get one fix round in the
          implementer's session, then a re-review by the same model; DEFERRED findings
          are carried into the epic review. The ticket writer follows
          planning-and-task-breakdown.
In both workflows the epic review follows code-review-and-quality, the verify repair follows
debugging-and-error-recovery, the launch plan follows shipping-and-launch, and the verifier
uses browser-testing-with-devtools when the project config turns the browser on.

A backend is the agent CLI and account of every session in the run, independent of the workflow:
  commandcode  the command-code CLI and its models (default).
  claude-api   Claude Code in bare mode on the company Anthropic API key, never a personal
               subscription (docs/adr/0002-*). Every stage must name a Claude model; CLAUDE.md
               and .claude/ join the protected paths; each session's cost is recorded.
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

from .backends import company_key
from .config import load_config, plan_problems, plan_stages, skill_problems, stage_refusals, stage_skills
from .display import dollars, paint, say
from .github import discover, gh_json
from .graph import classify, render_graph
from .processes import attempt, command, dirty
from .runner import Runner
from .settings import (BACKENDS, BOLD, BUDGETS, CLAUDE_DEFAULTS, CLAUDE_EFFORTS, CLAUDE_MODELS, COMMANDCODE_DEFAULTS, DIM, EFFORTS,
                       KEYCHAIN_SERVICE, LAUNCH_PLAN_MODELS, MODELS, OSMANI_SOURCE, RED, WORKFLOWS, YELLOW)


def positive(value):
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise argparse.ArgumentTypeError("expected a positive integer")
    return int(value)


def parse_args():
    p = argparse.ArgumentParser(prog="ticket-runner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Model aliases (commandcode): flash or deepseek (default), flash-fast, muse, luna, glm, claude.
Full CommandCode model IDs also work. DeepSeek and GLM run the epic review by default; the
launch plan runs on deepseek (or glm, never Claude); luna runs the verifier; verify
repairs use the implementation model.
Effort defaults: max for implementation (xhigh for Muse contributor), high for reviews.

Model aliases (claude-api): sonnet, opus, haiku; full claude-* IDs also work, and nothing else.
Defaults: sonnet @ medium implements (and runs fix, repair and verifier sessions), one opus
@ high runs the epic review and re-checks its own findings, opus @ medium the ticket review,
haiku @ low the launch plan. Efforts: low, medium, high, xhigh, max. The company key comes
from TICKET_RUNNER_ANTHROPIC_API_KEY, else the Keychain item krim-anthropic-api-key; store it
with: security add-generic-password -s krim-anthropic-api-key -a "$USER" -w
Without one the run (and --dry-run) refuses. An account failure (credit, key, spend limit)
stops every ticket; fix it, then --resume. A session over its *_MAX_BUDGET_USD ends budget-cap,
and --resume continues it with a fresh budget. The ticket graph shows the epic's cost so far;
--post-costs puts it on the tickets' done comments and the epic issue, never the pull request.
Ticket review (osmani only) alternates by ticket number over --ticket-review-model:
even tickets get the first model, odd tickets the second; a re-review keeps its model.

Examples:
  ticket-runner 207 --dry-run
  ticket-runner 207 --jobs 2 --model muse
  ticket-runner 207 --resume --max-turns 1000
  ticket-runner 207 --only 209
  ticket-runner 207 --epic-only               (epic review, verify, ship)
  ticket-runner 207 --epic-review-model glm   (one reviewer, which re-checks its own findings)
  ticket-runner 207 --epic-only --merge
  ticket-runner-osmani 207 --dry-run       (same runner, --workflow osmani)
  ticket-runner-osmani 207 --ticket-review-model glm
  ticket-runner-claude 207 --dry-run       (same runner, --backend claude-api)
  ticket-runner-claude 207 --workflow osmani --post-costs

Osmani plans: every open sub-ticket needs `## Acceptance criteria` and `## Verification`,
must not be XL or likely touch more than 8 files, and must list every dependency under
`## Blocked by` (or a native link). The plan check refuses otherwise, on --dry-run too;
--skip-plan-check overrides. The workflow and the backend are saved with the epic's state;
switching either is refused while any ticket is part-way through.

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
The config, AGENTS.md, linkFiles and serveEnv (on claude-api also CLAUDE.md and the tracked
files under .claude/) are protected: an agent changing one stops the run, and a ticket whose
diff touches one is not integrated. AGENTS.md may carry a `## Ticket Runner`
section naming the project's personal data, governed parameters, public contracts and
performance-sensitive areas; the prompts point agents at it.

Epic stages: --epic-only runs only them (and any fix tickets); --no-verify,
--no-epic-review and --no-ship skip one each. No epic review means no ship; --merge refuses
unless verify ran and passed and the epic review said SHIP for the head (through the repair
review after a verify repair). --resume also continues an interrupted epic reviewer,
verifier, verify repair, repair review or launch plan session. A needs-human verify keeps .scratch/worktrees/verify
(branch cc/<parent>/verify): fix it there, commit, then --epic-only.

Merged (done) and closed tickets are always skipped. --resume continues the saved
agent session of an unfinished ticket in its kept worktree, and retries
integration of a ticket that finished but did not merge. Fix a needs-human ticket
in its worktree (commit, finish any rebase), then --resume. For awaiting-human,
answer on the issue, then --resume --only <n>. No changes are discarded.
Sub-tickets are the parent's native GitHub sub-issues plus any issue naming the parent
in a "## Parent" section or a "Parent…: #N" line. Blockers are the union of native
"blocked by" links, a "## Blocked by" section, and labelled "Blocked by:" lines;
prose that just says "blocked by" counts for nothing.
State/logs: .scratch/run-tickets-commandcode/<parent>/ on either backend (state.json holds each
            attempt's cost; costs.md the table --post-costs posts)
Worktrees:  .scratch/worktrees/<n> on branch cc/<parent>/<n>
Env: BACKEND, COMMANDCODE_BIN, CLAUDE_BIN, TICKET_RUNNER_ANTHROPIC_API_KEY,
     TICKET_RUNNER_KEYCHAIN_SERVICE, WORKFLOW, IMPLEMENT_SKILL (pocock), AGENT_SKILLS_DIR (default
     ~/.agents/skills), IMPL_MODEL, IMPL_EFFORT, IMPL_MAX_TURNS, IMPL_JOBS,
     VERIFY_MODEL, VERIFY_EFFORT, VERIFY_MAX_TURNS (the verifier),
     TICKET_REVIEW_MODEL, TICKET_REVIEW_EFFORT, TICKET_REVIEW_MAX_TURNS,
     EPIC_REVIEW_MODEL, EPIC_REVIEW_EFFORT, EPIC_REVIEW_MAX_TURNS,
     LAUNCH_PLAN_MODEL, LAUNCH_PLAN_EFFORT, LAUNCH_PLAN_MAX_TURNS,
     IMPL_, TICKET_REVIEW_, EPIC_REVIEW_, VERIFY_ and LAUNCH_PLAN_MAX_BUDGET_USD (claude-api
     only: a dollar cap on each session of that stage),
     GATE_CMDS and INSTALL_CMD (override the config's gate and install).
""")
    p.add_argument("parent", type=positive)
    p.add_argument("--workflow", choices=WORKFLOWS, default=os.getenv("WORKFLOW"),
                   help="skills the prompts follow (default: the epic's saved workflow, else pocock)")
    p.add_argument("--backend", default=os.getenv("BACKEND") or "commandcode",
                   help="agent CLI and account of every session in the run: commandcode (default), or claude-api "
                   "(Claude Code on the company Anthropic API key)")
    p.add_argument("--model", default=os.getenv("IMPL_MODEL"),
                   help="implementation model (default flash; sonnet on claude-api)")
    p.add_argument("--effort", default=os.getenv("IMPL_EFFORT"),
                   help="implementation effort; use 'none' to omit the flag")
    p.add_argument("--max-turns", type=positive, default=os.getenv("IMPL_MAX_TURNS", "500"))
    p.add_argument("--jobs", type=positive, default=os.getenv("IMPL_JOBS", "3"),
                   help="tickets implemented at once (default 3)")
    p.add_argument("--ticket-review-model", default=os.getenv("TICKET_REVIEW_MODEL"),
                   help="comma-separated models ticket reviews alternate over (osmani; default luna,glm; opus on "
                   "claude-api)")
    p.add_argument("--ticket-review-effort", default=os.getenv("TICKET_REVIEW_EFFORT"))
    p.add_argument("--ticket-review-max-turns", type=positive, default=os.getenv("TICKET_REVIEW_MAX_TURNS", "500"))
    p.add_argument("--skip-plan-check", action="store_true", help="run an osmani epic despite plan check refusals")
    p.add_argument("--verify-model", default=os.getenv("VERIFY_MODEL"),
                   help="model of the verify stage's read-only verifier (default luna; sonnet on claude-api)")
    p.add_argument("--verify-effort", default=os.getenv("VERIFY_EFFORT"))
    p.add_argument("--verify-max-turns", type=positive, default=os.getenv("VERIFY_MAX_TURNS", "500"))
    p.add_argument("--no-verify", action="store_true", help="skip the verify stage")
    p.add_argument("--epic-review-model", default=os.getenv("EPIC_REVIEW_MODEL"),
                   help="one or two comma-separated, different epic reviewers (default deepseek,glm; opus on "
                   "claude-api); two cross-examine each other's findings, one re-checks its own")
    p.add_argument("--epic-review-effort", default=os.getenv("EPIC_REVIEW_EFFORT"))
    p.add_argument("--epic-review-max-turns", type=positive, default=os.getenv("EPIC_REVIEW_MAX_TURNS", "500"))
    p.add_argument("--launch-plan-model", default=os.getenv("LAUNCH_PLAN_MODEL"),
                   help="model that writes the ship stage's launch plan (deepseek or glm, default deepseek; "
                   "haiku on claude-api)")
    p.add_argument("--launch-plan-effort", default=os.getenv("LAUNCH_PLAN_EFFORT"))
    p.add_argument("--launch-plan-max-turns", type=positive, default=os.getenv("LAUNCH_PLAN_MAX_TURNS", "500"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--only", type=positive)
    p.add_argument("--epic-only", action="store_true", help="skip the tickets and run only the epic stages")
    p.add_argument("--epic-base", help="commit the epic started from (default: saved state, else HEAD; "
                   "--epic-only without saved state defaults to the merge-base with origin's default branch)")
    p.add_argument("--no-epic-review", action="store_true", help="skip the epic review (and so the ship stage)")
    p.add_argument("--no-ship", action="store_true", help="skip the ship stage (launch plan, push, pull request)")
    p.add_argument("--merge", action="store_true", help="after the ship stage, wait for CI on the pull request "
                   "and merge it when green (needs the verify stage and a SHIP verdict)")
    p.add_argument("--post-costs", action="store_true", help="add each ticket's cost to its done comment and "
                   "post the cost table on the epic issue (never the pull request)")
    a = p.parse_args()
    if a.epic_only and a.only:
        p.error("--epic-only cannot be combined with --only")
    if a.epic_only and a.no_verify and a.no_epic_review:
        p.error("--epic-only with --no-verify and --no-epic-review leaves nothing to run")
    if a.merge and (a.only or a.no_verify or a.no_epic_review or a.no_ship):
        p.error("--merge needs the verify stage, the epic review and the ship stage: drop --only, --no-verify, "
                "--no-epic-review and --no-ship")
    if a.backend not in BACKENDS:  # argparse leaves an environment default unchecked
        p.error(f"--backend must be one of {', '.join(BACKENDS)}")
    # Defaults belong to the backend: a flag or env var always wins over them.
    claude = a.backend == "claude-api"
    aliases = CLAUDE_MODELS if claude else MODELS
    for prefix, defaults in (CLAUDE_DEFAULTS if claude else COMMANDCODE_DEFAULTS).items():
        for field, value in zip(("model", "effort"), defaults):
            if getattr(a, prefix + field) is None:
                setattr(a, prefix + field, value)
    a.model = aliases.get(a.model, a.model)
    a.epic_review_models = [aliases.get(m, m) for m in a.epic_review_model.replace(" ", "").split(",") if m]
    if not 1 <= len(a.epic_review_models) <= 2 or len(set(a.epic_review_models)) < len(a.epic_review_models):
        p.error("--epic-review-model needs one or two different models")
    a.verify_model = aliases.get(a.verify_model, a.verify_model)
    if not claude and a.launch_plan_model not in LAUNCH_PLAN_MODELS:
        p.error(f"--launch-plan-model must be one of {', '.join(LAUNCH_PLAN_MODELS)}")
    a.launch_plan_model = aliases.get(a.launch_plan_model, a.launch_plan_model)
    a.ticket_review_models = [aliases.get(m, m) for m in a.ticket_review_model.replace(" ", "").split(",") if m]
    if not a.ticket_review_models:
        p.error("--ticket-review-model needs at least one model")
    if a.effort is None:
        a.effort = "xhigh" if a.model == MODELS["muse"] else "max"
    a.budgets = {}
    for stage, env in BUDGETS.items() if claude else ():
        raw = os.getenv(env)
        if raw:
            try:
                value = float(raw)
            except ValueError:
                value = 0
            if not 0 < value < float("inf"):
                p.error(f"{env} must be a positive amount of US dollars")
            a.budgets[stage] = value
    for model, effort in [(a.model, a.effort), (a.verify_model, a.verify_effort), (a.launch_plan_model, a.launch_plan_effort),
                          *((m, a.epic_review_effort) for m in a.epic_review_models),
                          *((m, a.ticket_review_effort) for m in a.ticket_review_models)]:
        allowed = EFFORTS.get(model) or (CLAUDE_EFFORTS if claude and model.startswith("claude-") else None)
        if allowed and effort != "none" and effort not in allowed:
            p.error(f"{model} effort must be one of {sorted(allowed)}, or none")
    return a


def stage_models(a):
    """Every (stage, model, effort, turns) this run could start a session on."""
    return [("implement", a.model, a.effort, a.max_turns),
            *(("ticket review", m, a.ticket_review_effort, a.ticket_review_max_turns) for m in a.ticket_review_models),
            *(("epic review", m, a.epic_review_effort, a.epic_review_max_turns) for m in a.epic_review_models),
            ("verify", a.verify_model, a.verify_effort, a.verify_max_turns),
            ("launch plan", a.launch_plan_model, a.launch_plan_effort, a.launch_plan_max_turns)]


def backend_refusals(a, key):
    """claude-api runs only on Claude models, the claude binary and the company key: proven before any spend."""
    refusals = [f"{stage} model {model} is not a Claude model, and claude-api runs every stage on Claude Code"
                for stage, model, _, _ in stage_models(a) if not re.fullmatch(r"claude-[a-z0-9][a-z0-9.-]*", model)]
    if not shutil.which(os.getenv("CLAUDE_BIN", "claude")):
        refusals.append("Claude Code binary not found; install it or set CLAUDE_BIN")
    if not key:
        service = os.getenv("TICKET_RUNNER_KEYCHAIN_SERVICE") or KEYCHAIN_SERVICE
        refusals.append(f"no company Anthropic API key: store it with `security add-generic-password -s {service} "
                        "-a \"$USER\" -w` (or set TICKET_RUNNER_ANTHROPIC_API_KEY)")
    return refusals


def plan_models(a, source):
    """The backend and each stage's model for --dry-run; never the key itself, only where it came from."""
    head = f"Backend: {a.backend}" + (f", company key from {source or 'nowhere'}" if a.backend == "claude-api" else "")
    lines, seen = [paint(head, BOLD)], set()
    for stage, model, effort, turns in stage_models(a):
        if stage == "ticket review" and a.workflow != "osmani":
            continue
        budget = a.budgets.get(stage)
        lines.append(f"  {stage if stage not in seen else '':<13} {model} @ {effort}, {turns} turns"
                     + (f", budget {dollars(budget)}" if budget else ""))
        seen.add(stage)
    return "\n".join(lines)


def epic_base(a):
    if a.epic_base:
        return command("git", "rev-parse", "--verify", f"{a.epic_base}^{{commit}}")
    head = command("git", "rev-parse", "HEAD")
    if not a.epic_only:
        return head
    # A fresh --epic-only has no recorded start; review everything the branch adds to the default branch.
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
    busy = [n for n in order if tickets[n]["state"] != "CLOSED"
            and data.get(str(n), {}).get("status") not in (None, "done")]
    # A part-way ticket's saved session was started under the other workflow's prompts, or by the other
    # backend's CLI, which cannot resume it. State from before the backend was saved is commandcode's.
    switches = [("workflow", saved, a.workflow),
                ("backend", data.get("_meta", {}).get("backend") or ("commandcode" if data.get("_meta") else None),
                 a.backend)]
    for kind, was, now in switches:
        if was and was != now and busy:
            raise RuntimeError(f"#{a.parent} runs the {was} {kind} and {' '.join(f'#{n}' for n in busy)} "
                               f"{'is' if len(busy) == 1 else 'are'} part-way through; finish them with "
                               f"--{kind} {was}, or reset them, before switching to {now}")
    claude = a.backend == "claude-api"
    # The company key is read once, here, and handed only to the runner's Claude Code children.
    key, source = company_key() if claude else (None, None)
    osmani = a.workflow == "osmani"
    review = "off" if a.no_epic_review else (f"{' + '.join(a.epic_review_models)} @ {a.epic_review_effort}, "
                                             f"{a.epic_review_max_turns} turns")
    ticket_review = (f"; ticket review: {'/'.join(a.ticket_review_models)} @ {a.ticket_review_effort}, "
                     f"{a.ticket_review_max_turns} turns") if osmani else ""
    backend = f"Backend: claude-api (company key from {source or 'nowhere'}). " if claude else ""
    say(f"{backend}Workflow: {a.workflow}. Implement: {a.model}/{a.effort}, {a.max_turns} turns, {a.jobs} at once"
        f"{ticket_review}; epic review: {review}", DIM)
    if osmani and a.model in a.ticket_review_models:
        say(f"Warning: ticket review shares {a.model} with the implementer, and with it the same blind spots",
            YELLOW, err=True)
    if not a.no_epic_review and a.model in a.epic_review_models:
        say(f"Warning: the epic review shares {a.model} with the implementer"
            + (", so the other reviewer's cross-examination matters most" if len(a.epic_review_models) > 1 else
               ", and with it the same blind spots"), YELLOW, err=True)
    skills_dir = Path(os.getenv("AGENT_SKILLS_DIR", str(Path.home() / ".agents/skills")))
    missing = skill_problems(skills_dir, stage_skills(a, cfg))
    for item in missing:
        say(f"Missing skill file: {item}", RED, err=True)
    refusals = [f"{len(missing)} skill file(s) missing; install the skills from {OSMANI_SOURCE}, and restore "
                f"their references with:\n  git clone --depth 1 --filter=blob:none --sparse {OSMANI_SOURCE} "
                f"/tmp/agent-skills && git -C /tmp/agent-skills sparse-checkout set references && "
                f"cp -R /tmp/agent-skills/references {skills_dir.parent}/"] if missing else []
    refusals += stage_refusals(a, cfg, root)
    if claude:
        refusals += backend_refusals(a, key)
    if osmani:
        if not a.epic_only:
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
        say(plan_models(a, source))
        say("Dry run: no agents, file writes or GitHub edits.", DIM)
        for item in refusals:
            say(f"A real run would refuse: {item}", RED, err=True)
        return 1 if refusals else 0
    if refusals:
        raise RuntimeError("; ".join(refusals))
    if not claude and not shutil.which(os.getenv("COMMANDCODE_BIN", "command-code")):
        raise RuntimeError("CommandCode binary not found; set COMMANDCODE_BIN")
    branch = command("git", "branch", "--show-current")
    if not branch:
        raise RuntimeError("Check out a branch before running")
    if "_meta" not in data or a.epic_base:
        data["_meta"] = {**data.get("_meta", {}), "branch": data.get("_meta", {}).get("branch", branch),
                         "epicBase": epic_base(a)}
    meta = data["_meta"]
    meta["workflow"], meta["backend"] = a.workflow, a.backend
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
    # Fix tickets from the epic review run through the implement skill too, even under --epic-only.
    if not osmani and not (a.epic_only and a.no_epic_review) and not skill.is_file():
        raise RuntimeError(f"Implementation skill not found: {skill}")
    return Runner(a, root, folder, path, data, tickets, levels, skill, skills_dir, cfg, key=key).execute()


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
