# Cross-examined epic review before verify, fixed through fix tickets

The epic review runs before verify, by two different models (deepseek and glm by default) that first review
independently and then cross-examine each other's findings with evidence; only a finding both stand behind
can block. Confirmed blocking findings become fix tickets, filed as GitHub sub-issues by the runner and run
through the ordinary ticket pipeline, followed by one follow-up review of only the fix diff. Verify runs last,
once, on code the reviewers already accepted; a verify repair gets its own repair review so every shipped
commit was read by a reviewer.

## Considered Options

- **Verify first, review second** (the earlier order): the reviewers saw runtime evidence, but every review
  fix invalidated verify, so the most expensive stage ran twice whenever the review found anything.
- **One Claude reviewer**: well grounded but the main token cost of a run; two cheaper models checking each
  other with cited evidence filter hallucinated findings instead.
- **One fix agent for all findings**: cheaper per run, but serial and without the ticket pipeline's
  worktrees, ticket review, gate and recovery.
- **Unlimited review and fix rounds**: each full re-review can surface new, unrelated findings; one
  follow-up round on the fix diff bounds the loop, and anything left needs a person.

## Consequences

- The epic reviewers no longer see the verify report; runtime problems are verify's to find.
- A SHIP verdict covers a later head only through a passing repair review of exactly the verify repair.
- Every sub-ticket closes when the epic review reaches SHIP; the epic itself stays open.
