"""Share links on the real thing, with the test account only (evan666-lgtm): every gh call goes
through a guard that lets no other account be listed, asked for or used, and the script stops
unless the bound account is that one. Chief of Staff's own code makes, opens and ends the links
(in-process runtimes on a stand-in Copilot; GitHub, gh and the viewer are real):

  S1  Create link: a real secret gist through the bound-account primitive, ciphertext only.
  S2  The viewer (site/ built, served locally) opens it in Chromium from GitHub's API, under
      its CSP.
  S3  Open in my Chief: a second Chief (its own data folder) opens the link from GitHub's API
      without signing in; the same link again is the first copy.
  S4  Revoke: the viewer and an import both say revoked within 2 s.
  S5  A link with a 2-minute expiry (the test-only setting) is deleted by the timer.
  S6  One that expired while its Chief was stopped: the viewer says expired though its gist is
      still there, and the next start deletes it.
  S7  An uncertain create (no answer after the gist was made) is found and deleted.
  S8  Share… on a report in a chat (0.1.60): from a report its card has a newer result than,
      the card as it is now (said so; not from its latest report or its line on Home); its
      link opens in the viewer with that latest result, and Revoke deletes its gist.
  S9  A title cut through a token (U9, 0.1.61): the token's start in the title is flagged with
      the whole one in its result; removed, the viewer shows neither, and Revoke deletes it.
  At the end, no gist this run made is left (each is deleted in `finally` too).

    uv run --extra dev --with playwright python scripts/live_share.py

LIVE_SHARE_PORT: the local viewer's port (20630). About four minutes (S5 and S6 wait for their
expiry). Screenshots in browser-shots/live-share/.
"""

import asyncio
import contextlib
import functools
import http.server
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

ACCOUNT = "evan666-lgtm"


def test_token() -> str:
    done = subprocess.run(
        ["gh", "auth", "token", "--hostname", "github.com", "--user", ACCOUNT],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    token = done.stdout.strip()
    if done.returncode or not token:
        raise SystemExit(f"{ACCOUNT} is not signed in to gh here: gh auth login, then again.")
    return token


# Asked of gh before the tests' own home replaces this one (conftest, imported below): from then
# on gh sees no account of the owner's, and this token is the only one there is.
TOKEN = test_token()
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))
from build_site import build  # noqa: E402
from conftest import Harness, call_tool, settle  # noqa: E402
from share_world import finished_task  # noqa: E402

from chiefofstaff import shares as shares_module  # noqa: E402
from chiefofstaff import sharing  # noqa: E402
from chiefofstaff.sources.github import GitHubSource, gh  # noqa: E402

PORT = int(os.environ.get("LIVE_SHARE_PORT", "20630"))
SCRATCH = Path(os.environ.get("LIVE_SHARE_SCRATCH", ROOT / "browser-shots" / "live-share-run"))
SHOTS = ROOT / "browser-shots" / "live-share"
FAILURES: list[str] = []
MADE: set[str] = set()  # Every gist this run made (deleted at the end, whatever happened).


def check(ok: bool, what: str):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        FAILURES.append(what)


class Guard:
    """gh as Chief calls it, for the test account only (gh runs in the tests' own home, with
    GH_TOKEN the test account's): `auth status` lists it alone, its token is the only one
    given, and an API call without it is refused. `hang_after_create`: the next gist is made,
    then no answer comes."""

    def __init__(self):
        self.hang_after_create = False

    async def __call__(self, *arguments, payload=None, token=""):
        args = list(arguments)
        if args[:2] == ["auth", "status"]:
            status = json.loads(await gh(*arguments))
            hosts = status.get("hosts", {}).get("github.com", [])
            mine = [dict(a, active=True) for a in hosts if a.get("login") == ACCOUNT][:1]
            return json.dumps({"hosts": {"github.com": mine}})
        if args[:2] == ["auth", "token"]:
            if args[-1] != ACCOUNT:
                raise RuntimeError(f"live_share uses {ACCOUNT} only")
            return TOKEN + "\n"
        if args[:1] == ["api"] and not token:
            raise RuntimeError("live_share: no gh call as gh's active account")
        answer = await gh(*arguments, payload=payload, token=token)
        if args[:2] == ["api", "gists"] and "POST" in args:
            made = json.loads(answer)
            MADE.add(made["id"])
            if self.hang_after_create:
                self.hang_after_create = False
                raise RuntimeError("GitHub request timed out.")
        return answer


async def chief(name: str, guard: Guard) -> Harness:
    folder = SCRATCH / name
    harness = Harness(folder)
    harness.runtime.github = GitHubSource(harness.store, harness.runtime, gh_command=guard)
    await harness.connect()
    return harness


async def share(harness: Harness, title: str) -> dict:
    task = await finished_task(harness, title=title)
    preview = await harness.runtime.shares.preview(task["id"])
    logins = [account["login"] for account in preview["accounts"]]
    if logins != [ACCOUNT]:
        raise SystemExit(f"live_share refuses: the bound account would be {logins}, not {ACCOUNT}")
    decisions = {hit["id"]: "remove" for hit in preview["hits"]}
    return await harness.runtime.shares.create(task["id"], decisions, preview["digest"], 1, ACCOUNT)


async def gist_status(gist_id: str) -> int:
    async with httpx.AsyncClient(timeout=15) as client:
        answer = await client.get(
            f"https://api.github.com/gists/{gist_id}",
            params={"_": str(time.time_ns())},
            headers={"Accept": "application/vnd.github+json", "User-Agent": "chiefofstaff-live"},
        )
    return answer.status_code


@contextlib.contextmanager
def serve(folder: Path):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(folder))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{PORT}"
    finally:
        server.shutdown()
        server.server_close()


async def viewer_state(page, base: str, link: str) -> str:
    await page.goto("about:blank")
    await page.goto(f"{base}/s/{link[link.index('#') :]}")
    await page.wait_for_selector("body[data-state]:not([data-state=opening])", state="attached")
    return await page.evaluate("document.body.dataset.state")


async def scenario(guard: Guard, base: str, page):
    owner = await chief("owner", guard)
    friend = await chief("friend", guard)
    shares, theirs = owner.runtime.shares, friend.runtime.shares

    print("S1 create link")
    made = await share(owner, "Fix the flaky login test")
    link, row = made["link"], made["share"]
    gist_id = sharing.parse_link(link)["gist_id"]
    async with httpx.AsyncClient(timeout=15) as client:
        gist = (
            await client.get(
                f"https://api.github.com/gists/{gist_id}",
                params={"_": str(time.time_ns())},
                headers={"User-Agent": "chiefofstaff-live"},
            )
        ).json()
    check(
        gist["public"] is False and gist["owner"]["login"] == ACCOUNT,
        "a secret gist of the test account",
    )
    check(
        gist["description"].startswith(f"Chief of Staff share {row['id']} · encrypted"),
        "described by its share id, nothing of the card",
    )
    content = gist["files"]["share.txt"]["content"]
    check("flaky" not in content and "Fix" not in content, "the gist holds ciphertext only")

    print("S2 the viewer")
    state = await viewer_state(page, base, link)
    check(state == "card", f"the viewer opens it from GitHub's API (state {state})")
    by = await page.locator(".v-by").inner_text()
    check(f"@{ACCOUNT}" in by, f"shared by the gist's owner, as GitHub says ({by})")
    await page.screenshot(path=SHOTS / "live-viewer-valid-1440-light.png", full_page=True)

    print("S3 open in my Chief")
    opened = await theirs.open_link(link)
    check(
        opened["message"]["meta"]["shared"]["card"]["title"] == "Fix the flaky login test",
        "a second Chief opens the card",
    )
    check(
        sharing.parse_link(link)["key"]
        not in json.dumps(friend.store.all("SELECT * FROM shared_cards")),
        "its key is kept nowhere",
    )
    again = await theirs.open_link(link)
    check(
        again["duplicate"] and again["message"]["id"] == opened["message"]["id"],
        "the same link again: the first copy",
    )

    print("S4 revoke")
    await shares.revoke(row["id"])
    gone = time.monotonic()
    state = await viewer_state(page, base, link)
    viewer_at = time.monotonic() - gone
    check(
        state == "revoked" and viewer_at < 2,
        f"the viewer says revoked {viewer_at:.1f} s after (state {state})",
    )
    await page.screenshot(path=SHOTS / "live-viewer-revoked-1440-light.png", full_page=True)
    try:
        await theirs.opened(link)
        check(False, "an import says revoked")
    except ValueError as said:
        check(
            "revoked" in str(said) and time.monotonic() - gone < 2.5,
            f"an import says revoked ({said})",
        )
    check(await gist_status(gist_id) == 404, "its gist is gone")
    copy_id = opened["message"]["meta"]["shared"]["id"]
    check(theirs.copy(copy_id)["login"] == ACCOUNT, "the copy opened before stays")

    print("S5 and S6 expiry (about two minutes)")
    os.environ[shares_module.TEST_SECONDS] = "120"
    timed = await share(owner, "Tidy the README")
    stopped = await chief("stopped", guard)
    os.environ[shares_module.TEST_SECONDS] = "60"
    early = await share(stopped, "Rename the settings flag")
    os.environ.pop(shares_module.TEST_SECONDS)
    early_gist = sharing.parse_link(early["link"])["gist_id"]
    await stopped.runtime.stop()  # Chief of Staff stops before it expires.
    stopped.store.close()
    check(owner.runtime.shares.timer is not None, "a timer waits for the live link")
    deadline = time.monotonic() + 170
    timed_gist = sharing.parse_link(timed["link"])["gist_id"]
    while time.monotonic() < deadline and await gist_status(timed_gist) != 404:
        await asyncio.sleep(5)
        await settle(owner.runtime)
    check(await gist_status(timed_gist) == 404, "the timer deleted the gist at expiry")
    check(shares.row(timed["share"]["id"])["state"] == "expired", "its link says expired")
    check(await gist_status(early_gist) == 200, "the stopped Chief's expired gist is still there")
    state = await viewer_state(page, base, early["link"])
    check(
        state == "expired",
        f"the viewer refuses it all the same: its expiry is inside (state {state})",
    )
    await page.screenshot(path=SHOTS / "live-viewer-expired-1440-light.png", full_page=True)
    restarted = await chief("stopped", guard)
    await restarted.runtime.shares.resume()
    check(await gist_status(early_gist) == 404, "its next start deleted it")

    print("S7 an uncertain create")
    guard.hang_after_create = True
    before = set(MADE)
    try:
        await share(owner, "Bump the lockfile")
        check(False, "no answer is said")
    except ValueError as said:
        check("didn't answer, so nothing was shared" in str(said), f"no answer is said ({said})")
    [uncertain] = MADE - before
    check(await gist_status(uncertain) == 404, "the gist it made anyway was found and deleted")

    print("S8 share from a report in a chat")
    task = await finished_task(
        owner, title="Speed up the nightly export", result="Made the export faster by batching."
    )
    said = await call_tool(
        owner.runtime, "report", task["session_id"], status="done", task_id=task["id"],
        summary="Made the export faster with one streaming query: 41 minutes down to 4.",
    )  # fmt: skip
    check(said.result_type == "success", "its report amended (the same turn): a newer result")
    rows = owner.store.all(
        "SELECT id, kind FROM messages WHERE json_extract(meta, '$.task_id')=? "
        "AND json_extract(meta, '$.status')='done' ORDER BY id",
        (task["id"],),
    )
    reports = [row["id"] for row in rows if row["kind"] == "report"]
    [home] = [row["id"] for row in rows if row["kind"] == "inbound"]
    preview = await shares.preview(task["id"], reports[0])
    check(
        [account["login"] for account in preview["accounts"]] == [ACCOUNT],
        "the bound account is the test account",
    )
    check(
        preview["older_report"] and "one streaming query" in preview["card"]["result"],
        "from its earlier report: the card as it is now, and the dialog says so",
    )
    for message in (reports[-1], home):
        check(
            not (await shares.preview(task["id"], message))["older_report"],
            "from its latest report, or its line on Home: nothing more said",
        )
    decisions = {hit["id"]: "remove" for hit in preview["hits"]}
    made = await shares.create(task["id"], decisions, preview["digest"], 1, ACCOUNT)
    state = await viewer_state(page, base, made["link"])
    shown = await page.locator("main").inner_text()
    check(
        state == "card" and "one streaming query" in shown and "batching" not in shown,
        f"the viewer opens it with its latest result (state {state})",
    )
    await page.screenshot(path=SHOTS / "live-viewer-from-report-1440-light.png", full_page=True)
    await shares.revoke(made["share"]["id"])
    report_gist = sharing.parse_link(made["link"])["gist_id"]
    check(await gist_status(report_gist) == 404, "revoked: its gist is gone")

    print("S9 a title cut through a token")
    # A Chief of its own (the owner's has as many workers at work as it starts by now).
    cutter = await chief("cut", guard)
    shares = cutter.runtime.shares
    token = "gh" + "p_" + "Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78"
    asked = f"Check the staging API: token {token} at http://10.0.4.17:8080/health"
    task = await finished_task(cutter, title=asked[:60], result=f"Checked it with {token}.")
    preview = await shares.preview(task["id"])
    check(
        sorted(where for hit in preview["hits"] for where in hit["where"]) == ["Result", "Title"],
        "the token's start in the title is flagged, with the whole one in its result",
    )
    decisions = {hit["id"]: "remove" for hit in preview["hits"]}
    made = await shares.create(task["id"], decisions, preview["digest"], 1, ACCOUNT)
    state = await viewer_state(page, base, made["link"])
    shown = await page.locator("main").inner_text()
    check(
        state == "card" and "ghp_" not in shown and "token [removed]" in shown,
        f"the viewer shows neither, removed (state {state})",
    )
    await page.screenshot(path=SHOTS / "live-viewer-cut-title-1440-light.png", full_page=True)
    await shares.revoke(made["share"]["id"])
    cut_gist = sharing.parse_link(made["link"])["gist_id"]
    check(await gist_status(cut_gist) == 404, "revoked: its gist is gone")


async def main() -> int:
    # (A Chief started again on its data folder resumes its Router on a stand-in that has no
    # session to resume, and says so: not this scenario's.)
    logging.getLogger("chiefofstaff").setLevel(logging.ERROR)
    os.environ["GH_TOKEN"] = TOKEN  # Any stray gh call is the test account's too.
    os.environ["GH_PROMPT_DISABLED"] = "1"
    shutil.rmtree(SCRATCH, ignore_errors=True)
    SCRATCH.mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    site = build(SCRATCH / "site")
    guard = Guard()
    try:
        with serve(site) as base:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                await scenario(guard, base, page)
                await browser.close()
    finally:
        for gist_id in sorted(MADE):
            with contextlib.suppress(Exception):
                await gh("api", "--method", "DELETE", f"gists/{gist_id}", token=TOKEN)
        listed = json.loads(await gh("api", "gists?per_page=100", token=TOKEN) or "[]")
        left = [gist["id"] for gist in listed if gist["id"] in MADE]
        check(not left, f"no gist this run made is left ({len(MADE)} made, {len(left)} left)")
        shutil.rmtree(SCRATCH, ignore_errors=True)
    print(f"\n{len(FAILURES)} failed" if FAILURES else "\nLIVE SHARE PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
