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
| **Running** | Each task in progress: what its worker is doing now, or what it waits for. Hover for *Cancel* and *Add a note*. |
| **Ready to review** | Finished tasks with the worker's report and [receipts](#receipts). [Review the changes](#reviewing-changes), *Create PR*, *Accept*, *Follow up*, or *Copy report* (markdown for a PR description). |

Finished and cancelled tasks stay under *Recently finished* for three days. **Chat** is the
conversation with your Router; the session list below it opens any worker's full timeline
(every step, report and message), where you can also talk to it directly.

The dot next to each session shows its state (hover it for the words): pulsing blue, working;
amber, waiting for your answer; green, done; red, failed; hollow grey, queued; grey, idle; a
blue ring, open in a terminal right now.

Until you set your code folder (see [Where workers work](#where-workers-work)), the inbox asks
for it; *Not now* hides that card, and Settings still has it.

## Giving work

Type into the inbox or Chat. Several things at once is fine: the Router splits them into
tasks and starts a worker for each, in parallel. When one task needs another's result, it
waits and then starts with that result. The Router replies with one line; results come back
as they finish.

Try `chiefofstaff --demo` to see this without an account.

## Tasks and workers

A **worker** is a Copilot session with its own folder. A **task** is something you asked for,
with a goal, a status and a result. A worker can take several tasks over time, so later work
on the same subject reuses its context.

Task states: *queued* (waiting for a slot or for tasks it depends on), *running*, *waiting*
(it asked something or is blocked), *review* (done, ready for you), *done* (you accepted it),
*failed* (retry it with an optional note) and *cancelled*.

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
  back.

A worktree is a fresh checkout: no dependencies installed, and none of the files git ignores.
Workers are told to install what the project needs (per its README or `AGENTS.md`). Gitignored
files a worktree needs, like `.env`, are copied in from your checkout: by default `.env` and
`.env.*` files; a `.worktreeinclude` file at the repository root (gitignore syntax, as with
Claude Code) lists others instead. Only files git ignores are copied, and never for workers that
read Slack.

Folders outside the code folder can be added under *Advanced → Other project folders*, and
*Task folders* puts the task folders somewhere else. A worker's header opens its folder in
Finder or VS Code.

With the **sandbox** on (Settings, default), workers' shell commands can write only to their
workspace, checkouts and the repositories' `.git` (not their hooks or config), never see your
GitHub token, and cannot read `~/.ssh`. On macOS, run `copilot sandbox ca create && copilot
sandbox ca trust` once so `gh` works inside the sandbox; Settings tells you when it is needed.

## Receipts

A worker's report is its claim. Receipts are what Chief of Staff observed:

- the shell commands it ran and their exit codes, recognized as tests, checks or builds;
- commits and changes in its checkouts (`git diff` against where its branch started);
- the state and checks of pull requests it reported.

The review card shows them under *Observed* and flags disagreements, e.g. *Says tests pass, but
no test run was observed*, *The last test run failed (exit 1)*, or *Checks are failing on
acme/app#7*. *Copy report* puts goal, result and receipts into markdown.

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

- **Questions**: a worker uses Copilot's `ask_user`. Pick a choice or type an answer.
- **Forms and links**: an MCP tool asks for input, or for you to open a link.
- **Sign-ins**: an MCP server needs OAuth. Sign in in the browser tab; the card closes by
  itself. Copilot keeps the sign-in for every session (see [SECURITY.md](../SECURITY.md)).
- **A folder**: the Router is not sure where a task belongs.
- **Approvals**: Slack replies and PR reviews drafted in your voice. Edit and *Send*, or
  *Reject* with a reason the session learns from.

There are no per-tool settings: Chief of Staff uses whatever your Copilot has (MCP servers,
CLIs). For a new ability ("read our company docs"), the Router starts a setup worker that finds
what is available, asks you to sign in, checks that it works and records how in **environment
notes** for later sessions (editable in Settings).

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
After two nudges without a new report the problem goes to the Router.

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

Settings sets a default model and one per role, and a default thinking effort per role. A
session's header switches its model, effort and context window (where the model has a
long-context tier). The Router may choose a stronger or cheaper model or effort for one task.
**Usage** (bottom left) totals credits by session, model and role for any date range; every
task shows its own credits. An optional credit limit per worker stops runaway workers.

## Daily summary and your profile

At the configured time (default 09:00) the Router posts one summary: what finished, what waits
for you, what was handled without you, credits used. Quiet days post nothing.

**Build from my work** (Settings → Your profile) starts a worker that distills a short profile
from your projects, GitHub activity, recent Copilot sessions and, with Slack, how you write. You
review it before sessions use it. Your edits to Slack drafts also teach the Slack session your
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
