# Ticket runner

Run `bin/ticket-runner --help` for workflow, configuration, and resume options.
`bin/ticket-runner-osmani` selects the Osmani workflow, and `bin/ticket-runner-claude` the
claude-api backend; the two compose (`ticket-runner-claude 207 --workflow osmani`). Every
command uses the `ticket_runner.py` entry point and requires the adjacent `ticket_runner_lib`
package.

## Backends

A backend is the agent CLI and account of every session in a run (`--backend`, env `BACKEND`):

- `commandcode` (default): the `command-code` CLI (`COMMANDCODE_BIN`) and its models.
- `claude-api`: Claude Code (`CLAUDE_BIN`, default `claude`) in bare mode, billed to the company
  Anthropic API key and never to a personal subscription. See `docs/adr/0002-*` for why.

The key comes from `TICKET_RUNNER_ANTHROPIC_API_KEY`, else the macOS Keychain item
`krim-anthropic-api-key` (`TICKET_RUNNER_KEYCHAIN_SERVICE` names another). Store it once with
`security add-generic-password -s krim-anthropic-api-key -a "$USER" -w`. Without a key the run,
and its `--dry-run`, refuse. The key is read once and set only in each Claude Code session's
environment, with every other Anthropic credential and provider switch removed; the runner's own
gate, verify and install commands never see it, and it is never logged or saved.

On claude-api the stages default to `sonnet` (implement, fix and repair sessions, verifier, at
`medium`), `opus` (one epic reviewer that re-checks its own findings, at `high`; the osmani ticket
review, at `medium`) and `haiku` (launch plan, at `low`). Every stage model must be a Claude model
(`sonnet`, `opus`, `haiku` or a full `claude-*` ID), checked before anything is spent. CLAUDE.md
and `.claude/` join the protected paths, and the runner appends the session's CLAUDE.md itself,
since bare mode does not read it. The backend is saved with the epic; switching it is refused while
a ticket is part-way through.

Each Claude Code session's cost, tokens, turns and time are recorded per attempt in `state.json`
(a resumed session's earlier spend is subtracted, as Claude Code reports session totals). The
ticket graph shows the epic's running total and the run ends with a table by stage and ticket;
`--post-costs` also puts each ticket's cost in its done comment and the table on the epic issue.
`IMPL_MAX_BUDGET_USD`, `TICKET_REVIEW_MAX_BUDGET_USD`, `EPIC_REVIEW_MAX_BUDGET_USD`,
`VERIFY_MAX_BUDGET_USD` and `LAUNCH_PLAN_MAX_BUDGET_USD` cap each session of their stage; a session
that reaches its cap ends `budget-cap`, which `--resume` continues with a fresh budget.

An account failure (credit balance too low, a revoked key, a spend limit) stops every ticket at
once; top up or fix the key, then `--resume`. Rate limits and overloads end only the session, as
`provider-error`.

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
temporary git repositories and fake GitHub, CommandCode, Claude Code and keychain executables. Runtime
verification tests also start a temporary server on localhost.

## Integration and recovery guarantees

The runner holds one checkout lock before reading saved state or discovering issues.
Every ticket runs the configured gate before integration, including tickets whose
base has not moved. Gates and verification commands must leave tracked code and
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
review reads only the fix diff; anything still blocking needs a human. A verify repair after
SHIP gets a repair review, and `--merge` accepts a head only through that chain. See
`CONTEXT.md` for the vocabulary and `docs/adr/0001-*` for why.

