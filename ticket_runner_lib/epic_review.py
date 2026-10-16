"""The epic review: independent reviewers, cross-examination, fix tickets, a follow-up, and closing tickets."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time

from .display import say
from .github import discover, gh_json
from .graph import waves
from .processes import attempt, dirty, git
from .settings import BLUE, BOLD, DIM, FIX_LABEL, GREEN, MODELS, RETRYABLE, REVIEW_PROTOCOL, YELLOW
from .text import empty, review_rows, section, verdict


FINDING = ("BLOCKING", "DEFERRED")
STEPS = {"review": "independent review", "check": "cross-examination", "tickets": "fix ticket drafts",
         "follow": "follow-up review", "follow-check": "follow-up cross-examination"}


def reviewer_name(model):
    """The name a person calls a model by: its alias (deepseek over flash), else its last path part."""
    names = [alias for alias, full in MODELS.items() if full == model]
    return "deepseek" if "deepseek" in names else names[0] if names else model.rsplit("/", 1)[-1]


def outcome(finding):
    return "withdrawn" if finding.get("withdrawn") else finding.get("outcome") or "unchecked"


class EpicReview:
    @staticmethod
    def blocking(finding):
        return finding["class"] == "BLOCKING" and outcome(finding) == "confirmed"

    def review_dir(self):
        return self.folder / "review"

    def review_fix_tickets(self, rv):
        """Own filed tickets, including a create interrupted before its number was saved."""
        marker = f"<!-- ticket-runner: epic #{self.a.parent} review {rv['start'][:12]} draft " if rv.get("start") else None
        created = {n for n in self.order if marker and marker in (self.tickets[n].get("body") or "")}
        return set(rv.get("fixTickets", [])) | set(rv.get("filed", {}).values()) | created

    def review_fingerprint(self, rv=None):
        """Requirements and diff base, independent of verification's runtime configuration."""
        rv = rv or {}
        pending_fixes = self.review_fix_tickets(rv) - set(rv.get("inputTickets", []))
        inputs = {"parent": self.parent_issue(), "base": self.data["_meta"]["epicBase"],
                  "tickets": [{"number": n, "title": self.tickets[n]["title"],
                               "body": self.tickets[n].get("body") or "",
                               "blockers": self.tickets[n]["blockers"]}
                              for n in self.order if n not in pending_fixes]}
        return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()

    def review_covers(self, head):
        """A SHIP verdict holds for its own head, and for the head a passing repair review extended it to."""
        rv = self.data.get("_review", {})
        if rv.get("protocol") != REVIEW_PROTOCOL or rv.get("workflow") != self.a.workflow or \
                rv.get("fingerprint") != self.review_fingerprint(rv) or rv.get("status") != "ship":
            return False
        if rv.get("head") == head:
            return True
        return head in rv.get("acceptedRepairs", [])

    def review_resumable(self, rv, head):
        """An unfinished review of the same epic by the same reviewers, at the head its phase expects."""
        if rv.get("protocol") != REVIEW_PROTOCOL or rv.get("workflow") != self.a.workflow or \
                rv.get("models") != self.a.epic_review_models or \
                rv.get("fingerprint") != self.review_fingerprint(rv):
            return False
        phase = rv.get("phase")
        if phase in ("review", "check", "tickets", "file"):
            return head == rv["start"]
        if phase == "fix":  # Fix tickets integrating move the epic on from where the review started.
            return not attempt(self.root, "merge-base", "--is-ancestor", rv["start"], head).returncode
        if phase in ("follow", "follow-check", "blocked"):
            return head == rv.get("fixHead")
        return False

    def epic_review(self):
        if dirty(self.root):
            raise RuntimeError("Epic review requires a clean tree")
        head = git(self.root, "rev-parse", "HEAD")
        rv = self.data.get("_review", {})
        if self.review_covers(head):
            say(f"Epic review already said SHIP for {head[:8]}; skipping. Report: "
                f"{os.path.relpath(self.review_dir() / 'report.md')}", GREEN, BOLD)
            self.close_tickets(rv)
            return 0
        if not self.review_resumable(rv, head):
            rv = self.fresh_review(head)
        steps = {"review": self.first_round, "check": self.check_round, "tickets": self.write_tickets,
                 "file": self.file_tickets, "fix": self.run_fixes, "follow": self.follow_up,
                 "follow-check": lambda rv: self.check_round(rv, follow=True), "blocked": self.still_blocked}
        while True:
            result = steps[rv["phase"]](rv)
            if result is not None:
                return result

    def fresh_review(self, head):
        old, folder = self.data.get("_review", {}), self.review_dir()
        if folder.exists():  # Keep an earlier review's reports beside the new one.
            stem, k = f"review-{(old.get('start') or old.get('head') or 'earlier')[:8]}", 1
            target = folder.with_name(stem)
            while target.exists():
                k += 1
                target = folder.with_name(f"{stem}-{k}")
            folder.rename(target)
        models = self.a.epic_review_models
        names = [reviewer_name(m) for m in models]
        letters = [n[0].upper() for n in names]
        if len(set(letters)) < len(letters) or not all("A" <= x <= "Z" for x in letters):
            letters = list("AB")[:len(models)]
        labels = [m if names.count(n) > 1 else n for n, m in zip(names, models)]
        rv = {"protocol": REVIEW_PROTOCOL, "workflow": self.a.workflow, "models": models, "start": head,
              "head": head, "fingerprint": self.review_fingerprint(), "inputTickets": list(self.order),
              "phase": "review", "status": "in-progress", "findings": {},
              "reviewers": [{"name": label, "letter": x, "model": m, "dir": f"{x.lower()}-{n}"}
                            for n, label, x, m in zip(names, labels, letters, models)]}
        with self.state:
            self.data["_review"] = rv
            self.persist()
        return rv

    def run_reviewers(self, rv, todo, step, prompt):
        """One session turn for each reviewer in `todo`, all at once: the finished ones' texts by full model ID.

        None when the main worktree changed; an unfinished session is left in rv["stopped"] for stopped().
        """
        a, head = self.a, git(self.root, "rev-parse", "HEAD")
        self.update(rv, status="in-progress", drop=("reason",))
        jobs = []
        for r in todo:
            # Later steps resume the reviewer's own session: it already knows the code.
            retry = a.resume and r.get("status") in RETRYABLE and r.get("step") == step
            session = r.get("sessionId") if step != "review" or retry else None
            self.update(r, step=step, drop=() if session else ("sessionId",))
            folder, text = self.review_dir() / r["dir"] / step, prompt(r)
            color = self.stage_color(rv["reviewers"].index(r))
            jobs.append(lambda r=r, folder=folder, text=text, session=session, color=color: self.run_agent(
                r["model"], a.epic_review_effort, a.epic_review_max_turns, text, folder, r, session,
                tag=f"epic review · {r['name']}", color=color))
        say(f"\n━━ Epic review of #{a.parent}: {STEPS[step]} with {' and '.join(r['name'] for r in todo)} ━━",
            BOLD, BLUE)
        texts = self.parallel(jobs)
        self.guard()
        if dirty(self.root) or git(self.root, "rev-parse", "HEAD") != head:
            self.epic_fail(rv, "needs-human", "an epic reviewer changed the main worktree, but the review must be "
                           "read-only; inspect it, then --epic-only")
            return None
        stopped = [r for r in todo if r["status"] != "reported"]
        if stopped:  # Recorded on the review; the caller keeps the others' reports before failing.
            r = stopped[0]
            self.update(rv, stopped=[r["status"], f"the {r['name']} reviewer ended {r['status']} during the "
                                     f"{STEPS[step]}; --resume --epic-only continues it (logs in "
                                     f"{self.review_dir() / r['dir'] / step})"])
        return {r["model"]: text for r, text in zip(todo, texts) if r["status"] == "reported"}

    def stopped(self, rv):
        """The failure a reviewer's unfinished session left for this step, once the others' reports are kept."""
        if rv.get("stopped"):
            status, reason = rv["stopped"]
            self.update(rv, drop=("stopped",))
            return self.epic_fail(rv, status, reason)
        return None

    def parse_findings(self, text, r, end):
        """The FINDINGS lines of one report, or a reason the report cannot be used."""
        body = section(text, "FINDINGS")
        if body is None:
            return None, "no `## FINDINGS` section"
        found, problem = review_rows(body, FINDING)
        if problem:
            return None, problem
        ids = [i for i, _, _ in found]
        if not found and not empty(body):
            return None, f"FINDINGS lines are not in the `- {r['letter']}1 BLOCKING file:line — …` shape"
        if any(i[0] != r["letter"] for i in ids):
            return None, f"its finding IDs must start with {r['letter']}"
        if len(ids) != len(set(ids)):
            return None, "a finding ID appears twice"
        result = verdict(text, end)
        if result == "MISSING":
            return None, f"its report does not end with {end.split('=')[0]}="
        blocking = any(c == "BLOCKING" for _, c, _ in found)
        if result in ("SHIP", "FIX-FIRST", "PASS", "FAIL") and (result in ("SHIP", "PASS")) == blocking:
            return None, f"contradictory report: VERDICT={result} with{'' if blocking else ' no'} BLOCKING findings"
        return {i: (c, t) for i, c, t in found}, None

    def first_round(self, rv):
        todo = [r for r in rv["reviewers"] if not r.get("reviewed")]
        if todo:
            texts = self.run_reviewers(rv, todo, "review", lambda r: self.epic_review_prompt(rv, r))
            if texts is None:
                return 1
            problems = []
            for r in (r for r in todo if r["model"] in texts):
                found, problem = self.parse_findings(texts[r["model"]], r, r"VERDICT=(SHIP|FIX-FIRST)")
                if problem:
                    problems.append(f"{r['name']}: {problem}")
                    continue
                with self.state:
                    for i, (cls, text) in found.items():
                        rv["findings"][i] = {"by": r["model"], "class": cls, "text": text, "round": "review"}
                self.update(r, reviewed=True)
            if self.stopped(rv):
                return 1
            if problems:
                return self.epic_fail(rv, "needs-human", f"unusable epic review report ({'; '.join(problems)}); "
                                      f"see {self.review_dir()}; --epic-only asks again")
        self.update(rv, phase="check")

    def check_round(self, rv, follow=False):
        """Each reviewer answers the other's findings with evidence; a lone reviewer re-checks its own."""
        step, round_, reviewers = ("follow-check", "follow", rv["reviewers"]) if follow else \
            ("check", "review", rv["reviewers"])
        pair = len(reviewers) > 1

        def targets(r):
            return [i for i, f in rv["findings"].items() if f["round"] == round_ and (f["by"] != r["model"]) == pair]

        todo = [r for r in reviewers if targets(r) and step not in r.get("checked", [])]
        if todo:
            def prompt(r):
                other = next((o for o in reviewers if o is not r), None)
                report = self.review_dir() / other["dir"] / round_ / "final.txt" if other else None
                return self.check_prompt(rv, r, other, targets(r), report, follow)
            texts = self.run_reviewers(rv, todo, step, prompt)
            if texts is None:
                return 1
            verbs = ("CONFIRMED", "DISPUTED") if pair else ("CONFIRMED", "WITHDRAWN")
            problems = []
            for r in (r for r in todo if r["model"] in texts):
                text, want = texts[r["model"]], targets(r)
                answers, problem = review_rows(section(text, "ANSWERS"), verbs)
                if problem:
                    problems.append(f"{r['name']}: {problem}")
                    continue
                if verdict(text, r"CHECK=(DONE)") == "MISSING":
                    problems.append(f"{r['name']}: its report does not end with CHECK=DONE")
                    continue
                if sorted(i for i, _, _ in answers) != sorted(want):
                    problems.append(f"{r['name']} must answer each of {', '.join(want)} exactly once")
                    continue
                own = {i for i, f in rv["findings"].items() if f["by"] == r["model"] and f["round"] == round_}
                withdrawn, problem = review_rows(section(text, "WITHDRAWN"), ("WITHDRAWN",)) if pair else ([], None)
                if problem or any(i not in own for i, _, _ in withdrawn):
                    problems.append(f"{r['name']}: {problem or 'withdrawal must identify its own finding'}")
                    continue
                with self.state:
                    for i, verb, evidence in answers:
                        rv["findings"][i].update(outcome={"CONFIRMED": "confirmed", "DISPUTED": "disputed",
                                                          "WITHDRAWN": "withdrawn"}[verb],
                                                 answer=evidence, checkedBy=r["model"])
                    for i, _, why in withdrawn:
                        if i in own:
                            rv["findings"][i]["withdrawn"] = why or "withdrawn by its reviewer"
                    r.setdefault("checked", []).append(step)
                    self.persist()
            if self.stopped(rv):
                return 1
            if problems:
                return self.epic_fail(rv, "needs-human", f"unusable {STEPS[step]} ({'; '.join(problems)}); see "
                                      f"{self.review_dir()}; --epic-only asks again")
        self.write_report(rv)
        if follow:
            return self.conclude(rv)
        blocking = [i for i, f in rv["findings"].items() if self.blocking(f)]
        if not blocking:
            return self.review_ship(rv)
        say(f"Epic review: {len(blocking)} confirmed blocking finding(s) ({', '.join(blocking)}); "
            "writing fix tickets", YELLOW, BOLD)
        self.update(rv, phase="tickets")

    def write_tickets(self, rv):
        """The first reviewer's session drafts fix tickets with the workflow's skill; the runner files them."""
        drafts, writer = self.review_dir() / "tickets", rv["reviewers"][0]
        shutil.rmtree(drafts, ignore_errors=True)  # Nothing is filed yet: start the drafts over.
        drafts.mkdir(parents=True)
        texts = self.run_reviewers(rv, [writer], "tickets",
                                   lambda r: self.ticket_writer_prompt(rv, drafts, rv.get("asked")))
        if texts is None or self.stopped(rv):
            return 1
        status, report = verdict(texts[writer["model"]]), self.review_dir() / writer["dir"] / "tickets" / "final.txt"
        if status == "NEEDS_HUMAN":
            self.update(rv, asked=True)
            return self.epic_fail(rv, "needs-human", f"the ticket writer needs a decision; see {report}. Answer on "
                                  f"#{self.a.parent}, then --epic-only")
        if status != "COMPLETE":
            return self.epic_fail(rv, "needs-human", f"the ticket writer ended status={status}; see {report}")
        parsed, problem = self.read_drafts(rv, drafts)
        if problem:
            return self.epic_fail(rv, "needs-human", f"unusable fix ticket drafts in {drafts}: {problem}; "
                                  "--epic-only writes them again")
        self.update(rv, drafts=parsed, phase="file", drop=("asked",))

    def read_drafts(self, rv, drafts):
        """The drafts in dependency order, or a reason they cannot be filed."""
        parsed = []
        for path in sorted(drafts.glob("*.md")):
            name = re.match(r"(\d{2})-", path.name)
            if not name:
                return None, f"{path.name} is not named NN-slug.md"
            body = path.read_text(errors="replace")
            title = re.match(r"\s*#\s+(.+)", body)
            if not title:
                return None, f"{path.name} does not start with a `# title`"
            for heading in ("Acceptance criteria", "Verification", "Findings"):
                if empty(section(body, heading)):
                    return None, f"{path.name} has no `## {heading}`"
            if re.search(r"\b(L|XL)\b", section(body, "Scope") or ""):
                return None, f"{path.name} is L or XL; split it"
            if len(re.findall(r"^\s*[-*]\s+\S", section(body, "Files likely touched") or "", re.M)) > 8:
                return None, f"{path.name} likely touches more than 8 files; split it"
            parsed.append({"num": name[1], "title": title[1].strip(), "body": body,
                           "findings": re.findall(r"\b[A-Z]\d+\b", section(body, "Findings")),
                           "blockers": re.findall(r"\b(\d{2})\b", section(body, "Blocked by") or "")})
        if not parsed:
            return None, "no drafts were written"
        numbers = [d["num"] for d in parsed]
        if len(numbers) != len(set(numbers)):
            return None, "two drafts share a number"
        wanted = sorted(i for i, f in rv["findings"].items() if self.blocking(f))
        if sorted(i for d in parsed for i in d["findings"]) != wanted:
            return None, f"each confirmed finding ({', '.join(wanted)}) must appear in exactly one draft's Findings"
        for d in parsed:
            unknown = set(d["blockers"]) - set(numbers) | ({d["num"]} & set(d["blockers"]))
            if unknown:
                return None, f"draft {d['num']} is blocked by {', '.join(sorted(unknown))}, which it cannot be"
        try:
            order = [n for level in waves({d["num"]: d["blockers"] for d in parsed}) for n in level]
        except RuntimeError:
            return None, "the drafts' Blocked by entries form a cycle"
        return sorted(parsed, key=lambda d: order.index(d["num"])), None

    def file_tickets(self, rv):
        """File each draft as a sub-issue of the epic, blockers first; a marker makes a retry find its issue."""
        filed = rv.setdefault("filed", {})
        (self.review_dir() / "filed").mkdir(parents=True, exist_ok=True)
        for d in rv["drafts"]:
            if d["num"] in filed:
                continue
            marker = f"<!-- ticket-runner: epic #{self.a.parent} review {rv['start'][:12]} draft {d['num']} -->"
            path = self.review_dir() / "filed" / f"{d['num']}.md"
            path.write_text(self.fix_ticket_body(rv, d, filed, marker))
            number, problem = self.create_fix_ticket(d["title"], path, marker)
            if problem:
                return self.epic_fail(rv, "needs-human", f"could not file fix ticket draft {d['num']} "
                                      f"({problem}); --epic-only retries")
            with self.state:
                filed[d["num"]] = number
                self.persist()
            say(f"Fix ticket #{number}: {d['title']}", BOLD)
        self.update(rv, phase="fix", fixTickets=[filed[d["num"]] for d in rv["drafts"]])

    def create_fix_ticket(self, title, path, marker, tries=4):
        """The draft's issue number and no problem, or a problem; a dropped connection is retried.

        Before every create the marker is looked up, so a create that GitHub accepted but whose reply
        was lost is found rather than filed twice.
        """
        problem = None
        for attempt_no in range(tries):
            if attempt_no:
                time.sleep(attempt_no * 5)
            try:
                existing = gh_json("issue", "list", "--state", "all", "--limit", "1000", "--json", "number,body")
            except RuntimeError as error:
                problem = str(error)
                continue
            number = next((i["number"] for i in existing if marker in (i.get("body") or "")), None)
            if number is not None:
                return number, None
            created = subprocess.run(["gh", "issue", "create", "--title", title, "--body-file", str(path)],
                                     capture_output=True, text=True)
            found = re.search(r"/issues/(\d+)", created.stdout)
            if not created.returncode and found:
                self.link_fix_ticket(int(found[1]))
                return int(found[1]), None
            problem = created.stderr.strip() or created.stdout.strip()
        return None, problem

    def fix_ticket_body(self, rv, d, filed, marker):
        body = re.sub(r"\A\s*#\s+[^\n]*\n", "", d["body"])
        blockers = "\n".join(f"- #{filed[b]}" for b in d["blockers"]) or "None — can start immediately"
        heading = re.compile(r"^(#+\s*Blocked by\s*)$.*?(?=^#+\s|\Z)", re.M | re.S | re.I)
        body = heading.sub(lambda m: f"{m[1]}\n{blockers}\n\n", body) if heading.search(body) else \
            f"{body.rstrip()}\n\n## Blocked by\n{blockers}\n"
        return (f"## Parent\n#{self.a.parent}\n\nFiled by the epic review of #{self.a.parent} at `{rv['start'][:8]}` "
                f"to fix its confirmed findings.\n\n{body.strip()}\n\n{marker}\n")

    def link_fix_ticket(self, n):
        """Label the fix ticket and make it a native sub-issue; the `## Parent` section already makes it one."""
        if not getattr(self, "_fix_label", False):
            subprocess.run(["gh", "label", "create", FIX_LABEL, "--color", "D93F0B", "--force", "--description",
                            "Filed by the ticket runner's epic review"], capture_output=True, text=True)
            self._fix_label = True
        problems = []
        result = subprocess.run(["gh", "issue", "edit", str(n), "--add-label", FIX_LABEL], capture_output=True, text=True)
        if result.returncode:
            problems.append(f"label: {result.stderr.strip()}")
        found = subprocess.run(["gh", "api", f"repos/{{owner}}/{{repo}}/issues/{n}", "--jq", ".id"],
                               capture_output=True, text=True)
        linked = found if found.returncode else subprocess.run(
            ["gh", "api", "--method", "POST", f"repos/{{owner}}/{{repo}}/issues/{self.a.parent}/sub_issues",
             "-F", f"sub_issue_id={found.stdout.strip()}"], capture_output=True, text=True)
        if found.returncode or linked.returncode:
            problems.append(f"sub-issue link: {(linked.stderr or found.stderr).strip()}")
        if problems:
            say(f"Warning: #{n} filed, but {'; '.join(problems)}", YELLOW, err=True)

    def refresh_tickets(self, numbers):
        """Discover the epic again, so filed fix tickets join the graph; the numbers discovery did not find."""
        self.tickets, self.levels = discover(self.a.parent)
        self.order = [n for level in self.levels for n in level]
        self.blockers = {n: t["blockers"] for n, t in self.tickets.items()}
        return [n for n in numbers if n not in self.tickets]

    def run_fixes(self, rv):
        missing = self.refresh_tickets(rv["fixTickets"])
        if missing:
            return self.epic_fail(rv, "needs-human", f"fix ticket(s) {' '.join(f'#{n}' for n in missing)} are not "
                                  f"sub-tickets of #{self.a.parent}; --epic-only looks again")
        if set(rv["fixTickets"]) - set(rv["inputTickets"]):
            inputs = {**rv, "inputTickets": list(self.order)}
            self.update(rv, inputTickets=inputs["inputTickets"], fingerprint=self.review_fingerprint(inputs))
        todo = [n for n in rv["fixTickets"] if not self.complete(n)]
        if todo:
            say(f"\n━━ Fix tickets from the epic review: {' '.join(f'#{n}' for n in todo)} ━━", BOLD, BLUE)
            self.selected = list(self.order)
            outcome_ = self.schedule(self.preflight(todo))
            if outcome_ is not None:
                return outcome_
        self.update(rv, phase="follow", fixHead=git(self.root, "rev-parse", "HEAD"))

    def follow_up(self, rv):
        """The reviewers read only the fix diff: each confirmed finding FIXED or NOT FIXED, and any regression."""
        todo = [r for r in rv["reviewers"] if not r.get("followed")]
        if todo:
            texts = self.run_reviewers(rv, todo, "follow", lambda r: self.follow_up_prompt(rv, r))
            if texts is None:
                return 1
            wanted = sorted(i for i, f in rv["findings"].items() if self.blocking(f))
            problems = []
            for r in (r for r in todo if r["model"] in texts):
                text = texts[r["model"]]
                fixes, fix_problem = review_rows(section(text, "FIXES"), ("NOT FIXED", "FIXED"))
                found, problem = self.parse_findings(text, r, r"FOLLOW-UP=(DONE)")
                problem = problem or fix_problem
                if not problem and sorted(i for i, _, _ in fixes) != wanted:
                    problem = f"it must mark each of {', '.join(wanted)} FIXED or NOT FIXED exactly once"
                if not problem and set(found) & set(rv["findings"]):
                    problem = "its new findings reuse earlier IDs"
                if problem:
                    problems.append(f"{r['name']}: {problem}")
                    continue
                with self.state:
                    for i, verb, evidence in fixes:
                        rv["findings"][i].setdefault("fixes", {})[r["model"]] = [verb, evidence]
                    for i, (cls, text_) in found.items():
                        rv["findings"][i] = {"by": r["model"], "class": cls, "text": text_, "round": "follow"}
                    r["followed"] = True
                    self.persist()
            if self.stopped(rv):
                return 1
            if problems:
                return self.epic_fail(rv, "needs-human", f"unusable follow-up review ({'; '.join(problems)}); see "
                                      f"{self.review_dir()}; --epic-only asks again")
        self.update(rv, phase="follow-check")

    def conclude(self, rv):
        """After the one fix round: SHIP, or a person for anything still blocking."""
        open_ = [i for i, f in rv["findings"].items() if f["round"] == "review" and self.blocking(f)
                 and any(v == "NOT FIXED" for v, _ in f.get("fixes", {}).values())]
        regressions = [i for i, f in rv["findings"].items() if f["round"] == "follow" and self.blocking(f)]
        if not open_ and not regressions:
            return self.review_ship(rv)
        reason = "; ".join(filter(None, [
            open_ and f"still not fixed after the fix tickets: {', '.join(open_)}",
            regressions and f"the fixes introduced: {', '.join(regressions)}"]))
        self.update(rv, phase="blocked")
        self.write_report(rv)
        self.post_findings(rv)
        return self.epic_fail(rv, "needs-human", f"{reason}; see {self.review_dir() / 'report.md'}. Fix it on the "
                              "epic branch, then --epic-only reviews again")

    def still_blocked(self, rv):
        return self.epic_fail(rv, "needs-human", rv.get("reason") or "the epic review is still blocked")

    def review_ship(self, rv):
        head = git(self.root, "rev-parse", "HEAD")
        self.update(rv, status="ship", head=head, phase=None, drop=("reason",))
        self.write_report(rv)
        self.post_findings(rv)
        say(f"Epic review: SHIP at {head[:8]}. Report: {os.path.relpath(self.review_dir() / 'report.md')}",
            GREEN, BOLD)
        self.close_tickets(rv)
        return 0

    def close_tickets(self, rv):
        """Every sub-ticket closes once the epic review says SHIP; the epic itself stays open."""
        closed = rv.setdefault("closed", [])
        for n in self.order:
            if n in closed or self.tickets[n]["state"] == "CLOSED":
                continue
            result = subprocess.run(["gh", "issue", "close", str(n), "--comment",
                                     f"Integrated into `{self.epic}` at `{rv['head'][:8]}`; the epic review of "
                                     f"#{self.a.parent} said SHIP."], capture_output=True, text=True)
            if result.returncode:
                say(f"Warning: could not close #{n} ({result.stderr.strip()}); the next run tries again",
                    YELLOW, err=True)
                continue
            with self.state:
                closed.append(n)
                self.persist()
            say(f"Closed #{n}", DIM)

    def report_sections(self, rv):
        names = {r["model"]: r["name"] for r in rv["reviewers"]}
        def name(model):
            return names.get(model, model)
        def line(i, f):
            text = f"- {i} ({name(f['by'])}) {f['text']}"
            if outcome(f) == "confirmed" and f.get("checkedBy"):
                text += f" — confirmed by {name(f['checkedBy'])}: {f.get('answer') or 'no detail'}"
            if outcome(f) == "disputed":
                text += f" — disputed by {name(f['checkedBy'])}: {f.get('answer') or 'no detail'}"
            if outcome(f) == "withdrawn":
                text += f" — withdrawn: {f.get('withdrawn') or f.get('answer') or 'no detail'}"
            for model, (verb, evidence) in f.get("fixes", {}).items():
                text += f"\n  - {name(model)}: {verb} {evidence}".rstrip()
            return text
        groups = {"Confirmed blocking findings": lambda f: self.blocking(f),
                  "Deferred findings": lambda f: f["class"] == "DEFERRED" and outcome(f) == "confirmed",
                  "Disputed findings": lambda f: outcome(f) == "disputed",
                  "Withdrawn findings": lambda f: outcome(f) == "withdrawn"}
        return {title: [line(i, f) for i, f in rv["findings"].items() if keep(f)] for title, keep in groups.items()}

    def write_report(self, rv):
        """The runner's consolidated report: the launch plan and the pull request read this, not a reviewer's."""
        names = ", ".join(f"{r['name']} ({r['letter']})" for r in rv["reviewers"])
        fixes = (f"; fix tickets {' '.join(f'#{n}' for n in rv['fixTickets'])}" if rv.get("fixTickets") else "") + \
            (f" up to `{rv['fixHead'][:8]}`" if rv.get("fixHead") else "")
        lines = [f"# Epic review of #{self.a.parent}", "",
                 f"Reviewers: {names}. Reviewed `{rv['start'][:8]}`{fixes}. Status: {rv['status']}."]
        for title, items in self.report_sections(rv).items():
            lines += ["", f"## {title}", *(items or ["None."])]
        lines += ["", "## Reviewer reports", *(f"- {path}" for path in sorted(self.review_dir().glob("*/*/final.txt")))]
        self.review_dir().mkdir(parents=True, exist_ok=True)
        (self.review_dir() / "report.md").write_text("\n".join(lines) + "\n")

    def post_findings(self, rv):
        """Deferred and disputed findings, posted once per review on the epic: they block nothing."""
        if rv.get("posted") == rv["start"]:
            return
        sections = self.report_sections(rv)
        parts = [f"## {title}\n" + "\n".join(sections[title])
                 for title in ("Deferred findings", "Disputed findings") if sections[title]]
        if not parts:
            return
        body = self.review_dir() / "findings.md"
        body.write_text(f"Findings from the epic review of #{self.a.parent} at `{rv['start'][:8]}` that block "
                        "nothing: deferred ones the epic does not own, and disputed ones the reviewers disagree "
                        "on, with both sides' evidence.\n\n" + "\n\n".join(parts) + "\n")
        result = subprocess.run(["gh", "issue", "comment", str(self.a.parent), "--body-file", str(body)],
                                capture_output=True, text=True)
        if result.returncode:
            return say(f"Warning: could not post the epic review's findings on #{self.a.parent} "
                       f"({result.stderr.strip()}); they are in {body}", YELLOW, err=True)
        self.update(rv, posted=rv["start"])
        say(f"Posted the epic review's deferred and disputed findings on #{self.a.parent}", DIM)
