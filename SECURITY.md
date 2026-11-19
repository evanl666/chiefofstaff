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
| A worker's shell commands write only to its workspace, its checkouts, its own temporary folder (beside workers' caches) and the system's, folders you add under *Also writable in the sandbox*, and **workers' own package caches** (in your cache folder: `~/Library/Caches/chiefofstaff/workers` on macOS, `~/.cache/chiefofstaff/workers` elsewhere): nothing else in your home. Copilot's *dev-tool access*, which opened your package caches read-write (npm's, pnpm's, Yarn's, Bun's, pip's, uv's, poetry's, Go's, Cargo's, Gradle's and its distributions, Maven's, Playwright's browsers…), is off for sandboxed workers. Chief of Staff grants instead, read-only and by name (nothing Copilot's own profile lets every command read already: `/usr`'s folders, `/opt/homebrew`, `/Library`…), and nothing of its own code outside a folder they write: their toolchains (every folder on your `PATH`, nvm's, pyenv's, SDKMAN's, asdf's, mise's, Volta's toolchains, rustup's for a Rust project, JDKs, uv's Pythons and tools, the homes `JAVA_HOME`, `GOROOT` and the like name) and, to workers on your own tasks only, the few of your caches they read where they are (Gradle's dependency cache, Maven's repository and Go's module downloads, read through; Playwright's browsers; pre-commit's hook environments; node-gyp's headers; Yarn's global cache and pnpm's store, which a checkout's packages load from: the rest they have copies of) and what their builds need of a private registry, as dev-tool access gave them (`~/.npmrc`, `~/.yarnrc.yml`, `~/.m2/settings.xml` and `settings-security.xml`, pip's, uv's, poetry's and Go's configuration, `~/.netrc`, Cargo's credentials, Gradle's init scripts, Bundler's, NuGet's, npm's global `npmrc`; they hold registry tokens; mise's global configuration too, whose `[env]` often does). A worker that reads Slack reads of a toolchain only what runs (`bin`, `lib`, `libexec`, `share`, `include`; not the folder above a `bin` on `PATH`, nor a tool home's caches), and every node install's `etc/npmrc`, Maven's `conf/settings.xml`, Homebrew's `var`, `etc/npmrc` and `etc/gitconfig`, the system's registry settings (`pip.conf`, `uv.toml`, `/etc/npmrc`, `/etc/gitconfig`), a virtualenv's or conda environment's `pip.conf`/`condarc`, and your caches within what it reads (each found where your tools keep it) are denied to it; of a folder on `PATH` that holds a cache (pnpm's home, beside its store) it reads only what is not a cache, and of pnpm's store only the entries its global tools use (resolved as it opens; none past 40). Each worker's init script (it may name a GOPROXY with a token) is in a folder only that worker reads, deleted as it closes. No worker can plant code your own builds later run. Chief of Staff, which runs unsandboxed, never writes through a link a worker made where workers write (their caches, temporary and own folders): it opens each folder without following links and writes by name in it; checkouts are made in a folder of its own and moved into place in one rename, and a worktree it removes goes out of reach in one rename before it is deleted | **OS sandbox** (Copilot's, Seatbelt on macOS, bubblewrap on Linux) | Sandbox is on (Settings; off by default since 0.1.13, always on for workers that read Slack) and the machine supports it. Workers on your own tasks share their caches: one could plant code another worker then runs, inside its sandbox. No configuration file makes anything writable: Chief of Staff reads only your own, in home (never a project's), to find your caches to copy from and, read-only, Maven's repository and Go's modules to read through; the variables it sets for workers take precedence over a project's configuration (npm, pnpm, pip, uv, NuGet…), and a tool a project points at your caches anyway (Yarn's `cacheFolder`, Maven's `.mvn/maven.config`) fails there. A folder you add under *Also writable* can change what your own tools run |
| No worker's command can change Chief of Staff's own code: its environment (where `uvx` or `uv tool install` put it, in uv's cache or not), the Python it runs on, every folder it imports from, its `chiefofstaff` command, the `pyproject.toml` and `uv.lock` of a source checkout it runs from, and the entries of uv's cache its packages come from are read-only to every sandboxed worker | **OS sandbox** | Sandbox is on. With the sandbox off, workers run as you do. A worker you asked to work in place in Chief of Staff's own checkout may change what is in it |
| Workers that read Slack have caches of their own in their workspace, shared with no other worker, can't read your package caches, and can't read the registry and credential files build tools use (`~/.npmrc`, `~/.m2/settings.xml`, `~/.gradle/gradle.properties`, `~/.pypirc`, Cargo's credentials, pip's and uv's configuration, `~/.netrc`, Docker's `config.json`, `~/.git-credentials`, `gh`'s `hosts.yml`, Bundler's, RubyGems', Go's, AWS's, Azure's, gcloud's and Kubernetes' configuration, npm's global `npmrc` and the like, also where an environment variable moves them) | **OS sandbox** | Always (they are always sandboxed). A file created after such a worker started is closed from its next start |
| A worker cannot change a repository's hooks or config, its submodules' config, or your other worktrees (all of which can run code when you use git there), nor create the files git would read in their place (`.git/commondir`, a missing `hooks/`, a `config.worktree` where the repository reads one, a missing `modules/` where it has submodules) | **OS sandbox**, as Chief of Staff sets it: Copilot's sandbox holds a listed path only once it exists, so one that does not is denied (macOS). On Linux it cannot hold one that does not exist (it leaves an empty folder in its place, which breaks git), so **Chief of Staff** looks for those after each of the worker's turns, as it stops or closes, and before it runs git itself: when one appeared, the worker waits for you, and Chief of Staff runs no git in that repository, until you click *Use it* (nothing is moved out of your repository; in the private folder of a worktree of Chief of Staff's, *Keep it removed* moves it aside into its data folder). The sandbox's lists are paths, and a folder moved into place brings what it holds past them (a worker in your checkout itself renames its `.git` and makes another): **Chief of Staff** keeps the identity of each `.git` folder a worker's commands could replace (your checkout's, when it works there itself; `objects`, `objects/info` and the worktrees' private folders of each repository it commits to) as it first sees it, and compares it before and after each turn and before it runs git there; one replaced makes the worker wait, with no git run there by Chief of Staff, until you click *Use this .git*. Chief of Staff's own git never looks into the files of a submodule in a worker's tree (a worktree's submodules keep their git folders in its own folder in the repository, which its worker can write, so a filter configured there would run as you): it reads their commits only, leaves them out of its snapshots as the index holds them, counts one checked out as holding work, and runs no git at all in a repository the worker made itself unless it is one of its checkouts. Chief of Staff's git never reads a git config a worker can write. A clone in a worker's folder (one Chief of Staff made there, or the worker's own, taken as a checkout at its task's end) and a submodule checked out in a worktree (whose git folder is in the worktree's own, which its worker can write) are read through a git folder of Chief of Staff's own, in its data folder: their objects by hash, their HEAD, refs and index copied in as data, a config Chief of Staff writes (how files are kept on disk, and the remote it recorded when it took the clone, where a PR's push goes). Their hooks, `info/attributes` and config never apply, so a worker can't change what that git runs even between Chief of Staff's looks. Their replace refs (`refs/replace/`, which make git read one commit as another) are never copied, and no git Chief of Staff runs uses one anywhere (`GIT_NO_REPLACE_OBJECTS`); a reftable's tables are copied as data too; an index naming a path out of its tree (`..`, an absolute one) or in a `.git` is not used (git would write there as it checks out): one is made from the last commit instead, and the log says so. Their files are read with no filter at all (your own git-lfs's included: a file a filter keeps is said as not checked, not counted; a filter only the repository's own config defines is none, its files read as they are), their gitattributes' own conversions (eol, text) applied; no object is fetched as it is read and no transport is used. A clone's `.git` must be a folder (not a `gitdir:` file or a link); one the worker made again where it was is read as the clone now, and its timeline says so. In Chief of Staff's own worktrees, its git leaves each submodule's path out where it reads files (git would otherwise read that submodule's config to tell its commit), and no push or fetch of its own goes into a submodule, whatever your config says (`submodule.recurse`). A submodule that can't be read so (reached through a link, its HEAD unreadable), or files a filter keeps that may have changed, keep a PR from opening until you say so, with the reason; a submodule's commit staged and not committed keeps it from opening. Every git Chief of Staff runs reaches another repository only through http(s), ssh, git or a local path, whatever a repository's config allows (a remote helper such as `codecommit::` is not used by its own fetches and pushes); from a repository a worker can write, not a local path either, and a PR's push from a clone in a worker's folder goes only to GitHub's address for the repository, made by Chief of Staff from its owner/name. Its diffs, shows and logs run no external diff or textconv program and show a submodule as the commit it is at (`--submodule=short`, whatever your `diff.submodule` says), never opening its repository | Sandbox is on. On Linux, one the worker makes is there until the next look: a git you run there meanwhile (yours, or your editor's) reads it, and one a command it left running makes after its turn is found at the next look; one your own git makes while a worker has that repository (`git sparse-checkout set` where worktree config is already on) makes the worker wait too, until you click *Use it*. A `.git` replaced is found at the next look too: a git you run there before that runs what the worker put in its place, and one put back within a turn is not seen at all. A worktree removed and made again under its name (`git worktree remove`, `add`: yours, or a worker's scratch one) is no replaced one: its folder is new too; but not one handed to a worker, which a worker's commands could remove and make again themselves. On a network disk whose identities change by themselves, the worker may wait for nothing. Treat the worker's own folder like a stranger's pull request: running anything there, git included, runs what the worker wrote. In Chief of Staff's own worktrees, a `.gitattributes` the worker writes can still name a filter your own git config defines (git-lfs's): Chief of Staff's git then runs your own tool on the worker's files |
| Hooks, MCP and language servers, and agents a worker writes into its folder's settings never load for it (Copilot's `.github/hooks/`, `.mcp.json`, `.github/mcp.json`, `.github/lsp.json`, `.github/copilot/`, `.github/extensions/`, custom agents in `.github/agents/` and `.claude/agents/` (which can declare their own MCP servers and hooks), and the hooks of `.claude/settings*.json`; Codex's `.codex/`), nor do the files their hooks run, as far as their commands name them (a hook's script). What a server runs (an MCP, language or custom agent's server: a build's output, its source) never runs changed: that server is off for the worker. They would run outside Chief of Staff's checks: a hook can approve what a check refuses (hooks run in a sandboxed worker's sandbox), an MCP or language server runs unsandboxed | **OS sandbox**, as Chief of Staff sets it: a sandboxed worker's commands cannot change those settings in your checkout (`.claude`'s settings one by one: its commands, skills and rules stay writable), nor, in its own worktree or workspace, the MCP server files Copilot loads by itself mid-session (`.mcp.json`, `.github/mcp.json`), nor anywhere the files their hooks run, nor (macOS) create one there. Its other settings there load only as its session starts or resumes, or through one of its tools, both of which Chief of Staff checks first (below). Before it updates its branch it checks that the update touches none of those it can't change, and when it does it leaves that to you in one line; when an update fails partway anyway on a file it can't change, the worker is told at once, in one line, to put its tree back before going on (words only: Chief of Staff runs no git in a worker's repository for this, where a filter or a partial clone's fetch could run what the worker wrote). Its edits to any of them are refused, whatever their case, sandbox or not. What a server runs stays the worker's to build and edit. **Chief of Staff**: when it makes a folder for a worker (a worktree, a clone, a workspace), or hands a worker your checkout for the first time (no worker that could write there has had it; your own session taken up from a terminal counts), it keeps what those files hold, in its own data, never in the folder, and says once on the worker's timeline which settings workers there use. Files a worktree carries from your checkout (`.worktreeinclude`) count only as your checkout holds them (once you said to use carried ones, those are). Any other folder it sees with such files in it (a folder of a checkout workers already had, one workers had before 0.1.46) waits for you to look at once. Before each start and each turn (one it sends, or one Copilot starts by itself), as each of the worker's tools starts, and as a sub-agent starts, it glances at them (sizes and times, no file read); for a sandboxed worker, Copilot asks Chief of Staff before each tool (a sub-agent's too), after the folder's own preToolUse hooks, and waits for its answer, which stands over their approval (a custom agent or a language server it put in place is denied before it loads). That answer comes within 2 seconds whatever happens (Copilot itself waits for it: over 5 minutes, checked on 1.0.100 and 1.0.112): when Chief of Staff can't look in time, or can't look at all, the tool is denied with a line saying so (the turn goes on), and a look that fails as a model round starts stops the worker until its folder can be checked. Its checks read regular files only (a FIFO or a device is never opened, nor waited on), in threads of their own (its own git's guard in others), at most 16 under way at once (past that a tool is denied at once: it can't check right now), and a look that takes over 15 seconds counts as one that can't be checked: the worker waits. When that glance finds a change, it reads them again (plain file reads: no git, whose config in a worker's folder could run code), from the top it recorded as it handed the folder out (a `.git` folder a worker makes lower down changes nothing), and compares. A Copilot worker whose settings differ starts no turn there (one at work is stopped, the tool about to run denied) until you click *Use these settings* on the line its timeline shows; a Codex worker's thread does not trust that folder. What a Copilot server runs is compared too, but never waited for: when it changed, that server is off for the worker from its next start (Copilot doesn't start that MCP server, a custom agent's included; the tool that would start that language server, or run that custom agent by its name as Copilot matches it, is denied, for a sandboxed worker from the tool after the change), and its timeline says so once, with *Use these settings* to turn it on again. A file a hook names that isn't there yet is a change once made. A settings file linked out of the folder is compared by what it leads to, and one leading into a folder workers can write is never used; a program a setting runs through such a link (a virtualenv's `python`) counts as one found on your PATH; a link to an agent's own folder (`~/.claude`), or a special file (a FIFO, a socket, a device), is never used: the worker waits until it is removed | Always for the check; the sandbox's part when it is on. Copilot loads a folder's hooks, plugins and extensions only as a session starts or resumes, a custom agent (1.0.109 and later) or a language server mid-session only through a tool, and its MCP server files at times by itself, within seconds of a change (seen on Copilot 1.0.100 and 1.0.112, not every time). It doesn't start an MCP server again once it exited, and a sandboxed worker's commands can't signal one (checked on both). A worker without the sandbox runs its commands as you and can write anywhere: what it changes is found as its next tool starts (Copilot doesn't wait for that) and before its next turn, and a server it adds to an MCP file, or a custom agent it adds and calls at once, may run before that. The sandbox's lists are paths (a worker that renames a folder above them moves them out from under them): the checks compare contents, wherever they came from, but an MCP file brought in that way (`mcp.json` in a folder renamed over `.github`, or `.mcp.json` in a folder put in place of the worker's own worktree, whose workspace it can write) can have its server started by Copilot, outside the sandbox, before any check (macOS); on Linux, whose sandbox cannot hold a path that is not there, so can an MCP file a sandboxed worker makes where there was none. A hook's script swapped mid-turn (a folder moved into place) runs once, in the worker's sandbox, before Chief of Staff's answer denies the tool. The glance looks at each setting's own path, 1,000 entries and four folders deep (past that, a change is found at the next start or the next turn Chief of Staff sends, which read them all). A server's change applies from its next start: one already running goes on (one that reloads its own code, in a watch mode, runs the worker's edits as they are made), and for a worker without the sandbox a custom agent or language server that starts mid-turn after a change starts as it is then. The files a setting runs are held as far as its commands name them (a script, `bash -c`, `cd x && ./y`, `pushd`, `. file`, a hook's `cwd`, a folder's entry point, `python -m` and `-c` modules, `node -r`, `BASH_ENV`, `NODE_OPTIONS`); what those files import (a module put beside a held Python script is imported first), and project code a command runs by a name (`uv run`, `npm run`, `npx`), are not, and what the parser does not follow (a loop, a pipe into a shell, a command substitution, inline code, a PATH into the project, words it can't resolve) is never dropped: the worker's timeline says once when a setting runs such code. Ones too many or too large to check make the worker wait too: *Use these settings* lets it use them unchecked, as far as they could be read then; when those change, it waits again. Agent homes (`~/.codex`, `~/.claude`, `~/.copilot`) are left out of your home folder's own settings. A turn Copilot starts by itself (from its own queue) is stopped as it starts. Skills (`.github/skills/`, `.claude/skills/`, `.agents/skills/`) are instructions only: their own MCP servers and hooks never load, and their allowed tools still ask Chief of Staff |
| Workers never see your GitHub token; `git` and `gh` authenticate through Copilot's local proxy | **OS sandbox** + Copilot's credential proxy | Sandbox is on |
| Workers cannot read `~/.ssh` keys or other files outside their folders' read rules | **OS sandbox** | Sandbox is on |
| Workers that read Slack reach only GitHub hosts over the network, and not localhost. Other workers may use localhost (servers their tests start, their build daemon) and so reach your local services; the tokens of your Gradle daemons and Jupyter servers are unreadable to them, and Unix sockets (Docker's) stay closed | **OS sandbox** | Sandbox is on |
| The Router, the Slack session and the Watchdog have no shell, file or web tools | **Copilot** (session tool allowlist) | Always |
| Slack messages are sent only after you approve a draft | **Chief of Staff** (Slack write tools are refused; only the app sends) | Always for the Slack tools; sessions are never given your Slack token |
| PR reviews are published only after you approve a draft | **Chief of Staff** (permission check refuses GitHub MCP writes, `gh pr review/merge/comment` and `gh api` writes) | Yolo is off for the worker (it is on by default, except for workers that read Slack). With yolo on, a worker's `gh` can still write to GitHub |
| No force pushes, no pushes to `main`/`master`, no recursive delete of home or root, no `sudo`, no `curl \| sh` | **Chief of Staff** (permission check on each command) | Yolo is off for the worker (on by default, except for workers that read Slack). These checks read commands as text, which can't be fully parsed: the push and delete checks are a best effort, not a sandbox, and an agent determined to evade them can (protect your default branch on the server, too) |
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

## Workers on other coding agents

Workers can also run on Codex or Claude Code (off until you allow one, on Home's card or in
Settings → *Coding agents*). For such a worker:

- **Sandbox.** A Claude worker runs in Claude's own sandbox, set from Chief of Staff's policy for
  a Copilot worker (writes only in its folders, protected paths read-only, your service secrets
  unreadable, the network as Settings says), only on a Claude version whose check showed a write
  outside its folder, a read of a protected file and a request to a host off the list all
  failing, with settings of the owner's that try to switch its sandbox off or weaken it in place
  (Chief of Staff's own settings win over those). Claude merges the lists of its sandbox settings
  across your settings, a trusted project's and managed settings: a sandboxed worker does not
  start where any of them adds to those lists or sets a proxy port, allows edits by an absolute
  path or adds directories (Claude makes those writable in its sandbox), or, for a worker whose
  network is limited, allows web domains, nor where managed settings weaken it at all. Those
  files are read as Claude reads them (a byte-order mark, any size; over 16 MB, the worker does
  not start). A sandboxed Claude worker runs no hooks at all (`disableAllHooks`): Claude runs
  hooks outside its sandbox (yours, a trusted repository's, its skills' and subagents', a
  plugin's) and can turn off only all of them, not some (checked on 2.1.322: managed-only and
  plugin-only hooks are read from managed settings only); Chief of Staff's own checks are not
  hooks of Claude's and still run. Codex workers never run while the worker sandbox is on
  (Codex's sandbox is not checked yet); with it off, a Codex worker can do anything your
  account can (`danger-full-access`), as Copilot workers then can.
- **Your checkouts.** Every edit asks Chief of Staff first (Codex's approval, Claude's
  `PreToolUse` hook), wherever it lands, a file move's destination included, and the same check
  as for Copilot workers keeps your own checkouts read-only to them; one of your own Codex
  `PermissionRequest` hooks can approve an edit first.
- **Ask first** (Codex versions before 0.168; Claude from 2.1.322): every command, edit and MCP
  call asks Chief of Staff, which refuses what it refuses for a Copilot worker (`gh` writes and
  GitHub MCP writes among them, so PR reviews stay drafts); Claude's commands are read as the
  shell Claude runs them in. Codex: ChatGPT's apps and its subagents (which could open another
  worker's conversation) are off, and your own Codex rules and hooks can allow what those checks
  would refuse. Claude: your own Claude hooks are off (one could approve what Chief of Staff
  refused); its subagents are checked as the worker; a cloud agent or a routine (which would run
  where no check reaches) needs your OK each time, and is refused to a worker that asks first or
  has a token limit. Chief of Staff checks once per version of each agent, on this machine,
  without a model, with only an allowlisted environment and every connection caught by a proxy
  of its own on 127.0.0.1, that each kind of action asks it first; a version that fails runs no
  worker that asks first (or none at all, when its edits do not ask).
- **Workers that read Slack** run on Copilot: not on Codex, and not on Claude yet.
- **What reaches a worker from outside.** A repository's own agent settings (Codex's `.codex/`,
  Claude's `.claude/` and `.mcp.json`) load only where you trust its folder in that agent; a
  worktree is trusted as its repository is. Elsewhere none of them loads, and its `AGENTS.md` or
  `CLAUDE.md` reaches the worker as text only: a message marked as the repository's, never in
  Chief of Staff's instructions, with any tag in it escaped. Claude's own settings in a worker's
  folder (`.claude/settings.json`, `.claude/settings.local.json`, `.mcp.json`, `.claude/agents/`,
  with the scripts their hooks run: hooks, `apiKeyHelper`, MCP servers and subagents Claude would
  load as its process starts there) are held as Copilot's are: kept by Chief of Staff as it hands
  the folder out, and read again before each start, each turn (one Claude starts itself too) and
  each subagent. One that differs keeps the worker from starting, and from going to a terminal,
  until you click *Use these settings* on the line its timeline shows; what an MCP server there
  runs changed turns that server off for the worker instead (Claude's `disabledMcpjsonServers`).
  Workers' edits to them are refused; with the sandbox on, its commands can't change them in
  your checkout, nor the scripts their hooks run anywhere. A message from another Claude
  session is shown with the name it gives itself, marked unverified (Claude does not pass on who
  sent it, and any process of yours can give any name), never as yours or a worker's, can approve
  nothing, and starts a turn only if Chief of Staff admits it; messages between Chief of Staff's own workers go through it, with
  the limits of `tell_session`, and one to a session that isn't one of them asks you each time.
- **A Codex worker's own changes to `.codex/` never load.** Codex reads a folder's `.codex/`
  when a thread starts or resumes there: a rule in it lets commands run without asking Chief of Staff, an MCP
  server runs its command, and its settings reach every command's environment. So each time a
  Codex worker's thread starts or resumes, Chief of Staff compares each `.codex/` folder Codex
  would read with what it held when Chief of Staff handed the worker that folder (kept in Chief
  of Staff's own data; read from the files, never through git). One that differs is not trusted
  for that worker until you click *Use these settings*: none of its settings load, its
  `AGENTS.md` reaches it as text, and its timeline says so once. Edits to a `.codex/` folder or
  to Codex's settings folder are refused to workers, whatever their case. A command can still
  write there; it is the check at the next start that keeps it from loading.
- **What a Claude worker schedules** (`/loop`, a reminder) runs only as a turn Chief of Staff
  admits. Claude brings a session's schedules back when it is resumed, so when its task is
  cancelled or set aside, or the worker closed, Chief of Staff deletes them in Claude too (one
  short turn that may only list and delete them), and *Continue in a terminal* waits until
  Claude's own list shows none left. In plan mode a worker writes only its plan, and removes no
  worktree, until the plan is approved.
- **Spend limit:** Chief of Staff's own ledger, in tokens, counted response by response on
  Claude. It may be passed by the model call in flight and by what subagents spend before they
  stop (on Claude, also by their output since the last answer); spend lost when the agent stops
  mid-turn is recorded as unknown, and a worker with a limit then waits for your *Go on*.
- **Memory:** Codex's memories and Claude's auto memory are off for every worker: Codex's
  background agent runs commands no approval reaches; Claude's memory folder is shared with your
  own sessions.
- **Its processes** run under a small supervisor of Chief of Staff's, without Chief of Staff's
  `CHIEFOFSTAFF_*` variables (your Slack token among them), and end when Chief of Staff's process
  goes away, however it stops. The supervisor runs isolated from the folder it starts in
  (`python -I`, in a folder of Chief of Staff's own), so no Python file of a repository runs as
  it. A Claude worker's MCP servers (their headers and variables) and instructions are in files
  only you can read, named by path on its command line and removed with it, in a folder no
  worker may read (its checks and its sandbox refuse it); Chief of Staff's own secrets (your
  Slack token) never reach a file or a command line. What Chief of Staff records of these
  processes keeps a hash of each command line, not the command line.
- **Your agent settings stay yours.** Chief of Staff never edits a Codex or Claude settings file
  and runs nothing that does (Codex, and `claude auth status`, only where that agent was set up,
  so none of its files is made; at startup only their version and sign-in commands; Codex
  refreshes its own model cache when Settings starts it). One change outside Claude's own folder
  is Claude Code's own, with no setting to turn it off: at a sandboxed worker's first command in a
  git repository, it adds the line `**/.claude/.cc-writes/` to your global git ignore file
  (unless git already ignores that folder) and makes an empty `.claude/.cc-writes/` staging
  folder there and in the repository's main checkout. Each Claude worker runs your installed
  `claude` itself, unmodified. Codex's
  settings folder (`~/.codex`, and `$CODEX_HOME`) and Claude's (`~/.claude`, and
  `$CLAUDE_CONFIG_DIR`) cannot be added to *Also writable in the sandbox* while that agent is on:
  their hooks run outside any sandbox.
- **Sign-in** is each agent's own; Chief of Staff never reads, stores or forwards it (no
  keychain item, no credentials file, no key of its own, no requests of its own), and keeps
  only how Claude is signed in (a plan, a key, a cloud), never who. What a worker reads and
  writes goes to OpenAI or Anthropic (or your cloud) under your account, as with your own
  sessions.

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
no telemetry. To say when a newer release is out, it reads its own public page on PyPI
(`pypi.org/pypi/chiefofstaff/json`) at most once a day, a plain request that sends nothing about
you or your machine; Settings → General turns that off. Its *Update and restart* runs only on
your click on its page (no tool, Slack or Send to Chief reaches it): it runs `uvx` or `uv tool
upgrade` for Chief of Staff alone, then starts the new version in its own process, which keeps
the private link's token (in that process's environment, as its own variable, never a worker's).
GitHub Copilot itself works under GitHub's terms and settings.

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
- The push check reads a command line as text; code the line only makes or keeps elsewhere is
  not read: a `-c` script built by a substitution or from input (`bash -c "$(…)"`,
  `while read l; do bash -c "$l"; done`), code in `trap '…'`, a script the line writes to a file
  and then runs (`… > s.sh && bash s.sh`), other interpreters (`python3 -c`, `perl -e`,
  `node -e`), and a script fed to a shell on a line that says neither push nor the name of an
  alias your git configuration has that pushes (`base64 -d | bash`, GNU `parallel` with no
  command). Nor is configuration that sends a branch the line names to
  another one (`git push origin feat` with `push.default=upstream`, or a
  `refs/heads/feat:refs/heads/main` push refspec), git configuration a line writes with a program
  that isn't a shell's redirection or a known writer (`python3 -c …`), or an alias only another
  repository has, run in a folder the line names by a variable (`cd "$D" && git p …`).
  Protect your default branch on the server too.
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
- On macOS, a command that daemonizes in two forks whose first child exits before Chief of
  Staff's supervisor sees it can outlive a stopped worker, as with Copilot workers.
