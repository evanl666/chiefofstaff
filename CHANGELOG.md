# Changelog

## 0.1.6 (2026-11-02)

- **Review a task's changes**: click *+N −M in K files* on a review card for its diff, comment on
  lines, and send the comments to the worker as one follow-up.
- **Create PR** on a review card pushes the worker's branch and opens a pull request with the
  task's report. The worker follows it, and the task is marked done when it is merged.
- New worktrees get the gitignored files they need: `.env` and `.env.*` by default, or what a
  `.worktreeinclude` file lists. Workers are told to install dependencies in a fresh worktree.
- A closed worker's changes still show in its receipts after its worktree is removed.

## 0.1.5 (2026-11-02)

- Opening one of your other Copilot sessions works again, and shows its latest conversation,
  read-only, current while it works in a terminal.
- A worker you open in a terminal is mirrored read-only from Copilot's own log: its messages
  and steps, and whether it is working. The Watchdog and idle closing leave it alone.
- The demo includes a session open in a terminal.

## 0.1.4 (2026-11-02)

- **One code folder** (like `~/workspace`) instead of listing projects: every repository in it
  is a project, and a repository a task needs that is not there is cloned into it once.
- Each worker's own folder is in `.chief/` inside the code folder, with a git worktree of each
  repository it changes. When a worker closes, its clean worktrees are removed (the branches
  stay); reopening it brings them back.
- A repository already on this machine is reused instead of cloned again.
- Settings: *Code folder* at the top; *Other project folders* and *Task folders* under
  Advanced.

## 0.1.3 (2026-11-02)

- Starting Chief of Staff while it is already running opens the running one, instead of an
  error.
- Ctrl+C stops it at once, without a traceback, even with the page open.

## 0.1.2 (2026-11-02)

- Sessions open in a terminal get their own mark, a blue ring, instead of the green of finished
  work. Every sidebar dot says what it means on hover.
- The *Add a project folder* card has *Not now*, for working without projects.

## 0.1.1 (2026-11-02)

- *Other Copilot sessions* marks the ones open in a terminal right now and lists them first.
- Handing a session over to the Router brings its conversation along, and is refused while it
  is open in a terminal. Chief of Staff never resumes a session another Copilot window has
  open.
- Sessions handed over before tasks existed no longer show as running.

## 0.1.0 (2026-11-02)

First public release.

- **Router** that splits requests into tasks, routes follow-ups to the worker that already
  knows the subject, and asks you when it is unsure which project a task belongs to.
- **Tasks and runs**: dependencies, reports, retries, cancellation and usage per task.
- **Inbox**: what needs you, what is running, and results ready to review with **receipts**
  (observed test runs, changes, commits and PR checks next to what the worker reported).
- **Focus Shield**: notifications wait while you focus; one summary afterwards.
- **Delegate by reaction**: react 🤖 to any Slack message to turn it into a task, answered
  in its thread when done.
- **Slack and GitHub**: replies and PR reviews drafted in your voice, sent only after you
  approve; followed threads and PRs go straight to their worker.
- **Watchdog** that notices stalled or looping workers and nudges them.
- **Sandboxed workers** (Copilot's OS sandbox): writes only to their own folders, no access to
  your keys, GitHub credentials through a proxy.
- **Reliable delivery**: interrupted sends and drafts are checked before anything is resent.
- **Demo mode** (`chiefofstaff --demo`): the whole product without any account.
