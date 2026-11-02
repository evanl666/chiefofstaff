# Work Buddy

One Copilot session that runs the others.

You talk to a **Router**. It starts **workers** (one Copilot session per task, each in its own
directory or git worktree), forwards Slack messages to a dedicated **Slack session**, picks up
GitHub review requests, and only interrupts you when a result is ready, a decision is needed or
something failed. Follow-ups in a Slack thread or PR go straight to the session that owns it,
and a **Watchdog** checks that workers are not stuck or drifting. A small local web UI shows
every session at a glance.

```
            you (web UI, desktop notifications)
                    │  ▲
                    ▼  │ notify_owner
 Slack watcher ──► Router ──spawn/message──► worker · worker · worker
 GitHub watcher ─┘    │                        │ report(done/question/blocked)
                      └──message──► Slack session ── draft_slack_reply ──► you approve ──► Slack
```

This is a local prototype built on the [Copilot SDK](https://github.com/github/copilot-sdk).
Your Copilot subscription, policies and usage limits apply.

## Quick start

Requires Python 3.11+, macOS or Linux, and a signed-in
[Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli).

```bash
uv sync
uv run python -m work_buddy
```

The browser opens `http://127.0.0.1:8765` with a private launch token; do not share that URL.
Closing the page does not stop your sessions. Ctrl+C stops Work Buddy; sessions resume where
they left off on the next start.

Open **Settings** once and add the project directories workers may use.

## The UI

- **Left:** Router, Slack and your workers. Blue pulse = working, amber = waiting for an
  answer, green = done, red = failed. Badges count unread updates or drafts waiting for you.
  **Other Copilot sessions** lists sessions you opened in a terminal; resume them with the shown
  command, or let the Router manage one as a worker.
- **Right:** the selected session. Type to the Router, or directly to any worker. Tool calls
  collapse into one "steps" line. Reports and updates are cards with a status and time; they
  render Markdown (lists, tables, code), show long paths as short chips (click to copy) and PR
  links as `owner/repo#123`, and fold when long. Drafts that wait for your approval are pinned on top: edit the
  text and **Send**, or **Reject** with an optional reason the session learns from.
- Closed workers move to the collapsed **Closed** group. Open one to read what it did; **Reopen**
  it, or just type to it, to continue with its conversation and checkout.

## How it works

| Session | Does | Can |
| --- | --- | --- |
| Router | Splits your requests into tasks, routes events and reports, tells you what matters | Only its tools: `spawn_worker`, `ask_workspace`, `message_session`, `list_sessions`, `find_sessions`, `notify_owner`, `close_worker`, `answer_session`. No shell, files or web. |
| Worker | A task, autonomously: investigate, change, test, commit, open a PR; later requests on the same subject | Everything in its working directory, plus your Copilot's MCP servers and CLIs. Reports with `report`, asks you with `ask_user`, asks other sessions with `ask_session`, records `record_setup` notes. May draft Slack replies (if given Slack access) and PR reviews. |
| Slack | Reads conversations and drafts replies in your voice | Slack read tools and `draft_slack_reply`. No shell, files or web. |
| Watchdog | Reviews workers that look stuck, looping or off track; nudges, interrupts or escalates | Only `assess_worker`. |

- Every session is a real, persistent Copilot session (`copilot --resume <id>` works).
- Type several tasks in one message: the Router starts one worker per task. A task that needs
  another's result is started with `after`: it waits, then starts with that task's report.
- Requests go to the worker that already knows the subject. Follow-ups, review comments and CI
  results about a task, and new questions about the same project, repo or topic, go to the
  worker that worked on it, with its context and checkout. Each worker keeps a record (task,
  project, branch, followed PRs and threads, files it wrote, reports). Your messages reach the
  Router with the workers whose record matches (keyword search, Chinese included, or a session
  id), and `spawn_worker` refuses once when related workers exist: the Router either continues
  in one of them (shown as *Continued in …*) or says why a new one is needed (shown in *Started
  … · new because …*), e.g. an independent change that needs its own branch and PR.
- A worker closes itself when its PR is merged or closed; finished workers that follow nothing
  close after *Close idle after* hours (default 24) without activity. Closing is reversible: a
  closed worker is reopened by its session id with its full conversation, unless Copilot no
  longer has the conversation or its directory is gone.
- Folders: Settings → *Projects* lists the folders workers may work in. The Router matches a
  task to a project (it sees each project's name, git remote and README headline); when it is
  not sure, or you name a folder that is not a project yet, it asks you on a card: pick a
  project, *No project*, or type another folder (which then becomes a project).
- Workspaces: every worker gets its own folder under *Workspaces folder* (default
  `<data>/work/workspaces/<task>`). A code change (`isolate=true`) gets a git worktree of each
  project inside it, all on branch `wb/<task>`, started from the latest default branch of
  `origin` (fetched first; the local HEAD when there is no remote) and not tracking it, so your
  own checkout is untouched and parallel workers never share one. A task across repositories
  passes the others in `more_projects`; a worker adds more later with `add_repo`, which also
  clones a repository that is not on this machine (git URL or GitHub `owner/name`, via `gh` when
  available) into its workspace. Reading or running a project without changing it happens in the
  project folder itself. Workers write only in their workspace, checkouts and the temp
  directory; a worker's header shows its repositories and opens its folder in Finder or VS Code.
  The Router, Slack and Watchdog sessions have no file or shell tools.
- At most *Parallel workers* run a turn at once; new tasks and follow-ups beyond that are queued
  and start when a slot frees up.
- Messages arriving while a session is busy are queued, not lost, and are replayed after a
  restart if the session had not handled them. Progress reports stay in the worker's timeline;
  only questions, blockers, results and failures reach the Router.
- The Router's replies to your messages are normal chat. When it acts on an event or a report,
  the result reaches you through `notify_owner` and its closing remark is shown as a footnote.

### How sessions talk

Sessions never talk to each other directly: a session calls a tool, and Work Buddy stores the
message and delivers it to the other session.

- **Envelopes.** Every message a session receives is wrapped as
  `<message from="worker:7494427b" type="report" status="done" title="…">…</message>`. Only the
  attributes say who sent it; the text inside is escaped, so a worker, a Slack message or a web
  page cannot pose as you or the Router. Sessions see 8-character ids, and every tool accepts
  them.
- **Delivery.** A message is stored before it is sent; everything a session has not received
  goes out as one message, in order, and is replayed after a restart.
- **Your messages steer.** What you send to a working session reaches it within its current
  turn, so "stop, wrong direction" takes effect at once (except right after you changed its
  model: then it waits for the next turn, which uses the new model).
- **Events are batched.** Reports and events for the Router (and thread updates for the Slack
  session) wait while it works and for two seconds after (`WORK_BUDDY_EVENT_DEBOUNCE`), so a
  burst becomes one turn.
- **Questions between sessions** (`ask_session`) are tracked: unanswered ones expire after two
  hours (`WORK_BUDDY_QUESTION_TTL`) and end when the asked session closes; the asker is told.
- **Router rotation.** When the Router's context passes 150k tokens
  (`WORK_BUDDY_ROUTER_CONTEXT`), a fresh Copilot session takes over when it is idle. Work Buddy
  writes the handover from its own records (open and recent workers, waiting cards and
  questions, the recent conversation with you), so no model turn is spent on it; the Router
  keeps one timeline in the UI.

### Models and usage

Settings → Models sets a default model and one per role (Router, workers, Slack, Watchdog); the
Router may pick a stronger or cheaper model, or a higher or lower thinking effort, for a single
task. A session's header switches its model, thinking effort (the levels that model offers) and
context window (e.g. 200k or 1M where the model has a long-context tier) from the next message
on; Settings also sets a default thinking effort per role. Every model call is recorded: the
header shows a session's credits, and **Usage** (bottom left) totals AI credits and premium
requests for today, 7 or 30 days or any date range, by session, model and role. An optional
credit limit per worker stops runaway workers.

### Daily summary and your profile

At the configured time (Settings, default 09:00) the Router posts one summary: what finished,
what waits for you, what was handled without you, and credits used. Quiet days post nothing.

**Build from my work** (Settings → Your profile, or ask the Router) starts a worker that
distills a short profile from your projects, your GitHub activity, your recent Copilot sessions
and, with Slack, how you write. You review and edit it before it is used; then the Router,
the Slack session and workers get it as context. Your edits and rejections of Slack drafts are
also kept as examples the Slack session follows.

### When a session needs you

Sessions ask you directly instead of guessing; each request is a card pinned on top (Router and
the session's view), with a desktop notification, and the session shows amber while it waits:

- **Questions** – a worker uses Copilot's `ask_user`: pick a choice or type an answer.
- **Forms and links** – an MCP tool asks for input (fields from its schema) or for you to open
  a link and confirm.
- **Sign-ins** – an MCP server your Copilot uses (company integrations such as docs or tickets)
  needs OAuth: click *Sign in*, finish in the browser tab, and the card closes by itself.
  Copilot keeps the credentials, so every session uses that sign-in; Work Buddy itself stores
  nothing. Only workers prompt for sign-ins.

Work Buddy has no per-tool settings: it uses whatever your Copilot has (MCP servers, CLIs such
as `gh`). When you ask for a new ability ("read our company docs"), the Router starts a setup
worker that finds what is available, asks you to sign in or grant access, checks that it works
and records how in **environment notes** (`record_setup`). The Router and later workers get
these notes; edit or delete them in Settings. Workers that read Slack cannot write notes.

Workers can also ask each other: `ask_session` sends a question to another worker (or the
Router), and the answer comes back as a message. Both sides show the exchange. Sessions that
read Slack and sessions that do not cannot ask each other, so Slack content stays isolated.

### Following threads

A Slack thread, a Slack DM or a pull request can be *followed* by a session. New activity there
goes straight to the followers; the Router is not woken (its timeline shows a one-line notice).

- A session follows a conversation automatically after you approve a reply it drafted there,
  and a worker follows a PR as soon as it reports the PR's URL or you approve its review.
- The Router passes thread keys from an event when it hands the work to a session
  (`follow` on `spawn_worker`/`message_session`); sessions can `follow_thread`/`unfollow_thread`.
- Slack threads and DMs are read every check, so replies without a mention arrive too. For PRs,
  new comments, reviews, CI results and the merge/close arrive; a closed PR is unfollowed.
- When a follower closes, activity there goes to the Router again. A followed DM expires after
  12 hours without activity.

### Watchdog

Every minute a cheap check (no model) looks at each worker: no activity for 15 minutes, the
same step repeated in its last 15 steps, the same instructions for 45 minutes, or a turn that
ended without a report. A flagged worker is reviewed by the Watchdog session (at most every 10
minutes per worker), which sees the task, the reason and recent activity, and decides: on track,
nudge, interrupt the hung step and nudge, or escalate to the Router. After two nudges without a
new report the problem always goes to the Router. Turn it off in Settings. Thresholds can be
changed with `WORK_BUDDY_WATCHDOG_INTERVAL`, `_STALL`, `_LONG` and `_COOLDOWN` (seconds).

### What runs without asking

Workers auto-approve tool use inside their workspace and checkouts, with these exceptions
(denied; the worker must ask you): writes outside them or the temp directory, `sudo`,
force pushes, pushes to `main`/`master`, `gh pr merge|close|review|comment`, `gh issue`
changes, `gh api` writes (an explicit method, fields, `--input`, GraphQL mutations), GitHub MCP
write tools, piping downloads into a shell, and system-level destructive commands.

Sessions that read Slack are contained further, because Slack text is untrusted: the Slack
session gets only its own tools and Slack's read tools; workers with Slack access cannot fetch
URLs or run commands that contact URLs; neither may store Copilot memories. Work Buddy's own
secrets (`WORK_BUDDY_*`) are removed from the environment Copilot and its shells run in.

Nothing is sent as you without approval: Slack messages and PR reviews are drafts until you
press **Send**. The Slack MCP tools given to sessions are read-only; sending is done by Work
Buddy itself with the exact text you approved.

**Yolo** (Settings, off by default; or per worker in its header) turns all these checks off for
workers, including workers that read Slack: every command and file change runs without approval.
Workers are still instructed not to force-push, push to `main`, merge or speak publicly as you,
but nothing enforces it. Slack replies and PR reviews remain drafts either way, because sessions
have no tool to send them. The header and sidebar show when yolo is on.

Treat everything as a prototype, not a security sandbox: workers run commands as your user.

## Slack (official Slack MCP server)

Work Buddy uses Slack's [official MCP server](https://docs.slack.dev/ai/slack-mcp-server/)
with your **user** token. A watcher searches for new DMs and mentions every minute without
using a model and wakes the Router only when something new arrived.

1. Create a Slack app **from `slack-manifest.json`** (user scopes only; token rotation off).
   Your workspace may require admin approval; only internal or Marketplace apps may use MCP.
2. Enable MCP access for the app (Slack shows the link if it is missing:
   `https://api.slack.com/apps/<APP_ID>/app-assistant`).
3. Install the app to your workspace and copy the **User OAuth Token** (`xoxp-…`) from
   *OAuth & Permissions*.
4. In Work Buddy **Settings**, paste the token, enable *Watch Slack*, and save. The token is
   stored in your OS keyring (or set `WORK_BUDDY_SLACK_USER_TOKEN`).

On first enable the watcher starts from "now"; older messages are not replayed.
`WORK_BUDDY_SLACK_MCP_URL` points Work Buddy at another MCP endpoint (used by the tests).

## GitHub reviews

Enable *Watch review requests* and list repositories (`owner/name`). Work Buddy polls
`gh search prs --review-requested=@me` every two minutes through your signed-in `gh` CLI. The
Router starts a worker per request; its review is published as a comment only after you approve
the draft, and only if the PR head has not changed since.

## Adding sources (Teams, email, …)

A source watches something and calls the runtime's trigger sink with stable keys, so the same
event never reaches the Router twice. See `src/work_buddy/sources/__init__.py`;
`sources/slack.py` and `sources/github.py` are complete examples.

## Data

Everything lives in the data directory (`--data-dir`, default per `platformdirs`): the SQLite
database with session metadata and timelines, worktrees and scratch directories under `work/`.
Conversation state itself is stored by Copilot in `~/.copilot/session-state`.

## Development

```bash
uv sync --extra dev
uv run pytest -q                 # unit tests with a fake Copilot SDK client and fake Slack MCP
uv run ruff check . && uv run ruff format --check .
```

Live tests use your real Copilot account (they consume premium requests) and a local stand-in
for Slack's MCP server (`scripts/fake_slack_mcp.py`, same tool names and annotations). They
delete the Copilot sessions they create.

```bash
uv run python scripts/live_e2e.py                              # API-level, ~5-10 minutes
uv run --with playwright python scripts/browser_smoke.py       # drives the UI, saves screenshots
```

Try the Slack flow by hand: run `uv run python scripts/fake_slack_mcp.py`, start Work Buddy
with `WORK_BUDDY_SLACK_MCP_URL=http://127.0.0.1:18900/mcp WORK_BUDDY_SLACK_USER_TOKEN=xoxp-demo`,
enable Slack in Settings, and type DMs from "Alice" into the fake server's prompt.
