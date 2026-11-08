# Architecture

Chief of Staff is one Python process (FastAPI and asyncio) that runs a set of GitHub Copilot
sessions through the [Copilot SDK](https://github.com/github/copilot-sdk) and coordinates them.
This page is the map; the code is written to be read next to it.

```
browser ── REST (actions) ──────────────► web.py ──► Runtime ◄── sources/ (Slack, GitHub watchers)
   ▲                                                  │  ▲          watch loop (Watchdog, digest,
   └── SSE (/api/events) ◄── Bus ◄── every change ────┘  │          focus, question expiry)
                                                         │ events
                                          AgentBackend (backend.py)
                                                         │ JSON-RPC over stdio
                                          copilot CLI ── Router · Slack · Watchdog · workers
```

## Roles

| Session | Does | Tools |
| --- | --- | --- |
| **Router** | Turns requests into tasks, routes reports and events, tells the owner what matters. | Only its own: `spawn_worker`, `message_session`, `cancel_task`, `stop_worker`, `list_sessions`, `find_sessions`, `ask_workspace`, `notify_owner`, `close_worker`, `answer_session`, `distill_profile`, `add_watch` (a card the owner confirms). No shell, files or web. |
| **Worker** | One task at a time, autonomously; later tasks on the same subject. | Everything Copilot offers in its folders, plus `report`, `ask_session`, `tell_session`, `add_repo`, `record_setup`, `follow_thread`, drafts. |
| **Slack session** | Reads conversations and drafts replies in the owner's voice. | Slack read tools, `draft_slack_reply` and `withdraw_draft`. |
| **Watchdog** | Reviews workers a cheap check flagged. | `assess_worker`. |

Roles differ only in their options (`Runtime.options`): system prompt (`prompts.py`), tools,
tool allowlist, permission and request callbacks, sandbox, model.

## Modules

| Module | Responsibility |
| --- | --- |
| `__main__.py`, `web.py` | Command line; HTTP API, auth (the launch key kept by the page and sent as an `X-Chief-Key` header, or `?key=` where a header cannot go: SSE, images; same-origin writes, CSP), SSE. |
| `runtime.py` | `Runtime`: the Copilot connection, each session's lifecycle (create, resume, rotate), session options, external events and threads, the UI snapshot. It creates the parts below; each keeps a reference to it. |
| `events.py` | What sessions report as they work: one small handler per event type. |
| `delivery.py` | Messages to sessions: stored, batched, sent, checked after a restart. |
| `workers.py` | The worker queue (`max_workers`, dependencies), starting, closing (open tasks are cancelled; the owner's *Reopen* brings back those it had started, stopped: `closedtask:` keys; after a merge only once no result of it waits for review), reopening, interrupting. |
| `tasks.py` | Tasks, runs and dependencies as records; status transitions. |
| `task_actions.py` | What happens to tasks: reports, cancel (a cancelled follow-up gives the earlier result back; a task whose result was reported stays done with it; its drafts still waiting for the owner are withdrawn), accept, retry, follow-up (its worker told that its report replaces the result: the whole result, updated, with its criteria; criteria a later report leaves out keep what the earlier result showed, verified only while what backs them holds); finished work its worker changes again (asked in chat, its question answered, its PR's review comments) is a new result to review, not news (a question asked after such a change keeps its start at the result); work apart from a task's result (the owner explicitly asking its worker for a new or separate task, a report with `new_task_title`, or code work for it on another branch than the task's open PR) is a task of its own, the task keeping its result and PR; once its PR is opened or merged, its card (and the PR's text) leaves out only what its result said of that before ("not pushed", "no PR", "(not merged)"), the stored result staying as reported; receipts (per task: between snapshots of its checkouts taken when it was taken up and when it reported its result, written to the repository's objects through an index of their own; on a branch, its own commits; in a shared folder, the files its own edits wrote or its own commands named, not what commits older than the task brought in, a pull or another branch checked out; a follow-up after other work leaves out what changed in between; each result's end is kept, so a split gives each result its share), PR state (refreshed every few minutes) and the *Ready to merge* card; the shareable report. |
| `tools.py` | Every tool a session can call, with its argument model; which role gets which. |
| `drafts.py` | Drafts the owner approves (Slack replies, PR reviews, a task's folder); interrupted sends; what each draft is for the Router (recipient, what it answers, its task) and what became of it (a held note); a worker following the Slack thread a sent review or reply was asked for in is woken to answer there. One reply per message: a worker's reply sent or drafted in a conversation settles the Slack session's replies waiting there (other workers' stay: they may answer another question), and the Slack session does not draft where a worker follows the conversation for an open or recently finished task (it took it over); a newer reply of a session replaces its older one there only for the same task (another task's answers another question; both cards stay); a card the owner edited is never withdrawn by its session (it is given their text) nor replaced (a newer draft waits beside it). A Slack reply waits until a review it may speak of is posted (its session's, under any of its tasks, or on a PR it names; `after_review` on its card); a merged PR takes its task's waiting drafts on it along. |
| `focus.py` | Focus Shield; its summary counts what needs the owner as Home does (`digest.needs_you`). |
| `digest.py` | The daily digest, from the record; what needs the owner, item by item as *Needs you* lists it (`digest.needs`: the board's rules, a result reviewed on its draft's card once), shared by the Router's `list_sessions`, the handover, the focus and daily summaries and tidying. |
| `state.py` | Per-session in-memory state (connection, turn flags) and the lock order. |
| `receipts.py` | Observed commands, changes and PR checks next to a report; acceptance criteria as verified, claimed or not met; a test run's outcome from its runner's summary (piped, chained, run by a script). |
| `workspaces.py` | The code folder (its repositories, clones into it), each worker's task folder and worktrees (removed on close, restored on reopen), change stats. |
| `policy.py` | Pure permission rules for actions without the sandbox. |
| `backend.py` | The `AgentBackend`/`AgentSession` protocols, Copilot backend, sandbox policy. |
| `scripted.py` | A backend without a model (scripts); runs `demo.py` and contract tests. |
| `asks.py` | Cards for the owner (questions, forms, sign-ins); workers' (and the Slack session's) questions to the owner, kept under *Needs you* until answered whatever becomes of their task (one per task; each of a session's questions without a task on its own); a task whose question or decision the owner answered goes on at once (`Asks.go_on`; finished work it asked about goes back where it was, and its worker restates the whole result in its next report); cards applied whenever they are answered, with nothing waiting (`Asks.offer`: a watch the Router proposes); questions between sessions; environment notes. |
| `peers.py` | Workers that work together (`works_with`): told about each other, passing information straight to each other (`tell_session`), a copy for the Router riding along with its next turn, a finished one's result for the others. |
| `decisions.py` | Decision cards (answered on the card or in the asking worker's chat, not by a question back; closed once the tasks that asked ended); the owner's decisions and the tasks that rely on them; telling those tasks when one changes. |
| `learning.py` | The owner's feedback; lessons a learning worker proposes and the owner approves; how often the owner agrees. |
| `habits.py` | The owner's own words, read from Slack, GitHub, git and their Copilot sessions; the style learned from them (versioned, undoable) and which sessions get it. |
| `connector.py` | The connector: one helper worker that reaches the owner's world with whatever tools Copilot has (Slack, Glean, mail). Runs the owner's watches on a schedule and hands findings to the Router as `watch` events; collects the owner's own words for `habits.py`; when the owner is away (idle keyboard), tells them what needs them (a gist of up to five items, each with a random single-use code), and applies their answers. It reads untrusted content, so it is sandboxed like a Slack reader (network to GitHub only); the away message itself goes through a separate `messenger` helper, given only Chief of Staff's text in a file and sandboxed as the owner's workers are, so a company bot's CLI can send it; the messenger alone reports that it sent (`report_notified`) and reads the owner's answers the same way (`report_owner_replies`). With Slack connected it gets no Slack tools of its own: `send_owner_slack_dm` and `read_owner_slack_dm` send the notification being sent to the owner's own Slack DM through Chief of Staff's Slack connection, and read only the owner's own messages there since. |
| `abilities.py` | Skills the owner uploads or names, Copilot plugins installed from Settings (`copilot plugin install`), and MCP servers from Settings, added to workers' options. |
| `checks.py` | Failure cases kept from workers' test runs; checks proposed from them, proven in a sandbox on both versions, approved, then run when a worker reports done. |
| `envelope.py` | The envelope every message to a session is wrapped in. |
| `machine.py` | How much memory the computer has left: new worker turns wait while it is low (`Workers.claim_slot`, `pump`); quiet workers let go of their Copilot session (`Workers.release_idle`). |
| `catalog.py` | Keyword ranking that finds the worker that owns a subject. |
| `watchdog.py` | Cheap checks that decide when a worker deserves a review, and the Watchdog session that reviews it. |
| `sources/` | Watchers: Slack (MCP search), GitHub (`gh`). |
| `store.py` | SQLite: schema, migrations, small helpers. |
| `bus.py` | Fan-out of UI events to browser tabs. |
| `static/` | The UI: plain HTML, CSS and JavaScript modules, no build step. |

## The agent backend

`copilot` runs as one child process; every session lives inside it. The SDK speaks JSON-RPC 2.0
over the child's stdin and stdout (LSP-style `Content-Length` framing). Calls go one way
(`session.create`, `session.send`, `session.abort` …); everything a session does comes back as
`session.event` notifications. `send` returns at once, so the runtime is event-driven.

The CLI's processes (the npm loader and the program it runs) are remembered (`copilot_pids`):
a session they have open never counts as open in the owner's terminal, even once orphaned, and
they are stopped with everything they started when Chief of Staff disconnects (or, left over
from an abrupt stop, when it connects). `Runtime.watch_process` checks that the CLI still runs;
when it stops by itself, the owner sees an error and it connects again, waiting longer after
each failure.

Custom tools, permission checks and questions for the owner travel the other way: the CLI asks
(`external_tool.requested`, `permission.requested`, `userInput.request`), the SDK finds the
Python callback registered for that session and answers with the result.

The runtime depends on the `AgentBackend` and `AgentSession` protocols, not the SDK.
`tests/test_backend_contract.py` checks what it relies on (event order, tools with parameters,
permission requests, resume, abort ending the running command, steering) against the scripted
backend always and against real Copilot with `CHIEFOFSTAFF_TEST_COPILOT=1`.

## Messages between sessions

Sessions never talk directly. A session calls a tool, and the runtime delivers the message.

- **Envelopes.** Every message a session receives is wrapped:
  `<message from="worker:7494427b" type="report" status="done" task="t3f9a2b1" title="…">…</message>`.
  Only the runtime writes the attributes; the body is escaped, so text cannot open or close an
  envelope or pose as the owner. External content is marked `trust="untrusted"`.
- **Outbox.** `Delivery.deliver` stores the message (its exact prompt in `messages.meta`) and
  `flush` sends everything after the session's `sent:` mark as one message. A `sending:`
  marker brackets each send; after a stop, the session's history (`get_events`) shows whether
  it arrived, so nothing is sent twice. A message that starts a turn shows in the session's
  history at once; one that does not within `CHIEFOFSTAFF_START_CHECK` seconds was never started
  on (Copilot gives up on a session's queue when it cannot load its settings, an MCP server's,
  with no error event): the session shows an error (a worker and its task fail, for *Retry*)
  and what it was sent goes out again on a new connection (`Runtime.not_started`). A settings
  change, or *Stop*, reconnects such a session at once.
- **Modes.** The owner's messages steer a working session (`immediate`, within its turn).
  Events and reports for the Router are batched: held while it works and briefly after, so a
  burst becomes one turn. Everything else is queued (`enqueue`).
- **Workers that work together.** `works_with` on `spawn_worker` links tasks that run at the
  same time and need each other. A worker's `tell_session` (and `ask_session`) goes straight to
  the other worker, within its turn; the Router gets a copy held in its outbox (`Delivery.hold`:
  stored, not flushed), so it rides along with the Router's next turn without starting one.
  When a linked worker reports done, the others still at work get its result.
- **Routing of external events.** Watchers turn new Slack messages and GitHub activity into
  events with stable keys (`triggers`, never twice). Activity in a followed thread or PR goes
  straight to its followers (`subscriptions`); the rest goes to the Router. A Slack thread a
  worker follows is that worker's (it took it over): the Slack session no longer gets it too
  (`Runtime.thread_owners`). A PR's news goes to
  the workers with a task that made it (`Runtime.pr_maker`), as that task's
  (`Runtime.thread_task`), not to those that only read or reviewed it, except replies to a
  comment one of them posted (`ghby:` keys); everyone following it when no task made it.
  Commits made in a worker's checkout (its reflogs say so: commits, amends, a rebase's or a
  `pull --rebase`'s picks, the commit a rebase goes on with after a conflict, merges) are not
  news, nor are commits already on the PR's base (a rebase onto a base others moved brings them
  in); a branch rewritten to a head made in a worker's checkout is that worker's own.

## Tasks

A worker session is a conversation and a folder. Each assignment is a **task** (`tasks`) with
a status (`queued → running ⇄ waiting → review → done`, or `failed`/`cancelled`), a result,
acceptance criteria and dependencies on other tasks (`task_deps`, met at `review` or `done`, or
only at `done` when they need the accepted result). A **run** (`runs`) is one attempt: it
starts when the worker starts on the task and ends with a report, a failure or a cancellation;
usage, observed commands, evidence and the worker's claims per criterion are kept per run.
Decisions (`decisions`) are linked to the tasks that rely on them (`task_decisions`). A report names its task (the
`task` attribute) or defaults to the worker's current task. A worker reports `failed` when its
task cannot be done; a task the Router has another one do instead (`replaces`) is cancelled as
replaced, so no *Retry* stays beside it.

## Events

`Events.handle_event` (`events.py`) runs on the event loop for every session event and passes it
to the handler for its type:

| Event | Effect |
| --- | --- |
| `user.message` | Whether this turn includes the owner (else the Router's closing words are a footnote); the task a worker sees now is taken up (its turn task, unless the message steered the turn in progress; a followed thread's news, the task it is for, which a stop does not resume; back on a finished task from other work, what it changes counts from there). A message sent while a worker works is only taken up here, when it sees it; if the turn is stopped first (Copilot drops it), it is sent again with the next message. |
| `assistant.message` | The session's words, into its timeline (the Router's closing words in a turn without the owner are a footnote; none when they only say it has nothing to say). |
| `assistant.turn_start` | A worker's slot is busy; status `working`. |
| `tool.execution_start` | A step in the timeline and the session's activity; shell commands are recorded for receipts; files an edit tool writes count as the turn task's (in a folder shared with the owner, only those are its changes). |
| `tool.execution_complete` | Exit code for receipts (a command left running keeps its shell id; reading or stopping that shell settles it); last output for the Watchdog. |
| `system.notification` | A command left running ended (`shell_completed`): its exit code. |
| `assistant.usage` | Credits and tokens per run; the Router's context size. |
| `session.idle` | Turn over: free the slot, send held messages, rotate the Router if needed, start queued workers. |
| `session.error` | A failed worker task; the Router is told. |

Events from a Copilot session the runtime has let go of (replaced, refreshed, closed) are
ignored.

## State and concurrency

What must survive a restart is in SQLite (`store.py`): sessions, messages (timeline and
outbox), tasks, runs, dependencies, commands, drafts (cards and approvals), questions between
sessions, triggers, subscriptions, usage, settings, and a key-value table for marks and flags
(`sent:<id>`, `idle:<id>`, `created:<id>`, `copilot:<id>`/`alias:<id>`, `preamble:<id>`, …).

What belongs to the running process is in `SessionState` (`state.py`): the live connection and
the current turn's flags. Coroutines interleave at every `await`, so the locks are taken in a
fixed order: rotation, then a session's lifecycle lock, then its sending lock, then the worker
slot lock (`Workers.pump_lock`, never across a session lock).

## Restart

On start, interrupted sends are checked against the sessions' histories; sessions that were mid
turn continue with a note quoting what they had received (the newest whole), and get what they
had not, in full: also what counted as sent but their history does not show (Copilot can lose
what it took just before it stopped, `Delivery.received`). Drafts whose sending was interrupted
are looked up in Slack or GitHub before anything is resent. Cards for the owner that wait on an
answer in memory end; a watch the Router proposed that ended so is told to it.

## Router rotation

When the Router's context passes `CHIEFOFSTAFF_ROUTER_CONTEXT` tokens, a fresh Copilot session
takes over while it is idle. The handover is written from the records (open tasks, workers,
cards with their recipient, what they answer and their task, the questions workers asked the
owner, tasks cancelled or failed in the last three days, the recent conversation),
so no model turn is spent on it; `alias:` keys map both Copilot sessions to the same Router.
What the owner cancels, closes or reopens, what became of each draft (sent, rejected,
withdrawn), and a worker's question the owner answered or dismissed reach the Router as a held
system note (`Delivery.hold`) with its next message, so it does not start that work again by
itself or call the question open; a cancel during a Router turn is steered into that turn. A
Router about to continue or cancel a task that changed since it looked (`list_sessions`,
`read_session`), by what it has not received yet, is told what changed instead.

The Router is never closed and replaced. While its Copilot session is open in a terminal,
Chief of Staff shows an error and connects again once it is closed there; a Copilot session
that cannot be resumed (Copilot no longer has it) gets a successor with the same handover
(`Runtime.renew_router`), so its timeline and what the owner told it stay.

## The UI

The UI is plain ES modules in `static/`, with no build step: `app.js` starts the page;
`state.js` loads a snapshot (`/api/state`) and applies SSE events (`message`, `session`, `task`,
`draft`, `toast`, `state`, `focus_summary`); `inbox.js` renders Home (the Router's chat beside
Needs you and the task board; Needs you also lists the Router's notes that need the owner,
`updates` in the snapshot, until seen: `POST /api/messages/{id}/seen` or a later owner message
to the Router). `board.js` (pure, tested with node) places each task in one of
four places (needs you, running, monitoring, recently finished) with a marker in words (queued,
up next, paused, checks back, delivered, overdue, to review…) from its status, its worker's
state, the task the worker is on (`turn_task`: its turn's task while open or followed up by the
owner, else its current open one; only instructions set it, not notes), whether it was taken up
(`started`), `check_back`, the threads it watches and when it was last active. Home, the
sidebar's dots and the cards all use it. `tasks.js` draws a box per worker in *Needs you* and
*Running* with its task cards, and a row per task in *Monitoring* and *Recently finished*
(scrolling lists; a row opens its card in place); every section folds (`inbox.js` fold); `artifacts.js` the Artifacts list (the PRs a task owns, from their receipts; docs and runs as
their workers declared them with `report(artifacts=…)` or linked in any report, kept per task;
links the task was given are references, not output). `session.js`, `sidebar.js`,
`drafts.js`,
`composer.js` (the message box, with images) and `settings.js` render their parts; `dom.js` and `text.js` are helpers (the Markdown subset is
escaped first). Writes are REST calls. Cards are reconciled by key and refreshed in place, so a
half-typed reply survives updates, and a card whose drawing fails shows so without stopping the
rest. ESLint checks the modules in CI.
