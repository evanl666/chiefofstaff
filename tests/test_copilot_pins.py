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

from chiefofstaff import runtime as runtime_module
from chiefofstaff import tools as tools_module
from chiefofstaff import watchdog as watchdog_module
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
    for name in (
        "SSH_AUTH_SOCK",
        "GRADLE_USER_HOME",
        "CHIEFOFSTAFF_WORKER_MCP",
        "CHIEFOFSTAFF_WORKER_HOOKS",
        "CHIEFOFSTAFF_ROUTER_CONTEXT",
    ):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(root=tmp_path, home=home, copilot=copilot)


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
        return plain(dataclasses.asdict(value))
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


def neutral(value, machine: SimpleNamespace) -> str:
    """The value's canonical JSON, with this run's folders made the same in every run."""
    text = json.dumps(plain(value), sort_keys=True, ensure_ascii=False, indent=1)
    text = text.replace(json.dumps(MACOS)[1:-1], "")
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
    "checker, created": (622, "5c31abb63632dd2bbcae8b5b2bdd9299bf5ccf0efc9004bc3232c96dd681a35b"),
    "checker, resumed": (586, "8dde15005c87f4d93a807c0a4a86b1181258bd82942c153a37f5587b0fb0b426"),
    "connector, created": (
        3282,
        "9e2f4c90e71a38a2a96047873ec501a2b9418b26c091d233c18952e554ffa1e6",
    ),
    "connector, resumed": (
        3245,
        "895683a402e86d752799850b501e593f175b2104035cb877c7aa0252c60555ca",
    ),
    "distill, created": (18120, "4ed81709c809a9d77ba30c2a1f505e6c4e5560eda77710ee0cc3d84b3f3bdfc5"),
    "distill, resumed": (18083, "ebcb60056d4a8e80594f5ae3fa7bf7fe0d776d1922eb9337287713e387f7023e"),
    "learner, created": (666, "70fb43283ea400be719f5642572058ae80a3e6d4761c6286ebc7ded97c9fd9ae"),
    "learner, resumed": (631, "ff91dca5fd3cd2ae236eda2d8f94db6c26a2769ad56308f78abaf85028df43b9"),
    "messenger, created": (
        3128,
        "056cf576bfe224b497d5e853535c5b67887b7bc8d7c75000985bf3ae051c53ef",
    ),
    "messenger, resumed": (
        3092,
        "4b8aaac81e658de91e19ed960f82af365a098e4de6d80032a92311ee58d93808",
    ),
    "router with Slack, created": (
        26732,
        "30d7902a1a5f17a7ed9fd3562f03d2aafb95b985670d29d3ad6edd5cf835c3fb",
    ),
    "router with Slack, resumed": (
        26696,
        "7da0b9090e90b759f0ee5c924e2a7227dfff7be5f1115c26cb55cc344da2015a",
    ),
    "router, created": (26732, "6b0427d16917051094e2b7781a3ec570183c4334c9493272dcaea13203541960"),
    "router, resumed": (26696, "2460b2599a8320985bcc4b63e221b8ec6fe64cf240800ba4a53b72af4782e360"),
    "slack, created": (4501, "45e96f706f8f884c1ad15e268e2717480a4482bd75697b9db1a83afe2a21275f"),
    "slack, resumed": (4466, "90e994a20c3bcaadb98e628effd8a48499f866cc408d11fc19b63cbcb3fed734"),
    "the Copilot sandbox of a worker asking first": (
        929,
        "5ce9b8b8c0d7f9bc71f1f000de4aef801cac02083ac5d8e2dbba561e376fd05a",
    ),
    "watchdog, created": (3317, "74c2464eda92b08d7c0c2f0be7e9c18d5b80e0b4d7d7db7c5cbf22099bd3f111"),
    "watchdog, resumed": (3279, "3211b0fe48200b7a8b7e24baa9eca2c19608f7f40a7c4282665e9ca51f11ca54"),
    "worker asking first, sandboxed, created": (
        19983,
        "4da99cbf2ff979aaa12e4c111221ed80f0da29424ea4dd269f9c3226b5068513",
    ),
    "worker asking first, sandboxed, resumed": (
        19938,
        "54bdb9f8f7f935229fac1b54b49f4fec954d48e2dfa0bced339abba17b847f9c",
    ),
    "worker in a trusted worktree, created": (
        18688,
        "1a5a196ab5e850d4d0304e7fbb38b906ce11b3a5262e4a0ae3e06790a1087cf4",
    ),
    "worker in a trusted worktree, resumed": (
        18612,
        "975d250bcb72472a04af36a9893065147566a7f2f0c36c4f60f2ce192f3b28d3",
    ),
    "worker that reads Slack, created": (
        20598,
        "3bc206ebdf57843533b538dcd00a045af2b903682cfe73ae6cb2783950506b25",
    ),
    "worker that reads Slack, resumed": (
        20551,
        "6cdcbf14bc8ed52686c9cac2f826f6d23345304ffad62befd0fd438bbca69dde",
    ),
    "worker with its model, created": (
        18222,
        "a199d9438c84948b77f0be530795b2f0fa9c7328e07787c653148727df4ef349",
    ),
    "worker with its model, resumed": (
        18175,
        "d909959d08a91c128babe63ad2ba4d0a1c5a84d823e4f9fc63c0d61cc07a9121",
    ),
    "worker, created": (18172, "e2fec61fd8b9ade4b94ea6a66e236da32ad163dbb96de9bc4636120a152bb164"),
    "worker, resumed": (18124, "20bf19db0ce58ec438a63f1eb78a88f7f6f8dad47c64a568224680993f19136b"),
}


async def test_what_each_copilot_session_is_created_and_resumed_with(machine):
    check(await every_role(machine), SESSION_OPTIONS)


TOOL_SCHEMAS: dict[str, tuple[int, str]] = {
    "ack_decision": (939, "f1e8d23ad674c13de6635630984698c2a58d8f1cb11468a7a0d065be2e2d5b6f"),
    "add_repo": (765, "4bb640479d9246948cc8a06f11a751e97eb3669cbde252473ba406d958c258b0"),
    "add_watch": (1054, "5e9023cdbbec4eb38baf87e6ae69dc9f841b6ccda7f6bc3e9f7249cce22a074c"),
    "answer_session": (586, "821f8a2b99ddc017adb3ac8bfbb727bffbd4ee8454269dd9e45b4f7cb70a800c"),
    "ask_decision": (2207, "83dac4c0d53d5144938535ea9a2099b616e068d7f685e0d6c276a01175c64ec8"),
    "ask_session": (703, "ea1608de8c0199c78e570fc6ab4e0c470d8a14a67f29a1688a6a4e45d5a8471a"),
    "ask_workspace": (1176, "5d00bb6fc095b66a8be34246c09de112066d435e7ea8e40de8e042672c462f2e"),
    "assess_worker": (1283, "2f97c542176a6aadd04812f43e9e54e2a6a0b667b818149693018c844c87be36"),
    "cancel_task": (720, "2011f9c18cbbe0da27a39489620ef13c7381f1acb64cdfe8ca3b86b7b62595ef"),
    "check_back": (1212, "93e0b1687efc478d9624e8f40c0032c0a8e048c190b23c2da2a705fe526d3c80"),
    "check_prs": (767, "3dac38ab006814eb88e1b7324e0f9f36a8dfa761bbae0f5f18e6341702884ed0"),
    "close_worker": (505, "c4103af68548be57f389ba91d1d9c1b3370d3eb1ed9c6d9c0a4b8485e6a789cd"),
    "distill_profile": (544, "2904b62209db0faa88cdb3cca631b120b47223876dee91465c1f8158ad64cafd"),
    "draft_github_review": (
        3744,
        "415f8588a338650cc1ad4791a478dc000aa2813b08093ab28d2286508d3d647e",
    ),
    "draft_reply": (1261, "a8f9c664f39cac09dd951758a9926c4a145467a1f695ade5f398de0b821344a5"),
    "draft_slack_reply": (1637, "ade8d62321edf4ad95986e4c1026036ea648858437c0935a5a2e65c4fc21f659"),
    "find_sessions": (697, "520d139ad33d3a6f20f570cace73910d08065273860a6f1f438cc9e871ede4c4"),
    "follow_thread": (604, "1355a1c9e10baf424ff466bbb8fe25b521ddd8938cf9190327976867cea30fbb"),
    "list_sessions": (384, "7e2274c2dc66bdfc8932cce784f5e2d7d35aa225f65e24bb1fe7fa929cf88ddd"),
    "message_session": (4220, "1a81fa321dda2f176615276812da37c54ca6efd3d1637f8ca908a7143e54d84c"),
    "notify_owner": (929, "48c64d00736798f86196e5b8a29e77df3e3fd5a5a9d9ad65467fdcf94f10598e"),
    "propose_checks": (1630, "5eb89b6cb43d056874a436564b001a80c8fb4ae1f61800670da4ba964445a3d0"),
    "propose_lessons": (1501, "1add6af96ecfa09803b0492fcae1e1e209869ded7c4902e9e2d5d3d3af4860b1"),
    "propose_profile": (504, "cb173d090da7a52cda8b929a441f1a0f740f18d10c97f1a58328587e4b2ffcc3"),
    "read_owner_slack_dm": (
        363,
        "55d6b05b17fb98b3d0f50b9dc2094067bf70f7788125c93fda83c793ac0bca7a",
    ),
    "read_session": (796, "c747394155b271a5174e865b154df3db5ea52acf2f9ab2872de5b33271af217d"),
    "record_decision": (1242, "ccb97e9570775c39b98d2e4b64972a08fd7f363843066d156bc1537e883b5e78"),
    "record_setup": (819, "b94ea5acba4574b2c72523bc7810a8235b259ec73d15118b2c3f448680376848"),
    "rename_task": (674, "46d382544532fc7e2dd9069c69762f4bf4b192b40bced67ec7202f3d9a915717"),
    "report": (5630, "a4d8d01f5db54b9b166243d5eb130b562b80b692931a9377714767a85c51ddd8"),
    "report_found": (4051, "ed8e85f84e45ba39ddc536deb71722b32140c0857b90f4ba445b030394873011"),
    "report_notified": (1256, "61abcc1b21e540a3d5942f042e95722ad9c867827ab989628446c153cce65a19"),
    "report_owner_replies": (
        1841,
        "8472cb5ba21db939a07eed89eba6902747e954a8f348225ee99fecac614df18f",
    ),
    "report_owner_words": (
        1138,
        "e2ed0186c295364aa63697199623976748c1cb6ae6dbace3fd34864173306312",
    ),
    "send_owner_slack_dm": (
        656,
        "d70572f0098ee21c9e306dac71960bdbd91f9f0170b5b4bcfe3d3695b4d0e135",
    ),
    "spawn_worker": (6609, "0998c80184dfcb77562eecc40ba13839992e935db94f70bf73f69542935f7a56"),
    "split_task": (749, "864be536c2e4538c57e97d174049cffa8c70df8c143e613c638726fedded5f17"),
    "stop_following": (1063, "1160b14e799d595492bf7eadfc22c7b831cff88293ecf480071367273d78e116"),
    "stop_worker": (694, "4dfb89b8f99a5b0165892bf5431a13e5914025b0703443b115304a98fd51edbd"),
    "tell_session": (1080, "954826bda43d6780f3d7eee6dd69b414677f19e9c009aed737f47ecc1d6cbc7f"),
    "unfollow_thread": (529, "4d0e0cad07080ede07d35c5290ba93a4df7baf2ebe4e80dd613ea85a4f786433"),
    "update_style": (1020, "9743accf4ae8261e065eaa98e67c21a5907d68b566ccab321dde2684a6b45915"),
    "withdraw_draft": (985, "97b45ac4722e60fbc841d6c71847f37a6ec1b5600a45742ce6a75a20ffe1de59"),
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
