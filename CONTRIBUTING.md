# Contributing

Thanks for helping. This page gets you from a clone to a merged change. The map of the code is
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); a list of well-scoped first changes is
[docs/good-first-issues.md](docs/good-first-issues.md).

## Set up

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). A Copilot account is optional:
most work needs none.

```bash
git clone https://github.com/evanl666/chiefofstaff && cd chiefofstaff
uv sync --extra dev
uv run chiefofstaff --demo          # the whole product, scripted, no accounts
```

## Tests

| Command | What | Needs |
| --- | --- | --- |
| `uv run pytest -q` | Unit and scenario tests (fake and scripted backends) | Nothing |
| `uv run --with playwright python scripts/demo_smoke.py` | The UI through the demo scenario, light and dark | `playwright install chromium` once |
| `CHIEFOFSTAFF_TEST_COPILOT=1 uv run pytest -q -m copilot` | Contract tests against the real Copilot CLI | Copilot; a few small model calls |
| `uv run python scripts/live_e2e.py` | Every feature end to end with real Copilot | Copilot; ~10 minutes of model use |
| `uv run --with playwright python scripts/browser_smoke.py` | The UI with real Copilot | Copilot |
| `uv run --with playwright python scripts/ui_edges.py` | UI edge cases (long reviews, memory cards and settings), light and dark | `playwright install chromium` once |
| `uv run python scripts/live_routing.py` | Does the Router give work to the worker that knows the subject? | Copilot |
| `uv run python scripts/live_usecases.py` | Everyday flows: revise, cancel and retry, questions, Create PR, long answers | Copilot; a GitHub test repository |
| `uv run python scripts/live_memory.py` | How you write is learned and used; a fixed mistake becomes a check that catches it again | Copilot; macOS or `bwrap` |

Before a pull request:

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q
npx eslint@10 src/chiefofstaff/static     # the web UI
```
CI runs those, the demo browser test and a secret scan on every push.

Write tests for behavior, not for implementation: a scenario through `tests/conftest.py`'s
`Harness` (a fake Copilot you drive with `session.emit(...)`) is usually right. Failures at the
worst moment belong in `tests/test_faults.py`.

## Style

- Code: `ruff` (100 columns) and `pyright` for Python, ESLint for the UI; all clean. Names say what things are; comments say why, not what.
- Text the owner reads (UI, notifications, prompts): short, plain, specific. No exclamation
  marks, no "successfully".
- Keep the UI simple: plain HTML, CSS and JavaScript; no framework or build step.
- One change per pull request, with a test. Say what changes for the owner.

## Add an event source

Teams, email, Linear, PagerDuty: a source watches something and hands new events to the
runtime. Slack (`sources/slack.py`) and GitHub (`sources/github.py`) are complete examples.

```python
# src/chiefofstaff/sources/linear.py
import asyncio

from ..store import Store


class LinearSource:
    name = "linear"

    def __init__(self, store: Store, host, client):
        self.store, self.host, self.client = store, host, client
        self.status = {"enabled": True, "connected": False, "error": ""}
        self.job = None

    async def start(self):
        self.job = asyncio.create_task(self.loop())

    async def stop(self):
        if self.job:
            self.job.cancel()

    def wake(self):
        pass

    async def loop(self):
        while True:
            for issue in await self.client.assigned_to_me():
                # A stable key per event: the same issue update never reaches the Router twice.
                await self.host.on_trigger(
                    "linear",
                    [f"linear:{issue.id}@{issue.updated_at}"],
                    f"New issue assigned to the owner: {issue.title}\n{issue.url}",
                )
            await asyncio.sleep(120)
```

Register it with `runtime.sources.append(LinearSource(store, runtime, client))` (in the
runtime factory, or behind a setting). The Router receives the event in an envelope marked
`trust="untrusted"`; tell it what to do with such events in `prompts.py` if the default
judgment is not enough. To let sessions follow an item (comments go straight to the worker),
add a thread key format in `threads.py` and call `host.on_thread(...)` for followed items, as
`GitHubSource.poll_pull` does.

## Debug a delivery

Everything a session was sent is in the database. With Chief of Staff running (WAL mode lets
you read it):

```bash
DB=~/Library/Application\ Support/chiefofstaff/chiefofstaff.db   # see "Data:" at startup
sqlite3 "$DB" "select substr(id,1,8), role, status, title from sessions where closed_at is null"
# A session's timeline; rows with meta.prompt are its outbox
sqlite3 "$DB" "select id, kind, substr(text,1,60), substr(meta,1,80) from messages
               where session_id like 'abcd1234%' order by id desc limit 20"
# How far it was sent (sent:) and handled (idle:); a sending: key means a send was interrupted
sqlite3 "$DB" "select key, value from kv where key glob 's*:abcd1234*' or key glob 'idle:abcd1234*'"
# Tasks, runs and observed commands
sqlite3 "$DB" "select id, status, title from tasks order by created_at desc limit 10"
sqlite3 "$DB" "select run_id, kind, exit_code, substr(command,1,60) from commands order by id desc limit 10"
```

`copilot --resume <session id>` opens the same conversation in a terminal. To see the raw
JSON-RPC traffic, wrap `copilot._jsonrpc.JsonRpcClient._send_message` and `_handle_message`
in a small script (both take one message dict).

## Pull requests

- Describe the change from the owner's side, and how you tested it (paste the demo
  screenshots for UI changes).
- Keep pull requests small; open an issue first for anything that changes how sessions behave.
- By contributing you agree that your contribution is licensed under the MIT license.

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
