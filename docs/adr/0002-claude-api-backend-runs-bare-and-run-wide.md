# The claude-api backend runs bare and run-wide

A run on `--backend claude-api` puts every stage on Claude Code billed to a company Anthropic API key, never
to the operator's personal Claude subscription and never to CommandCode credits. To make that guarantee fail
closed, every session runs `claude --bare`: in bare mode the CLI authenticates only with `ANTHROPIC_API_KEY`
and never reads OAuth or the keychain, so a missing key means no session rather than a silent fallback to the
subscription. The backend is a property of the whole run, not of each stage, so the preflight can prove
before anything is spent that no stage still needs `command-code`.

## Considered Options

- **Normal mode, verifying `apiKeySource`** from each session's stream-json init event and killing the
  session if it is not the env key: keeps auto-memory and CLAUDE.md auto-discovery, but the guarantee then
  rests on a parser the runner has to keep in step with the CLI.
- **Normal mode, trusting precedence**: the env key usually wins in `-p` mode, but an empty or missing key
  falls back to the subscription without a word.
- **Per-stage backend** (each model alias carrying its own CLI): lets a cheap CommandCode implementer pair
  with an Opus reviewer, but one forgotten default (the launch plan was hard-wired to deepseek or glm) quietly
  puts part of a run on the wrong account, and "only the company key" stops being checkable.

## Consequences

- Bare mode skips CLAUDE.md auto-discovery, so the runner appends the worktree's CLAUDE.md itself with
  `--append-system-prompt-file`. Do not drop that flag as redundant. CLAUDE.md and `.claude/` join the
  protected paths on this backend, because the agent now reads them.
- Bare mode also skips auto-memory, plugins, settings hooks, user MCP config and commit attribution. Personal
  memory deliberately does not steer company-billed runs; the verifier's browser MCP is passed with
  `--mcp-config`.
- The key is read from the macOS Keychain (or `TICKET_RUNNER_ANTHROPIC_API_KEY`) and set only in each
  child's environment, so the operator's interactive Claude Code stays on the subscription. An agent can
  still read it from its own environment.
- With one model family there is no cross-family check. The epic review falls back to one Opus reviewer that
  re-checks its own findings (the option ADR-0001 rejected for cost; on this backend cost is tracked per
  session instead), and the osmani ticket review is by a different model, not a different family.
