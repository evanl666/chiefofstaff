# Guide

How Chief of Staff works from your side: what you see, what it does on its own, and what it
asks you. For how it is built, see [ARCHITECTURE.md](ARCHITECTURE.md). For what keeps agents in
bounds, see [SECURITY.md](../SECURITY.md).

- [Home](#home)
- [Giving work](#giving-work)
- [Send to Chief](#send-to-chief)
- [Tasks and workers](#tasks-and-workers)
- [Where workers work](#where-workers-work)
- [Receipts](#receipts)
- [Reviewing changes](#reviewing-changes)
- [When a session needs you](#when-a-session-needs-you)
- [Decisions](#decisions)
- [How you work: learned, and kept up to date](#how-you-work-learned-and-kept-up-to-date)
- [Checks learned from mistakes](#checks-learned-from-mistakes)
- [Focus](#focus)
- [Slack](#slack)
- [GitHub reviews](#github-reviews)
- [Following threads](#following-threads)
- [Watchdog](#watchdog)
- [What runs without asking](#what-runs-without-asking)
- [Models and usage](#models-and-usage)
- [Daily summary and your profile](#daily-summary-and-your-profile)
- [Settings and environment variables](#settings-and-environment-variables)
- [Data](#data)

## Home

Home is the one screen you work from. On the left is your conversation with the Router: what
you asked, its short answers, and one line for each thing that happened on the way (*Started
“Fix the flaky login test”*, *Fix the flaky login test finished · View*; a worker that could not
go on says why, *Slack couldn't go on: …*). **View** brings up the
task's card on the right instead of repeating its report in the chat; lines from Slack, the
Watchdog and other sessions open in place, as does a report's line (to read all of it).

On the right, everything has one place:

| Section | What is there |
| --- | --- |
| **Needs you** | Questions and forms from workers, sign-ins, decisions, a folder to pick, Slack replies, PR reviews and replies to approve, PRs of yours ready to merge (*Merge* merges it as shown; anything pushed since stops it: the card says so and goes, and the PR comes back once its new commits are ready), sends a restart interrupted, failed or blocked tasks. A Slack reply someone answered before you sent it is not sent: its card shows what they wrote, marked new, and its one action is *Send anyway* (held again only if someone writes after that); a reply you emptied is not sent either. A draft its worker withdrew or replaced leaves, and its line in the chat says so (after a reload too). A worker's (or the Slack session's) question stays here until you answer or dismiss it, also when its task was cancelled or finished meanwhile: answering means you want the work, and that task goes on with your answer. Each of the Slack session's questions has a card of its own (a new one does not replace the last), and your answer reaches it with the question it answers. A note of the Router's that needs you (*The Router*, marked *Urgent* when it cannot wait) stays here until you say *Got it* or *Open* it (it shows in the chat), or write to the Router after it. Each card has one main action (*Send*, *Answer*, *Accept*…); the rest is behind **⋯**. A card leaves as soon as you act and a short note confirms it; if that did not work, it comes back with the reason. While the page is open, a short note at the bottom also tells you what comes for you (click it to go there): what needs you, and away from Home news too; not what the session you are looking at says itself, and while you [focus](#focus) only what is urgent. |
| **Tasks** | Task by task, in four areas after *Needs you*, then *Artifacts*. Every area folds (click its title; its count stays in sight, and it stays as you left it; *Needs you* opens again by itself when something new comes). Every task is one row, the same in every area: its title (click it to open the row into its whole card; **▴** or Esc folds it back), where it stands and why, its latest progress (the step it is in and for how long, *Waiting for background process* when its turn is over and a command it left running goes on, or its latest report), its worker as a small link (its current name, or *Worker chat*; an icon on a narrow panel;
opens its chat at that task), what it made, and the one action that matters now. A task is finished only once you accepted it and nothing is left to run or monitor. *Needs you*: every finished result (*Awaiting acceptance*: a report that changed no code too; nothing accepts it for you, not time, its worker going on to other work, nor its PR being merged), with *Accept*, *View report*, *Follow up* and *Request changes* (say what to change: its worker is told you did not accept it, and its next report is a new result to accept); questions (why it asks, its choices as buttons, what it does next), tasks stopped, failed or blocked, and news on finished work (*Update*). *Running*: the tasks being worked on now (a worker does one at a time; a result you asked more about shows here, *Following up*, while its worker is at it). *Monitoring*: tasks nobody has to act on now that something still has to come for: a review, CI or the merge of a PR it made (also once you accepted it), a reply on a thread it follows, a time it checks back (`check_back`, for a long run), a run it started, a slot (*Queued*), its turn (*Up next*) or its worker's return (*Paused*). A row says what it waits for and on what (*Waiting for approval · app#594*), when it was last checked and when next (a run Chief of Staff does not read itself: when its worker said where it stands, *reported running 3h ago*), and, hover it, what wakes it; long quiet it says so (*No updates for 26h*) and stays here: it is never taken for finished. A run nothing has told of for a day (its worker is not at it and set no check-back) is *Status unknown · reported running 2d ago*: **⋯** → *Mark ended* or *Stop tracking*, and the task leaves *Monitoring* once none of its runs is followed. A GitHub Actions run is read on GitHub every few minutes until it ends. A PR of yours to fix (*CI failing*, *changes requested*, *merge conflicts*) is under *Needs you*; one ready to merge is offered on a *Merge* card there. *Overdue*: no activity for *Stalled after* hours (Settings → General, 4 by default) and nothing to wait on: its worker is asked once where it stands, *Nudge* asks again; no answer within the hour brings it to *Needs you*. *Recently finished*: accepted tasks with nothing left to monitor (a PR you reviewed for someone else ends here once your review is in: news on it comes back as an *Update*), and tasks cancelled or set aside, which say so, never *Done*; the newest first, 30 at a time (*Show more*), for three days. *Monitoring* and *Recently finished* scroll on their own and do not move while the mouse is over them. *Artifacts* (last, so it never pushes your tasks down): the PRs, documents and runs your tasks delivered, by their real names, each PR with what the task did to it (*Created*, *Modified*, *Reviewed*) and where it stands now as GitHub has it (*draft*, *in review*, *changes requested*, *CI failing*, *awaiting approval*, *ready to merge*, *merged*…; a document or run as its worker last said); a PR is listed for the task that made, changed or reviewed it, never for one that only mentions it. A task's deliverables are listed together, its PRs and documents first; three runs or more are one line (*9 runs · 3 running · 1 failed*) with the latest and the failed ones under it, the rest behind *+N*. Files, mockups, screenshots, CSVs, logs, links it only mentions and a document site's home page are not listed: they stay on its card, in its report and files. **✕** takes one off the list; *Show … taken off* brings it back. A PR a task follows that cannot be read says why on its row: no access, not found, GitHub's rate limit or GitHub not answering (with *Retry*); *Stop following* there stops it for that task only. Hover anything for more (its id, worker, credits): the tip goes as soon as the list changes. *Copy report* (markdown for a PR description), *Create PR* (for commits on a branch of its own, a *chief/…* one or one you named, that no PR is from yet), *Rename…* and, when its latest result is about something else, *Move the latest result to a new task…* (with *Undo split* for ten minutes) are under **⋯**. Every count counts tasks. |

When a finished task's worker drafted something for you (a Slack reply, a PR review, a reply on
your own PR), the draft is the one card: the task's own card is in it (*Result of …*: what it
came to, its receipts and news, *Accept* or *Got it*, and **⋯** with *Create PR*), and sending
it accepts the task.

Click a task's title (or *Open worker*) to see its worker's full timeline at that task's report
(the worker may have gone on to other work since), every step, report and message, and to talk
to it directly; **← Home** goes back. *Stop*, next to *Thinking…* at
the end of a timeline (*Waiting for background process* when its turn is over but a command it
left running goes on), interrupts what a session is doing (and a command it left running in
the background): the task its worker was on waits under *Needs you* as *Stopped* until you say
*Go on* (or *Go on with a note…*), tell it in its chat, or cancel it, and what it was sent
during that turn goes out again with its next message. Ask the Router to stop ("stop all my
work") and it does the same (*Stopped by the Router*, with *Go on*): it cancels only when you
say cancel. Cancelling a stopped task lets the worker go on with its other tasks at once (also
after a restart); its replies and reviews still waiting for you go with it. Cancelling a task
that had finished already (its result reported) stops nothing: it is done with its result, and
the Router says it had finished. Under your latest messages to a session its chat says where
they stand: *Waiting to be sent*, *Sent*, *Received* (its conversation shows it), *Handled* (the
turn it was in ended); one its conversation does not show within 20 seconds, and that Copilot
does not hold for it either, is sent again (*Not received yet — sending again*), and after two
tries it says *Not delivered* and tells you, so a message is never lost without a word. *Go on*
and replies show the same on the task's row. When you cancel a task, the Router hears of it with its next message, and it
does not start that work again (nor a new Router after a handover) unless you ask. What you
write in a worker's chat answers the task that asked you (or the question or decision card it
waits on), and that task goes on at once; about finished work, its answer is an
*Update* on the task, which keeps its result, unless it changed code for you, or redid or
corrected the work: then that is its new result (to review again when you had not accepted it),
and the one before is under *Earlier reports* on its card. A message there that asks for something else ("New, separate task: add mod()
on a new branch", another PR or question) is told apart by the worker that reads it, not by
its words: it reports that work as a task of its own, with your message and what it ran for
it, and the task it was on keeps its result (or, still at work, goes on).
More asked of a reported result (*Follow up…*, or the Router continuing it)
comes back as the whole result, updated: its worker is told that its report replaces the
result, and one that reports only what it added is asked once for the whole. Written to it from Home (@ in the Router chat), its answer shows there too
(once: not when its report is there already). A worker's **⋯** (top right) has its
model, its permission checks, its folder and *Close worker*. On a narrow window Home shows
*Chat*, *Needs you* and *Tasks* one at a time, and narrower still (a phone) the sidebar folds
away behind **☰** at the top left (with what needs you counted on it; Esc or a pick closes it).
The sidebar has Home (its count is what needs
you, the only count anywhere), your workers, running first, then *Closed* and *Other Copilot
sessions*; Settings and Usage are the icons at the bottom.

A file path in a report or message opens the file when it is in a worker's folders (a
walkthrough, results, a log): Markdown shows formatted. Other paths are copied.

The dot next to each worker says where it stands, in the same terms as Home (hover it for the
words, or click *?* beside *Workers*): amber, one of its tasks needs you; pulsing blue, at work
on one now; a blue ring, its tasks wait on something (a marker in words beside it: *queued*,
*up next*, *checks back*, *overdue*…); grey, nothing open: those fold away under *Nothing
open*. A worker is listed by its work (the task it is on, else its newest), its first title in
the tooltip, also in its header and under *Closed*.

Until you set your code folder (see [Where workers work](#where-workers-work)), Home asks for
it; *Not now* hides that card, and Settings still has it.

## Giving work

Type on Home. Several things at once is fine: the Router splits them into
tasks and starts a worker for each, in parallel. When one task needs another's result, it
waits and then starts with that result. The Router replies with one line; results come back
as they finish: each worker's report comes to you whole, as a card, and the Router does not
repeat it. When you write to the Router, its reply is the one answer you get.

**To one worker:** type `@` on Home and pick a worker (a few letters of its name find it, words
with spaces too: `@fix ch`; Esc closes the list); the message goes straight to it (`→
worker` in the chat), and the Router hears of it with its next message. Or open the worker, or
use *Add a note* in its card's **⋯**.

**Images:** paste a screenshot, drop images on the chat, or use the paperclip (up to five, 5 MB
each); they show small above the message until you send it. Long messages you sent fold, with
*Show more*. A message holds up to 20,000 characters: near that the box counts them, and a
longer one is not sent but stays for you to shorten.

**Progress:** ask the Router ("how is it going?"). It reads what each worker is doing now and
said last, and answers at once instead of waiting on the workers. A message you send a worker in
a long step (a query, a build, a job) reaches it at once: Copilot moves the command it runs to
the background, where it goes on; a note says so, with what it is doing.

Instructions you give again and again ("run a small smoke job before the full one") are
[learned](#how-you-work-learned-and-kept-up-to-date), so you do not have to repeat them.

Try `chiefofstaff --demo` to see this without an account.

## Tasks and workers

A **worker** is a Copilot session with its own folder. A **task** is something you asked for,
with a goal, a status and a result. A worker can take several tasks over time, so later work
on the same subject reuses its context.

Task states: *queued* (waiting for a slot or for tasks it depends on), *running*, *waiting*
(it asked something or is blocked), *review* (done, ready for you), *done* (you accepted it),
*failed* (it could not start, or its worker found it cannot be done and says why; retry it
with an optional note) and *cancelled*. When the Router has another worker do a failed task
instead, the failed one is marked replaced by the new one, so no *Retry* is left beside it.

A task that needs another's result starts once that one reports done. When it must build on
work you have checked (an API others code against), the Router makes it wait until you
*Accept* the other one instead; its card says *waiting for your review of …*.

**Workers that work together.** Some tasks run at the same time and need each other while they
work: two sides of one change (an API and its client), a value one of them finds that the other
uses, a schema or file format they share. The Router starts them together and says they work
together; each is told about the others. They pass what the other needs straight to it
(`tell_session`, or `ask_session` for a question), in their exact words and at once, without a
Router turn in between. A worker waiting for another one's message waits quietly (its card
says *waiting for “…”*) and goes on when it arrives; when the other one finishes, its result
reaches the ones still at work. The Router keeps the picture without being in the way: a copy
of each message rides along with its next turn, and Home shows it in a line (*↔ “Token Scout”
told “Token Writer”: …*), so "what did the scout pass on?" is answered at once. What workers
tell each other is information, never new work: when one should change course (scope, another
approach, overlapping work), it asks the Router, which decides or asks you. Independent tasks
started together are not linked.

**Routing to the right worker** matters most. Your message reaches the Router together with
the workers whose record matches it (title, task, files, branches, reports; keyword search,
Chinese included). For work on a subject a worker already knows, the Router either:

- **continues** that worker's task (a correction, more information, a follow-up question; a
  finished task reopens),
- gives the same worker a **new task** with its own result to review, or
- starts a **new worker that builds on earlier results** (`context_from`): for an independent
  change with its own branch and PR, or work that must run in parallel. It gets the earlier
  tasks' results, folders and files, not their whole conversations.

`spawn_worker` refuses once when related workers exist, so the Router decides explicitly; so
does a new task for a worker given one in the last half hour ("oh wait, one more thing" is
more of that task), unless the Router says why it is another. The timeline shows *New task …
for …*, *Continued in …* or *Started …* (or *New task …*) *· new because …*.

Workers close themselves when their PR is merged or closed. Finished workers that follow
nothing close after *Close idle after* hours (default 24), unless something of theirs is still
under *Needs you* (a result to review, news, a question, a failed task, a card): those stay in
the sidebar and the @ list until it is settled. Closing is reversible: talk to a
closed worker, or open it and press *Reopen*, to continue with its conversation and checkout.
Closing a worker at work stops it and cancels its open tasks, and the Router hears of it;
*Reopen* (or writing to it) brings back the ones it had started, where they were: stopped,
waiting for your *Go on* or *Cancel* (or still waiting for your answer).

At most *Parallel workers* run at once (up to 1000); the rest queue.

**Your computer stays responsive.** A Copilot session is light (an open, idle one takes about
half a megabyte; one that has worked about ten, plus the MCP servers it starts for itself).
What weighs is what workers run: test suites, builds, browsers. So:

- While the computer is low on memory (less than 10% free, as macOS's memory pressure or
  Linux's available memory says), new work waits; its card says *Waiting for memory: 6% free
  on this computer*, and it starts once memory frees up. Work already running is never stopped.
- A worker that has been quiet for ten minutes lets go of its Copilot session, which ends the
  MCP servers it started; its next message (a follow-up, news on its PR, another worker's)
  picks it up again where it was. Not one that is in a turn, waits in one for you, has a
  command still running (a server it started), or is open in a terminal.

How many workers to run at once depends on what they run: four builds of a large project need
more than four searches.

### Your other Copilot sessions

*Other Copilot sessions* in the sidebar lists the sessions Chief of Staff does not manage, the
ones you started in a terminal. Those open in a terminal right now come first, marked *open*.
Opening one shows its latest conversation, and keeps it current while it works. Sessions a
program made through Copilot's SDK (a script's, one per item of a batch, say) are not yours to
continue and are not listed, unless you continued one in a terminal. Nor are one-shot
`copilot -p` sessions (one prompt, then done) that a script made in a batch (ten or more that
start with the same words, each within two minutes of the one before, or any named
`batch: …` with `copilot -p --name`, even while it runs) or that a worker made while it ran a
command, for two hours at most: in its own folder, or where the command ran or in a temporary
folder (`/tmp`, `$TMPDIR`) when the command runs `copilot -p` or the sessions are a batch it
started (five or more alike). Nothing in a session's files tells `copilot -p` from a question
you asked in a terminal and left, so a few alike (from five to nine) stay listed as yours:
`chiefofstaff tidy-sessions` names them. A single `copilot -p` you run yourself is listed, as is
every session you typed more than one message in, resumed, or have open (unless it is named
`batch: …`).
The Router does not see the others either. Workers name each call of a batch
`batch: <their task>` and give it a Copilot home of their own (`COPILOT_HOME` in their folder),
so its sessions do not reach yours at all.

**Moving them out of the way.** Sessions left from before still take room in
`~/.copilot/session-state` and in `copilot --resume`. `chiefofstaff tidy-sessions` shows those it
would move: how many, how large, a few of each, who made them, and what was learned from them,
then the few alike it leaves listed and does not move; it reads every log first (a line on how
many are left as it goes, and how many it could not read, if any) and changes nothing. `chiefofstaff tidy-sessions --apply`, run in your own terminal, moves them to
`~/.copilot/session-state-moved-by-chiefofstaff/<date>/` with a `manifest.json` saying what each
was: move a folder back into `session-state` to have that session again. Nothing is deleted, and
sessions open now or changed in the last hour stay where they are. What was read from them as
your words and not learned from yet is dropped; a style or habit learned from them before stays
until you undo or remove it in Settings → *You* (the command names them).

**Where each stands.** Each row's dot says it, read from the log Copilot keeps on this computer
(no model call; nothing stored or sent): amber when it asks you something or it is your turn, a
pulsing ring while it works, dashed when it has made no progress for 15 minutes, a ring when
idle, green once it finished; for a wait, how long replaces *open*. Hover a row for the question
or its last answer; the first line of its view says it too. At most five rows show at once,
those waiting for you first, the rest under *N more*. A row whose status cannot be told for
sure looks as before, and its tooltip says why. Turn it off with *Show where my other Copilot
sessions stand* in Settings → General.

**Send it a message to continue it here.** It becomes a worker, with its conversation so far in
its timeline, and keeps working in its own folder, inside your code folder or not: nothing is
moved or copied. *Copy resume command* gives `copilot --resume <id>` to continue it in a
terminal instead.

The Router knows these sessions too: ask it what one open in a terminal is doing, and it reads
it. It cannot type into it: give such a session instructions there, or exit it there. To
continue one that is not open anywhere, the Router asks you first: a *Continue a session here?*
card shows the session and what the Router wants to give it.

Chief of Staff never writes to a session another Copilot window has open: two programs writing
to one session would mix up its history. Such a session (or a worker you opened in a terminal)
is shown read-only, with *Working in a terminal* and its current step, from the log Copilot
writes; the message box says to continue there, or exit it there (`/exit`) to message it here.
The Watchdog and idle closing leave it alone meanwhile. What a worker did in a terminal shows in
its timeline, also when Chief of Staff was not running then.

In a timeline, what a session says on the way to its next steps ("Now the tests:") is folded
into the line of steps, which shows the latest; its answers, reports and questions stand out.

## Where workers work

You set one **code folder**: where your repositories live, like `~/workspace`.

```
~/workspace/                 your code folder
  aven/                      your repositories, one copy each; workers never touch them
  racer/
  .chief/                    Chief of Staff's task folders (hidden)
    fix-login-a1b2/          one folder per task
      aven/                  a git worktree of aven on branch chief/fix-login-a1b2
    research-flags-e5f6/     research: an empty folder for notes
```

- Every git repository in the code folder (up to two levels down) is a project: the Router
  matches a task to one by its name, git remote and README headline, and asks you on a card
  when it is not sure.
- A repository a task needs that is not there yet (a GitHub owner/name or a git URL) is cloned
  into the code folder once and stays for later tasks.
- Each worker works in its own task folder, with a **git worktree** of each repository it
  changes, on branch `chief/<task>`, started from the latest default branch of `origin`. Your
  checkouts are never touched, and parallel workers never share one: two agents in one folder
  would overwrite each other's files and switch each other's branches. A task that changes no
  code may read your own checkout, but nothing it does writes there (its edits are refused and,
  sandboxed, its commands cannot): to run tests or install, it takes a worktree of its own.
  Ask in so many words for the work to be done directly in your folder (no worktree), and that
  worker may change it.
- Follow-ups on a task go to the same worker and folder. When a worker closes, its worktrees
  are removed if they hold nothing uncommitted (the branches stay); reopening it brings them
  back. A worktree with ignored files you may want (an edited `.env`, a report) is kept, with
  its dependencies and build output deleted; those alone do not keep it (see
  [Disk space](#disk-space)).

A worktree is a fresh checkout: no dependencies installed, and none of the files git ignores.
Workers are told to install what the project needs (per its README or `AGENTS.md`). Gitignored
files a worktree needs, like `.env`, are copied in from your checkout: by default `.env` and
`.env.*` files; a `.worktreeinclude` file at the repository root (gitignore syntax, as with
Claude Code) lists others instead. Only files git ignores are copied, and never for workers that
read Slack.

Folders outside the code folder can be added under *Advanced → Other project folders*, and
*Task folders* puts the task folders somewhere else. A worker's header opens its folder in
Finder or VS Code.

Workers run like the Copilot sessions you start in a terminal: they load your plugins, your MCP
servers (yours, your company's, and a repository's own `.mcp.json`), skills and custom agents,
and their commands can use what yours can (your tools, keychain, Docker, other repositories). A
worker in a worktree of a repository you trust in Copilot is trusted too, so that repository's
MCP servers load there as well. If you use Copilot's own sandbox, workers follow your settings
for it.

With the **sandbox** on (Settings; always for workers that read Slack), workers' shell commands can write only to their
workspace, checkouts and the repositories' `.git` (not their hooks, config, or your checkout's
`HEAD` and index), never see your
GitHub token, and cannot read `~/.ssh`. On macOS, run `copilot sandbox ca create && copilot
sandbox ca trust` once so `gh` works inside the sandbox; Settings tells you when it is needed.
The sandbox needs macOS or Linux (`bwrap`): on Windows workers' commands run unsandboxed.

Workers can start servers on localhost (a test's server, Gradle's daemon); the token files of
your own Gradle daemons and Jupyter servers stay unreadable to them (their folders,
`~/.gradle/daemon` and Jupyter's `runtime`, are created empty if they do not exist yet). Workers that read Slack
cannot reach localhost.

So that installs and builds are as fast as in your terminal: npm's package store and Yarn's
archives are shared (checked against the lockfile before use; Copilot already shares pip's and
uv's), and Gradle builds read your dependency cache without writing it, with the worker's own
daemon. Caches whose contents would run as they are (Gradle and Maven jars, pre-commit hooks,
npx's installs) stay out of reach; add one under *Also writable in the sandbox* only if you accept that. Workers
that read Slack share no caches. Before a worker's worktree is made, only origin's default branch
is fetched, once for workers that start together.

Hooks of your Copilot plugins run in the sandbox too. One that writes elsewhere (a journal, say)
fails there; the worker carries on and its timeline notes it once. To let it write, add its
folder under *Also writable in the sandbox*.

Inside the sandbox, a worker cannot use your keychain, Docker, Kerberos tickets, tools installed
under `~/.local`, or read your home and other repositories, and `gh` needs Copilot's certificate
trusted. That is the price of the confinement; with the sandbox off they work as in your
terminal.

### Disk space

Workers' builds fill folders fast, so Chief of Staff cleans up after them and watches the disk:

- **When a worker closes**, what a build makes again is deleted from its worktrees: build
  output, dependencies and caches (`build/`, `node_modules/`, `.venv/`, `.gradle/`, `.next/`,
  `*.egg-info`, `__pycache__`…), and only what git ignores. The worktree itself stays only for
  your other files (logs, an edited `.env`, a report) or uncommitted changes; why it stays is
  recorded. Tracked files, new files, a folder with its own `.git` and anything a link leads out
  to are never deleted.
- **A worker idle for 3 days** (*After idle (days)*, in Storage) with nothing queued, running or
  waiting gets the same cleanup, but never while a program runs in its folder (a dev server),
  another open worker uses it, it is open in a terminal, or it is marked *Keep its files*.
- **Free space**: below 30 GB or 5% of the disk, whichever is more, you get one alert naming
  the biggest folders and what can be cleaned. Below 10 GB or 2%, new worker turns wait for
  space ("waiting for disk space") while running ones go on. Both clear once there is room again.
- **Storage** (the disk icon at the bottom of the sidebar, amber or red when the disk is low)
  shows free space, each worker's folder the largest first and what can be cleaned, why a kept
  worktree stays, recent cleanups, and anything it could not delete (files another user owns,
  say). From there: review a worktree's files, clean its build
  output, delete a closed worker's worktree (type "delete"; its branch stays), mark a worker
  *Keep its files*, and set what counts as build output or must never be cleaned.
- **uv's cache** is shown with its size, never deleted. Each `uvx chiefofstaff@latest` of a new
  version leaves an environment there; `uv cache prune` removes the ones nothing uses. Never run
  `uv cache clean` while Chief of Staff is running: it can delete the environment it runs from.
  `uv tool install chiefofstaff` once, then `uv tool upgrade chiefofstaff`, avoids the pile-up.

## Receipts

A worker's report is its claim. Receipts are what Chief of Staff observed:

- the shell commands it ran and their exit codes, recognized as tests, checks or builds (also
  tests run through a script of the project's, known by the test runner's summary, and runs a
  worker hands to one of Copilot's sub-agents);
- commits and changes in its checkouts (`git diff` against where its branch started), each
  task's own: from when it was taken up to its result, less what other work changed in between
  (a later task's, before a follow-up on it). In a folder of yours (not a worktree), a file is a
  task's when its edits wrote it, or it changed while one of its commands ran that names it or
  writes files it does not name (a glob, a variable, a script); what you (or a pull, or another
  task) changed there meanwhile, even in a file it only read, is not counted (*not counted: 2
  files*). A new file too large to
  count is *1 large new file*. A finished task is *Out of date* when its own files changed
  since its result (not when a later task changed others);
- the state and checks of pull requests it reported.

The review card shows them under *Observed* and flags disagreements, e.g. *Says tests pass, but
no test run was observed*, *The last test run failed (exit 1)*, *Checks are failing on
acme/app#7*, or *Says it made changes, but its checkouts have none* (each worker says with its
report whether it changed files). A test that failed first and then passed (a regression test
for a fix) shows as *✗→✓ tests*. *Copy report* puts goal, result and receipts into markdown.

Receipts show the latest: the card changes as soon as a test run, push or commit ends, also
after the report (work on its PR). A test run Copilot left running (a long suite, or one in the
background) shows *… tests running* and gets its result when it ends; a command that never ran
(refused, blocked) is no failed run; in `pytest && git push`, a rejected push is not failed
tests. A PR shows in a few words, as GitHub has it now: *app#7 · ✓ CI · approved*, *CI
running*, *✗ CI* (hover for the failing checks), *merged*. When the PR's checks passed after
the last local test run failed, the card says so (*✗ tests locally*, and *The last local test
run failed (exit 1), but the checks on … pass now*): CI has the newer word.

**Acceptance criteria.** For work that can be checked, the Router gives the task 1-4 *done when*
criteria: a test that passes, a command and its output, a request and its response (for a bug
fix, a regression test that fails before the fix and passes after). The worker reports each
one with its evidence, and the card marks it:

| Mark | Meaning |
| --- | --- |
| ✓ | **Verified**: the evidence names a command the worker was seen running, and it passed (a search that found nothing, `grep` exiting 1, backs "none remains") |
| ~ | **Claimed**: reported as met, but nothing observed backs it |
| ✗ | **Not met**, or reported met while the command behind it failed |
| – | **Not checked** |

Above them, one verdict: *✓ Verified*, *Partly verified · 2/3*, *Not verified* or *✗ Not met*.
The summary a worker writes is never counted as evidence.

**Evidence is about one version of the code.** The card shows the commit it was checked at
(*at 2dbecc5*, *+ edits* for uncommitted ones). When the code changes after that (more commits,
edits, another task in the same worker) or a decision it relied on changes, the card says *Out
of date* and why.

## Reviewing changes

Click the changes on a review card (*+12 −3 in 2 files*) to read the worker's diff: its commits,
uncommitted edits and new files, from where its branch started (also after the worker closed).
Click a line (or Tab to it and press Enter) to comment on it; *Send to the worker* sends all comments as one follow-up, each
with its file and line, and the task comes back for review when the worker is done.

*Create PR* pushes the worker's branch to `origin` and opens a pull request on GitHub with your
signed-in `gh`: titled with what its commits say (their subjects; the task's title when it has
none to read), and its report as the description, without what the report said about pushing or
a PR (moot once the PR is open), and what was checked. If a PR from that branch
is open already, that one is used. Uncommitted work is not pushed: follow up and ask the worker
to commit first. An open worker then follows the PR (reviews, comments and CI go to it); its
task stays under *Monitoring* until the PR is merged or closed, and waits for your *Accept* until
you give it (merging does not accept it).

## When a session needs you

Sessions ask instead of guessing. Each request is a card under *Needs you*, with a desktop
notification:

- **Decisions**: a choice that is yours (scope, a trade-off, a risky change such as a migration).
  The card says why it comes up, the options and what each means, and what the worker
  recommends; pick one or write your own. Workers that hit the same question wait on the same
  card, and one answer reaches all of them. Answering in the worker's chat answers its card
  too (a question back, "what does B change?", does not: the worker answers it and the card
  stays); a card whose task ended (done, failed, cancelled) leaves. See [Decisions](#decisions).
- **Questions**: a worker uses Copilot's `ask_user` for quick facts. Pick a choice or type an
  answer.
- **Forms and links**: an MCP tool asks for input, or for you to open a link.
- **Sign-ins**: an MCP server needs OAuth. Sign in in the browser tab; the card closes by
  itself. Copilot keeps the sign-in for every session (see [SECURITY.md](../SECURITY.md)).
- **A folder**: the Router is not sure where a task belongs.
- **Approvals**: Slack replies and PR reviews drafted in your voice. Edit and *Send*; or,
  under **⋯**, *Ask to redo…* (say what should change, it is needed: the same session drafts it
  again and a new card comes) or *Reject* with a reason the session learns from. A Slack draft
  shows where
  it goes (DM, channel or thread) and the latest message there, read from Slack itself, so you
  can check it answers the right conversation. When someone wrote there after the draft, *Send*
  does not send: the card shows what they wrote, and *Send anyway* sends it. A newer draft for
  the same conversation replaces the older one, and a session can withdraw a draft that turned
  out wrong. An empty reply is never sent. A question its worker asked about the draft (send
  it?) is answered by what you do with it, and its task goes on (or, finished, stays where it
  was). Edits you make to a finished task's draft reach its worker with its next message: it is
  not woken for them, and the task keeps its result. When someone asked for a review or reply in
  a Slack thread its worker follows, that worker is woken once you send it, to draft a reply
  there telling them. The Router knows what waits for you (each card's recipient, what it
  answers and its task, and the workers' questions) and hears what you did with each draft.

There are no per-tool settings: Chief of Staff uses whatever your Copilot has (MCP servers,
CLIs). For a new ability ("read our company docs"), the Router starts a setup worker that finds
what is available, asks you to sign in, checks that it works and records how in **environment
notes** for later sessions (editable in Settings).

## Decisions

Work depends on decisions: "v1 is email login only", "limit attempts per account". Chief of
Staff keeps them, each with the tasks that rely on it:

- your answers on decision cards;
- decisions you state to the Router ("SMS login is out for v1"), which it records and links to
  the tasks they concern.

**Change one and the work follows.** Tell the Router ("change of plan: SMS login is in after
all"). It records the new decision in place of the old one, and Chief of Staff:

- tells every worker whose task relied on the old one, right away (also mid-turn), what changed;
- reopens tasks that were already finished, and marks their checked results *Out of date*;
- shows a *Decision changed* card: what was decided before and now, the tasks it affects, how
  many checked results need redoing, and which workers confirmed what they change. Each worker
  confirms with `ack_decision`; once all have, the card goes away.

Work that does not rely on the decision carries on untouched.

When a worker is about to ask something you decided before, it is told first: it follows your
earlier decision, or asks anyway and the card shows *You decided before: …*.

## How you work: learned, and kept up to date

Chief of Staff keeps learning how you write and work, so its sessions get closer to doing it as
you would.

**How you write.** Every two hours (and when you press *Learn now*) it reads what you wrote
since: the Slack messages you sent, your PR reviews and review comments (on the account active
in `gh`; not what Chief of Staff posted for you, which it knows by what it posted; nothing is
added to what it posts, so what Chief of Staff on another computer of yours posts on the same
account is learned as your own writing: you approved it), your commit messages in your projects
(not Copilot's, nor those a worker
made), what you asked your own Copilot sessions, and what you told the Router. Only your own
words; others' messages are never kept. A short-lived worker, which can do nothing but propose,
then describes how you write in each place: answers and reports to you, Slack DMs, Slack
channels, PR reviews, commit messages (language, length, tone, greetings, formatting, a short
quote or two). It never sets the language: answers are in the language of the message they
answer (write in English and you are answered in English, in Chinese and you are answered in
Chinese), and a Slack reply is in the language of the message it replies to, or in your words
when you said what to answer. What you write to one worker changes only that worker's answers:
the Router and new tasks' reports stay in the language you write to the Router in, and a task
for a request that came without you (a Slack message) reports in that request's language.
Each style applies at once to the sessions that write that way
for you (the Slack session and workers that read Slack get your Slack style; workers get your
commit and review style; the Router and workers answer you the way you talk to them), and the
Router chat says what was learned, with a link to Settings. Settings → *You* → *How you work* shows each, with its version; *Undo* goes back to the
previous one. Turn *Learn from my Slack messages, PR reviews, commits and Copilot sessions* off
to stop reading them. Of your Copilot sessions, only those you talked in are read (two messages
of yours or more): not a one-shot one, such as a `copilot -p` you ran or one question you asked
and left, which is written for a script or a single answer; nor sessions a program, a worker or a
script made (see [Your other Copilot sessions](#your-other-copilot-sessions)). What was read from
those and not learned from yet is dropped before learning; `chiefofstaff tidy-sessions` names
what was learned from a worker's or a script's sessions before that.

**How you want work done.** Your sessions also learn from what you do with their work:

- your decisions, especially when you go against what a worker recommended;
- review comments on a diff, follow-ups on a finished result, notes when you retry a task,
  reasons when you cancel one;
- the drafts you edit before sending (each line comment of a review you rewrite or leave out,
  and a verdict you change), and why you rejected one;
- the decisions you change;
- the instructions you give the Router and workers, so that guidance you repeat becomes a lesson.

Only your own choices and words count; what a session proposed is kept beside them as context.
Once a few new pieces of feedback came in (or guidance you keep giving your own sessions), the
learning worker turns them into habits, e.g. *Give every database migration a rollback
migration* or *Run a small smoke job before the full one*, each with where it applies
(everywhere, Slack replies, one project) and what it rests on. A **Learned from your work** card
shows them: keep, reword or untick each. Only what you keep goes into your sessions'
instructions; habits learned while the card waits join it.

Settings → *You* lists the habits in use (remove any, or add your own: they apply from your
sessions' next message) and shows how close the sessions
get to you: the share of recommendations you took, drafts you sent as written and results you
accepted as they were, over the last 30 days and the 30 before. Usage shows what a result you
accepted cost, by model.

## Checks learned from mistakes

When a worker's test run fails and a later run passes after it changed the code, Chief of Staff
keeps both versions (as git commits under `refs/chiefofstaff/`, without touching the worker's
checkout). A short-lived worker reads the failure and the fix and, when it shows a mistake that
can come back (a blocking call in async code, a forgotten migration), proposes a check: a shell
command that fails when the mistake is there, e.g. `! git grep -nE '^[[:space:]]+time[.]sleep'
-- 'src/*.py'`. Before you see it, the check runs on both versions: it must fail on the code with
the mistake and pass on the fix. A **Check learned from a mistake** card shows it with that
proof; *Use this check* turns it on.

From then on, when a worker in that repository says it is done with changes to files the check
applies to, the check runs on a clean copy of its work. If it fails, the worker is sent back
once to fix it (or to say why it does not apply); if it still fails, the review card flags it.
Settings → *You* lists the checks, how often each ran and caught something, and turns
any off. Checks run in a sandbox: no network, no writes, and nothing in your home folder but the
repository is readable. They need macOS (`sandbox-exec`) or `bwrap` on Linux, so none run on
Windows.

## Focus

**Focus** (at the top of Home) holds notifications for 30 minutes to 4 hours. The Router keeps
working: Slack messages that need a reply get drafts, code work goes to workers, noise is
ignored. Only what the Router marks urgent (an outage, someone blocked on you today) gets
through. The chat says *Focusing for 1h*; the top of Home shows until when. When focus ends
you get one summary: the messages and PR activity that came in, replies drafted (not those
replaced or withdrawn since), tasks finished, and how many things need you, counted as
*Needs you* counts them.

## Slack

Chief of Staff uses Slack's [official MCP server](https://docs.slack.dev/ai/slack-mcp-server/)
with your **user** token. A watcher looks for new DMs and mentions every minute without a model
and wakes the Router only when something new arrived.

1. Create a Slack app **from `slack-manifest.json`** (user scopes only; token rotation off).
   Your workspace may require admin approval; only internal or Marketplace apps may use MCP.
2. Enable MCP access for the app (`https://api.slack.com/apps/<APP_ID>/app-assistant`).
3. Install it and copy the **User OAuth Token** (`xoxp-…`) from *OAuth & Permissions*.
4. In **Settings**, paste the token, enable *Watch Slack*, and save. The token is stored in
   your OS keychain (or set `CHIEFOFSTAFF_SLACK_USER_TOKEN`).

The Router forwards messages that need a reply to the **Slack session**, which reads the
conversation and drafts a reply in your voice for you to approve. Messages that need work
outside Slack go to a worker with Slack access. Older messages are not replayed on first
enable.

### Delegate by reaction

React with 🤖 (`:robot_face:`) to any Slack message, in any channel, yours or someone else's,
to hand it to your chief of staff: "take care of this". The Router treats it as a request from
you: it starts a worker (or gives it to the one that knows the subject), and when the author is
waiting, the worker drafts a reply in that thread with the outcome for you to approve. Write a
note to yourself and react to it to delegate from your phone.

Change the emoji (or empty it to turn this off) in **Settings → Connections → Slack**. The watcher finds
reactions with Slack's `hasmy::robot_face:` search, among the 20 most recent messages you
reacted to; reactions from before you turned it on are ignored. The message itself is still
someone else's text: the Router checks with you before anything risky it asks for.

## GitHub reviews

Enable *Watch review requests* and list repositories (`owner/name`). Chief of Staff checks
`gh search prs --review-requested=@me` through your signed-in `gh`. The Router starts a worker
per request; its review is published only after you approve the draft, and only if the PR head
has not changed since.

You can also just ask: "review checkout-service PR #1529". A worker drafts reviews on a PR its task
names (by URL, or by the repository's name and number), one it opened, follows or was given, or
one GitHub asks you to review. A review carries a **verdict**: approve, request changes or
comment. Ask "review it and approve it if nothing blocks merging" and it is drafted as an
approval, a real GitHub approval once sent. A review also carries **comments on lines** of the
diff; lines outside the diff are refused when drafting, so what you approve can be published.

The card under *Needs you* says what the PR is (“Add greet” by @maya · +3 −1 in 1 file), the
verdict, the start of the summary and how many line comments there are. **Review…** opens the
review to check it: the PR and what it is about, the summary, and each line comment with the
lines of code it is about. Edit anything; *✕ Remove* leaves a comment out (*Undo* brings it
back); an empty summary posts the line comments alone. The verdict is beside the send button,
which says what it does (*Approve & send*, *Request changes & send*, *Send review*). Not like
this at all? *Ask to redo…* sends it back to the worker with what should change.

On a PR a worker is working on, it can also draft **replies to review comments**, in any
repository: each card shows the comment it answers, and the reply is posted in that thread once
you approve it. When the reply says the point is fixed or settled, the card has *Resolve the
conversation* ticked: sent, the conversation is resolved on GitHub too (untick it to leave it
open). A comment that is not on a line (a top-level comment, or the summary of someone's
review) has no thread to reply in: the answer is posted in the PR's conversation and starts
with @ and its author's login.

**Posted it yourself?** Copied a drafted reply or review and posted it on GitHub yourself
(edited a little, even)? Within a few minutes Chief of Staff finds it there: the card leaves as
sent, and the worker is told, so it does not ask you about it again. A worker also always hears
what became of its drafts (sent, edited, rejected), with its next message. What you post on a PR
yourself, and what its own worker pushes (any commit made in a worker's checkout, quiet or not,
a rebase onto a base others moved too: their commits on the base are not the PR's), is not news
to the workers following it; news on a PR goes to the task that made it (the task is
named in it; not a later task of that worker that only asks about it), and once handled, a
finished task goes back to where it was. A worker that only read or reviewed a PR someone else's
task made is not asked too, except for replies to its own review comments.

**Several GitHub accounts** (a personal one active in `gh`, your company's signed in as well):
Chief of Staff reaches each repository with the account that can see it, and remembers it per
organization. Sign in with each once (`gh auth login`); you need not switch. Workers are told the
same: they open PRs and push as that account (gh's active one, unless it cannot see the
repository), not as another one that happens to be signed in. When none can see a
repository, the worker tells you which accounts are signed in.

Workers commit on their branch but do not push unless the task asks for it: you open the PR with
*Create PR*. Analyses and research need no branch at all.

## Watches: what to look out for

Settings → Connections → *Watches* says what to look out for, in your words: "Slack DMs and
mentions of me", "comments on my Google Docs", "emails from customers". Optionally say with
what: "Glean search", "the Slack MCP server", your company's own search. Chief of Staff's
connector checks them on a schedule (every 5 minutes in work hours, every 30 after; both
settable, 0 turns after-hours checks off; weekends count as after hours: Settings says
*Weekend: every 30 min*, or *Not scanning now* and why when after-hours checks are off) with
whatever MCP servers, skills and CLIs your
Copilot has here, so it works without a Slack token wherever your company gives Copilot a
search. *Scan now* saves the watches as written and checks at once. You can also ask the
Router ("watch #deploys for payment failures"): it proposes the watch on a card under *Needs
you*, and *Submit* adds it (edit it first if you like); a watch you have already is not added
twice, and a card that a restart ended unanswered is told to the Router, which can propose it
again.

What needs you (a question, a request, a mention that expects your answer) goes to the Router,
like a Slack event. It has a worker look into it, read the conversation and draft the reply in
your voice. Where Chief of Staff can send it (Slack connected), you approve and it is sent;
otherwise approving copies it for you to paste. Bots, FYIs and what you already answered are
left alone (the connector reads a message's thread and leaves out what you answered there), and
nothing is handed over twice. A watch whose tool is missing here says so under *Watches*. The
connector only looks: it never posts, replies or reacts. It reads other people's messages, so
it runs sandboxed with network to GitHub only, whatever the worker sandbox setting.

Each watch is searched from where its last complete search ended (up to a week back), not only
the last day, so a slow day of searches does not skip anything: a search that could not cover
everything (a tool that returns only the newest results, a failed page) does not count as done,
and that time is searched again. Time that still cannot be searched after a few tries (or that
the tool cannot reach back to) is kept as a gap: Settings → *Watches* lists it, and you are told
("Could not check “#deploys” between 3:00 PM and 9:00 PM"); a gap that keeps growing is told
again once it doubles or passes an hour or a day, with all of it ("Still could not check…").

It also collects what you write there (your own messages only), so *How you work* learns your
voice even where Slack is reached through another tool.

## When you are away

Turn on Settings → Connections → *When you're away* and say how to reach you, for example
"DM me on Slack through the acmebot plugin". After that many minutes without keyboard or mouse,
what needs you (cards, questions from workers, a question the Router asked you in the chat,
tasks you stopped, failed tasks (of the last three days, as under *Needs you*)) is sent to you
that way, once: a
short gist of up to five items (*and 3 more*),
each with a code of its own (random, five letters, for that item only, valid for a week). Answer
where you got it with the code: "K3XQ7 send" sends a draft, "K3XQ7 2" picks option 2 of a
decision, "K3XQ7 use port 9090" answers a question, "K3XQ7 retry" retries a failed task,
"K3XQ7 go on" or "K3XQ7 cancel" does what *Go on* or *Cancel* does with a task you stopped. A code
works once, and only for what it was sent with. A question that comes up again (its task asked
something new) is sent again. What your answer to a draft means (send it, drop it, change it, or
not clear) is the messenger's reading of your words, in any language: "ship it", "发吧", "say
Friday instead"; one message can answer several codes ("K3XQ7 send, B4XJT no"). Each answer is
read once, and a code's latest answer is the one that counts. A send you hold back or make
conditional ("after lunch", "tmrw", "先别发", "once Bob confirms"), a question back ("send?")
and a joke are never sent, whatever the reading; nor is a draft that still has a blank to fill
("[yes / no]", "___"). Such an answer is asked again where you got it, saying why ("Not sent
yet: “after lunch” sounds like not now"); an answer to a code you already used says what it
did. *Send a test* tries the way you gave (a test that fails is said
as such in the chat, each time). Without a bot that can message you, say "send it to my own
Slack DM": with Slack connected in Settings, Chief of Staff sends it there through its own Slack
connection and reads your answers there; you see it without a phone notification.

The notifications are sent by a messenger of their own, not the connector: it is given only
Chief of Staff's message, in a file (plain text, Slack formatting and Block Kit to choose from),
and follows your worker sandbox setting, so a company bot's command-line tool works (with the
sandbox off, it runs as in your own sessions). It has no Slack tools of its own: for your own
Slack DM it asks Chief of Staff to send the message there (nothing else, to nobody else) and to
read what you wrote there since. An item counts as sent only once the messenger
says it went out; a failed send is tried again ten minutes later. The messenger also reads your
answers, the way it sent (the bot's DM, your own DM), every few minutes for the first hour
after a notification, then every half hour, while you are away and for a day after; your own
DM (and the threads of the notifications in it) is checked every few minutes all day, and read
when you wrote something new there. A reply with no code answers the only code that waits;
with several waiting, you are asked which. Written in Chinese, you
get the notifications in Chinese. The chat notes each one. When sending fails, the chat says so
once and Settings shows the latest attempt.

Each item in a notification says what it is (*Needs your input*, *Failed*, *Stopped*…), the
task it is about, what it needs in a sentence or two (whole sentences, not a heading such as
"Issue:") and where to answer it (*Open Needs you → Fix deployment compatibility issue*).

Settings → *When you're away* shows what the way you gave can do, as tried (by *Send a test*
and by every notification since), not as assumed: *Send* (a message goes out), *Delivery* (it
can be read back where it went), *Read replies* (your answers there are read) and *New
commands* (no, by design: only answers with a code act). When replies cannot be read it says
*Send only*: notifications then carry no codes (none are even made) and do not suggest
replying; each says where to go in Chief of Staff. When some notification threads could not be read (or there was more than
one page of messages), it says *Coverage limited* and how much, and those messages are read
again later: an answer there is not taken for read until it was.

## Send to Chief

Hand Chief of Staff work from wherever you are, without switching to Home: a note, and a link
or text you pass along. Turn it on at the end of Settings → Connections (*Send to Chief from the
command line*); it says once what it does and saves only when you press *Turn on*. It needs
macOS or Linux: on Windows a Unix socket cannot tell who connects, so it stays off. Then:

```
chiefofstaff send "fix the flaky login test in acme/api"
chiefofstaff send "review this" --url https://github.com/acme/api/pull/7 --title "Speed up exports"
pbpaste | chiefofstaff send "answer Maya with the outcome" --text-from -
```

What you send becomes one request for the Router, as a message on Home does: the note is your
request; the link, its title and the text (`--text-from FILE`, or `-` for what comes in on
standard input) are material it passes along, never instructions, and without a note the Router
takes the item as "take care of this". A pull request link is followed as a delegated Slack
message's thread is, a Slack message link names its thread, and an issue is named by its
reference. Anything risky it asks for, your note's requests too, is confirmed with you first: a
note sent from outside Chief of Staff is only as surely yours as your computer is. The work it
starts runs with your settings, as any task does. It waits for the Router to finish what it is
doing, and several sent at once come as one turn. Home shows one line per item in the chat
(*Sent from the command line: …*), which opens to what you sent; nothing else.

It reaches the running Chief of Staff on this computer only, through a socket in its data
directory that only your user can use (see [SECURITY.md](../SECURITY.md#send-to-chief)); pass
`--data-dir` when Chief of Staff runs with one. The command prints *Sent to Chief of Staff.* when
Chief of Staff took the item, and otherwise why not, with your note so nothing is lost. It
exits with:

| Code | Meaning |
| --- | --- |
| 0 | Sent, or taken already (the same note and link sent again within ten minutes is taken once) |
| 2 | Usage: nothing to send, a link that is not http or https, a note longer than 20,000 characters |
| 3 | Chief of Staff isn't running, Send to Chief is off, or something other than your Chief of Staff is at its socket: nothing was sent |
| 4 | More than 10 items in a minute or 300 in a day: not taken |
| 5 | Chief of Staff is reconnecting to Copilot: not taken, try again in a moment |
| 6 | *Delivery unconfirmed*: the request went out and no answer came, even after two more tries with the same item |

After *Delivery unconfirmed*, run the same command with the `--item` it prints: Chief of Staff
takes an item once, and answers a repeat with the same receipt. Nothing is queued: when Chief of
Staff is not running, the command says so at once. Text longer than an item holds (20,000
characters, or 64 KB) is cut, and the command says so.

Other tools can send with plain HTTP over the socket (`POST /v1/send`, JSON with `item`, 32
hexadecimal characters you make once and keep for retries, and `note`, `url`, `title`, `text`):

```
curl --unix-socket "$HOME/Library/Application Support/chiefofstaff/send/socket" \
  -H 'Content-Type: application/json' -d '{"item": "…", "note": "…"}' http://chief/v1/send
```

curl does not check who answers at that path; `chiefofstaff send` does, before it writes a byte.

### From a hotkey

Each of these calls the command by its full path (`command -v chiefofstaff` prints it), since
launchers do not read your shell's `PATH`:

- **Shortcuts (macOS):** a new shortcut with *Ask for Input* (text, "What should Chief of Staff
  do?"), then *Run Shell Script* with *Pass Input* set to *as arguments* and the script
  `/full/path/to/chiefofstaff send "$1"`. Shortcuts asks once to let it run scripts (Settings
  → Advanced → *Allow Running Scripts*). In the shortcut's details, *Add Keyboard Shortcut*.
- **Raycast:** a script command (save it in a script folder you added to Raycast):

  ```bash
  #!/bin/bash
  # @raycast.schemaVersion 1
  # @raycast.title Send to Chief
  # @raycast.mode compact
  # @raycast.argument1 { "type": "text", "placeholder": "What should Chief of Staff do?" }
  exec /full/path/to/chiefofstaff send "$1"
  ```

  Give it a hotkey in Raycast's settings; its last line of output is shown when it ends.
- **Alfred:** a workflow with a *Keyword* input (with an argument), connected to *Run Script*
  (`/bin/bash`, input *as argv*) running `/full/path/to/chiefofstaff send "$1"`, and a
  *Hotkey* trigger if you like; a *Post Notification* output shows what it printed.
- **Linux:** a custom keyboard shortcut (GNOME: Settings → Keyboard → Custom Shortcuts) running
  `sh -c 'note=$(zenity --entry --title="Send to Chief" --text="What should Chief of Staff do?") && notify-send "Chief of Staff" "$(/full/path/to/chiefofstaff send "$note" 2>&1)"'`.

**The front browser tab** (macOS): the same, with the tab's link and title read by AppleScript
and passed along. In a *Run Shell Script* (or the script above), before the command:

```bash
# Chrome:
url=$(osascript -e 'tell application "Google Chrome" to get URL of active tab of front window')
title=$(osascript -e 'tell application "Google Chrome" to get title of active tab of front window')
# Safari:
url=$(osascript -e 'tell application "Safari" to get URL of current tab of front window')
title=$(osascript -e 'tell application "Safari" to get name of current tab of front window')

/full/path/to/chiefofstaff send "$1" --url "$url" --title "$title"
```

To add text you copied first, pipe the clipboard in: `LC_ALL=en_US.UTF-8 pbpaste |
/full/path/to/chiefofstaff send "$1" --url "$url" --title "$title" --text-from -` (launchers run
scripts without a locale, and `pbpaste` then writes accents and curly quotes in Mac Roman, not
UTF-8). macOS asks once to let the launcher control the browser (Automation); no browser
extension is involved, and the browser only answers the launcher's question.

## Abilities: skills, plugins and MCP servers

Workers load everything your own Copilot sessions load: your plugins, skills, MCP servers and
agents, your company's, and a repository's own. Settings → Connections → *Abilities* adds more:
upload a skill (a `SKILL.md` with a name and a description; one with the name of a skill you
have is refused, not replaced), name folders of skills, list Copilot plugins to install
(`owner/repo`, `owner/repo:path`, `name@marketplace` or an https git URL; installed with
`copilot plugin install`, each with its status), and MCP servers as in Copilot's
`mcp-config.json`, checked as Copilot checks them (`tools` and `args` lists of strings, `env` and
`headers` objects of strings, `timeout` in whole milliseconds): Copilot never starts on a message
to a session with one wrong field, and says nothing. If a worker's message is not started on
within a minute and a half anyway, the worker and its task show that as a failure (not
*working* forever): fix the setting and *Retry*. A settings change, or *Stop* then *Go on*, sends
what it was not started on again on a new connection with the settings as they are then. They
run as they do in your own sessions: add only what you would install
for yourself.

## Images

Paste or drop screenshots into the chat (PNG, JPEG, GIF or WebP, up to 5 MB, five at a time).
The Router sees them, and a worker it gives them to gets them too; a message straight to a
worker (@) brings them along.

## Following threads

A Slack thread, a Slack DM or a pull request can be *followed* by a session. New activity there
goes straight to it; the Router is not woken (its timeline shows one line).

- A worker follows every PR its task is to act on (review it, check it, fix, merge or watch
  it), every PR it opens or reports, and the PR of a review you sent; not a PR its task only
  mentions for context (a link to write into a file, *see #60*). A session follows a Slack
  conversation after you approve a reply it drafted there.
- The Router passes an event's thread keys when it hands work to a session.
- For PRs: new comments, replies, reviews, commits, CI results (checks and commit statuses, also
  when a check passes on a re-run) and the merge or close arrive; your own reviews and comments
  do not come back as news. The worker tells you each time, even when its task is finished: news
  as a line in the chat ("CI passes now"), and a question when you should decide how to go on
  (someone answered your comment: reply, re-review, approve, or leave it), often with a reply
  drafted. A merged PR does not accept its task for you: it waits for your *Accept* (merging
  and accepting are separate).
- A PR merged or closed before it was followed is let go at once when a task only names it. One
  a worker was asked to follow (with `follow_thread`, or handed over by the Router to watch it)
  stays followed for two weeks if it is closed, not merged: its reopening (and comments on it)
  is news. After two weeks it is let go, and its worker's chat and the Router's say so.
- A task card's PR chips (a review task's card shows the PR it reviews) show the PR as it is
  now: read again every few minutes and as soon as something happens on it, judging each check
  by its latest run. "What's the status of my PRs?" is answered from that, not from an earlier
  report.
- When a follower closes, activity goes to the Router again.
- A PR that cannot be read says why on its task's card: GitHub says this account cannot read it
  (permission), it does not exist (not found), GitHub's rate limit, or GitHub did not answer
  (a timeout, an outage). It is read again later, each wait longer than the last up to an hour
  (a rate limit: when GitHub says it resets); *Retry* reads it now. A PR GitHub says three times
  in a row does not exist is let go, and its worker is told. The first good read clears the
  warning at once. *Stop following* lets go of it for that task only; once nobody follows it,
  what was kept for reading it is cleared.

## Watchdog

Every minute a cheap check (no model) looks at each working worker: no activity for 15 minutes,
the same step repeated, the same instructions for 45 minutes, or a turn that ended without a
report. A flagged worker is reviewed by the Watchdog session, which decides: on track, nudge
(into the worker's current turn), interrupt a hung step and nudge, or escalate to the Router.
After two nudges without a new report the problem goes to the Router. It does not coach a worker
that is making progress, however long it takes; a nudge says why it stepped in (*Watchdog ·
looks stuck*, and the evidence).

## What runs without asking

Workers act without asking inside their folders. With *Ask first* (below), Chief of Staff also
refuses (the worker must ask you): writes outside its folders, `sudo`, force pushes, pushes to `main`/`master`,
`gh pr merge|close|review|comment`, `gh issue` changes, `gh api` writes, GitHub MCP writes,
piping downloads into a shell, and system-level destructive commands. Workers that read Slack
cannot fetch URLs, and their network reaches only GitHub when sandboxed.

**Yolo** is on by default, as in your own Copilot sessions: workers run without these checks.
Turn it off in Settings → *Workers* (or per worker in its header's **⋯**: *Ask first*) to have
them enforced. Workers that read Slack keep the checks unless you choose yolo for them there. The sandbox
still applies when it is on. Slack replies and PR reviews remain drafts either way.

Nothing is sent as you without your approval. See [SECURITY.md](../SECURITY.md) for which of
these boundaries the OS enforces and which rely on the model.

## Models and usage

Settings sets a default model and one per role, and a default thinking effort per role (it
applies to Copilot's default model too; *Auto*, which picks a model each turn, takes none). A
session's **⋯** (top right; on Home, the Router's) switches its model, effort and context window
(where the model has a long-context tier). The Router may choose a stronger or cheaper model or effort for one task.
**Usage** (the chart icon, bottom left, with today's credits) totals credits by session, model
and role for any date range; every task shows its own credits (hover its time). It includes what a worker used while you had it open in a terminal,
read from Copilot's log (also from before Chief of Staff was running). An optional credit
limit per worker stops runaway workers.

## Daily summary and your profile

At the configured time (default 09:00) the Router posts one summary: what finished (your tasks,
not Chief of Staff's own helpers' nor those cancelled since), what waits for you (results to
accept, as *Needs you* shows them: a report that only informs is not one), what was handled
without you, how many tasks are running, credits used. Quiet days post nothing.

**Build from my work** (Settings → *You* → *Your profile*) starts a worker that distills a short profile
from your projects, GitHub activity, recent Copilot sessions and, with Slack, how you write. It
arrives as a card under *Needs you*: correct anything, then *Use this profile*; sessions use it only
then. Your edits to Slack drafts also teach the Slack session your
style.

## Settings and environment variables

Most settings are in the UI. Environment variables for tuning and tests:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHIEFOFSTAFF_SLACK_USER_TOKEN` | | Slack user token instead of the keychain |
| `CHIEFOFSTAFF_SLACK_MCP_URL` | Slack's | Another Slack MCP endpoint (tests) |
| `CHIEFOFSTAFF_EVENT_DEBOUNCE` | `2` | Seconds events for the Router wait to batch |
| `CHIEFOFSTAFF_ROUTER_CONTEXT` | `150000` | Router context (tokens) at which a fresh Router takes over |
| `CHIEFOFSTAFF_QUESTION_TTL` | `7200` | Seconds before an unanswered question between sessions expires |
| `CHIEFOFSTAFF_REQUEST_TIMEOUT` | `3600` | Seconds a card for you stays open |
| `CHIEFOFSTAFF_WATCHDOG_INTERVAL`, `_STALL`, `_LONG`, `_COOLDOWN` | `60`, `900`, `2700`, `600` | Watchdog timing (seconds) |
| `CHIEFOFSTAFF_MIN_FREE_MEMORY` | `10` | New worker turns wait while less memory than this (percent) is free; `0` turns it off |
| `CHIEFOFSTAFF_RELEASE_IDLE_MINUTES` | `10` | A quiet worker lets go of its Copilot session after this long |
| `CHIEFOFSTAFF_DISK_WARNING_GB`, `_WARNING_PERCENT` | `30`, `5` | Below this much free space (whichever is more) the disk is low: an alert |
| `CHIEFOFSTAFF_DISK_CRITICAL_GB`, `_CRITICAL_PERCENT` | `10`, `2` | Below this, new worker turns wait for space |

Chief of Staff removes its own `CHIEFOFSTAFF_*` variables from the environment Copilot and its
commands run in.

## Data

Everything stays on your machine, in the data directory (`--data-dir`, default per
`platformdirs`, e.g. `~/Library/Application Support/chiefofstaff`): a SQLite database with
sessions, tasks and timelines, workspaces under `work/` and, while Send to Chief is on, its
socket in `send/`. Conversations themselves are
stored by Copilot in `~/.copilot/session-state`; every session is a real Copilot session
(`copilot --resume <id>` works).
