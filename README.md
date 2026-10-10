# Ticket runner

Run `bin/ticket-runner --help` for workflow, configuration, and resume options.
`bin/ticket-runner-osmani` selects the Osmani workflow. Both commands use the
`ticket_runner.py` entry point and require the adjacent `ticket_runner_lib`
package.

## Code layout

- `cli.py`: argument parsing, startup validation, and exit handling.
- `runner.py`: shared state, locks, file protection, and stage orchestration.
- `scheduling.py`: concurrent scheduling, dependency waits, and interruption.
- `tickets.py`: individual ticket implementation and lifecycle.
- `worktrees.py`: provisioning, integration, gates, and conflict repair.
- `agents.py`: CommandCode sessions and logged shell processes.
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
temporary git repositories and fake GitHub and CommandCode executables. Runtime
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
files. Changed inputs invalidate verification and its downstream review and launch
plan, even at unchanged HEAD. Env values are never stored in fingerprints or logs.
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

