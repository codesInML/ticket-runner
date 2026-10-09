"""Launch plans, epic pushes, pull requests, and CI-gated merges."""

import os
import re
import subprocess

from .display import say
from .github import gh_json
from .processes import attempt, command, dirty, git
from .settings import BLUE, BOLD, CONFIG, DIM, GREEN, RETRYABLE
from .text import section


class Shipping:
    def ship(self):
        """Launch plan, push and pull request for a SHIP verdict; with --merge, merge once CI is green."""
        a, rec = self.a, self.data.setdefault("_ship", {})
        head = git(self.root, "rev-parse", "HEAD")
        verify = self.data.get("_verify", {})
        verified = not a.no_verify and verify.get("status") == "pass" and verify.get("head") == head
        evidence = self.evidence(head)
        if rec.get("head") != head or rec.get("verified") != verified or rec.get("evidence") != evidence:
            self.update(rec, head=head, verified=verified, evidence=evidence, status="in-progress",
                        drop=("plan", "pr", "url", "reason"))
        say(f"\n━━ Ship stage for #{a.parent} at {head[:8]} ━━", BOLD, BLUE)
        if rec.get("pr"):
            remote = gh_json("pr", "view", str(rec["pr"]), "--json", "state,headRefOid,headRefName")
            if remote.get("headRefOid") != head or remote.get("headRefName") != self.epic:
                return self.epic_fail(rec, "needs-human", f"pull request #{rec['pr']} no longer matches "
                                      "the reviewed branch and commit; inspect it before shipping")
            if remote.get("state") == "CLOSED":
                return self.epic_fail(rec, "needs-human", f"pull request #{rec['pr']} was closed without merging; "
                                      "reopen it before shipping")
            if remote.get("state") not in ("OPEN", "MERGED"):
                return self.epic_fail(rec, "needs-human", "could not confirm the pull request state")
            self.update(rec, status="merged" if remote["state"] == "MERGED" else "pr-open")
            say(f"Pull request #{rec['pr']} is already up to date for {head[:8]} ({rec.get('url')})", DIM)
        else:
            if not self.launch_plan(rec, head, verified):
                return 1
            if not self.push(rec):
                return 1
            if not self.pull_request(rec, head, verified):
                return 1
        if rec.get("status") == "merged":
            say(f"Pull request #{rec['pr']} already merged", DIM)
            return 0
        return self.merge_pr(rec, head, verified) if a.merge else 0

    def launch_plan(self, rec, head, verified):
        a, sdir = self.a, self.folder / "ship"
        plan = rec.setdefault("plan", {})
        if plan.get("status") == "done" and plan.get("head") == head and (sdir / "launch-plan.md").exists():
            say(f"Launch plan already written for {head[:8]}: {os.path.relpath(sdir / 'launch-plan.md')}", DIM)
            return True
        session = plan.get("sessionId") if a.resume and plan.get("status") in RETRYABLE \
            and plan.get("head") == head else None
        self.update(plan, drop=() if session else ("sessionId",))
        base, report = self.data["_meta"]["epicBase"], self.folder / "verify" / "report.md"
        evidence = (f"the verify report at {report}" if verified else
                    "no verify report: the verify stage was skipped, so say \"verify skipped\" in the checklist "
                    "and count every runtime check as unverified")
        dod = self.skills_dir.parent / "references" / "definition-of-done.md"
        say(f"Launch plan with {a.final_model}", BOLD, BLUE)
        text = self.run_agent(a.final_model, a.final_effort, a.final_max_turns, f"""You write the launch plan for epic #{a.parent}, which passed its epic review with VERDICT=SHIP.
The runner pastes your report into the epic's pull request as written. Follow
{self.skill_file('shipping-and-launch')} and the definition of done in {dod}.
Read AGENTS.md, the parent issue with gh issue view {a.parent}, git diff {base}..{head} and git log
{base}..{head}, the epic review at {self.folder / 'review' / 'final.txt'}, and {evidence}.
{self.domain()}Write these markdown sections, in order, each backed by evidence (file:line, a command and its output,
a report line), not boilerplate:
## Pre-launch checklist: the shipping-and-launch checklist and the definition of done, each item checked
with its evidence or unchecked with what is missing; drop items that cannot apply, one line each on why.
## Feature flags: every flag the epic adds or changes, its default, and the ticket that removes it (or "no
removal ticket"). "None." when there are none.
## Migrations: every schema or data migration and how to roll it back. "None." when there are none.
## Residual risk: every UNVERIFIABLE runtime check and why, and anything else nobody verified.
## Rollback plan: trigger conditions, steps, and time to roll back.
## Post-merge watch list: what to watch after the merge, and the threshold that triggers the rollback.
Deployment is out of scope: no deploy steps and no deploy section.
Read-only: do not edit files, commit, change branches, push, open pull requests, or post comments.
Use only CommandCode for agent work; do not invoke codex or claude CLIs.
""", sdir / "plan", plan, session, tag="launch plan")
        self.guard()
        if dirty(self.root) or git(self.root, "rev-parse", "HEAD") != head:
            self.epic_fail(rec, "needs-human", "the launch plan agent changed the main worktree, but it must be "
                           "read-only; inspect it, then --final-only")
            return False
        if plan["status"] != "reported":
            self.epic_fail(rec, plan["status"], f"the launch plan ended {plan['status']}; --resume --final-only "
                           f"continues it (logs in {sdir / 'plan'})")
            return False
        if section(text, "Rollback plan") is None:
            self.epic_fail(rec, "needs-human", f"the launch plan has no `## Rollback plan`; see {sdir / 'plan'}")
            return False
        (sdir / "launch-plan.md").write_text(text.strip() + "\n")
        self.update(plan, status="done")
        say(f"Launch plan: {os.path.relpath(sdir / 'launch-plan.md')}", GREEN)
        return True

    def push(self, rec):
        """Push the epic branch; never over commits the remote has that the epic lacks."""
        branch = self.epic
        remote = attempt(self.root, "ls-remote", "--exit-code", "--heads", "origin", branch)
        if remote.returncode not in (0, 2):
            self.epic_fail(rec, "needs-human", f"could not read origin: {remote.stderr.strip()}")
            return False
        if remote.returncode == 0:
            theirs = remote.stdout.split()[0]
            fetched = attempt(self.root, "fetch", "-q", "origin", f"refs/heads/{branch}")
            if fetched.returncode:
                self.epic_fail(rec, "needs-human", f"could not fetch origin/{branch}: {fetched.stderr.strip()}")
                return False
            if attempt(self.root, "merge-base", "--is-ancestor", theirs, branch).returncode:
                self.epic_fail(rec, "needs-human", f"origin/{branch} has commits that {branch} lacks; integrate "
                               "them by hand (the runner never force-pushes), then --final-only")
                return False
        say(f"Pushing {branch} to origin", BOLD)
        pushed = attempt(self.root, "push", "-u", "origin", branch)
        if pushed.returncode:
            self.epic_fail(rec, "needs-human", f"git push failed: {pushed.stderr.strip()}")
            return False
        return True

    def pr_base(self):
        if self.cfg["prBase"]:
            return self.cfg["prBase"]
        found = attempt(self.root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").stdout.strip()
        if found:
            return found.split("/", 1)[1]
        return command("gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name")

    def verify_summary(self, verified):
        verify = self.data.get("_verify", {})
        if not verified:
            return "Verify skipped (--no-verify)."
        if verify.get("ran") is False:
            return f"Verify ran nothing: no `verify` commands and no `serve` in {CONFIG}."
        lines = [f"- {'PASS' if c['rc'] == 0 else 'FAIL'} `{c['cmd']}`" for c in verify.get("commands", [])]
        runtime = verify.get("runtime", {})
        if runtime.get("report"):
            counts = ", ".join(f"{v} {k}" for k, v in runtime.get("counts", {}).items())
            lines.append(f"- Runtime checks: VERIFY={runtime['status'].upper()} ({counts})")
        else:
            lines.append(f"- Runtime checks {runtime.get('status', 'skipped')}: {runtime.get('reason', '')}")
        if verify.get("repaired"):
            lines.append("- One repair round ran before verify passed.")
        return "\n".join(lines)

    def pull_request(self, rec, head, verified):
        """Open the epic's pull request, or refresh the body of the one already open; the runner writes it."""
        a, sdir, branch = self.a, self.folder / "ship", self.epic
        parent = self.parent_issue()
        base_commit = self.data["_meta"]["epicBase"]
        commits = git(self.root, "rev-list", "--count", f"{base_commit}..{head}")
        stat = git(self.root, "diff", "--shortstat", base_commit, head) or "no file changes"
        tickets = "\n".join(f"- #{n} {self.tickets[n]['title']}" for n in self.order)
        parts = [f"Epic #{a.parent}: {parent.get('title', '')}", "",
                 f"{commits} commits from `{base_commit[:8]}` to `{head[:8]}`: {stat.strip()}.", "",
                 "## Tickets", tickets, "", "## Verify", self.verify_summary(verified), "",
                 "## Launch plan", (sdir / "launch-plan.md").read_text().strip()]
        review = self.folder / "review" / "final.txt"
        deferred = section(review.read_text(), "DEFERRED") if review.exists() else None
        if deferred and not re.fullmatch(r"none\.?", deferred, re.I):
            parts += ["", "## Deferred findings", deferred]
        body = sdir / "pr-body.md"
        body.write_text("\n".join(parts) + "\n")
        title = f"{parent.get('title') or 'Epic'} (#{a.parent})"
        try:
            found = gh_json("pr", "list", "--head", branch, "--state", "open", "--json", "number,url")
            if found:
                command("gh", "pr", "edit", str(found[0]["number"]), "--body-file", str(body))
                say(f"Updated pull request #{found[0]['number']}: {found[0]['url']}", GREEN, BOLD)
            else:
                command("gh", "pr", "create", "--base", self.pr_base(), "--head", branch, "--title", title,
                        "--body-file", str(body))
                found = gh_json("pr", "list", "--head", branch, "--state", "open", "--json", "number,url")
                if not found:
                    raise RuntimeError("gh pr create succeeded but no open pull request lists the branch")
                say(f"Opened pull request #{found[0]['number']}: {found[0]['url']}", GREEN, BOLD)
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            self.epic_fail(rec, "needs-human", f"could not open or update the pull request ({exc}); body in {body}")
            return False
        self.update(rec, status="pr-open", pr=found[0]["number"], url=found[0]["url"], drop=("reason",))
        return True

    def merge_pr(self, rec, head, verified):
        """--merge: only a SHIP verdict on a verified head, and only once CI is green; never deletes branches."""
        review, verify, n = self.data.get("_review", {}), self.data.get("_verify", {}), rec["pr"]
        problems = []
        if review.get("status") != "ship" or review.get("head") != head:
            problems.append(f"the epic review did not say SHIP for {head[:8]}")
        if not verified:
            problems.append("the verify stage did not pass at this head")
        elif verify.get("ran") is False:
            problems.append(f"the verify stage ran nothing (no verify commands or serve in {CONFIG})")
        if verify.get("runtime", {}).get("counts", {}).get("FAILED"):
            problems.append("a runtime check FAILED")
        if problems:
            return self.epic_fail(rec, "merge-refused", "--merge refused: " + "; ".join(problems))
        say(f"Waiting for CI on pull request #{n}", BOLD)
        if subprocess.run(["gh", "pr", "checks", str(n), "--watch"], cwd=self.root).returncode:
            return self.epic_fail(rec, "needs-human", f"CI is red (or reported no checks) on pull request #{n}; "
                                  "fix it, then --final-only --merge")
        merged = subprocess.run(["gh", "pr", "merge", str(n), f"--{self.cfg['mergeMethod']}",
                                 "--match-head-commit", head], capture_output=True, text=True)
        if merged.returncode:
            return self.epic_fail(rec, "needs-human", f"gh pr merge failed: {merged.stderr.strip()}")
        self.update(rec, status="merged", drop=("reason",))
        say(f"Merged pull request #{n} ({self.cfg['mergeMethod']})", GREEN, BOLD)
        return 0
