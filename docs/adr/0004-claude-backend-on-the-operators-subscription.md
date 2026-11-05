# The claude backend runs on the operator's subscription and normal config

A run on `--backend claude` uses Claude Code on the operator's own claude.ai login for every session, and the
`claude-api` backend and its company key are gone. This reverses ADR-0002's "never a personal subscription":
the operator now chooses their own subscription deliberately, as ADR-0003 did for Codex. Sessions run on the
normal `~/.claude`, because Claude Code 2.1.318 reports `loggedIn: false` under any other `CLAUDE_CONFIG_DIR`:
the login is a macOS Keychain item (`Claude Code-credentials`) tied to the default config directory, with no
credentials file to link. So the Codex backend's private home with a symlinked login cannot work here.

## Considered Options

- **A private config directory and a `claude setup-token` token**: keeps sessions apart from the operator's
  config, as ADR-0003 does. It adds a long-lived token to store and renew, and agents could read it from their
  environment. Rejected: the operator asked for the normal CLI with nothing to set up.
- **Keeping the API key backend beside the subscription**: two Claude accounts to explain, test and choose
  between, with no remaining use for the company key.

## Consequences

- Sessions read the operator's global `CLAUDE.md` and skills and appear in their history. Auto-memory stays off,
  with `CLAUDE_CODE_DISABLE_AUTO_MEMORY` and inline settings, so agents cannot leave notes for later sessions.
  `--strict-mcp-config` keeps the operator's MCP servers out, and inline settings turn off attribution.
- The boundary now fails the other way: nothing may reach an API bill. Inherited Anthropic credentials, token
  handoffs, provider switches and `CLAUDE_CONFIG_DIR` are stripped, preflight needs `authMethod: "claude.ai"`
  from `claude auth status` run in that same environment, and an init whose `apiKeySource` is not `none` stops
  the run. `--setting-sources user` still keeps project settings from replacing the account or endpoint.
- The limit is the plan's usage, not a budget, so `*_MAX_BUDGET_USD` and `budget-cap` are gone and the defaults
  save Opus for the epic review and stuck tickets. Shared blind spots from a Sonnet ticket review of Sonnet's
  work are an accepted cost, as in ADR-0003. No dollar figure is shown for subscription usage.
- A spent plan reaches the stream as a `rate_limit` error, so it is told apart from a passing rate limit by a
  `rate_limit_event` with status `rejected`, or the CLI's "You've hit your…" wording, and stops the run. The field
  names and wording come from the 2.1.318 binary's own strings. A real limit was not induced, and `none` for a
  subscription session's `apiKeySource` is carried over from ADR-0002's probe rather than re-checked.
- An epic saved on `claude-api` moves only when `--backend` names its new one, because its account changes.
  Its sessions cannot be resumed from another config directory, so part-way tickets start fresh sessions.
