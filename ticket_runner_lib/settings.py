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
}
EPIC_REVIEW_MODEL = "claude-sonnet-5-5"
EFFORTS = {
    MODELS["flash"]: {"off", "low", "high", "max"},
    MODELS["flash-fast"]: {"off", "low", "high", "max"},
    MODELS["muse"]: {"low", "medium", "high", "xhigh"},
    MODELS["luna"]: {"low", "medium", "high", "xhigh", "max"},
    MODELS["glm"]: {"low", "high", "max"},
    EPIC_REVIEW_MODEL: {"low", "medium", "high", "xhigh", "max"},
}
WORKFLOWS = ("pocock", "osmani")
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
BROWSER_ADD = (f"command-code mcp add --scope user --transport stdio {BROWSER_SERVER} -- "
               "npx -y chrome-devtools-mcp@latest --isolated")
OSMANI_SOURCE = "https://github.com/addyosmani/agent-skills.git"
RETRYABLE = {"turn-cap", "provider-error", "interrupted", "in-progress", "error"}
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
TICKET_COLORS = ("36", "35", "33", "32", "34", "96", "95", "93", "92", "94")
FILE_TOOLS = {"read_file", "edit_file", "write_file"}
WRITE_TOOLS = {"edit_file", "write_file"}
PRINT = threading.Lock()


class Fatal(RuntimeError):
    """Run-wide problem: stop every ticket and exit."""


class Stop(Exception):
    """The run is being interrupted; unwind the ticket without touching its work."""
