# Security

Chief of Staff runs AI agents on your machine with your accounts. This page says what keeps
them in bounds, and how firmly: some boundaries are enforced by the operating system, some by
Chief of Staff, and some only by the agents' instructions. Please read it before you use
yolo (on by default), Slack or GitHub.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: **Security → Report a vulnerability**
on this repository. Do not open a public issue. You will get an answer within a week.

## What is at stake

- Your code and repositories, and your GitHub account (through `git` and `gh`).
- Slack: reading your messages, and speaking as you.
- Services that agents sign in to through MCP servers.
- Your machine: files, credentials, network.

The main threat is **instructions hidden in content an agent reads**: a Slack message, a pull
request comment, a web page, a file in a repository. A secondary one is an agent that simply
makes a mistake.

## Boundaries

| Boundary | Enforced by | Holds when |
|---|---|---|
| A worker's shell commands write only to its workspace, its checkouts and temporary folders, plus npm's package store and Yarn's archives (whose contents those tools check against lockfiles before use; in a Node project Copilot itself also opens npm's whole cache folder) and folders you add under *Also writable in the sandbox*. Caches whose contents run as they are (Gradle and Maven jars, pre-commit hooks) are read-only | **OS sandbox** (Copilot's, Seatbelt on macOS, bubblewrap on Linux) | Sandbox is on (Settings; off by default since 0.1.13, always on for workers that read Slack) and the machine supports it. *Workers use your package caches* off keeps npm's and Yarn's out of reach too; workers that read Slack never get them. A folder you add under *Also writable* can change what your own tools run |
| A worker cannot change a repository's hooks or config, its submodules' config, or your other worktrees (all of which can run code when you use git there) | **OS sandbox** | Sandbox is on. Treat the worker's own folder like a stranger's pull request: running anything there, git included, runs what the worker wrote |
| Workers never see your GitHub token; `git` and `gh` authenticate through Copilot's local proxy | **OS sandbox** + Copilot's credential proxy | Sandbox is on |
| Workers cannot read `~/.ssh` keys or other files outside their folders' read rules | **OS sandbox** | Sandbox is on |
| Workers that read Slack reach only GitHub hosts over the network, and not localhost. Other workers may use localhost (servers their tests start, their build daemon) and so reach your local services; the tokens of your Gradle daemons and Jupyter servers are unreadable to them, and Unix sockets (Docker's) stay closed | **OS sandbox** | Sandbox is on |
| The Router, the Slack session and the Watchdog have no shell, file or web tools | **Copilot** (session tool allowlist) | Always |
| Slack messages are sent only after you approve a draft | **Chief of Staff** (Slack write tools are refused; only the app sends) | Always for the Slack tools; sessions are never given your Slack token |
| PR reviews are published only after you approve a draft | **Chief of Staff** (permission check refuses GitHub MCP writes, `gh pr review/merge/comment` and `gh api` writes) | Yolo is off for the worker (it is on by default, except for workers that read Slack). With yolo on, a worker's `gh` can still write to GitHub |
| No force pushes, no pushes to `main`/`master`, no `sudo`, no `curl \| sh` | **Chief of Staff** (permission check on each command) | Yolo is off for the worker (on by default, except for workers that read Slack). These are pattern checks, not a sandbox: an agent determined to evade them can |
| A worker does not write outside its folders with Copilot's file-edit tool | **Chief of Staff** (permission check) and Copilot (best effort) | Yolo is off for the worker |
| The connector (watches, notifications when you are away) only looks, and messages nobody but you | **The model**, guided by instructions; it runs as a worker that reads Slack (sandboxed, permission checks on, Slack writes refused) | As far as the model follows its instructions. It can use every MCP server your Copilot has, including ones that can send: give it a way to reach you that can message only you if you can |
| A message cannot pretend to come from you or the Router | **Chief of Staff** (envelopes: only the app writes the sender; text inside is escaped) | Always |
| Only your user can send to Chief of Staff with `chiefofstaff send`, and only a request, never an approval | **OS** (the socket and its folders) and **Chief of Staff** (who connected, checked at both ends; two routes) | Send to Chief is on (off by default). Programs running as you can send too (see [Send to Chief](#send-to-chief)) |
| Content from Slack, GitHub and the web is treated as data, not instructions | **The model**, guided by marked envelopes (`trust="untrusted"`) | As far as the model follows its instructions |
| Only your own reaction delegates a Slack message (others reacting with the same emoji do nothing) | **Chief of Staff** (the watcher searches `hasmy::emoji:`, your reactions only) | Always |
| A delegated message is acted on for what it asks, but risky requests in it are confirmed with you | **The model**, guided by instructions | As far as the model follows its instructions. Delegate only messages you would forward to a colleague |
| Workers stay on their task, don't merge PRs, don't speak publicly as you | **The model**, guided by instructions; the Watchdog flags drift | As far as the model follows its instructions |

**Yolo** turns off Chief of Staff's permission prompts and checks for workers. It is on by
default, as in your own Copilot sessions, except for workers that read Slack (they keep the
checks unless you choose yolo in their header); turn it off in Settings, or per worker with
*Ask first*. With the sandbox on, the OS boundaries above still hold. With the sandbox off (or unsupported), yolo means a
worker can do anything your user account can. Yolo is on by default since 0.1.18, as your own
Copilot sessions usually run: if workers read content you do not trust (PR comments from
strangers, say), turn it off in Settings.

**Abilities you add** (Settings → Connections: skills, plugins, MCP servers) run as they would
in your own Copilot sessions: a plugin's hooks and an MCP server run as you, outside the
sandbox. Add only what you would install for yourself.

## Sign-ins and credentials

- **MCP sign-ins are shared.** When a worker asks you to sign in to an MCP server, Copilot keeps
  that sign-in, and every later session can use the server with your identity. Sign in only to
  servers you want all your workers to use. Remove a sign-in with `copilot` (`/mcp`) or in your
  OS keychain (`copilot-mcp-oauth`).
- **Your Slack token** is stored in the OS keychain and given only to Copilot as an MCP header;
  sessions receive only read-only Slack tools.
- **GitHub**: workers use your `gh` and `git` sign-in (through the sandbox proxy when it is on).
- Chief of Staff removes its own `CHIEFOFSTAFF_*` environment variables before starting Copilot.
  Other secrets in your environment are visible to unsandboxed commands.

## The web UI

- It listens on `127.0.0.1` only, and answers only requests addressed to `127.0.0.1` or
  `localhost` (a page on another name that resolves there is refused).
- Opening it needs the key in the private launch URL, new each time Chief of Staff starts (also
  kept in the data directory's `launch-url` file, readable only by you). The page keeps the key in
  its own storage and sends it in a header with every request, or in the address only for the
  event stream, where a browser cannot send a header and which is never shown or opened. Images
  and files you attached are fetched with the header and shown from addresses of the page itself,
  so no link, copied address or history entry carries the key; the server accepts the key in no
  other address. It is never a cookie: a browser would send a cookie for `127.0.0.1` to every
  server on it.
- Requests that change anything must come from the same origin; responses carry a strict
  Content-Security-Policy.

## Send to Chief

`chiefofstaff send` (off by default; Settings → Connections) is a way in, so it is narrow:

- **Where:** a Unix socket, `send/socket` in the data directory, open only while the setting is
  on and Chief of Staff runs: no network port, and no web page can reach a Unix socket (a
  request carrying an `Origin` is refused anyway). Turning the setting off closes it and removes
  the file.
- **Who:** Chief of Staff opens it only when the data directory and every folder above it are
  owned by you or root and writable by no one else (no group or other write bit, no macOS
  access control list letting others write), in a `send/` folder of yours (`0700`, its access
  control list removed), the socket itself `0600`. It removes only a socket of yours left by a
  Chief of Staff that stopped; anything else at those paths is left alone and Settings says why
  the socket stays closed. On each connection, the user id of the process connecting is checked
  before anything is read, and another user's connection is closed. The command checks the same
  folders, that the socket is yours and that the process answering runs as you, before it
  writes a byte: *Something other than your Chief of Staff is at …: nothing was sent*.
- **What:** two routes, `POST /v1/send` and `POST /v1/hello` (the API version); one request a
  connection, at most 64 KB, read within 5 seconds; at most 10 items a minute and 300 a day.
  Nothing there reads state, chats or results, answers a card, approves a draft, merges,
  cancels or changes a setting. An item can at most start work, as a message on Home can.
- **As what:** the note you type is wrapped as yours (`from="owner"`); a link, its title and
  text are wrapped `from="capture" trust="untrusted"`, escaped, by Chief of Staff (a client
  cannot send envelope text). The Router treats both as delegated: risky requests, the note's
  too, are confirmed with you first. A link to this computer (`127.0.0.1`, `localhost`, `::1`)
  loses its query and fragment. On Home, what was passed along shows in a code block, never as
  formatting or a link.
- **No key, on purpose:** the checks above are the credential. A key file would add nothing, as
  any program able to read it runs as you and could connect anyway.
- **Limits:** programs running as you can use the socket, unsandboxed workers included, as they
  can read `launch-url` and the database today; what they send is handled as delegated and Home
  shows where each item came from. Sandboxed workers cannot reach the data directory or Unix
  sockets. Sent work runs under your settings like any task: with yolo on and the sandbox off (the
  defaults), passed text that persuades a worker reaches what your account can. What Chief of
  Staff receives goes to GitHub Copilot under your account, as every message to the Router does.
  curl and other tools that speak HTTP over the socket do not check who answers; the command
  does.

## Your data

Everything stays on your machine: a SQLite database and work folders in the data directory
(owner-only permissions), plus Copilot's own session storage (`~/.copilot`). Chief of Staff sends
no telemetry. GitHub Copilot itself works under GitHub's terms and settings.

To learn how you write (Settings → *How you work*), Chief of Staff stores your own words in that
database: Slack messages you sent, your PR reviews and review comments, your commit messages and
what you asked your own Copilot sessions. Others' messages are not kept. The learning worker
that reads them has no tools but its own (no shell, files or web). Turn the setting off to stop
reading them.

Checks learned from workers' mistakes are stored there too; the versions they were proven on are
git commits under `refs/chiefofstaff/` in your repository (never pushed). A check runs only in a
sandbox: no network, no writes outside a scratch folder, and no reads in your home folder other
than the repository. Proposed checks run on those versions before you see them; afterwards only
the checks you approved run.

## Known limits

- Pattern checks on shell commands are guardrails, not isolation. Unless you turn the sandbox on,
  workers that do not read Slack run as your own Copilot sessions do: with your access.
- Unsandboxed commands run as you: they can reach whatever your account can, including
  keychain items that do not ask for a password.
- The sandbox does not confine Copilot's built-in file edits as strictly as shell commands
  (Copilot applies the same policy on a best-effort basis); Chief of Staff's permission check
  covers them while yolo is off.
- A worker in a shared project folder (not isolated) writes to that folder itself.
- MCP servers are not sandboxed: they are what you or your company configured for Copilot
  (including your plugins' and, in a repository you trust, the repository's own), and run as they
  do there. A worker can use any MCP server Copilot offers it. A worktree of a repository you
  trust in Copilot is added to Copilot's trusted folders.
- Workers that do not read Slack can search Copilot's record of your past sessions, as your own
  sessions can. The Router, the Slack session and workers that read Slack are not recorded
  there (from 0.1.13; earlier versions recorded them), so Slack messages do not reach workers
  that way.
- Agents can be persuaded. Approve drafts with care, and keep yolo off for workers that read
  untrusted content.
