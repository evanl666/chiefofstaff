"""The share links viewer (site/s/), built as Pages serves it, in Chromium: every state it can
show, under its own Content-Security-Policy and Trusted Types, with no request but the one for
the gist (past GitHub's cache); hostile cards drawn as text; its keyboard order; and the format
held to the same bytes by its WebCrypto and Chief's Python `cryptography`, both ways.

GitHub's API is answered from fixtures (page.route); scripts/live_share.py runs it against a real
secret gist. Screenshots at 1440, 800 and 390 px, light and dark, in browser-shots/viewer/.

    uv run --extra dev --with playwright python scripts/viewer_check.py [--site BUILT]

VIEWER_PORT: the local server's port (any free one by default).
"""

# The page's checks are JavaScript, written on long lines.
# ruff: noqa: E501

import argparse
import asyncio
import contextlib
import functools
import http.server
import json
import os
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from build_site import build  # noqa: E402

from chiefofstaff import sharing  # noqa: E402

SHOTS = ROOT / "browser-shots" / "viewer"
VECTOR = json.loads((ROOT / "tests" / "share_vector.json").read_text())
NOW = datetime(2026, 11, 25, 1, 56, tzinfo=UTC)
OWNER = {"login": "jordan", "id": 4242001}
GIST = "6f1c0d2e9a8b47c3b5e2f0a1d4c7e9b2"
FAILURES: list[str] = []


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


CARD = {
    "title": "Fix the flaky login test",
    "status": "review",
    "status_words": "Ready for review",
    "finished_at": stamp(NOW - timedelta(days=2, minutes=7)),
    "result": "Fixed the flaky login test; all tests pass.\n\n"
    "- **Cause:** token expiry read the real clock, so slow runs failed\n"
    "- **Fix:** `app/login.py` takes the clock as a parameter\n"
    "- Reproduced it first on staging, [removed], with `API_TOKEN=[removed]`\n"
    "- Maya ([removed]) confirmed the timeouts on her side\n"
    "- `uv run pytest -q`: 8 passed\n"
    "- Notes in [the runbook](https://docs.example.com/runbooks/login)",
    "verdict": "verified",
    "criteria": [
        {"text": "`uv run pytest -q` passes", "state": "verified", "check": "uv run pytest -q"},
        {
            "text": "The login tests pass 20 runs in a row",
            "state": "verified",
            "check": "uv run pytest -q --count 20 tests/test_login.py",
        },
    ],
    "checks": [
        {
            "label": "tests",
            "command": "uv run pytest -q --count 20 tests/test_login.py",
            "outcome": "passed",
            "failed_before": True,
            "runs": 3,
        }
    ],
    "changes": {"files": 1, "insertions": 1, "deletions": 1, "commits": 1},
    "at": "75dc3be",
    "prs": [
        {
            "label": "acme/api#418",
            "url": "https://github.com/acme/api/pull/418",
            "title": "Fix the flaky login test: inject the clock",
            "state": "OPEN",
            "checks": "passing",
            "review": "",
        }
    ],
    "flags": [],
}
HOSTILE = {
    **CARD,
    "title": '<img src=x onerror="alert(1)"> Totally safe',
    "result": "<script>alert(1)</script> <b>bold?</b>\n\n[click me](javascript:alert(1)) "
    "and [a docs page](https://evil.example/phish) and ![img](https://evil.example/x.png)\n\n"
    + ("A long result. " * 2600),
    "flags": ["<iframe src=https://evil.example>"],
}


def payload(card: dict = CARD, **times) -> dict:
    return {
        "v": 1, "kind": "card",
        "created_at": stamp(times.get("created", NOW - timedelta(days=2))),
        "expires_at": stamp(times.get("expires", NOW + timedelta(days=5))),
        "by": OWNER, "app": "0.1.58", "card": card,
    }  # fmt: skip


def answer(blob: str, owner: dict = OWNER, files: dict | None = None) -> dict:
    return {
        "id": GIST, "public": False, "description": "Chief of Staff share sh_3f9a0c21 · encrypted",
        "owner": owner,
        "files": files if files is not None else {"share.txt": {"filename": "share.txt", "truncated": False, "content": blob}},
    }  # fmt: skip


def fixtures() -> dict:
    """Each state: its link, and how GitHub answers ("offline": no answer)."""
    shared = payload()
    blob, key = sharing.seal(shared, OWNER["id"])
    link = sharing.make_link(GIST, key, shared["expires_at"])
    stale = payload(expires=NOW - timedelta(hours=3))
    stale_blob, stale_key = sharing.seal(stale, OWNER["id"])
    raw = bytearray(sharing.unb64u(blob))
    raw[40] ^= 1
    newer = bytearray(sharing.unb64u(blob))
    newer[0] = 2
    hostile = payload(HOSTILE)
    hostile_blob, hostile_key = sharing.seal(hostile, OWNER["id"])
    reset = str(int((NOW + timedelta(minutes=41)).timestamp()))
    return {
        "valid": (link, 200, answer(blob), {}),
        "expired": (sharing.make_link(GIST, stale_key, stale["expires_at"]), 200, answer(stale_blob), {}),
        "expired-gone": (sharing.make_link(GIST, key, stamp(NOW - timedelta(days=1))), 404, {"message": "Not Found"}, {}),
        "revoked": (link, 404, {"message": "Not Found"}, {}),
        "damaged": (link, 200, answer(sharing.b64u(bytes(raw))), {}),
        "other-owner": (link, 200, answer(blob, {"login": "someone", "id": 999}), {}),
        "two-files": (link, 200, answer(blob, files={"share.txt": {"content": blob, "truncated": False}, "x.txt": {"content": "", "truncated": False}}), {}),
        "newer": (link, 200, answer(sharing.b64u(bytes(newer))), {}),
        "newer-link": (link.replace("#1.", "#2."), 200, answer(blob), {}),
        "incomplete": (link[:-30], 200, answer(blob), {}),
        "limited": (link, 403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
        "offline": (link, 0, {}, {}),
        "hostile": (sharing.make_link(GIST, hostile_key, hostile["expires_at"]), 200, answer(hostile_blob), {}),
    }  # fmt: skip


def check(ok: bool, what: str):
    print(("  ok   " if ok else "  FAIL ") + what, flush=True)
    if not ok:
        FAILURES.append(what)


@contextlib.contextmanager
def serve(folder: Path):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(folder))
    port = int(os.environ.get("VIEWER_PORT", "0"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


WATCH = """
window.__csp = [];
document.addEventListener("securitypolicyviolation", (event) => window.__csp.push(`${event.violatedDirective} ${event.blockedURI}`));
"""


async def viewer(context, base: str, case: tuple, path: str = "/s/"):
    link, status, body, headers = case
    page = await context.new_page()
    await page.clock.set_fixed_time(NOW)
    await page.add_init_script(WATCH)
    errors: list[str] = []
    requests: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: message.type == "error" and errors.append(message.text))
    page.on("request", lambda request: requests.append(request.url))

    async def route(intercepted):
        if not status:
            await intercepted.abort("internetdisconnected")
            return
        await intercepted.fulfill(
            status=status, body=json.dumps(body),
            headers={"content-type": "application/json", "access-control-allow-origin": "*",
                     "access-control-expose-headers": "X-RateLimit-Remaining, X-RateLimit-Reset", **headers},
        )  # fmt: skip

    await page.route("https://api.github.com/gists/**", route)
    await page.goto(f"{base}{path}{link[link.index('#') :]}")
    await page.wait_for_selector("body[data-state]:not([data-state=opening])", state="attached")
    return page, errors, requests


async def states(browser, base: str):
    print("states")
    cases = fixtures()
    expected = {
        "valid": "card", "expired": "expired", "expired-gone": "expired", "revoked": "revoked",
        "damaged": "damaged", "other-owner": "damaged", "two-files": "damaged", "newer": "newer",
        "newer-link": "newer", "incomplete": "incomplete", "limited": "limited",
        "offline": "offline", "hostile": "card",
    }  # fmt: skip
    titles = {
        "expired": "This link has expired", "expired-gone": "This link has expired",
        "revoked": "This link was revoked", "damaged": "This link is damaged",
        "newer": "This link needs a newer page", "incomplete": "This link is incomplete",
        "limited": "GitHub needs a short break", "offline": "Couldn't reach GitHub",
    }  # fmt: skip
    context = await browser.new_context(
        viewport={"width": 1440, "height": 900}, timezone_id="UTC", locale="en-US"
    )
    alerts: list[str] = []
    context.on("page", lambda page: page.on("dialog", lambda dialog: alerts.append(dialog.message)))
    for name, want in expected.items():
        page, errors, requests = await viewer(context, base, cases[name])
        got = await page.evaluate("document.body.dataset.state")
        check(got == want, f"{name}: shows {want} (got {got})")
        if name in titles:
            heading = await page.locator(".v-notice h1").inner_text()
            check(heading == titles[name], f"{name}: “{titles[name]}”")
        violations = await page.evaluate("window.__csp")
        check(not violations, f"{name}: nothing its CSP refuses ({violations[:2]})")
        script_errors = [text for text in errors if "Failed to load resource" not in text]
        check(not script_errors, f"{name}: no script error ({script_errors[:2]})")
        outside = [
            url for url in requests if not url.startswith((base, "https://api.github.com/gists/"))
        ]
        check(not outside, f"{name}: no request but the gist ({outside[:2]})")
        if name == "valid":
            gists = [url for url in requests if url.startswith("https://api.github.com/")]
            check(
                len(gists) == 1 and gists[0].startswith(f"https://api.github.com/gists/{GIST}?_="),
                f"one request for the gist, past GitHub's cache ({gists})",
            )
            check(
                await page.locator(".card.shared .head .title").inner_text() == CARD["title"],
                "the card's title, decrypted",
            )
            by = await page.locator(".v-by").inner_text()
            check(
                by == "Shared by @jordan · 2 days ago",
                f"shared by the gist's owner, as GitHub says ({by})",
            )
            check(
                not await page.locator("#decrypted").is_hidden(),
                "Decrypted in this browser, under a card",
            )
            check(
                "like the card here" in await page.locator(".v-made").inner_text(),
                "Made with: like the card here",
            )
            star = page.get_by_role("link", name="★ Star it on GitHub")
            check(
                await star.get_attribute("referrerpolicy") == "origin",
                "the repository link sends the site's name, nothing more",
            )
            report = await page.locator("#report-link").get_attribute("href")
            check(
                report == "https://github.com/contact/report-abuse?report=jordan",
                "Report this card: GitHub's abuse report for that account",
            )
            refused = await page.evaluate(
                "() => { try { document.body.innerHTML = '<b>x</b>'; return 'allowed'; } catch (e) { return e.name; } }"
            )
            check(refused == "TypeError", f"Trusted Types refuse an HTML string ({refused})")
            meta = await page.locator("meta[http-equiv=Content-Security-Policy]").get_attribute(
                "content"
            )
            check(
                "connect-src https://api.github.com;" in meta
                and "require-trusted-types-for 'script'" in meta
                and "script-src 'self';" in meta,
                "its CSP: its own scripts, GitHub's API, Trusted Types",
            )
            check(
                await page.locator("meta[name=referrer]").get_attribute("content") == "no-referrer",
                "no referrer",
            )
            for url in [
                await a.get_attribute("href") for a in await page.locator("#state a").all()
            ]:
                check(url.startswith("https://"), f"a link in the card goes to https ({url})")
            order = await keyboard(page)
            want_order = [
                "api#418",
                "the runbook",
                "Copy",
                "★ Star it on GitHub",
                "Report this card",
            ]
            positions = [
                next((i for i, text in enumerate(order) if want in text), -1) for want in want_order
            ]
            check(
                all(p >= 0 for p in positions) and positions == sorted(positions),
                f"keyboard order: the card's links, Copy, Star, Report ({order})",
            )
        elif name == "hostile":
            card = page.locator(".card.shared")
            check(
                await card.locator("img, script, iframe, b").count() == 0,
                "a hostile card's HTML is drawn as text",
            )
            check(
                "<img src=x" in await card.locator(".head .title").inner_text(),
                "its title, as written",
            )
            hrefs = [await a.get_attribute("href") for a in await card.locator("a").all()]
            check(
                all(href.startswith(("https://", "http://")) for href in hrefs),
                f"no javascript: link ({hrefs})",
            )
            host = await card.locator(".link-host").all_inner_texts()
            check(
                any("evil.example" in text for text in host),
                "a link shows its host when its words differ",
            )
            check(not alerts, f"nothing ran ({alerts})")
        else:
            check(
                await page.locator("#decrypted").is_hidden(),
                f"{name}: no Decrypted in this browser",
            )
            check(
                "like the card here" not in await page.locator(".v-made").inner_text(),
                f"{name}: Made with without “like the card here”",
            )
        if name == "limited":
            words = await page.locator(".v-notice p").inner_text()
            check("Try again at 2:37 AM" in words, f"says when to try again ({words})")
        if name == "offline":
            # The same link as valid's: once GitHub answers, Try again opens it.
            await page.unroute("https://api.github.com/gists/**")
            await page.route(
                "https://api.github.com/gists/**",
                lambda route: route.fulfill(
                    status=200,
                    body=json.dumps(cases["valid"][2]),
                    headers={
                        "access-control-allow-origin": "*",
                        "content-type": "application/json",
                    },
                ),
            )
            await page.get_by_role("button", name="Try again").click()
            await page.wait_for_selector("body[data-state=card]", state="attached")
            check(True, "offline: Try again opens it once GitHub answers")
        await page.close()
    # Framed: never drawn inside another page.
    page = await context.new_page()
    await page.route(
        f"{base}/framed.html",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body=f'<iframe src="{base}/s/{cases["valid"][0][cases["valid"][0].index("#") :]}" width="900" height="600"></iframe>',
        ),
    )
    await page.route(
        "https://api.github.com/gists/**",
        lambda route: route.fulfill(
            status=200,
            body=json.dumps(cases["valid"][2]),
            headers={"access-control-allow-origin": "*", "content-type": "application/json"},
        ),
    )
    await page.goto(f"{base}/framed.html")
    frame = page.frames[1]
    await frame.wait_for_selector("body[data-state]", state="attached")
    check(
        await frame.evaluate("document.body.dataset.state") == "framed",
        "inside another page's frame: Open this card in its own tab",
    )
    await page.close()
    await context.close()


async def keyboard(page) -> list[str]:
    await page.locator("body").focus()
    seen = []
    for _ in range(14):
        await page.keyboard.press("Tab")
        text = await page.evaluate(
            "(document.activeElement.textContent || document.activeElement.getAttribute('aria-label') || '').trim()"
        )
        if text in seen:
            break
        seen.append(text)
    return seen


async def crypto(browser, base: str):
    print("format: WebCrypto <-> Python cryptography")
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(f"{base}/s/#")
    module = f"{base}/s/sharecrypto.js"
    same = await page.evaluate(
        """async ([module, v]) => { const f = await import(module); return (await f.sealWith(v.payload, v.owner_id, new Uint8Array(v.key_bytes), new Uint8Array(v.nonce_bytes))).blob; }""",
        [module, VECTOR],
    )
    check(same == VECTOR["blob"], "WebCrypto makes the known-answer vector's bytes exactly")
    blob, key = sharing.seal_with(
        VECTOR["payload"],
        VECTOR["owner_id"],
        bytes(VECTOR["key_bytes"]),
        bytes(VECTOR["nonce_bytes"]),
    )
    check(blob == same, "Python makes the same bytes from the same key and nonce")
    shared = payload()
    sealed = await page.evaluate(
        """async ([module, p, owner]) => (await import(module)).seal(p, owner)""",
        [module, shared, OWNER["id"]],
    )
    check(
        sharing.open_payload(sealed["blob"], sealed["key"], OWNER["id"]) == shared,
        "WebCrypto seals, Python opens it unchanged",
    )
    blob, key = sharing.seal(shared, OWNER["id"])
    raw = bytearray(sharing.unb64u(blob))
    flipped, newer = bytearray(raw), bytearray(raw)
    flipped[-1] ^= 1
    newer[0] = 2
    results = await page.evaluate(
        """async ([module, blob, key, owner, flipped, newer]) => {
          const f = await import(module);
          const outcome = async (fn) => { try { return JSON.stringify(await fn()); } catch (e) { return e.constructor.name; } };
          return {
            opened: await outcome(() => f.open(blob, key, owner)),
            flipped: await outcome(() => f.open(flipped, key, owner)),
            otherOwner: await outcome(() => f.open(blob, key, owner + 1)),
            newer: await outcome(() => f.open(newer, key, owner)),
            cut: await outcome(() => f.open(blob.slice(0, blob.length / 2), key, owner)),
            wrongKey: await outcome(() => f.open(blob, key.slice(0, -2) + (key.endsWith("AA") ? "BA" : "AA"), owner)),
            naive: f.expired({ expires_at: "2099-01-01T00:00:00" }),
            past: f.expired({ expires_at: "2020-01-01T00:00:00Z" }),
            none: f.expired({}),
            future: f.expired({ expires_at: "2099-01-01T00:00:00Z" }),
            link: f.parseLink(`https://evanl666.github.io/chiefofstaff/s/#1.${"a".repeat(32)}.${key}.tpaaoc`),
          };
        }""",
        [module, blob, key, OWNER["id"], sharing.b64u(bytes(flipped)), sharing.b64u(bytes(newer))],
    )  # fmt: skip
    check(
        json.loads(results["opened"]) == sharing.clean_payload(shared),
        "Python seals, WebCrypto opens it unchanged",
    )
    for name, want in (
        ("flipped", "DamagedError"),
        ("otherOwner", "DamagedError"),
        ("newer", "NewerError"),
        ("cut", "DamagedError"),
        ("wrongKey", "DamagedError"),
    ):
        check(results[name] == want, f"WebCrypto refuses {name} as {want} ({results[name]})")
    check(
        results["naive"] and results["past"] and results["none"] and not results["future"],
        "expiry: past, naive or none refused",
    )
    py = sharing.parse_link(f"https://evanl666.github.io/chiefofstaff/s/#1.{'a' * 32}.{key}.tpaaoc")
    check(
        results["link"]["gistId"] == py["gist_id"]
        and results["link"]["key"] == py["key"]
        and datetime.fromisoformat(results["link"]["expiresHint"])
        == datetime.fromisoformat(py["expires_hint"]),
        "a link parses the same in both",
    )
    await context.close()


async def screenshots(browser, base: str):
    print("screenshots")
    SHOTS.mkdir(parents=True, exist_ok=True)
    cases = fixtures()
    for scheme in ("light", "dark"):
        for width in (1440, 800, 390):
            context = await browser.new_context(
                viewport={"width": width, "height": 900 if width > 400 else 844},
                color_scheme=scheme,
                device_scale_factor=2 if width == 390 else 1,
            )
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
            for name in ("valid", "expired", "revoked", "damaged"):
                page, _, _ = await viewer(context, base, cases[name])
                overflow = await page.evaluate(
                    "document.documentElement.scrollWidth > innerWidth + 1"
                )
                check(not overflow, f"{name} {width} {scheme}: no sideways scrolling")
                await page.screenshot(
                    path=str(SHOTS / f"viewer-{name}-{width}-{scheme}.png"),
                    full_page=True,
                    animations="disabled",
                )
                await page.close()
            await context.close()
    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    for name in ("limited", "incomplete", "offline", "newer"):
        page, _, _ = await viewer(context, base, cases[name])
        await page.screenshot(
            path=str(SHOTS / f"viewer-{name}-1440-light.png"), full_page=True, animations="disabled"
        )
        await page.close()
    await context.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--site", type=Path, help="a site built already (else built here)")
    args = parser.parse_args()
    site = args.site or build(ROOT / "browser-shots" / "viewer-site")
    with serve(site) as base:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            await crypto(browser, base)
            await states(browser, base)
            await screenshots(browser, base)
            await browser.close()
    print(f"\n{len(FAILURES)} failed" if FAILURES else "\nVIEWER CHECK PASSED")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
