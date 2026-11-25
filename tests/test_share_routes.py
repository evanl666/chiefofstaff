"""Share links through Chief's routes (shares.py, web.py), on a fake gh: the preview, Create
link (a secret gist of the owner's bound account, the card sealed to its id), the list in
Settings → You, Revoke, the HTML file's card, and what GitHub or gh can refuse."""

import re
from datetime import datetime, timedelta

import pytest
from conftest import FakeClient, FakeSlackMcp, FakeTokens, Harness
from fastapi.testclient import TestClient
from share_world import NO_SCOPE, SLOW, TIMED_OUT, FakeGitHub, finished_task
from test_share_remove import KEY_ID, KEY_SECRET, block

from chiefofstaff import sharing
from chiefofstaff.runtime import Runtime
from chiefofstaff.shares import SHARE_ID, ChangedError
from chiefofstaff.sources.github import GitHubSource
from chiefofstaff.sources.slack import SlackSource
from chiefofstaff.web import create_app

TOKEN = "launch-token"
ORIGIN = {"origin": "http://127.0.0.1:8765"}


@pytest.fixture
async def world(tmp_path):
    harness = Harness(tmp_path / "data")
    fake = FakeGitHub()
    harness.runtime.github = GitHubSource(harness.store, harness.runtime, gh_command=fake)
    await harness.connect()
    harness.fake = fake
    harness.shares = harness.runtime.shares
    return harness


def decide(preview: dict, choice: str = "remove") -> dict:
    return {hit["id"]: choice for hit in preview["hits"]}


async def test_the_preview_is_the_card_as_it_would_be_shared(world):
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    card = preview["card"]
    assert card["title"] == "Fix the flaky login test" and card["status"] == "review"
    assert card["status_words"] == "Ready for review"
    assert card["verdict"] == "verified"
    assert card["criteria"] == [
        {"text": "`uv run pytest -q` passes", "state": "verified", "check": "uv run pytest -q"}
    ]
    assert [(h["id"], h["label"]) for h in preview["hits"]] == [
        ("h1", "an internal address"),
        ("h2", "a GitHub token"),
        ("h3", "an email address"),
    ]
    assert preview["accounts"] == [{"login": "jordan", "active": True}]
    assert preview["account"] == "jordan" and preview["live"] is None
    assert preview["days"] == [1, 7, 30]
    assert not world.shares.exists(), "a preview makes nothing"
    gh_writes = [args for args, _ in world.fake.calls if "POST" in args or "DELETE" in args]
    assert gh_writes == []


async def test_create_link_seals_the_decided_card_into_a_secret_gist(world):
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    decisions = {"h1": "remove", "h2": "remove", "h3": "keep"}
    made = await world.shares.create(task["id"], decisions, preview["digest"], 7, "jordan")
    [gist] = world.fake.gists.values()
    assert gist["public"] is False
    share_id = made["share"]["id"]
    assert SHARE_ID.fullmatch(share_id)
    assert gist["description"].startswith(f"Chief of Staff share {share_id} · encrypted · expires ")
    assert "flaky" not in gist["description"], "the description names nothing of the card"
    link = sharing.parse_link(made["link"])
    assert link["gist_id"] == gist["id"]
    payload = sharing.open_payload(gist["files"]["share.txt"]["content"], link["key"], 4242001)
    assert payload["by"] == {"login": "jordan", "id": 4242001}
    shared = payload["card"]["result"]
    assert "10.20.4.17" not in shared and "ghp_" not in shared
    assert "[removed]" in shared and "maya.chen@example.com" in shared, "kept as decided"
    created = datetime.fromisoformat(payload["created_at"])
    assert datetime.fromisoformat(payload["expires_at"]) - created == timedelta(days=7)
    with pytest.raises(sharing.DamagedError):
        sharing.open_payload(gist["files"]["share.txt"]["content"], link["key"], 4242002)
    # Made as the bound account: its token, its identity checked, never the shared routing.
    tokens = {token for args, token in world.fake.calls if args[:2] == ("api", "gists")}
    assert tokens == {"tok-jordan"}
    assert world.store.get("share:account") == "jordan"
    listed = world.shares.listed()
    assert [(row["title"], row["state"], row["link"]) for row in listed] == [
        ("Fix the flaky login test", "live", made["link"])
    ]
    again = await world.shares.preview(task["id"])
    assert again["live"]["id"] == share_id, "Share… on this card opens on its live link"


async def test_create_link_waits_for_every_decision_and_the_card_as_it_was_shown(world):
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    with pytest.raises(ValueError, match="Remove or keep each"):
        await world.shares.create(task["id"], {"h1": "remove"}, preview["digest"], 7, "jordan")
    world.runtime.tasks.move(task["id"], "review", result="Now at http://10.0.0.5/x instead.")
    with pytest.raises(ChangedError, match="changed since this preview"):
        await world.shares.create(task["id"], decide(preview), preview["digest"], 7, "jordan")
    fresh = await world.shares.preview(task["id"])
    with pytest.raises(ValueError, match="1, 7 or 30"):
        await world.shares.create(task["id"], decide(fresh), fresh["digest"], 365, "jordan")
    assert world.fake.gists == {}


async def test_what_remove_takes_out_is_out_of_the_file_the_link_and_a_copy_opened(world):
    # SECURITY-REVIEW.md, 1: each secret goes whole, in the HTML file, the sealed card and a copy.
    key = block("RSA ", 3)
    result = (
        f"Rotated the deploy credentials.\n\n{key}\n\n"
        f"aws_access_key_id = {KEY_ID}\naws_secret_access_key = {KEY_SECRET}\n"
        "DATABASE_URL=postgres://app:S3cretPass@db.example.com/app\n"
        'DB_PASSWORD=hunter2hunter2\n{"password": "json-secret-1"}'
    )
    task = await finished_task(world, result=result)
    preview = await world.shares.preview(task["id"])
    decisions = decide(preview)
    filed = world.shares.file(task["id"], decisions, preview["digest"])["card"]
    made = await world.shares.create(task["id"], decisions, preview["digest"], 7, "jordan")
    [gist] = world.fake.gists.values()
    blob, link = gist["files"]["share.txt"]["content"], sharing.parse_link(made["link"])
    sealed = sharing.open_payload(blob, link["key"], 4242001)["card"]
    world.shares.transport = world.fake.transport()
    opened = (await world.shares.open_link(made["link"]))["message"]["meta"]["shared"]["card"]
    secrets = [*key.splitlines()[1:], KEY_ID, KEY_SECRET, "S3cretPass", "hunter2", "json-secret"]
    for card in (filed, sealed, opened):
        assert card["result"] == (
            "Rotated the deploy credentials.\n\n[removed]\n\naws_access_key_id = [removed]\n"
            "DATABASE_URL=[removed]\n[removed]\n{[removed]}"
        )
        every = "\n".join(text for _, text, _ in sharing.strings(card))
        assert not [secret for secret in secrets if secret in every]


async def test_only_a_finished_card_can_be_shared(world):
    task = await finished_task(world)
    world.runtime.tasks.move(task["id"], "running")
    with pytest.raises(ValueError, match="Only a finished card"):
        await world.shares.preview(task["id"])


async def test_no_gh_sign_in_says_so_and_shares_nothing(world):
    task = await finished_task(world)
    world.fake.sign_out("jordan")
    preview = await world.shares.preview(task["id"])
    assert preview["accounts"] == [] and preview["account"] == ""
    with pytest.raises(ValueError, match="No GitHub account is signed in to gh"):
        await world.shares.create(task["id"], decide(preview), preview["digest"], 7, "jordan")
    file = world.shares.file(task["id"], decide(preview), preview["digest"])
    assert file["name"] == "fix-the-flaky-login-test.html"
    assert "ghp_" not in repr(file["card"]) and file["card"]["title"] == "Fix the flaky login test"
    assert world.fake.gists == {} and not world.shares.exists()


@pytest.mark.parametrize(
    ("fault", "words"),
    [
        (NO_SCOPE, "@jordan's sign-in can't create gists. Run `gh auth refresh -s gist`"),
        (SLOW, "GitHub asked to wait a minute before the next link. Nothing was shared."),
        (TIMED_OUT, "GitHub didn't answer, so nothing was shared."),
    ],
)
async def test_what_github_refuses_is_said_and_nothing_is_left(world, fault, words):
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    world.fake.create_fault = fault
    with pytest.raises(ValueError, match=re.escape(words)):
        await world.shares.create(task["id"], decide(preview), preview["digest"], 7, "jordan")
    assert world.fake.gists == {} and world.shares.listed() == []


async def test_a_create_with_no_answer_that_went_through_is_found_and_deleted(world):
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    world.fake.create_fault, world.fake.created_anyway = TIMED_OUT, True
    with pytest.raises(ValueError, match="GitHub didn't answer, so nothing was shared"):
        await world.shares.create(task["id"], decide(preview), preview["digest"], 7, "jordan")
    assert world.fake.gists == {}, "found by its share id in its description, and deleted"


async def test_a_create_cut_short_by_a_stop_is_reconciled_at_the_next_start(world):
    task = await finished_task(world)
    world.shares.table()
    world.store.execute(
        "INSERT INTO shares(id, task_id, title, login, created_at, expires_at, state) "
        "VALUES('sh_0badf00d', ?, 't', 'jordan', '2026-11-24T00:00:00Z', "
        "'2026-12-01T00:00:00Z', 'creating')",
        (task["id"],),
    )
    world.fake.made("jordan", 4242001, "x", "Chief of Staff share sh_0badf00d · encrypted")
    keep = world.fake.made("jordan", 4242001, "y", "my own gist")
    await world.shares.resume()
    assert list(world.fake.gists) == [keep], "only the one it made, nothing of the owner's"
    assert world.shares.row("sh_0badf00d")["state"] == "failed"


async def test_chiefs_own_limit_per_account(world, monkeypatch):
    import chiefofstaff.shares as shares_module

    monkeypatch.setattr(shares_module, "LINKS_A_MINUTE", 2)
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    for _ in range(2):
        await world.shares.create(task["id"], decide(preview), preview["digest"], 1, "jordan")
    with pytest.raises(ValueError, match="wait a minute"):
        await world.shares.create(task["id"], decide(preview), preview["digest"], 1, "jordan")
    assert len(world.fake.gists) == 2


async def test_with_several_accounts_the_link_is_made_as_the_one_chosen(world):
    world.fake.accounts["maya"] = {"id": 77, "token": "tok-maya", "active": False, "in": True}
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    assert [a["login"] for a in preview["accounts"]] == ["jordan", "maya"], "gh's active first"
    made = await world.shares.create(task["id"], decide(preview), preview["digest"], 1, "maya")
    [gist] = world.fake.gists.values()
    assert gist["owner"]["login"] == "maya" and made["share"]["login"] == "maya"
    again = await world.shares.preview(task["id"])
    assert again["account"] == "maya", "the last one used is remembered"
    with pytest.raises(ValueError, match="@nobody isn't signed in"):
        await world.shares.create(task["id"], decide(again), again["digest"], 1, "nobody")


async def test_a_private_repositorys_pr_is_said_in_the_preview(world):
    task = await finished_task(world)
    view = world.runtime.task_actions.view(task)
    assert view  # Its PRs come from its receipts; a card with none has no note.
    assert (await world.shares.private_repos({"prs": []})) == []
    world.fake.private.add("acme/api")
    card = {"prs": [{"label": "acme/api#418"}, {"label": "acme/web#2"}]}
    assert await world.shares.private_repos(card) == ["acme/api"]


async def test_revoke_deletes_the_gist_at_once(world):
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    made = await world.shares.create(task["id"], decide(preview), preview["digest"], 7, "jordan")
    assert world.shares.timer is not None, "a timer for its expiry while it is live"
    assert world.shares.timer not in world.runtime.jobs, "no work in progress: nothing waits on it"
    revoked = await world.shares.revoke(made["share"]["id"])
    assert revoked["state"] == "revoked" and revoked["link"] == ""
    assert world.fake.gists == {}
    assert world.shares.timer is None, "no timer once none is live"
    assert [row["state"] for row in world.shares.listed()] == ["revoked"]
    assert (await world.shares.preview(task["id"]))["live"] is None


async def test_revoke_signed_out_says_how_and_keeps_it_listed_as_live(world):
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    made = await world.shares.create(task["id"], decide(preview), preview["digest"], 7, "jordan")
    world.fake.sign_out("jordan")
    [gist_id] = world.fake.gists
    said = (
        "Couldn't revoke: @jordan isn't signed in to gh on this computer. Sign in with `gh auth "
        f"login`, or delete it yourself on GitHub: gist.github.com/jordan/{gist_id}"
    )
    with pytest.raises(ValueError, match=f"^{re.escape(said)}$"):
        await world.shares.revoke(made["share"]["id"])
    [row] = world.shares.listed()
    assert (
        row["state"] == "live" and row["error"] == "@jordan isn't signed in to gh on this computer"
    )
    world.fake.accounts["jordan"]["in"] = True
    assert (await world.shares.revoke(row["id"]))["state"] == "revoked"
    assert world.fake.gists == {}


async def test_a_gist_already_gone_revokes_fine(world):
    task = await finished_task(world)
    preview = await world.shares.preview(task["id"])
    made = await world.shares.create(task["id"], decide(preview), preview["digest"], 7, "jordan")
    world.fake.gists.clear()  # Deleted by the owner on GitHub.
    assert (await world.shares.revoke(made["share"]["id"]))["state"] == "revoked"


@pytest.fixture
def client(tmp_path):
    fake = FakeGitHub()
    holder = {}

    def factory(store, bus):
        async def notifier(title, text):
            pass

        runtime = Runtime(
            store, bus, backend_factory=lambda settings: FakeClient(), notifier=notifier
        )
        runtime.slack = SlackSource(store, runtime, tokens=FakeTokens(""), mcp=FakeSlackMcp())
        runtime.github = GitHubSource(store, runtime, gh_command=fake)
        holder["runtime"] = runtime
        return runtime

    app = create_app(tmp_path / "data", TOKEN, 8765, runtime_factory=factory)
    with TestClient(app, base_url="http://127.0.0.1:8765") as http:
        http.fake = fake
        http.runtime = holder["runtime"]
        yield http


def test_every_share_route_needs_the_launch_key_and_a_trusted_origin(client):
    routes = [
        ("GET", "/api/tasks/t1/share"),
        ("POST", "/api/tasks/t1/share"),
        ("POST", "/api/tasks/t1/share-snapshot"),
        ("GET", "/api/shares"),
        ("POST", "/api/shares/sh_00000000/revoke"),
    ]
    for method, path in routes:
        assert client.request(method, path, json={}, headers=ORIGIN).status_code == 401, path
    client.headers["X-Chief-Key"] = TOKEN
    for method, path in routes:
        if method != "GET":
            refused = client.request(method, path, json={}, headers={"origin": "https://evil"})
            assert refused.status_code == 403, path
    assert client.get("/api/shares").json() == []
    assert client.get("/api/tasks/nope/share").status_code == 404
    assert client.post("/api/shares/sh_00000000/revoke", headers=ORIGIN).status_code == 404


def test_the_routes_preview_create_list_and_revoke(client):
    runtime = client.runtime
    client.headers["X-Chief-Key"] = TOKEN
    task = runtime.tasks.create(runtime.router_id(), "Write the notes", "x")
    runtime.tasks.move(task["id"], "review", result="Notes for a.b@example.com")
    preview = client.get(f"/api/tasks/{task['id']}/share").json()
    assert [hit["label"] for hit in preview["hits"]] == ["an email address"]
    body = {
        "decisions": {"h1": "remove"},
        "digest": preview["digest"],
        "days": 30,
        "login": "jordan",
    }
    stale = client.post(
        f"/api/tasks/{task['id']}/share", json={**body, "digest": "old"}, headers=ORIGIN
    )
    assert stale.status_code == 409 and stale.json()["changed"] is True
    made = client.post(f"/api/tasks/{task['id']}/share", json=body, headers=ORIGIN).json()
    assert made["link"].startswith(sharing.VIEWER)
    file = client.post(
        f"/api/tasks/{task['id']}/share-snapshot",
        json={"decisions": {"h1": "keep"}, "digest": preview["digest"]},
        headers=ORIGIN,
    ).json()
    assert file["card"]["result"] == "Notes for a.b@example.com"
    [row] = client.get("/api/shares").json()
    assert row["state"] == "live" and row["link"] == made["link"]
    revoked = client.post(f"/api/shares/{row['id']}/revoke", headers=ORIGIN).json()
    assert revoked["state"] == "revoked" and client.fake.gists == {}
    bad = client.post(
        f"/api/tasks/{task['id']}/share", json={**body, "decisions": {"h1": "x"}}, headers=ORIGIN
    )
    assert bad.status_code == 422


def test_never_used_there_is_no_table_no_timer_and_no_github_call(client):
    client.headers["X-Chief-Key"] = TOKEN
    runtime = client.runtime
    assert client.get("/api/shares").json() == []
    assert not runtime.shares.exists() and runtime.shares.timer is None
    tables = {row["name"] for row in runtime.store.all("SELECT name FROM sqlite_master")}
    assert "shares" not in tables
    assert not [args for args, _ in client.fake.calls if args[:1] == ("api",)]
    assert runtime.store.get("share:account") is None
