# Chief of Staff

**An AI chief of staff for your GitHub Copilot agents.** Tell it what you need, several things
at once if you like. It hands each task to its own Copilot session, sends follow-ups to the
session that already knows the subject, drafts your Slack replies and PR reviews for you to
approve, and keeps quiet until something needs you.

![The inbox: results ready to review, with what was observed](https://raw.githubusercontent.com/evanl666/chiefofstaff/main/docs/images/inbox-review.png)

```bash
uvx chiefofstaff --demo     # try it now: scripted sessions, no account needed
```

## Why

Running many Copilot sessions in parallel is easy; keeping track of them is not. You end up
switching between terminals to check on them, answering Slack pings in between, and losing
track of which session knew what. Chief of Staff gives you **one inbox** instead:

- **Needs you**: a question a worker cannot answer, a reply to approve, a sign-in.
- **Running**: what each task is doing right now, or what it waits for.
- **Ready to review**: finished work with **receipts**: the tests that ran and passed, the
  commits, the PR checks, next to what the worker says it did, and a flag where they disagree.

## What it does

- **Routes work to the session that knows it.** Follow-ups on the login fix go to the worker
  that fixed the login, with its context and checkout. Independent work gets its own worker,
  and can build on earlier results.
- **Tasks that wait for each other.** "Fix the flaky test, then add rate limiting": the second
  starts when the first is done, with its result.
- **Delegate by reaction.** React 🤖 to any Slack message ("can someone fix the export?") and
  it becomes a task; when it is done, the answer is drafted in that thread for you to approve.
- **Handles your Slack.** DMs and mentions get a reply drafted in your voice; nothing is sent
  until you approve. Replies in that thread go straight to the session that owns it.
- **Reviews PRs you are asked to review**, as drafts you approve.
- **Focus mode.** Notifications wait; the Router handles what it can; you get one summary.
- **A Watchdog** notices stalled or looping workers and nudges them back on track.
- **Workers in their own folders**: a git worktree per task, in Copilot's OS sandbox, so your
  checkout is never touched and commands cannot write elsewhere.
- **Runs on the Copilot you already have**: your company's Copilot plan, models, MCP servers
  and policies. No new AI vendor to approve.

![Approving a Slack reply drafted in your voice](https://raw.githubusercontent.com/evanl666/chiefofstaff/main/docs/images/approve-dark.png)

## Get started

You need macOS or Linux, Python 3.11+, and the [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli)
signed in (`npm install -g @github/copilot`, then `copilot login`).

```bash
uvx chiefofstaff            # or: pipx install chiefofstaff && chiefofstaff
```

Your browser opens a private local page. Add a project folder when it asks, then tell your
chief of staff what you need. Closing the page does not stop your sessions; they resume after a
restart.

Optional: [connect Slack](https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md#slack) to have DMs and mentions answered, and
[GitHub review requests](https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md#github-reviews).

## How it works

```
 you ──► inbox ◄─────────────────────────────── one summary, cards that need you
          │
          ▼
       Router ──── tasks ───► worker · worker · worker     (each: its own Copilot session,
          ▲                     │  report, ask you,           folder and git worktree)
          │                     ▼  ask each other
 Slack, GitHub watchers    Slack session ──► drafts you approve ──► Slack
          Watchdog ──► nudges stalled workers
```

Every session is a real Copilot session (`copilot --resume <id>` works). Chief of Staff
coordinates them: it stores every message before delivering it, wraps it in an envelope that
says who sent it (so a Slack message cannot pose as you), checks what was observed against what
was reported, and keeps the owner's attention for what matters.

- [Guide](https://github.com/evanl666/chiefofstaff/blob/main/docs/guide.md): the inbox, tasks, workspaces, Slack, focus, settings.
- [Architecture](https://github.com/evanl666/chiefofstaff/blob/main/docs/ARCHITECTURE.md): how it is built.
- [Security](https://github.com/evanl666/chiefofstaff/blob/main/SECURITY.md): what the OS enforces, what Chief of Staff enforces, and what only the
  model's instructions do.

## Status

Alpha: expect rough edges and changes. Copilot usage counts against your plan like any
Copilot session; the inbox shows credits per task.

## Contributing

Start with the demo and [CONTRIBUTING.md](https://github.com/evanl666/chiefofstaff/blob/main/CONTRIBUTING.md); [good first issues](https://github.com/evanl666/chiefofstaff/blob/main/docs/good-first-issues.md)
are listed there. Ideas and bug reports are welcome as issues.

## License

[MIT](https://github.com/evanl666/chiefofstaff/blob/main/LICENSE). Chief of Staff is an independent project, not affiliated with GitHub.
"GitHub Copilot" is a trademark of GitHub, Inc.
