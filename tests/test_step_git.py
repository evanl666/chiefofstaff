"""A step's git, in its sandbox (tests/step_sandbox.py: the step's actual policy, enforced by the
operating system): it commits its fix, and changes nothing of the repository it shares with the
owner and other workers: their branches, tags and objects."""

import asyncio
import os
from pathlib import Path

import pytest
from conftest import settle
from shepherd_world import TARGET, World, run
from step_sandbox import SANDBOX
from step_sandbox import run as sandboxed


@pytest.fixture
def world(harness, tmp_path):
    return World(harness, tmp_path)


async def stepping(world, fresh: bool):
    runtime = await world.start()
    await runtime.shepherd.tick()
    await settle(runtime)
    if fresh:
        # The worker's branch moved on: the step works in a copy of its own at the PR's head.
        tree = runtime.store.session(world.worker)["cwd"]
        run("git", "commit", "-q", "--allow-empty", "-m", "more", cwd=tree)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    step = runtime.shepherd.control.open_step(TARGET)
    assert step["state"] == "running", step
    return runtime, step, world.session().options["sandbox_config"]


def refs(clone) -> str:
    return run("git", "for-each-ref", "--format=%(refname) %(objectname)", cwd=clone)


@pytest.mark.skipif(not SANDBOX, reason="an OS sandbox: macOS's, or bubblewrap where it runs")
@pytest.mark.parametrize("fresh", [False, True])
async def test_a_steps_commands_change_no_branch_or_object_of_the_shared_repository(world, fresh):
    _runtime, step, policy = await stepping(world, fresh)
    clone = world.clone
    common = clone / ".git"
    run("git", "branch", "chief/other-worker", world.h0, cwd=clone)
    run("git", "tag", "v1", world.h0, cwd=clone)
    before = refs(clone)
    packs = sorted(str(p) for p in (common / "objects" / "pack").iterdir())
    tree = step["tree"]
    for line in (
        "git update-ref -d refs/heads/main",
        "git update-ref refs/heads/chief/other-worker HEAD~1",
        "git branch -D chief/other-worker",
        "git tag -d v1",
        f"rm -f {common}/packed-refs {common}/refs/heads/main",
        f"rm -f {common}/objects/pack/*",
        "git gc --prune=now",
    ):
        sandboxed(policy, line, tree)
    assert refs(clone) == before
    assert sorted(str(p) for p in (common / "objects" / "pack").iterdir()) == packs
    run("git", "fsck", "--no-progress", "--connectivity-only", cwd=clone)
    # What a step is for: it commits its fix in its tree.
    done = sandboxed(
        policy, "git -c user.name=t -c user.email=t@t commit -q --allow-empty -m fix && "
        "git rev-parse HEAD", tree,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("fresh", [False, True])
async def test_its_commit_reaches_the_repository_through_chief_of_staff_once_it_ends(world, fresh):
    """The step's commit is in its own git folder while it runs; at its end Chief of Staff takes
    it into the repository, and in its worker's own worktree moves the worker's branch to it
    (as a commit there did before): the worktree's .git is its own again, the folder gone."""
    from test_shepherd import fixed

    from chiefofstaff import stepgit

    runtime = await world.start()
    await runtime.shepherd.tick()
    await settle(runtime)
    worker_tree = runtime.store.session(world.worker)["cwd"]
    if fresh:
        run("git", "commit", "-q", "--allow-empty", "-m", "more", cwd=worker_tree)
    branch_before = run("git", "rev-parse", "chief/fix-login", cwd=world.clone)
    pointer = (Path(worker_tree) / ".git").read_text()
    step, sha = await fixed(world, runtime)
    assert step["state"] == "prepared", step
    record = step["evidence"]["private"]
    assert record["released"]
    assert not await asyncio.to_thread(os.path.lexists, stepgit.folder_of(step["folder"]))
    assert run("git", "cat-file", "-t", sha, cwd=world.clone) == "commit"
    assert (Path(worker_tree) / ".git").read_text() == pointer
    assert (Path(step["tree"]) / ".git").read_text() == record["was"]
    if fresh:
        assert run("git", "rev-parse", "chief/fix-login", cwd=world.clone) == branch_before
    else:
        assert run("git", "rev-parse", "chief/fix-login", cwd=world.clone) == sha
        assert run("git", "rev-parse", "HEAD", cwd=worker_tree) == sha
        assert run("git", "status", "--porcelain", cwd=worker_tree) == ""
    run("git", "fsck", "--no-progress", cwd=world.clone)


async def test_a_step_stopped_by_a_restart_or_a_downgrade_gives_its_worktree_back(world, harness):
    """A stop mid-step (Chief of Staff's process ended, or --prepare-downgrade before an earlier
    version runs) never leaves the worker's worktree pointing at the step's git folder."""
    from chiefofstaff.shepherd import prepare_downgrade

    runtime, step, _ = await stepping(world, fresh=False)
    tree = Path(step["tree"])
    pointer = step["evidence"]["private"]["was"]
    assert (tree / ".git").read_text() != pointer  # At the step's own git folder now.
    prepare_downgrade(runtime.store)
    assert (tree / ".git").read_text() == pointer
    # Pointed again, as a stop mid-step leaves it: put back as Chief of Staff starts.
    (tree / ".git").write_text(f"gitdir: {step['evidence']['private']['git']}\n")
    runtime.shepherd.mark(world.worker, step["id"])
    await runtime.shepherd.start()
    assert (tree / ".git").read_text() == pointer
    assert run("git", "rev-parse", "HEAD", cwd=tree) == world.h0


async def test_in_its_workers_detached_worktree_the_worktrees_head_moves_to_its_commit(world):
    """A worker's worktree at the PR's head without a branch: the step's commit is where its
    HEAD is once the step ends, as a commit there did before."""
    from test_shepherd import fixed

    runtime = await world.start()
    await runtime.shepherd.tick()
    await settle(runtime)
    worker_tree = runtime.store.session(world.worker)["cwd"]
    run("git", "checkout", "-q", "--detach", cwd=worker_tree)
    branch = run("git", "rev-parse", "chief/fix-login", cwd=world.clone)
    step, sha = await fixed(world, runtime)
    assert step["state"] == "prepared" and step["tree"] == worker_tree, step
    assert run("git", "rev-parse", "HEAD", cwd=worker_tree) == sha
    assert run("git", "status", "--porcelain", cwd=worker_tree) == ""
    assert run("git", "rev-parse", "chief/fix-login", cwd=world.clone) == branch


async def test_a_step_folder_put_elsewhere_by_a_link_is_never_used(world):
    """Its git folder's place must be one the step can't write: a link in its place (a worker
    could make one in its own folder) refuses the attempt."""
    runtime = await world.start()
    await runtime.shepherd.tick()
    await settle(runtime)
    workspace = Path(runtime.workspaces.workspace(runtime.store.session(world.worker)))
    elsewhere = Path(runtime.store.session(world.worker)["cwd"]) / "elsewhere"
    elsewhere.mkdir()
    (workspace / ".shepherd").symlink_to(elsewhere)
    await runtime.shepherd.fix_it(TARGET)
    await settle(runtime)
    [step] = runtime.shepherd.control.steps(TARGET)
    assert step["state"] == "failed" and "is a link" in step["why"], step
    assert not list(elsewhere.glob("*.git"))
