# Chief of Staff

**English** · [简体中文](README.zh-CN.md)

[![PyPI](https://img.shields.io/pypi/v/chiefofstaff)](https://pypi.org/project/chiefofstaff/) [![Python](https://img.shields.io/pypi/pyversions/chiefofstaff)](https://pypi.org/project/chiefofstaff/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

One home for all your GitHub Copilot sessions. Say what you need: each task gets its own
Copilot session, follow-ups go to the session that already has the context, and you are asked
only when a decision is yours or something is ready to review.

<p align="center"><img src="docs/images/captain.svg" width="600" alt="The captain, Chief of Staff's duck, in its five moods: idle, working, needs you, watching, done"></p>

![Home: the chat, what needs you, and every task's live progress](docs/images/home.png)

```bash
uvx chiefofstaff --demo    # try it with scripted sessions, no sign-in needed
```

## Why

I run several Copilot sessions at once: one fixing a test, one reviewing a PR, one reading
logs. I kept switching terminals to see which was stuck, which had a question and which had
finished, while Slack kept pinging me. Now I talk to one session, the Router, and Chief of
Staff keeps track of the rest.

## What it does

**All your work in one place**
- Splits a request into tasks and runs each in its own Copilot session, in parallel or in order.
- Sends a follow-up to the session that did the work, with its history and checkout.
- Shows every task live on one board: what it is doing now, what waits and why, what is done.
- The captain, the duck beside the name, shows how things stand at a glance: it salutes when
  something needs you, takes the wheel while work runs, keeps watch while tasks wait, and
  cheers when a task is done.

**Results you can check**
- Shows what was observed next to each report (test runs, commits, diff, PR checks), and flags
  claims nothing backs.
- Lets you comment on the diff, send it back, and open the PR in one click.

**Asks only when it needs you**
- One card per thing: a decision with options and a recommendation, a question, a draft.
- Reaches you when you are away from your desk (through your company's Slack bot, say) and
  takes your answers from there.
- Focus mode: notifications wait, and you get one summary at the end.

**Speaks for you, once you approve**
- Drafts Slack replies and PR reviews, approvals included, in your voice.
- Watches what you ask it to (Slack mentions, doc comments, mail) with any tool your Copilot
  has, Glean or your company's search included.
- React 🤖 to any Slack message to hand it over as a task.

**Learns how you work**
- Picks up how you write and what you keep asking for. Your style applies at once; habits wait
  for your OK.
- Turns mistakes workers fixed into checks that catch them next time.

**Your Copilot, your machine**
- Uses the Copilot you already have: your plan, models, plugins, skills, MCP servers and your
  company's policies, plus anything you add in Settings. No other AI service or API key.
- Every worker gets its own folder and git worktree, so your checkouts stay untouched. Workers
  run like your own Copilot sessions; turn on permission checks or an OS sandbox in Settings.

## Install

You need macOS, Linux or Windows (experimental), Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/)
(or pipx) and the [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli):

```bash
npm install -g @github/copilot
copilot login
uv tool install chiefofstaff && chiefofstaff   # or: pipx install chiefofstaff
```

Your browser opens Chief of Staff through a private link printed in the terminal (keep it to
yourself). Tell it where your code lives, like `~/workspace`, and type what you need.

Everything runs on your machine; data stays in `~/Library/Application Support/chiefofstaff`
(macOS), `~/.local/share/chiefofstaff` (Linux), or `%LOCALAPPDATA%\chiefofstaff\chiefofstaff` (Windows). Ctrl+C stops it, and sessions pick up where
they left off next time. Options: `--port`, `--data-dir`, `--no-browser`, `--demo`. Update with
`uv tool upgrade chiefofstaff`. Launching `uvx chiefofstaff@latest` again and again works too,
but each new version leaves a cached environment behind in uv's cache: `uv cache prune` removes
the ones nothing uses. Never run `uv cache clean` while Chief of Staff is running: it can delete
the environment it runs from. Workers' folders, and how much disk they take, are under Storage
(the disk icon at the bottom of the sidebar).

On Windows, Send to Chief, the Copilot command sandbox, sandboxed checks, and some storage management features
are unavailable. Workers can access files and networks available to your Windows account.
Unix permission bits do not protect the data directory on Windows: check its Windows access
control list restricts access to trusted users, especially when using `--data-dir`.
Git can still hit path length limits in deeply nested folders; use a shorter project or data
directory if that happens.

To start this checkout after `uv sync --extra dev`:

```powershell
.\.venv\Scripts\python.exe -m chiefofstaff
```

**Optional sign-ins**
- **GitHub**: `gh auth login` with every account you use, to review PRs and open them.
- **Slack**: paste a user token in Settings ([how](docs/guide.md#slack)), or reach Slack through
  a tool your Copilot already has, such as Glean.
- **Other services** (docs, issue trackers): a sign-in card appears when a worker needs one.

## How it works

Each session is a regular Copilot CLI session, driven through the Copilot SDK; `copilot --resume`
lists them as "Chief of Staff · …". Sessions never talk to each other directly: they call tools
such as `spawn_worker`, `report` and `tell_session`, and Chief of Staff delivers each message,
labeled with who sent it, so a Slack message cannot pass itself off as you. Workers that work
together pass what the other needs straight to it; the Router gets a copy.

```
you ── Home ── Router ── tasks ──► worker · worker · worker
                 ▲                 (own folder and worktree)
Slack, GitHub, ──┘    drafts ────► wait for your approval
your watches          Watchdog ──► nudges stuck workers
```

## Learn more

- [User guide](docs/guide.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Security](SECURITY.md): what the OS sandbox, Chief of Staff and the model each enforce
- [Changelog](CHANGELOG.md)

## Status

Early (0.1): expect rough edges. Copilot usage counts against your plan, and Home shows the
credits each task used. Issues and ideas are welcome; to contribute, see
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. Chief of Staff is an independent project, not made by or affiliated with GitHub. GitHub
Copilot is a trademark of GitHub, Inc.
