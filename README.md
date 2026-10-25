# Work Buddy

A **local-first prototype** of a personal work agent: give a Lead your daily goal,
let it delegate independent investigations to Copilot CLI workers, and make the
important decisions from a localhost workbench or Slack.

**This is a working prototype, not an autonomous employee or a production security
sandbox.** Copilot invocations are read-only. Proposed file changes and Slack replies
are executed by the application only after approval of the exact payload.

## Quick start

Requires **Python 3.11+**, macOS or Linux. For real tasks, install and authenticate
[GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli).
Your existing subscription, organization policies, model access, and usage limits apply.
The subprocess integration was exercised against CLI 1.0.102; older versions may
not support its tool filtering and JSONL flags.

```bash
git clone git@github.com:evanl666/work-buddy.git
cd work-buddy
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m work_buddy
```

Alternatively, with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --python 3.13
uv run python -m work_buddy
```

The program opens `http://127.0.0.1:8765` using a private launch token. If the
browser does not open, use the launch URL printed in your terminal. **Do not share
that URL or token.** It grants control of your local workbench.

```bash
python -m work_buddy --no-browser --port 8766
python -m work_buddy --data-dir /path/to/private/work-buddy-data
```

Keep the Python process running. Closing the webpage does not stop tasks. Ctrl+C
stops the service and its CLI invocations. Sleeping/offline computers do not
continue executing work. A busy port is an error, not an invitation to connect
to an unrelated service.

## First run

1. In **Connections & settings**, choose an existing project directory, a narrow
   folder without credentials. Check the Copilot executable and save settings.
   A version check confirms the executable; your first real task verifies login.
2. In **Profile & learning**, paste your responsibilities, goals, and representative
   replies/reviews. Optionally select project-relative source files. Click
   **Distill with Lead**.
3. Review each candidate memory in **Workbench**. Confirm useful candidates;
   reject unsupported inferences. Nothing is activated automatically.
4. Give Lead a goal and a definition of done. Independent work can become up to
   four child tasks, with a configurable maximum of 1-4 concurrent workers
   (default two), plus one Lead invocation.
5. Review decisions, exact file diffs, or Slack reply drafts. Add a reason when
   deciding so the feedback loop can propose lessons.
6. Use **Teach your buddy** on a task to explain corrections. Lead analyzes the
   original task, result, and feedback, and proposes new memories for confirmation.

### Try the workflow without spending model credits

Select **Demo simulator** in Settings. Submit any goal: the simulator creates two
workers, requests a decision, then finishes. Try distillation and feedback to see
explicitly synthetic memory candidates. Demo is never silently substituted for
a failed Copilot call, and it cannot propose real file writes or Slack replies.
Confirmed demo memories are excluded from real Copilot task context.

## Local distillation and feedback loop

```text
Initial material / selected files -> Lead distillation -> candidate memories
                                              -> owner confirmation
Task -> result -> human correction / decision reason -> Lead learning
                                              -> candidate memories
                                              -> owner confirmation
                                              -> context for future work
```

Memories carry **category** (`fact`, `preference`, `workflow`), **scope**
(`project`, `general`), evidence, and source task. The latest 40 applicable
confirmed memories are included in each invocation. Pending candidates are not.
Learning never modifies permissions, and one approved action never becomes
standing authorization. Correct contradictions by removing an old memory and
confirming or explicitly adding its replacement.

Application data is outside the checkout by default:

- macOS: `~/Library/Application Support/work-buddy/`
- Linux: `~/.local/share/work-buddy/` (or the configured XDG data location)
- SQLite: `work-buddy.db` holds tasks, results, candidate approvals, feedback, and events.
- **`memory/confirmed.json`** is a human-readable export regenerated on memory changes.
  The SQLite database is authoritative; edit memories through the UI, not this export.
- **`artifacts/<task-id>-<round>.json`** holds validated Lead/Worker outputs, including
  distillation results and memory candidates.

Removing a memory excludes it from future invocations and exports, not from
historical tasks/feedback or an already-running model call. This is not a
data-erasure tool. General memories remain within this local installation; there
is no cross-company profile sync or automatic model fine-tuning.

## Slack setup (optional, private/internal prototype)

Slack is not required for the local workbench or learning loop.

1. Create a Slack app **from `slack-manifest.json`** in your authorized workspace.
   Workspace policy may require administrator approval.
2. Under Basic Information, create an app-level token with `connections:write`.
3. Install the app to the workspace and obtain its bot token.
4. Enter both tokens under **Slack credentials**. The app saves them in the OS
   keyring, not in SQLite, source files, or browser storage.
5. Enable Slack, enter your **member ID** (`U...`), and optionally allow channel
   IDs (`C...`). Invite the bot to each channel you want it to monitor.

For environments without a working OS keyring, set these variables through your
local secret manager before launching the app:

```text
WORK_BUDDY_SLACK_BOT_TOKEN
WORK_BUDDY_SLACK_APP_TOKEN
```

Do not commit tokens, put them in issue reports, or paste them into agent prompts.
These variables are not forwarded to Copilot subprocesses.

The bridge uses Socket Mode: no public endpoint or cloud backend is required.
The bot receives your DMs **to the bot**, not all your private messages. In allowed
channels, it reacts to the configured keyword, or (when no keyword is configured)
mentions of you or the bot. Bot messages and retries are deduplicated. Access to
thread history depends on Slack token scopes; failures appear in the event log.
Old history is not automatically backfilled.

Decisions arrive in the owner's Slack DM with a confirmation modal and optional
reason. Only the configured owner may resolve them. Actual replies are posted
as **Work Buddy**, not impersonating you. File approvals link back to the localhost
diff; that link only works on the computer running the service.
Completed-task notifications include **Give feedback**, opening a dialog whose
corrections are analyzed by Lead and returned as confirmable memory candidates.

Public distribution / Marketplace onboarding and centralized OAuth are outside
this prototype. Do not distribute a shared app-level token to other users.

## Architecture and limits

- FastAPI + static HTML/CSS/JS; no frontend build or external CDN.
- SQLite task state survives restarts. One Lead slot + bounded Worker slots.
- Copilot runs through supported non-interactive JSONL output using stdin prompts.
  No terminal scraping, SDK runtime download, or custom model training.
- Each invocation is a fresh CLI session with explicit task context, confirmed
  memories, prior decisions, and child outputs. Task state, not an infinite chat,
  is the continuity mechanism.
- Lead delegates at most once per work task. Workers cannot delegate. Maximum
  eight invocations per task, with a configurable invocation timeout.
- CLI tool allowlist: `view`, `glob`, `rg`; shell and write permissions denied.
  Built-in MCPs and custom instructions are disabled. The application never requests
  remote control or session sharing.
  Existing arbitrary MCP tools are not exposed. Internal documents must currently
  be in the authorized local project; hosted document connectors are not included.
- Code/document changes are full-file proposals. Approval checks the original
  content hash and rejects stale previews, path traversal, symlinks, and changes
  to credential/repository metadata. Files are replaced atomically individually,
  **not as a multi-file transaction**. No deletion or automatic shell/test execution.
- Slack writes and file changes are explicit approval actions. Approvals expire
  after 24 hours. Executing actions interrupted by a crash become **uncertain**:
  verify externally before creating a new task. They are never auto-replayed.
- Pause prevents new invocations; it does not cancel active ones. Cancel tasks to
  stop their CLI process groups. Restarted in-flight tasks require explicit retry.
- A failure is displayed as failed/interrupted, never converted into a demo success.

### Trust boundary

The server binds only to loopback, validates Host and mutating request Origin,
uses a launch-token-authenticated HttpOnly/SameSite cookie, and does not enable
CORS. Output is rendered as text, not executable HTML. Secrets live in the OS
keyring. Data files are private to the local OS user on normal installations.

**This is not an OS-level sandbox.** CLI read boundaries depend on CLI permission
enforcement and the selected project. Do not authorize directories containing
secrets or run hostile executables. Existing local CLI plugins/configurations are
part of your trusted computing base. Local users/processes with your OS privileges
can access your files. Approved file content can affect later builds if *you*
execute it; inspect diffs before approving.

Local-first does **not** mean offline inference: relevant content goes to the
configured model provider. Slack receives notifications and approved replies.
Copilot may retain its own session logs outside Work Buddy's data directory.
Follow organizational data policies and use only data you may access.

## Development

```bash
uv sync --extra dev --python 3.13
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Optional browser smoke test (uses temporary data and the demo backend):

```bash
uv run --with playwright python -m playwright install chromium
uv run --with playwright python scripts/browser_smoke.py
# Or use installed Chrome: WORK_BUDDY_BROWSER_CHANNEL=chrome <the command above>
```

Tests use explicit fakes/demo backends and temporary data, never real Slack
credentials. Real Copilot smoke checks require an installed authenticated CLI
and consume model usage. No credentials are needed for CI.
