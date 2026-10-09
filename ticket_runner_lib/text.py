"""Issue section and agent verdict parsing."""

import re


def verdict(text, pattern=r"STATUS=(COMPLETE|NEEDS_HUMAN|BLOCKED)"):
    lines = text.strip().splitlines()
    found = re.fullmatch(pattern + r"\s*", lines[-1]) if lines else None
    return found[1] if found else "MISSING"


def section(body, name):
    """Text under a `## <name>` heading up to the next heading, or None when there is no such heading."""
    found = re.search(rf"^#+\s*{name}\b[^\n]*$(.*?)(?=^#+\s|^VERDICT=|\Z)", body, re.M | re.S | re.I)
    return found[1].strip() if found else None
