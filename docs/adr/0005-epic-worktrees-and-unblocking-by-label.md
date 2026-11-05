# Each epic runs in its own worktree, and a person unblocks by removing a label

The runner used to work on whatever branch the operator's folder had checked out, fast-forward it there, and exit once
nothing was running. That held one epic per checkout and left the folder unusable during a run, and a ticket a person
had fixed sat until someone reran the runner. Now each epic has a worktree at `.scratch/epics/<parent>` on a branch named
with `--branch`, and a run stays alive while a ticket or the epic needs a person, continuing it when `needs-human` comes
off the issue.

## Considered Options

- **Switch the operator's folder to `--branch`**, or only check it is on the right one: small changes, but still one
  epic per checkout, and the first takes the folder away without being asked.
- **Default to the current branch**: git allows a branch in one place, so this needs either a second way of running or
  moving the operator's folder. `--branch` is required on a first run instead, and saved.
- **Keep the epic worktree until the pull request merges**: without `--merge` the run has exited by then, and while the
  worktree exists the operator cannot check the branch out to test it. It goes when the run succeeds; every commit stays
  on the branch.
- **Unblock on a new comment or commit**: fires too early, on the first of three commits or a half-finished thought.
- **A local `--unblock` command**: needs the operator at the machine, and a way to reach the running process. The label
  also works from a phone and already shows the ticket's state on GitHub.
- **A time limit on waiting**: an unblock after the limit would be missed, which is the problem being solved.

## Consequences

- A missing label means unblocked only once the run has seen that label on GitHub, or a failed update would look like
  a person's decision. An issue that cannot be read counts as still blocked.
- `--resume` follows the label too, so restarting after Ctrl-C cannot restart a ticket nobody looked at. Reaching past it
  takes naming the ticket (`--resume --only <n>`) or `--epic-only --force`.
- Epic-level fixes are committed in the epic worktree, not the operator's folder. The verify worktree is per epic, at
  `.scratch/worktrees/verify-<parent>`, and the run holds a shared lock on the old per-checkout lock so an older version
  of the runner and this one keep out of each other's checkout.
- Only the epic worktree's instruction files are watched for agent edits. In the operator's folder only link files, and
  an uncommitted config, are, since that folder may now change branch at any time.
