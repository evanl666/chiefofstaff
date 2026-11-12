"""Other coding agents that can run the owner's workers beside Copilot (Codex).

Nothing of them is stored, looked for or started until the owner turns one on: their
configuration is one key-value entry (`agents`), not a Settings field, so the settings stay as
they were. An agent's backend module is imported only when it starts.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .backend import AgentBackend
    from .store import Store

CONFIG = "agents"
# The agents this release runs workers on, by the name rows keep (`agent:<row>`).
NAMES = {"codex": "Codex"}


def configured(store: Store) -> dict[str, dict[str, Any]]:
    """Each agent the owner has turned on at some point, with its settings ("enabled" while on,
    "model" and "effort" for its new workers, "limit": the tokens each of its workers may spend,
    none when 0 or absent)."""
    try:
        value = json.loads(store.get(CONFIG) or "{}")
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        name: entry for name, entry in value.items() if name in NAMES and isinstance(entry, dict)
    }


def enabled(store: Store, name: str) -> bool:
    return bool(configured(store).get(name, {}).get("enabled"))


def limit(store: Store, name: str) -> int:
    """The tokens each of its workers may spend (0: no limit)."""
    value = configured(store).get(name, {}).get("limit")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def configure(store: Store, name: str, **values: Any) -> dict[str, Any]:
    """Change an agent's settings (made on first use); the rest is kept."""
    if name not in NAMES:
        raise ValueError(f"Unknown agent {name!r}.")
    agents = configured(store)
    agents[name] = {**agents.get(name, {}), **values}
    if agents[name].get("enabled"):
        store.agent_usage_table()  # Its usage's own table, from its first use.
    store.set(CONFIG, json.dumps(agents, sort_keys=True))
    return agents[name]


def display(name: str) -> str:
    return NAMES.get(name, name)


def backend(name: str) -> AgentBackend:
    """An agent's backend, its module imported now."""
    if name == "codex":
        from .backend_codex import codex_backend

        return codex_backend()
    raise RuntimeError(f"This Chief of Staff does not run {name}.")
