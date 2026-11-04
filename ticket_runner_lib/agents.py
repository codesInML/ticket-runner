"""Agent sessions on the run's backend, their spend, and logged shell processes."""

import copy
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
        cwd = cwd or self.tree
        instructions = self.codex_snapshot(cwd) if self.backend.name == "codex" else None
        folder.mkdir(parents=True, exist_ok=True)
        if session and record.get("backend", "commandcode") != self.backend.name:
            session = None  # a session saved by the other CLI cannot be resumed by this one
        number = record.get("attempts", 0) + 1
        # Kept once a record has one, so a commandcode attempt after a claude-api one can resume its own session.
        self.update(record, attempts=number, model=model, effort=effort, maxTurns=turns,
                    **({"backend": self.backend.name} if self.backend.name != "commandcode" or "backend" in record else {}))
        budget = self.budgets.get(BUDGET_OF.get(stage, stage)) if self.claude else None
        argv = self.backend.argv(model, effort, turns, prompt, session, cwd, budget, browser)
        stream = self.backend.stream(turns)
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
                    if instructions is not None and frame.get("type") == "item.completed":
                        self.guard_codex(cwd, instructions)
                    for event in shown:
                        try:
                            narrate(event, started, tag, color)
                        except Exception as exc:  # the feed is cosmetic; never let it kill the agent
                            say(f"{tag} unreadable progress event ({type(exc).__name__}); see {events.name}", DIM)
                    if getattr(stream, "stop_at_cap", False):
                        kill(child)
                        break
                rc = child.wait()
            except BaseException:
                kill(child)
                self.update(record, status="interrupted", head=git(cwd, "rev-parse", "HEAD"))
                raise
            finally:
                self.reap(child)
        if instructions is not None:
            self.guard_codex(cwd, instructions)
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
        with self.state:
            self.costs()  # migrate legacy records before this attempt is added
        costs = list(record.get("costs", []))
        totals = {**({"costUsd": spent["costUsd"]} if "costUsd" in spent else {}), **spent.get("usage", {})}
        base = next((c["session"] for c in reversed(costs) if session and c.get("sessionId") == session
                     and isinstance(c.get("session"), dict)), None)
        # A total below the earlier one means nothing was restored (the CLI exited without saving it).
        restored = base and (totals["costUsd"] >= base.get("costUsd", 0) if "costUsd" in totals else
                             all(v >= base.get(k, 0) for k, v in totals.items()))
        own = {k: max(0, v - base.get(k, 0)) for k, v in totals.items()} if restored else totals
        entry = {"stage": stage, "model": model, "effort": effort}
        if "costUsd" in own:
            entry["costUsd"] = round(own["costUsd"], 6)
        if spent.get("usage"):
            entry["usage"] = {k: own[k] for k in spent["usage"]}
        entry.update({k: spent[k] for k in ("numTurns", "durationMs") if k in spent})
        # A killed session may emit no usage event; keep the last baseline for its next resume.
        entry.update(sessionId=record.get("sessionId"), session=totals or base or {})
        costs.append(entry)
        with self.state:
            owner = "epic"
            def contains(node):
                if node is record:
                    return True
                if isinstance(node, dict):
                    return any(contains(v) for k, v in node.items() if k != "costs")
                if isinstance(node, list):
                    return any(contains(v) for v in node)
                return False
            for key, value in self.data.items():
                if key.isdigit() and contains(value):
                    owner = f"#{key}"
                    break
            self.data["_costs"].append({"owner": owner, "entry": copy.deepcopy(entry)})
            record.update(costs=costs)
            if any("costUsd" in c for c in costs):
                record["costUsd"] = round(sum(c.get("costUsd", 0) for c in costs), 6)
            self.persist()  # ledger and resumed-session baseline are one atomic checkpoint
        self.spent = True
        parts = [f"{'✓' if status == 'reported' else '✗'} {label}",
                 dollars(entry["costUsd"]) if "costUsd" in entry else self.tokens(entry)]
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
            if "_costs" not in self.data:
                for key, value in self.data.items():
                    walk(value, f"#{key}" if key.isdigit() else "epic")
                self.data["_costs"] = [{"owner": owner, "entry": copy.deepcopy(entry)} for owner, entry in found]
            return [(c["owner"], copy.deepcopy(c["entry"])) for c in self.data["_costs"]]

    def spent_total(self):
        return self.spend_total([c for _, c in self.costs()])

    @staticmethod
    def token_count(entry):
        usage = entry.get("usage", {})
        return usage.get("inputTokens", 0) + usage.get("outputTokens", 0)

    @classmethod
    def tokens(cls, entry):
        return f"{cls.token_count(entry):,} tokens"

    @classmethod
    def spend_total(cls, entries):
        if not entries:
            return None
        priced = [c["costUsd"] for c in entries if "costUsd" in c]
        unpriced = [c for c in entries if "costUsd" not in c]
        if not unpriced:
            return sum(priced)
        tokens = f"{sum(cls.token_count(c) for c in unpriced):,} tokens"
        return f"{dollars(sum(priced))} + {tokens}" if priced else tokens

    def cost_tables(self):
        """The epic's spend by stage and ticket, with tokens for sessions that report no price."""
        found = self.costs()
        if not found:
            return None
        stages, owners = {}, {}
        for owner, c in found:
            stages.setdefault(c["stage"], []).append(c)
            owners.setdefault(owner, []).append(c)
        priced = sum("costUsd" in c for _, c in found)
        column = "Tokens" if not priced else "Cost" if priced == len(found) else "Spend"

        def amount(entries):
            if not priced:
                return f"{sum(self.token_count(c) for c in entries):,}"
            total = self.spend_total(entries)
            return total if isinstance(total, str) else dollars(total)

        order = sorted(owners, key=lambda o: (o == "epic", int(o[1:]) if o != "epic" else 0))
        return ([f"| Stage | Sessions | {column} |", "| --- | ---: | ---: |",
                 *(f"| {s} | {len(entries)} | {amount(entries)} |" for s, entries in stages.items())],
                [f"| Ticket | {column} |", "| --- | ---: |",
                 *(f"| {'epic stages' if o == 'epic' else o} | {amount(owners[o])} |" for o in order)],
                self.spend_total([c for _, c in found]))

    def report_costs(self):
        """The end-of-run table; with --post-costs, the same table on the epic issue, never the pull request."""
        tables = self.cost_tables()
        if not tables:
            return
        by_stage, by_ticket, total = tables
        amount = total if isinstance(total, str) else dollars(total)
        title = "Usage" if isinstance(total, str) else "Cost"
        estimate = "" if isinstance(total, str) else " (estimated)"
        say(f"\n{title} of epic #{self.a.parent} so far{estimate}: {amount}", BOLD)
        for row in by_stage[2:] + [""] + by_ticket[2:]:
            cells = [c.strip() for c in row.strip("|").split("|")] if row else []
            say("  " + "  ".join(f"{c:<16}" if i == 0 else f"{c:>9}" for i, c in enumerate(cells)), DIM)
        if not (getattr(self.a, "post_costs", False) and self.spent):
            return
        body = self.folder / "costs.md"
        note = "" if isinstance(total, str) else " (the agent CLI's estimate, not a bill)"
        body.write_text(f"{title} of epic #{self.a.parent} so far: {amount}{note}.\n\n"
                        + "\n".join(by_stage) + "\n\n" + "\n".join(by_ticket) + "\n")
        result = subprocess.run(["gh", "issue", "comment", str(self.a.parent), "--body-file", str(body)],
                                capture_output=True, text=True)
        if result.returncode:
            say(f"Warning: could not post the cost table on #{self.a.parent} ({result.stderr.strip()}); it is in "
                f"{body}", YELLOW, err=True)

    def cancel_children(self):
        """Prevent further spawns and terminate active process groups; joining belongs to the caller."""
        with self.spawn:
            self.stopping.set()
            children = list(self.children)
        for child in children:
            kill(child)

    def parallel(self, jobs):
        """Run sessions at once; return results in order. Run-wide failures cancel siblings immediately."""
        if len(jobs) == 1:
            return [jobs[0]()]
        results, errors = [None] * len(jobs), [None] * len(jobs)

        def run(i, job):
            try:
                results[i] = job()
            except BaseException as exc:  # handed to the main thread below
                errors[i] = exc
                if isinstance(exc, Fatal):
                    with self.spawn:
                        if self.fatal is None:
                            self.fatal = exc
                    self.cancel_children()

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
        if self.fatal is not None:
            raise self.fatal
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
