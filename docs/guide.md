# Guide

How Chief of Staff works from your side: what you see, what it does on its own, and what it
asks you. For how it is built, see [ARCHITECTURE.md](ARCHITECTURE.md). For what keeps agents in
bounds, see [SECURITY.md](../SECURITY.md).

- [The inbox](#the-inbox)
- [Giving work](#giving-work)
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

## The inbox

The home view answers three questions:

| Section | What is there |
| --- | --- |
| **Needs you** | Questions and forms from workers, sign-ins, a folder to pick, Slack replies and PR reviews to approve, sends a restart interrupted, failed or blocked tasks. |
| **Running** | Each task in progress at a glance: the step its worker is in now and for how long, and what it said last; or what it waits for. Hover for *Cancel* and *Add a note*. |
| **Ready to review** | Finished tasks with the worker's report and [receipts](#receipts). [Review the changes](#reviewing-changes), *Create PR*, *Accept*, *Follow up*, or *Copy report* (markdown for a PR description). |

Finished and cancelled tasks stay under *Recently finished* for three days. **Chat** is the
conversation with your Router; the session list below it opens any worker's full timeline
(every step, report and message), where you can also talk to it directly.

A file path in a report or message opens the file when it is in a worker's folders (a
walkthrough, results, a log): Markdown shows formatted. Other paths are copied.

The dot next to each session shows its state (hover it for the words): pulsing blue, working;
amber, waiting for your answer; green, done; red, failed; hollow grey, queued; grey, idle; a
blue ring, open in a terminal right now.

Until you set your code folder (see [Where workers work](#where-workers-work)), the inbox asks
for it; *Not now* hides that card, and Settings still has it.

## Giving work

Type into the inbox or Chat. Several things at once is fine: the Router splits them into
tasks and starts a worker for each, in parallel. When one task needs another's result, it
waits and then starts with that result. The Router replies with one line; results come back
as they finish: each worker's report comes to you whole, as a card, and the Router does not
repeat it. When you write to the Router, its reply is the one answer you get.

**To one worker:** type `@` in the inbox or Chat and pick a worker; the message goes straight to
it (`→ worker` in Chat), and the Router hears of it with its next message. Or open the worker,
or use *Add a note* on its card.

**Progress:** ask the Router ("how is it going?"). It reads what each worker is doing now and
said last, and answers at once instead of waiting on the workers. A worker in a long step (a
query, a build, a job) reads new messages only when that step ends; a message you send it then
gets a note saying so, with what it is doing.

Instructions you give again and again ("run a small smoke job before the full one") are
[learned](#how-you-work-learned-and-kept-up-to-date), so you do not have to repeat them.

Try `chiefofstaff --demo` to see this without an account.

## Tasks and workers

A **worker** is a Copilot session with its own folder. A **task** is something you asked for,
with a goal, a status and a result. A worker can take several tasks over time, so later work
on the same subject reuses its context.

Task states: *queued* (waiting for a slot or for tasks it depends on), *running*, *waiting*
(it asked something or is blocked), *review* (done, ready for you), *done* (you accepted it),
*failed* (retry it with an optional note) and *cancelled*.

A task that needs another's result starts once that one reports done. When it must build on
work you have checked (an API others code against), the Router makes it wait until you
*Accept* the other one instead; its card says *waiting for your review of …*.

**Routing to the right worker** matters most. Your message reaches the Router together with
the workers whose record matches it (title, task, files, branches, reports; keyword search,
Chinese included). For work on a subject a worker already knows, the Router either:

- **continues** that worker's task (a correction, more information, a follow-up question; a
  finished task reopens),
- gives the same worker a **new task** with its own result to review, or
- starts a **new worker that builds on earlier results** (`context_from`): for an independent
  change with its own branch and PR, or work that must run in parallel. It gets the earlier
  tasks' results, folders and files, not their whole conversations.

`spawn_worker` refuses once when related workers exist, so the Router decides explicitly; the
timeline shows *New task … for …*, *Continued in …* or *Started … · new because …*.

Workers close themselves when their PR is merged or closed. Finished workers that follow
nothing close after *Close idle after* hours (default 24). Closing is reversible: talk to a
closed worker, or open it and press *Reopen*, to continue with its conversation and checkout.

At most *Parallel workers* run at once; the rest queue.

### Your other Copilot sessions

*Other Copilot sessions* in the sidebar lists the sessions Chief of Staff does not manage, the
ones you started in a terminal. Those open in a terminal right now come first, marked *open*.
Opening one shows its latest conversation, and keeps it current while it works.

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
  would overwrite each other's files and switch each other's branches.
- Follow-ups on a task go to the same worker and folder. When a worker closes, its worktrees
  are removed if they hold nothing uncommitted (the branches stay); reopening it brings them
  back. A worktree with ignored files you may want (an edited `.env`, a report) is kept;
  dependencies and build output alone do not keep it.

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

## Receipts

A worker's report is its claim. Receipts are what Chief of Staff observed:

- the shell commands it ran and their exit codes, recognized as tests, checks or builds;
- commits and changes in its checkouts (`git diff` against where its branch started);
- the state and checks of pull requests it reported.

The review card shows them under *Observed* and flags disagreements, e.g. *Says tests pass, but
no test run was observed*, *The last test run failed (exit 1)*, or *Checks are failing on
acme/app#7*. A test that failed first and then passed (a regression test for a fix) shows as
*✗→✓ tests*. *Copy report* puts goal, result and receipts into markdown.

**Acceptance criteria.** For work that can be checked, the Router gives the task 1-4 *done when*
criteria: a test that passes, a command and its output, a request and its response (for a bug
fix, a regression test that fails before the fix and passes after). The worker reports each
one with its evidence, and the card marks it:

| Mark | Meaning |
| --- | --- |
| ✓ | **Verified**: the evidence names a command the worker was seen running, and it passed |
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
Click a line to comment on it; *Send to the worker* sends all comments as one follow-up, each
with its file and line, and the task comes back for review when the worker is done.

*Create PR* pushes the worker's branch to `origin` and opens a pull request on GitHub with your
signed-in `gh`: the task's title, and its report as the description. If a PR from that branch
is open already, that one is used. Uncommitted work is not pushed: follow up and ask the worker
to commit first. An open worker then follows the PR (reviews, comments and CI go to it), and
when the PR is merged its task is marked done.

## When a session needs you

Sessions ask instead of guessing. Each request is a card under *Needs you*, with a desktop
notification:

- **Decisions**: a choice that is yours (scope, a trade-off, a risky change such as a migration).
  The card says why it comes up, the options and what each means, and what the worker
  recommends; pick one or write your own. Workers that hit the same question wait on the same
  card, and one answer reaches all of them. See [Decisions](#decisions).
- **Questions**: a worker uses Copilot's `ask_user` for quick facts. Pick a choice or type an
  answer.
- **Forms and links**: an MCP tool asks for input, or for you to open a link.
- **Sign-ins**: an MCP server needs OAuth. Sign in in the browser tab; the card closes by
  itself. Copilot keeps the sign-in for every session (see [SECURITY.md](../SECURITY.md)).
- **A folder**: the Router is not sure where a task belongs.
- **Approvals**: Slack replies and PR reviews drafted in your voice. Edit and *Send*, or
  *Reject* with a reason the session learns from. A Slack draft shows where it goes (DM,
  channel or thread) and the latest message there, read from Slack itself, so you can check it
  answers the right conversation.

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
since: the Slack messages you sent, your PR reviews and review comments (on every GitHub account
signed in to `gh`), your commit messages in your projects (not Copilot's, nor those a worker
made), what you asked your own Copilot sessions, and what you told the Router. Only your own
words; others' messages are never kept. A short-lived worker, which can do nothing but propose,
then describes how you write in each place: answers and reports to you, Slack DMs, Slack
channels, PR reviews, commit messages (language, length, tone, greetings, formatting, a short
quote or two). Each applies at once to the sessions that write that way for you (the Slack
session and workers that read Slack get your Slack style; workers get your commit and review
style; the Router and workers answer you the way you talk to them), and the Router chat says
what was learned. Settings → *How you work* shows each, with its version; *Undo* goes back to the
previous one. Turn *Learn from my Slack messages, PR reviews, commits and Copilot sessions* off
to stop reading them.

**How you want work done.** Your sessions also learn from what you do with their work:

- your decisions, especially when you go against what a worker recommended;
- review comments on a diff, follow-ups on a finished result, notes when you retry a task,
  reasons when you cancel one;
- the drafts you edit before sending, and why you rejected one;
- the decisions you change;
- the instructions you give the Router and workers, so that guidance you repeat becomes a lesson.

Only your own choices and words count; what a session proposed is kept beside them as context.
Once a few new pieces of feedback came in (or guidance you keep giving your own sessions), the
learning worker turns them into habits, e.g. *Give every database migration a rollback
migration* or *Run a small smoke job before the full one*, each with where it applies
(everywhere, Slack replies, one project) and what it rests on. A **Learned from your work** card
shows them: keep, reword or untick each. Only what you keep goes into your sessions'
instructions; habits learned while the card waits join it.

Settings → *How you work* lists the habits in use (remove any) and shows how close the sessions
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
Settings → *How you work* lists the checks, how often each ran and caught something, and turns
any off. Checks run in a sandbox: no network, no writes, and nothing in your home folder but the
repository is readable. They need macOS (`sandbox-exec`) or `bwrap` on Linux.

## Focus

*Focus…* (inbox header) holds notifications for 30 minutes to 4 hours. The Router keeps
working: Slack messages that need a reply get drafts, code work goes to workers, noise is
ignored. Only what the Router marks urgent (an outage, someone blocked on you today) gets
through. When focus ends you get one summary: messages handled, replies drafted, tasks ready to
review, and what needs you.

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

Change the emoji (or empty it to turn this off) in **Settings → Slack**. The watcher finds
reactions with Slack's `hasmy::robot_face:` search, among the 20 most recent messages you
reacted to; reactions from before you turned it on are ignored. The message itself is still
someone else's text: the Router checks with you before anything risky it asks for.

## GitHub reviews

Enable *Watch review requests* and list repositories (`owner/name`). Chief of Staff checks
`gh search prs --review-requested=@me` through your signed-in `gh`. The Router starts a worker
per request; its review is published only after you approve the draft, and only if the PR head
has not changed since.

You can also just ask: "review tscp-targeting PR #1529". A worker drafts reviews on a PR its task
names (by URL, or by the repository's name and number), one it opened, follows or was given, or
one GitHub asks you to review. A review carries **comments on lines** of the diff: the card
shows each one to edit, or empty to leave out. Lines outside the diff are refused when drafting,
so what you approve can be published.

On a PR a worker is working on, it can also draft **replies to review comments**, in any
repository: each card shows the comment it answers, and the reply is posted in that thread once
you approve it.

**Several GitHub accounts** (a personal one active in `gh`, your company's signed in as well):
Chief of Staff reaches each repository with the account that can see it, and remembers it per
organization. Sign in with each once (`gh auth login`); you need not switch. When none can see a
repository, the worker tells you which accounts are signed in.

Workers commit on their branch but do not push unless the task asks for it: you open the PR with
*Create PR*. Analyses and research need no branch at all.

## Following threads

A Slack thread, a Slack DM or a pull request can be *followed* by a session. New activity there
goes straight to it; the Router is not woken (its timeline shows one line).

- A session follows a conversation after you approve a reply it drafted there; a worker follows
  a PR as soon as it reports the PR's URL.
- The Router passes an event's thread keys when it hands work to a session.
- For PRs: new comments, reviews, CI results and the merge or close arrive. A merged PR marks
  the task that delivered it done.
- When a follower closes, activity goes to the Router again.

## Watchdog

Every minute a cheap check (no model) looks at each working worker: no activity for 15 minutes,
the same step repeated, the same instructions for 45 minutes, or a turn that ended without a
report. A flagged worker is reviewed by the Watchdog session, which decides: on track, nudge
(into the worker's current turn), interrupt a hung step and nudge, or escalate to the Router.
After two nudges without a new report the problem goes to the Router. It does not coach a worker
that is making progress, however long it takes; a nudge says why it stepped in (*Watchdog ·
looks stuck*, and the evidence).

## What runs without asking

Workers act without asking inside their folders. Chief of Staff refuses (the worker must ask
you): writes outside its folders, `sudo`, force pushes, pushes to `main`/`master`,
`gh pr merge|close|review|comment`, `gh issue` changes, `gh api` writes, GitHub MCP writes,
piping downloads into a shell, and system-level destructive commands. Workers that read Slack
cannot fetch URLs, and their network reaches only GitHub when sandboxed.

**Yolo** (Settings, or per worker in its header) turns these checks off. The sandbox still
applies when it is on. Slack replies and PR reviews remain drafts either way.

Nothing is sent as you without your approval. See [SECURITY.md](../SECURITY.md) for which of
these boundaries the OS enforces and which rely on the model.

## Models and usage

Settings sets a default model and one per role, and a default thinking effort per role (it
applies to Copilot's default model too; *Auto*, which picks a model each turn, takes none). A
session's header switches its model, effort and context window (where the model has a
long-context tier). The Router may choose a stronger or cheaper model or effort for one task.
**Usage** (bottom left) totals credits by session, model and role for any date range; every
task shows its own credits. It includes what a worker used while you had it open in a terminal,
read from Copilot's log (also from before Chief of Staff was running). An optional credit
limit per worker stops runaway workers.

## Daily summary and your profile

At the configured time (default 09:00) the Router posts one summary: what finished, what waits
for you, what was handled without you, credits used. Quiet days post nothing.

**Build from my work** (Settings → Your profile) starts a worker that distills a short profile
from your projects, GitHub activity, recent Copilot sessions and, with Slack, how you write. It
arrives as a card in the inbox: correct anything, then *Use this profile*; sessions use it only
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

Chief of Staff removes its own `CHIEFOFSTAFF_*` variables from the environment Copilot and its
commands run in.

## Data

Everything stays on your machine, in the data directory (`--data-dir`, default per
`platformdirs`, e.g. `~/Library/Application Support/chiefofstaff`): a SQLite database with
sessions, tasks and timelines, and workspaces under `work/`. Conversations themselves are
stored by Copilot in `~/.copilot/session-state`; every session is a real Copilot session
(`copilot --resume <id>` works).
