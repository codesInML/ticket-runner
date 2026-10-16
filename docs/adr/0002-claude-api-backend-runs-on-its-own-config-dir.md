# The claude-api backend runs on its own config directory, run-wide

A run on `--backend claude-api` puts every stage on Claude Code billed to a company Anthropic API key, never
to the operator's personal Claude subscription and never to CommandCode credits. To make that guarantee fail
closed, every session runs with `CLAUDE_CONFIG_DIR` pointed at a directory of the runner's own
(`.scratch/claude-config`) that holds no login: without the key a session fails "Not logged in" and spends
nothing, rather than falling back to the subscription stored in the operator's config. The runner also stops
the run if a session's init event reports any `apiKeySource` other than `ANTHROPIC_API_KEY`. The backend is a
property of the whole run, not of each stage, so the preflight can prove before anything is spent that no
stage still needs `command-code`.

## Considered Options

- **`claude --bare`** (this ADR's first version): authenticates strictly with `ANTHROPIC_API_KEY`, but
  Claude Code 2.1.300 limits a bare session to the Bash, Edit and Read tools whatever `--tools` says. No
  sub-agents, Write or Skill: the pocock implement skill's own review runs in sub-agents, so bare mode broke
  it. Found by probing the real CLI after the first implementation.
- **Normal mode on the operator's config, trusting precedence**: the env key usually wins in `-p` mode, but
  an empty or missing key falls back to the subscription without a word, and `apiKeySource` alone cannot
  prevent it: by the time the runner reads it the login is already in reach.
- **Per-stage backend** (each model alias carrying its own CLI): lets a cheap CommandCode implementer pair
  with an Opus reviewer, but one forgotten default (the launch plan was hard-wired to deepseek or glm) quietly
  puts part of a run on the wrong account, and "only the company key" stops being checkable.

## Consequences

- The config directory is seeded with a link to the operator's skills and settings that turn off commit
  attribution and auto-memory: personal memory and global CLAUDE.md never steer company-billed runs, and
  agents cannot leave notes that steer later sessions. Project CLAUDE.md is still discovered as usual, so
  CLAUDE.md and `.claude/` join the protected paths on this backend.
- `CLAUDE_CODE_OAUTH_TOKEN` works from any config directory, so it is stripped from every session's
  environment along with the other Anthropic credentials and provider switches.
- The key is read from the macOS Keychain (or `TICKET_RUNNER_ANTHROPIC_API_KEY`) and set only in each
  child's environment, so the operator's interactive Claude Code stays on the subscription. An agent can
  still read it from its own environment.
- With one model family there is no cross-family check. The epic review falls back to one Opus reviewer that
  re-checks its own findings (the option ADR-0001 rejected for cost; on this backend cost is tracked per
  session instead), and the osmani ticket review is by a different model, not a different family.
