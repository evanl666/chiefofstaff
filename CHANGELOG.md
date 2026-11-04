# Changelog

## 0.1.12 (2026-11-03)

From a day of real use on another machine.

- **Progress at a glance**: each running card in the inbox shows the step its worker is in now
  (and for how long) and what it said last.
  - Ask the Router how work is going: it reads each worker's activity (new `read_session`) and
    answers at once, instead of asking busy workers and waiting, or calling a busy worker
    stopped.
  - A message to a worker that is in a long step (a query, a job) gets a note at once: it reads
    the message when that step ends, and what it is doing meanwhile.
  - The Router's follow-ups reach a busy worker within its turn.
- **Message one worker directly**: type `@` in the inbox or Chat and pick a worker. Chat shows
  it as `→ worker`; the Router hears of it with its next message.
- **Your other Copilot sessions**: the Router knows the ones open in a terminal and can say what
  they are doing. It says when it cannot write to one (open there), and asks you on a card before
  continuing one here.
- **One message per result**: a worker's report is the message, whole (a long explanation, a
  table): the Router no longer repeats it in an update, nor answers you twice (in two languages).
  Its remarks after events fold into its steps. Reports are no longer cut at 4,000 characters,
  and when you ask for an explanation the worker puts it in its report, not only in a file.
- **Open files from reports**: a path to a file in a worker's folders opens it in the app;
  Markdown shows formatted.
- **Your profile** arrives as a card in the inbox, with its text, to correct and use; it is no
  longer only in Settings.
- **PR review comments**: a worker on a PR (one it opened, follows or was given) drafts replies
  to its review comments, in any repository; each card shows the comment it answers.
- **Workers as fast as your terminal**: in the sandbox, Gradle could not run at all (its
  daemon talks over localhost), tests that start a server failed, and npm and similar tools
  could not write their caches, so workers got stuck or moved `HOME` and downloaded everything
  again. Now (measured: a worker is as fast as the same task in a terminal):
  - workers may use localhost (not workers that read Slack); your own Gradle daemons' and
    Jupyter servers' tokens stay unreadable to them;
  - Gradle builds use your dependency cache read-only, with the worker's own daemon;
  - npm's package store and Yarn's archives are shared (both are checked against the lockfile
    before use); caches whose contents would run as they are (Gradle and Maven jars,
    pre-commit hooks, npx's installs) are not;
  - before a worktree is made, only the default branch is fetched, once for workers that
    start together.
- **No unasked pushes**: workers commit on their branch and leave pushing and the PR to you;
  analyses and research need no branch.
- Repeated instructions ("run a small smoke job first") are learned like other feedback.
- A worker that researched a subject is preferred for acting on it next.
- The Watchdog leaves a worker that makes progress alone, and its nudges say why it stepped in.
- Fixes:
  - A Copilot plugin hook that fails in the sandbox no longer marks the worker failed or fails
    your message: the session goes on, with one note. *Also writable in the sandbox* (Settings)
    lets such a plugin write its folder (never keys, Copilot's settings or plugin code).
  - A session continued here that was started in your home folder no longer gets your home
    folder to write in.
  - The thinking effort chosen in Settings applies to Copilot's default model too (it was
    dropped until the default was known); Copilot's *Auto*, which takes none, is said so.
  - Cancelling a stopped task keeps it cancelled even when the worker's report was already on its
    way; news about a task in review or accepted no longer leaves it stuck "in progress".
  - With many sessions (about 25 and up) the Router could no longer read its own list of them;
    the list now stays compact.
  - A PR merged after later reports that did not link it again still settles its task.

## 0.1.11 (2026-11-03)

Fixes from a second bug bash.

- Starting workers, stopping and reconnecting:
  - A task cancelled while its worker was starting stays cancelled; nothing is sent to it.
  - A worker whose start was interrupted (stopped, reconnected, shut down) waits in the queue
    and starts again instead of failing.
  - Reconnecting, or a worker failing, frees its slot, so queued workers start.
  - A cancel notice or PR note no longer starts a finished worker after a restart; it goes
    with your next message.
  - A session that was started but never given a message (Copilot does not keep those over a
    restart) is started again instead of failing.
  - A session opened while Copilot reconnects is not left on the old connection, where every
    message to it failed.
- Slack:
  - If Copilot lost the Slack session, a new one is started instead of failing at startup.
  - Slack messages and threads only reach sessions allowed to read Slack.
  - A burst of more messages than one check reads is picked up over the next checks, not
    skipped.
  - Draft cards show where a reply goes (DM, channel, thread) from Slack's own ids, and the
    latest message there as read from Slack, so you can check the session picked the right
    conversation.
- A PR merged before Chief of Staff first checked it (right after *Create PR*, say) now marks
  its task done; it was silently dropped. A PR followed again after it merged is just
  unfollowed.
- A worker that adds a project with `add_repo` while sandboxed can commit there from its next
  turn, and goes on with the task by itself (unless you stopped it).
- Closing a worker keeps its worktree if it holds ignored files you may need (an edited `.env`,
  reports); a worktree that only holds dependencies is still removed.
- The sandbox also keeps workers from changing your checkout's `HEAD` and index.
- Only *Send* publishes a draft.
- After an edit, the draft card shows the text that was sent and how it went.
- A code folder that later became a git repository no longer resets other settings.
- UI:
  - Enter while choosing characters in an input method (Chinese, Japanese) no longer sends.
  - Switching sessions quickly no longer shows another session's messages.

## 0.1.10 (2026-11-03)

Fixes from a bug bash.

- A message straight to a worker no longer reopens its finished task (a question left it
  "running" forever); new work it reports still comes back for review. Tasks left running this
  way are put back to review at startup.
- Acceptance criteria: a command matches the evidence by whole words only (a later `ls` no longer
  "verifies" anything); results reported on an earlier task of the worker are kept; a failure
  later in a `&&` chain no longer counts against its first part.
- Evidence goes out of date when a new, uncommitted file is edited, and opening a PR keeps it
  marked out of date.
- A decision change that cannot reach a closed worker says so instead of leaving its task
  running.
- A mistyped decision id no longer leaves a half-made worker behind.
- Content from Slack and PRs stays out of decision cards and of what the learning worker reads.
- A learning worker that stopped without proposing anything no longer blocks *Learn now*.
- Exiting one of your other sessions in its terminal unlocks its message box right away.

## 0.1.9 (2026-11-03)

- **Sessions learn from your feedback**: your decisions, review comments, follow-ups, retry and
  cancel notes and draft edits become lessons ("Give every migration a rollback"). You keep,
  reword or drop each on a card; only kept ones reach your sessions. Settings lists them and
  shows how often you now take recommendations, send drafts as written and accept results.
- A worker about to ask something you decided before is told your earlier decision first;
  the card shows it too.
- Usage counts what workers used while open in a terminal, including before Chief of Staff was
  running.
- A worker working in a terminal shows under Running. An adopted session's placeholder task no
  longer shows as finished work.
- The inbox's reply strip no longer shows a stray "null".

## 0.1.8 (2026-11-03)

- **Acceptance criteria**: tasks get *done when* checks, and the worker reports each one with
  evidence. The review card marks each as verified (a command it was seen running backs it),
  claimed, not met or not checked, with one verdict for the task. A test that failed and then
  passed shows as *✗→✓*.
- **Evidence is about one version of the code**: the card shows the commit it was checked at,
  and says *Out of date* once the code or a decision it relied on changes.
- **Decision cards**: a worker asks for a decision with why it comes up, the options and their
  effects, and its recommendation. Workers that hit the same question share one card.
- **Change a decision and the work follows**: tell the Router; every task that relied on it is
  told, finished results reopen, and a card shows the impact until each worker confirms.
- A task can wait for another's accepted result instead of its reported one.

## 0.1.7 (2026-11-03)

- Timelines are easier to read: what a session says on the way to its next steps is folded into
  the line of steps, so its answers and reports stand out.
- **Message one of your other Copilot sessions to continue it here.** It becomes a worker with
  its conversation so far and keeps working in its own folder.
- A session open in a terminal says so where the message box would be, instead of a box you
  cannot type in.
- What a worker did in a terminal shows in its timeline, also when Chief of Staff was started
  later or not running then. Long session logs show their latest messages.
- A message straight to a worker whose task was done brings its next result back for review.

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
