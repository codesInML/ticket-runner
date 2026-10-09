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
- `review.py`: ticket and epic reviews and deferred findings.
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
