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
| **Router** | Turns requests into tasks, routes reports and events, tells the owner what matters. | Only its own: `spawn_worker`, `message_session`, `cancel_task`, `list_sessions`, `find_sessions`, `ask_workspace`, `notify_owner`, `close_worker`, `answer_session`, `distill_profile`. No shell, files or web. |
| **Worker** | One task at a time, autonomously; later tasks on the same subject. | Everything Copilot offers in its folders, plus `report`, `ask_session`, `add_repo`, `record_setup`, `follow_thread`, drafts. |
| **Slack session** | Reads conversations and drafts replies in the owner's voice. | Slack read tools and `draft_slack_reply`. |
| **Watchdog** | Reviews workers a cheap check flagged. | `assess_worker`. |

Roles differ only in their options (`Runtime.options`): system prompt (`prompts.py`), tools,
tool allowlist, permission and request callbacks, sandbox, model.

## Modules

| Module | Responsibility |
| --- | --- |
| `__main__.py`, `web.py` | Command line; HTTP API, auth (launch token cookie, same-origin writes, CSP), SSE. |
| `runtime.py` | Coordination: sessions' lifecycle, tools (`t_*`), event handling, scheduling, Router rotation, drafts, focus, the UI snapshot. |
| `state.py` | Per-session in-memory state (connection, turn flags) and the lock order. |
| `delivery.py` | Messages to sessions: stored, batched, sent, checked after a restart. |
| `tasks.py` | Tasks, runs and dependencies; status transitions. |
| `receipts.py` | Observed commands, changes and PR checks next to a report. |
| `workspaces.py` | Worker folders, worktrees, clones, change stats. |
| `policy.py` | Pure permission rules for actions without the sandbox. |
| `backend.py` | The `AgentBackend`/`AgentSession` protocols, Copilot backend, sandbox policy. |
| `scripted.py` | A backend without a model (scripts); runs `demo.py` and contract tests. |
| `asks.py` | Cards for the owner (questions, forms, sign-ins), questions between sessions, environment notes. |
| `envelope.py` | The envelope every message to a session is wrapped in. |
| `catalog.py` | Keyword ranking that finds the worker that owns a subject. |
| `watchdog.py` | Cheap checks that decide when a worker deserves a review. |
| `sources/` | Watchers: Slack (MCP search), GitHub (`gh`). |
| `store.py` | SQLite: schema, migrations, small helpers. |
| `bus.py` | Fan-out of UI events to browser tabs. |
| `static/` | The UI: plain HTML, CSS and JavaScript, no build step. |

## The agent backend

`copilot` runs as one child process; every session lives inside it. The SDK speaks JSON-RPC 2.0
over the child's stdin and stdout (LSP-style `Content-Length` framing). Calls go one way
(`session.create`, `session.send`, `session.abort` …); everything a session does comes back as
`session.event` notifications. `send` returns at once, so the runtime is event-driven.

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
  it arrived, so nothing is sent twice.
- **Modes.** The owner's messages steer a working session (`immediate`, within its turn).
  Events and reports for the Router are batched: held while it works and briefly after, so a
  burst becomes one turn. Everything else is queued (`enqueue`).
- **Routing of external events.** Watchers turn new Slack messages and GitHub activity into
  events with stable keys (`triggers`, never twice). Activity in a followed thread or PR goes
  straight to its followers (`subscriptions`); the rest goes to the Router.

## Tasks

A worker session is a conversation and a folder. Each assignment is a **task** (`tasks`) with
a status (`queued → running ⇄ waiting → review → done`, or `failed`/`cancelled`), a result and
dependencies on other tasks (`task_deps`, met at `review` or `done`). A **run** (`runs`) is one
attempt: it starts when the worker starts on the task and ends with a report, a failure or a
cancellation; usage and observed commands are counted per run. A report names its task (the
`task` attribute) or defaults to the worker's current task.

## Events

`Runtime.handle_event` runs on the event loop for every session event:

| Event | Effect |
| --- | --- |
| `user.message` | Whether this turn includes the owner (else the Router's closing words are a footnote). |
| `assistant.message` | The session's words, into its timeline. |
| `assistant.turn_start` | A worker's slot is busy; status `working`. |
| `tool.execution_start` | A step in the timeline and the session's activity; shell commands are recorded for receipts. |
| `tool.execution_complete` | Exit code for receipts; last output for the Watchdog. |
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
slot lock (never across a session lock).

## Restart

On start, interrupted sends are checked against the sessions' histories; sessions that were mid
turn continue with a note quoting what they had received, and get what they had not; drafts
whose sending was interrupted are looked up in Slack or GitHub before anything is resent.

## Router rotation

When the Router's context passes `CHIEFOFSTAFF_ROUTER_CONTEXT` tokens, a fresh Copilot session
takes over while it is idle. The handover is written from the records (open tasks, workers,
cards, questions, the recent conversation), so no model turn is spent on it; `alias:` keys map
both Copilot sessions to the same Router.

## The UI

`static/app.js` loads a snapshot (`/api/state`) and then applies SSE events (`message`,
`session`, `task`, `draft`, `toast`, `state`, `focus_summary`). Writes are REST calls. Cards are
reconciled by key, so a half-typed reply survives updates. There is no build step.
