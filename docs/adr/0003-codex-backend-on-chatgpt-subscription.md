# The codex backend runs on the operator's ChatGPT subscription in its own home

A run on `--backend codex` uses the Codex CLI on the operator's ChatGPT subscription for every session.
The runner owns `.scratch/codex-home`, writes its config and symlinks its `auth.json` to the operator's
login. Preflight requires ChatGPT auth and `gpt-*` models; inherited authentication and endpoint
variables never reach child processes. Implementation, ticket review and verification default to
`gpt-6-luna` at high effort, the launch plan to Luna at low effort, and one epic reviewer to
`gpt-6.1-sol` at high effort. The runner enforces tool-count turn caps and records tokens and time.

## Considered Options

- **An OpenAI API key**: ADR-0002 keeps a company bill off personal accounts. Here the operator chose
  their own subscription deliberately, so that constraint has a different account boundary. API-key
  fallback would bill an account the operator did not choose.
- **Sharing `~/.codex` with `--ignore-user-config`**: config is only one input; memories, plugins,
  instructions and history can still leak both ways. A separate home keeps the runner's sessions
  separate from interactive work.
- **Copying `auth.json`**: refresh tokens rotate; separate copies can invalidate one another and sign
  the operator out. The symlink shares refreshed tokens without sharing config or history.
- **Sol for ticket review**: reserved for epic review. **`gpt-5.6-luna`**: a weaker reviewer. Luna in a
  fresh session is the deliberate ticket-review default; shared blind spots are an accepted cost,
  exposed by the yellow warning and backstopped by the Sol epic review.
- **No turn cap**, or **a per-stage wall-clock timeout**: neither bounds tool work consistently.
  Counting completed tool items provides a retryable cap with a fresh count on resume.

## Consequences

- Confirmed with installed Codex CLI **0.163.0**, using tiny Luna/low prompts in a disposable git
  repository and a separate home with a symlinked login. Success exits 0; a dummy signed-out login
  exits 1; terminating a session with SIGTERM returns -15 in Python. Resume accepts `--json`, `-m`,
  `-c`, `--strict-config` and `--dangerously-bypass-approvals-and-sandbox` after `exec resume` and
  before the thread ID and prompt. The same thread ID is emitted before results on both invocations.
- JSONL starts with `thread.started` / `thread_id`, then `turn.started`. Tools use `item.started` and
  `item.completed`; `command_execution`, `file_change`, `mcp_tool_call`, `collab_tool_call` and
  `web_search` are tool items. `agent_message.text` carries narration and the last report.
  `turn.completed.usage` contains input, cached input and output tokens. The runner counts each
  completed tool item once, including failed tools, and kills the process group at the cap.
- `-c model_reasoning_effort="low"` worked for the probes; stage overrides use that same key.
  `--strict-config` accepted the runner's config keys: `model_provider`, `forced_login_method`,
  `cli_auth_credentials_store`, `notify`, `features.memories`, `memories.generate_memories`,
  `memories.use_memories`, and `history.persistence`. Prompt history is disabled while session
  transcripts remain resumable. The deprecated `features.memory_tool` emitted a warning during
  the first probe and is deliberately omitted.
- The probe transcript loaded `~/.agents/skills` with the custom home and did not include the
  operator's global AGENTS.md. Global instructions and rules resolve against the custom home;
  memories were disabled. No skills link is needed for the default directory; an explicit
  `AGENT_SKILLS_DIR` uses the supported legacy `$CODEX_HOME/skills` location.
- `codex mcp list --json` on that home returned no servers. Inline overrides
  `-c mcp_servers.chrome-devtools.command="npx"` and
  `-c 'mcp_servers.chrome-devtools.args=["-y","chrome-devtools-mcp@latest","--isolated"]'`
  resolved the expected server; the verifier alone receives them when browser checks are enabled.
  `--strict-config` is accepted by `exec`, but not by `mcp list`.
- Versioned source confirms the JSON schema and cumulative usage totals in
  [exec_events.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/exec/src/exec_events.rs) and
  [event_processor_with_jsonl_output.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/exec/src/event_processor_with_jsonl_output.rs).
  A resumed probe reported increased thread totals, so earlier usage is subtracted per attempt.
  Cached input is part of input and is not added a second time to displayed totals.
- Usage-limit failures were not induced against the account. The exact variants and wording come from
  [protocol/src/error.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/protocol/src/error.rs):
  `UsageLimitReached` carries `resets_at` and formats a retry timestamp. Exec currently exposes only
  `message` in `error` and `turn.failed.error`, so narrow CLI-error wording is necessary as a fallback.
  Structured codes/statuses take precedence when present. Model reports and command output never
  trigger account classification. Request rate limits, overload and server errors stay session-local.
- The dummy login emitted `plan type is required for chatgpt authentication`, ending in `turn.failed`.
  Refresh failures (`refresh_token_expired`, `refresh_token_reused`, `refresh_token_invalidated`) and
  their sign-in wording are defined in
  [login/src/auth/manager.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/login/src/auth/manager.rs).
  That file also identifies `CODEX_ACCESS_TOKEN` and refresh/client endpoint overrides. The runner
  strips all inherited `CODEX_*` and `OPENAI_*` variables before any child, capturing the binary and
  operator auth path first; sessions receive only their isolated `CODEX_HOME`.
- The login remained a symlink after successful exec and resume. Refresh was not forced against the
  real account: [login/src/auth/storage.rs](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/login/src/auth/storage.rs)
  saves with `OpenOptions` and truncates/writes through the link, rather than replacing it by rename.
- No dollar figure is fabricated for subscription usage. The existing ledger stores usage, completed
  tool count and duration; tables and the graph display tokens. Budget variables are ignored.
  Nested AGENTS.md and AGENTS.override.md, `.codex/` and `.agents/` are protected from agent changes.
