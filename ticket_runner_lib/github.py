"""GitHub issue discovery and dependency links."""

import json
import re
import subprocess
import time

from .display import say
from .graph import waves
from .processes import command
from .settings import YELLOW


EDGES = "blockedBy(first: 100) { totalCount nodes { number state repository { nameWithOwner } } }"


def references(text):
    """Issue numbers in a blocker clause: link targets and parenthetical asides don't count."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*/issues/(\d+)[^)]*\)", r"\1 #\2", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\([^)]*\)", "", text)
    if re.match(r"[\s*_:-]*(none|nothing)\b", text, re.I):
        return []
    return [int(n) for n in re.findall(r"#(\d+)", text)]


def parent_of(body):
    """The parent named by a `## Parent` section or a `Parent…: #N` line; a passing mention is not one."""
    section = re.search(r"^#+\s*Parent\s*$(.*?)(?=^#+\s|\Z)", body, re.M | re.S | re.I)
    line = re.search(r"^[\s>*_-]*Parent\b[^\n#]*?#(\d+)", body, re.M | re.I)
    first = re.search(r"#(\d+)", section[1]) if section else None
    return int(first[1]) if first else int(line[1]) if line else None


def blockers_of(body):
    """Blockers from a `## Blocked by` section and every labelled `Blocked by:` line or `· Blocked by:` field.

    Prose that merely says "blocked by" ("blocked by the vendor decision, see #12") names no blocker.
    """
    found = []
    for section in re.findall(r"^#+\s*Blocked by\s*$(.*?)(?=^#+\s|\Z)", body, re.M | re.S | re.I):
        for line in section.splitlines():
            found += references(re.sub(r"^[\s*-]*(Blocked by)?", "", line, flags=re.I))
    for clause in re.findall(r"(?:^|·)[ \t>*_-]*Blocked by\s*\**\s*:\**(.*)", body, re.M | re.I):
        found += references(clause.split(" · ")[0])
    return sorted(set(found))


def ref(blocker):
    """Display form of a blocker: #N in this repo, owner/repo#N elsewhere."""
    return f"#{blocker}" if isinstance(blocker, int) else blocker


def gh_json(*args):
    for tries in range(4):
        try:
            return json.loads(command("gh", *args))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            if tries == 3:
                raise RuntimeError(f"gh {' '.join(args[:2])} failed after four attempts")
            time.sleep(tries + 1)


def native_links(parent, numbers):
    """GitHub's own sub-issue and blocked-by links: the parent's sub-issues, plus every ticket's blockers.

    /to-tickets prefers these native relationships, so a ticket may record its blockers nowhere else.
    """
    aliases = "\n".join(f"i{n}: issue(number: {n}) {{ {EDGES} }}" for n in sorted(numbers))
    query = f"""query($owner: String!, $name: String!, $parent: Int!) {{
  repository(owner: $owner, name: $name) {{
    nameWithOwner
    parent: issue(number: $parent) {{
      subIssues(first: 100) {{ totalCount nodes {{ number title body state repository {{ nameWithOwner }} {EDGES} }} }}
    }}
    {aliases}
  }}
}}"""
    repo = gh_json("api", "graphql", "-F", "owner={owner}", "-F", "name={repo}", "-F", f"parent={parent}",
                   "-f", f"query={query}")["data"]["repository"]
    children = repo["parent"]["subIssues"]
    if children["totalCount"] > len(children["nodes"]):
        raise RuntimeError(f"#{parent} has more than 100 sub-issues; split the epic before running")
    edges = {int(key[1:]): node["blockedBy"] for key, node in repo.items() if re.fullmatch(r"i\d+", key)}
    edges.update({c["number"]: c["blockedBy"] for c in children["nodes"]
                  if c["repository"]["nameWithOwner"] == repo["nameWithOwner"]})
    for n, linked in edges.items():
        if linked["totalCount"] > len(linked["nodes"]):
            raise RuntimeError(f"#{n} has more than 100 blocked-by links; trim them before running")
    return repo["nameWithOwner"], children["nodes"], edges


def discover(parent):
    """Sub-tickets and their blockers, from issue bodies and GitHub's native links together; fails on cycles."""
    issues = gh_json("issue", "list", "--state", "all", "--limit", "1000", "--json", "number,title,body,state")
    if len(issues) == 1000:
        raise RuntimeError("Issue listing reached its limit; narrow discovery before running")
    tickets = {i["number"]: i for i in issues if i["number"] != parent and parent_of(i.get("body") or "") == parent}
    home, children, edges = native_links(parent, tickets)
    for child in children:
        if child["repository"]["nameWithOwner"] != home:
            say(f"Skipping sub-issue {child['repository']['nameWithOwner']}#{child['number']}: "
                f"it lives outside {home}", YELLOW, err=True)
            continue
        tickets.setdefault(child["number"], {k: child[k] for k in ("number", "title", "body", "state")})
    for n, issue in tickets.items():
        local, foreign = set(blockers_of(issue.get("body") or "")), []
        for node in edges.get(n, {"nodes": []})["nodes"]:
            where = node["repository"]["nameWithOwner"]
            if where == home:
                local.add(node["number"])
            elif node["state"] != "CLOSED":
                foreign.append(f"{where}#{node['number']}")
        # A ticket naming its own parent as a blocker would wait on the open epic forever.
        issue["blockers"] = sorted(local - {parent, n})
        issue["foreign"] = sorted(foreign)
    if not tickets:
        raise RuntimeError(f"No sub-tickets found for #{parent}")
    levels = waves({n: t["blockers"] for n, t in tickets.items()})
    return tickets, levels
