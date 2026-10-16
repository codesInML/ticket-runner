"""Agent sessions on the run's backend, their spend, and logged shell processes."""

import json
import os
import subprocess
import threading
import time

from .display import dollars, narrate, say, span
from .processes import git, kill
from .settings import BLUE, BOLD, BUDGET_OF, BUDGETS, DIM, Fatal, GREEN, Stop, YELLOW


class AgentProcesses:
    def spawn_child(self, argv, **kwargs):
        with self.spawn:
            if self.stopping.is_set():
                raise Stop()
            child = subprocess.Popen(argv, start_new_session=True, **kwargs)
            self.children.add(child)
        return child

    def reap(self, child):
        with self.spawn:
            self.children.discard(child)

    def run_agent(self, model, effort, turns, prompt, folder, record, session=None, cwd=None,
                  tag="review", color=BLUE, browser=False, stage="implement"):
        """One agent session on the run's backend; its final text. `stage` names its cost and its budget."""
        cwd = cwd or self.root
        folder.mkdir(parents=True, exist_ok=True)
        if session and record.get("backend", "commandcode") != self.backend.name:
            session = None  # a session saved by the other CLI cannot be resumed by this one
        number = record.get("attempts", 0) + 1
        # Kept once a record has one, so a commandcode attempt after a claude-api one can resume its own session.
        self.update(record, attempts=number, model=model, effort=effort, maxTurns=turns,
                    **({"backend": self.backend.name} if self.claude or "backend" in record else {}))
        budget = self.budgets.get(BUDGET_OF.get(stage, stage)) if self.claude else None
        argv = self.backend.argv(model, effort, turns, prompt, session, cwd, budget, browser)
        stream = self.backend.stream()
        # Separate stdout NDJSON from stderr, and preserve every attempt's logs.
        events = folder / f"attempt-{number}.jsonl"
        started = time.monotonic()
        say(f"{tag} streaming to {os.path.relpath(events)}", DIM)
        with events.open("w") as output, (folder / f"attempt-{number}.log").open("w") as errors:
            child = self.spawn_child(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=errors, text=True,
                                     env=self.backend.env())
            try:
                for line in child.stdout:
                    output.write(line)
                    output.flush()
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    try:
                        sid, shown = stream.feed(frame)
                    except Exception as exc:  # the feed is cosmetic; never let it kill the agent
                        say(f"{tag} unreadable progress event ({type(exc).__name__}); see {events.name}", DIM)
                        continue
                    # Session IDs may arrive before the final result, allowing interrupt recovery.
                    if isinstance(sid, str) and sid and sid != record.get("sessionId"):
                        self.update(record, sessionId=sid)
                    if getattr(stream, "refusal", None):
                        raise Fatal(stream.refusal)  # killed below, before the session spends anything
                    for event in shown:
                        try:
                            narrate(event, started, tag, color)
                        except Exception as exc:  # the feed is cosmetic; never let it kill the agent
                            say(f"{tag} unreadable progress event ({type(exc).__name__}); see {events.name}", DIM)
                rc = child.wait()
            except BaseException:
                kill(child)
                self.update(record, status="interrupted", head=git(cwd, "rev-parse", "HEAD"))
                raise
            finally:
                self.reap(child)
        head = git(cwd, "rev-parse", "HEAD")
        if self.stopping.is_set():
            self.update(record, status="interrupted", head=head)
            raise Stop()
        status, text, spent, fatal = stream.outcome(rc)
        (folder / "final.txt").write_text(text)
        self.update(record, exitCode=rc, head=head, status=status)
        label = f"{tag.split()[0]} {stage}" if tag.startswith("#") else tag
        if spent:
            self.spend(record, stage, model, effort, spent, session, status, label)
        if status == "budget-cap":
            say(f"{label} spent its {dollars(budget or 0)} budget ({BUDGETS[BUDGET_OF.get(stage, stage)]}): the "
                "budget ran out, not the work; --resume continues the session with a fresh budget", YELLOW)
        if fatal:
            raise Fatal(fatal)
        return text

    def spend(self, record, stage, model, effort, spent, session, status, label):
        """Keep one attempt's spend. A resumed Claude Code session reports the whole session's totals, restored
        from its transcript, so what the session's earlier attempts already counted is subtracted."""
        costs = list(record.get("costs", []))
        totals = {"costUsd": spent["costUsd"], **spent.get("usage", {})}
        base = next((c["session"] for c in reversed(costs) if session and c.get("sessionId") == session
                     and isinstance(c.get("session"), dict)), None)
        # A total below the earlier one means nothing was restored (the CLI exited without saving it).
        own = {k: max(0, v - base.get(k, 0)) for k, v in totals.items()} \
            if base and totals["costUsd"] >= base.get("costUsd", 0) else totals
        entry = {"stage": stage, "model": model, "effort": effort, "costUsd": round(own["costUsd"], 6)}
        if spent.get("usage"):
            entry["usage"] = {k: own[k] for k in spent["usage"]}
        entry.update({k: spent[k] for k in ("numTurns", "durationMs") if k in spent})
        entry.update(sessionId=record.get("sessionId"), session=totals)
        costs.append(entry)
        self.update(record, costs=costs, costUsd=round(sum(c["costUsd"] for c in costs), 6))
        self.spent = True
        parts = [f"{'✓' if status == 'reported' else '✗'} {label}", dollars(entry["costUsd"])]
        parts += [f"{entry['numTurns']} turns"] if entry.get("numTurns") else []
        parts += [span(entry["durationMs"] / 1000)] if entry.get("durationMs") else []
        say(" · ".join(parts), GREEN if status == "reported" else YELLOW)

    def costs(self):
        """Every recorded attempt's spend as (owner, entry): '#n' for a ticket's sessions, 'epic' otherwise."""
        found = []

        def walk(node, owner):
            if isinstance(node, dict):
                found.extend((owner, c) for c in node.get("costs", ()) if isinstance(c, dict))
                for key, value in node.items():
                    if key != "costs":
                        walk(value, owner)
            elif isinstance(node, list):
                for value in node:
                    walk(value, owner)
        with self.state:
            for key, value in self.data.items():
                walk(value, f"#{key}" if key.isdigit() else "epic")
        return found

    def spent_total(self):
        found = self.costs()
        return sum(c["costUsd"] for _, c in found) if found else None

    def cost_tables(self):
        """The epic's spend by stage and by ticket, as markdown rows; None before any session reported one."""
        found = self.costs()
        if not found:
            return None
        stages, owners = {}, {}
        for owner, c in found:
            count, total = stages.get(c["stage"], (0, 0))
            stages[c["stage"]] = (count + 1, total + c["costUsd"])
            owners[owner] = owners.get(owner, 0) + c["costUsd"]
        order = sorted(owners, key=lambda o: (o == "epic", int(o[1:]) if o != "epic" else 0))
        return (["| Stage | Sessions | Cost |", "| --- | ---: | ---: |",
                 *(f"| {s} | {n} | {dollars(t)} |" for s, (n, t) in stages.items())],
                ["| Ticket | Cost |", "| --- | ---: |",
                 *(f"| {'epic stages' if o == 'epic' else o} | {dollars(owners[o])} |" for o in order)],
                sum(owners.values()))

    def report_costs(self):
        """The end-of-run table; with --post-costs, the same table on the epic issue, never the pull request."""
        tables = self.cost_tables()
        if not tables:
            return
        by_stage, by_ticket, total = tables
        say(f"\nCost of epic #{self.a.parent} so far (estimated): {dollars(total)}", BOLD)
        for row in by_stage[2:] + [""] + by_ticket[2:]:
            cells = [c.strip() for c in row.strip("|").split("|")] if row else []
            say("  " + "  ".join(f"{c:<16}" if i == 0 else f"{c:>9}" for i, c in enumerate(cells)), DIM)
        if not (getattr(self.a, "post_costs", False) and self.spent):
            return
        body = self.folder / "costs.md"
        body.write_text(f"Cost of epic #{self.a.parent} so far: {dollars(total)} (the agent CLI's estimate, not a "
                        "bill).\n\n" + "\n".join(by_stage) + "\n\n" + "\n".join(by_ticket) + "\n")
        result = subprocess.run(["gh", "issue", "comment", str(self.a.parent), "--body-file", str(body)],
                                capture_output=True, text=True)
        if result.returncode:
            say(f"Warning: could not post the cost table on #{self.a.parent} ({result.stderr.strip()}); it is in "
                f"{body}", YELLOW, err=True)

    def parallel(self, jobs):
        """Run agent sessions at once; their results in order. A failure in any is raised after all finish."""
        if len(jobs) == 1:
            return [jobs[0]()]
        results, errors = [None] * len(jobs), [None] * len(jobs)

        def run(i, job):
            try:
                results[i] = job()
            except BaseException as exc:  # handed to the main thread below
                errors[i] = exc

        threads = [threading.Thread(target=run, args=(i, job), daemon=True) for i, job in enumerate(jobs)]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                while thread.is_alive():
                    thread.join(0.5)  # a bounded join keeps Ctrl-C deliverable
        except BaseException:
            self.stop()
            for thread in threads:
                thread.join(30)
            raise
        for exc in errors:
            if exc is not None:
                raise exc
        return results

    def log(self, tdir, kind):
        return tdir / f"{kind}-{len(list(tdir.glob(f'{kind}-*.log'))) + 1}.log"

    def shell(self, cmd, cwd, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as out:
            child = self.spawn_child(cmd, shell=True, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
            try:
                rc = child.wait()
            finally:
                kill(child)
                self.reap(child)
        if self.stopping.is_set():
            raise Stop()
        return rc
