"""Issue section and agent verdict parsing."""

import re


def verdict(text, pattern=r"STATUS=(COMPLETE|NEEDS_HUMAN|BLOCKED)"):
    lines = text.strip().splitlines()
    found = re.fullmatch(pattern + r"\s*", lines[-1]) if lines else None
    return found[1] if found else "MISSING"


def section(body, name):
    """Text under a `## <name>` heading up to the next heading or end line (VERDICT=…), or None without one."""
    found = re.search(rf"^#+\s*{name}\b[^\n]*$(.*?)(?=^#+\s|^[A-Z][A-Z_-]*=[A-Z]|\Z)", body, re.M | re.S | re.I)
    return found[1].strip() if found else None


def nested_sections(body, name):
    """Named Markdown sections, retaining subordinate headings and fenced code."""
    headings = []
    fenced = False
    for match in re.finditer(r"^([^\n]*)$", body, re.M):
        line = match[1]
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        if not fenced:
            heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading:
                headings.append((match.start(), match.end(), len(heading[1]), heading[2]))
    for index, (_, end, level, title) in enumerate(headings):
        if not re.match(name + r"\b", title, re.I):
            continue
        stop = next((start for start, _, depth, _ in headings[index + 1:] if depth <= level), len(body))
        yield title, body[end:stop].strip()


def rows(body, verbs):
    """`- D1 VERB rest` lines: (ID, verb, rest) for every line whose verb is one of `verbs`."""
    pattern = (r"^[\s>*_|-]*(?:\d+[.)]\s*)?\**([A-Z]\d+)\**[\s:.)\]-]+\**(" + "|".join(verbs) +
               r")\b\**(?:[\s:—–-]+(.*))?$")
    return [(m[1], m[2], (m[3] or "").strip()) for m in re.finditer(pattern, body or "", re.M)]


def empty(body):
    """A section that says there is nothing: blank, or just "None."."""
    return not body or bool(re.fullmatch(r"[\s*_-]*none\.?[\s*_]*", body, re.I))
