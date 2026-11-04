# Ticket runner

Run `bin/ticket-runner --help` for workflow, configuration, and resume options.

## Escalation and self-verification

A ticket is stuck when its implementer ends NEEDS_HUMAN, BLOCKED or without a STATUS line, when its fix
round does, or when its ticket review still blocks after the fix round. Before it goes to a person, the
runner hands it once per kind (implement, review) to the escalation model (`--escalate-model`, env
`ESCALATE_MODEL`, default `sol` @ medium on codex and `opus` @ medium on claude, `off` on commandcode). That
session runs in a fresh thread in the ticket's worktree with the earlier reports. It is told to fix the
problem, run the checks itself, and stop only for a decision that belongs to a person. A review escalation
is followed by one more review round with the same reviewer; a ticket still blocking after that needs a
human. Every re-review confirms the previous findings and draws new BLOCKING findings only from the
fix diff (since the commit the previous review read); older problems it notices are DEFERRED unless they
leave a criterion UNMET. After a person fixes a needs-human ticket in its worktree, `--resume` re-reviews
their commits against the last review the same way. An escalation that ends NEEDS_HUMAN waits on the issue (`awaiting-human`) as before, and an
interrupted one resumes its own thread with `--resume`.

Every implement, fix and escalation prompt tells the session to run the ticket's Verification checks
itself, manual checks included, on the platform each one names. It also tells the session to report them
under `## VERIFICATION`. Set `appVerification` in `.agents/ticket-runner.json` to a doc saying how to run
the app (devices, scripts, test data) and every working prompt, and the verifier, points at it. The doc is
protected like the config, and ticket worktrees see it only if it is committed or listed in `linkFiles`.
`bin/ticket-runner` is the only command. `--backend` and `--workflow` (or `BACKEND` and `WORKFLOW`)
choose the agent CLI and skill set, are required on an epic's first run, and are saved with the epic,
so later runs need neither. The command uses the `ticket_runner.py` entry point and requires the
adjacent `ticket_runner_lib` package.

## Backends

A backend is the agent CLI and account of every session in a run (`--backend`, env `BACKEND`):

- `commandcode`: the `command-code` CLI (`COMMANDCODE_BIN`) and its models. Ticket reviews alternate
  over `minimax` (`minimax/minimax-m3.1-flash`) and `glm`, and `minimax` runs the verifier. Luna is
  refused on this backend, by alias or full ID, from any flag or variable; a ticket's saved Luna
  review moves to its current reviewer on its next round.
- `claude`: Claude Code (`CLAUDE_BIN`, default `claude`) on the operator's normal login and
  subscription. See `docs/adr/0004-*` for why.
- `codex`: the Codex CLI (`CODEX_BIN`, default `codex`) billed to the operator's ChatGPT subscription.

### Codex on a ChatGPT subscription

Run `codex login` first. Preflight, including `--dry-run`, refuses unless the operator's
`$CODEX_HOME/auth.json` (default `~/.codex/auth.json`) reports `auth_mode: "chatgpt"`, the binary exists,
and every stage names a `gpt-*` model. No API key is used. Before spawning children the runner removes
inherited `OPENAI_*` and `CODEX_*` variables; only sessions receive the runner-owned `CODEX_HOME` at
`<project root>/.scratch/codex-home`. Gates, installs and GitHub commands receive none of them.

The runner writes its own config, with memories and prompt history off, no notifications or configured
MCP servers, and no inherited model or effort. Its `auth.json` is a symlink to the operator's file so
rotating refresh tokens stay shared. A regular file at that destination is refused. Skills load from
`~/.agents/skills`; an explicit `AGENT_SKILLS_DIR` is linked into the runner home. Session transcripts
remain there for `--resume`. Browser verification receives Chrome DevTools configuration inline.

| Stage | Default model | Effort |
| --- | --- | --- |
| Implement, fix, integration repair, verify repair | `gpt-6-luna` | high |
| Ticket review (osmani) | `gpt-6-luna` | high |
| Epic review, repair review | `gpt-6.1-sol` | high |
| Verify | `gpt-6-luna` | high |
| Launch plan | `gpt-6-luna` | low |
| Escalate (a stuck ticket) | `gpt-6.1-sol` | medium |

Aliases `luna` and `sol`, or full `gpt-*` IDs, work for every stage; flags and environment variables
override defaults. Luna accepts low/medium/high/xhigh/max; Sol also accepts ultra. The epic review has
one reviewer that re-checks its own findings. The default ticket review intentionally shares the
implementer's model in a fresh session; the yellow warning keeps those shared blind spots visible.

The runner counts completed tool items and terminates the process group at the stage's turn cap.
The resulting `turn-cap` is retryable: `--resume` continues the same thread with a fresh count.
A ChatGPT usage limit stops every ticket and reports the reset time when available; wait for access,
then `--resume`. Signed-out or expired/revoked logins stop the run with `codex login`, then `--resume`.
Request rate limits, overloads and server errors leave only that session retryable as `provider-error`.

Costs are tokens and time only, with no dollar amount in state, the graph or the tables posted with
`--post-costs`. Budget variables ending in `_MAX_BUDGET_USD` are ignored. Input, cached input and output
tokens are saved; displayed totals add input and output (cached input is already included in input).
Resumed thread totals subtract usage already recorded. `AGENTS.md` and `AGENTS.override.md` at every
depth, `.codex/` and `.agents/` join the protected paths: main-worktree changes stop the run, and ticket
diffs touching them never integrate. Saved-backend rules also apply to codex. See `docs/adr/0003-*`.

### Claude Code on a subscription

Run `claude auth login` first. Preflight, including `--dry-run`, runs `claude auth status` as a session
would and refuses unless it reports `loggedIn` with `authMethod: "claude.ai"`, the binary exists, and
every stage names a Claude model (`sonnet`, `opus`, `haiku` or a full `claude-*` ID). No API key is used.
Each session's environment has every inherited Anthropic credential, token handoff, provider switch and
`CLAUDE_CONFIG_DIR` removed, and a session whose init reports any `apiKeySource` but `none`, or another
API provider, stops the run before its first API call.

Sessions run on the operator's own `~/.claude`, because the login lives in the macOS Keychain, tied to
that config directory. They read the global `CLAUDE.md` and skills and appear in the operator's history.
Only user settings load (`--setting-sources user`), so project settings cannot replace the account or
endpoint. `--strict-mcp-config` leaves out configured MCP servers, the verifier's browser server is passed
inline, and inline settings turn off commit attribution and auto-memory. CLAUDE.md and CLAUDE.local.md at
every project depth and `.claude/` join the protected paths, since Claude Code reads them.

| Stage | Default model | Effort |
| --- | --- | --- |
| Implement, fix, integration repair, verify repair | `sonnet` | medium |
| Ticket review (osmani) | `sonnet` | high |
| Epic review, repair review | `opus` | medium |
| Verify | `sonnet` | medium |
| Launch plan | `haiku` | low |
| Escalate (a stuck ticket) | `opus` | medium |

Costs are tokens and time, with no dollar amount in state, the graph or the tables posted with
`--post-costs`, and a resumed session's earlier usage is subtracted. A spent usage limit stops every ticket
and reports the reset time when the session gave one; wait for it, then `--resume`. A signed-out or expired
login stops the run with `claude auth login`, then `--resume`. Other rate limits and overloads end only
the session, as `provider-error`.

An epic saved on the removed `claude-api` backend continues only once `--backend` names its new one, since
its account changes. Its part-way tickets start fresh sessions in their kept worktrees, as the old config
directory's sessions cannot be resumed.

## Code layout

- `cli.py`: argument parsing, startup validation, and exit handling.
- `runner.py`: shared state, locks, file protection, and stage orchestration.
- `scheduling.py`: concurrent scheduling, dependency waits, and interruption.
- `tickets.py`: individual ticket implementation and lifecycle.
- `worktrees.py`: provisioning, integration, gates, and conflict repair.
- `agents.py`: agent sessions on the run's backend, their cost, and logged shell processes.
- `backends.py`: each backend's CLI argv and environment, event stream, and session outcome.
- `prompts.py`: stage prompts and skill guidance.
- `review.py`: ticket reviews and their fix round.
- `epic_review.py`: the epic review: reviewers, cross-examination, fix tickets, the follow-up
  review, and closing sub-tickets.
- `verification.py`: command and runtime checks, repairs, and reports.
- `shipping.py`: launch plans, pushes, pull requests, and CI-gated merges.
- `config.py`, `github.py`, and `graph.py`: configuration validation, issue
  discovery, and dependency graph operations.
- `settings.py`, `display.py`, `processes.py`, and `text.py`: defaults and shared
  terminal, process, and parsing helpers.

These modules live in `ticket_runner_lib`. `Runner` composes the workflow classes;
it owns their shared state and synchronization. The workflow classes group methods
by responsibility without introducing separate copies of runner state. Helper
modules do not import the workflow classes.

## Tests

Run `python3 -m unittest discover -s tests -q`. The integration suite uses
temporary git repositories and fake GitHub, CommandCode, Claude Code and Codex executables. Runtime
verification tests also start a temporary server on localhost.

## Integration and recovery guarantees

The runner holds one checkout lock before reading saved state or discovering issues.
Every ticket with commits runs the configured gate before integration, including tickets
whose base has not moved. A ticket that ends STATUS=COMPLETE with no commits and a clean
worktree has nothing to integrate: it is done at once, without a ticket review or gate,
and its worktree and branch are removed as after a merge. An implement session whose
final report lacks the STATUS line is resumed once and asked for only that line; a
second miss needs a human. A ticket review that ends without its VERDICT line, and an
epic reviewer or ticket writer whose report lacks its end line, get the same single nudge. Gates and verification commands must leave tracked code and
HEAD unchanged; new unignored files created by verification commands are kept for
inspection and require a human. Ignore intentional build artifacts in the project.
Runtime artifacts already present when the verifier starts may remain untracked in
the disposable verification worktree. Ignore uploads, logs, and other app artifacts
that checks can create later: newly appearing unignored files cannot be distinguished
from verifier edits, so the runner preserves them and requires inspection.

Verification repairs and human commits in a kept verification worktree require a
passing gate for the exact commit and effective command before merging. A failed
gate is retried on resume. Unexpected edits are preserved rather than discarded.
The runner itself is protected when it lives inside the target repository.

Cached verification fingerprints the epic commit, effective configuration and gate,
ticket Verification sections, parent checkpoints, and hashes of local linked/env
files. Changed runtime inputs invalidate verification and the launch plan, even at
unchanged HEAD. The epic review separately fingerprints the parent and ticket requirements
and the epic base; changing these invalidates completed and unfinished reviews. Accepted
repair reviews extend SHIP coverage independently of temporary verification state. Env values are never stored in fingerprints or logs.
The verifier reports every stable check ID from `checks.md` exactly once, with a
result and evidence; missing, duplicate, or contradictory results require inspection.

Shipping refreshes a saved pull request's state and commit from GitHub. Closed
unmerged requests require reopening; requests on a different commit require
inspection. Readiness probes obey the remaining timeout, and process groups are
terminated on completion, timeout, or interruption, including background descendants.

State writes remain immediate and atomic for recovery. A local benchmark of 200
writes measured about 1.0 ms/write for 10 records and 1.4 ms/write for 100 records;
there was no evidence that sacrificing immediate checkpoints would help.

## Epic review guarantees

The epic review runs before verify. Each reviewer's findings carry stable IDs, and every
cross-examination or self-check must answer each ID it is given exactly once; the runner,
not a model, decides which findings are confirmed, disputed or withdrawn. Only confirmed
blocking findings become fix tickets, and only the runner files them, with a marker in each
body so a retry finds an issue it already filed instead of filing it twice. One follow-up
review reads only the fix diff; anything still blocking needs a human. When that person commits
fixes on the epic branch, `--epic-only` gives their commits (since the fix tickets) a repair review by
the last reviewer instead of a fresh review of the whole epic: each open finding FIXED or NOT FIXED,
plus anything those commits broke. It repeats for each later commit until it says SHIP, and only a
branch that no longer contains the fix tickets, or changed requirements or reviewers, start over. A
verify repair after SHIP gets a repair review too, and `--merge` accepts a head only through that
chain. See
`CONTEXT.md` for the vocabulary and `docs/adr/0001-*` for why.

