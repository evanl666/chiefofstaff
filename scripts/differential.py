"""Differential test of the off-state contract (docs/design/README.md, "Off-state contract"): with
its setting off, a new feature leaves Chief of Staff as the baseline release was, apart from the
differences the feature declares by name in FEATURES below.

    uv run --with playwright python scripts/differential.py [--feature NAME] [--mode off|on] [--out DIR]
    uv run --with playwright python scripts/differential.py --copilot-alone [--out DIR]

--mode off (the default): the baseline (git tag v0.1.52, checked out with `git worktree add` and
run in its own `uv run` environment) against this working tree with every registered feature (or
only --feature) at its off settings, allowing only those features' `allow_off`. The baseline runs
with those of the settings it has: a feature it shipped with is off on both sides.
--copilot-alone: as off mode, every feature off, with no other coding agent on the PATH (no
stand-ins; the owner's own hidden): only COPILOT_ALONE is allowed (and FIXES, and the `allow_off`
of the features the baseline lacks, see below).
--mode on: this working tree with --feature off against the same with it on, allowing only that
feature's `allow_on` (and its own settings keys). A feature not behind a setting does its
`on_action` instead (more_agents: Codex turned on, then off again; claude_agents: Claude). A
feature with a `setup` runs it on both sides, the baseline's too, before the script, when it is
the one chosen (--feature), allowing its `allow_setup` too (then only): codex_on_claude_never
compares the baseline with Codex on to this tree with Codex on and Claude never turned on.
--keep-on NAME: any mode (but --copilot-alone) with that feature at its on settings on this
tree's sides (on by default, as worker_fork: each mode with it off and on), allowing its
`allow_on` against the baseline (off mode).

Each side runs the real web app in a process of its own tree's environment, on a scripted Copilot
(chiefofstaff.scripted) with a fake Slack, a fake `gh`, stand-in `codex` and `claude` CLIs on the
PATH (each run of them is captured) and a fixed clock (time-machine; time moves only between
steps). The app's periodic rounds (an asyncio.sleep of PERIODIC seconds or more in
its code) wait for the script's tick, so they run at the same point on both sides. Sides run one
after the other in the same folder on the same port (8810-8819, or ten from
DIFFERENTIAL_PORT_BASE), so paths and ports match; that folder is a short one in the system's
temporary folder (run_folder), so a socket fits in its data directory from any checkout.

Compared, after each step of the script (boot, ask, decide, slack, approve, blocked, accept,
tick) and once more after the page was looked at:
- the API: /api/state and the other GETs the page reads at boot, each session's messages;
- the store: every table of the SQLite database (drafts, notices, usage rows, tasks, runs...)
  and its schema;
- what the scripted backend received: sessions created and resumed with their options (system
  message, tools with their schemas, model...), prompts sent, each session's turns; desktop
  notifications, Slack calls and `gh` commands; any run of an agent CLI, and which agent
  modules (AGENT_MODULES) the app imported;
- the page: the API requests it makes, its errors, DOM snapshots and screenshots of Home (with
  its narrow tabs), the sidebar (and its other Copilot sessions) and each Settings tab (each
  scrolled page), at 800, 1024, 1280 and 1440 px, light and dark. Screenshots differ when more
  than PIXEL_LIMIT pixels differ by more than PIXEL_DELTA in a channel (anti-aliasing).

Normalized on both sides alike before comparing: paths (the tree, the run folder, home), ports,
ids (uuids, task t…, run r…, decision dec…, question q…, owner refs o…, message ids, hex tokens
and commit hashes become <task-1>, <uuid-2>... in order of first appearance), times (ISO
times, epoch seconds and Slack ts become their offset from the fixed clock, <T+3m>), and the
number of events published on the wall clock while the page is open (TIMED_EVENTS): whether there
were any.

FIXES: deliberate fixes of how the baseline looks (not a feature's), allowed in every run.

The registry, FEATURES: a name -> Feature(off={settings}, on={settings}, allow_off=[Allow...],
allow_on=[...]). `off` and `on` are the settings the working tree runs with (off is compared to
the baseline; on mode compares off to on, every other feature off); each Allow names one allowed
difference: a settings key at a value, a data path (regex), lines of text, or a page element
(CSS selector) left out of DOM snapshots and screenshots. See Allow and EXAMPLE.
--feature in off mode applies only that feature (others run as they are, but for those behind a
setting the baseline lacks: off too, with their `allow_off`, as one on by default, such as
delegate_reply, would differ from the baseline; --copilot-alone turns those off as well); --tree
tests another checkout, --baseline another ref.

Prints a report (also in OUT/report.txt, with OUT/differences.json) and exits 1 on any difference
that is not allowed; screenshots of both sides are in OUT/<side>/, and before/after/diff images of
each that differs in OUT/diff/. The baseline worktree, the run folder and the processes are
removed at the end, whatever happened.
"""

# The page's DOM and image checks are JavaScript, written on long lines.
# ruff: noqa: E501

import argparse
import asyncio
import base64
import contextlib
import dataclasses
import difflib
import fcntl
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(__file__).resolve()
BASELINE = "v0.1.52"
# DIFFERENTIAL_PORT_BASE moves the ten ports (another checkout's run on the same machine).
PORT_BASE = int(os.environ.get("DIFFERENTIAL_PORT_BASE", "8810"))
PORTS = range(PORT_BASE, PORT_BASE + 10)
TOKEN = "differential-token"
WORK = ROOT / "browser-shots" / "differential-work"
OUT = ROOT / "browser-shots" / "differential"
CLOCK = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
STEP_MINUTES = 3
TICK_MINUTES = 61
PERIODIC = 2.5
WIDTHS = (800, 1024, 1280, 1440)
SCHEMES = ("light", "dark")
HEIGHT = 900
PIXEL_DELTA = 24
PIXEL_LIMIT = 12
SHOWN = 30  # Differences printed per kind; all of them are in the report file.
STEPS = ("boot", "ask", "decide", "slack", "approve", "blocked", "accept", "tick")


def log(text: str):
    print(f"  {text}", flush=True)


ANY = object()  # An allowance's value when any will do.

# The registry: what each feature may change, by name.


@dataclass
class Allow:
    """One allowed difference. It matches a data difference by `setting` (a settings key, in the
    API's snapshot and in the stored settings) or `data` (a regex searched in the difference's
    path, e.g. `steps.ask.api./api/state.tasks[0].status` or `backend.sessions.router.options.
    system_message.content`), optionally only at `value` (on the side tested), and for changed
    text only when each line added or removed matches `lines` (or that of another allowance of the
    same text: a text two features change, each its own lines), or only by `added` (a regex: the
    text tested is the other side's with one match of it put in), or when it is the same on both
    sides once each match of `without` is taken out. An `element` (a CSS selector) is
    left out of the DOM snapshots and hidden in the screenshots, on both sides, in the views
    whose names match `views`: screenshots home-1280, home-needs-800, sidebar-1024,
    sidebar-external-1440, settings-general-p1-1280 (p2... as it scrolls); DOM snapshots
    home-1280, settings-1280 (the dialog without its tabs' sections), settings-general-1280."""

    name: str
    setting: str = ""
    data: str = ""
    value: Any = ANY
    lines: str = ""
    added: str = ""
    element: str = ""
    views: str = ""
    # A count only this many more on the side tested (one more event, say), nothing else.
    more: int = 0
    # False: the views `views` names are not photographed at all, on either side (no screenshot,
    # no DOM snapshot), so one may exist on one side only: for a view a release redraws on
    # purpose, compared by what it holds and does instead (Settings v2: SETTINGS_V2).
    pixels: bool = True
    # A text changed only by what this regex finds: the same on both sides once each match of
    # it is taken out (a key added inside a tool's JSON answer, say).
    without: str = ""

    def matches(
        self, path: str, after: Any, text_lines: list[str] | None, before: Any = None
    ) -> bool:
        if (self.element or not self.pixels) and not (self.setting or self.data):
            return False  # About pictures only.
        if self.setting:
            key = re.escape(self.setting)
            # (Settings' own field of it, and what Save sent of it: settings_round_trip.)
            where = (
                r"\./api/state\.settings|\.db\.settings\.1\.value|^page\.settings\.(?:fields|sent)"
            )
            if not re.search(rf"(?:{where})\.{key}$", path):
                return False
        if self.data and not re.search(self.data, path):
            return False
        if self.value is not ANY and after != self.value:
            return False
        if self.more and not (isinstance(before, int) and after == before + self.more):
            return False
        if self.added:
            return (
                isinstance(after, str)
                and isinstance(before, str)
                and any(
                    after[: m.start()] + after[m.end() :] == before
                    for m in re.finditer(self.added, after)
                )
            )
        if self.lines:
            return text_lines is not None and all(re.search(self.lines, x) for x in text_lines)
        if self.without:
            return (
                isinstance(before, str)
                and isinstance(after, str)
                and re.sub(self.without, "", before) == re.sub(self.without, "", after)
            )
        return True


@dataclass
class Feature:
    """A feature behind a setting: the settings the working tree runs with when it is off and
    when it is on, and the differences each may show (against the baseline when off, against
    off when on). `on_action`: what the "on" side does once the app is ready, before the
    script's steps, for a feature not turned on by a setting (a Scenario method's name)."""

    off: dict[str, Any]
    on: dict[str, Any]
    allow_off: list[Allow] = field(default_factory=list)
    allow_on: list[Allow] = field(default_factory=list)
    on_action: str = ""
    # What both sides do once the app is ready, before the script, when this feature is the one
    # chosen in off mode (a Scenario method's name, which the baseline's app runs too), and the
    # differences allowed then only (never in a run of every feature).
    setup: str = ""
    allow_setup: list[Allow] = field(default_factory=list)


FEATURES: dict[str, Feature] = {}

# The format, not registered: a switch in Settings → General, on by default, compared off.
EXAMPLE = Feature(
    off={"example": False},
    on={"example": True},
    allow_off=[
        Allow("its setting, off", setting="example", value=False),
        Allow(
            "its switch line in Settings → General",
            element="#settings-dialog label:has(> input[name=example])",
            views=r"^settings-general-",
        ),
    ],
    allow_on=[
        Allow(
            "its line in the Router's prompt",
            data=r"\.system_message\.content$",
            lines=r"(?i)example",
        ),
        Allow("its section in the sidebar", element="#sidebar .example", views=r"^(home|sidebar)"),
    ],
)

# Deliberate fixes of how the baseline looks, not any feature's: allowed in every run (the plain
# one, and each feature's off and on), each with why. Those of 0.1.41 to 0.1.52 (the one-line
# heading, workers' batch CLI line, learning from talked sessions, the runs kept, role rules, the
# Router's title rule, a check-back's runs, workers' own caches, agent settings snapshots, .git
# identities, held messages, a chat's task, tool search, the Router's rule for a crossed report,
# ask_decision's task, Watch scans that failed, drafts' has_final, tasks' settings and aged,
# follow_thread's description, a read-only checkout's own copy, workers' rule on waiting, QA8's
# settings kept only for sandboxed workers, Handled for you, E115's answers, a new worker when
# the owner asks for one, a task's result being done, E116's account to connect, sessions'
# client name, every session's context size) are the baseline's own behaviour.
# 0.1.54 (live r76 E5): work the owner asks done directly in their own folder goes to a worker
# started in place, never to one that reads it or works in a copy: the Router's reuse rule names
# it as its one exception (its paragraph wrapped again), these lines and no others.
IN_PLACE_RULE_BEFORE = (
    "reason for a new worker. Start a new worker only when no worker covers the subject, when the",
    "request is an independent code change on a different subject (its own branch and PR: another",
    "feature, function or bug, even in the same project, never on a branch that holds other work),",
    "when it must run in parallel with what the related worker is doing now, or when the owner",
    "asks for one. spawn_worker refuses once when related workers exist and lists them: then",
    "either message_session one of them, or call spawn_worker again with why_new saying briefly",
    "why none fits (the owner sees it); the owner asking for a new worker is reason enough (quote",
    "their words).",
)
IN_PLACE_RULE_AFTER = (
    "reason for a new worker. Work the owner asks done directly in their own folder (in place, not a",
    "copy) is the one exception: only a worker started for it with in_place=true can do it (a worker",
    "you have works in a copy of that folder or only reads it, unless it was itself started in place",
    "there), so spawn_worker in_place=true, with context_from the related worker's task; never give it",
    "to that worker. Start a new worker only when no worker covers the subject, when the request is an",
    "independent code change on a different subject (its own branch and PR: another feature, function",
    "or bug, even in the same project, never on a branch that holds other work), when it must run in",
    "parallel with what the related worker is doing now, for work in place, or when the owner asks for",
    "one. spawn_worker refuses once when related workers exist and lists them: then either",
    "message_session one of them, or call spawn_worker again with why_new saying briefly why none fits",
    "(the owner sees it); the owner asking for a new worker is reason enough (quote their words).",
)
# 0.1.54 (live memory M1): what the owner said to answer, the Slack session writes their way;
# their exact words only where they quoted them. Its rule's lines, wrapped again.
SLACK_SAID_BEFORE = (
    "reply to; when the owner said what to answer, in their words and language. The owner",
    "approves, edits or rejects it; you will get the outcome.",
)
SLACK_SAID_AFTER = (
    "reply to; when the owner said what to answer, that, in their language and the way they write",
    "(their exact words only where they quoted them). The owner approves, edits or rejects it;",
    "you will get the outcome.",
)
FIXES: list[Allow] = [
    # E126: tasks replaced before their replacedby: link was kept get it once, read back from
    # their progress words; the mark that this ran is in the store. Nothing is shown.
    Allow("replaced tasks' links, read back once", data=r"\.db\.kv\.migrated:replacedby$"),
    # E129 (team threads): questions workers wait on each other for are in the state; on Home,
    # a run of two or more routing receipts (Started, Queued, New task, Continued, Told) or of
    # what workers told each other is one line that opens to them (a line alone is as it was).
    Allow("open questions between workers", data=r"/api/state\.peer_waiting$"),
    # Slack's MCP server answers in JSON since 2026-11-21 (read again in 0.1.53): Chief of Staff's
    # own searches ask for messages only, so no other section follows a search's last message.
    Allow(
        "Slack searches ask for messages only",
        data=r"^slack\.\w+\[\d+\]\.arguments\.content_types$",
        value="messages",
    ),
    Allow(
        "Home's routing receipts and peer messages, a run folded in one line",
        element='#timeline > .fold, #timeline > .event[data-kind="peer"], #timeline > '
        '.event[data-kind="notice"]:has(> .what:is([title^="Started"], [title^="Queued"], '
        '[title^="New task"], [title^="Continued"], [title^="Told"]))',
        views=r"^home",
    ),
    # E131-E136 (usage as Copilot counts it): each call keeps what started it (a premium
    # request only for a user message's call), its kind (a compaction's calls too) and its
    # cache tokens. The Usage view's totals and rows say more (fresh input, cache reads and
    # writes, context a call; older calls unread), By task, what the spend went to (in place of
    # Per accepted result) and a worker's tasks under its row. Not in the screenshots.
    Allow(
        "usage rows: what started each call, its kind, calls and cache tokens",
        data=r"\.db\.usage\[\d+\]\.(initiator|kind|calls|cache_read_tokens|cache_write_tokens)$",
    ),
    Allow(
        "usage's new columns",
        data=r"\.db\._schema\.usage$",
        lines=r"^\s*, run_id TEXT NOT NULL DEFAULT ''(, initiator TEXT NOT NULL DEFAULT '', .*"
        r"cache_write_tokens INTEGER NOT NULL DEFAULT 0)?\)$",
    ),
    Allow(
        "the Usage view's totals and rows: tokens by kind, compactions, older calls unread",
        data=r"/api/usage\?since=[^.]*\.(total|by_role\[\d+\]|by_model\[\d+\]|by_session\[\d+\])"
        r"\.(premium_unread|compactions|cache_read_tokens|cache_write_tokens|fresh_input_tokens"
        r"|context_tokens|context_per_call|model_calls)$",
    ),
    Allow(
        "the Usage view's worker rows: a worker, its tasks",
        data=r"/api/usage\?since=[^.]*\.by_session\[\d+\]\.(worker|tasks)$",
    ),
    Allow(
        "the Usage view's By task and spend (Per accepted result's place)",
        data=r"/api/usage\?since=[^.]*\.(by_task|spend|outcomes)$",
    ),
    # E129 (team threads), phases 2-4: the Router is given a digest of what workers tell each
    # other, not a copy of each message, and reads a team's thread when it must (its rule, one
    # more tool, so its tool search defers one later); workers have a short name and a team,
    # the state lists the teams; a worker's tabs (none on Home: hidden there).
    Allow(
        "the Router's rule: a team digest, read_team_thread when it must",
        data=r"^backend\.sessions\.router:.*\.system_message\.content$",
        lines=r"^  (straight to each other \(no need to relay it\); (a copy reaches you|your next "
        r"turn gets)|riding along with your next turn|on or retell\. What a task|change course "
        r"reports a question to you\. Pass the owner|worker needs \(links, constraints|\(type="
        r'"team-digest"|exchanges \(read_team_thread\)|task up, and never pass on|does what stay '
        r"with you|the owner's request faithfully|project\)\. When the task is one of the owner's)",
    ),
    Allow(
        "the Router's read_team_thread",
        data=r"^backend\.sessions\.router:.*\.options\.(tools|available_tools)\[\d+\]$",
    ),
    Allow(
        "the Router's tool search, one tool more",
        data=r"^backend\.sessions\.router:.*\.options\.tool_search\.defer_threshold$",
        value=49,
    ),
    Allow("teams in the state", data=r"/api/state\.teams$"),
    Allow(
        "workers' short names and teams",
        data=r"(/api/state\.sessions\[\d+\]|/api/sessions/[^.]+)\.(label|team)$",
    ),
    Allow("a worker's tabs, none on Home", element="#worker-tabs", views=r"^home"),
    Allow(
        "the Router's rule for work in place",
        data=r"\.system_message\.content$",
        lines=r"^  (?:"
        + "|".join(map(re.escape, IN_PLACE_RULE_BEFORE + IN_PLACE_RULE_AFTER))
        + ")$",
    ),
    # Task relationships: another worker taking over an open task replaces it (replaces=…), so
    # the tasks that wait on it wait on the new one, rather than a cancel and a new task.
    Allow(
        "the Router's rule: another worker taking over an open task replaces it",
        data=r"^backend\.sessions\.router:.*\.system_message\.content$",
        lines=r'^  (task="new"\): it is not left with a Retry beside the new task\.( The same when '
        r"the owner has)?|another worker \(or a new one\) take over a task still open: |"
        r"replaces=\[its id\], never cancel_task and then a new task; |that wait on it wait on "
        r"the new one\.$)",
    ),
    # 0.1.54 (live e2e r81): record_decision's tasks are work already given; what the Router
    # starts in the same turn relies on the decision by itself.
    Allow(
        "record_decision's tasks: work already given",
        data=r"\.options\.tools\[\d+\]\.parameters\.properties\.tasks\.description$",
        value="Task ids (t…) or worker ids whose work already given relies on this decision. "
        "Not the work you are about to give: tasks you start in this turn rely on it by "
        "themselves",
    ),
    Allow(
        "the Slack session's rule for what the owner said to answer",
        data=r"\.system_message\.content$",
        lines=r"^   (?:" + "|".join(map(re.escape, SLACK_SAID_BEFORE + SLACK_SAID_AFTER)) + ")$",
    ),
    # 0.1.56 (owner report, item 1): a worker asking the owner to reconsider a decision in
    # force names it (replaces); the answer replaces it.
    Allow(
        "ask_decision's replaces: a decision to reconsider",
        data=r"^backend\.sessions\.worker:.*\.options\.tools\[\d+\]\.parameters\.properties"
        r"\.replaces$",
    ),
    # 0.1.56: a task's dot says where it is (amber under Needs you, read or not), in words on
    # hover; a result not opened yet says New before its title instead of a blue dot.
    Allow(
        "a task's dot, its place in words on hover",
        element=".card.task .head > .dot",
        views=r"^home",
    ),
    Allow(
        "a result not opened yet: New before its title, its dot amber (was blue)",
        element=".card.task .head:has(> .dot.unread, > .new-tag)",
        views=r"^home",
    ),
    # 0.1.57: Settings → Workers says what holds either way, yolo or not (the floor: no worker
    # speaks on GitHub as the owner, force-pushes or pushes to main unasked).
    Allow(
        "Settings → Workers: what holds either way, yolo or not",
        element="#settings-dialog label:has(> input[name=worker_yolo]) + small",
        views=r"^settings-workers-",
    ),
    # 0.1.57: Pause watching names Prepared for you, which it pauses too.
    Allow(
        "Settings → Advanced: Pause watching names Prepared for you",
        element="#settings-dialog label:has(> input[name=paused])",
        views=r"^settings-advanced-",
    ),
    # 0.1.57: "away" is read from the keyboard and mouse (macOS, Windows) or the page (Linux),
    # in the state, and its setting's label says which (the same words here, on macOS).
    Allow("what away is read from, in the state", data=r"/api/state\.connector\.away\.idle$"),
    Allow(
        "Settings → Connections: Away after's label, said from what it is read",
        element="#settings-dialog label:has(> input[name=away_minutes]) > span",
        views=r"^settings-connections-",
    ),
    # 0.1.57: the daily summary's Handled counts what Handled for you lists (here, nothing: the
    # line goes), not the replies the owner sent themselves.
    Allow(
        "the daily summary: no Handled line for the owner's own replies",
        data=r"(/api/sessions/[^.]+/messages|\.db\.messages)\[\d+\]\.text$",
        lines=r"^• Handled: \d+ repl(y|ies) sent, \d+ thread follow-ups? without the Router$",
    ),
    Allow(
        "the daily summary, a line shorter",
        element="#timeline .card.update:has(li > strong.key)",
        views=r"^home",
    ),
    # 0.1.57, Teams lite (teams-v2.md): only what the owner sees of teams. The state's teams are
    # the owner's requests in parts (`teams`, declared above), with Past teams and the questions
    # that hold a worker up; Home's Teams rows, the sidebar's team line, a team's page, a
    # Needs-you card per question, a worker's Team or Notes tab (declared above); passive marks of
    # which request the Router made a task for, and which tasks an exchange was between. What
    # workers and the Router are given is the same.
    Allow("Teams lite: Past teams, how many", data=r"/api/state\.past_teams$"),
    Allow("Teams lite: questions holding a worker up", data=r"/api/state\.held_questions$"),
    Allow(
        "Teams lite: a task's request, a request's tasks, a part's teammates, a task's "
        "exchanges, the owner's look at a done team (passive marks)",
        data=r"\.db\.kv\.(req|reqtasks|with|withpending|xchg|teamseen):",
    ),
    Allow(
        "Teams lite: the tasks an exchange was between, in its message's meta",
        data=r"messages.*\.meta\.(from_task|to_task)$",
    ),
    Allow(
        "Teams lite: the tasks an exchange was between, in its message's meta (as stored)",
        data=r"messages.*\.meta$",
        without=r', "(from|to)_task": "[^"]*"|"(from|to)_task": "[^"]*", ',
    ),
    # Home counts a question holding a worker up as one card each (it was one a team): the
    # Router's count of what needs the owner is Home's (list_sessions' needs_owner, a fresh
    # Router's handover), and the daily digest's line counts the workers held up.
    Allow(
        "Teams lite: the Router's count of what needs the owner, a held question one each "
        "(list_sessions' needs_owner)",
        data=r"^backend\.(turns|sessions)\.router:",
        without=r'"needs_owner": \{"count": \d+, "summary": "[^"]*"',
    ),
    Allow(
        "Teams lite: the Router's count of what needs the owner, a held question one each "
        "(a fresh Router's handover)",
        data=r"^backend\.(turns|sessions)\.router:",
        without=r"Under the owner's Needs you now \(Home's count\): \d+: [^\n]*",
    ),
    Allow(
        "Teams lite: the daily digest's workers held up by another's answer",
        data=r"notices\[\d+\]\.text$",
        without=r"\d+ workers? waiting on another one's answer",
    ),
    Allow(
        "Teams lite: Home's Teams rows, a held question's card",
        element="#tasks-teams, #needs .card.team-wait",
        views=r"^(home|sidebar)",
    ),
    # 0.1.57 (the owner's choice, QA E143, E146, E147, E150): the Workers sidebar lists workers
    # only, in one list by status: no team line, rail, Compare caption or indentation (as
    # v0.1.56 had them); a small ⑂ after a second worker's name, ⇄ on a side of a Compare, when a
    # check-back wakes its worker (↻ in 12 min, ↻ 3:28 PM) and the worker one waits on. A second
    # worker started for a busy one's new task is marked so in the state (fork_of's busy).
    Allow(
        "the Workers sidebar: workers only, flat by status (⑂, ⇄, ↻ times, waits on)",
        element="#session-list :is(.team-line, .compare-group, .item.forked, .item.nested, "
        ".item.in-team, .item:has(.fork-mark, .compare-mark, > .sub.back, > .sub.waits))",
        views=r"^(home|sidebar)",
    ),
    Allow(
        "a second worker for a busy one's new task, marked so",
        data=r"(/api/state\.sessions\[\d+\]\.fork_of\.busy|\.db\.kv\.forkof:.*)$",
    ),
    # 0.1.57 (the owner's word): the sidebar's Other Copilot sessions is Other sessions: its
    # heading (and its name for a screen reader), its empty line, the setting's line.
    Allow(
        "Other sessions (was Other Copilot sessions): the sidebar's heading and its empty line",
        element="#external > summary, #external-list .empty-note",
        views=r"^(home|sidebar)",
    ),
    Allow(
        "Settings → General: Show where my other sessions stand (was …other Copilot sessions…)",
        element="#settings-dialog label:has(> input[name=other_sessions])",
        views=r"^settings-general-",
    ),
    # 0.1.57 (QA E149): workers' colors (their avatars, a team's parts) are no status's hue.
    Allow(
        "workers' colors, none a status's hue (avatars, a team's parts)",
        element=".avatar[class*=' c'], .pa[class*=' c'], .tv2-node[class*=' c']",
        views=r"^(home|sidebar)",
    ),
    # 0.1.57 (owner report): an answer the owner asked for (the Router asked a worker for them,
    # or they asked it on Home) stands apart on Home: an Answer label, its worker's short name
    # and its first line in the text's colour (was "Answer/Update from “<task>”: …", muted).
    Allow(
        "Home's answer line: an Answer label, its worker's short name",
        element='#timeline .event.answer, #timeline .event:has(.what:is([title^="Answer/Update '
        'from"], [title^="Answer from"]))',
        views=r"^home",
    ),
]

# Where the owner's other Copilot sessions stand (others.py, docs/design/adopt-sessions.md): on
# by default (the owner agreed), so it is compared switched off against the baseline. On, the
# scenario's two terminal sessions have logs (one asks the owner something), and only the group
# Other Copilot sessions, the view behind its rows (not opened here), /api/others and the
# `others` events differ; Home's screenshots and DOM are the same.
FEATURES["other_sessions"] = Feature(
    off={"other_sessions": False},
    on={"other_sessions": True},
    allow_off=[
        Allow("its setting, off", setting="other_sessions", value=False),
        Allow(
            "its switch line in Settings → General",
            element="#settings-dialog label:has(> input[name=other_sessions])",
            views=r"^settings-general-",
        ),
    ],
    allow_on=[
        Allow(
            "its switch line in Settings → General, checked",
            element="#settings-dialog label:has(> input[name=other_sessions])",
            views=r"^settings-general-",
        ),
        Allow(
            "the group Other sessions: unfolded on the first load, its rows at rest and"
            " N more, their dots, small words, tooltips, names and order",
            element="#sidebar #external",
            views=r"^(home|sidebar)",
        ),
        Allow(
            "the view's meta line and the question's note line (that view is not opened here)",
            element="#session-meta, #timeline [data-question]",
            views=r"^external-",
        ),
        Allow(
            "/api/others, read by the page",
            data=r"^page\.requests\[\d+\]$",
            value="GET /api/others",
        ),
        Allow("the `others` events", data=r"^steps\.\w+\.bus\.others$"),
    ],
)

# Send to Chief (sendsocket.py, docs/design/send-anywhere.md): off by default. Off, only its
# switch line at the end of Settings → Connections is new, and no socket opens. On, with nothing
# sent: that line checked, the command under it, and the Router's rule for what is sent, the last
# line of its role (its socket opens in the run's data directory, which nothing here reads).
FEATURES["send_to_chief"] = Feature(
    off={"send_to_chief": False},
    on={"send_to_chief": True},
    allow_off=[
        Allow("its setting, off", setting="send_to_chief", value=False),
        Allow(
            "its switch line at the end of Settings → Connections",
            element="#settings-dialog #send-line",
            views=r"^settings-connections-",
        ),
    ],
    allow_on=[
        Allow(
            "its line in Settings → Connections, checked, with the command under it",
            element="#settings-dialog #send-line",
            views=r"^settings-connections-",
        ),
        Allow(
            "the Router's rule for what is sent with it",
            data=r"\.system_message\.content$",
            lines=r'^- from="capture" type="delegated": something the owner sent you with Send to '
            r"Chief from outside this UI ",
        ),
    ],
)

# More agents (docs/design/more-agents.md): workers on other coding agents (Codex), not a setting
# but the owner's Coding agents tab in Settings. Off and never used, on a machine with `codex` and
# `claude` on the PATH (every run has them): v0.1.48's last line of Settings → Models is gone,
# and the picker shows what INSTALLED says; nothing else of agents runs or is imported (both
# captured, so either is a difference). Its "on" mode is turned on and then off again, before
# the script (5.11): it may leave its configuration (marked off) and its usage table, empty; the
# Router lets go of its session (its tools and prompt are made again when the agents on change)
# and resumes it with its next message, with the very options it was made with (captured): not
# live until then, and one more session event.
FEATURES["more_agents"] = Feature(
    off={},
    on={},
    allow_on=[
        Allow("its configuration, kept and marked off", data=r"\.db\.kv\.agents$"),
        Allow(
            "decided on in Settings: its first-run card is not shown again",
            data=r"\.db\.kv\.agentoffer:(codex|claude)$",
            value={"value": "settings"},
        ),
        Allow("its usage table, empty", data=r"\.db\.agent_usage$"),
        Allow(
            "its usage table and indexes in the schema",
            data=r"\.db\._schema\.(agent_usage|agent_usage_by_session|agent_usage_by_time)$",
        ),
        Allow(
            "the Router's session resumed once after the switch, as made",
            data=r"^backend\.sessions\.router:[^.\[]*\[1\]$",
        ),
        Allow(
            "the Router let go of its session after the switch, until its next message",
            data=r"^steps\.boot\.api\./api/(state\.sessions\[0\]|sessions/<uuid-1>)\.live$",
            value=False,
        ),
        Allow(
            "one more session event: the Router's session opened again",
            data=r"^steps\.\w+\.bus\.session$",
        ),
    ],
    on_action="agents_drained",
)

# The notice of a newer release (updates.py): on by default (the owner asked for it), so it is
# compared switched off against the baseline. PyPI is never read here (CHIEFOFSTAFF_UPDATE_CHECK
# is 0 on both sides): on, only its switch line is checked.
FEATURES["update_notice"] = Feature(
    off={"update_notice": False},
    on={"update_notice": True},
    allow_off=[
        Allow("its setting, off", setting="update_notice", value=False),
        Allow(
            "its switch line in Settings → General",
            element="#settings-dialog label:has(> input[name=update_notice])",
            views=r"^settings-general-",
        ),
    ],
    allow_on=[
        Allow(
            "its switch line in Settings → General, checked",
            element="#settings-dialog label:has(> input[name=update_notice])",
            views=r"^settings-general-",
        ),
    ],
)

# A reply in the thread of a Slack message the owner reacted to with their delegate emoji goes out
# at once (drafts.py, Drafts.handed_over): on by default (the owner asked for it), so compared
# switched off against the baseline. The Router's rule on delegated messages says either way;
# the script hands no message over, so on, only its switch line is checked.
FEATURES["delegate_reply"] = Feature(
    off={"slack_delegate_reply": False},
    on={"slack_delegate_reply": True},
    allow_off=[
        Allow("its setting, off", setting="slack_delegate_reply", value=False),
        Allow(
            "its switch line under the delegate emoji in Settings → Connections",
            element="#settings-dialog label:has(> input[name=slack_delegate_reply])",
            views=r"^settings-connections-",
        ),
        Allow(
            "the Router's rule on delegated messages: the reply written for the people there",
            data=r"\.system_message\.content$",
            lines=r"^(  author waits for an answer, pass slack=true and its thread keys in "
            r"`follow`, and ask the|  worker to draft a reply in that thread with the outcome "
            r"when done(, written for the people| \(the owner approves it\)\.)|  there \(the "
            r"owner approves it, or it goes out at once when they let it\)\.)$",
        ),
    ],
    allow_on=[
        Allow(
            "its switch line under the delegate emoji in Settings → Connections, checked",
            element="#settings-dialog label:has(> input[name=slack_delegate_reply])",
            views=r"^settings-connections-",
        ),
    ],
)

# Claude workers (more-agents.md 5.12): the same tab, one more row. Off and never used, as
# more_agents: only INSTALLED's look at `claude` (never the Agent SDK). Its "on" mode
# turns Claude on and then off again before the script (5.11), leaving what Codex's does.
FEATURES["claude_agents"] = Feature(
    off={},
    on={},
    allow_on=[
        Allow("its configuration, kept and marked off", data=r"\.db\.kv\.agents$"),
        Allow(
            "decided on in Settings: its first-run card is not shown again",
            data=r"\.db\.kv\.agentoffer:(codex|claude)$",
            value={"value": "settings"},
        ),
        Allow("its usage table, empty", data=r"\.db\.agent_usage$"),
        Allow(
            "its usage table and indexes in the schema",
            data=r"\.db\._schema\.(agent_usage|agent_usage_by_session|agent_usage_by_time)$",
        ),
        Allow(
            "the Router's session resumed once after the switch, as made",
            data=r"^backend\.sessions\.router:[^.\[]*\[1\]$",
        ),
        Allow(
            "the Router let go of its session after the switch, until its next message",
            data=r"^steps\.boot\.api\./api/(state\.sessions\[0\]|sessions/<uuid-1>)\.live$",
            value=False,
        ),
        Allow(
            "one more session event: the Router's session opened again",
            data=r"^steps\.\w+\.bus\.session$",
        ),
    ],
    on_action="claude_drained",
)

# Codex on and Claude never turned on (more-agents.md 5.12.8, 5.12.10): both sides turn Codex on
# before the script. The group Codex's row is in has Claude's beside it (the stand-in `claude`
# says 2.1.322, signed in with a plan): to show it, Settings looks at it as it does at Codex (its
# version, then its sign-in's state), from the module that looks, which keys its check by the
# check's revision. No Claude session, no check run, no Agent SDK imported.
FEATURES["codex_on_claude_never"] = Feature(
    off={},
    on={},
    setup="codex_on",
    allow_setup=[
        Allow(
            "the agents' view says the Router does not choose the agent (the picker's option)",
            data=r"\./api/state\.agents\.router_chooses$",
            value=False,
        ),
        Allow(
            "the Router can move a worker's work to another agent when asked (continue_worker)",
            data=r"^backend\.sessions\.router:Router\[\d+\]\.options\.(available_)?tools\[\d+\]$",
        ),
        Allow(
            "the Router's rule for agents says when to use continue_worker",
            data=r"\.system_message\.content$",
            lines=r"^- Workers can run on Codex as well as Copilot: ",
        ),
        Allow(
            "the agents' view names the agent the Router runs on (the @ list marks it)",
            data=r"\./api/state\.agents\.router_on$",
            value="copilot",
        ),
        Allow(
            "the Coding agents tab is not photographed (INSTALLED), so it does not look",
            data=r"^page\.requests\[\d+\]$",
            value="GET /api/agents",
        ),
        Allow(
            "Codex decided on in Settings: its first-run card is not shown",
            data=r"\.db\.kv\.agentoffer:codex$",
            value={"value": "settings"},
        ),
        Allow(
            "Claude's row in the Coding agents group",
            element="#settings-dialog .agent-row[data-agent=claude]",
            views=r"^settings-models-",
        ),
        Allow(
            "Settings looks at Claude for its row: its version",
            data=r"^agent_clis\[\d+\]$",
            value="claude --version",
        ),
        Allow(
            "Settings looks at Claude for its row: how it is signed in",
            data=r"^agent_clis\[\d+\]$",
            value="claude auth status --json",
        ),
        Allow(
            "the module that looks at Claude",
            data=r"^agent_modules\[\d+\]$",
            value="chiefofstaff.backend_claude",
        ),
        Allow(
            "the module that keys Claude's check (its revision; the check does not run)",
            data=r"^agent_modules\[\d+\]$",
            value="chiefofstaff.claude_check",
        ),
        Allow(
            "Other sessions lists Codex's own sessions, starting Codex for that (the stand-in "
            "can't start): again once its list is 55 s old, or the group is opened",
            data=r"^agent_clis\[\d+\]$",
            value='codex app-server -c approvals_reviewer="user"',
        ),
        Allow(
            "the Router hears which agents on can run now",
            data=r"^backend\.turns\.router:Router\[\d+\]\[\d+\]$",
            added=r', "available": \["codex"\]',
        ),
        Allow(
            "while another agent is on, each worker shows its agent's mark (Copilot's too): its "
            "sidebar row, its tasks, its header",
            element='.agent-mark[data-agent="copilot"]',
        ),
        Allow(
            "while another agent is on, the line that says a worker started has its agent's "
            "chip (Restart on… before its first step)",
            element="#timeline .agent-chip-wrap",
        ),
        Allow(
            "a worker started while another agent is on can restart on it until its first step",
            data=r"(\.db\.kv\.untouched:<uuid-\d+>|\.untouched)$",
        ),
        Allow(
            "Copilot's tool search counts the Router's one more tool (continue_worker)",
            data=r"^backend\.sessions\.router:Router\[\d+\]\.options\.tool_search\.defer_threshold$",
            more=1,
        ),
        Allow(
            "the agents' view names each model as its agent lists it (once listed)",
            data=r"\./api/state\.agents\.model_names$",
        ),
        Allow(
            "spawn_worker can compare agents (compare_with), when the owner asks",
            data=r"\.options\.tools\[\d+\]\.parameters\.properties\.compare_with$",
        ),
    ],
)

# Prepared for you (shepherd.py, docs/guide.md): off by default. Off, its keys are in the
# settings at their off values and one line in Settings → General is new; nothing else (no
# table, no read of GitHub, no session's options). On, with no PR of the owner's in scope: that
# line checked, its tab in Settings (left out of the views: a tab the baseline has not), the
# `shepherd` part of the state (its section on Home stays empty), its tables, the day's
# retention mark, and the Router's rule and the `shepherd` field of spawn_worker and
# message_session.
SHEPHERD_TABLES = r"(pr_claims|shepherd_steps|actions|ledger_events|verdicts)"
FEATURES["proactive"] = Feature(
    off={"proactive": False},
    on={"proactive": True},
    allow_off=[
        Allow("its setting, off", setting="proactive", value=False),
        Allow("PRs my workers made, its default", setting="proactive_worker_prs", value=True),
        Allow("my other PRs, its default", setting="proactive_my_prs", value=True),
        Allow("no kind's level chosen", setting="proactive_levels", value={}),
        Allow("no credits it may spend a day without asking", setting="proactive_budget", value=0),
        Allow("no other hosts fixes may download from", setting="step_hosts", value=[]),
        Allow("no repository's choices", setting="proactive_repos", value={}),
        Allow(
            "its switch line in Settings → General",
            element="#settings-dialog #proactive-line",
            views=r"^settings-general-",
        ),
    ],
    allow_on=[
        Allow(
            "its switch line in Settings → General, checked",
            element="#settings-dialog #proactive-line",
            views=r"^settings-general-",
        ),
        Allow(
            "its tab in Settings, and its section (not photographed: the baseline has none)",
            element="#settings-tabs [data-tab=proactive], #settings-pane .tab[data-tab=proactive]",
            views=r"^settings-",
        ),
        Allow(
            "its tab's fields in Settings",
            data=r"^page\.settings\.fields\.(?:proactive_worker_prs|proactive_my_prs|level_[\w-]+"
            r"|proactive_budget|step_hosts)$",
        ),
        Allow("its part of the state", data=r"\./api/state\.shepherd$"),
        Allow("its tables", data=rf"\.db\.{SHEPHERD_TABLES}$"),
        Allow(
            "its tables and indexes in the schema",
            data=rf"\.db\._schema\.{SHEPHERD_TABLES}(_by_\w+)?$",
        ),
        Allow("the day its old plans were last pruned", data=r"\.db\.kv\.proactive:pruned$"),
        Allow(
            "the Router's rule while it is on",
            data=r"\.system_message\.content$",
            lines=r"^- Prepared for you is on: ",
        ),
        Allow(
            "the `shepherd` field of spawn_worker and message_session",
            data=r"^backend\.sessions\.router:[^.]*\.options\.tools\[\d+\]\.parameters\.properties\.shepherd$",
        ),
    ],
)

# The agent picker with other coding agents installed and never decided on (the stand-ins are on
# the PATH in every run but --copilot-alone): v0.1.48's last line of Settings → Models is gone;
# Settings has a Coding agents tab after Models, which looks at them only when shown (left out
# here, so the page never shows it); at its start the app glances at each once (its CLI's version,
# then its sign-in, in its own folder) for a first-run card on Home: the stand-in `claude` is
# signed in, so Claude's card shows (published once: one more state event); the stand-in `codex`
# fails `codex --version`, so none for it. Allowed in every run but --copilot-alone, whatever the
# features.
INSTALLED = [
    Allow(
        "v0.1.48's line at the end of Settings → Models, gone (the Coding agents tab instead)",
        element="#settings-dialog #agents-line",
        views=r"^settings-models-",
    ),
    Allow(
        "the Coding agents tab, after Models (left out: never shown here)",
        element="#settings-tabs #agents-tab",
        views=r"^settings",
    ),
    Allow("the agents found on the PATH, in the page's state", data=r"\./api/state\.agents_found$",
          value=["codex", "claude"]),
    Allow("Claude's first-run card, in the page's state", data=r"\./api/state\.agent_offers$"),
    Allow(
        "each agent's status for the @ picker (ready, sign-in needed, not installed), in the "
        "page's state",
        data=r"\./api/state\.agents_status$",
    ),
    Allow(
        "Claude's first-run card on Home (the notices it alone is in; Home shows at the corners "
        "of the Settings dialog too)",
        element="#notices:has(> #agent-offer:only-child), #notices[hidden]:empty",
        views=r"^(home|settings)",
    ),
    Allow("the first-run card published: one more state event", data=r"^steps\.\w+\.bus\.state$",
          more=1),
    Allow(
        "the Router's composer: @ picks an agent too (\"Ask anything… type @ to pick an agent or a "
        "worker\")",
        element="#composer-input",
        views=r"^(home|sidebar-\d)",
    ),
    *(
        Allow(f"the glance at startup: {run}", data=r"^agent_clis\[\d+\]$", value=run)
        for run in ("codex --version", "claude --version", "claude auth status --json")
    ),
    *(
        Allow(f"the glance at startup: {module}", data=r"^agent_modules\[\d+\]$", value=module)
        for module in ("chiefofstaff.backend_codex", "chiefofstaff.codex_check",
                       "chiefofstaff.backend_claude")
    ),
]  # fmt: skip

# Settings v2, phase 1 (docs/design/settings-v2.md §9.5): an intentional change of the Settings
# view, so for this release it is compared by what it holds (every named field once mounted, by
# name: its kind and value) and what it does (Save with nothing changed: what it sends and what
# is stored), not by its pixels; Home stays strict, also as it is after Settings closed
# (home-after-settings-*). Allowed in every run. The next release's baseline is this one, and
# compares Settings' pictures again (this list removed).
SETTINGS_V2 = [
    Allow(
        "Settings v2 (docs/design/settings-v2.md): its views are compared by fields and round trip",
        views=r"^settings",
        pixels=False,
    ),
    Allow(
        "Settings v2: Copilot's and each agent's model, a menu that keeps what it can't list "
        "(§8.7), not a free text field: the same value",
        data=r"^page\.settings\.fields\.(?:model|model_(?:router|worker|slack|watchdog)|agent_\w+_model)$",
        without=r"^(?:input:text|select:select-one):",
    ),
    Allow(
        "Settings v2: an agent's effort, settable now (§9.1)",
        data=r"^page\.settings\.fields\.agent_\w+_effort$",
    ),
]

# Copilot alone (--copilot-alone: no other coding agent on the PATH, the owner's hidden too): the
# agent picker shows nothing of other agents anywhere, so this tree is v0.1.48 itself but for the
# one line v0.1.48 ended Settings → Models with, which is gone. Nothing looked for or run, no
# agent module imported, no request about them, no snapshot key: each would be a difference.
COPILOT_ALONE = [
    Allow(
        "v0.1.48's line at the end of Settings → Models, gone (nothing of other agents shows)",
        element="#settings-dialog #agents-line",
        views=r"^settings-models-",
    ),
]

# A busy worker's new task in a second worker with its context (forks.py,
# docs/design/worker-fork.md): on by default (the owner asked for it to be automatic), so it is
# compared switched off. Off: only its switch line in Settings → General. On, with no worker
# ever given a new task while busy (the script never does): that line checked, the Router's rule
# for busy workers (the last of its role), `same_worker` and `parallel` on its message_session
# and each open worker's load where the Router reads about workers (QA E87).
FEATURES["worker_fork"] = Feature(
    off={"fork_busy": False},
    on={"fork_busy": True},
    allow_off=[
        Allow("its setting, off", setting="fork_busy", value=False),
        Allow(
            "its switch line in Settings → General",
            element="#settings-dialog label:has(> input[name=fork_busy])",
            views=r"^settings-general-",
        ),
    ],
    allow_on=[
        Allow(
            "its switch line in Settings → General, checked",
            element="#settings-dialog label:has(> input[name=fork_busy])",
            views=r"^settings-general-",
        ),
        Allow(
            "the Router's rule for busy workers' new tasks",
            data=r"\.system_message\.content$",
            lines=r"^- Each open worker's load \(list_sessions, find_sessions, the related workers ",
        ),
        Allow("same_worker on the Router's message_session", data=r"same_worker"),
        Allow(
            "parallel on the Router's message_session",
            data=r"^backend\.sessions\.router:.*\.parameters\.properties\.parallel$",
        ),
        Allow(
            "fork_of on the Router's spawn_worker (a new worker from a related one's context)",
            data=r"^backend\.sessions\.router:.*\.parameters\.properties\.fork_of$",
        ),
        Allow(
            "each open worker's load where the Router reads about workers (list_sessions), and "
            "fork_of where spawn_worker lists related workers",
            data=r"^backend\.turns\.router:",
            without=r', "load": \{[^{}]*\}| \(with fork_of the session_id of the one that knows '
            r"the subject, when the work needs what it knows: the new worker starts with its "
            r"conversation so far, or a brief of it where the conversation can't be copied\)"
            r"| A busy one too \(its load says busy\): a second worker with its conversation so "
            r"far, or a brief of it where the conversation can't be copied, takes the task at "
            r"once\.",
        ),
    ],
)

# What another coding agent would leave if it were used while off: its CLI run, its modules.
AGENT_MODULES = (
    "chiefofstaff.backend_codex",
    "chiefofstaff.agentrun",
    "chiefofstaff.codex_check",
    "chiefofstaff.backend_claude",
    "chiefofstaff.claude_check",
    "claude_agent_sdk",
)
AGENT_CLIS = ("codex", "claude")
CLAUDE_ANSWERS = (
    'case "$*" in\n'
    '  --version) echo "2.1.322 (Claude Code)"; exit 0 ;;\n'
    '  \'auth status --json\') echo \'{"loggedIn": true, "authMethod": "claude.ai", '
    '"apiProvider": "firstParty"}\'; exit 0 ;;\n'
    "esac\n"
)

# The side under test: the app in-process on a scripted Copilot, in its own tree's environment.

ENVELOPE = re.compile(r"<message ([^>]*)>\n(.*?)\n</message>", re.DOTALL)
ATTRIBUTE = re.compile(r'(\w+)="([^"]*)"')
ASK = "Fix the flaky login test in acme-api, and compare a few feature-flag services for us."
UPGRADE = "Upgrade the payments SDK in acme-api to version 5."
FIX, COMPARE, SDK = (
    "Fix the flaky login test",
    "Compare feature-flag services",
    "Upgrade the payments SDK",
)
DM = "D0DIFFMAYA"
LOGIN = '''"""Login with expiring tokens."""

import time

TOKEN_SECONDS = 60


def issue(user: str) -> dict:
    return {"user": user, "expires": time.time() + TOKEN_SECONDS}
'''
LOGIN_FIXED = LOGIN.replace("(user: str)", "(user: str, now=time.time)").replace(
    "time.time() +", "now() +"
)


def envelopes(prompt: str) -> list[tuple[dict, str]]:
    return [(dict(ATTRIBUTE.findall(head)), body) for head, body in ENVELOPE.findall(prompt)]


def role(session) -> str:
    prompt = session.system_prompt
    if "Router inside Chief of Staff" in prompt:
        return "router"
    if "Slack session inside Chief of Staff" in prompt:
        return "slack"
    if "Chief of Staff's Watchdog" in prompt:
        return "watchdog"
    return "worker"


def brain():
    """What each session does with what it is sent: the same on both sides."""
    from chiefofstaff.scripted import CallTool, Say, Shell, WriteFile

    async def router(session, prompt):
        for attrs, body in envelopes(prompt):
            sender, kind = attrs.get("from", ""), attrs.get("type", "")
            if sender == "owner" and kind == "message" and "flaky login test" in body:
                listed = json.loads((yield CallTool("list_sessions", {})))
                project = listed["projects"][0]["path"] if listed.get("projects") else ""
                yield CallTool(
                    "spawn_worker",
                    {"title": FIX, "instructions": "The login tests fail now and then. Find out "
                     "why and fix it.", "project": project, "isolate": True,
                     "done_when": ["`uv run pytest -q` passes"]},
                )  # fmt: skip
                yield CallTool(
                    "spawn_worker",
                    {"title": COMPARE, "instructions": "Compare three feature-flag services for "
                     "a small team: price, SDKs, self-hosting.",
                     "why_new": "Research, not about the login code."},
                )  # fmt: skip
                yield Say("Started two tasks: the flaky login test, then a comparison.")
            elif sender == "owner" and kind == "message" and "payments SDK" in body:
                yield CallTool(
                    "spawn_worker",
                    {"title": SDK, "instructions": body, "why_new": "Another change."},
                )
                yield Say("Started a worker on the SDK upgrade.")
            elif sender == "owner" and kind == "message":
                yield Say("Noted.")
            elif sender.startswith("worker:") and kind == "report":
                if attrs.get("status") in {"question", "blocked", "failed"}:
                    yield CallTool(
                        "notify_owner",
                        {"text": f"“{attrs.get('title')}”: {body.strip()}", "needs_action": True},
                    )
            elif sender == "slack" and kind == "event":
                sessions = json.loads((yield CallTool("list_sessions", {})))["sessions"]
                if slack := next((s for s in sessions if s["role"] == "slack"), None):
                    yield CallTool(
                        "message_session", {"session_id": slack["session_id"], "text": body}
                    )

    async def slack(session, prompt):
        if "Maya" in prompt:
            yield CallTool(
                "draft_slack_reply",
                {"channel_id": DM, "thread_ts": "", "to": "Maya Chen", "text": "Yes! The login "
                 "fix is waiting for review; I'll ping you when it ships.",
                 "note": "Maya asks whether the login fix goes out today."},
            )  # fmt: skip
            yield Say("Drafted a reply for Maya.")
            return
        yield Say("Nothing to reply to.")

    async def watchdog(session, prompt):
        yield Say("Reviewed.")

    async def worker(session, prompt):
        for attrs, _body in envelopes(prompt):
            sender, kind = attrs.get("from", ""), attrs.get("type", "")
            task = attrs.get("task", "")
            if sender == "router" and kind == "task" and attrs.get("title") == FIX:
                yield Shell("uv run pytest -q tests/test_login.py", "F\n1 failed in 0.21s", 1,
                            seconds=0.05, note="Reproducing the flaky test first.")  # fmt: skip
                yield WriteFile("app/login.py", LOGIN_FIXED)
                yield Shell("uv run pytest -q", "........\n8 passed in 0.29s", seconds=0.05)
                yield CallTool(
                    "report",
                    {"status": "done", "task_id": task, "summary": "Fixed the flaky login test."
                     "\n- Cause: token expiry read the real clock\n- Fix: `app/login.py` takes "
                     "the clock as a parameter\n- `uv run pytest -q`: 8 passed",
                     "criteria": [{"criterion": "`uv run pytest -q` passes", "result": "met",
                                   "evidence": "`uv run pytest -q`: 8 passed"}]},
                )  # fmt: skip
                return
            if sender == "router" and kind == "task" and attrs.get("title") == COMPARE:
                yield CallTool(
                    "ask_decision",
                    {"question": "Price the comparison for how many seats?",
                     "why": "Prices change a lot between a team of 10 and one of 50.",
                     "options": [{"choice": "10 seats", "effect": "Today's team"},
                                 {"choice": "50 seats", "effect": "Next year's team"}],
                     "recommendation": "50 seats: the choice should last"},
                )  # fmt: skip
                yield Say("Asked the owner how many seats to price.")
                return
            if sender == "router" and kind == "task" and attrs.get("title") == SDK:
                yield Shell("uv add payments-sdk==5", "error: no access to the private index", 1,
                            seconds=0.05)  # fmt: skip
                yield CallTool(
                    "report",
                    {"status": "blocked", "task_id": task, "summary": "Version 5 is on the "
                     "vendor's private index, which needs a token I do not have."},
                )  # fmt: skip
                return
            if sender == "owner" and kind == "decision":
                yield CallTool(
                    "report",
                    {"status": "done", "task_id": task, "summary": "Compared three services "
                     "for 50 seats.\n- **Hosted A**: free up to 10 seats\n- **Hosted B**: usage "
                     "pricing\n- **Open source C**: self-hosted\n- Recommendation: A"},
                )  # fmt: skip
                return
        yield Say("OK.")

    scripts = {"router": router, "slack": slack, "watchdog": watchdog}
    return lambda session, prompt: scripts.get(role(session), worker)(session, prompt)


class FakeSlack:
    """Slack without Slack: one DM from Maya once the script says so; what is sent is kept."""

    url = "https://slack.differential.invalid/mcp"

    def __init__(self, record):
        self.record = record
        self.armed = False
        self.stamp = ""

    def arm(self):
        """Maya's DM, written now."""
        self.armed = True
        self.stamp = f"{int(time.time())}.000200"

    async def list_tools(self):
        from types import SimpleNamespace

        def tool(name, read_only=True, description=""):
            return SimpleNamespace(
                name=name, description=description,
                annotations=SimpleNamespace(readOnlyHint=read_only),
            )  # fmt: skip

        return [
            tool(
                "slack_search_public_and_private",
                description="Current user's user_id is U0DIFFYOU.",
            ),
            tool("slack_read_thread"),
            tool("slack_read_channel"),
            tool("slack_send_message", read_only=False),
        ]

    async def call(self, name: str, arguments: dict) -> str:
        self.record("slack", {"tool": name, "arguments": arguments})
        if name == "slack_send_message":
            sent = f"{int(time.time())}000300"
            return (
                f"Message sent: https://diff.slack.com/archives/{arguments['channel_id']}/p{sent}"
            )
        if name == "slack_search_public_and_private":
            if arguments.get("query") == "to:me" and self.armed:
                self.armed = False
                return (
                    "Results:\n1. DM from Maya Chen: hey! is the login fix going out today?\n"
                    f"   https://diff.slack.com/archives/{DM}/p{self.stamp.replace('.', '')}"
                )
            return "No results found."
        if not self.stamp:
            return "No messages."
        return (
            f"Channel: <#{DM}> | From: <@U0DIFFMAYA> (Maya Chen) | ts: {self.stamp}\n"
            "Text: hey! is the login fix going out today?"
        )


class Tokens:
    def get(self) -> str:
        return "xoxp-differential"

    def save(self, token: str):
        pass

    def delete(self):
        pass


def others(project: str) -> list[dict]:
    """The owner's own Copilot sessions: one open in a terminal, asking the owner something, one
    older; made in a terminal (as Copilot's list says), their logs as the CLI writes them."""
    from types import SimpleNamespace

    def meta(session_id: str, summary: str, age: timedelta):
        context = SimpleNamespace(working_directory=project, repository="", branch="main")
        found = SimpleNamespace(session_id=session_id, summary=summary, is_remote=False,
                                modified_time=CLOCK - age, context=context)  # fmt: skip
        vars(found).update(client_name="github/cli", last_client_name="", clients_listed=True)
        return found

    def log(session_id: str, age: timedelta, *events: tuple) -> list[dict]:
        start = {"sessionId": session_id, "version": 1, "producer": "copilot-agent"}
        written = []
        for index, (kind, data) in enumerate([("session.start", start), *events]):
            at = (CLOCK - age).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            written.append({"type": kind, "data": data, "timestamp": at,
                            "id": f"{session_id[:8]}-{index}"})  # fmt: skip
        return written

    asking, older = "0d3e8c7a-5b1f-4c2e-9a6d-2f8b1c4e7a90", "7b2e4f1a-9c3d-4e8b-a5f6-1d0c9e8b7a64"
    ask = {"toolCallId": "c-1", "name": "ask_user", "arguments": {"message": "Batch them now?"}}
    return [
        {"meta": meta(asking, "Profile the slow orders page", timedelta(minutes=5)),
         "open": True,
         "log": log(asking, timedelta(minutes=5),
                    ("user.message", {"content": "Why is the orders page slow?"}),
                    ("assistant.turn_start", {"turnId": "0"}),
                    ("assistant.message", {"content": "One query per order line; batch them.",
                                           "toolRequests": [ask]}),
                    ("tool.execution_start", {"toolCallId": "c-1", "toolName": "ask_user",
                                              "arguments": ask["arguments"]}))},
        {"meta": meta(older, "Write the API changelog", timedelta(days=1)),
         "log": log(older, timedelta(days=1),
                    ("user.message", {"content": "Draft a changelog entry for the login API."}),
                    ("assistant.turn_start", {"turnId": "0"}),
                    ("assistant.message", {"content": "Drafted it in CHANGELOG.md."}),
                    ("assistant.turn_end", {"turnId": "0"}),
                    ("session.shutdown", {"shutdownType": "routine"}))},
    ]  # fmt: skip


def plain(value: Any) -> Any:
    """Options, tools and SDK objects as JSON (a function as <function>)."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"))
    if callable(value):
        return "<function>"
    if hasattr(value, "__dict__"):
        return plain(vars(value))
    return repr(value)


def decoded(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "{[":
        with contextlib.suppress(ValueError):
            return json.loads(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def dump(store) -> dict:
    """Every table: keyed by its primary key when that is text, else rows in order; and the
    schema (`_schema`: each table's and index's SQL, by name)."""
    tables: dict[str, Any] = {}
    with store.lock:
        tables["_schema"] = {
            row[0]: row[1]
            for row in store.db.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        }
        names = [
            row[0]
            for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        for name in names:
            columns = list(store.db.execute(f'PRAGMA table_info("{name}")'))
            keys = [c["name"] for c in columns if c["pk"]]
            text_key = (
                len(keys) == 1
                and "INT" not in str(next(c["type"] for c in columns if c["pk"])).upper()
            )
            query = f'SELECT * FROM "{name}"'  # noqa: S608  (names from sqlite_master)
            try:
                rows = [dict(row) for row in store.db.execute(f"{query} ORDER BY rowid")]
            except sqlite3.OperationalError:  # A table without rowid.
                rows = [dict(row) for row in store.db.execute(f"{query} ORDER BY 1")]
            rows = [{k: decoded(v) for k, v in row.items()} for row in rows]
            if text_key or (len(keys) == 1 and name == "settings"):
                tables[name] = {str(row.pop(keys[0])): row for row in rows}
            else:
                tables[name] = rows
    return tables


class Periodic:
    """The app's periodic rounds wait for the script's ticks instead of the wall clock."""

    def __init__(self):
        self.event = asyncio.Event()
        self.waiting: set = set()
        self.real = asyncio.sleep

    def install(self):
        def sleep(delay, result=None):
            caller = sys._getframe(1).f_globals.get("__name__", "")
            if delay >= PERIODIC and caller.startswith("chiefofstaff."):
                return self.wait(result)
            return self.real(delay, result)

        asyncio.sleep = sleep

    async def wait(self, result):
        event, task = self.event, asyncio.current_task()
        self.waiting.add(task)
        try:
            await event.wait()
        finally:
            self.waiting.discard(task)
        return result

    def tick(self):
        self.event.set()
        self.event = asyncio.Event()


class Scenario:
    """The script, run against one tree; what it saw is written to spec["capture"]."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.run = Path(spec["run"])
        self.port = spec["port"]
        self.step = "start"
        self.records: dict[str, list] = {"notices": [], "slack": [], "backend": []}
        self.published: Counter = Counter()
        self.steps: dict[str, Any] = {}
        self.settled: dict[str, bool] = {}

    def record(self, kind: str, entry: dict):
        self.records[kind].append({"step": self.step, **entry})

    def prepare(self):
        """Fixed ids, clock and periodic rounds: before the app is imported. An id is drawn from
        where it is made (the two callers above uuid4) and how many were made there, so a new
        caller elsewhere leaves the others' ids as they were."""
        import hashlib
        import secrets
        import uuid

        import time_machine

        made: Counter = Counter()

        def drawn(size: int) -> bytes:
            frame, where = sys._getframe(2), []
            while frame and len(where) < 2:
                where.append(f"{frame.f_globals.get('__name__')}:{frame.f_code.co_name}")
                frame = frame.f_back
            made[tuple(where)] += 1
            return hashlib.sha512(f"{where}#{made[tuple(where)]}".encode()).digest()[:size]

        uuid.uuid4 = lambda: uuid.UUID(bytes=drawn(16), version=4)
        secrets.token_hex = lambda nbytes=None: drawn(nbytes or 32).hex()
        secrets.choice = lambda sequence: sequence[int.from_bytes(drawn(4)) % len(sequence)]
        self.clock = time_machine.travel(CLOCK, tick=False).start()
        self.periodic = Periodic()
        self.periodic.install()

    def build(self):
        import chiefofstaff
        from chiefofstaff import machine
        from chiefofstaff.models import Settings
        from chiefofstaff.runtime import Runtime
        from chiefofstaff.scripted import ScriptedBackend
        from chiefofstaff.sources.slack import SlackSource
        from chiefofstaff.web import create_app

        where = Path(chiefofstaff.__file__).resolve()
        if not where.is_relative_to(Path(self.spec["tree"]).resolve()):
            raise RuntimeError(f"chiefofstaff comes from {where}, not from {self.spec['tree']}")
        # The machine as the app sees it: plenty of memory and disk, nothing else running.
        machine.free_memory = lambda: None
        machine._cached = (0.0, None)
        machine.read_disk = lambda paths: {
            "free": 400 * machine.GB, "total": 1000 * machine.GB, "path": paths[0] if paths else "/"
        }  # fmt: skip

        async def disk_used(paths, limit=600, depth=0):
            return {str(path): 1_000_000 for path in paths if Path(path).exists()}

        async def process_cwds(limit=30):
            return set()

        machine.disk_used = disk_used
        machine.process_cwds = process_cwds

        project = str(self.run / "code" / "acme-api")
        wanted = {
            "projects": [project], "code_folder": str(self.run / "code"), "max_workers": 1,
            "slack_enabled": True, "slack_poll_seconds": 900, "mac_notifications": True,
        }  # fmt: skip
        given = self.spec["settings"]
        unknown = set(given) - set(Settings.model_fields)
        if unknown and self.spec.get("baseline"):
            given = {key: value for key, value in given.items() if key not in unknown}
        elif unknown:
            raise RuntimeError(f"No such settings in {self.spec['tree']}: {sorted(unknown)}")
        settings = Settings.model_validate({**wanted, **given})
        scenario = self

        class Recording(ScriptedBackend):
            async def create_session(self, *, session_id, name, **options):
                session = await super().create_session(session_id=session_id, name=name, **options)
                return scenario.opened(session, "create", name, options)

            async def resume_session(self, session_id, **options):
                session = await super().resume_session(session_id, **options)
                return scenario.opened(session, "resume", "", options)

        self.backend = Recording(brain(), others=others(project))
        # Their logs in the run's Copilot home (where a tree that reads them looks).
        self.backend.home = Path(os.environ["COPILOT_HOME"])
        self.slack = FakeSlack(self.record)

        def factory(store, bus):
            async def notifier(title, text):
                self.record("notices", {"title": title, "text": text})

            store.save_settings(settings)
            publish = bus.publish

            def published(kind, data=None):
                self.published[kind] += 1
                publish(kind, data)

            bus.publish = published
            runtime = Runtime(store, bus, backend_factory=lambda s: self.backend, notifier=notifier)
            runtime.slack = SlackSource(store, runtime, tokens=Tokens(), mcp=self.slack)
            self.runtime = runtime
            return runtime

        return create_app(self.run / "data", TOKEN, self.port, runtime_factory=factory)

    def opened(self, session, how: str, name: str, options: dict):
        self.record("backend", {"call": how, "session": session.session_id, "name": name,
                                "options": plain(options)})  # fmt: skip
        send, set_model, abort = session.send, session.set_model, session.abort

        async def sending(prompt, *args, **kwargs):
            self.record("backend", {"call": "send", "session": session.session_id,
                                    "prompt": prompt, "how": plain([args, kwargs])})  # fmt: skip
            return await send(prompt, *args, **kwargs)

        async def model(name, *args, **kwargs):
            self.record("backend", {"call": "set_model", "session": session.session_id,
                                    "model": name, "how": plain([args, kwargs])})  # fmt: skip
            return await set_model(name, *args, **kwargs)

        async def aborting():
            self.record("backend", {"call": "abort", "session": session.session_id})
            return await abort()

        session.send, session.set_model, session.abort = sending, model, aborting
        return session

    def fingerprint(self) -> tuple:
        runtime, backend = self.runtime, self.backend
        return (
            runtime.store.db.total_changes,
            sum(self.published.values()),
            sum(len(history) for history in backend.histories.values()),
            sum(1 for s in backend.sessions.values() if s.turn and not s.turn.done()),
            sum(1 for job in runtime.jobs if not job.done() and job not in self.periodic.waiting),
            len(asyncio.all_tasks()) - len(self.periodic.waiting),
        )

    async def settle(self, limit: float = 60) -> bool:
        """Until nothing has moved for a while: no turn, no job, no write, no event."""
        quiet, last = 0, None
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            await self.periodic.real(0.05)
            now = self.fingerprint()
            quiet = quiet + 1 if now == last else 0
            last = now
            if quiet >= 12:
                return True
        return False

    async def get(self, client, path: str) -> Any:
        response = await client.get(path)
        if response.status_code != 200:
            return {"status": response.status_code, "body": response.text[:2000]}
        return response.json()

    async def snapshot(self, client) -> dict:
        state = await self.get(client, "/api/state")
        since = int((CLOCK - timedelta(days=1)).timestamp())
        api = {"/api/state": state}
        for path in ("/api/models", "/api/workers/closed", "/api/external", "/api/storage",
                     f"/api/usage?since={since}"):  # fmt: skip
            api[path] = await self.get(client, path)
        for session in [*state.get("sessions", []), *state.get("closed", [])]:
            base = f"/api/sessions/{session['id']}"
            api[base] = await self.get(client, base)
            api[f"{base}/messages"] = await self.get(client, f"{base}/messages?limit=400")
        return {
            "api": api,
            "db": dump(self.runtime.store),
            "bus": dict(sorted(self.published.items())),
        }

    async def act(self, client, name: str):
        """One step of the script, as the owner (through the API) or the world does it; one
        that cannot be done (no card to answer) is a difference too."""
        try:
            await self.do(client, name)
        except Exception as exc:
            self.record("backend", {"call": "owner", "path": name, "status": "failed",
                                    "answer": f"{type(exc).__name__}: {exc}"})  # fmt: skip

    async def do(self, client, name: str):
        router = self.runtime.store.get("router_id")
        state = (await client.get("/api/state")).json()
        pending = [d for d in state["drafts"] if d["status"] == "pending"]
        if name == "ask":
            await self.post(client, f"/api/sessions/{router}/messages", {"text": ASK})
        elif name == "decide":
            decision = next(d for d in pending if d["kind"] == "decision")
            await self.post(
                client, f"/api/drafts/{decision['id']}", {"action": "answer", "text": "50 seats"}
            )
        elif name == "slack":
            self.slack.arm()
            self.runtime.slack.wake()
        elif name == "approve":
            reply = next(d for d in pending if d["kind"] == "slack")
            await self.post(
                client, f"/api/drafts/{reply['id']}", {"action": "send", "text": reply["text"]}
            )
        elif name == "blocked":
            await self.post(client, f"/api/sessions/{router}/messages", {"text": UPGRADE})
        elif name == "accept":
            task = next(t for t in state["tasks"] if t["title"] == FIX)
            await self.post(client, f"/api/tasks/{task['id']}/accept", {})
        elif name == "tick":
            self.clock.shift(timedelta(minutes=TICK_MINUTES - STEP_MINUTES))
            self.periodic.tick()

    async def post(self, client, path: str, body: dict):
        response = await client.post(path, json=body)
        self.record("backend", {"call": "owner", "path": path, "status": response.status_code,
                                "answer": response.text[:400]})  # fmt: skip

    async def agents_drained(self, name: str = "codex"):
        """More agents' on mode: Codex turned on, then off, as a Settings save of the Coding
        agents group does it; the events it publishes are not the script's."""
        from chiefofstaff import agents

        store, published = self.runtime.store, Counter(self.published)
        for enabled in (True, False):
            before = agents.configured(store)
            if agents.save(store, agents.planned(store, {name: {"enabled": enabled}})):
                await self.runtime.agents_saved(before)
        await self.settle()
        self.published = published

    async def claude_drained(self):
        """Claude workers' on mode: Claude turned on, then off, as Codex's is."""
        await self.agents_drained("claude")

    async def codex_on(self):
        """Codex turned on (and left on), as a Settings save does it, on either side."""
        from chiefofstaff import agents

        store, published = self.runtime.store, Counter(self.published)
        before = agents.configured(store)
        if agents.save(store, agents.planned(store, {"codex": {"enabled": True}})):
            await self.runtime.agents_saved(before)
        await self.settle()
        self.published = published

    def router_options_kept(self) -> bool:
        """Whether every time the Router's session was opened, it had the same options."""
        router = self.runtime.store.get("router_id")
        opened = [
            json.dumps(entry["options"], sort_keys=True, default=str)
            for entry in self.records["backend"]
            if entry["call"] in {"create", "resume"}
            and self.runtime.row_id(entry["session"]) == router
        ]
        return len(set(opened)) <= 1

    def sessions(self) -> dict[str, str]:
        """Copilot session id -> a name that holds on both sides: role and title."""
        names: dict[str, str] = {}
        rows = self.runtime.store.all("SELECT id, role, title FROM sessions ORDER BY rowid")
        by_row = {row["id"]: f"{row['role']}:{row['title']}" for row in rows}
        for entry in self.records["backend"]:
            copilot = entry.get("session")
            if copilot and copilot not in names:
                name = by_row.get(self.runtime.row_id(copilot)) or f"other:{entry.get('name')}"
                taken = sum(1 for n in names.values() if n == name or n.startswith(f"{name} #"))
                names[copilot] = f"{name} #{taken + 1}" if taken else name
        return names

    def backend_record(self) -> dict:
        """What the backend received, by session and step: the order between sessions running
        at once is the scheduler's, within one session it is theirs."""
        names = self.sessions()
        opened: dict[str, list] = {}
        sent: dict[str, dict[str, list]] = {}
        owner: list = []
        for entry in self.records["backend"]:
            entry = dict(entry)
            if entry["call"] == "owner":
                owner.append(entry)
                continue
            name = names.get(entry.pop("session"), "?")
            if entry["call"] in {"create", "resume"}:
                opened.setdefault(name, []).append(entry)
            else:
                sent.setdefault(entry.pop("step"), {}).setdefault(name, []).append(entry)
        turns = {
            names.get(session, session): [list(turn) for turn in history]
            for session, history in self.backend.histories.items()
        }
        return {
            "sessions": dict(sorted(opened.items())),
            "sent": {step: dict(sorted(by.items())) for step, by in sent.items()},
            "owner": owner,
            "turns": dict(sorted(turns.items())),
        }

    def by_step(self, kind: str) -> dict:
        found: dict[str, list] = {}
        for entry in self.records[kind]:
            entry = dict(entry)
            found.setdefault(entry.pop("step"), []).append(entry)
        return {step: sorted(items, key=json.dumps) for step, items in found.items()}

    async def main(self):
        import httpx
        import uvicorn

        app = self.build()
        server = uvicorn.Server(uvicorn.Config(app, port=self.port, log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await self.periodic.real(0.05)
        origin = f"http://127.0.0.1:{self.port}"
        headers = {"x-chief-key": TOKEN, "origin": origin}
        try:
            async with httpx.AsyncClient(base_url=origin, headers=headers, timeout=60) as client:
                for _ in range(200):
                    if self.runtime.ready():
                        break
                    await self.periodic.real(0.05)
                # Its first look for other agents (their first-run cards) over first, on either
                # side, whatever an action then does (a version without it has none).
                for _ in range(200):
                    offering = getattr(self.runtime, "offering", None)
                    if offering is None or offering.done():
                        break
                    await self.periodic.real(0.05)
                for action in (self.spec.get("setup"), self.spec.get("action")):
                    if action:
                        await getattr(self, action)()
                for index, name in enumerate(STEPS):
                    self.step = name
                    if index:
                        self.clock.shift(timedelta(minutes=STEP_MINUTES))
                    await self.act(client, name)
                    self.settled[name] = await self.settle()
                    self.steps[name] = await self.snapshot(client)
                    print(f"differential: step {name}", flush=True)
                now = datetime.fromtimestamp(time.time(), UTC).isoformat()
                print(f"differential: ready {now}", flush=True)
                # The page is looked at now; then what it did is captured too.
                self.step = "page"
                await asyncio.to_thread(sys.stdin.readline)
                self.settled["page"] = await self.settle()
                self.steps["page"] = await self.snapshot(client)
                gh = Path(self.spec["gh_log"])
                agents_run = Path(self.spec["agents_log"])
                capture = {
                    "steps": self.steps,
                    "settled": self.settled,
                    "backend": self.backend_record(),
                    "notices": self.by_step("notices"),
                    "slack": self.by_step("slack"),
                    "gh": sorted(gh.read_text().splitlines()) if gh.exists() else [],
                    # Another coding agent's CLI run, and its modules imported: none, ever, off.
                    "agent_clis": agents_run.read_text().splitlines()
                    if agents_run.exists()
                    else [],
                    "agent_modules": sorted(set(AGENT_MODULES) & set(sys.modules)),
                    "router_made_as_resumed": self.router_options_kept(),
                }
        finally:
            server.should_exit = True
            await serving
        Path(self.spec["capture"]).write_text(json.dumps(capture, indent=1, default=str))
        print("differential: done", flush=True)


def serve(spec: dict):
    scenario = Scenario(spec)
    scenario.prepare()
    asyncio.run(scenario.main())


# The comparison: both sides, run one after the other.


@dataclass
class Side:
    name: str
    tree: Path
    settings: dict
    action: str = ""  # Feature.on_action, on the "on" side.
    setup: str = ""  # Feature.setup, on both sides.


def claim_port() -> tuple[int, Any]:
    """A free port of PORTS, held (with the folders named after it) until this process ends."""
    WORK.mkdir(parents=True, exist_ok=True)
    for port in PORTS:
        lock = (WORK / f"port-{port}.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            continue
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)):
                return port, lock
        lock.close()
    raise RuntimeError(f"Ports {PORTS.start}-{PORTS.stop - 1} are all taken.")


def git(*arguments: str, cwd: Path = ROOT, env: dict | None = None) -> str:
    done = subprocess.run(["git", *arguments], cwd=cwd, env=env, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"git {' '.join(arguments)}: {done.stderr.strip()}")
    return done.stdout


def drop_worktree(path: Path):
    if path.exists():
        with contextlib.suppress(RuntimeError):
            git("worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)
    with contextlib.suppress(RuntimeError):
        git("worktree", "prune")


# A socket's path, its ending zero included, fits in this many bytes (macOS's sockaddr_un).
SOCKET_PATH = 104


def run_folder(port: int) -> Path:
    """The folder both sides run in: short, as the tests' home is (tests/own_home.py), so a data
    directory in it can hold Send to Chief's socket (`data/send/socket`) from any checkout,
    however long its path; in folders only its owner can change. The run folder under WORK
    when none of those is short enough."""
    import pwd
    import tempfile

    sys.path.insert(0, str(ROOT / "tests"))
    from own_home import owners_only

    bases = [tempfile.gettempdir(), os.environ.get("XDG_RUNTIME_DIR", ""),
             pwd.getpwuid(os.getuid()).pw_dir]  # fmt: skip
    for base in filter(None, bases):
        real = Path(os.path.realpath(base))
        socket_path = real / f"d{port} xxxxxxxx" / "data" / "send" / "socket"
        if real.is_dir() and len(str(socket_path)) < SOCKET_PATH and owners_only(real):
            return Path(os.path.realpath(tempfile.mkdtemp(prefix=f"d{port} ", dir=real)))
    return WORK / f"run {port}"


def make_run(run: Path, alone: bool = False) -> dict:
    """A fresh run folder, the same for both sides: a git project, a fake `gh` that keeps what
    it was asked, an empty Copilot home and git config; the environment to run in. `alone`:
    Copilot alone, no other coding agent on the PATH (the owner's hidden too)."""
    shutil.rmtree(run, ignore_errors=True)
    (run / "bin").mkdir(parents=True)
    (run / "copilot-home").mkdir()
    (run / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n")
    gh = run / "bin" / "gh"
    gh.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$DIFFERENTIAL_GH_LOG"\necho "gh: not here" >&2\nexit 1\n'
    )
    gh.chmod(0o755)
    # Other coding agents' CLIs, installed but never to be run while none is on: each run kept.
    # `claude` says its version and that it is signed in (in a Claude configuration of its own,
    # set up), so that a look at it goes all the way (codex_on_claude_never).
    for name in () if alone else AGENT_CLIS:
        cli = run / "bin" / name
        cli.write_text(
            f'#!/bin/sh\nprintf \'{name} %s\\n\' "$*" >> "$DIFFERENTIAL_AGENTS_LOG"\n'
            + (CLAUDE_ANSWERS if name == "claude" else "")
            + "exit 1\n"
        )
        cli.chmod(0o755)
    (run / "claude-config").mkdir()
    (run / "claude-config" / ".claude.json").write_text("{}\n")
    # Codex set up (its folder there, empty), as on a computer that has it: not the owner's.
    (run / "codex-home").mkdir()
    stamp = CLOCK.isoformat()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("VIRTUAL_ENV", "CHIEFOFSTAFF_", "GIT_"))
    }
    path = env.get("PATH", "")
    if alone:
        sys.path.insert(0, str(ROOT / "tests"))
        from agent_paths import without_agents

        path = without_agents(path, run / "path")
    env |= {
        "PATH": f"{run / 'bin'}{os.pathsep}{path}",
        "TZ": "UTC",
        "COPILOT_HOME": str(run / "copilot-home"),
        "GIT_CONFIG_GLOBAL": str(run / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": "owner@example.com",
        "GIT_COMMITTER_NAME": "Owner", "GIT_COMMITTER_EMAIL": "owner@example.com",
        "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
        "CHIEFOFSTAFF_EVENT_DEBOUNCE": "0",
        "CHIEFOFSTAFF_RETRY_DELAY": "0.01",
        "CHIEFOFSTAFF_UPDATE_CHECK": "0",
        "DIFFERENTIAL_GH_LOG": str(run / "gh.log"),
        "DIFFERENTIAL_AGENTS_LOG": str(run / "agents.log"),
        "CLAUDE_CONFIG_DIR": str(run / "claude-config"),
        "CODEX_HOME": str(run / "codex-home"),
    }  # fmt: skip
    project = run / "code" / "acme-api"
    (project / "app").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "README.md").write_text("# acme-api\n\nA small login service.\n")
    (project / "app" / "__init__.py").write_text("")
    (project / "app" / "login.py").write_text(LOGIN)
    (project / "tests" / "test_login.py").write_text("from app.login import issue\n")
    git("init", "-q", "-b", "main", cwd=project, env=env)
    git("add", ".", cwd=project, env=env)
    git("commit", "-qm", "Initial commit", cwd=project, env=env)
    return env


async def start_side(side: Side, run: Path, port: int, folder: Path, env: dict):
    spec = {
        "tree": str(side.tree), "run": str(run), "port": port, "settings": side.settings,
        "baseline": side.name == "baseline", "action": side.action, "setup": side.setup,
        "capture": str(folder / "capture.json"), "gh_log": env["DIFFERENTIAL_GH_LOG"],
        "agents_log": env["DIFFERENTIAL_AGENTS_LOG"],
    }  # fmt: skip
    process = await asyncio.create_subprocess_exec(
        "uv", "run", "--quiet", "--with", "time-machine", "python", str(SCRIPT),
        "--serve", json.dumps(spec),
        cwd=side.tree, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    output = (folder / "server.log").open("w")

    async def line() -> str:
        raw = await process.stdout.readline()
        if not raw:
            output.close()
            tail = "".join((folder / "server.log").read_text().splitlines(keepends=True)[-40:])
            raise RuntimeError(f"The {side.name} side stopped:\n{tail}")
        text = raw.decode(errors="replace")
        output.write(text)
        output.flush()
        return text

    while not (text := await line()).startswith("differential: ready"):
        if text.startswith("differential: step"):
            log(f"{side.name}: {text.split(': ', 1)[1].strip()}")
    clock = datetime.fromisoformat(text.split("ready ", 1)[1].strip())

    async def drain():
        with contextlib.suppress(RuntimeError):
            while not (await line()).startswith("differential: done"):
                pass
        output.close()

    return process, clock, drain


# What the page shows, the same way on both sides.

QUIET = """([ms, cap]) => new Promise((resolve) => {
  let timer;
  const observer = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(done, ms); });
  function done() { observer.disconnect(); resolve(); }
  observer.observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });
  timer = setTimeout(done, ms);
  setTimeout(done, cap);
})"""

# The DOM as lines: tag#id.classes [attributes, values of form fields] "its own text". Elements
# matching `skip` are left out (allowed differences).
DOM = """([selector, skip]) => {
  const lines = [];
  const walk = (node, depth) => {
    if (skip && node.matches(skip)) return;
    const tag = node.tagName.toLowerCase();
    if (tag === "script" || tag === "style") return;
    const attrs = [...node.attributes].filter((a) => a.name !== "id" && a.name !== "class").map((a) => a.value === "" ? a.name : `${a.name}=${JSON.stringify(a.value)}`).sort();
    if (node.matches("input, textarea, select")) {
      if (node.type === "checkbox" || node.type === "radio") attrs.push(node.checked ? ":checked" : ":unchecked");
      else attrs.push(`:value=${JSON.stringify(node.value)}`);
    }
    if (tag === "details") attrs.push(node.open ? ":open" : ":closed");
    // (A duck moves for 8 s once drawn: whether it still does depends on the machine's pace.)
    const names = typeof node.className === "string" ? node.className.trim().split(/\\s+/).filter((c) => c && !(c === "moving" && node.classList.contains("duck"))) : [];
    const classes = names.length ? "." + names.join(".") : "";
    const text = [...node.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join(" ").replace(/\\s+/g, " ").trim();
    lines.push("  ".repeat(depth) + tag + (node.id ? `#${node.id}` : "") + classes + (attrs.length ? ` [${attrs.join(" ")}]` : "") + (text ? ` ${JSON.stringify(text)}` : ""));
    if (tag !== "svg") for (const child of node.children) walk(child, depth + 1);
  };
  const root = document.querySelector(selector);
  if (root) walk(root, 0);
  return lines.join("\\n");
}"""

# Allowed elements hidden for a screenshot, then shown again. Through each one's own style (the
# page's CSP refuses an added <style> element), its style attribute as it was afterwards.
# Hiding an element keeps what scrolls to its end (the chat, pinned to its latest) at its end, as
# if the element had never been there.
HIDE = """(selector) => {
  const hidden = (window.__differentialHidden ||= new Map());
  const nodes = selector ? [...document.querySelectorAll(selector)] : [...hidden.keys()];
  const pinned = nodes.length ? [...document.querySelectorAll("*")].filter((s) => s.scrollHeight > s.clientHeight
    && ["auto", "scroll"].includes(getComputedStyle(s).overflowY) && s.scrollTop + s.clientHeight >= s.scrollHeight - 2) : [];
  if (selector) {
    for (const node of nodes) {
      if (!hidden.has(node)) hidden.set(node, node.getAttribute("style"));
      node.style.setProperty("display", "none", "important");
    }
  } else {
    for (const [node, style] of hidden) {
      if (style === null) node.removeAttribute("style");
      else node.style.cssText = style;
    }
    hidden.clear();
  }
  for (const scroller of pinned) scroller.scrollTop = scroller.scrollHeight;
}"""


class Camera:
    """Screenshots and DOM snapshots of one side, with allowed elements left out."""

    def __init__(self, folder: Path, allowances: dict[str, Allow]):
        self.folder = folder
        self.elements = {name: a for name, a in allowances.items() if a.element}
        self.unseen = {name: a for name, a in allowances.items() if not a.pixels}
        self.dom: dict[str, str] = {}
        self.hidden: Counter = Counter()  # Allowance -> screenshots it hid something in.

    def applying(self, view: str) -> dict[str, str]:
        return {
            name: a.element
            for name, a in self.elements.items()
            if not a.views or re.search(a.views, view)
        }

    def skipped(self, view: str) -> str:
        return ", ".join(self.applying(view).values())

    def unpictured(self, view: str) -> bool:
        """Whether an allowance keeps this view out of the comparison of pictures (pixels=False)."""
        return any(
            not a.pixels and (not a.views or re.search(a.views, view)) for a in self.unseen.values()
        )

    async def snapshot(self, page, view: str, selector: str, skip: str = ""):
        if self.unpictured(view):
            return
        skip = ", ".join(filter(None, [self.skipped(view), skip]))
        self.dom[view] = await page.evaluate(DOM, [selector, skip])

    async def shoot(
        self, page, view: str, scheme: str, selector: str = "", dom: str = "", restore=True
    ):
        if self.unpictured(view):
            return
        skip = self.skipped(view)
        for name, element in self.applying(view).items():
            if await page.evaluate("s => document.querySelectorAll(s).length", element):
                self.hidden[name] += 1
        if skip:
            await page.evaluate(HIDE, skip)
        if dom and scheme == SCHEMES[0]:
            await self.snapshot(page, re.sub(r"-p1-", "-", view), dom)
        path = self.folder / f"{view}-{scheme}.png"
        target = page.locator(selector) if selector else page
        await target.screenshot(path=path, animations="disabled", caret="hide")
        if skip and restore:
            await page.evaluate(HIDE, "")


async def quiet(page, ms: int = 250, cap: int = 4000):
    await page.evaluate(QUIET, [ms, cap])


async def photograph(
    port: int, folder: Path, clock: datetime, allowances: dict[str, Allow]
) -> dict:
    from playwright.async_api import async_playwright

    camera = Camera(folder, allowances)
    requests: set[str] = set()
    errors: list[str] = []
    held: dict = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for scheme in SCHEMES:
            context = await browser.new_context(
                viewport={"width": WIDTHS[-1], "height": HEIGHT}, color_scheme=scheme,
                reduced_motion="reduce", timezone_id="UTC", locale="en-US",
            )  # fmt: skip
            await context.clock.set_fixed_time(clock)
            page = await context.new_page()

            def requested(request):
                if "/api/" in request.url:
                    # (A Save's body is compared field by field: settings_round_trip's `sent`.)
                    saving = request.url.endswith("/api/settings") and request.method == "POST"
                    body = "" if saving else request.post_data or ""
                    requests.add(
                        f"{request.method} {request.url.split(str(port), 1)[-1]} {body}".strip()
                    )

            page.on("request", requested)
            page.on("pageerror", lambda error: errors.append(f"page error: {error}"))
            page.on(
                "console",
                lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None,
            )
            await page.goto(f"http://127.0.0.1:{port}/#token={TOKEN}")
            await page.locator("#application").wait_for()
            await page.evaluate("document.fonts.ready.then(() => true)")
            await quiet(page, 600)
            for width in WIDTHS:
                await page.set_viewport_size({"width": width, "height": HEIGHT})
                await quiet(page)
                # Home's chat at its latest, where the app keeps it (a resize may leave it a few
                # pixels short, depending on what was drawn before).
                await page.evaluate(PINNED)
                await camera.shoot(page, f"home-{width}", scheme, dom="#application")
                tabs = page.locator("#home-tabs")
                if await tabs.is_visible():
                    for tab in await tabs.locator("[role=tab]").all():
                        name = await tab.get_attribute("data-tab")
                        await tab.click()
                        await quiet(page)
                        await camera.shoot(page, f"home-{name}-{width}", scheme)
                    await tabs.locator("[data-tab=chat]").click()
                sidebar = page.locator("#sidebar")
                if await sidebar.is_visible():
                    await camera.shoot(page, f"sidebar-{width}", scheme, "#sidebar")
                    external = page.locator("#external > summary")
                    if await external.is_visible():
                        await external.click()
                        await quiet(page)
                        await camera.shoot(page, f"sidebar-external-{width}", scheme, "#sidebar")
                        await external.click()
                await settings(page, camera, scheme, width)
            if scheme == SCHEMES[0]:
                # What Settings holds and does: every field once mounted, and a Save of nothing
                # changed; then Home again, as it is after Settings closed.
                held = await settings_round_trip(page)
                for width in WIDTHS:
                    await page.set_viewport_size({"width": width, "height": HEIGHT})
                    await quiet(page)
                    await page.evaluate(PINNED)
                    await camera.shoot(page, f"home-after-settings-{width}", scheme)
            await context.close()
        await browser.close()
    return {"requests": sorted(requests), "errors": sorted(set(errors)), "dom": camera.dom,
            "hidden": camera.hidden, "settings": held}  # fmt: skip


PINNED = """() => { const chat = document.querySelector("#scroller"); if (chat) chat.scrollTop = chat.scrollHeight; }"""
# Agents & models shown: its look answered (data-looked, settings-v2.md §3.6); before Settings
# v2, the Coding agents tab's rows no longer "Checking…".
AGENTS_LOOKED = """() => {
  const page = document.querySelector("#settings-page-agents");
  if (page && !page.hidden) return ["none", "done", "failed"].includes(page.dataset.looked);
  return ![...document.querySelectorAll("#agents-rows [role=status]")].some(
    (line) => line.textContent === "Checking…");
}"""
# Every named control of Settings, by name: its kind and its value (checked, for a checkbox).
FIELDS = """() => Object.fromEntries([...document.querySelector("#settings-form").elements]
  .filter((field) => field.name)
  .map((field) => [field.name, `${field.tagName.toLowerCase()}:${field.type}:${
    field.type === "checkbox" ? field.checked : field.value}`]))"""
# Each page, connection, agent's row and fold of Settings shown once (all its fields mounted, as
# they would be by an owner going through it).
VISIT = """async () => {
  const pause = () => new Promise((done) => setTimeout(done, 150));
  for (const tab of document.querySelectorAll("#settings-tabs [role=tab]")) {
    if (tab.hidden) continue;
    tab.click();
    await pause();
    const page = document.querySelector(`#settings-pane > .tab[data-tab="${tab.dataset.tab}"]`);
    for (const row of page?.querySelectorAll(".conn .conn-open") || []) {
      row.click();
      await pause();
      document.querySelector("#settings-pane [data-conn-detail]:not([hidden]) [data-back]")?.click();
    }
    for (const fold of page?.querySelectorAll(".agent .fold[aria-expanded=false]") || []) fold.click();
    for (const part of page?.querySelectorAll("details") || []) part.open = true;
  }
}"""


async def settings_round_trip(page) -> dict:
    """Settings opened, every page, connection, row and fold shown (each agent looked at as the
    page looks), its fields read (`fields`), then Save with nothing changed: what it sent
    (`sent`, without `base`)."""
    await page.locator("#settings-button").click()
    dialog = page.locator("#settings-dialog")
    await dialog.wait_for()
    await page.evaluate(VISIT)
    for tab in await page.locator("#settings-tabs [role=tab]").all():
        if not await tab.is_visible():
            continue
        await tab.evaluate("b => b.click()")
        await page.wait_for_function(AGENTS_LOOKED, timeout=20000)
    await quiet(page)
    await page.evaluate(VISIT)
    fields = await page.evaluate(FIELDS)
    async with page.expect_request("**/api/settings") as request:
        await page.locator("#settings-form footer button[type=submit]").click()
    sent = (await request.value).post_data_json or {}
    sent.pop("base", None)
    await dialog.wait_for(state="hidden")
    await quiet(page)
    return {"fields": fields, "sent": sent}


async def settings(page, camera: Camera, scheme: str, width: int):
    """Each tab of Settings, its folded sections open, page by page as it scrolls."""
    if camera.unpictured(f"settings-{width}") and camera.unpictured(f"settings-general-p1-{width}"):
        return  # Compared by what it holds and does (settings_round_trip), not its pictures.
    await page.locator("#settings-button").click()
    dialog = page.locator("#settings-dialog")
    await dialog.wait_for()
    sections = "#settings-pane > .tab"
    if scheme == SCHEMES[0]:
        await camera.snapshot(page, f"settings-{width}", "#settings-dialog", sections)
    for tab in await page.locator("#settings-tabs [role=tab]").all():
        name = await tab.get_attribute("data-tab")
        skip = camera.skipped(f"settings-{name}-p1-{width}")
        if skip and await tab.evaluate("(b, s) => b.matches(s)", skip):
            continue  # A tab an allowance leaves out (its button): nothing of it is compared.
        # Clicked without the pointer: no hover is left where a button left out moved the others.
        await tab.evaluate("b => b.click()")
        await page.evaluate(
            "t => document.querySelectorAll(`#settings-pane .tab[data-tab='${t}'] details`).forEach((d) => { d.open = true; })",
            name,
        )
        await quiet(page)
        # The Coding agents group, opened, looks for agents: what it found, not its loading
        # rows (their height decides how many pages there are).
        await page.wait_for_function(AGENTS_LOOKED, timeout=20000)
        await quiet(page)
        # What an allowance leaves out is out of the count of pages too, until the tab is done.
        if left := camera.skipped(f"settings-{name}-p1-{width}"):
            await page.evaluate(HIDE, left)
        pane = page.locator("#settings-pane")
        height, shown = await pane.evaluate("p => [p.scrollHeight, p.clientHeight]")
        pages = max(1, min(6, -(-height // max(shown, 1))))
        for index in range(pages):
            await pane.evaluate("(p, top) => { p.scrollTop = top; }", index * shown)
            await quiet(page, 150)
            view = f"settings-{name}-p{index + 1}-{width}"
            dom = f"{sections}[data-tab='{name}']" if index == 0 else ""
            await camera.shoot(page, view, scheme, "#settings-dialog", dom=dom, restore=False)
        await page.evaluate(HIDE, "")
        await pane.evaluate("p => { p.scrollTop = 0; }")
    await page.locator("#settings-cancel").click()
    await dialog.wait_for(state="hidden")


# Normalizing: what differs from run to run whatever the code.

ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
EPOCH = re.compile(r"\b1[6-9]\d{8}(?:\.?\d{3,6})?\b")
PORT = re.compile(r"\b(127\.0\.0\.1|localhost):\d{2,5}\b")
# Ids, in the order they are looked for: uuids, commits, hex ids, tokens, then the app's own
# (a letter or three and hex: dec… decision, t… task or trigger, r… run, q… question, d… draft,
# l… lesson, k… check, c… case, o… owner ref, m-… message).
IDS = [
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("sha", re.compile(r"\b[0-9a-f]{40}\b")),
    ("hex", re.compile(r"\b[0-9a-f]{32}\b")),
    ("token", re.compile(r"\b[0-9a-f]{16}\b")),
    (
        "",
        re.compile(
            r"\b(?:dec[0-9a-f]{6}|[tr][0-9a-f]{7,8}|[qd][0-9a-f]{8}|[lkc][0-9a-f]{7}|o[0-9a-f]{6}|m-[0-9a-f]{8})\b"
        ),
    ),
]
PREFIXES = {"dec": "decision", "t": "task", "r": "run", "q": "question", "d": "draft",
            "l": "lesson", "k": "check", "c": "case", "o": "ref", "m": "message"}  # fmt: skip
# A session's short id (8) and its folders' suffix (6): known only as the start of a known id.
SHORT = re.compile(r"\b[0-9a-f]{6}(?:[0-9a-f]{2})?\b")
COMMIT = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,12}\b")


def offset(moment: float) -> str:
    seconds = round(moment - CLOCK.timestamp(), 3)
    sign, seconds = ("-" if seconds < 0 else "+"), abs(seconds)
    if seconds % 60 == 0:
        return f"<T{sign}{int(seconds // 60)}m>"
    return f"<T{sign}{seconds:g}s>"


class Normalizer:
    """Replaces what differs from run to run by stable placeholders. Ids are numbered by kind in
    the order collect() first meets them, then replaced everywhere (their starts too)."""

    def __init__(self, paths: list[tuple[str, str]]):
        self.paths = sorted(paths, key=lambda pair: -len(pair[0]))
        self.ids: dict[str, str] = {}
        self.starts: dict[str, str] = {}
        self.counts: Counter = Counter()

    def id(self, kind: str, raw: str) -> str:
        if raw not in self.ids:
            if not kind:
                head = "dec" if raw.startswith("dec") else raw[0]
                kind = "trigger" if head == "t" and len(raw) == 9 else PREFIXES[head]
            self.counts[kind] += 1
            self.ids[raw] = f"<{kind}-{self.counts[kind]}>"
            if kind in {"uuid", "hex"}:
                for size in (6, 8):
                    start = raw[:size]
                    self.starts[start] = "" if start in self.starts else self.ids[raw]
        return self.ids[raw]

    def time(self, raw: str) -> str:
        try:
            moment = datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            return "<time>"
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return offset(moment.timestamp())

    def epoch(self, raw: str) -> str:
        digits = raw.replace(".", "")
        seconds = float(raw) if "." in raw else float(digits[:10] + "." + digits[10:])
        return offset(seconds)

    def plain(self, value: str) -> str:
        for old, new in self.paths:
            value = value.replace(old, new)
        value = PORT.sub(r"\1:<port>", value)
        value = ISO.sub(lambda m: self.time(m.group(0)), value)
        return EPOCH.sub(lambda m: self.epoch(m.group(0)), value)

    def collect(self, value: Any):
        if isinstance(value, dict):
            for key, item in value.items():
                self.collect(str(key))
                self.collect(item)
        elif isinstance(value, list):
            for item in value:
                self.collect(item)
        elif isinstance(value, str):
            value = self.plain(value)
            for kind, pattern in IDS:
                for raw in pattern.findall(value):
                    self.id(kind, raw)
                value = pattern.sub(" ", value)

    def text(self, value: str) -> str:
        value = self.plain(value)
        for kind, pattern in IDS:
            value = pattern.sub(lambda m, kind=kind: self.id(kind, m.group(0)), value)
        value = SHORT.sub(lambda m: self.starts.get(m.group(0)) or m.group(0), value)
        return COMMIT.sub(lambda m: self.id("sha", m.group(0)), value)

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int | float):
            near = abs(value - CLOCK.timestamp()) < 400 * 86400
            near_ms = abs(value / 1000 - CLOCK.timestamp()) < 400 * 86400
            if near or near_ms:
                return offset(value if near else value / 1000)
            return value
        if isinstance(value, dict):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        return value


# Comparing.


@dataclass
class Difference:
    where: str  # api, db, backend, notices, slack, gh, page, dom, pixels
    path: str
    kind: str  # changed, added, removed
    before: Any = None
    after: Any = None
    lines: list[str] | None = None  # for changed text: the lines added (+) and removed (-)
    allowed: str = ""
    image: str = ""


def short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "⏎")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compare(before: Any, after: Any, path: str, found: list[Difference]):
    if before == after:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in before:
            if key not in after:
                found.append(Difference("", f"{path}.{key}", "removed", before=before[key]))
            else:
                compare(before[key], after[key], f"{path}.{key}", found)
        for key in after:
            if key not in before:
                found.append(Difference("", f"{path}.{key}", "added", after=after[key]))
        return
    if isinstance(before, list) and isinstance(after, list):
        keys = [json.dumps(item, sort_keys=True) for item in before]
        others = [json.dumps(item, sort_keys=True) for item in after]
        matcher = difflib.SequenceMatcher(None, keys, others, autojunk=False)
        for op, i1, i2, j1, j2 in matcher.get_opcodes():
            if op == "equal":
                continue
            if op == "replace" and i2 - i1 == j2 - j1:
                for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                    compare(before[i], after[j], f"{path}[{i}]", found)
                continue
            for i in range(i1, i2):
                found.append(Difference("", f"{path}[{i}]", "removed", before=before[i]))
            for j in range(j1, j2):
                found.append(Difference("", f"{path}[{j}]", "added", after=after[j]))
        return
    lines = None
    if isinstance(before, str) and isinstance(after, str) and ("\n" in before or "\n" in after):
        lines = [
            line
            for line in difflib.unified_diff(
                before.splitlines(), after.splitlines(), lineterm="", n=0
            )
            if line[:1] in "+-" and not line.startswith(("+++", "---"))
        ]
    found.append(Difference("", path, "changed", before=before, after=after, lines=lines))


def section(path: str) -> str:
    return path.split(".", 1)[0].split("[", 1)[0]


def allow(differences: list[Difference], allowances: dict[str, Allow]):
    for difference in differences:
        if difference.where in {"dom", "pixels"}:
            continue
        changed = [line[1:] for line in difference.lines] if difference.lines is not None else None
        value = difference.after if difference.kind != "removed" else difference.before
        for name, allowance in allowances.items():
            if allowance.matches(difference.path, value, changed, difference.before):
                difference.allowed = name
                break
        else:
            if not changed:
                continue
            # Text more than one feature changes (the Router's role, say): each changed line
            # allowed by one of them, all those named.
            by: list[str] = []
            for line in changed:
                name = next(
                    (
                        name
                        for name, allowance in allowances.items()
                        if allowance.lines
                        and allowance.matches(difference.path, value, [line], difference.before)
                    ),
                    "",
                )
                if not name:
                    break
                by += [name] if name not in by else []
            else:
                difference.allowed = " + ".join(by)


async def pixels(before: Path, after: Path, out: Path, page) -> tuple[int, str]:
    """How many pixels differ beyond anti-aliasing; a diff image when any do (the after image
    faded, what differs in red)."""
    if before.read_bytes() == after.read_bytes():
        return 0, ""
    urls = [
        f"data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}"
        for p in (before, after)
    ]
    found = await page.evaluate(
        """async ([a, b, tolerance]) => {
        const load = (src) => new Promise((ok, fail) => { const image = new Image(); image.onload = () => ok(image); image.onerror = fail; image.src = src; });
        const [x, y] = await Promise.all([load(a), load(b)]);
        const w = Math.max(x.width, y.width), h = Math.max(x.height, y.height);
        const read = (image) => { const canvas = new OffscreenCanvas(w, h); const g = canvas.getContext("2d"); g.drawImage(image, 0, 0); return g.getImageData(0, 0, w, h).data; };
        const p = read(x), q = read(y);
        const canvas = new OffscreenCanvas(w, h), g = canvas.getContext("2d"), out = g.createImageData(w, h);
        let count = 0, left = w, top = h, right = 0, bottom = 0;
        for (let i = 0; i < p.length; i += 4) {
          const delta = Math.max(Math.abs(p[i] - q[i]), Math.abs(p[i + 1] - q[i + 1]), Math.abs(p[i + 2] - q[i + 2]), Math.abs(p[i + 3] - q[i + 3]));
          if (delta > tolerance) {
            count++;
            const at = i / 4, px = at % w, py = Math.floor(at / w);
            left = Math.min(left, px); right = Math.max(right, px); top = Math.min(top, py); bottom = Math.max(bottom, py);
            out.data.set([230, 20, 40, 255], i);
          } else {
            const gray = 200 + ((q[i] + q[i + 1] + q[i + 2]) / 3) * 0.2;
            out.data.set([gray, gray, gray, 255], i);
          }
        }
        g.putImageData(out, 0, 0);
        const blob = await canvas.convertToBlob({ type: "image/png" });
        const data = await new Promise((ok) => { const reader = new FileReader(); reader.onload = () => ok(reader.result); reader.readAsDataURL(blob); });
        return { count, box: count ? [left, top, right - left + 1, bottom - top + 1] : null, sizes: [[x.width, x.height], [y.width, y.height]], data };
        }""",
        [*urls, PIXEL_DELTA],
    )  # fmt: skip
    if found["count"] <= PIXEL_LIMIT:
        return found["count"], ""
    out.mkdir(parents=True, exist_ok=True)
    stem = before.stem
    shutil.copy(before, out / f"{stem}-{before.parent.name}.png")
    shutil.copy(after, out / f"{stem}-{after.parent.name}.png")
    (out / f"{stem}-diff.png").write_bytes(base64.b64decode(found["data"].split(",", 1)[1]))
    (a_w, a_h), (b_w, b_h) = found["sizes"]
    left, top, width, height = found["box"]
    size = f"; sizes {a_w}x{a_h} and {b_w}x{b_h}" if (a_w, a_h) != (b_w, b_h) else ""
    return found["count"], f"{width}x{height} px at ({left}, {top}){size}"


async def compare_pages(folders: list[Path], doms: list[dict], out: Path) -> list[Difference]:
    from playwright.async_api import async_playwright

    found: list[Difference] = []
    before, after = doms
    for view in sorted(set(before) | set(after)):
        old, new = before.get(view), after.get(view)
        if old is None or new is None:
            found.append(Difference("dom", view, "added" if old is None else "removed"))
        elif old != new:
            lines = [
                line
                for line in difflib.unified_diff(
                    old.splitlines(), new.splitlines(), lineterm="", n=1
                )
                if not line.startswith(("+++", "---"))
            ]
            found.append(Difference("dom", view, "changed", lines=lines))
    images = sorted({p.name for folder in folders for p in folder.glob("*.png")})
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        for name in images:
            a, b = folders[0] / name, folders[1] / name
            if not a.exists() or not b.exists():
                found.append(
                    Difference("pixels", name[:-4], "added" if not a.exists() else "removed")
                )
                continue
            count, where = await pixels(a, b, out / "diff", page)
            if where:
                found.append(Difference("pixels", name[:-4], "changed", before=count, after=where,
                                        image=str(out / "diff" / f"{name[:-4]}-diff.png")))  # fmt: skip
        await browser.close()
    return found


def grouped(differences: list[Difference]) -> list[tuple[Difference, list[str]]]:
    """The same difference at every step (or every width) once, with where it was."""
    groups: dict[str, tuple[Difference, list[str]]] = {}
    for difference in differences:
        at = ""
        if difference.where == "dom":
            view, at = difference.path.rsplit("-", 1)
            key = json.dumps(["dom", view, difference.kind, difference.lines])
        elif difference.path.startswith("steps."):
            _, at, rest = difference.path.split(".", 2)
            key = json.dumps([difference.where, rest, difference.kind, difference.before,
                              difference.after, difference.lines], default=str)  # fmt: skip
        else:
            key = f"#{len(groups)}"
        groups.setdefault(key, (difference, []))[1].append(at) if at else groups.setdefault(
            key, (difference, [])
        )
    for _, at in groups.values():
        at.sort(
            key=lambda step: (
                int(step) if step.isdigit() else STEPS.index(step) if step in STEPS else 99
            )
        )
    return list(groups.values())


def describe(difference: Difference, where: list[str]) -> list[str]:
    path = difference.path
    if difference.where == "dom":
        path = re.sub(r"-\d{3,4}$", "", path)
    elif path.startswith("steps.") and where:
        path = "steps.*." + path.split(".", 2)[2]
    at = f" (at {', '.join(where)})" if where else ""
    if difference.where == "pixels":
        if difference.kind != "changed":
            return [
                f"{path}: only on the {'after' if difference.kind == 'added' else 'before'} side"
            ]
        return [
            f"{path}: {difference.before} pixels differ, in {difference.after}",
            f"    {difference.image}",
        ]
    if difference.where == "dom":
        if difference.kind != "changed":
            return [f"{path}: only on one side{at}"]
        shown = difference.lines[:40]
        more = len(difference.lines) - len(shown)
        return [
            f"{path}{at}:",
            *(f"    {line}" for line in shown),
            *([f"    … {more} more lines"] if more else []),
        ]
    if difference.kind == "added":
        return [f"{path}: added {short(difference.after)}{at}"]
    if difference.kind == "removed":
        return [f"{path}: removed {short(difference.before)}{at}"]
    if difference.lines is not None:
        shown = difference.lines[:30]
        more = len(difference.lines) - len(shown)
        return [f"{path}: text changed{at}:", *(f"    {short(line, 150)}" for line in shown),
                *([f"    … {more} more lines"] if more else [])]  # fmt: skip
    return [f"{path}: {short(difference.before)} → {short(difference.after)}{at}"]


TITLES = {
    "api": "API (GET responses after each step)",
    "db": "Store (SQLite tables after each step)",
    "bus": "Events published (by kind, after each step)",
    "backend": "Scripted Copilot (sessions, options, prompts sent, turns)",
    "notices": "Desktop notifications",
    "slack": "Slack calls",
    "gh": "gh commands",
    "agent_clis": "Other coding agents' CLIs run",
    "agent_modules": "Other coding agents' modules imported",
    "router_made_as_resumed": "The Router's session opened with the same options each time",
    "settled": "Steps that settled",
    "page": "The page (API requests, errors)",
    "dom": "DOM snapshots",
    "pixels": "Screenshots",
}


def report(
    differences: list[Difference], allowances: dict[str, Allow], hidden: Counter, title: str
) -> tuple[list[str], int]:
    lines = [title]
    failing = [d for d in differences if not d.allowed]
    for where, heading in TITLES.items():
        mine = grouped([d for d in failing if d.where == where])
        if not mine:
            continue
        lines.append(f"✗ {heading}: {len(mine)} difference{'s' * (len(mine) != 1)}")
        for difference, at in mine[:SHOWN]:
            text = describe(difference, at)
            lines += [f"  {text[0]}", *(f"  {line}" for line in text[1:])]
        if len(mine) > SHOWN:
            lines.append(f"  … {len(mine) - SHOWN} more (all in differences.json)")
    used = Counter(name for d in differences if d.allowed for name in d.allowed.split(" + "))
    if allowances:
        lines.append("Allowed:")
        for name, allowance in allowances.items():
            notes = [f"{used[name]} difference{'s' * (used[name] != 1)}"] if used[name] else []
            if allowance.element and hidden[name]:
                notes.append(f"hidden in {hidden[name]} screenshots")
            lines.append(f"  {name}: {', '.join(notes) or 'declared, not seen'}")
    return lines, len(grouped(failing))


async def run_sides(
    sides: list[Side], out: Path, allowances: dict[str, Allow], port: int, alone: bool = False
) -> tuple[list[dict], list[dict], list[Path], Path]:
    # Short, so Send to Chief's socket in its data folder fits (run_folder), its name with a
    # space, as macOS's data folder has one ("Application Support"): every path Chief of
    # Staff puts in a command must keep it whole.
    run = run_folder(port)
    captures, pages, folders = [], [], []
    try:
        for side in sides:
            started = time.monotonic()
            folder = out / side.name
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True)
            env = make_run(run, alone)
            process, clock, drain = await start_side(side, run, port, folder, env)
            try:
                page = await photograph(port, folder, clock, allowances)
                (folder / "page.json").write_text(json.dumps(page, indent=1, ensure_ascii=False))
                log(f"{side.name}: page looked at ({len(list(folder.glob('*.png')))} screenshots)")
                process.stdin.write(b"done\n")
                await process.stdin.drain()
                await asyncio.wait_for(drain(), 120)
                await asyncio.wait_for(process.wait(), 60)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            captures.append(json.loads((folder / "capture.json").read_text()))
            pages.append(page)
            folders.append(folder)
            log(f"{side.name}: done in {time.monotonic() - started:.0f} s")
    finally:
        shutil.rmtree(run, ignore_errors=True)
    return captures, pages, folders, run


# Events published on the wall clock while a page is open, not because of anything that
# happened: how many depends on how long the page was looked at. `others`: where the owner's other
# Copilot sessions stand, renewed every 2 s pass while a tab is open (others.STEADY, shorter than
# PERIODIC), so a page looked at a moment longer gets one more.
TIMED_EVENTS = {"others"}


def normalized(capture: dict, page: dict, side: Side, run: Path) -> dict:
    for step in capture.get("steps", {}).values():
        published = step.get("bus", {})
        for kind in TIMED_EVENTS & set(published):
            published[kind] = "some" if published[kind] else 0
    home = str(Path.home())
    paths = [(str(run.resolve()), "<run>"), (str(run), "<run>"), (str(side.tree.resolve()), "<tree>"),
             (str(side.tree), "<tree>"), (home, "~")]  # fmt: skip
    normalizer = Normalizer(paths)
    document = {
        **capture,
        "page": {key: page[key] for key in ("requests", "errors", "settings") if key in page},
    }
    normalizer.collect([document, page["dom"]])
    return {
        "data": normalizer.value(document),
        "dom": {view: normalizer.text(text) for view, text in page["dom"].items()},
    }


def classify(differences: list[Difference]):
    for difference in differences:
        difference.path = difference.path.lstrip(".")
        if difference.path.startswith("steps."):
            parts = difference.path.split(".")
            difference.where = parts[2] if len(parts) > 2 and parts[2] in {"db", "bus"} else "api"
        else:
            difference.where = section(difference.path)


def baseline_lacks(baseline: str) -> list[str]:
    """The features behind a setting the baseline has none of (in its Settings, models.py): off in
    every run against it, whichever feature is chosen, as one on by default (delegate_reply)
    would differ there."""
    models = git("show", f"{baseline}:src/chiefofstaff/models.py")
    return [
        name
        for name, feature in FEATURES.items()
        if feature.off
        and not any(
            re.search(rf"^\s+{re.escape(key)}\s*:", models, re.MULTILINE) for key in feature.off
        )
    ]


def plan(args, parser, port: int) -> tuple[list[Side], dict[str, Allow], str]:
    """The two sides and the differences allowed between them."""
    if args.feature and args.feature not in FEATURES:
        known = ", ".join(FEATURES) or "none yet"
        parser.error(f"no feature {args.feature!r} is registered (registered: {known})")
    if args.mode == "on" and not args.feature:
        parser.error("--mode on needs --feature")
    tree = args.tree.resolve()
    lacking = baseline_lacks(args.baseline)
    keep = args.keep_on
    if keep and (keep not in FEATURES or keep == args.feature or not FEATURES[keep].on):
        parser.error(f"--keep-on takes another feature behind a setting, not {keep!r}")
    kept: dict[str, Allow] = {}
    if keep:
        kept = {f"{keep} (kept on): {a.name}": a for a in FEATURES[keep].allow_on}
        kept |= {f"{keep} (kept on): its setting {key}": Allow(key, setting=key)
                 for key in FEATURES[keep].on}  # fmt: skip
    if args.copilot_alone:
        if args.feature or args.mode != "off":
            parser.error("--copilot-alone compares the baseline with every feature off")
        allowances = {f"fix: {a.name}": a for a in FIXES + SETTINGS_V2} | {
            f"Copilot alone: {a.name}": a for a in COPILOT_ALONE
        }
        allowances |= kept
        allowances |= {
            f"{name}: {a.name}": a
            for name in lacking
            if name != keep
            for a in FEATURES[name].allow_off
        }
        off = {key: value for name in lacking for key, value in FEATURES[name].off.items()}
        mine = {**off, **FEATURES[keep].on} if keep else off
        sides = [Side("baseline", WORK / f"baseline-{port}", off), Side("current", tree, mine)]
        title = f"Differential test, Copilot alone: {args.baseline} against {tree}"
        return sides, allowances, title + (f", {keep} on" if keep else "")
    chosen = [args.feature] if args.feature and args.mode == "off" else list(FEATURES)
    chosen += [name for name in lacking if name not in chosen]
    off = {key: value for name in chosen for key, value in FEATURES[name].off.items()}
    fixes = {f"fix: {a.name}": a for a in FIXES + SETTINGS_V2}
    installed = {f"installed: {a.name}": a for a in INSTALLED}
    if keep in chosen:
        chosen.remove(keep)
    mine = {**off, **FEATURES[keep].on} if keep else off
    if args.mode == "off":
        allowances = (
            fixes
            | installed
            | kept
            | {f"{name}: {a.name}": a for name in chosen for a in FEATURES[name].allow_off}
        )
        sides = [Side("baseline", WORK / f"baseline-{port}", off), Side("current", tree, mine)]
        if args.feature:
            for side in sides:
                side.setup = FEATURES[args.feature].setup
            allowances |= {
                f"{args.feature}: {a.name}": a for a in FEATURES[args.feature].allow_setup
            }
        title = f"Differential test, off: {args.baseline} against {tree}"
        title += f", {', '.join(chosen)} off" if chosen else ""
        return sides, allowances, title + (f", {keep} on" if keep else "")
    feature = FEATURES[args.feature]
    allowances = fixes | installed | {f"{args.feature}: {a.name}": a for a in feature.allow_on}
    for key in feature.on:
        allowances[f"{args.feature}: its setting {key}"] = Allow(key, setting=key)
    sides = [Side("off", tree, mine), Side("on", tree, {**mine, **feature.on}, feature.on_action)]
    for side in sides:
        side.setup = feature.setup
    title = f"Differential test, on: {args.feature} off against on, in {tree}"
    return sides, allowances, title + (f", {keep} on" if keep else "")


async def differential(
    args, sides: list[Side], allowances: dict[str, Allow], title: str, port: int
) -> int:
    print(title, flush=True)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(out / "diff", ignore_errors=True)
    started = time.monotonic()
    WORK.mkdir(parents=True, exist_ok=True)
    baseline = sides[0].tree if args.mode == "off" else None
    try:
        if baseline:
            drop_worktree(baseline)
            git("worktree", "add", "--detach", "--quiet", str(baseline), args.baseline)
            log(f"baseline: {args.baseline} checked out in {baseline.relative_to(ROOT)}")
        captures, pages, folders, run = await run_sides(
            sides, out, allowances, port, args.copilot_alone
        )
    finally:
        if baseline:
            drop_worktree(baseline)
    documents = [
        normalized(capture, page, side, run)
        for capture, page, side in zip(captures, pages, sides, strict=True)
    ]
    differences: list[Difference] = []
    compare(documents[0]["data"], documents[1]["data"], "", differences)
    classify(differences)
    differences += await compare_pages(folders, [d["dom"] for d in documents], out)
    allow(differences, allowances)
    hidden = sum((Counter(page["hidden"]) for page in pages), Counter())
    lines, failing = report(differences, allowances, hidden, title)
    shots = len(list(folders[0].glob("*.png")))
    lines.append(
        f"{'FAILED' if failing else 'PASSED'}: {failing} difference{'s' * (failing != 1)} not "
        f"allowed; {len(STEPS) + 1} steps, {shots} screenshots a side, "
        f"{time.monotonic() - started:.0f} s. Report: {out / 'report.txt'}"
    )
    (out / "report.txt").write_text("\n".join(lines) + "\n")
    found = [dataclasses.asdict(d) for d in differences]
    (out / "differences.json").write_text(
        json.dumps(found, indent=1, ensure_ascii=False, default=str)
    )
    print("\n".join(lines[1:]), flush=True)
    return 1 if failing else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--feature", help="a registered feature (off: default all of them)")
    parser.add_argument("--mode", choices=("off", "on"), default="off")
    parser.add_argument("--out", type=Path, default=OUT, help=f"default {OUT.relative_to(ROOT)}")
    parser.add_argument("--baseline", default=BASELINE, help=f"git ref (default {BASELINE})")
    parser.add_argument("--tree", type=Path, default=ROOT, help="the tree to test (default: this)")
    parser.add_argument(
        "--keep-on", help="a feature behind a setting kept on, on this tree's sides (see above)"
    )
    parser.add_argument(
        "--copilot-alone",
        action="store_true",
        help="no other coding agent on the PATH: only COPILOT_ALONE allowed (off mode)",
    )
    parser.add_argument("--serve", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(json.loads(args.serve))
        return
    port, lock = claim_port()
    try:
        sides, allowances, title = plan(args, parser, port)
        failed = asyncio.run(differential(args, sides, allowances, title, port))
    finally:
        lock.close()
        Path(lock.name).unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            WORK.rmdir()
    sys.exit(failed)


if __name__ == "__main__":
    main()
