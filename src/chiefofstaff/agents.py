"""Other coding agents that can run the owner's workers beside Copilot (Codex).

Nothing of them is stored, looked for or started until the owner turns one on: their
configuration is one key-value entry (`agents`), not a Settings field, so the settings stay as
they were. An agent's backend module is imported only when it starts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import AgentBackend
    from .store import Store

CONFIG = "agents"
# The agents this release runs workers on, by the name rows keep (`agent:<row>`).
NAMES = {"codex": "Codex"}
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


# How the group in Settings says an agent is signed in (Codex's account types).
SIGNED_IN = {
    "chatgpt": "signed in with ChatGPT",
    "apiKey": "signed in with an API key",
    "amazonBedrock": "signed in with Amazon Bedrock",
    "gcp": "signed in with Google Cloud",
}
# Each agent's command, and what to install when it is not found.
COMMANDS = {"codex": "codex"}
FOLDERS = {"codex": (".codex", "CODEX_HOME")}
INSTALL = {"codex": "the Codex CLI"}


def command(name: str) -> str:
    return COMMANDS[name]


async def look(name: str) -> dict[str, Any]:
    """An agent's version, sign-in and models, from a process of its own started for this and
    stopped (its module imported now): raises with why it cannot run here."""
    if name == "codex":
        from .backend_codex import look as look_at_codex

        return await look_at_codex(command(name))
    raise RuntimeError(f"This Chief of Staff does not run {name}.")


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


def backend(name: str) -> AgentBackend:
    """An agent's backend, its module imported now."""
    if name == "codex":
        from .backend_codex import codex_backend

        return codex_backend()
    raise RuntimeError(f"This Chief of Staff does not run {name}.")
