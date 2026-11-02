# Security

Chief of Staff runs AI agents on your machine with your accounts. This page says what keeps
them in bounds, and how firmly: some boundaries are enforced by the operating system, some by
Chief of Staff, and some only by the agents' instructions. Please read it before you turn on
yolo, Slack or GitHub.

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
| A worker's shell commands write only to its workspace, its checkouts and temporary folders | **OS sandbox** (Copilot's, Seatbelt on macOS, bubblewrap on Linux) | Sandbox is on (Settings, default) and the machine supports it |
| A worker cannot change a repository's hooks or config, its submodules' config, or your other worktrees (all of which can run code when you use git there) | **OS sandbox** | Sandbox is on. Treat the worker's own folder like a stranger's pull request: running anything there, git included, runs what the worker wrote |
| Workers never see your GitHub token; `git` and `gh` authenticate through Copilot's local proxy | **OS sandbox** + Copilot's credential proxy | Sandbox is on |
| Workers cannot read `~/.ssh` keys or other files outside their folders' read rules | **OS sandbox** | Sandbox is on |
| Workers that read Slack reach only GitHub hosts over the network | **OS sandbox** | Sandbox is on |
| The Router, the Slack session and the Watchdog have no shell, file or web tools | **Copilot** (session tool allowlist) | Always |
| Slack messages are sent only after you approve a draft | **Chief of Staff** (Slack write tools are refused; only the app sends) | Always for the Slack tools; sessions are never given your Slack token |
| PR reviews are published only after you approve a draft | **Chief of Staff** (permission check refuses GitHub MCP writes, `gh pr review/merge/comment` and `gh api` writes) | Yolo is off. With yolo on, a worker's `gh` can still write to GitHub |
| No force pushes, no pushes to `main`/`master`, no `sudo`, no `curl \| sh` | **Chief of Staff** (permission check on each command) | Yolo is off. These are pattern checks, not a sandbox: an agent determined to evade them can |
| A worker does not write outside its folders with Copilot's file-edit tool | **Chief of Staff** (permission check) and Copilot (best effort) | Yolo is off |
| A message cannot pretend to come from you or the Router | **Chief of Staff** (envelopes: only the app writes the sender; text inside is escaped) | Always |
| Content from Slack, GitHub and the web is treated as data, not instructions | **The model**, guided by marked envelopes (`trust="untrusted"`) | As far as the model follows its instructions |
| Only your own reaction delegates a Slack message (others reacting with the same emoji do nothing) | **Chief of Staff** (the watcher searches `hasmy::emoji:`, your reactions only) | Always |
| A delegated message is acted on for what it asks, but risky requests in it are confirmed with you | **The model**, guided by instructions | As far as the model follows its instructions. Delegate only messages you would forward to a colleague |
| Workers stay on their task, don't merge PRs, don't speak publicly as you | **The model**, guided by instructions; the Watchdog flags drift | As far as the model follows its instructions |

**Yolo** turns off Chief of Staff's permission prompts and checks for workers. With the sandbox
on, the OS boundaries above still hold. With the sandbox off (or unsupported), yolo means a
worker can do anything your user account can.

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

- It listens on `127.0.0.1` only. Opening it needs the launch URL's one-time token, which becomes
  an `HttpOnly`, `SameSite=Strict` cookie.
- Requests that change anything must come from the same origin; responses carry a strict
  Content-Security-Policy.

## Your data

Everything stays on your machine: a SQLite database and work folders in the data directory
(owner-only permissions), plus Copilot's own session storage (`~/.copilot`). Chief of Staff sends
no telemetry. GitHub Copilot itself works under GitHub's terms and settings.

## Known limits

- Pattern checks on shell commands are guardrails, not isolation. Keep the sandbox on.
- Unsandboxed commands run as you: they can reach whatever your account can, including
  keychain items that do not ask for a password.
- The sandbox does not confine Copilot's built-in file edits as strictly as shell commands
  (Copilot applies the same policy on a best-effort basis); Chief of Staff's permission check
  covers them while yolo is off.
- A worker in a shared project folder (not isolated) writes to that folder itself.
- MCP servers are not sandboxed: they are what you or your company configured for Copilot, and
  run as they do there. A worker can use any MCP server Copilot offers it.
- Agents can be persuaded. Approve drafts with care, and keep yolo off for workers that read
  untrusted content.
