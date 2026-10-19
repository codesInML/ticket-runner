"""Models, workflow defaults, terminal settings, and runner exceptions."""

import os
import sys
import threading


MODELS = {
    "flash": "deepseek/deepseek-v4.1-flash",
    "flash-fast": "deepseek/deepseek-v4.1-flash-fast",
    "muse": "meta/muse-spark-1.3-contributor",
    "luna": "gpt-6-luna",
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
    MODELS["luna"]: {"low", "medium", "high", "xhigh", "max"},
    MODELS["glm"]: {"low", "high", "max"},
    CLAUDE_MODEL: {"low", "medium", "high", "xhigh", "max"},
}
WORKFLOWS = ("pocock", "osmani")
# The agent CLI and account of a whole run (docs/adr/0002-*): CommandCode, or Claude Code billed to a
# company Anthropic API key.
BACKENDS = ("commandcode", "claude-api")
CLAUDE_MODELS = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}
CLAUDE_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
EFFORTS.update({m: CLAUDE_EFFORTS for m in CLAUDE_MODELS.values()})
# Per-stage spend caps (claude-api only), passed to that stage's sessions as --max-budget-usd.
BUDGETS = {"implement": "IMPL_MAX_BUDGET_USD", "ticket review": "TICKET_REVIEW_MAX_BUDGET_USD",
           "epic review": "EPIC_REVIEW_MAX_BUDGET_USD", "verify": "VERIFY_MAX_BUDGET_USD",
           "launch plan": "LAUNCH_PLAN_MAX_BUDGET_USD"}
# Sessions that spend another stage's budget: they run on that stage's model.
BUDGET_OF = {"fix": "implement", "integration repair": "implement", "verify repair": "implement",
             "repair review": "epic review"}
KEYCHAIN_SERVICE = "krim-anthropic-api-key"
# Removed from every Claude Code session's environment, so nothing but the company key can authenticate it.
# CLAUDE_CODE_OAUTH_TOKEN is a subscription token that works from any config directory.
FOREIGN_AUTH = ("TICKET_RUNNER_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CONFIG_DIR", "CCR_OAUTH_TOKEN_FILE", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR")
# The runner's Claude Code config directory (relative to the project root) and the settings seeded into it.
CLAUDE_CONFIG_DIR = ".scratch/claude-config"
CLAUDE_SETTINGS = {"attribution": {"commit": "", "pr": ""}, "autoMemoryEnabled": False}
# Claude Code reads these, so on claude-api an agent changing one changes what later sessions are told.
CLAUDE_PROTECTED = ("CLAUDE.md", ".claude/", "CLAUDE.local.md",
                    ":(glob)**/CLAUDE.md", ":(glob)**/CLAUDE.local.md")
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
COMMANDCODE_DEFAULTS = {"": ("flash", None), "ticket_review_": ("luna,glm", "high"), "verify_": ("luna", "high"),
                        "epic_review_": (EPIC_REVIEWERS, "high"), "launch_plan_": ("deepseek", "high")}
CLAUDE_DEFAULTS = {"": ("sonnet", "medium"), "ticket_review_": ("opus", "medium"), "verify_": ("sonnet", "medium"),
                   "epic_review_": ("opus", "high"), "launch_plan_": ("haiku", "low")}
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
# budget-cap: the session spent its stage's budget, which says nothing about the ticket.
RETRYABLE = {"turn-cap", "budget-cap", "provider-error", "interrupted", "in-progress", "error"}
# Optional project config, relative to the project root. Every key is optional; these are the defaults.
CONFIG = ".agents/ticket-runner.json"
DEFAULTS = {"install": "npm ci", "lockfiles": ["package-lock.json"], "dependencyDirs": ["node_modules"],
            "linkFiles": [".env"], "gate": ["npx tsc --noEmit", "npm test"], "verify": [], "serve": None,
            "ready": None, "readyTimeout": 120, "serveEnv": None, "browser": False, "prBase": None,
            "mergeMethod": "merge"}
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
WRITE_TOOLS = {"edit_file", "write_file", "Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"shell_command", "Bash"}
PRINT = threading.Lock()


class Fatal(RuntimeError):
    """Run-wide problem: stop every ticket and exit."""


class Stop(Exception):
    """The run is being interrupted; unwind the ticket without touching its work."""
