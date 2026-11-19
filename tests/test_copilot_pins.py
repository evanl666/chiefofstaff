"""Pins: what Copilot sessions are given, and how Chief of Staff answers, as it is today.

Workers may come to run on other coding agents beside Copilot. None of that may change what a
Copilot session is created or resumed with (its options, prompt and tools, and each tool's
schema), Copilot's models, refusals and fallbacks, what a worker hears when it tells another
something, how a worker used in a terminal catches up and the refusal while it is open there, or
the store an upgrade opens. Each is pinned here exactly: a large value by the length and SHA-256
of its canonical JSON (with session ids numbered and this run's folders made neutral), a text by
its words.

A failing pin prints the new pins, to paste in once the change is deliberate.
"""

import dataclasses
import hashlib
import itertools
import json
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from types import SimpleNamespace
from uuid import UUID

import pytest
from conftest import Harness, call_tool, settle
from copilot.tools import Tool
from test_runtime import at, logged

from chiefofstaff import caches as caches_module
from chiefofstaff import runtime as runtime_module
from chiefofstaff import tools as tools_module
from chiefofstaff import watchdog as watchdog_module
from chiefofstaff import workspaces as workspaces_module
from chiefofstaff.backend import CopilotBackend
from chiefofstaff.models import Settings
from chiefofstaff.roles import WORKER
from chiefofstaff.runtime import OpenElsewhereError


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A home and a Copilot home of the test's own: what a session is given does not depend on
    the machine running the tests (its package caches, ssh files, trusted folders)."""
    home = tmp_path / "home"
    for cache in (".npm/_cacache", ".yarn/berry/cache", ".gradle/caches", ".ssh"):
        (home / cache).mkdir(parents=True)
    (home / ".ssh" / "known_hosts").write_text("")
    copilot = home / ".copilot"
    copilot.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("COPILOT_HOME", str(copilot))
    # Session ids in the order sessions are made (they name workers' folders).
    made = itertools.count(1)

    def numbered():
        return UUID(f"{next(made):08x}-0000-4000-8000-000000000000")

    for module in (runtime_module, tools_module, watchdog_module):
        monkeypatch.setattr(module, "uuid4", numbered)
    # How the sandbox holds paths that do not exist yet: as on macOS (Linux does not hold them).
    monkeypatch.setattr(workspaces_module, "SANDBOX_PLATFORM", "darwin")
    for name in (
        "SSH_AUTH_SOCK",
        "GRADLE_USER_HOME",
        "CHIEFOFSTAFF_WORKER_MCP",
        "CHIEFOFSTAFF_WORKER_HOOKS",
        "CHIEFOFSTAFF_ROUTER_CONTEXT",
        *workspaces_module.MOVED_CACHES,
        *workspaces_module.MOVED_SECRETS,
        *caches_module.TOOL_HOMES,
        *caches_module.MOVED_CONFIG,
    ):
        monkeypatch.delenv(name, raising=False)
    # Chief of Staff's own code (where this Python runs from) and each system's own cache
    # folders, made the same everywhere.
    chief = tmp_path / "chief"
    chief.mkdir()
    monkeypatch.setattr(runtime_module, "own_code", lambda: [str(chief)])
    monkeypatch.setattr(
        caches_module, "worker_temp", lambda session, tier=None: tmp_path / f"t-{session[:8]}"
    )
    monkeypatch.setattr(caches_module, "path_folders", lambda home, slack=False: [])
    monkeypatch.setattr(caches_module, "global_npmrc", list)
    monkeypatch.setattr(caches_module, "SYSTEM_PREFIXES", ())  # Homebrew's, where it is.
    monkeypatch.setattr(caches_module, "SYSTEM_CONFIGS", ())  # /etc/pip.conf, where it is.
    caches = workspaces_module.PLATFORM_CACHES["darwin"]
    monkeypatch.setattr(workspaces_module, "PLATFORM_CACHES", {"darwin": caches, "linux": caches})
    return SimpleNamespace(root=tmp_path, home=home, copilot=copilot)


def fields_set(value) -> dict:
    """A dataclass's fields, but those left unset (at a default of None or False): a later SDK
    adds such fields (the newest one installs may get is tested too), which change nothing a
    session is given."""
    return {
        field.name: getattr(value, field.name)
        for field in dataclasses.fields(value)
        if not (field.default in (None, False) and getattr(value, field.name) is field.default)
    }


def plain(value):
    """A value as JSON: tools by name (their schemas are pinned on their own), callbacks by
    what they are, dataclasses and SDK objects as their fields."""
    if isinstance(value, Tool):
        return f"<tool {value.name}>"
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return plain(fields_set(value))
    if hasattr(value, "to_dict"):
        return plain(value.to_dict())
    if callable(value):
        return f"<{getattr(value, '__qualname__', type(value).__name__)}>"
    return value


# Said to workers on macOS only: checked on its own, so the pins hold on every system.
MACOS = (
    "\nThis is macOS: BSD sed needs `sed -i ''` (GNU's `sed -i` fails here); your edit tool "
    "avoids the difference."
)


MACOS_SANDBOX = (
    "\nPlaywright's Chromium runs in a single process in this sandbox (it refuses more); "
    "Firefox, WebKit and full Chrome cannot start in it: if a test needs them, say so in your "
    "report, with the command for the owner."
)


def neutral(value, machine: SimpleNamespace) -> str:
    """The value's canonical JSON, with this run's folders made the same in every run."""
    text = json.dumps(plain(value), sort_keys=True, ensure_ascii=False, indent=1)
    text = text.replace(json.dumps(MACOS)[1:-1], "").replace(json.dumps(MACOS_SANDBOX)[1:-1], "")
    return text.replace(str(machine.home), "<home>").replace(str(machine.root), "<tmp>")


def exactly(text: str) -> str:
    return f"^{re.escape(text)}$"


def pin(text: str) -> tuple[int, str]:
    return len(text), hashlib.sha256(text.encode()).hexdigest()


def check(found: dict[str, str], pinned: dict[str, tuple[int, str]]):
    pins = {name: pin(text) for name, text in found.items()}
    changed = sorted(
        name for name in pins.keys() | pinned.keys() if pins.get(name) != pinned.get(name)
    )
    assert not changed, (
        f"Changed: {changed}\n"
        + "".join(f"\n--- {name}\n{found.get(name, '(gone)')}" for name in changed[:3])
        + "\n\nNew pins:\n"
        + "".join(f"    {name!r}: {pins[name]!r},\n" for name in sorted(pins))
    )


def spawned(result) -> str:
    return json.loads(result.text_result_for_llm)["session_id"]


async def spawn(runtime, harness, title, **extra):
    extra.setdefault("why_new", "test")
    return spawned(
        await call_tool(runtime, "spawn_worker", harness.router_id, title=title, instructions=title,
                        **extra)
    )  # fmt: skip


async def helper(runtime, kind: str, title: str) -> str:
    """One of Chief of Staff's own helpers (as its module starts it: a worker with its kind)."""
    row = runtime.store.create_session(
        str(runtime_module.uuid4()), WORKER, title, "idle", cwd=str(runtime.role_dir("scratch"))
    )
    runtime.store.set(f"{kind}:{row['id']}", "1")
    await runtime.open_live(row, create=True)
    return row["id"]


def created(harness: Harness, session_id: str) -> dict:
    """What its Copilot session was created with, and its name."""
    copilot = harness.runtime.copilot_id(session_id)
    assert harness.client.created.count(copilot) == 1
    return {"name": harness.client.names[copilot], **harness.client.sessions[copilot].options}


async def resumed(harness: Harness, session_id: str) -> dict:
    """What its Copilot session is resumed with (after a restart, or to take new settings)."""
    copilot = harness.runtime.copilot_id(session_id)
    await harness.runtime.detach(session_id)
    await harness.runtime.session_for(session_id)
    assert harness.client.resumed[-1] == copilot
    return harness.client.sessions[copilot].options


def repository(path):
    def git(*arguments):
        subprocess.run(["git", *arguments], cwd=path, check=True, capture_output=True)

    path.mkdir(parents=True)
    git("init", "-q", "-b", "main")
    (path / ".mcp.json").write_text('{"mcpServers": {}}')
    git("add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return path


async def sessions_of(harness: Harness, machine, sessions: dict[str, str]) -> dict[str, str]:
    found = {}
    for name, session_id in sessions.items():
        found[f"{name}, created"] = neutral(created(harness, session_id), machine)
    for name, session_id in sessions.items():
        found[f"{name}, resumed"] = neutral(await resumed(harness, session_id), machine)
    return found


async def every_role(machine) -> dict[str, str]:
    """What each kind of session is created and resumed with, under the settings that shape it."""
    # Default settings: the Router, the Watchdog, a worker, one with its own model, helpers.
    harness = Harness(machine.root / "plain")
    runtime = await harness.connect()
    sessions = {
        "router": harness.router_id,
        "worker": await spawn(runtime, harness, "Fix the login test"),
        "worker with its model": await spawn(
            runtime, harness, "Profile the query", model="smart-model", effort="high"
        ),
        "watchdog": await runtime.watchdog.session_id(),
    }
    for kind, title in (
        ("learner", "Learn"),
        ("checker", "Checks"),
        ("distill", "Profile"),
        ("connector", "Connect"),
        ("messenger", "Notify"),
    ):
        sessions[kind] = await helper(runtime, kind, title)
    await settle(runtime)
    found = await sessions_of(harness, machine, sessions)
    worker = harness.client.sessions[sessions["worker"]].options["system_message"]["content"]
    assert (MACOS in worker) == (sys.platform == "darwin")
    await runtime.stop()

    # Slack on: the Slack session, a worker that reads Slack, and the Router with Slack.
    harness = Harness(machine.root / "slack", Settings(slack_enabled=True))
    runtime = await harness.connect()
    sessions = {
        "router with Slack": harness.router_id,
        "slack": harness.store.open_sessions("slack")[0]["id"],
        "worker that reads Slack": await spawn(runtime, harness, "Answer the thread", slack=True),
    }
    await settle(runtime)
    found |= await sessions_of(harness, machine, sessions)
    await runtime.stop()

    # Ask first, sandboxed, with a credit limit, a model for workers, and the owner's MCP servers
    # and skills.
    skills = machine.root / "skills"
    skills.mkdir()
    settings = Settings(
        worker_yolo=False,
        worker_sandbox=True,
        worker_credit_limit=40,
        mcp_servers={"docs": {"type": "http", "url": "https://docs.example.com/mcp"}},
        skill_folders=[str(skills)],
        role_models={WORKER: "fast-model"},
    )
    harness = Harness(machine.root / "careful", settings)
    runtime = await harness.connect()
    careful = await spawn(runtime, harness, "Upgrade the SDK")
    await settle(runtime)
    found |= await sessions_of(harness, machine, {"worker asking first, sandboxed": careful})
    # What Copilot itself is given for that sandbox.
    policy = harness.client.sessions[careful].options["sandbox_config"]
    copilot = CopilotBackend(None).sandbox(policy)  # type: ignore[arg-type]
    found["the Copilot sandbox of a worker asking first"] = neutral(copilot, machine)
    await runtime.stop()

    # In a worktree of a repository the owner trusts in Copilot, which has its own MCP servers.
    project = repository(machine.root / "acme-api")
    (machine.copilot / "config.json").write_text(
        "// managed by Copilot\n" + json.dumps({"trustedFolders": [str(project)]})
    )
    harness = Harness(machine.root / "trusted", Settings(projects=[str(project)]))
    runtime = await harness.connect()
    trusted = await spawn(runtime, harness, "Add a flag", project=str(project), isolate=True)
    await settle(runtime)
    found |= await sessions_of(harness, machine, {"worker in a trusted worktree": trusted})
    await runtime.stop()
    return found


SESSION_OPTIONS: dict[str, tuple[int, str]] = {
    "checker, created": (687, "bd0430074b294726f0ff9a8836a483e17f0a26dd0c9287f4e7728498eadba4f9"),
    "checker, resumed": (651, "7c3c21f2c42c6055ad5e96959ef34900287de88433fa543be99c9e1913e72b50"),
    "connector, created": (
        3301,
        "79be7bb285cfef27852115b450348aec546922e8f5834a2e46970321b3041a7e",
    ),
    "connector, resumed": (
        3264,
        "4df73869b2dae13f0c13102ea0964554acb846342671e08bbb3f9c85ab3ed1a4",
    ),
    "distill, created": (18139, "cbcaa465ed6beb2ba3cb9bdd33f4d97f85440d2e5aa9c127650e574879836892"),
    "distill, resumed": (18102, "dd4a1930ac4a2225d08fcb98d849802846e772a9a1e83850dfd4409196782772"),
    "learner, created": (731, "91982fd00db66c9f3230d5c596dd31a401298730e43d30595f7d819572d6f061"),
    "learner, resumed": (696, "469981aa96e8edf4f853edc4c8599332cc702b9ce2a0f1b2fd2cbfe6caa640c1"),
    "messenger, created": (
        3219,
        "f43af845b8b494cb3d97e3fc7e52c7ab155592123e31fa362cc83c9b21901de3",
    ),
    "messenger, resumed": (
        3183,
        "60428d28aad3cdca34c2a973a8a0127a5179e4491c78c21c199de155615032f3",
    ),
    "router with Slack, created": (
        26900,
        "1264ab30359a96d8085d144f5946fda1801ee53baf1ace1dadda71581e5917c7",
    ),
    "router with Slack, resumed": (
        26864,
        "93c321a3ca2b9ee549d46eef46152e5e6984efb992a398b53e1efca2dc1d6e3a",
    ),
    "router, created": (26900, "3bc9dc305e2a412b43e6b7e5c9ea211c387b2313133ddf31b0e4d8b1d667d6bd"),
    "router, resumed": (26864, "ef8e7eb64c1caadb6d2af847473a69c12ab9a203059bb53a0e19283bec91cada"),
    "slack, created": (4566, "6a2c958151166b3dbf3d0d6789f7e8e62a9bc6a609ef88e4d25f6338ff3c5894"),
    "slack, resumed": (4531, "0201a125d8919e539c5289acf88a3cb2509f79ba4a136793af02ad83a85535a6"),
    "the Copilot sandbox of a worker asking first": (
        946,
        "4e17ae9d8664283969ddf98e0c9637579ce9d568ecee2eb460504e64cab83e41",
    ),
    "watchdog, created": (3382, "e3ae732c64bc3796a98d0ee8015e8485f26b7b4695757da463dfea17e52fd1c5"),
    "watchdog, resumed": (3344, "ba4828117ea24e74adedd35a373d08e7375e550c3f143742141d4f822dcd5e24"),
    "worker asking first, sandboxed, created": (
        21092,
        "23d00567a300e036f1ee942ea37f7caa0b4b7ddf93023e72d2e23195e048f33e",
    ),
    "worker asking first, sandboxed, resumed": (
        21047,
        "c72ccff37cec65e5d1ead09ec2a8c897531e791d228e55adcb7117b2cae3bf8d",
    ),
    "worker in a trusted worktree, created": (
        18707,
        "3b7b8a2c8d6095b234b924e8d2c365361c16cf6e920c0e23e545e44339dd3c68",
    ),
    "worker in a trusted worktree, resumed": (
        18631,
        "6a117eb153f651b1166692beb4aa5d072a2ddd3eb3acf0a0477ef439c7f4e4f7",
    ),
    "worker that reads Slack, created": (
        21331,
        "1d6f8e9d4d33d2ab19a1f247a81f3e44edfe85964ac8505cbd0d4204a8b8b158",
    ),
    "worker that reads Slack, resumed": (
        21284,
        "b5f9e23914a3b9a777d991cb645e46522ffe08bcb895c726ab6f88caadcec68a",
    ),
    "worker with its model, created": (
        18241,
        "281ac490c8fd35dc5db98a59dad534b6bbe7ef7b6ed71277a7b9b7c22dc07f9c",
    ),
    "worker with its model, resumed": (
        18194,
        "d69610aaa37011e557e19b70593c9f55ae4ed98769e32c362b3c98ba8b886d72",
    ),
    "worker, created": (18191, "9d97942a866772a1a2bdd92176f7a054954ebe3b06321950f48ead221befc5e2"),
    "worker, resumed": (18143, "4f004e2e145b40c22530dc0cf46768bed8f2f79dce1fe81a931cda1e529d7815"),
}


async def test_what_each_copilot_session_is_created_and_resumed_with(machine):
    check(await every_role(machine), SESSION_OPTIONS)


TOOL_SCHEMAS: dict[str, tuple[int, str]] = {
    "ack_decision": (942, "74330f335aeec313a1da45ef19008c3bd8a34b73debb159c81c2f786b47ff98f"),
    "add_repo": (768, "c36420ef475289ef84a7c2f17ea128f818ef2d3788c54b2abc9f6180175df812"),
    "add_watch": (1057, "df0be8b658c68d8022836f7ee07746db6a1df51a3e4cbfdb76948edc6a626704"),
    "answer_session": (589, "f941f9f2f7819460b968b9e19a394eea6cbb2552d4355e97aec8f26c329e5fda"),
    "ask_decision": (2210, "6dd625c09a0a9adc7872a7133db62066d7498c04dbbd66ef9838fea5738fcb6c"),
    "ask_session": (706, "9d28cb03a14fa235bbbd43fecd0aed807dd41851213dae53b61a44838639610d"),
    "ask_workspace": (1179, "b671e7e233c3536dd067b5567471879a2c2d4fa17a21a33ae0b65a15967a014c"),
    "assess_worker": (1286, "921119a4af034fe578ef48ebe1b1d226851e3a5627c915bf9629e9ee972c3926"),
    "cancel_task": (723, "17ef9378f26658f13259418d5917fdf63834c069872edc2febc262f6a0bcf39d"),
    "check_back": (1530, "b4c5070acc72c860d4ff8040d51a1a15088ea88f477de6742e7086cd31cab555"),
    "check_prs": (770, "eb8697fa05c61ab54621ccc97b452a6068a385ac774664278d8af6d1ce291b5b"),
    "close_worker": (508, "bfd6c48e5e4dbdf30909e1c5beab959af19901a330ce847471f0b5c302d0832b"),
    "distill_profile": (547, "edfc2f2419eb1bcb294962722dc6aa455728f5b343770aa8e9e0bf0b8e5e039a"),
    "draft_github_review": (
        3747,
        "61e40448d32d5f9f374e82aa886c15e4fce0520f7d50187a1ee79931155f987a",
    ),
    "draft_reply": (1264, "5e09a0260c312cf7d47bd3efa7e465af0b13d315fb5d35896326f21bd6c6ccb9"),
    "draft_slack_reply": (1640, "01a8ed658e37605670a51008134bf6196920b82a37637eed5e5dc7b735f60515"),
    "find_sessions": (700, "7818ee46d8cd971b3b7d05afe1ce7f1c53dd73444c9c421924337ab09406e8f5"),
    "follow_thread": (607, "0c262e480aaa102df23f2251daeaa20d38e13b54790b97e32ab583765222218d"),
    "list_sessions": (387, "1a7cd32acbf4370d535391cf0fcc777f7cb70dc682395dc77ff675e1e994e939"),
    "message_session": (4223, "98175d95ebcf1b0ee490df9291af8e0f52dfab70e45a1b9c1a8e28bfda6a9ce7"),
    "notify_owner": (932, "226014f3937727081727ba9cc2e3f7159b4a863bbef8e7133aabfac71e9b4801"),
    "propose_checks": (1633, "bc0fff0cc551ca97236f4c657864d62089afac9a23654017ed11cb143f2fb153"),
    "propose_lessons": (1504, "620f5327bee6d0b57eb7397df29db8bb62dcab9b8bb6b6632543767f55dd70e9"),
    "propose_profile": (507, "0f4e7a031c4c8fe4fc6a04ce3f4e2a9977afd218ea5e524b9dbdabfbfab3400d"),
    "read_owner_slack_dm": (
        366,
        "3809d6a24ca6a77e56988c1e0e208175f9ba786ecbdfcc407e8d458322dd60af",
    ),
    "read_session": (799, "2ddf8949fb6b53e21662e9b6e6e5ac27f7b43b04e421e48c6d44e74b1ec1bf5e"),
    "record_decision": (1245, "730679b1cd74e5ebc59b7b6c42a1f541a1df7a25cc50a623f9d2f5e64803bdd5"),
    "record_setup": (822, "54b61514390daf8546f36c21eb89328e9db18d4b6bafe9b06f2f82e74fc815c5"),
    "rename_task": (677, "f53f147f83bb8cf3c1f82e207ffe1a90db2ba074f7db62c069caf2e006fc7feb"),
    "report": (5633, "6a646395b1205a17c0e3f7a6497c9fb6b8b232a260b258066a5a36352a13f557"),
    "report_found": (4054, "d7fa352a708e5d3607db879845609ca4126d2808a41eae4d82bb130b64453d43"),
    "report_notified": (1317, "fd87bcf8fd0eab4cbe3061bf0f2d8db958471602bf960f083873e7655d001e85"),
    "report_owner_replies": (
        1844,
        "0c6fee1b07486e1edf587988bd7f21b0fb45fdf6e7706cecec3699fd498238a3",
    ),
    "report_owner_words": (
        1141,
        "ce31475ad16c0d0f3c96a1e097c93c5a8455ec3da99e39e7078b9da20d581a17",
    ),
    "send_owner_slack_dm": (
        659,
        "5d94cdef48c7ace23d40cd0facfeffbdcef3b3c47a28038c44833d4f0493fbf2",
    ),
    "spawn_worker": (6612, "df9a92d37a295940668a9e964f138953631a1571879849d8e41cd3832e3186b2"),
    "split_task": (752, "6501cb51d7f895cb168b16561e04a5a95f226d8eef72f6efe38ab3113a89233d"),
    "stop_following": (1066, "dc8a7dfa8d265d7c10a47e6ccc054bb3a490074a3dcff0e3ce206c39baa51987"),
    "stop_worker": (697, "f0f6bbf65ce2804ce4434cbff704516cc9feb28227982e9289ff3afe66320d9f"),
    "tell_session": (1083, "25c24cd21ce8512273cc9c0f29d3c67e3eb9e158303824d377af74871a05c1af"),
    "unfollow_thread": (532, "5b9ecb42b54c4e00f8db73dbddb5c29d1e0007440f95558144d0362424f17204"),
    "update_style": (1023, "28477d9a51bfb1fc13d99059515af906dc266e47460d2ee0ed7e43e649c9790c"),
    "withdraw_draft": (988, "e2c439ec41c19e31f472061120987b9f841d9326f70e0cd2cbd1529ec6fc8e0c"),
}


async def test_every_tools_schema(machine):
    harness = Harness(machine.root / "data")
    tools = harness.runtime.tools
    found = {
        name: neutral(
            {k: v for k, v in dataclasses.asdict(tool).items() if k != "handler"}, machine
        )
        for name, tool in tools.items()
    }
    assert all(name == tool.name for name, tool in tools.items())
    check(found, TOOL_SCHEMAS)


async def test_copilots_models_refusals_and_fallbacks(harness):
    runtime = await harness.connect()
    assert await runtime.available_models() == [
        {"id": "auto", "name": "Auto", "efforts": [], "default_effort": "", "context": 200000,
         "long_context": 0},
        {"id": "fast-model", "name": "Fast-Model", "efforts": [], "default_effort": "",
         "context": 200000, "long_context": 0},
        {"id": "smart-model", "name": "Smart-Model", "efforts": ["low", "medium", "high"],
         "default_effort": "medium", "context": 200000, "long_context": 1000000},
    ]  # fmt: skip
    with pytest.raises(
        ValueError, match=exactly("Unknown model 'gpt-0'. Available: auto, fast-model, smart-model")
    ):
        await runtime.check_model("gpt-0")
    worker = await spawn(runtime, harness, "Hard bug")
    with pytest.raises(ValueError, match=exactly("That model's thinking efforts: none.")):
        await runtime.set_session_model(worker, "fast-model", effort="high")
    with pytest.raises(ValueError, match=exactly("That model has no long-context option.")):
        await runtime.set_session_model(worker, "fast-model", context="long_context")
    view = await runtime.set_session_model(
        worker, "smart-model", effort="high", context="long_context"
    )
    session = harness.session(worker)
    assert (session.models, session.tunings) == (
        ["smart-model"],
        [{"reasoning_effort": "high", "context_tier": "long_context"}],
    )
    assert (view["model"], view["effort"], view["context"]) == (
        "smart-model",
        "high",
        "long_context",
    )


def notices(harness, session_id) -> list[str]:
    return [
        m["text"] for m in harness.store.messages(session_id) if m["kind"] in {"notice", "error"}
    ]


async def test_each_refusal_of_a_copilot_worker_falls_back_as_it_did(tmp_path):
    settings = Settings(model="smart-model", role_efforts={WORKER: "high"})
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    worker = await spawn(runtime, harness, "Hard bug")
    session = harness.session(worker)
    task = session.sent[-1]
    refused = "Execution failed: 400 The requested model is not supported. (Request ID: x)"

    session.emit("session.error", message=refused)
    await settle(runtime)
    assert (session.models, session.tunings) == (
        ["smart-model"],
        [{"reasoning_effort": "medium", "context_tier": "default"}],
    )
    assert session.sent[-1] == task and harness.store.session(worker)["status"] == "working"
    assert notices(harness, worker)[-1] == (
        "Copilot refused the thinking effort and context size chosen for it (Execution failed: "
        "400 The requested model is not supported. (Request ID: x)). It goes on with the model's "
        "own settings; choose another in Settings or this session's header. Your message is sent "
        "again."
    )
    assert (
        harness.store.get(f"fallback:{worker}"),
        harness.store.get(f"fallbackfor:{worker}"),
    ) == ("tuning", "smart-model")
    assert harness.store.get("refused_models") is None

    session.emit("session.error", message=refused)
    await settle(runtime)
    assert (session.models[-1], session.tunings[-1]) == ("auto", {})
    assert session.sent[-1] == task
    assert notices(harness, worker)[-1] == (
        "Copilot refused the model smart-model (Execution failed: 400 The requested model is not "
        "supported. (Request ID: x)). It goes on with Copilot's default model; choose another in "
        "Settings or this session's header. Your message is sent again."
    )
    assert harness.store.get(f"fallback:{worker}") == "model"
    assert harness.store.get("refused_models") == '["smart-model"]'

    session.emit("session.error", message=refused)
    await settle(runtime)
    assert notices(harness, worker)[-1] == refused
    assert harness.store.session(worker)["status"] == "failed"
    assert runtime.tasks.current(worker)["status"] == "failed"


async def test_an_effort_copilot_kept_from_the_model_before_is_cleared_as_it_was(tmp_path):
    settings = Settings(role_models={WORKER: "fast-model"})
    harness = Harness(tmp_path / "data", settings)
    runtime = await harness.connect()
    worker = await spawn(runtime, harness, "Docs")
    session = harness.session(worker)
    sent = len(session.sent)
    kept = "Execution failed: Reasoning effort 'high' is not supported for model 'fast-model'."
    session.emit("session.error", message=kept)
    await settle(runtime)
    assert (session.models, session.tunings) == (["fast-model"], [{"reasoning_effort": None}])
    assert len(session.sent) == sent + 1 and harness.store.session(worker)["status"] == "working"
    assert not notices(harness, worker)
    session.emit("session.error", message=kept)  # Again within the hour: an error as any other.
    await settle(runtime)
    assert notices(harness, worker) == [kept]
    assert harness.store.session(worker)["status"] == "failed"


async def test_a_model_copilot_does_not_offer_at_a_start_falls_back_as_it_did(tmp_path):
    harness = Harness(tmp_path / "data", Settings(role_models={WORKER: "smart-model"}))
    runtime = await harness.connect()
    create = harness.client.create_session

    async def refuse(**options):
        if options.get("model") == "smart-model":
            raise RuntimeError('Model "smart-model" is not available.')
        return await create(**options)

    harness.client.create_session = refuse
    worker = await spawn(runtime, harness, "Count words")
    await settle(runtime)
    assert notices(harness, worker) == [
        "Copilot does not offer the model smart-model, so this worker uses Copilot's default "
        "model. Choose another in Settings (Models) or in this worker's header."
    ]
    assert "model" not in harness.session(worker).options
    assert harness.store.get("refused_models") == '["smart-model"]'
    assert harness.store.get(f"fallback:{worker}") == "model"
    assert harness.store.session(worker)["status"] == "working"


async def test_an_effort_copilots_default_model_takes_none_of_is_left_out_as_it_was(tmp_path):
    harness = Harness(tmp_path / "data", Settings(role_efforts={WORKER: "high"}))
    runtime = await harness.connect()
    create = harness.client.create_session
    tried = []

    async def auto(**options):
        tried.append("reasoning_effort" in options)
        if "reasoning_effort" in options:
            raise RuntimeError('Reasoning effort is not supported when using the "auto" model.')
        return await create(**options)

    harness.client.create_session = auto
    worker = await spawn(runtime, harness, "Count words")
    await settle(runtime)
    assert tried == [True, False]
    assert notices(harness, worker) == [
        "Copilot's default model takes no thinking effort (Auto picks a model each turn), so this "
        "session uses the model's own. Choose a model in Settings to set one."
    ]
    assert harness.store.get("effort_unsupported_noted") == "1"


async def test_what_telling_another_worker_answers(tmp_path):
    harness = Harness(tmp_path / "data", Settings(slack_enabled=True))
    runtime = await harness.connect()
    teller = await spawn(runtime, harness, "Rename the column")
    busy = await spawn(runtime, harness, "Write the migration", why_new="other")
    told = await call_tool(
        runtime, "tell_session", teller, session_id=busy, text="It is user_id now."
    )
    assert told.text_result_for_llm == "Told “Write the migration”."
    assert harness.session(busy).modes[-1] == "immediate", "steered into its turn"
    done = await spawn(runtime, harness, "Docs", why_new="other")
    await call_tool(runtime, "report", done, status="done", summary="Docs written.")
    told = await call_tool(runtime, "tell_session", teller, session_id=done, text="Mention it.")
    assert told.text_result_for_llm == (
        "“Docs” has finished its task; it reads this with its next message. If its finished work "
        "must change, report it to the Router (status question)."
    )
    reader = await spawn(runtime, harness, "Reply", why_new="other", slack=True)
    assert harness.store.session(reader)["slack"]
    for target, text, said in (
        ("nobody", "x", "No such worker; use find_sessions."),
        (teller, "x", "That is you."),
        (harness.router_id, "x", "tell_session is for other workers; report to the Router, "
                                 "which tells the owner."),
        (reader, "x", "That worker's Slack access differs from yours; report to the Router."),
        (busy, "  ", "The message is empty."),
    ):  # fmt: skip
        refused = await call_tool(runtime, "tell_session", teller, session_id=target, text=text)
        assert refused.text_result_for_llm == f"Error: {said}", refused.text_result_for_llm
    for count in range(11):
        await call_tool(runtime, "tell_session", teller, session_id=busy, text=f"Note {count}")
    refused = await call_tool(runtime, "tell_session", teller, session_id=busy, text="One more")
    assert refused.text_result_for_llm == (
        "Error: You told that worker a lot this hour: report to the Router what needs "
        "coordinating, and it decides."
    )
    await runtime.workers.close(done, by="you")
    refused = await call_tool(runtime, "tell_session", teller, session_id=done, text="x")
    assert refused.text_result_for_llm == (
        "Error: That worker is closed. If its finished work must change, report it to the Router "
        "(status question)."
    )


async def test_a_worker_used_in_a_terminal_catches_up_and_is_refused_while_open_there(harness):
    runtime = await harness.connect()
    worker = await spawn(runtime, harness, "Docs")
    await settle(runtime)
    await call_tool(runtime, "report", worker, status="done", summary="Docs written.")
    await runtime.detach(worker)
    envelope = '<message from="router" type="task">\nDocs\n</message>'
    harness.client.logs[worker] = [
        {**logged("user.message", content=envelope), "timestamp": at(-60)},
        {**logged("user.message", content="Add a FAQ"), "timestamp": at(5)},
        {**logged("tool.execution_start", toolName="bash", arguments={"command": "make faq"}),
         "timestamp": at(6)},
        {**logged("assistant.message", content="Added the FAQ."), "timestamp": at(7)},
        {**logged("session.usage_record", usage={"model": "fast-model", "inputTokens": 900,
                                                 "outputTokens": 40,
                                                 "copilotUsage": {"totalNanoAiu": 2_000_000_000}}),
         "timestamp": at(8)},
    ]  # fmt: skip
    before = len(harness.store.messages(worker))
    await runtime.owner_send(worker, "Thanks, ship it")
    shown = [
        (m["kind"], m["text"], {k: v for k, v in m["meta"].items() if k != "prompt"})
        for m in harness.store.messages(worker)[before:]
    ]
    assert shown == [
        ("notice", "Meanwhile, in a terminal", {"terminal": True}),
        ("owner", "Add a FAQ", {"terminal": True}),
        ("tool", "bash make faq", {"terminal": True}),
        ("assistant", "Added the FAQ.", {"terminal": True}),
        ("owner", "Thanks, ship it", shown[-1][2]),
    ]
    used = harness.store.all("SELECT model, credits, input_tokens, output_tokens FROM usage "
                             "WHERE session_id=?", (worker,))  # fmt: skip
    assert [tuple(row.values()) for row in used] == [("fast-model", 2.0, 900, 40)]
    assert harness.store.get(f"usage_log:{worker}") == "5"

    await runtime.detach(worker)
    harness.client.in_use = {worker: 4242}
    with pytest.raises(OpenElsewhereError) as refused:
        await runtime.session_for(worker)
    assert str(refused.value) == (
        "“Docs” is open in another Copilot window (process 4242). Exit it there (/exit), then try "
        "again."
    )
    assert refused.value.pid == 4242
    assert harness.store.get(f"refusedopen:{worker}")


STORE_SCHEMA = (9771, "83310c3579e35b6f8fcaf2c46fe227727bcfa80071039eaf1ba78fd054a993d5")


def stored(path) -> dict[str, list]:
    """Every row of every table of a store's database, read straight from the file."""
    with closing(sqlite3.connect(path)) as db:
        tables = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return {
            table: sorted(map(repr, db.execute(f'SELECT * FROM "{table}"').fetchall()))  # noqa: S608
            for table in tables
        }


async def test_a_store_from_before_opens_and_reads_back_unchanged(machine):
    """A store with work in it, as this release leaves it: its schema, and what an upgrade that
    opens it reads, unchanged; a start adds no kind of key it did not have."""
    harness = Harness(machine.root / "data")
    runtime = await harness.connect()
    worker = await spawn(runtime, harness, "Fix the login test")
    session = harness.session(worker)
    session.emit("user.message", content=session.sent[0])
    session.emit("assistant.turn_start")
    session.emit("tool.execution_start", tool_call_id="c1", tool_name="bash",
                 arguments={"command": "pytest -q"})  # fmt: skip
    session.emit("tool.execution_complete", tool_call_id="c1", success=True,
                 result=SimpleNamespace(content="3 passed in 0.1s"))  # fmt: skip
    await call_tool(runtime, "report", worker, status="done", summary="Fixed.")
    session.emit("session.idle")
    await settle(runtime)
    await runtime.stop()
    database = harness.store.directory / "chiefofstaff.db"
    schema = harness.store.all(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    )
    assert pin(neutral(schema, machine)) == STORE_SCHEMA, pin(neutral(schema, machine))
    before = stored(database)
    kinds = {key.split(":", 1)[0] for key in harness.store.keys("")}

    again = Harness(machine.root / "data", client=harness.client)  # Copilot keeps its sessions.
    assert stored(database) == before, "opened, nothing in it changed"
    views = {
        row["id"]: again.runtime.view(row) for row in again.store.all("SELECT * FROM sessions")
    }
    assert views == {
        row["id"]: runtime.view(row) for row in harness.store.all("SELECT * FROM sessions")
    }
    runtime = await again.connect()
    await runtime.stop()
    assert {key.split(":", 1)[0] for key in again.store.keys("")} == kinds
    assert {table: len(rows) for table, rows in stored(database).items()}.keys() == before.keys()


async def test_a_helper_run_again_reopens_by_its_own_first_conversation(harness):
    """Chief of Staff's helpers run each time on a fresh Copilot conversation (`copilot:<id>`,
    Tools.rerun_special). Reopened, one is looked up by its own first conversation, as it was:
    whether Copilot kept a rerun's or not."""
    runtime = await harness.connect()
    await runtime.toolset.start_special("learner", "Learn", "Learn from the first week.")
    helper = runtime.toolset.last_special("learner")["id"]
    await runtime.workers.close(helper)
    await runtime.toolset.start_special("learner", "Learn", "Learn from the second week.")
    rerun = runtime.copilot_id(helper)
    assert rerun != helper and rerun in harness.client.sessions
    await runtime.workers.close(helper)

    kept = harness.client.sessions.pop(rerun)  # Copilot did not keep the rerun's.
    await runtime.workers.reopen(helper)
    assert harness.store.session(helper)["closed_at"] is None
    await runtime.workers.close(helper)

    harness.client.sessions[rerun] = kept
    del harness.client.sessions[helper]  # Nor its first one, this time.
    with pytest.raises(ValueError, match=exactly(
        "Copilot no longer has that conversation; start a new one instead."
    )):  # fmt: skip
        await runtime.workers.reopen(helper)
    assert harness.store.session(helper)["closed_at"]


async def test_a_settings_save_leaves_copilot_workers_at_work_as_it_did(harness):
    """Turning the worker sandbox on or off while Copilot workers work stops none of them: each
    picks up its new options once its turn ends, and what it is sent goes in as before."""
    runtime = await harness.connect()
    result = await call_tool(
        runtime, "spawn_worker", harness.router_id, title="Lint", instructions="Lint", why_new="t"
    )
    worker = json.loads(result.text_result_for_llm)["session_id"]
    await settle(runtime)
    assert harness.store.session(worker)["status"] == "working"
    session = harness.client.sessions[worker]
    for sandbox in (True, False):
        old = harness.store.settings()
        new = old.model_copy(update={"worker_sandbox": sandbox})
        harness.store.save_settings(new)
        await runtime.settings_changed(old, new)
        await settle(runtime)
        assert harness.store.session(worker)["status"] == "working"
        assert runtime.states[worker].stale and not session.aborted
        assert not [text for kind, text in harness.kinds(worker) if text.startswith("Stopped")]
    await runtime.owner_send(worker, "Also the docs")
    assert session.modes[-1] == "immediate" and "Also the docs" in session.sent[-1]


async def test_a_session_made_while_copilot_reconnects_is_let_go_as_it_was(harness):
    runtime = await harness.connect()
    create = harness.client.create_session

    async def reconnecting(**options):
        session = await create(**options)
        runtime.connections += 1  # Copilot reconnected meanwhile.
        return session

    harness.client.create_session = reconnecting
    harness.store.create_session("w1", WORKER, "W", "starting", cwd=str(harness.store.directory))
    with pytest.raises(RuntimeError, match=exactly("Copilot reconnected meanwhile; try again.")):
        await runtime.open_live(harness.store.session("w1"), create=True)
    assert harness.client.sessions["w1"].disconnected
