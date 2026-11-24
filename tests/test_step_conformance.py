"""The step profile on each agent: a shepherd step's actual policy (Shepherd.step_policy, as its
worker's session gets it) through each backend's translation, and what that enforces. Copilot
holds all of it; Codex has no sandbox, and Claude's can't keep commands from the keychain on
macOS: neither prepares a fix (Shepherd.executor_refusal), and neither ever runs one with less."""

import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from conftest import fake_claude, settle
from shepherd_world import TARGET, World
from test_backend_claude import Turn, options

from chiefofstaff import backend_claude
from chiefofstaff.backend import CopilotBackend, StepSandboxPolicy
from chiefofstaff.backend_claude import ClaudeWorker, claude_backend
from chiefofstaff.backend_codex import CodexBackend
from chiefofstaff.shepherd import BLOCKED_HOSTS

REGISTRY = "registry.acme.dev"


@pytest.fixture
def world(harness, tmp_path):
    return World(harness, tmp_path)


async def step_policy(world, hosts: bool) -> StepSandboxPolicy:
    """The policy of a step in the world's worker: offline (its project needs no registry), or
    with a host the owner listed."""
    runtime = await world.start(step_hosts=[REGISTRY] if hosts else [])
    await runtime.shepherd.tick()
    await settle(runtime)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    assert runtime.shepherd.control.open_step(TARGET)["state"] == "running"
    policy = world.session().options["sandbox_config"]
    assert isinstance(policy, StepSandboxPolicy) and policy.keychain is False
    assert policy.outbound is hosts and policy.allowed_hosts == ((REGISTRY,) if hosts else ())
    return policy


def secrets_denied(denied) -> bool:
    return all(
        any(path.endswith(name) for path in denied)
        for name in (".config/gh", ".ssh", ".git-credentials", "Library/Keychains")
    )


@pytest.mark.parametrize("hosts", [False, True])
async def test_copilot_holds_a_steps_whole_policy(world, hosts):
    policy = await step_policy(world, hosts)
    config = CopilotBackend(None).sandbox(policy)  # type: ignore[arg-type]
    assert (config.auth.gh, config.auth.git) == (False, False)
    network = config.user_policy.network
    assert network.allow_outbound is hosts
    assert network.allowed_hosts == ([REGISTRY] if hosts else None)
    assert network.blocked_hosts == list(BLOCKED_HOSTS)
    assert config.user_policy.seatbelt.keychain_access is False
    assert config.allow_bypass is False and config.allow_dev_tool_access is False
    assert secrets_denied(config.user_policy.filesystem.denied_paths)
    assert config.user_policy.filesystem.readwrite_paths == list(policy.readwrite)


async def test_codex_has_no_sandbox_for_it_so_it_never_prepares(world, monkeypatch):
    policy = await step_policy(world, False)
    codex = CodexBackend("codex")
    assert codex.capabilities.sandbox is False and codex.sandbox(policy) is None
    runtime = world.runtime
    monkeypatch.setattr(runtime, "agent", lambda session_id: "codex")
    row = runtime.store.session(world.worker)
    assert await runtime.shepherd.executor_refusal(row) == "its worker runs on Codex, not Copilot"


@pytest.mark.parametrize("hosts", [False, True])
async def test_claudes_translation_reaches_only_a_steps_hosts_never_githubs(world, hosts):
    """Claude restricts its network only where allowedDomains is set (2.1.322): a step's is
    always set (empty offline), GitHub's hosts denied, nothing asked about. 0.1.56 left an
    offline step's unset (every host reachable) and its blocked hosts out."""
    policy = await step_policy(world, hosts)
    settings = backend_claude.sandbox_settings(policy)
    network = settings["network"]
    assert network["allowedDomains"] == ([REGISTRY] if hosts else [])
    assert network["deniedDomains"] == list(BLOCKED_HOSTS) and network["strictAllowlist"] is True
    assert secrets_denied(settings["filesystem"]["denyRead"])
    worker = SimpleNamespace(options={"sandbox": policy})
    for host in ("github.com", "api.github.com", "objects.githubusercontent.com", "example.com"):
        assert ClaudeWorker.network_refused(worker, host), host  # type: ignore[arg-type]
    assert bool(ClaudeWorker.network_refused(worker, REGISTRY)) is not hosts  # type: ignore[arg-type]
    # A host the owner listed never opens one of GitHub's under it.
    listed = SimpleNamespace(options={"sandbox": replace(policy, allowed_hosts=("github.com",))})
    assert ClaudeWorker.network_refused(listed, "github.com")  # type: ignore[arg-type]


async def test_claude_never_runs_a_policy_that_keeps_commands_from_the_keychain(
    world, tmp_path, monkeypatch
):
    """On macOS, Claude's sandbox lets every command reach the keychain's service (2.1.322):
    a step's policy is refused there, never run without that limit."""
    policy = await step_policy(world, False)
    agent = claude_backend(fake_claude(tmp_path / "fake", monkeypatch), folder=tmp_path / "claude")
    await agent.start()
    try:
        folder = tmp_path / "step"
        folder.mkdir()
        if sys.platform == "darwin":
            with pytest.raises(Exception, match="can't keep commands from your keychain"):
                await agent.create_session(
                    session_id="c3" * 16, name="t", **options(folder, sandbox=policy)
                )
            policy = replace(policy, keychain=None)  # Its network, below, as it would hold it.
        session = await agent.create_session(
            session_id="c4" * 16, name="t", **options(folder, sandbox=policy)
        )
        turn = Turn(session)
        for host in ("github.com", "example.com"):
            await session.send(f"Run the shell command `curl -sS https://{host}/`, then reply ok")
            await turn.ended()
        results = [done.result.content for done in turn.of("tool.execution_complete")]
        assert "github.com blocked" in results[0] and "example.com blocked" in results[1]
    finally:
        await agent.stop()


@pytest.mark.parametrize("agent", ["claude", "codex"])
async def test_a_worker_on_another_agent_never_opens_in_a_steps_place(world, monkeypatch, agent):
    """Marked for a step (refused at admission, so never in use), a worker on another agent is
    not opened at all: never with its own options, which have none of the step's limits."""
    runtime = await world.start()
    monkeypatch.setattr(runtime, "agent", lambda session_id: agent)
    runtime.shepherd.mark(world.worker, "s1")
    name = "Claude" if agent == "claude" else "Codex"
    with pytest.raises(RuntimeError, match=f"A fix is never prepared on {name}"):
        await runtime.open_live(runtime.store.session(world.worker))
