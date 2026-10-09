"""Dependency scheduling helpers and terminal graph rendering."""

from .display import fit, paint
from .settings import BLUE, BOLD, CYAN, DIM, GREEN, RED, YELLOW


KINDS = {"done": ("✓", GREEN), "running": ("●", CYAN), "ready": ("○", BLUE), "waiting": ("·", DIM),
         "failed": ("✗", RED), "held": ("⊘", YELLOW), "deferred": ("⊘", YELLOW), "skipped": ("–", DIM)}


def waves(blockers):
    """Group tickets into dependency levels; blockers outside the map are ignored."""
    levels, pending = [], set(blockers)
    while pending:
        level = sorted(n for n in pending if not set(blockers[n]) & pending)
        if not level:
            raise RuntimeError(f"Dependency cycle among tickets: {sorted(pending)}")
        levels.append(level)
        pending.difference_update(level)
    return levels


def ready(pending, blockers, done, busy):
    """Pending tickets, in order, whose in-epic blockers have all merged."""
    return [n for n in pending if n not in busy and all(b in done for b in blockers[n] if b in blockers)]


def held_back(stuck, blockers):
    """Tickets that depend, directly or transitively, on a stuck ticket."""
    held = set()
    while True:
        new = {n for n, bs in blockers.items() if n not in held | set(stuck) and set(bs) & (held | set(stuck))}
        if not new:
            return held
        held |= new


def classify(blockers, done, running=(), failed=(), deferred=(), skipped=()):
    """Kind of every ticket: done, running, failed, deferred, skipped, held, ready or waiting."""
    held = held_back(set(failed) | set(deferred), blockers)
    return {n: "done" if n in done else "running" if n in running else "failed" if n in failed else
               "deferred" if n in deferred else "skipped" if n in skipped else "held" if n in held else
               "ready" if all(b in done for b in bs if b in blockers) else "waiting"
            for n, bs in blockers.items()}


def render_graph(levels, tickets, kinds, labels=None):
    """Wave-by-wave picture of the epic; a wave that is all done collapses to one line."""
    labels = labels or {}
    counts = [f"{sum(k == kind for k in kinds.values())} {kind}" for kind in KINDS
              if any(k == kind for k in kinds.values())]
    lines = [paint("Ticket graph: ", BOLD) + " · ".join(counts)]
    for i, level in enumerate(levels, 1):
        wave = f"  wave {i:<2} "
        if all(kinds[n] == "done" for n in level):
            lines.append(paint(f"{wave}✓ all {len(level)} done: " + " ".join(f"#{n}" for n in level), DIM))
            continue
        for j, n in enumerate(level):
            kind = kinds[n]
            glyph, color = KINDS[kind]
            label = labels.get(n) or kind
            open_blockers = [b for b in tickets[n]["blockers"] if b in kinds and kinds[b] != "done"]
            if kind in {"waiting", "held"} and open_blockers:
                label += (" on " if kind == "waiting" else " by ") + " ".join(f"#{b}" for b in open_blockers)
            plain = f"{wave if j == 0 else ' ' * len(wave)}{glyph} #{n} {label:<22} "
            title = fit(tickets[n]["title"], len(plain))
            lines.append(paint(plain[:len(wave)], DIM) + paint(plain[len(wave):], color) +
                         paint(title, DIM) if kind == "done" else
                         paint(plain[:len(wave)], DIM) + paint(plain[len(wave):], color, BOLD) + title)
    return "\n".join(lines)
