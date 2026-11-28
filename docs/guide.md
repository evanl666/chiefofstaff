# Guide

How to use Chief of Staff, from the first start to a full day of work. Every behavior and setting
in detail, with its edge cases, is in the [Reference](reference.md); this page links to it where
you may want more.

- [Quickstart](#quickstart)
- [Concepts](#concepts): the Router, workers and tasks, *Needs you*, teams, decisions, receipts
- [Daily use](#daily-use): giving work, reviewing, Slack, GitHub, watches, away, Send to Chief
- [Agents: Copilot, Codex and Claude](#agents-copilot-codex-and-claude)
- [Settings](#settings)
- [Safety and privacy](#safety-and-privacy)
- [Sharing](#sharing)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

## Quickstart

**1. Try the demo.** No account, no sign-in, nothing leaves your machine:

```bash
uvx chiefofstaff@latest --demo
```

Your browser opens on Home. The demo plays a short scenario on scripted agents: a request
becomes three tasks on Copilot, Codex and Claude, one worker tells another what it changed, one
of them asks you a decision, a Slack message gets a reply drafted for you, a teammate's PR gets
a review drafted, and the Watchdog nudges a worker that stalled. Click around: answer the card,
open a report, accept a result, share it. Anything you type gets a simple scripted run too.

**2. Install it.** You need macOS or Linux (Windows is experimental, on Copilot only), Python
3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/) or pipx, and one coding
agent signed in: the GitHub Copilot CLI, the Codex CLI or Claude Code.

```bash
npm install -g @github/copilot && copilot login   # or: codex login, or: claude auth login
uv tool install chiefofstaff && chiefofstaff       # or: pipx install chiefofstaff
```

Without installing, `uvx chiefofstaff@latest` runs the newest version (plain `uvx chiefofstaff`
runs whatever version uv cached first). No agent installed or signed in yet? Start Chief of Staff
anyway: Home's card has *Install* and *Sign in* for each agent and runs them for you.

**3. First start.** The terminal prints a private link and opens it in your browser; keep the
link to yourself. Chief of Staff runs on the first agent it finds ready (Copilot, then Codex,
then Claude Code) and says which in your chat. Home asks for your **code folder**, where your
repositories live (`~/workspace`, say). Then type what you need:

- "Fix the flaky login test in acme-api, then add rate limiting to login attempts."
- "Review acme/api PR #1529 and approve it if nothing blocks merging."
- "Compare three feature-flag services for a team of ten: price, SDKs, self-hosting."

**4. Optional sign-ins.**

- **GitHub:** `gh auth login`, with every account you use, so workers can open PRs and you can
  review them.
- **Slack:** a user token in Settings → Connections → *Slack* (see [Slack](#slack)), for
  drafted replies and 🤖 delegation.
- **Other services** (docs, issue trackers): a sign-in card shows up when a worker needs one.

Ctrl+C stops Chief of Staff; sessions pick up where they left off next time. Closing the browser
stops nothing. Options: `--port`, `--data-dir`, `--no-browser`, `--demo`.

## Concepts

### The Router

You talk to one session, the **Router**, on Home. It reads your request, splits it into tasks,
starts a worker for each (or gives it to a worker that already knows the subject), and answers
in a line. It does no work itself: it routes, keeps your decisions, and tells you what needs
you. Ask it anything about your work ("how is it going?", "what is the login fix waiting on?")
and it answers from what the workers are doing now, without interrupting them.

### Workers and tasks

A **worker** is a real session of a coding agent (Copilot, Codex or Claude Code) with a folder of
its own. A **task** is something you asked for, with a goal, a status and a result. A worker can
take several tasks over time, so later work on the same subject reuses what it knows.

- Each worker works in its own **git worktree** of each repository it changes, on a branch
  `chief/<task>` from the latest default branch. Your own checkouts are never written.
- A task goes *queued* → *running* → *review* (a result for you) → *done* (you accepted it).
  It may wait on you (*waiting*), fail (with why, and *Retry*), or be cancelled.
- A task that needs another's result starts once that one is done, with its result.
- *Workers at once* (Settings → General) caps how many run at the same time; the rest queue.
  While memory or disk is low, new work waits and running work goes on.
- Finished workers close after a day idle and can be reopened with their conversation and
  checkout: talk to one, or press *Reopen*.

Every worker shows its agent: a small diamond on its sidebar row (purple Copilot, green Codex,
orange Claude), a chip on its line in your chat, its name in its header.

### Needs you

The right side of Home has one place for everything:

| Section | What is there |
| --- | --- |
| **Needs you** | Decisions, questions, drafts to approve (Slack replies, PR reviews), results to accept, failed or stopped tasks, sign-ins |
| **Teams** | One row per request that became a team: its parts, what it waits on, its latest change |
| **Running** | Tasks being worked on now, each with its live step |
| **Monitoring** | Tasks nobody has to act on now but that wait for something: CI, a review, a reply, a run, a check-back |
| **Recently finished** | Accepted work, cancelled work, for three days |
| **Artifacts** | The PRs, documents and runs your tasks delivered, with where each stands now |

Each card has one main action (*Answer*, *Send*, *Accept*…); the rest is behind **⋯**. Approvals
that only need your OK (a habit, a check, a PR ready to merge, a sign-in) are lines in one card,
**Your OK**, and what was handled for you is one quiet line under it, **Handled for you**, each
with *Undo* where it can be undone. A result that changed no code and opened no PR is accepted
for you a little after you read it; everything else waits for your *Accept*. More:
[Reference → Home](reference.md#home), [Your OK and Handled for
you](reference.md#your-ok-and-handled-for-you).

### Teams

When the Router splits one request into parts that work together (two sides of one change, a
part that needs another's result), those parts are a **team**, named after your request.

- Workers in a team **tell and ask each other** what the other needs, in their own words and at
  once, without a Router turn in between. A worker waiting for another's message waits quietly
  and goes on when it arrives. When one finishes, the others still at work get its result.
- What they tell each other is information, never new work: a change of course goes through the
  Router, which decides or asks you.
- The **team page** (click a team's row on Home) is where you run it: one line saying where the
  request stands and the one thing that moves it, the **plan** as a rail of parts with each
  part's state and output, **So far** (what was made, what was decided, what to watch), **Since
  you looked**, and **Tell the team** to send one message to the parts you pick. Done, the page
  says what the request delivered, with *Copy summary*.

Workers are listed one by one in the sidebar; teams are on Home. More:
[Reference → Tasks and workers](reference.md#tasks-and-workers).

### Decisions

Workers ask instead of guessing. A **decision card** says what is to decide and why, the options
and what each means, and what the worker recommends; pick one or write your own.

- **One choice, asked once.** Within one request, workers (and the Router) that hit the same
  choice share one card, and your one answer reaches all of them, also a worker in the middle of
  a turn. Across requests, a card says *Possibly the same as “…”* and *Merge* joins them.
- **Decisions are kept**, each with the tasks that rely on it. Tell the Router "change of plan:
  limit login attempts per IP address only" and every worker whose task relied on the old
  decision hears what changed, finished work that relied on it reopens, and a *Decision changed*
  card shows who confirmed what.

More: [Reference → Decisions](reference.md#decisions).

### Receipts

A worker's report is its claim; **receipts** are what Chief of Staff saw it do. Next to every
result: the test and build commands it ran and their exit codes (*✗→✓ tests* when a failing test
was made to pass), its commits and its diff, the PR checks. Each acceptance criterion is
*verified* (a check was seen running and passing), *claimed* (only the report says so) or
*failed*. A report that claims something nothing backs is flagged. More: [Reference →
Receipts](reference.md#receipts).

## Daily use

### Giving work

Type on Home. Several things at once is fine: the Router splits them and starts them in parallel,
or in order when one needs another's result.

- **To one worker:** type `@` and pick it; your message goes straight to it.
- **On one agent:** type `@codex` or `@claude` (or pick one from the list `@` opens) for what you
  ask next; two to compare (see [Agents](#agents-copilot-codex-and-claude)).
- **Images:** paste or drop screenshots (up to five, 5 MB each); the worker gets them too.
- **Progress:** ask the Router. A worker's routine updates stay quiet on its task's card.
- **Stop:** *Stop* in a worker's chat interrupts it; its task waits under *Needs you* for *Go on*
  or *Cancel*. "Stop all my work" to the Router does the same for every worker.

For work on a subject a worker already knows, the Router continues that worker's task, gives it
a new task, or starts a new worker that builds on its results. When that worker is busy, its new
task can start at once in a **second worker** with its conversation so far (⑂ in the sidebar).
Say "a new worker" in so many words and you get one.

### Reviewing results

A finished result is a card under *Needs you*, marked *New* until you open it.

- **View report** opens it whole, with its receipts.
- **Accept** finishes the task. **Request changes** sends it back with what to change; its next
  report is a new result to review. **Follow up** asks more: its worker reports the whole result
  again, updated, and that replaces the one before.
- Click the changes (*+12 −3 in 2 files*) to read the diff; click a line to comment, and *Send to
  the worker* sends all your comments as one follow-up.
- **Create PR** (in **⋯**) pushes the worker's branch and opens a pull request with your `gh`.

A worker's PR stays under *Monitoring* until it is merged: its CI, reviews and merge state are
read from GitHub, and news on it comes back as an *Update*. Workers close themselves when their
PR is merged or closed.

### Following threads, check-backs and the Watchdog

Work is followed until it is really done:

- **Threads.** A worker follows every PR it opens or acts on, and the Slack thread of a reply you
  approved. New comments, reviews, commits, CI results and the merge go straight to that worker;
  the Router is not woken.
- **Check-backs.** A worker waiting on a long run (a training job, a deploy) sets a time to look
  again; its row says when (*↻ in 12 min*) and what it checks.
- **The Watchdog.** Every minute a cheap check (no model) looks for workers with no sign of work
  for 15 minutes, the same step repeated, the same instructions for 45 minutes, or a turn that
  ended without a report. A model call counts as work while it is open, and a command while its
  output still grows: a model call that never answers is caught at the 45-minute mark, not at 15.
  The Watchdog session reviews each one and nudges it, interrupts a hung step, or tells the
  Router. A worker making progress is never disturbed, however long it takes.
- **Overdue.** A task with no activity for four hours (*Overdue after*) gets its worker asked
  where it stands; no answer within the hour brings it to *Needs you*.

More: [Reference → Following threads](reference.md#following-threads),
[Watchdog](reference.md#watchdog).

### Focus and the daily summary

**Focus** (top of Home) holds notifications for 30 minutes to 4 hours while work goes on; only
what the Router marks urgent gets through, and you get one summary at the end. The **daily
summary** (Settings → General, 09:00 by default) says what finished, what waits for you, what
was handled for you and what it used. Quiet days post nothing.

### Slack

Chief of Staff uses Slack's official MCP server with your **user** token. A watcher looks for new
DMs and mentions every minute without a model, and wakes the Router only when something new
arrived.

1. Create a Slack app **from `slack-manifest.json`** in this repository (user scopes only; token
   rotation off). Your workspace may need an admin to approve it.
2. Enable MCP access for the app (`https://api.slack.com/apps/<APP_ID>/app-assistant`).
3. Install it and copy the **User OAuth Token** (`xoxp-…`) from *OAuth & Permissions*.
4. Settings → Connections → *Slack*: paste the token, turn on *Watch Slack*, save. The token is
   kept in your OS keychain (or set `CHIEFOFSTAFF_SLACK_USER_TOKEN`).

Messages that need a reply get one **drafted in your voice**, on a card with *Send*; nothing goes
out until you approve it. Messages that need work become tasks.

**Delegate by reaction.** React 🤖 to any Slack message, yours or someone else's, in any channel,
to hand it over. The Router treats it as your request, and when the author is waiting, the worker
replies in that thread with the outcome: your reaction is your OK for that one reply (it falls
back to a card when someone wrote in the thread since, or the reply looks like it holds a
secret). Write a note to yourself and react to it to delegate from your phone.

No Slack app? A watch can read Slack through a tool your agent already has (see
[Watches](#watches)). More: [Reference → Slack](reference.md#slack).

### GitHub reviews

Turn on Settings → Connections → *GitHub reviews* and list repositories (`owner/name`): each
review requested of you starts a worker that drafts the review. Or just ask: "review
checkout-service PR #1529". A drafted review has a verdict (approve, request changes or comment)
and comments on lines of the diff; the card shows the PR, the review and each comment with its
code. It is published only when you approve it, and only if the PR has not changed since. More:
[Reference → GitHub reviews](reference.md#github-reviews).

### Prepared for you: keeping your PRs moving

Turn on Settings → General → *Keep my PRs moving*. When a PR a worker opened for you fails CI or
gets review comments, the same worker prepares what it needs: a fix, checked, or replies to the
comments. Its row under *Needs you* changes to *Review fix* or *Review & post*, and nothing
reaches GitHub until you approve it. Its *Options* set how far it goes for each kind of change
and what it may spend a day. Preparing fixes needs Copilot for now; on other agents it still
finds what needs doing. More: [Reference → Prepared for
you](reference.md#prepared-for-you-keeping-your-prs-moving).

### Watches

Settings → Connections → *Watches* says what to look out for, in your words: "Slack DMs and
mentions of me", "comments on my docs", "emails from customers", and optionally with what tool.
Chief of Staff checks them on a schedule (every 5 minutes in work hours, every 30 after; *Scan
now* checks at once) with the MCP servers, skills and CLIs your agent has, and turns what needs
you into tasks and drafts. You can also ask the Router ("watch #deploys for payment failures"):
it proposes the watch on a card. More: [Reference →
Watches](reference.md#watches-what-to-look-out-for).

### When you're away

Turn on Settings → Connections → *When you're away* and say how to reach you ("DM me on Slack
through our team's bot plugin", or "send it to my own Slack DM"). After some minutes without
keyboard or mouse, what needs you is sent there once, as a short list, each item with a code of
its own. Answer where you got it: "K3XQ7 send" sends a draft, "K3XQ7 2" picks option 2 of a
decision (or folder 2 of a folder question), "K3XQ7 use port 9090" answers a question, "K3XQ7 retry" retries a failed task. A code works once,
and only for what it came with; anything else you write there is never taken as a command. *Send
a test* tries the way you gave. More: [Reference → When you are
away](reference.md#when-you-are-away).

### Send to Chief

Hand over work from wherever you are without switching to Home. Turn it on in Settings →
Connections → *Send to Chief from the command line* (macOS and Linux), then:

```bash
chiefofstaff send "fix the flaky login test in acme/api"
chiefofstaff send "review this" --url https://github.com/acme/api/pull/7
pbpaste | chiefofstaff send "answer Maya with the outcome" --text-from -
```

Started with `uvx`, there is no `chiefofstaff` in your shell: run `uv tool install chiefofstaff`
once, or use `uvx chiefofstaff send …`.

**From a hotkey:** bind `/full/path/to/chiefofstaff send "$1"` (`command -v chiefofstaff` prints
the path) to a key with macOS Shortcuts (*Ask for Input*, then *Run Shell Script* with input as
arguments, then *Add Keyboard Shortcut*), Raycast, Alfred, or a custom shortcut on Linux.
Step-by-step recipes for each, and one that sends the front browser tab: [Reference → From a
hotkey](reference.md#from-a-hotkey).

### Your other sessions

*Other sessions* in the sidebar lists the sessions you started yourself in a terminal (Copilot's,
and Codex's and Claude Code's while those agents are on), the ones open now first, each with
where it stands: asking you, working, idle, done. Open one to read it. **Send it a message to
continue it here**: it becomes a worker, with its conversation so far. While a session is open in
a terminal, Chief of Staff only reads it: continue there, or exit it there to message it here.
More: [Reference → Your other sessions](reference.md#your-other-sessions).

### How it learns you

- **How you write.** Every two hours (or *Learn now* in Settings → You) it reads what you wrote:
  your Slack messages, PR reviews, commit messages, what you asked your own Copilot sessions, what
  you told the Router. Only your own words. Your writing style applies to drafts at once.
- **How you work.** Instructions you keep giving ("run a small smoke job first") become
  **habits**. One your own words backed three times or more, and not about secrets, money or what
  goes out, is kept at once and listed under *Handled for you* with *Undo*; the others wait for
  your OK under *Your OK*. Once you undo or drop more than one in five of those kept at once,
  they all wait for your OK again.
- **Your profile.** Settings → You → *Build from my work* distills a short profile from your
  projects and activity; sessions use it once you approve it.
- **Checks learned from mistakes.** When a worker's failing test passes after its fix, a helper
  proposes a check (a shell command) that fails on the mistake. It runs on both versions before
  you see it: it must fail on the code with the mistake and pass on the fix. *Use this check*
  turns it on, and from then on a worker in that repository that reintroduces the mistake is sent
  back before you see its result.

Everything learned is listed in Settings → You, where you can edit or remove it. More:
[Reference → How you work](reference.md#how-you-work-learned-and-kept-up-to-date), [Checks
learned from mistakes](reference.md#checks-learned-from-mistakes).

## Agents: Copilot, Codex and Claude

<a id="workers-on-other-coding-agents"></a>

Workers, and Chief of Staff's own sessions (the Router, the Watchdog, the Slack session, the
watcher and the helpers), run on any of three coding agents, each with your own sign-in and plan:

| Agent | Sign in with | Uses |
| --- | --- | --- |
| [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli) | `copilot login` | Your Copilot plan (premium requests, shown as credits) |
| [Codex CLI](https://developers.openai.com/codex/cli) | `codex login` | Your ChatGPT sign-in, or an API key |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | `claude auth login` | Your Claude plan, or an API key |

Codex and Claude Code run on macOS and Linux; on Windows, Chief of Staff runs on Copilot for now.

**Turning one on.** Once Codex or Claude Code is installed and signed in, Home asks once:
*Codex is ready. Allow workers to use it?* Settings → Agents & models has a row per agent with
its status (*Ready*, *Sign in needed*, *At its limit · resets …*), a meter of its plan's limits,
*Sign in* (which runs the agent's own sign-in, in your browser) and *Use*; Copilot's row has
*Install* and *Sign in* too. An agent older than the versions tested says so with *Update*, which
shows the agent's own updater, asks first and runs it. Claude workers also
need Anthropic's Agent SDK for Python; Chief of Staff installs the tested version by itself when
you turn Claude on (*Setting up Claude…*, usually under a minute).

**One is enough.** Chief of Staff needs one coding agent, not Copilot in particular. With
several, Copilot's row has *Use* too, and you can turn it off.

**Who does the work.** Settings → Agents & models → *Who does the work* has a row for *New
workers* and one for each of Chief of Staff's own sessions: the agent it runs on, its model and
its effort. For one task, type `@codex` or `@claude` on Home (a chip says *On Codex*; click it
for the model and effort), or just say it: "have Codex add rate limiting".

**Compare.** Pick two agents (`@codex`, then `@claude`), or say "do this with Codex and Claude":
the same task runs on each, a worker each in its own worktree (⇄ in the sidebar), and the Router
gives you both results side by side.

**Move work.** Until a worker has done anything, its chip offers *Restart on Claude*. After that,
*Continue with…* in its **⋯** hands its work to a new worker on another agent, from a summary of
what it did.

**When an agent can't run** (signed out, not installed, its plan's limit reached):

- **New work** starts on the next agent on that can, in the order Copilot, Codex, Claude Code,
  and its line in your chat says why. Work you named an agent for waits for it instead, with
  *Sign in* and *Continue with Copilot* on its card, and starts there by itself once it can.
- **Work in progress** that hits a usage limit pauses and says until when. Where the agent says
  when its limit resets, the worker goes on by itself then; *Continue with Copilot* (or another
  agent) moves it now. Nothing switches agents mid-task by itself.
- **The Router and Chief of Staff's own sessions** go on with the next agent that can, say so in
  one line, and go back by themselves once theirs can.

**Usage.** Each agent's row shows how much of its plan's limits is used and when they reset.
Copilot work counts in credits; Codex and Claude work in tokens, shown on each worker and in
*Usage*. A *Limit per worker* (unfold the agent's row) stops a worker once it has spent it.

**What differs.** Workers on Codex and Claude Code follow the same floor and the same
*Ask first* checks. Some things stay Copilot's: Copilot plugins, premium requests (credits),
learning from your past Copilot sessions, *Show where my other sessions stand*, and preparing
fixes in *Keep my PRs moving*. Codex can't run workers while the worker sandbox is on (its
sandbox hasn't passed Chief of Staff's checks yet).

Everything else, including signing in and installing from Chief of Staff, API keys, setup
failures, roles' fallback choices and downgrading: [Reference → Workers on other coding
agents](reference.md#workers-on-other-coding-agents).

## Settings

Settings (the gear at the bottom of the sidebar) has four pages.

| Page | What is there |
| --- | --- |
| **General** | *Code folder*; *Workers at once*; *Ask first* and *Sandbox* (see [Safety](#safety-and-privacy)); *Keep my PRs moving*; *A busy worker's new task starts at once*; *Daily summary*; desktop notifications. Under *More*: when finished workers close, when a task is *Overdue*, *Show where my other sessions stand*, update notices, other project folders, where task folders go |
| **Agents & models** | Each agent's status, sign-in and *Use*; *Who does the work*: the agent, model and effort for new workers and for each of Chief of Staff's own sessions; what happens when an agent can't run |
| **Connections** | *Slack*, *GitHub reviews*, *Watches*, *When you're away*, *Skills, plugins and MCP servers* (more for workers; plugins are Copilot's), *Send to Chief from the command line*, and *Pause watching* to stop all of them at once |
| **You** | *Learn from what I write*, *Learn now*, checks from mistakes; what was learned (writing styles, habits, checks, your profile); *Shared links* |

**Usage** (the chart icon) shows credits and tokens by day, task and agent. **Storage** (the disk
icon) shows free space and each worker's folder: Chief of Staff deletes build output when a
worker closes or idles for three days, alerts you when the disk runs low, and lets you clean or
keep a worker's files.

Workers on Copilot load what your own Copilot sessions load: your plugins, skills, MCP servers
and custom agents, your company's, and a repository's own; workers on Codex and Claude Code use
their agent's own configuration in the same way ([Reference → Workers on other coding
agents](reference.md#workers-on-other-coding-agents)). An MCP server a worker needs a sign-in for,
on any agent, gets a card under *Needs you* whose *Sign in* runs that sign-in for you.
Gitignored files a worktree needs (`.env`, `.env.*`, or what a `.worktreeinclude` lists) are
copied in from your checkout.

Environment variables (Slack token, timeouts, the Watchdog's timing, disk thresholds) are listed
in [Reference → Settings and environment
variables](reference.md#settings-and-environment-variables).

## Safety and privacy

Chief of Staff runs agents on your machine with your accounts. What keeps them in bounds, and how
firmly (the operating system, Chief of Staff, or only the agents' instructions), is in
[SECURITY.md](../SECURITY.md). In short:

**The floor, always, on every agent.** No worker comments on, reviews, merges or closes a PR as
you, opens, changes or comments on an issue, writes through `gh api` or GitHub's MCP server,
force-pushes a branch that isn't its own, or pushes to `main`/`master`. Chief of Staff checks
every command and tool call of every worker and refuses these; the worker asks you or hands you a
draft instead. It pushes its own branches and opens PRs.

**Your voice needs your OK.** Slack replies and PR reviews go out only from a draft you approve
(Chief of Staff refuses Slack's write tools; no agent is ever given your Slack token). The one
exception is the one you choose: a 🤖 reaction is your OK for the one reply to that thread.

**Your checkouts stay yours.** Workers work in their own worktrees. A worker that only reads your
checkout can't write there; a test it runs there runs in a copy.

**Ask first** (Settings → General; off by default) has workers ask before writes outside their
folders, `sudo`, any force push, piping a download into a shell, and other risky commands. Off,
workers act without asking, as your own sessions of those agents do; the floor holds either way.
Choose it per worker in its header's **⋯**. Workers that read Slack ask first unless you set
otherwise for one.

**Sandbox** (Settings → General; macOS and Linux) runs workers' commands in an OS sandbox: they
write only in their own folders, never see your GitHub token, and can't read your SSH keys. Codex
can't run workers while it is on (its sandbox hasn't passed Chief of Staff's checks yet). Workers
that read Slack are always sandboxed on Copilot and Claude, with the network limited to GitHub;
on Codex they run without a sandbox and ask first. On macOS, Copilot workers use `gh` in it once
Copilot's certificate is trusted: Settings says so, with *Trust the sandbox's certificate*, which
runs Copilot's two commands for you after asking. Turn it on for repositories you don't trust:
with it, settings a worker writes into its folder (hooks, MCP servers) never load without your
word.

**Privacy.** Everything runs on your machine and stays in your data folder
(`~/Library/Application Support/chiefofstaff` on macOS, `~/.local/share/chiefofstaff` on
Linux). There is no account and no telemetry: your code and prompts go only to the coding agents
you use, and Slack and GitHub are reached with your own sign-ins. The one call Chief of Staff
makes for itself is a look at its PyPI page once a day for a newer version, which sends nothing
about you (Settings → General → More turns it off). Content from Slack, GitHub and the web
reaches agents marked as data, not instructions; still, delegate only messages you would forward
to a colleague.

## Sharing

*Share…* on a finished result (in its card's **⋯**, or beside its line in a chat) makes a link
anyone can open in a browser, with nothing to install or sign in to.

1. The dialog shows the card exactly as others will see it. Whatever looks private (tokens, keys,
   passwords, internal addresses, email addresses, Slack links) is marked: *Remove* or *Keep*
   each one; *Create link* waits until every one is decided. Paths on your computer are
   shortened. Never shared: the worker's conversation, the changes themselves, cost, Slack
   threads.
2. Choose how long it works (1, 7 or 30 days) and *Create link*. The card is encrypted and kept
   in a secret gist of your GitHub account (the one signed in to `gh`); the key is only in the
   link, after the `#`, so GitHub never sees it.
3. Turn it off any time (*Share…* on the card, or Settings → You → *Shared links*); it stops
   working at once. When it expires, Chief of Staff deletes the gist.

Someone with Chief of Staff (0.1.58 or later) can open the link in their own, as a card on Home.
No GitHub account? *Download HTML file* saves the same card as a file. More: [Reference → Share
links](reference.md#share-links).

## Troubleshooting

**Chief of Staff is already running.** Started again on the same data folder, it prints the
running one's link and opens it. **Port 8765 is unavailable**: something else listens there;
start with another `--port`. To run a second Chief of Staff, give it its own `--port` and
`--data-dir`.

**Copilot is not signed in** (or another agent says *Sign in needed*). Run `copilot login` (or
use *Sign in* on the agent's row in Settings → Agents & models). With another agent on, Chief of
Staff goes on with it meanwhile and comes back to Copilot by itself.

**Your Copilot CLI is newer than Chief of Staff supports.** Nothing to do: Chief of Staff runs a
Copilot version it speaks, with the same sign-in, settings and sessions, and says so in one line.

**A worker seems stuck.** The Watchdog looks at it every minute and nudges it when it is. *Nudge*
on its row asks it where it stands now; *Stop* interrupts it, and *Go on* or *Go on with a
note…* resumes it.

**A message says *Not delivered*.** It was sent twice and the session never showed it; nothing
is lost without a word. Send it again, or open the worker to see where it stands.

**Work waits for memory or disk.** New work waits while less than 10% of memory is free, or the
disk is nearly full; running work goes on. Storage (the disk icon) shows what takes the room and
cleans build output.

**`uvx chiefofstaff` runs an old version.** uv runs what it cached first: use
`uvx chiefofstaff@latest`, or install it once with `uv tool install chiefofstaff`. `uv cache
prune` removes old cached versions; never run `uv cache clean` while Chief of Staff is running.

**An agent's settings in a folder changed** (with the sandbox on). A worker whose folder's hooks
or MCP servers changed waits, and its timeline says what changed; click **Use these settings**
if you trust the change.

**A session says *Reconnecting to Codex…*** (or its agent's name). Chief of Staff restarted while
it was at work; once its agent has it open again, it gets what that turn was given, once, and goes
on. It is tried again by itself; *Retry* tries now. Nothing is lost to a restart.

**A session says *Codex stopped answering; restarting it…*** (or Claude). Its agent hung (alive,
answering nothing): Chief of Staff ended it and started it again, and the turn goes on. After
*Stop*, it starts again with the next message.

**Chief of Staff won't start: its database is damaged, held, or empty.** The line it ends with
names the file and what to do: close the program holding it, or restore it from a backup (or move
it aside to start with a new, empty one).

**Update and restart** waits for the turns running to end (two minutes at most; one still running
then goes on after the restart; a worker waiting for your answer doesn't hold it up). If it fails, Home says why, and the version you had keeps running.
Installed into a system's Python, or on Windows, update with the command the sidebar shows.

**On Windows**, Send to Chief, the sandbox and folder sizes are unavailable, and only Copilot runs
workers. See [Reference → Installing and running](reference.md#installing-and-running).

Still stuck? [Open an issue](https://github.com/evanl666/chiefofstaff/issues) with what you did,
what you saw, and the version (`chiefofstaff --version`).

## FAQ

**Is a worker a real Copilot (or Codex, or Claude Code) session?** Yes. Copilot's show up in
`copilot --resume` as "Chief of Staff · …"; for a Codex or Claude worker, **⋯** → *Continue in a
terminal* copies its agent's own command.

**Does it work without Slack or GitHub?** Yes. Both are optional; without them you hand it work
on Home, by hotkey or from the command line.

**What does it cost?** Nothing itself. Each agent's work counts against your plan with it:
Home shows the Copilot credits each task used, and Usage every agent's.

**Can I keep using my agents directly?** Yes. Sessions you start in a terminal are yours; Chief
of Staff lists them under *Other sessions* and writes to none that is open elsewhere.

**Where is my data, and how do I remove it?** In your data folder (`Data:` at startup says
where), the task folders (`.chief/` in your code folder, unless you moved them) and, for
conversations, where each agent keeps its sessions. Stop Chief of Staff and delete the first two
to remove what it made; your own checkouts were never changed.

**How do I go back to an earlier version?** Stop Chief of Staff and run `chiefofstaff
--prepare-downgrade` first when workers ran on Codex or Claude Code; then install the version
you want.

**Does it run on a server?** It is made for your own computer: it listens on 127.0.0.1 only,
behind a private link.
