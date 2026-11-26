"""Other coding agents that can run the owner's workers beside Copilot (Codex, Claude).

Nothing of them is stored, looked for or started until the owner turns one on: their
configuration is one key-value entry (`agents`), not a Settings field, so the settings stay as
they were. An agent's backend module is imported only when it starts.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import re
import shutil
import sys
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
# In the configuration, beside each agent's settings: the agent new workers use, and whether
# the Router chooses one for each new worker (Settings → Agents & models, off unless chosen).
NEW_WORKERS = "new_workers"
ROUTER_CHOOSES = "router_chooses"
# "New workers run on: Last used": the agent the owner last chose for a worker (kv LAST_USED;
# Copilot until they chose one), never a default unless picked.
LAST = "last"
LAST_USED = "agentlast"
# Whether the owner answered an agent's first-run card (kv `agentoffer:<agent>`).
OFFERED = "agentoffer:"
# Every agent Chief of Staff runs, in the one order every list has and in which a session whose
# agent can't run it goes on with another (agents-anywhere.md 2.3): Copilot, Codex, Claude.
AGENTS = (COPILOT, *NAMES)
# Chief of Staff's own sessions, as its messages name each ("The Router runs on Codex now"):
# each runs on any agent on. Their choices are kept in the configuration under ROLE_CHOICES
# ({role: agent}; one not chosen runs on the default agent). Watch for you is the connector
# helper; the helpers are the others (learner, checker, distill, messenger).
ROLES = {
    "router": "The Router",
    "connector": "Watch for you",
    "watchdog": "The Watchdog",
    "slack": "The Slack session",
    "helpers": "Chief of Staff's helpers",
}
ROLE_CHOICES = "roles"
# What a role does while the agent chosen for it can't run it (signed out, not installed, its
# plan's limit): go on with another agent that can and say so (the default, NEXT), or wait for
# the owner ("ask": its messages kept until they sign in or say Use <another> until then).
# {role: "ask"}, none else. ("copilot", what 0.1.57 said for NEXT, is read as NEXT.)
ROLE_UNAVAILABLE = "role_unavailable"
ASK = "ask"
NEXT = "next"
# The model and effort of each kind of work on each agent where Settings does not hold them
# (agents-anywhere.md 2.2): {kind: {agent: {"model": str, "effort": str}}}. Copilot's for the
# workers, the Router, the Slack session and the Watchdog are Settings' role_models and
# role_efforts; another agent's for its workers are its own entry's "model" and "effort".
MODELS = "models"
# The kinds of work Settings → Agents & models lists (Who does the work): new workers, then
# Chief of Staff's own sessions.
WORKERS = "workers"
KINDS = (WORKERS, *ROLES)
# Settings' role for a kind's model on Copilot, where Settings holds it.
SETTINGS_ROLES = {WORKERS: "worker", "router": "router", "slack": "slack", "watchdog": "watchdog"}
# The helper workers of the helpers' kind (store.HELPER_KINDS but the connector).
HELPER_SPECIALS = frozenset({"learner", "checker", "distill", "messenger"})
# Done once: Settings' Default model and each role's model on another agent written where each
# kind runs (migrate).
MIGRATED = "migrated:sessions"


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
    """The agents beside Copilot on now, by name (none: Chief of Staff is as it is without
    them)."""
    return sorted(name for name, entry in configured(store).items() if entry.get("enabled"))


def copilot_on(store: Store) -> bool:
    """Whether Chief of Staff uses Copilot: yes unless the owner turned it off (Settings, its
    row's Use), or a first run found it not ready and another agent was (agents-anywhere.md
    3.2)."""
    entry = stored(store).get(COPILOT)
    return not (isinstance(entry, dict) and entry.get("enabled") is False)


def usable(store: Store) -> list[str]:
    """Every agent on now, Copilot's place first (AGENTS): what a session may run on."""
    on = {*turned_on(store), *([COPILOT] if copilot_on(store) else [])}
    return [name for name in AGENTS if name in on]


def default_agent(store: Store) -> str:
    """The agent what was never put elsewhere runs on: Copilot while it is on, else the first
    agent on (Copilot when none is: nothing can run then)."""
    return next(iter(usable(store)), COPILOT)


def fallbacks(store: Store, wanted: str) -> list[str]:
    """The agents a session whose agent can't run it goes on with, in order (another on)."""
    return [name for name in usable(store) if name != wanted]


def found() -> list[str]:
    """The supported agents whose CLI is on this computer (a look at the PATH: nothing runs);
    none on Windows, where none runs workers yet."""
    if sys.platform == "win32":
        return []
    return [name for name in NAMES if shutil.which(COMMANDS[name])]


def router_chooses(store: Store) -> bool:
    """Whether the Router chooses the agent of each new worker (among those on)."""
    return bool(stored(store).get(ROUTER_CHOOSES)) and len(usable(store)) > 1


def new_workers(store: Store) -> str:
    """The agent new workers use: the one the owner chose while it is on (or Last used, and the
    one they last chose for a worker is on), else the default agent."""
    chosen = new_workers_choice(store)
    if chosen == LAST:
        chosen = store.get(LAST_USED) or default_agent(store)
    return chosen if chosen in usable(store) else default_agent(store)


def new_workers_choice(store: Store) -> str:
    """ "New workers run on" as chosen: an agent on, or Last used (else the default agent)."""
    chosen = stored(store).get(NEW_WORKERS)
    if isinstance(chosen, str) and chosen in (LAST, *usable(store)):
        return chosen
    return default_agent(store)


def used(store: Store, agent: str):
    """The owner chose this agent for a worker (named it, picked it, continued on it): what
    Last used starts new workers on."""
    if agent in AGENTS and store.get(LAST_USED) != agent:
        store.set(LAST_USED, agent)


def roles(store: Store) -> dict[str, str]:
    """The agent the owner chose for each role while it is on, else the default agent."""
    chosen = stored(store).get(ROLE_CHOICES)
    chosen = chosen if isinstance(chosen, dict) else {}
    on, default = usable(store), default_agent(store)
    return {
        role: name if isinstance(name := chosen.get(role), str) and name in on else default
        for role in ROLES
    }


def role_agent(store: Store, role: str) -> str:
    return roles(store).get(role, default_agent(store))


def role_unavailable(store: Store, role: str) -> str:
    """What a role does while its agent can't run it: NEXT (another that can), or ASK."""
    chosen = stored(store).get(ROLE_UNAVAILABLE)
    return ASK if isinstance(chosen, dict) and chosen.get(role) == ASK else NEXT


def kind_of(role: str, special: str = "") -> str:
    """The kind of work a session is (KINDS), by its role and the helper it is."""
    if special == "connector":
        return "connector"
    if special in HELPER_SPECIALS:
        return "helpers"
    return WORKERS if role == "worker" else role


def kind_model(store: Store, kind: str, agent: str, settings: Any = None) -> dict[str, str]:
    """The model and effort Settings chose for a kind of work on an agent ("" each: the
    agent's own default)."""
    if agent == COPILOT and kind in SETTINGS_ROLES:
        settings = settings or store.settings()
        role = SETTINGS_ROLES[kind]
        return {
            "model": settings.role_models.get(role) or "",
            "effort": settings.role_efforts.get(role) or "",
        }
    if kind == WORKERS and agent in NAMES:
        entry = configured(store).get(agent, {})
    else:
        models = stored(store).get(MODELS)
        kinds: dict = models if isinstance(models, dict) else {}
        per_agent = kinds.get(kind)
        per_agent = per_agent if isinstance(per_agent, dict) else {}
        entry = per_agent.get(agent)
        entry = entry if isinstance(entry, dict) else {}
    model, effort = entry.get("model"), entry.get("effort")
    return {
        "model": model if isinstance(model, str) else "",
        "effort": effort if effort in EFFORTS else "",
    }


def work_view(store: Store, settings: Any = None) -> dict[str, Any]:
    """Who does the work, as Settings → Agents & models draws it (the snapshot's `work`): the
    agents on, the default one, where each of Chief of Staff's own sessions was put, what one
    does if its agent can't run it, and each kind's model and effort on each agent on."""
    settings = settings or store.settings()
    on = usable(store)
    return {
        "agents": on,
        "default": default_agent(store),
        "new_workers": new_workers_choice(store),
        "roles": roles(store),
        "unavailable": {role: role_unavailable(store, role) for role in ROLES},
        "models": {
            kind: {agent: kind_model(store, kind, agent, settings) for agent in on}
            for kind in KINDS
        },
    }


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
# Who stopped a worker past its token limit, or whose spend is not known (its task's `stopped:`);
# who paused one as its plan's limit was reached.
SPEND_STOP = "its token limit"
PLAN_STOP = "its plan's limit"


def switched_to_ask_first(name: str) -> str:
    """Why a worker on another agent stopped as Ask first was turned on for it (Settings)."""
    return f"{display(name)} takes up Ask first only in a new process."


# An agent's effort as Settings saves it: one of Copilot's thinking efforts (models.Effort), or
# "" for its model's own default.
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")


def planned(store: Store, change: Any) -> dict[str, Any]:
    """The Agents & models group of a Settings save, checked as the page names its fields: the
    configuration it leaves (each agent's entry, who new workers use, where each of Chief of
    Staff's own sessions runs and with which model, what one does if its agent can't run it)."""
    if not isinstance(change, dict):
        raise ValueError("Agents & models: expected each agent's settings.")
    plan: dict[str, Any] = {**stored(store)}
    for name, entry in change.items():
        if name in {NEW_WORKERS, ROLE_CHOICES, ROLE_UNAVAILABLE, MODELS}:
            continue
        if name == ROUTER_CHOOSES:
            if not isinstance(entry, bool):
                raise ValueError("Let the Router choose: on or off.")
            plan[ROUTER_CHOOSES] = entry
            continue
        if name == COPILOT and isinstance(entry, dict):
            if set(entry) - {"enabled"} or not isinstance(entry.get("enabled", True), bool):
                raise ValueError("Copilot, Use: on or off.")
            if "enabled" in entry:
                plan[COPILOT] = {"enabled": entry["enabled"]}
            continue
        if name not in NAMES or not isinstance(entry, dict):
            raise ValueError(f"Agents & models: Chief of Staff does not run {name!r}.")
        if unknown := set(entry) - {"enabled", "model", "effort", "limit"}:
            raise ValueError(f"Agents & models: unknown setting {min(unknown)!r}.")
        fresh = {**configured(store).get(name, {})}
        if "enabled" in entry:
            if not isinstance(entry["enabled"], bool):
                raise ValueError(f"{display(name)}, Use: on or off.")
            fresh["enabled"] = entry["enabled"]
        if "model" in entry:
            fresh["model"] = checked_model(entry["model"])
        if "effort" in entry:
            # Its workers' (Runtime.agent_options).
            fresh["effort"] = checked_effort(entry["effort"])
        if "limit" in entry:
            limit = 0 if entry["limit"] in (None, "") else entry["limit"]
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError("Limit per worker: a whole number of tokens, or empty for none.")
            if limit > MOST_TOKENS:
                raise ValueError(f"Limit per worker: at most {MOST_TOKENS:,} tokens.")
            fresh["limit"] = limit
        plan[name] = fresh
    copilot = plan.get(COPILOT)
    on = [
        name
        for name in AGENTS
        if (
            not (isinstance(copilot, dict) and copilot.get("enabled") is False)
            if name == COPILOT
            else isinstance(plan.get(name), dict) and plan[name].get("enabled")
        )
    ]
    if not on:
        raise ValueError("Agents & models: Chief of Staff needs one agent on.")
    default = on[0]
    chosen = change.get(NEW_WORKERS, stored(store).get(NEW_WORKERS, default))
    if chosen not in (LAST, *on):
        if NEW_WORKERS in change:
            raise ValueError("New workers run on: Last used, or an agent turned on below.")
        chosen = default  # The agent they used is off now.
    plan[NEW_WORKERS] = chosen
    # Each role on the agent chosen for it: one turned off puts it on the default agent.
    kept = stored(store).get(ROLE_CHOICES)
    kept = {r: agent for r, agent in kept.items() if agent in on} if isinstance(kept, dict) else {}
    if ROLE_CHOICES in change:
        asked = change[ROLE_CHOICES]
        if not isinstance(asked, dict):
            raise ValueError("Agents & models: expected the agent of each role.")
        for role, agent in asked.items():
            if role not in ROLES:
                raise ValueError(f"Agents & models: no role {role!r}.")
            if agent not in on:
                raise ValueError(f"{ROLES[role]} runs on: an agent turned on below.")
            kept[role] = agent
    # Copilot's while it is the default is said by no entry (as 0.1.57 kept it).
    kept = {role: agent for role, agent in kept.items() if agent != COPILOT or default != COPILOT}
    if kept:
        plan[ROLE_CHOICES] = dict(sorted(kept.items()))
    else:
        plan.pop(ROLE_CHOICES, None)
    waits = stored(store).get(ROLE_UNAVAILABLE)
    waits = {r: ASK for r, how in waits.items() if how == ASK} if isinstance(waits, dict) else {}
    if ROLE_UNAVAILABLE in change:
        asked = change[ROLE_UNAVAILABLE]
        if not isinstance(asked, dict):
            raise ValueError(
                "Agents & models: expected what each role does if its agent can't run."
            )
        for role, how in asked.items():
            if role not in ROLES or how not in (NEXT, COPILOT, ASK):
                raise ValueError(
                    "If its agent can't run it: use another that can and tell me, or wait and "
                    "ask me."
                )
            if how == ASK:
                waits[role] = ASK
            else:
                waits.pop(role, None)
    if waits:
        plan[ROLE_UNAVAILABLE] = dict(sorted(waits.items()))
    else:
        plan.pop(ROLE_UNAVAILABLE, None)
    if MODELS in change:
        plan_models(plan, change[MODELS])
    return plan


def checked_model(value: Any) -> str:
    model = value.strip() if isinstance(value, str) else None
    if model is None or len(model) > 200:
        raise ValueError("Model: a model's name, or empty for the agent's default.")
    return model


def checked_effort(value: Any) -> str:
    if value not in ("", *EFFORTS):
        raise ValueError(
            "Effort: none, low, medium, high, xhigh or max, or empty for the model's default."
        )
    return value


def plan_models(plan: dict[str, Any], asked: Any):
    """Each kind of work's model and effort on an agent (MODELS), into `plan`: another agent's
    for its workers in its own entry; Copilot's that Settings holds are refused here (they are
    Settings' role models)."""
    if not isinstance(asked, dict):
        raise ValueError("Agents & models: expected each kind of work's models.")
    found = plan.get(MODELS)
    found = found if isinstance(found, dict) else {}
    models = {kind: dict(entry) for kind, entry in found.items() if isinstance(entry, dict)}
    for kind, per_agent in asked.items():
        if kind not in KINDS or not isinstance(per_agent, dict):
            raise ValueError(f"Agents & models: no kind of work {kind!r}.")
        for agent, entry in per_agent.items():
            if agent not in AGENTS or not isinstance(entry, dict):
                raise ValueError(f"Agents & models: Chief of Staff does not run {agent!r}.")
            if unknown := set(entry) - {"model", "effort"}:
                raise ValueError(f"Agents & models: unknown setting {min(unknown)!r}.")
            if agent == COPILOT and kind in SETTINGS_ROLES:
                raise ValueError(f"Agents & models: {kind} on Copilot is a Settings model.")
            fresh = {key: checked_model(value) if key == "model" else checked_effort(value)
                     for key, value in entry.items()}  # fmt: skip
            if kind == WORKERS:
                plan[agent] = {**(plan.get(agent) or {}), **fresh}
                continue
            merged = {**(models.get(kind, {}).get(agent) or {}), **fresh}
            merged = {key: value for key, value in merged.items() if value}
            if merged:
                models.setdefault(kind, {})[agent] = merged
            else:
                models.get(kind, {}).pop(agent, None)
    models = {kind: entry for kind, entry in sorted(models.items()) if entry}
    if models:
        plan[MODELS] = models
    else:
        plan.pop(MODELS, None)


def save(store: Store, plan: dict[str, Any]) -> bool:
    """A plan (planned) as the configuration, when it changes it: whether it did. Nothing is
    stored until an agent is first turned on, or a session's agent or model is chosen here;
    then an agent's usage gets its own table."""
    current = stored(store)
    on = any(isinstance(plan.get(name), dict) and plan[name].get("enabled") for name in NAMES)
    chosen = any(plan.get(key) for key in (MODELS, ROLE_CHOICES, ROLE_UNAVAILABLE, COPILOT))
    if plan == current | {NEW_WORKERS: current.get(NEW_WORKERS, COPILOT)}:
        return False
    if not (current or on or chosen):
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
    "accessToken": "signed in with an access token",
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
# Who pays for an agent's work, as the @ picker and Settings say it (by how it is signed in:
# VS Code's users took Claude there for their own plan when it was Copilot's).
COPILOT_PAYS = "Uses your Copilot plan"
PAYS = {
    "chatgpt": "Uses your ChatGPT sign-in",
    "accessToken": "Uses your access token",
    "claudePlan": "Uses your Claude plan",
    "amazonBedrock": "Uses your Amazon Bedrock account",
    "gcp": "Uses your Google Cloud account",
    "foundry": "Uses your Foundry account",
}
API_KEY_PAYS = {"codex": "Uses your OpenAI API key", "claude": "Uses your Anthropic API key"}
# What makes an agent ready, as the owner would run it in a terminal (Copy command beside Sign
# in and Install, which run the same for them: agentsetup.py): the command, and what to do after.
INSTALL_FIX = {
    "copilot": {"command": "npm install -g @github/copilot", "then": ""},
    "codex": {"command": "curl -fsSL https://chatgpt.com/codex/install.sh | sh", "then": ""},
    "claude": {"command": "curl -fsSL https://claude.ai/install.sh | bash", "then": ""},
}
SIGN_IN_FIX = {
    "copilot": {"command": "copilot login", "then": ""},
    "codex": {"command": "codex login", "then": ""},
    "claude": {"command": "claude auth login", "then": ""},
}
# An agent as the @ picker and Settings say it: ready, its sign-in needed, not installed.
READY, SIGN_IN, MISSING, PROBLEM, CHECKING = "ready", "signin", "missing", "problem", "checking"
# Why work for an agent is held for it (its card's Sign in, Install), to go on by itself after.
HELD = frozenset({SIGN_IN, MISSING})
# An agent whose plan's limit is used up until a time it said: work for it is held too, and goes
# on by itself once that limit resets (Runtime.limit_reset).
LIMITED = "limited"
# One whose check by Chief of Staff could not run (its machine busy, say): work for it is held
# too, and goes on by itself once that check, tried again by itself, has run (Runtime.check_later).
UNCHECKED = "unchecked"
WAITS = HELD | {LIMITED, UNCHECKED}
# Claude while Chief of Staff installs what its workers need (claude_setup.py), and while that
# is missing with Claude off: it is set up once turned on.
SETTING_UP, NOT_SET_UP = "settingup", "setup"
STATUS_WORDS = {
    READY: "Ready",
    SIGN_IN: "Sign-in needed",
    MISSING: "Not installed",
    CHECKING: "Checking…",
}
NEEDS_SIGN_IN = re.compile(
    r"\b401\b|unauthori[sz]ed|not (?:signed|logged) in|not set up|invalid api key"
    r"|please run /login|oauth token has expired|authentication[ _](?:failed|error)"
    r"|login (?:expired|reached its time limit)|refresh token|(?:sign|log) ?in again",
    re.IGNORECASE,
)
# How limit_line says a limit holds an agent's work (status_of: LIMITED).
LIMIT_SAID = re.compile(r"limit is used up|refusing requests for a rate limit")
# What a check that could not run says (claude_check.check, CodexBackend.check): the reason, and
# what it holds meanwhile after it.
CHECK_UNRUN = re.compile(
    r"(Chief of Staff's check of .+? could not (?:run|finish) \(.*\))(?:[:.]|$)"
)


# A key in the environment Chief of Staff started in, which an agent's workers use over its
# sign-in, its billing then the key's: Claude Code's ANTHROPIC_API_KEY; Codex's access token
# (CODEX_ACCESS_TOKEN: its app-server's, as every Codex command's, over what `codex login` keeps;
# not CODEX_API_KEY, which `codex exec` alone reads, nor OPENAI_API_KEY, no sign-in of Codex's own
# provider; 0.167.2, login/src/auth/manager.rs). Said by name; its value is never kept.
KEY_VARIABLES = {"claude": "ANTHROPIC_API_KEY", "codex": "CODEX_ACCESS_TOKEN"}


def pays(name: str, account: str, key_env: str = "") -> str:
    """Whose plan or key an agent's work uses, from how it is signed in ("" when unknown); a key
    in the environment it runs in (key_env) is said by its name."""
    if name == COPILOT:
        return COPILOT_PAYS
    if key_env:
        said = API_KEY_PAYS.get(name) if account == "apiKey" else PAYS.get(account)
        return f"{said or 'Uses an API key'} ({key_env})"
    if account == "apiKey":
        return API_KEY_PAYS.get(name, "")
    return PAYS.get(account, "")


def status_of(name: str, why: str) -> dict[str, Any]:
    """An agent that cannot run, by why (its glance's or its start's words): its sign-in
    needed (and the command for it), not installed, its check by Chief of Staff not run yet, or
    another problem, said as it is."""
    if CHECK_UNRUN.search(why):
        return {"status": UNCHECKED, "said": why}
    if NEEDS_SIGN_IN.search(why):
        return {"status": SIGN_IN, "said": STATUS_WORDS[SIGN_IN], "fix": SIGN_IN_FIX[name]}
    if re.search(r"not found|not installed", why, re.IGNORECASE):
        return {"status": MISSING, "said": STATUS_WORDS[MISSING], "fix": INSTALL_FIX[name]}
    if LIMIT_SAID.search(why):
        return {"status": LIMITED, "said": why}
    return {"status": PROBLEM, "said": why}


# What an agent's workers do differently while workers are sandboxed (its row says so).
SANDBOX_NOTES = {"claude": "Sandboxed Claude workers don't run hooks."}
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


def reset_words(at: float, now: float | None = None) -> str:
    """When a limit resets, in local time, as the page says it (board.js resetTime): "3:40 PM"
    today, "tomorrow 11:11 AM", "Sat 11:11 AM" within a week, else "Nov 28 11:11 AM"."""
    stamp = time.localtime(at)
    hour = f"{(stamp.tm_hour % 12) or 12}:{stamp.tm_min:02d} {'AM' if stamp.tm_hour < 12 else 'PM'}"
    today = time.localtime(time.time() if now is None else now)
    days = (datetime.date(*stamp[:3]) - datetime.date(*today[:3])).days
    if days <= 0:
        return hour
    if days == 1:
        return f"tomorrow {hour}"
    if days < 7:
        return f"{time.strftime('%a', stamp)} {hour}"
    return f"{time.strftime('%b', stamp)} {stamp.tm_mday} {hour}"


def blocked(limit: Any) -> bool:
    """Whether a plan's limit, as last said, refuses its workers' requests: reached, and no
    extra usage carrying them on."""
    return isinstance(limit, dict) and limit.get("status") == "rejected" and not limit.get("extra")


def limit_line(limit: Any, now: float | None = None, name: str = "claude") -> str:
    """What a plan's limit, as agent `name`'s worker last said it, means now ("" once it reset,
    or while nothing is near). Status "rejected": its plan's limit is used up (the window that
    holds it, and when that resets: "Codex's weekly limit is used up · resets tomorrow 11:11
    AM"; plainly when the agent did not say); "limited": its requests were refused for a rate
    limit (a plan's or an account's) with no plan's limit said."""
    limit = limit if isinstance(limit, dict) else {}
    now = time.time() if now is None else now
    resets = limit.get("resets_at") if isinstance(limit.get("resets_at"), int | float) else 0
    if resets and resets <= now:
        return ""
    window = WINDOWS.get(str(limit.get("window") or "")) or str(limit.get("label") or "usage limit")
    when = f" · resets {reset_words(resets, now)}" if resets else ""
    if limit.get("status") == "rejected" and limit.get("extra"):
        until = f" until it resets at {clock(resets)}" if resets else ""
        return (
            f"Your {display(name)} plan's {window} is reached; workers go on with extra usage"
            f"{until}."
        )
    if limit.get("status") == "rejected":
        return f"{display(name)}'s {window} is used up" + (when or "; it didn't say when it resets")
    if limit.get("status") == "allowed_warning":
        at = f"; it resets at {clock(resets)}" if resets else ""
        return f"Your {display(name)} plan's {window} is close{at}."
    if limit.get("status") == "limited":
        return f"{display(name)} is refusing requests for a rate limit" + (
            when or "; it didn't say for how long"
        )
    return ""


def limit_status(limit: Any, name: str, now: float | None = None) -> dict | None:
    """An agent whose plan's limit is used up until a time it said, as the @ list says it
    (status LIMITED: "Weekly limit used up", and when it resets); None while it can run (no
    limit, extra usage on, its reset passed, or no reset said: then its next request finds
    out)."""
    limit = limit if isinstance(limit, dict) else {}
    resets = limit.get("resets_at") if isinstance(limit.get("resets_at"), int | float) else 0
    if not blocked(limit) or not resets or resets <= (time.time() if now is None else now):
        return None
    window = WINDOWS.get(str(limit.get("window") or "")) or str(limit.get("label") or "usage limit")
    return {
        "status": LIMITED,
        "said": f"{window[:1].upper()}{window[1:]} used up",
        "resets_at": int(resets),
        "line": limit_line(limit, now, name),
    }


def pause_facts(limit: Any, name: str) -> dict:
    """What a worker its agent paused for a limit says of it on its row: the agent, when the
    limit resets if the agent said (0: not said), and whether it is a rate limit rather than
    its plan's."""
    limit = limit if isinstance(limit, dict) else {}
    resets = limit.get("resets_at")
    window = WINDOWS.get(str(limit.get("window") or "")) or str(limit.get("label") or "")
    return {
        "agent": name,
        "resets_at": int(resets) if isinstance(resets, int | float) and resets > 0 else 0,
        "rate": limit.get("status") == "limited",
        **({"window": window} if window and limit.get("status") != "limited" else {}),
    }


def pause_line(facts: dict, now: float | None = None) -> str:
    """A paused task's line, its reset first ("Codex's weekly limit is used up · resets tomorrow
    11:11 AM"), as its row on Home says it (board.js pauseLine)."""
    name = display(str(facts.get("agent") or ""))
    what = (
        f"{name} is refusing requests for a rate limit"
        if facts.get("rate")
        else f"{name}'s {facts.get('window') or 'usage limit'} is used up"
    )
    resets = int(facts.get("resets_at") or 0)
    if not resets:
        return f"{what}; it didn't say {'for how long' if facts.get('rate') else 'when it resets'}"
    now = time.time() if now is None else now
    return f"{what} · {'reset at' if resets <= now else 'resets'} {reset_words(resets, now)}"


def plan_usage(store: Store, name: str, facts: dict) -> list[dict]:
    """How much of each of its plan's limits is used, where the agent says it ({label, used:
    percent, resets_at}): Codex's from its look, Claude's as its workers' streams last said it
    (none before a worker ran, nor with an API key). Never anything about the account."""
    if name == "codex":
        return [
            {"label": str(entry["window"]), "used": int(entry["used"]),
             "resets_at": int(entry.get("resets_at") or 0)}
            for entry in facts.get("limits") or []
            if isinstance(entry, dict) and isinstance(entry.get("used"), int)
        ]  # fmt: skip
    live = facts.get("limit")
    if not live:
        try:
            live = json.loads(store.get(f"agentlimit:{name}") or "{}")
        except ValueError:
            live = {}
    if not isinstance(live, dict) or not isinstance(live.get("used"), int):
        return []
    resets = live.get("resets_at") if isinstance(live.get("resets_at"), int) else 0
    if resets and resets <= time.time():
        return []  # Reset since: not known now.
    label = WINDOWS.get(str(live.get("window") or ""), "usage limit")
    return [{"label": label, "used": live["used"], "resets_at": resets}]


def live_limit(store: Store, name: str, live: Any = None) -> dict:
    """An agent's plan limit: as its running backend has it, else as last kept (kv
    `agentlimit:<agent>`)."""
    if not live:
        try:
            live = json.loads(store.get(f"agentlimit:{name}") or "{}")
        except ValueError:
            live = {}
    return live if isinstance(live, dict) else {}


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


async def glance(name: str, folder: Path | None = None) -> dict[str, Any]:
    """An agent's version and sign-in from its CLI's own short commands, in `folder` (Chief of
    Staff's own for it), for a first-run card at startup: nothing more is run until the owner
    opens Settings (look). Raises with why it cannot run here."""
    if name == "codex":
        from .backend_codex import glance as glance_at_codex

        return await glance_at_codex(command(name), folder)
    if name == "claude":
        from .backend_claude import glance as glance_at_claude

        return await glance_at_claude(command(name), folder)
    raise RuntimeError(f"This Chief of Staff does not run {name}.")


async def opened_elsewhere(name: str) -> dict[str, int]:
    """An agent's conversations open in a terminal now, read while it has no backend running
    (no process is its workers'): id -> pid."""
    if name == "codex":
        from .backend_codex import opened_elsewhere as found
    elif name == "claude":
        from .backend_claude import opened_elsewhere as found
    else:
        return {}
    return await asyncio.to_thread(found)


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
    put_back_on_copilot(store, named)
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


def put_back_on_copilot(store: Store, named: dict[str, str]):
    """Before a downgrade: Chief of Staff's own sessions on another agent (the Router, the
    Watchdog, the Slack session, Watch for you and the helpers), which an earlier version runs
    only on Copilot, are put back on it (`named` loses them): the Router, the Watchdog and the
    Slack session on the Copilot session they had (a fresh one, made there, when they never had
    one), a helper on a fresh run."""
    from uuid import uuid4

    from .roles import ROUTER, SLACK, WATCHDOG
    from .store import HELPER_KINDS

    for key in list(named):
        row = store.find_session(key)
        if not row:
            continue
        helper = any(store.get(f"{kind}:{key}") for kind in HELPER_KINDS)
        if row["role"] not in {ROUTER, WATCHDOG, SLACK} and not helper:
            continue
        del named[key]
        store.delete(f"agent:{key}")
        store.delete(f"rerun:{key}")
        session = "" if helper else store.get(f"rolecopilot:{key}") or ""
        if not session:
            session = str(uuid4())
            store.set(f"fresh:{session}", "1")  # Made there on its first use.
            if helper:
                store.delete(f"created:{key}")
        store.set(f"alias:{session}", key)
        store.set(f"copilot:{key}", session)
        if row["status"] == "working":
            store.update_session(key, status="idle", activity="")
    if (config := stored(store)).get(ROLE_CHOICES):
        config.pop(ROLE_CHOICES)
        store.set(CONFIG, json.dumps(config, sort_keys=True))


# Chief of Staff's own sessions an earlier version kept on Copilot whatever was chosen for them
# (agent-roles.md: Watch for you and the Slack session): such a choice is taken up now.
HELD_BEFORE = ("connector", "slack")


def migrate(store: Store) -> list[tuple[str, str]]:
    """Once, from an earlier version's settings (agents-anywhere.md 7), with nothing changing
    for anyone but what the owner chose: Settings' Default model (Copilot's, for any session
    without one of its own) is written into each kind of work that took it, and cleared; Watch
    for you and the helpers, which took the workers' Copilot model and effort, get them as
    their own; a role on another agent, which took that agent's workers' model and effort, gets
    them as its own there. Returns the roles whose choice an earlier version held back and that
    now run where the owner put them (role, agent), to say so once."""
    if store.get(MIGRATED):
        return []
    settings = store.settings()
    fallback = settings.model
    role_models: dict[str, str] = {**settings.role_models}
    for role in SETTINGS_ROLES.values():
        if fallback and not role_models.get(role):
            role_models[role] = fallback
    asked: dict[str, dict[str, dict[str, str]]] = {}
    workers = {
        "model": role_models.get("worker") or "",
        "effort": settings.role_efforts.get("worker") or "",
    }
    if workers := {key: value for key, value in workers.items() if value}:
        for kind in ("connector", "helpers"):
            asked.setdefault(kind, {})[COPILOT] = dict(workers)
    chosen = stored(store).get(ROLE_CHOICES)
    for role, agent in (chosen if isinstance(chosen, dict) else {}).items():
        entry = configured(store).get(agent, {}) if agent in NAMES else {}
        own = {key: entry[key] for key in ("model", "effort") if isinstance(entry.get(key), str)}
        if role in ROLES and (own := {key: value for key, value in own.items() if value}):
            asked.setdefault(role, {})[agent] = own
    if asked:
        plan = {**stored(store)}
        plan_models(plan, asked)
        store.set(CONFIG, json.dumps(plan, sort_keys=True))
    if fallback or role_models != settings.role_models:
        store.save_settings(settings.model_copy(update={"model": "", "role_models": role_models}))
    store.set(MIGRATED, "1")
    on = usable(store)
    return [
        (role, agent)
        for role, agent in sorted((chosen if isinstance(chosen, dict) else {}).items())
        if role in HELD_BEFORE and agent in on and agent != COPILOT
    ]


# The agents whose own sandbox Chief of Staff can give a worker as its own sandbox, once the
# check of their version shows it holding (Claude's: backend_claude.sandbox_settings).
SANDBOXED = frozenset({"claude"})


def can_sandbox(name: str) -> bool:
    return name in SANDBOXED


def resume_command(name: str, session_id: str, cwd: str) -> str:
    """How the owner continues a worker's conversation in a terminal, by hand: its agent's own
    command (Claude's from the worker's folder, where it keeps its transcript)."""
    import shlex

    if name == "codex":
        return f"codex resume {session_id}"
    if name == "claude":
        from .backend_claude import dashed

        # Claude takes a UUID only (a row's 32 hex digits are refused: "is not a UUID").
        return f"cd {shlex.quote(cwd)} && claude --resume {dashed(session_id)}"
    return f"copilot --resume {session_id}"


def history_reader(name: str) -> Any:
    """How an agent's session's conversation is read with no process of it running (Claude's,
    from its transcript), or None when it needs one (Codex's)."""
    if name == "claude":
        from .backend_claude import read_history

        return read_history
    return None


def clear_private(folder: Path, *, keep_own: bool = False):
    """An agent's private files (Claude's: a worker's MCP configuration and prompt, 0600), and
    its plugins (Settings' skills, linked), left by processes that ended without removing them
    (Chief of Staff was killed); `keep_own`:
    not those of this process's workers (they are removed as each ends)."""
    import shutil

    mine = f"{os.getpid()}-"
    found = [path for part in ("private", "plugins") if (folder / part).is_dir()
             for path in (folder / part).iterdir()]  # fmt: skip
    for path in found:
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
