"""Terminal output and live agent event narration."""

import json
import os
import shutil
import sys
import time

from .settings import BOLD, COLOR, CYAN, DIM, FILE_TOOLS, MAGENTA, PRINT, RED, WRITE_TOOLS, YELLOW


def paint(text, *codes):
    return f"\033[{';'.join(codes)}m{text}\033[0m" if COLOR and codes else text


def say(text, *codes, err=False):
    with PRINT:
        print(paint(text, *codes), file=sys.stderr if err else sys.stdout, flush=True)


def fit(text, used=0):
    width = shutil.get_terminal_size((120, 20)).columns - used
    text = " ".join(text.split())
    return text if len(text) <= width else text[:max(width - 1, 10)] + "…"


def describe(tool, args):
    if not isinstance(args, dict):
        return str(args)
    if tool in FILE_TOOLS:
        return os.path.relpath(args["file_path"]) if args.get("file_path") else ""
    if tool == "shell_command":
        return args.get("description") or args.get("command", "")
    if tool in {"grep", "glob"}:
        return f"{args.get('pattern', '')}  {os.path.relpath(args.get('path') or '.')}"
    return os.path.relpath(args["path"]) if args.get("path") else json.dumps(args)[:200]


def message(value):
    if isinstance(value, dict):
        return str(value.get("message") or json.dumps(value))
    return "" if value is None else str(value)


def narrate(event, started, tag, color):
    # Live progress feed; the full NDJSON stream still goes to attempt-N.jsonl.
    kind = event.get("type")
    elapsed = int(time.monotonic() - started)
    head = paint(f"[{elapsed // 60:02d}:{elapsed % 60:02d}]", DIM) + " " + paint(tag, BOLD, color)
    used = 9 + len(tag)
    if kind == "turn_start":
        say(f"{head} " + paint(f"── turn {event.get('turnNumber')} ──", DIM, color))
    elif kind == "message_end":
        content = event.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": message(content)}]
        text = " ".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        if text.strip():
            say(f"{head} {paint('AGENT', BOLD, color)} {fit(text, used + 6)}")
    elif kind == "tool_queued":
        tool = str(event.get("toolName", "?"))
        tint = MAGENTA if tool in WRITE_TOOLS else CYAN
        say(f"{head}   {paint('▸ ' + tool, tint)} {fit(describe(tool, event.get('input') or {}), used + 6 + len(tool))}")
    elif kind == "tool_errored":
        say(f"{head}   {paint('✗ ' + str(event.get('toolName', '?')), RED, BOLD)} {fit(message(event.get('error')), used + 24)}")
    elif kind in {"api_retry", "stream_restart"}:
        detail = message(event.get("error")) or f"attempt {event.get('attempt')}/{event.get('maxAttempts')}"
        say(f"{head} {paint('↻ ' + kind.replace('_', ' '), YELLOW)} {fit(detail, used + 16)}")
    elif kind == "run_error":
        say(f"{head} {paint('run error', RED, BOLD)} {message(event.get('error'))}")
