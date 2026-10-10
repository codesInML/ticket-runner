"""Epic verification, runtime checks, repair, and reports."""

from pathlib import Path
import os
import hashlib
import json
import re
import subprocess
import time

from .display import say
from .processes import attempt, edited, git, kill, probe, read_env, rebasing, untracked
from .settings import BLUE, BOLD, BROWSER_TOOLS, CONFIG, DIM, Fatal, GREEN, RED, RETRYABLE, YELLOW
from .text import nested_sections, section, verdict


class Verification:
    def verify(self):
        rec = self.data.setdefault("_verify", {})
        tip = git(self.root, "rev-parse", self.epic)
        vdir = self.folder / "verify"
        fingerprint = self.verify_fingerprint(tip)
        if rec.get("status") == "pass" and rec.get("head") == tip and rec.get("fingerprint") == fingerprint:
            say(f"Verify stage already passed at {tip[:8]}; skipping", DIM)
            return 0
        vdir.mkdir(parents=True, exist_ok=True)
        if not self.cfg["verify"] and not self.cfg["serve"]:
            self.update(rec, status="pass", head=tip, fingerprint=fingerprint, ran=False, drop=("reason", "phase"))
            self.verify_report(rec, tip, [], {"status": "skipped", "reason": "no `verify` commands and no "
                                              f"`serve` in {CONFIG}"})
            say(f"Verify stage: nothing configured in {CONFIG}; skipping", DIM)
            return 0
        say(f"\n━━ Verify stage for #{self.a.parent} at {tip[:8]} ━━", BOLD, BLUE)
        wt = self.verify_worktree(rec, tip)
        if wt is None:
            return 1
        while True:
            # A repaired commit cannot be accepted until its gate passed for this
            # exact head and command, including after an interrupted/failed run.
            if git(wt, "rev-parse", "HEAD") != tip and not self.verify_gate(rec, wt, vdir):
                return 1
            if rec.get("phase") != "repair":
                passed = self.verify_pass(rec, wt)
                if passed is None:
                    return 1
                if passed:
                    break
                if rec.get("repaired"):
                    return self.epic_fail(rec, "needs-human", f"verify still fails after one repair; fix it in "
                                          f"{os.path.relpath(wt)} (commit there), then --epic-only. Report: "
                                          f"{vdir / 'report.md'}")
                self.update(rec, phase="repair")
            if not self.verify_repair(rec, wt, vdir):
                return 1
            self.update(rec, phase="recheck")
        return self.verify_merge(rec, wt, tip)

    def verify_fingerprint(self, head):
        files = {}
        for name in dict.fromkeys([*self.cfg["linkFiles"], self.cfg["serveEnv"]]):
            if name:
                path = self.root / name
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        inputs = {"head": head, "config": self.cfg, "gate": self.gate_cmd, "install": self.install_cmd,
                  "files": files, "parent": self.parent_issue(),
                  "tickets": [{"number": n, "title": self.tickets[n]["title"],
                               "verification": list(nested_sections(self.tickets[n].get("body") or "", "Verification"))}
                              for n in self.order]}
        return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()

    def verify_gate(self, rec, wt, vdir):
        head = git(wt, "rev-parse", "HEAD")
        evidence = {"head": head, "command": self.gate_cmd}
        if rec.get("gatePassed") == evidence:
            return True
        log = self.log(vdir, "gate")
        say(f"verify gate: {self.gate_cmd}", BOLD)
        before_untracked = untracked(wt)
        rc = self.shell(self.gate_cmd, wt, log)
        if self.verify_tree_problem(wt, head) or untracked(wt) - before_untracked:
            self.update(rec, unexpectedUntracked=sorted(untracked(wt) - before_untracked))
            self.epic_fail(rec, "needs-human", "the verify gate changed the worktree; inspect it, then --epic-only")
            return False
        if rc:
            self.epic_fail(rec, "needs-human", f"the gate fails after the verify repair; see {log}. "
                           f"Fix it in {os.path.relpath(wt)}, then --epic-only")
            return False
        self.update(rec, gatePassed=evidence)
        return True

    def verify_tree_problem(self, wt, head):
        return rebasing(wt) or edited(wt) or git(wt, "rev-parse", "HEAD") != head or \
            self.check_worktree(wt, self.verify_branch())

    def verify_branch(self):
        return f"cc/{self.a.parent}/verify"

    def verify_worktree(self, rec, tip):
        """The verify worktree at the epic tip; a kept one (after needs-human) is reused with its fixes."""
        branch, wt = self.verify_branch(), self.worktrees / "verify"
        created = not wt.exists()
        if created:
            self.worktrees.mkdir(parents=True, exist_ok=True)
            if attempt(self.root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode:
                git(self.root, "worktree", "add", "-q", "-b", branch, str(wt), tip)
                self.update(rec, drop=("repaired", "phase", "check", "repair", "reason", "gatePassed"))
            else:
                git(self.root, "worktree", "add", "-q", str(wt), branch)
        if created:
            self.provision(wt)
        unexpected = set(rec.get("unexpectedUntracked", [])) & untracked(wt)
        if unexpected:
            self.epic_fail(rec, "needs-human", f"unexpected uncommitted files remain in {wt}: "
                           f"{', '.join(sorted(unexpected))}; commit or remove them, then --epic-only")
            return None
        self.update(rec, drop=("unexpectedUntracked",))
        resuming = self.a.resume and rec.get("phase") == "repair" and \
            rec.get("repair", {}).get("status") in RETRYABLE
        problem = (f"{os.path.relpath(wt)} is mid-rebase" if rebasing(wt) else
                   f"{os.path.relpath(wt)} has uncommitted changes" if edited(wt) and not resuming else
                   self.check_worktree(wt, branch))
        if not problem and attempt(wt, "merge-base", "--is-ancestor", tip, "HEAD").returncode:
            if attempt(wt, "merge-base", "--is-ancestor", "HEAD", tip).returncode:
                problem = f"{branch} has diverged from {self.epic}; rebase it onto {self.epic} or remove it"
            else:
                git(wt, "merge", "-q", "--ff-only", tip)
        if problem:
            self.epic_fail(rec, "needs-human", f"{problem}; fix it, then --epic-only")
            return None
        self.provision(wt)
        self.update(rec, status="in-progress", head=tip, worktree=str(wt), drop=("reason",))
        return wt

    def verify_pass(self, rec, wt):
        """Every verify command, then the runtime checks; True, False, or None when a person must look."""
        vdir, cmds = self.folder / "verify", self.cfg["verify"]
        results = []
        expected_head = git(wt, "rev-parse", "HEAD")
        before_untracked = untracked(wt)
        for i, cmd in enumerate(cmds, 1):
            log = vdir / f"cmd-{i}.log"
            say(f"verify [{i}/{len(cmds)}]: {cmd}", BOLD)
            rc = self.shell(cmd, wt, log)
            say(f"verify [{i}/{len(cmds)}] {'passed' if rc == 0 else f'failed (exit {rc})'} ({os.path.relpath(log)})",
                BOLD, GREEN if rc == 0 else RED)
            results.append({"cmd": cmd, "rc": rc, "log": str(log)})
            self.guard()
            if self.verify_tree_problem(wt, expected_head) or untracked(wt) - before_untracked:
                self.update(rec, unexpectedUntracked=sorted(untracked(wt) - before_untracked))
                self.epic_fail(rec, "needs-human", f"verify command changed tracked code, HEAD, or worktree "
                               f"configuration; inspect {wt}, then --epic-only")
                return None
        if any(r["rc"] for r in results):
            runtime = {"status": "not-run", "reason": "verify commands failed"} if self.cfg["serve"] else \
                {"status": "skipped", "reason": f"no `serve` in {CONFIG}"}
        elif self.cfg["serve"]:
            runtime = self.runtime_checks(rec, wt, vdir)
        else:
            runtime = {"status": "skipped", "reason": f"no `serve` in {CONFIG}"}
        head = git(wt, "rev-parse", "HEAD")
        self.update(rec, commands=results, runtime={k: v for k, v in runtime.items() if k != "text"})
        self.verify_report(rec, head, results, runtime)
        if runtime["status"] == "stuck":
            self.epic_fail(rec, runtime.get("agent", "needs-human"), runtime["reason"])
            return None
        self.update(rec, checkedHead=head)
        passed = not any(r["rc"] for r in results) and runtime["status"] in ("pass", "skipped")
        say(f"Verify {'passed' if passed else 'failed'}. Report: {os.path.relpath(vdir / 'report.md')}",
            GREEN if passed else RED, BOLD)
        return passed

    def runtime_checks(self, rec, wt, vdir):
        cfg = self.cfg
        env = dict(os.environ, **read_env(self.root / cfg["serveEnv"]))
        log = vdir / "serve.log"
        say(f"verify: starting the app with {cfg['serve']} (env from {cfg['serveEnv']})", BOLD)
        with log.open("w") as out:
            app = self.spawn_child(cfg["serve"], shell=True, cwd=wt, env=env, stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + cfg["readyTimeout"]
            while not probe(cfg["ready"], wt, env, min(30, max(0.001, deadline - time.monotonic()))):
                if app.poll() is not None:
                    return {"status": "fail", "reason": f"the app exited ({app.returncode}) before `ready` "
                            f"succeeded; see {log}"}
                if time.monotonic() > deadline:
                    return {"status": "fail", "reason": f"`ready` did not succeed within {cfg['readyTimeout']}s; "
                            f"see {log}"}
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            say("verify: the app is ready; running the verifier", BOLD)
            check, head = rec.setdefault("check", {}), git(wt, "rev-parse", "HEAD")
            session = check.get("sessionId") if self.a.resume and check.get("status") in RETRYABLE \
                and check.get("head") == head else None
            self.update(rec, phase="check")
            self.update(check, drop=() if session else ("sessionId",))
            extra = ["--tools-enable", ",".join(BROWSER_TOOLS)] if cfg["browser"] else []
            verifier_untracked = untracked(wt)
            text = self.run_agent(self.a.verify_model, self.a.verify_effort, self.a.verify_max_turns,
                                  self.verifier_prompt(wt), vdir / "check", check, session, cwd=wt,
                                  tag="verify", extra=extra)
            self.guard()
            self.update(rec, phase=None)
            if self.verify_tree_problem(wt, head) or untracked(wt) - verifier_untracked:
                self.update(rec, unexpectedUntracked=sorted(untracked(wt) - verifier_untracked))
                return {"status": "stuck", "reason": f"the verifier changed {os.path.relpath(wt)}, but it must "
                        "be read-only; inspect the worktree, then --epic-only"}
            if check["status"] != "reported":
                return {"status": "stuck", "agent": check["status"], "reason": f"the verifier ended "
                        f"{check['status']}; --resume --epic-only continues it (logs in {vdir / 'check'})"}
            result = verdict(text, r"VERIFY=(PASS|FAIL)")
            if result == "MISSING":
                return {"status": "stuck", "reason": f"the verifier gave no VERIFY= line; see {vdir / 'check'}"}
            rows = re.findall(r"^[\s>*_|-]*(?:\d+[.)]\s*)?\**(VERIFIED|FAILED|UNVERIFIABLE)\s+(C\d+)\b[^\n]*", text, re.M)
            expected = {check["id"] for check in self.check_manifest()}
            reported = [check_id for _, check_id in rows]
            if set(reported) != expected or len(reported) != len(set(reported)):
                return {"status": "stuck", "reason": "verifier report must identify every check exactly once "
                        f"using its check ID; see {vdir / 'check'}"}
            counts = {k: sum(status == k for status, _ in rows)
                      for k in ("VERIFIED", "FAILED", "UNVERIFIABLE")}
            if result == "PASS" and counts["FAILED"]:
                return {"status": "stuck", "reason": f"contradictory verifier report: FAILED checks with "
                        f"VERIFY=PASS; see {vdir / 'check'}"}
            # Do not accept unstructured extra failures concealed by a final PASS.
            all_failed = re.findall(r"^[\s>*_|-]*(?:\d+[.)]\s*)?\**FAILED\b", text, re.M)
            if result == "PASS" and all_failed:
                return {"status": "stuck", "reason": f"contradictory verifier report: FAILED checks with "
                        f"VERIFY=PASS; see {vdir / 'check'}"}
            return {"status": result.lower(), "report": str(vdir / "check" / "final.txt"), "counts": counts,
                    "text": text}
        finally:
            kill(app)
            self.reap(app)

    def check_manifest(self):
        """Assign stable IDs to explicit check items; prose sections stay one check."""
        checks = []
        def add(source, body):
            body = re.sub(r"^\s*(```|~~~)[^\n]*\n.*?^\s*\1[^\n]*$", "", body, flags=re.M | re.S)
            items = re.findall(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)(?:\[[ xX]\]\s*)?(.+)$", body, re.M)
            for item in items or ([body.strip()] if body.strip() else []):
                checks.append({"id": f"C{len(checks) + 1:03d}", "source": source, "text": item})
        for n in self.order:
            for _, body in nested_sections(self.tickets[n].get("body") or "", "Verification"):
                add(f"ticket #{n}", body)
        parent = self.parent_issue().get("body") or ""
        checkpoints = list(nested_sections(parent, "Checkpoint"))
        for title, body in checkpoints:
            add(f"parent #{self.a.parent}: {title}", body)
        if not checkpoints:
            add(f"parent #{self.a.parent}", parent)
        if not checks:
            checks.append({"id": "C001", "source": "runtime", "text": "Exercise the running app's primary behavior."})
        return checks

    def checks_file(self):
        """Every ticket's `## Verification` items and the parent issue, for the verifier."""
        parts = [f"# Checks for epic #{self.a.parent}\n"]
        for n in self.order:
            found = "\n\n".join(body for _, body in nested_sections(self.tickets[n].get("body") or "", "Verification"))
            parts.append(f"## #{n} — {self.tickets[n]['title']}\n{found or '(no Verification section)'}\n")
        parts.append(f"## Parent issue #{self.a.parent} (use its checkpoints)\n{self.parent_issue().get('body') or ''}\n")
        parts += ["## Check IDs (report every ID exactly once)",
                  *[f"- {c['id']}: {c['source']} — {c['text']}" for c in self.check_manifest()]]
        path = self.folder / "verify" / "checks.md"
        path.write_text("\n".join(parts))
        return path

    def verify_repair(self, rec, wt, vdir):
        """One repair agent on the implementer model, in the verify worktree."""
        branch, rep = self.verify_branch(), rec.setdefault("repair", {})
        session = rep.get("sessionId") if self.a.resume and rep.get("status") in RETRYABLE else None
        failing = []
        for r in rec.get("commands", []):
            if r["rc"]:
                tail = "\n".join(Path(r["log"]).read_text(errors="replace").splitlines()[-80:])
                failing.append(f"  $ {r['cmd']}   (exit {r['rc']}; full log: {r['log']})\n```\n{tail}\n```")
        runtime = rec.get("runtime", {})
        if runtime.get("status") == "fail" and runtime.get("report"):
            failing.append(f"FAILED runtime checks: see the verifier's report at {runtime['report']}. Fix only the "
                           "FAILED checks; UNVERIFIABLE ones are not failures.")
        elif runtime.get("status") == "fail":
            failing.append(f"The app did not start for the runtime checks: {runtime.get('reason')}")
        head = git(wt, "rev-parse", "HEAD")
        say(f"\n━━ Verify repair with {self.a.model} in {os.path.relpath(wt)} ━━", BOLD, YELLOW)
        prompt = f"""The runner's verify stage failed for epic #{self.a.parent} at {head[:12]}, in this worktree ({wt})
on branch {branch}. Each verify command ran in its own process from the worktree root.
{chr(10).join(failing)}
Follow {self.skill_file('debugging-and-error-recovery')}: reproduce each failure, localize it, fix the root
cause, then guard it with a regression test. Re-run the failing commands, then the gate `{self.gate_cmd}`.
Commit onto {branch} referencing #{self.a.parent}. Do not push, rebase, merge, change branches, or add
co-author trailers. Do not modify the runner, {self.hands_off}, agent configuration, hooks, or anything
under {self.folder}. When the fix needs a human decision (the check itself is wrong, or fixing it changes
what a ticket asked for), commit buildable work, explain, and end with STATUS=NEEDS_HUMAN.
End with exactly one line: STATUS=COMPLETE, STATUS=NEEDS_HUMAN, or STATUS=BLOCKED.
"""
        self.update(rep, drop=() if session else ("sessionId",))
        before = untracked(wt)  # The app's leftovers; a file the repair adds but never commits is not.
        text = self.run_agent(self.a.model, self.a.effort, self.a.max_turns, prompt, vdir / "repair", rep,
                              session, cwd=wt, tag="verify repair", color=YELLOW)
        self.guard()
        left = edited(wt) or bool(untracked(wt) - before)
        if rep["status"] != "reported":
            self.epic_fail(rec, rep["status"] if not left else "needs-human",
                           f"the verify repair ended {rep['status']}; work kept in {os.path.relpath(wt)}; "
                           "--resume --epic-only continues it")
            return False
        self.update(rec, repaired=True)
        status = verdict(text)
        problem = ("a rebase is in progress" if rebasing(wt) else "uncommitted changes remain" if left
                   else self.check_worktree(wt, branch))
        if status != "COMPLETE" or problem:
            self.update(rec, phase=None)
            self.epic_fail(rec, "needs-human", f"the verify repair ended status={status}"
                           f"{'; ' + problem if problem else ''}; see {vdir / 'repair'}. Fix it in "
                           f"{os.path.relpath(wt)}, then --epic-only")
            return False
        return True

    def verify_merge(self, rec, wt, tip):
        """Fast-forward the epic to the verified head (same guards as a ticket), then drop the worktree."""
        branch, head = self.verify_branch(), git(wt, "rev-parse", "HEAD")
        if self.verify_tree_problem(wt, rec.get("checkedHead")):
            return self.epic_fail(rec, "needs-human", f"verification worktree changed since checks; inspect {wt}")
        if head != tip and rec.get("gatePassed") != {"head": head, "command": self.gate_cmd}:
            return self.epic_fail(rec, "needs-human", "verification repair gate has not passed for this commit")
        if head != tip:
            touched = git(wt, "diff", "--name-only", tip, "HEAD", "--", *self.protected)
            if touched:
                return self.epic_fail(rec, "needs-human", f"the verify repair changed protected files "
                                      f"({', '.join(touched.split())}); revert those commits in "
                                      f"{os.path.relpath(wt)}, then --epic-only")
            with self.integration:
                self.check_main()
                git(self.root, "merge", "-q", "--ff-only", branch)
                self.guard()
            say(f"Verified fixes merged into {self.epic} (now {head[:8]})", GREEN, BOLD)
            if self.lock_changed(self.root, tip, head):
                say(f"a lockfile changed; running {self.install_cmd} in the main worktree", BOLD)
                if subprocess.run(self.install_cmd, shell=True, cwd=self.root).returncode:
                    raise Fatal(f"{self.install_cmd} failed in the main worktree after the verify repair")
        self.update(rec, status="pass", head=head, fingerprint=self.verify_fingerprint(head), ran=True, phase=None, drop=("reason",))
        try:
            git(self.root, "worktree", "remove", "--force", str(wt))
            git(self.root, "branch", "-q", "-d", branch)
        except subprocess.CalledProcessError as exc:
            say(f"Verify passed, but removing {os.path.relpath(wt)} failed ({exc}); remove it by hand", YELLOW)
        return 0

    def verify_report(self, rec, head, results, runtime):
        lines = [f"# Verify report: epic #{self.a.parent} at {head[:12]}", "", "## Commands"]
        lines += [f"- {'PASS' if r['rc'] == 0 else 'FAIL'} `{r['cmd']}`"
                  f"{'' if r['rc'] == 0 else ' (exit ' + str(r['rc']) + ')'}: log {r['log']}" for r in results]
        lines += [] if results else ["- none configured"]
        lines += ["", "## Runtime checks"]
        status = runtime["status"]
        if status in ("skipped", "not-run"):
            lines.append(f"- {'skipped' if status == 'skipped' else 'not run'}: {runtime['reason']}")
        elif runtime.get("report"):
            counts = ", ".join(f"{v} {k}" for k, v in runtime.get("counts", {}).items())
            lines.append(f"- VERIFY={status.upper()} ({counts}); verifier report: {runtime['report']}")
        else:
            lines.append(f"- {status}: {runtime['reason']}")
        if rec.get("repaired"):
            lines += ["", "## Repair", f"- one repair round ran: {self.folder / 'verify' / 'repair' / 'final.txt'}"]
        if runtime.get("text"):
            lines += ["", "## Runtime check results", runtime["text"].strip()]
        (self.folder / "verify" / "report.md").write_text("\n".join(lines) + "\n")
