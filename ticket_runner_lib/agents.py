"""CommandCode sessions and logged shell processes."""

import json
import os
import subprocess
import time

from .display import narrate, say
from .processes import git, kill
from .settings import BLUE, DIM, Stop


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
                  tag="review", color=BLUE, extra=()):
        cwd = cwd or self.root
        folder.mkdir(parents=True, exist_ok=True)
        number = record.get("attempts", 0) + 1
        self.update(record, attempts=number, model=model, effort=effort, maxTurns=turns)
        argv = [self.binary, "-p", prompt, "-m", model, "--max-turns", str(turns),
                "--output-format", "json", "--yolo", "--trust", "--skip-onboarding", "--no-auto-update"]
        if effort and effort != "none":
            argv.extend(["--effort", effort])
        if session:
            argv.extend(["--resume", session])
        argv.extend(extra)
        # Separate stdout NDJSON from stderr, and preserve every attempt's logs.
        events = folder / f"attempt-{number}.jsonl"
        result = None
        started = time.monotonic()
        say(f"{tag} streaming to {os.path.relpath(events)}", DIM)
        with events.open("w") as output, (folder / f"attempt-{number}.log").open("w") as errors:
            child = self.spawn_child(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=errors, text=True)
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
                    event = frame.get("event") if isinstance(frame.get("event"), dict) else {}
                    # Session IDs may arrive before the final result, allowing interrupt recovery.
                    sid = frame.get("sessionId") or event.get("sessionId")
                    if isinstance(sid, str) and sid and sid != record.get("sessionId"):
                        self.update(record, sessionId=sid)
                    if frame.get("type") == "result":
                        result = frame
                    elif event:
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
        text = str((result or {}).get("finalText") or "")
        (folder / "final.txt").write_text(text)
        if rc == 8 or (result or {}).get("subtype") == "max_turns":
            status = "turn-cap"
        elif rc or not result or result.get("subtype") != "success" or not text.strip():
            status = "provider-error" if rc in {3, 5, 6, 7, 10} else "no-output"
        else:
            status = "reported"
        self.update(record, exitCode=rc, head=head, status=status)
        return text

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
