"""Other coding agents that can run the owner's workers beside Copilot (Codex, Claude).

Nothing of them is stored, looked for or started until the owner turns one on: their
configuration is one key-value entry (`agents`), not a Settings field, so the settings stay as
they were. An agent's backend module is imported only when it starts.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import AgentBackend
    from .store import Store

CONFIG = "agents"
# The agents this release runs workers on, by the name rows keep (`agent:<row>`).
NAMES = {"codex": "Codex", "claude": "Claude"}
COPILOT = "copilot"
# In the configuration, beside each agent's settings: the agent new workers use.
NEW_WORKERS = "new_workers"


def stored(store: Store) -> dict[str, Any]:
    try:
        value = json.loads(store.get(CONFIG) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def configured(store: Store) -> dict[str, dict[str, Any]]:
    """Each agent the owner has turned on at some point, with its settings ("enabled" while on,
    "model" and "effort" for its new workers, "limit": the tokens each of its workers may spend,
    none when 0 or absent)."""
    return {
        name: entry
        for name, entry in stored(store).items()
        if name in NAMES and isinstance(entry, dict)
    }


def enabled(store: Store, name: str) -> bool:
    return bool(configured(store).get(name, {}).get("enabled"))


def turned_on(store: Store) -> list[str]:
    """The agents on now, by name (none: Chief of Staff is as it is without them)."""
    return sorted(name for name, entry in configured(store).items() if entry.get("enabled"))


def new_workers(store: Store) -> str:
    """The agent new workers use: Copilot, unless the owner chose one that is on."""
    chosen = stored(store).get(NEW_WORKERS)
    return chosen if isinstance(chosen, str) and chosen in turned_on(store) else COPILOT


def limit(store: Store, name: str) -> int:
    """The tokens each of its workers may spend (0: no limit)."""
    value = configured(store).get(name, {}).get("limit")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def configure(store: Store, name: str, **values: Any) -> dict[str, Any]:
    """Change an agent's settings (made on first use); the rest is kept."""
    if name not in NAMES:
        raise ValueError(f"Unknown agent {name!r}.")
    agents = stored(store)
    agents[name] = {**configured(store).get(name, {}), **values}
    if agents[name].get("enabled"):
        store.agent_usage_table()  # Its usage's own table, from its first use.
    store.set(CONFIG, json.dumps(agents, sort_keys=True))
    return agents[name]


# The most a worker's token limit can be (a typo's extra zeros are refused, not kept).
MOST_TOKENS = 10**12
# Who stopped a worker past its token limit, or whose spend is not known (its task's `stopped:`).
SPEND_STOP = "its token limit"


def switched_to_ask_first(name: str) -> str:
    """Why a worker on another agent stopped as Ask first was turned on for it (Settings)."""
    return f"{display(name)} takes up Ask first only in a new process."


def planned(store: Store, change: Any) -> dict[str, Any]:
    """The Coding agents group of a Settings save, checked as the page names its fields: the
    configuration it leaves (each agent's entry, and who new workers use)."""
    if not isinstance(change, dict):
        raise ValueError("Coding agents: expected each agent's settings.")
    plan: dict[str, Any] = {**stored(store)}
    for name, entry in change.items():
        if name == NEW_WORKERS:
            continue
        if name not in NAMES or not isinstance(entry, dict):
            raise ValueError(f"Coding agents: Chief of Staff does not run {name!r}.")
        if unknown := set(entry) - {"enabled", "model", "limit"}:
            raise ValueError(f"Coding agents: unknown setting {min(unknown)!r}.")
        fresh = {**configured(store).get(name, {})}
        if "enabled" in entry:
            if not isinstance(entry["enabled"], bool):
                raise ValueError(f"Use {display(name)} for workers: on or off.")
            fresh["enabled"] = entry["enabled"]
        if "model" in entry:
            model = entry["model"].strip() if isinstance(entry["model"], str) else None
            if model is None or len(model) > 200:
                raise ValueError("Model: a model's name, or empty for the agent's default.")
            fresh["model"] = model
        if "limit" in entry:
            limit = 0 if entry["limit"] in (None, "") else entry["limit"]
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError("Limit per worker: a whole number of tokens, or empty for none.")
            if limit > MOST_TOKENS:
                raise ValueError(f"Limit per worker: at most {MOST_TOKENS:,} tokens.")
            fresh["limit"] = limit
        plan[name] = fresh
    on = sorted(
        name for name in NAMES if isinstance(plan.get(name), dict) and plan[name].get("enabled")
    )
    chosen = change.get(NEW_WORKERS, stored(store).get(NEW_WORKERS, COPILOT))
    if chosen not in (COPILOT, *on):
        if NEW_WORKERS in change:
            raise ValueError("New workers use: Copilot, or an agent turned on below.")
        chosen = COPILOT  # The agent they used is off now.
    plan[NEW_WORKERS] = chosen
    return plan


def save(store: Store, plan: dict[str, Any]) -> bool:
    """A plan (planned) as the configuration, when it changes it: whether it did. Nothing is
    stored until an agent is first turned on; then its usage gets its own table."""
    current = stored(store)
    on = any(isinstance(plan.get(name), dict) and plan[name].get("enabled") for name in NAMES)
    if plan == current | {NEW_WORKERS: current.get(NEW_WORKERS, COPILOT)} or not (current or on):
        return False
    if on:
        store.agent_usage_table()
    store.set(CONFIG, json.dumps(plan, sort_keys=True))
    return True


def folders(name: str, home: Path) -> list[Path]:
    """An agent's own folder (its settings, sign-in and hooks; the one its variable names too):
    kept out of what sandboxed workers may write while it is on, as Copilot's is."""
    folder, variable = FOLDERS[name]
    found = [home / folder]
    if configured := os.environ.get(variable):
        found.append(Path(configured).expanduser().resolve())
    return list(dict.fromkeys(found))


def display(name: str) -> str:
    return "Copilot" if name == COPILOT else NAMES.get(name, name)


# How the group in Settings says an agent is signed in (Codex's account types; Claude's, as
# backend_claude.signed_in reads `claude auth status`).
SIGNED_IN = {
    "chatgpt": "signed in with ChatGPT",
    "apiKey": "signed in with an API key",
    "amazonBedrock": "signed in with Amazon Bedrock",
    "gcp": "signed in with Google Cloud",
    "claudePlan": "signed in with your Claude plan",
    "foundry": "signed in with Foundry",
}
# The one line Settings and the guide add under an agent signed in a way that has terms of its
# own (more-agents.md 5.12.1).
PLAN_NOTES = {
    "claudePlan": "On a Claude plan, Claude workers count against your plan's usage limits, "
    "under Anthropic's terms."
}
# A plan's limits, as Claude's stream names their windows.
WINDOWS = {
    "five_hour": "5-hour limit",
    "seven_day": "weekly limit",
    "seven_day_opus": "weekly Opus limit",
    "seven_day_sonnet": "weekly Sonnet limit",
}


def clock(at: float) -> str:
    """A time of day as Settings says it ("3:40 pm")."""
    stamp = time.localtime(at)
    return f"{(stamp.tm_hour % 12) or 12}:{stamp.tm_min:02d} {'am' if stamp.tm_hour < 12 else 'pm'}"


def limit_line(limit: Any, now: float | None = None) -> str:
    """What a plan's limit, as a worker's stream last said it, means now ("" once it reset, or
    while nothing is near)."""
    limit = limit if isinstance(limit, dict) else {}
    resets = limit.get("resets_at") if isinstance(limit.get("resets_at"), int | float) else 0
    if resets and resets <= (time.time() if now is None else now):
        return ""
    window = WINDOWS.get(str(limit.get("window") or ""), "usage limit")
    when = f"; it resets at {clock(resets)}" if resets else ""
    if limit.get("status") == "rejected":
        return f"Your Claude plan's {window} is reached{when}."
    if limit.get("status") == "allowed_warning":
        return f"Your Claude plan's {window} is close{when}."
    return ""


def plan_limit(store: Store, name: str, live: Any = None) -> str:
    """An agent's plan limit line for Settings: as its running backend has it, else as last
    kept (kv `agentlimit:<agent>`)."""
    if not live:
        try:
            live = json.loads(store.get(f"agentlimit:{name}") or "{}")
        except ValueError:
            live = {}
    return limit_line(live)


# Each agent's command, and what to install when it is not found.
COMMANDS = {"codex": "codex", "claude": "claude"}
FOLDERS = {"codex": (".codex", "CODEX_HOME"), "claude": (".claude", "CLAUDE_CONFIG_DIR")}
INSTALL = {"codex": "the Codex CLI", "claude": "Claude Code"}


def command(name: str) -> str:
    return COMMANDS[name]


async def look(name: str, folder: Path | None = None) -> dict[str, Any]:
    """An agent's version, sign-in and models, from a process of its own started for this and
    stopped (its module imported now), in `folder` (Chief of Staff's own for it): raises with
    why it cannot run here."""
    if name == "codex":
        from .backend_codex import look as look_at_codex

        return await look_at_codex(command(name), folder)
    if name == "claude":
        from .backend_claude import look as look_at_claude

        return await look_at_claude(command(name), folder)
    raise RuntimeError(f"This Chief of Staff does not run {name}.")


def ever_used(store: Store) -> bool:
    """Whether a worker ever ran on another agent than Copilot here, or one's processes did."""
    return any(agent != COPILOT for agent in store.items("agent:").values()) or bool(
        store.items("agentpids:")
    )


def prepare_downgrade(store: Store) -> tuple[list[str], dict[int, str]]:
    """Before an earlier Chief of Staff, which runs every worker on Copilot, opens this data
    directory (more-agents.md 8), with this one not running: each worker on another agent that
    was at work stops, as after any stop (its tasks wait under Needs you; the earlier version
    never starts it on Copilot), and none of those agents' processes may remain (what is left of
    them is ended). Returns the workers stopped, by title, and the processes still running."""
    from .backend import copilot_orphans, copilot_tree, end_processes
    from .tasks import TaskBook

    named = {key: agent for key, agent in store.items("agent:").items() if agent != COPILOT}
    tasks = TaskBook(store)
    stopped = []
    for row in store.open_sessions():
        if row["id"] not in named or row["status"] not in {"working", "queued", "starting"}:
            continue
        store.update_session(row["id"], status="idle", activity="")
        store.add_message(
            row["id"], "notice", "Stopped by you, before a downgrade.", {"stopped": True}
        )
        for task in tasks.for_session(row["id"]):
            if task["status"] in {"queued", "running"}:
                store.set(f"stopped:{task['id']}", "you")
                store.delete(f"goingon:{task['id']}")
                tasks.move(task["id"], "waiting", progress="Stopped by you.")
        stopped.append(row["title"])
    left: dict[int, str] = {}
    for agent, value in store.items("agentpids:").items():
        try:
            records = json.loads(value)
        except ValueError:
            continue
        if found := copilot_orphans(records, store.directory):
            end_processes(copilot_tree(found))
        if found := copilot_orphans(records, store.directory):
            left |= found
        else:
            store.delete(f"agentpids:{agent}")  # None runs: nothing for a later start to end.
    return stopped, left


def clear_private(folder: Path, *, keep_own: bool = False):
    """An agent's private files (Claude's: a worker's MCP configuration and prompt, 0600) left
    by processes that ended without removing them (Chief of Staff was killed); `keep_own`:
    not those of this process's workers (they are removed as each ends)."""
    import shutil

    private = folder / "private"
    mine = f"{os.getpid()}-"
    for path in private.iterdir() if private.is_dir() else []:
        if keep_own and path.name.startswith(mine):
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            continue


def backend(name: str, folder: Path | None = None) -> AgentBackend:
    """An agent's backend, its module imported now; `folder`: Chief of Staff's own folder for
    it (its processes start there; Claude's launcher is there)."""
    if name == "codex":
        from .backend_codex import codex_backend

        return codex_backend(folder=folder)
    if name == "claude":
        from .backend_claude import claude_backend

        return claude_backend(folder=folder)
    raise RuntimeError(f"This Chief of Staff does not run {name}.")
