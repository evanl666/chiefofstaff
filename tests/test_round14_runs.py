"""QA round 14, on what a task delivered (E71, E73, E75, E77): a documents site's page and a run
are listed wherever they are served (a port of their own, a private address), only pages of
this computer, or a dev server's said to be serving, are not; a CI build is a run; a run a later
report names again is listed again; and a run's time is since it stood so."""

import json

from conftest import call_tool
from test_round10_lifecycle import spawn, view
from test_round12_checkbacks import idle, turn
from test_round12_runs import FLYTE, NIGHTLY, ago, delivered, said_at

from chiefofstaff import task_actions
from chiefofstaff.digest import needs
from chiefofstaff.task_actions import RUN_UNKNOWN_HOURS


def hosts_here(tmp_path, monkeypatch):
    """This computer's hosts file, as the test has it: a name of this computer, one of its
    network."""
    hosts = tmp_path / "hosts"
    hosts.write_text(
        "127.0.0.1 localhost devbox.acme.test\n10.1.2.3 wiki.corp builds.corp  # its network\n"
    )
    monkeypatch.setattr(task_actions, "HOSTS_FILE", (0.0, 0.0, {}))
    real = task_actions.Path
    monkeypatch.setattr(
        task_actions, "Path", lambda *parts: hosts if parts == ("/etc/hosts",) else real(*parts)
    )


def titles(runtime, task_id: str) -> list[str]:
    return sorted(item["title"] for item in view(runtime, task_id)["deliverables"])


async def declared(harness, runtime, *items: dict) -> dict:
    """A task its worker declared these in (at most 12 a report), reported done, accepted."""
    worker, task = await spawn(runtime, harness.router_id, "Ship the internal pages")
    for start in range(0, len(items), 12):
        said = await call_tool(runtime, "report", worker, status="progress", summary="Made.",
                               artifacts=list(items[start : start + 12]))  # fmt: skip
        assert said.result_type == "success", said.text_result_for_llm
    await call_tool(runtime, "report", worker, status="done", summary="Shipped.")
    runtime.task_actions.accept(task["id"])
    return task


async def test_documents_and_runs_are_listed_wherever_they_are_served(
    harness, tmp_path, monkeypatch
):
    """E71: a wiki page published on :8090, a DAG run on a private address (tracked, and
    under Needs you a day later), were never on Artifacts."""
    hosts_here(tmp_path, monkeypatch)
    runtime = await harness.connect()
    listed = [
        {"url": "http://wiki.corp:8090/display/ENG/Rollout", "name": "Rollout",
         "status": "published"},
        {"url": "http://10.1.2.9:8080/dags/nightly/grid?dag_run_id=m1", "name": "Nightly DAG",
         "status": "running"},
        {"url": "https://preview.local/plan", "name": "Plan", "status": "in review"},
        {"url": "http://192.168.1.20/report", "name": "Report", "status": "done"},
        {"url": "http://localhost:8080/dags/local/grid", "name": "Local DAG", "status": "running"},
        {"url": "https://docs.google.com/document/d/1AbCdEfGh/edit", "name": "Spec",
         "status": "running notes"},
    ]  # fmt: skip
    left_off = [
        {"url": "http://localhost:3000/app", "name": "Dev app", "status": "done"},
        {"url": "http://127.0.0.1/page", "name": "Loopback", "status": "published"},
        {"url": "https://app.localhost/x", "name": "Localhost name", "status": "published"},
        {"url": "https://devbox.acme.test/page", "name": "Hosts loopback", "status": "published"},
        {"url": "http://wiki.corp:3000/preview", "name": "Dev server", "status": "running"},
        {"url": "https://preview.local/app", "name": "Local server", "status": "serving"},
    ]
    task = await declared(harness, runtime, *listed, *left_off)
    assert titles(runtime, task["id"]) == sorted(item["name"] for item in listed)
    # Its own links, all of them.
    assert len(view(runtime, task["id"])["artifacts"]) == len(listed) + len(left_off)


async def test_a_dev_servers_page_on_every_address_is_left_off(harness):
    """Review of E71: a page Python's http.server serves (it says 0.0.0.0) was listed as a doc
    again, as one on [::] was (0.1.45 left them off)."""
    runtime = await harness.connect()
    pages = [
        {"url": "http://0.0.0.0:8000/report.html", "name": "Report", "status": "ready"},
        {"url": "http://[::]:8000/report2.html", "name": "Report 2", "status": "built"},
        {"url": "http://0:8000/report3.html", "name": "Report 3", "status": "ready"},
    ]
    task = await declared(harness, runtime, *pages)
    assert titles(runtime, task["id"]) == []
    assert len(view(runtime, task["id"])["artifacts"]) == len(pages)


async def test_a_dev_servers_route_on_this_computer_is_no_run(harness):
    """Review of E71: a local dev server's `/pipelines/new` "running" was taken for a run,
    followed, and a day later under Needs you as "status unknown"; on this computer only what
    says which runner it is makes a run."""
    runtime = await harness.connect()
    routes = [
        {"url": "http://127.0.0.1:5173/pipelines/new", "name": "New pipeline page",
         "status": "running"},
        {"url": "http://localhost:3000/runs/12", "name": "Runs page", "status": "running"},
        {"url": "http://localhost:3000/workflows/abc", "name": "Flows page", "status": "running"},
        {"url": "http://localhost:3000/builds/12", "name": "Builds page", "status": "running"},
        {"url": "http://localhost:3000/job/report/12", "name": "Job page", "status": "running"},
    ]  # fmt: skip
    runs = [
        {"url": "http://localhost:8080/dags/local/grid", "name": "Local DAG", "status": "running"},
        {"url": "http://localhost:8080/jenkins/job/deploy/12/", "name": "Local Jenkins",
         "status": "running"},
        {"url": "http://127.0.0.1:8929/acme/app/-/pipelines/9", "name": "Local GitLab",
         "status": "running"},
    ]  # fmt: skip
    for item in routes:
        assert task_actions.deliverable_kind(item["url"], declared=True)[0] != "flow", item
    task = await declared(harness, runtime, *routes, *runs)
    for item in routes + runs:
        said_at(harness, task["id"], item["url"], at=ago(hours=RUN_UNKNOWN_HOURS + 2))
    shown = view(runtime, task["id"])
    assert {item["url"] for item in shown["monitoring"]} == {item["url"] for item in runs}
    assert titles(runtime, task["id"]) == sorted(item["name"] for item in runs)
    # The same route elsewhere is a run, as before.
    assert task_actions.deliverable_kind("https://ci.acme.dev/pipelines/new", declared=True)[0] == (
        "flow"
    )


async def test_a_run_that_can_need_the_owner_is_on_artifacts(harness, tmp_path, monkeypatch):
    """E71's invariant: what can bring a task under Needs you (a run of unknown status) is on
    Artifacts too, wherever it runs."""
    hosts_here(tmp_path, monkeypatch)
    runtime = await harness.connect()
    runs = [
        {"url": "http://10.1.2.9:8080/dags/nightly/grid", "name": "Private", "status": "running"},
        {"url": "http://builds.corp:8443/job/deploy/42/", "name": "Jenkins", "status": "running"},
        {"url": "http://localhost:8080/dags/local/grid", "name": "Local", "status": "running"},
        {"url": "https://flyte.example.local/console/projects/a/domains/b/executions/c1",
         "name": "Flyte", "status": "running"},
    ]  # fmt: skip
    task = await declared(harness, runtime, *runs)
    for item in runs:
        said_at(harness, task["id"], item["url"], at=ago(hours=RUN_UNKNOWN_HOURS + 2))
    shown = view(runtime, task["id"])
    unknown = {item["url"] for item in shown["monitoring"] if item.get("unknown")}
    assert unknown == {item["url"] for item in runs}
    assert needs(runtime).markers[task["id"]] == "runs-unknown"
    listed = {item["url"] for item in shown["deliverables"]}
    assert unknown <= listed, unknown - listed


async def test_a_ci_build_said_to_run_is_a_run(harness):
    """E73: a Jenkins build "running" was neither on Artifacts nor followed: not taken for a
    run, and left off as a server's page."""
    runtime = await harness.connect()
    builds = {
        "https://ci.acme.dev/job/deploy/123/": "Jenkins · deploy #123",
        "https://ci.acme.dev/job/team/job/release/45/console": "Jenkins · release #45",
        "https://buildkite.com/acme/app/builds/678": "buildkite · build 678",
        "https://gitlab.acme.dev/acme/app/-/pipelines/901": "GitLab · app pipeline 901",
        "https://gitlab.acme.dev/acme/app/-/jobs/902": "GitLab · app job 902",
        "https://app.circleci.com/pipelines/github/acme/app/12/workflows/ab12": "app · ab12",
        "https://dev.azure.com/acme/app/_build/results?buildId=555": "Azure DevOps · build 555",
    }
    assert {url: task_actions.deliverable_kind(url, declared=True) for url in builds} == {
        url: ("flow", called) for url, called in builds.items()
    }
    runs = [{"url": url, "name": f"Build {n}", "status": "running"} for n, url in enumerate(builds)]
    task = await declared(harness, runtime, *runs)
    shown = view(runtime, task["id"])
    assert {item["url"]: item["kind"] for item in shown["deliverables"]} == dict.fromkeys(
        builds, "flow"
    )
    assert {item["url"] for item in shown["monitoring"]} == set(builds)
    # Only when its worker says it made it: a build its report mentions is a PR's CI.
    assert task_actions.deliverable_kind("https://ci.acme.dev/job/deploy/123/") == ("", "")


async def test_a_run_a_later_report_names_is_listed_again(harness):
    """E75: "its later report no longer lists it" stayed after a later report named the run."""
    runtime = await harness.connect()
    worker, task, _told = await delivered(harness, runtime, NIGHTLY)
    session = harness.session(worker)
    await turn(runtime, session, "Is it finished?")
    await call_tool(runtime, "report", worker, status="done", task_id=task["id"],
                    summary="Closed: nothing more to do.")  # fmt: skip
    await idle(runtime, session, "Closed.")
    said_at(harness, task["id"], FLYTE, at=ago(hours=RUN_UNKNOWN_HOURS + 2))
    [run] = view(runtime, task["id"])["monitoring"]
    assert run["unlisted"]
    await turn(runtime, session, "And the export?")
    await call_tool(runtime, "report", worker, status="progress", task_id=task["id"],
                    summary="The nightly export is still going, two shards left.")  # fmt: skip
    [kept] = json.loads(harness.store.get(f"artifacts:{task['id']}"))
    assert "unlisted" not in kept, kept
    [run] = view(runtime, task["id"])["monitoring"]
    assert "unlisted" not in run


async def test_a_runs_time_is_since_it_stood_so(harness):
    """E77: just marked ended, a run read "ended · 3d ago" (since its worker first said it)."""
    runtime = await harness.connect()
    other = {"url": FLYTE + "x", "name": "Other", "status": "running"}
    _worker, task, _told = await delivered(harness, runtime, NIGHTLY, other)
    for url in (FLYTE, other["url"]):
        said_at(harness, task["id"], url, at=ago(days=3))
    # Review: GitHub read it as it was said ("queued" both times): no change, not "just now".
    said_at(harness, task["id"], other["url"], checked_at=ago(minutes=5))
    runtime.task_actions.end_runs(task["id"], [FLYTE], "ended")
    runs = {item["title"]: item for item in view(runtime, task["id"])["deliverables"]}
    marked = json.loads(harness.store.get(f"artifacts:{task['id']}"))
    assert runs["Nightly export"]["changed_at"] == marked[0]["changed_at"]
    assert runs["Other"]["changed_at"] == marked[1]["at"]


async def test_a_run_said_again_as_it_stood_keeps_its_time(harness):
    """E77: its worker saying again "running" of a run running for three days is no change."""
    runtime = await harness.connect()
    worker, task, _told = await delivered(harness, runtime, NIGHTLY)
    said_at(harness, task["id"], FLYTE, at=ago(days=3))
    [first] = json.loads(harness.store.get(f"artifacts:{task['id']}"))

    async def says(status: str) -> tuple[dict, dict]:
        run = {**NIGHTLY, "status": status}
        await call_tool(runtime, "report", worker, status="progress", task_id=task["id"],
                        summary="Where it stands.", artifacts=[run])  # fmt: skip
        [run] = view(runtime, task["id"])["deliverables"]
        [kept] = json.loads(harness.store.get(f"artifacts:{task['id']}"))
        return run, kept

    run, _kept = await says("running")
    assert run["changed_at"] == first["at"]
    # One you stopped tracking, said again: followed again, from then.
    runtime.task_actions.end_runs(task["id"], [FLYTE], "untracked")
    said_at(harness, task["id"], FLYTE, changed_at=ago(hours=5))
    run, kept = await says("running")
    assert (run.get("untracked"), run["changed_at"]) == (None, kept["at"])
    assert kept["at"] > ago(minutes=1)
    run, kept = await says("succeeded")
    assert run["changed_at"] == kept["at"] > first["at"]
