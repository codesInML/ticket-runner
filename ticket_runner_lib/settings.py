"""Models, workflow defaults, terminal settings, and runner exceptions."""

import os
import sys
import threading


MODELS = {
    "flash": "deepseek/deepseek-v4.1-flash",
    "flash-fast": "deepseek/deepseek-v4.1-flash-fast",
    "muse": "meta/muse-spark-1.3-contributor",
    "minimax": "minimax/minimax-m3.1-flash",
    "glm": "z-ai/glm-5.3-flash",
    "claude": "claude-sonnet-5-5",
}
MODELS["deepseek"] = MODELS["flash"]
CLAUDE_MODEL = MODELS["claude"]
# On commandcode the launch plan never runs on Claude: it is the cheapest stage to hand a cheaper model.
LAUNCH_PLAN_MODELS = ("deepseek", "glm")
EFFORTS = {
    MODELS["flash"]: {"off", "low", "high", "max"},
    MODELS["flash-fast"]: {"off", "low", "high", "max"},
    MODELS["muse"]: {"low", "medium", "high", "xhigh"},
    MODELS["minimax"]: {"low", "medium", "high", "xhigh", "max"},
    MODELS["glm"]: {"low", "high", "max"},
    CLAUDE_MODEL: {"low", "medium", "high", "xhigh", "max"},
}
WORKFLOWS = ("pocock", "osmani")
# The agent CLI and account of a whole run: CommandCode, or Claude Code on the operator's subscription
# (docs/adr/0004-*), or Codex on their ChatGPT subscription.
BACKENDS = ("commandcode", "claude", "codex")
# The company API key backend docs/adr/0002-* described: a saved epic can still name it.
REMOVED_BACKEND = "claude-api"
CODEX_MODELS = {"luna": "gpt-6-luna", "sol": "gpt-6.1-sol"}
CODEX_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
EFFORTS.update({CODEX_MODELS["luna"]: CODEX_EFFORTS,
                CODEX_MODELS["sol"]: CODEX_EFFORTS | {"ultra"}})
CLAUDE_MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
EFFORTS.update({m: CLAUDE_EFFORTS for m in CLAUDE_MODELS.values()})
# Removed from every Claude Code session's environment, so nothing but the operator's login can authenticate it.
# CLAUDE_CONFIG_DIR is here too: the login belongs to the operator's normal config (docs/adr/0004-*).
FOREIGN_AUTH = ("TICKET_RUNNER_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CONFIG_DIR", "CCR_OAUTH_TOKEN_FILE", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR")
# Passed inline to every session: no co-author attribution, no auto-memory carried from one session into the next.
CLAUDE_SETTINGS = {"attribution": {"commit": "", "pr": ""}, "autoMemoryEnabled": False}
# Claude Code reads these, so on claude an agent changing one changes what later sessions are told.
CLAUDE_PROTECTED = ("CLAUDE.md", ".claude/", "CLAUDE.local.md",
                    ":(glob)**/CLAUDE.md", ":(glob)**/CLAUDE.local.md")
CODEX_HOME_DIR = ".scratch/codex-home"
CODEX_PROTECTED = (":(glob)**/AGENTS.md", "AGENTS.override.md", ":(glob)**/AGENTS.override.md", ".codex/", ".agents/")
CODEX_CONFIG = '''model_provider = "openai"
forced_login_method = "chatgpt"
cli_auth_credentials_store = "file"
notify = []
[features]
memories = false
[memories]
generate_memories = false
use_memories = false
[history]
persistence = "none"
'''
# Osmani skills the runner names in its prompts. Each links ../../references/*.md, which the
# skills installer leaves out; the preflight refuses to run while any of those files is missing.
# Skills each stage's prompts name.
IMPLEMENT_SKILLS = ("incremental-implementation", "test-driven-development", "code-simplification",
                    "security-and-hardening", "api-and-interface-design", "performance-optimization",
                    "frontend-ui-engineering", "observability-and-instrumentation", "documentation-and-adrs",
                    "deprecation-and-migration", "ci-cd-and-automation", "debugging-and-error-recovery",
                    "doubt-driven-development")
TICKET_REVIEW_SKILLS = ("code-review-and-quality", "security-and-hardening", "performance-optimization",
                        "api-and-interface-design")
INTEGRATION_SKILLS = ("git-workflow-and-versioning", "debugging-and-error-recovery")
EPIC_REVIEW_SKILLS = ("code-review-and-quality", "security-and-hardening", "performance-optimization")
# The epic review's ticket writer follows the workflow's own ticket-writing skill.
TICKET_WRITING_SKILLS = {"osmani": "planning-and-task-breakdown", "pocock": "to-tickets"}
# Epic review defaults: two different model families that cross-examine each other's findings.
EPIC_REVIEWERS = "deepseek,glm"
# Each backend's (model, effort) per stage, keyed by the stage's argument prefix; used only where no flag or
# env var names one. CommandCode's implementation effort follows its model (see cli.py).
# The escalation model takes over a stuck ticket; "off" leaves it to a human (CommandCode by default, so a
# run never starts spending on Claude unless asked).
COMMANDCODE_DEFAULTS = {"": ("flash", None), "ticket_review_": ("minimax,glm", "high"), "verify_": ("minimax", "high"),
                        "epic_review_": (EPIC_REVIEWERS, "high"), "launch_plan_": ("deepseek", "high"),
                        "escalate_": ("off", "high")}
# Sonnet on every ticket, Opus saved for the epic review and stuck tickets: the limit here is the plan's usage.
CLAUDE_DEFAULTS = {"": ("sonnet", "medium"), "ticket_review_": ("sonnet", "high"), "verify_": ("sonnet", "medium"),
                   "epic_review_": ("opus", "medium"), "launch_plan_": ("haiku", "low"), "escalate_": ("opus", "medium")}
CODEX_DEFAULTS = {"": ("luna", "high"), "ticket_review_": ("luna", "high"), "verify_": ("luna", "high"),
                  "epic_review_": ("sol", "high"), "launch_plan_": ("luna", "low"), "escalate_": ("sol", "medium")}
BACKEND_DEFAULTS = {"commandcode": COMMANDCODE_DEFAULTS, "claude": CLAUDE_DEFAULTS, "codex": CODEX_DEFAULTS}
BACKEND_MODELS = {"commandcode": MODELS, "claude": CLAUDE_MODELS, "codex": CODEX_MODELS}
BACKEND_EFFORTS = {"commandcode": None, "claude": CLAUDE_EFFORTS, "codex": CODEX_EFFORTS}
# Bumped whenever the shape of a saved epic review changes; an older record is reviewed again.
REVIEW_PROTOCOL = 3
FIX_LABEL = "epic-review-fix"
VERIFY_SKILLS = ("debugging-and-error-recovery",)
SHIP_SKILLS = ("shipping-and-launch",)
BROWSER_SKILL = "browser-testing-with-devtools"
# The verifier's chrome-devtools MCP tools, which a headless run withholds unless named.
BROWSER_SERVER = "chrome-devtools"
BROWSER_TOOLS = tuple(f"mcp__{BROWSER_SERVER}__{t}" for t in (
    "list_pages", "new_page", "select_page", "close_page", "navigate_page", "wait_for", "take_snapshot",
    "take_screenshot", "click", "fill", "fill_form", "hover", "press_key", "handle_dialog", "evaluate_script",
    "list_console_messages", "get_console_message", "list_network_requests", "get_network_request",
    "resize_page", "emulate", "performance_start_trace", "performance_stop_trace", "performance_analyze_insight"))
# Bare mode loads no user MCP config, so the verifier gets the server inline.
BROWSER_MCP = {"mcpServers": {BROWSER_SERVER: {"command": "npx",
                                               "args": ["-y", "chrome-devtools-mcp@latest", "--isolated"]}}}
BROWSER_ADD = (f"command-code mcp add --scope user --transport stdio {BROWSER_SERVER} -- "
               "npx -y chrome-devtools-mcp@latest --isolated")
OSMANI_SOURCE = "https://github.com/addyosmani/agent-skills.git"
RETRYABLE = {"turn-cap", "provider-error", "interrupted", "in-progress", "error"}
# Optional project config, relative to the project root. Every key is optional; these are the defaults.
CONFIG = ".agents/ticket-runner.json"
DEFAULTS = {"install": "npm ci", "lockfiles": ["package-lock.json"], "dependencyDirs": ["node_modules"],
            "linkFiles": [".env"], "gate": ["npx tsc --noEmit", "npm test"], "verify": [], "serve": None,
            "ready": None, "readyTimeout": 120, "serveEnv": None, "browser": False, "prBase": None,
            "mergeMethod": "merge", "appVerification": None}
MERGE_METHODS = ("merge", "squash", "rebase")
# Copy-on-write clones keep each worktree's dependency directories isolated at near-zero cost.
CLONES = (["cp", "-cR"], ["cp", "-R"]) if sys.platform == "darwin" else (["cp", "-R", "--reflink=auto"],)
# NO_COLOR / FORCE_COLOR follow https://no-color.org conventions.
COLOR = bool(os.getenv("FORCE_COLOR")) or (
    not os.getenv("NO_COLOR") and os.getenv("TERM") != "dumb" and sys.stdout.isatty())
BOLD, DIM, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = "1", "2", "31", "32", "33", "34", "35", "36"
# 256 colours where the terminal says it has them; the 16 basic ones otherwise.
COLOR256 = "256color" in os.getenv("TERM", "") or os.getenv("COLORTERM", "") in ("truecolor", "24bit")


def shade(n, basic):
    return f"38;5;{n}" if COLOR256 else basic


# Tool lines keep one colour per kind of tool in every session, and no ticket or stage uses any of them:
# reads steel blue, writes orange, shell lime, MCP (browser) tools teal. Errors stay red, retries yellow.
TOOL_COLORS = {"read": shade(68, CYAN), "write": shade(208, MAGENTA), "shell": shade(112, GREEN),
               "mcp": shade(37, "96")}
# What the agent says is soft grey: the tag before it carries the ticket's colour.
AGENT_TEXT = shade(250, "37")
# Tickets count up this palette and the epic stages count down it, clear of the tool colours above.
TICKET_COLORS = tuple(f"38;5;{n}" for n in (213, 121, 141, 180, 159, 168, 147, 177, 219, 135, 174, 183)) \
    if COLOR256 else ("34", "94", "93", "92", "95", "91", "97")
# CommandCode's tool names, then Claude Code's.
FILE_TOOLS = {"read_file", "edit_file", "write_file"}
WRITE_TOOLS = {"edit_file", "write_file", "Edit", "Write", "MultiEdit", "NotebookEdit", "file_change"}
SHELL_TOOLS = {"shell_command", "Bash", "command_execution"}
PRINT = threading.Lock()


class Fatal(RuntimeError):
    """Run-wide problem: stop every ticket and exit."""


class Stop(Exception):
    """The run is being interrupted; unwind the ticket without touching its work."""
