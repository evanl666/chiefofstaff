"""Live test of the owner's answers reaching the worker that asked (0.1.58, owner report R1 and QA
E153-E157), against real Copilot: the scenarios of the decision-card repeat fix, phase 1, and of
the decision ledger (0.1.59).

D1  The owner answers a decision card while its worker is still in the turn that asked (it runs
    a slow simulation after asking): the answer reaches it in that turn (no turn of it ended in
    between), before its report; it uses it, and no second card follows (R1, R7).
D2  A worker asked again, in other words, what the owner decided for its task: it gets their
    answer back, and no card.
D3  The same worker asks the owner to reconsider, saying why (replaces): the card says "You
    answered this at …" and why it is asked again; Keep on it (in the page) leaves the decision
    in force, and the worker hears it keeps it.
D4  A worker's question survives its progress report (E155): still under Needs you, its task
    waiting; asked again with what it found since, it is the same question, updated.
D5  The owner answers a worker's question while it works on (E156): the answer reaches that
    turn, and no second question or blocked card follows.
D6  A worker blocked on three things only the owner can give; the owner asks the Router a
    separate question, which it relays to that worker on the same task: the blocked ask stays
    under Needs you and its task waits (owner report, 0.1.58).

The decision ledger (0.1.59, QA10 E151, E152, E158, E159):

D7  Two workers of one request ask the owner the same choice, each in its own words: one card
    under Needs you, asked by both (Chief of Staff joins them, not the model).
D8  The Router is asked to put the same question to the owner as a note: it joins the open ask
    (no second item under Needs you), which now says the Router asks too.
D9  The owner answers that one card: every asker hears it and uses it, and one decision is in
    force for both tasks.
D10 A worker reports done with an ask for the owner at its end: the report is held once with
    how to ask; the ask then reaches Needs you (or the report leaves it out), never lost with
    the result.

Uses your signed-in Copilot CLI and consumes premium requests (LIVE_COPILOT_MODEL,
claude-sonnet-5.5 by default); the Copilot sessions it creates are deleted at the end; about 20
minutes. Its data folder has a space in its path, as macOS's own has. Set TMPDIR to a scratch
folder of yours (its temporary folders go there). Ports: LIVE_DECISIONS_PORT (20780) for the
app, the next one for the stand-in Slack. Scenarios by name run alone (D3 runs D2 first).

    TMPDIR=<scratch> uv run --extra dev --with playwright python scripts/live_decisions.py
    TMPDIR=<scratch> uv run --extra dev --with playwright python scripts/live_decisions.py D1 D6
    TMPDIR=<scratch> uv run --extra dev --with playwright python scripts/live_decisions.py D7 D10

D8 and D9 run D7 first.
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import live_e2e
import live_parity  # noqa: F401  (it sets its own port: this one is set after)

live_e2e.PORT = int(os.environ.get("LIVE_DECISIONS_PORT", "20780"))
live_e2e.SLACK_PORT = live_e2e.PORT + 1
live_e2e.BASE = f"http://127.0.0.1:{live_e2e.PORT}"
from live_e2e import App, cleanup, data_dir, log, transcript, wait_for  # noqa: E402
from live_parity import setup  # noqa: E402

MODEL = os.environ.get("LIVE_COPILOT_MODEL", "claude-sonnet-5.5")
FINISHED = {"review", "done"}
# Where a message to a worker stands once its conversation shows it.
READ = {"received", "processed"}


class Run:
    def __init__(self, app: App, data: Path):
        self.app = app
        self.data = data
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append((name, ok, detail))
        log(f"{'PASS' if ok else 'FAIL'} {name}{f': {detail}' if detail else ''}")

    def db(self, sql: str, params: tuple = ()) -> list[dict]:
        """Chief of Staff's own records, read only (decisions and closed cards are in no API)."""
        with sqlite3.connect(f"file:{self.data / 'chiefofstaff.db'}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, params)]

    def cards(self, worker: str) -> list[dict]:
        """Every decision card the worker asked, whatever became of it, oldest first."""
        rows = self.db(
            "SELECT id, status, outcome, target FROM drafts WHERE kind='decision' "
            "AND session_id=? ORDER BY created_at",
            (worker,),
        )
        return [{**row, "target": json.loads(row["target"])} for row in rows]

    def decisions(self, worker: str) -> list[dict]:
        """The decisions in force that answered the worker's cards."""
        return [
            found
            for card in self.cards(worker)
            for found in self.db(
                "SELECT id, text, created_at FROM decisions WHERE status='active' AND source=?",
                (f"card:{card['id']}",),
            )
        ]

    async def say(self, text: str):
        router = (await self.app.get("/api/state"))["router_id"]
        await self.app.post(f"/api/sessions/{router}/messages", {"text": text})

    async def tell(self, worker: str, text: str):
        await self.app.post(f"/api/sessions/{worker}/messages", {"text": text})

    async def worker(self, title: str) -> dict:
        async def found():
            sessions = (await self.app.get("/api/state"))["sessions"]
            return next((s for s in sessions if s.get("title") == title), None)

        return await wait_for(f"the worker {title!r}", found, 300, self.app)

    async def card(self, worker: str, after: int = 0, timeout: float = 600) -> dict:
        """Its next decision card under Needs you (the `after`-th and later)."""

        async def found():
            cards = self.cards(worker)
            return (
                cards[after] if len(cards) > after and cards[after]["status"] == "pending" else None
            )

        return await wait_for(
            f"decision card {after + 1} of {worker[:8]}", found, timeout, self.app
        )

    async def task(self, worker: str, *statuses: str, timeout: float = 900) -> dict:
        async def found():
            tasks = (await self.app.get("/api/state"))["tasks"]
            mine = [t for t in tasks if t["session_id"] == worker]
            return next((t for t in reversed(mine) if t["status"] in statuses), None)

        return await wait_for(f"its task {'/'.join(statuses)}", found, timeout, self.app)

    async def question(self, worker: str) -> dict:
        """Its question to the owner, as its waiting task shows it."""

        async def found():
            tasks = (await self.app.get("/api/state"))["tasks"]
            mine = [t for t in tasks if t["session_id"] == worker and t.get("question")]
            return mine[-1]["question"] if mine else None

        return await wait_for(f"the question of {worker[:8]}", found, 600, self.app)

    async def messages(self, worker: str) -> list[dict]:
        return await self.app.get(f"/api/sessions/{worker}/messages?prompts=1")

    def turn_ends(self, worker: str) -> str:
        """Its last turn's end, as Chief of Staff marks it (the last message then)."""
        found = self.db("SELECT value FROM kv WHERE key=?", (f"idle:{worker}",))
        return found[0]["value"] if found else ""

    async def watch_turn(self, worker: str, answered: str, timeout: float = 900) -> dict:
        """From the owner's answer (a message whose meta answers `answered`) until the worker's
        task is finished: when the answer was first read, when the report came, and whether a
        turn of the worker ended between the answer and its reading (read in a later turn, not
        the one that asked)."""
        ended = self.turn_ends(worker)
        seen = {"read": 0.0, "report": 0.0, "later": False}
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            messages = await self.messages(worker)
            answer = [m for m in messages if (m.get("meta") or {}).get("answers") == answered]
            if answer and answer[-1].get("delivery") in READ and not seen["read"]:
                seen["read"] = time.monotonic() - started
                seen["later"] = self.turn_ends(worker) != ended
            reports = [
                m for m in messages
                if m["kind"] == "report" and m["id"] > (answer[-1]["id"] if answer else 0)
            ]  # fmt: skip
            if reports and not seen["report"]:
                seen["report"] = time.monotonic() - started
            tasks = [t for t in (await self.app.get("/api/state"))["tasks"]
                     if t["session_id"] == worker]  # fmt: skip
            if seen["report"] and tasks and tasks[-1]["status"] in FINISHED:
                return seen
            await asyncio.sleep(2)
        return seen


def written(worker: dict, name: str) -> str:
    path = Path(worker["cwd"]) / name
    return path.read_text().strip() if path.exists() else ""


async def d1_the_answer_reaches_the_turn_that_asked(run: Run):
    app = run.app
    await run.say(
        "Start a worker titled 'Tie Breaker' (no project). Tell it, word for word: Use "
        "ask_decision to ask me which tie-break rule to add, with two options, 'Rule A' (a "
        "tie-break round) and 'Rule B' (points for the first move), recommending Rule A. Do not "
        "end your turn after asking: right after it, run the shell command `sleep 60; echo "
        "'simulation: with rule A or B players still tie 61% of games'` and wait for it. Then, "
        "if my decision has reached you, write the rule I picked into rule.txt in your own "
        "folder and report done, naming it; if it has not, ask me again with ask_decision, from "
        "what the simulation found."
    )
    worker = await run.worker("Tie Breaker")
    card = await run.card(worker["id"])
    await app.post(f"/api/drafts/{card['id']}", {"action": "answer", "text": "Rule A"})
    seen = await run.watch_turn(worker["id"], card["id"])
    run.check(
        "D1 the answer reached the turn that asked, before its report",
        bool(seen["read"]) and seen["read"] <= seen["report"] and not seen["later"],
        f"read after {seen['read']:.0f}s, report after {seen['report']:.0f}s, "
        f"in a later turn: {seen['later']}",
    )
    cards = run.cards(worker["id"])
    plain = [c for c in cards[1:] if not c["target"].get("replaces")]
    run.check(
        "D1 no second card built without the answer",
        not plain,
        f"{len(cards)} card(s): {[c['target']['question'][:60] for c in cards]}",
    )
    found = run.decisions(worker["id"])
    worker = await app.get(f"/api/sessions/{worker['id']}")
    run.check(
        "D1 one decision in force, and the worker used it",
        len(found) == 1 and "A" in written(worker, "rule.txt"),
        f"{[d['text'] for d in found]}; rule.txt {written(worker, 'rule.txt')!r}",
    )


async def d2_asked_again_it_gets_the_answer(run: Run) -> dict:
    app = run.app
    await run.say(
        "Start a worker titled 'Export Format' (no project). Tell it, word for word: Use "
        "ask_decision to ask me whether the export should be CSV or JSON, with those two "
        "options, and end your turn to wait for my decision. Once my decision reaches you, call "
        "ask_decision once more, in other words, asking which file format the export uses (to "
        "double-check it). Then write the format alone into format.txt in your own folder and "
        "report done, quoting what that second ask_decision call answered you."
    )
    worker = await run.worker("Export Format")
    card = await run.card(worker["id"])
    await app.post(f"/api/drafts/{card['id']}", {"action": "answer", "text": "CSV"})
    done = await run.task(worker["id"], *FINISHED)
    worker = await app.get(f"/api/sessions/{worker['id']}")
    cards = run.cards(worker["id"])
    run.check(
        "D2 asked again on a decided task, it got the decision back, no card",
        len(cards) == 1 and written(worker, "format.txt").upper() == "CSV",
        f"{len(cards)} card(s); format.txt {written(worker, 'format.txt')!r}; it reported "
        f"{(done['result'] or '')[:160]!r}",
    )
    return worker


async def d3_a_reconsideration_says_when_and_keep_keeps_it(run: Run, worker: dict):
    from playwright.async_api import async_playwright, expect

    app = run.app
    [decision] = run.decisions(worker["id"])
    await run.tell(
        worker["id"],
        "New evidence: the tool downstream reads JSON only. Ask me to reconsider the export "
        f'format decision: call ask_decision with replaces="{decision["id"]}", and say in why '
        "what changed.",
    )
    card = await run.card(worker["id"], after=1)
    target = card["target"]
    run.check(
        "D3 the reconsideration names the decision and when it was made",
        target.get("replaces") == decision["id"]
        and target.get("previous_at") == decision["created_at"]
        and bool(target.get("why")),
        f"replaces {target.get('replaces')}, why {target.get('why', '')[:100]!r}",
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1280, "height": 1000})
        await page.goto(f"{live_e2e.BASE}/#token={app.launch_token}")
        shown = page.locator(f'#needs .card.draft[data-id="{card["id"]}"]')
        await expect(shown).to_be_visible(timeout=30000)
        line = await shown.locator(".answered").inner_text()
        run.check(
            "D3 the card says when you answered and why it is asked again",
            bool(re.match(r"You answered this at .+ \(“CSV”\)\. Asked again because: ", line)),
            line[:200],
        )
        await shown.get_by_role("button", name="Keep", exact=True).click()
        await expect(shown).to_have_count(0, timeout=30000)
        await browser.close()
    [closed] = [c for c in run.cards(worker["id"]) if c["id"] == card["id"]]
    found = run.decisions(worker["id"])
    run.check(
        "D3 Keep leaves the decision in force",
        closed["outcome"] == "Kept: CSV" and [d["id"] for d in found] == [decision["id"]],
        f"{closed['outcome']!r}; in force {[d['text'] for d in found]}",
    )

    async def heard():
        messages = await run.messages(worker["id"])
        kept = [m for m in messages if "keeps their decision" in m["meta"].get("prompt", "")]
        return kept[-1] if kept and kept[-1].get("delivery") in READ else None

    await wait_for("the worker hears it is kept", heard, 300, app)
    run.check("D3 the worker heard the decision stands", True)


async def d4_progress_keeps_the_question(run: Run):
    app = run.app
    await run.say(
        "Start a worker titled 'Port Choice' (no project). Tell it, word for word: Call "
        "report(status='question') to ask me 'Port 8080 or 9090?' with the options 8080 and "
        "9090. Then, without ending your turn, run the shell command `sleep 20` and call "
        "report(status='progress') with 'Load-testing both ports while I wait.' Then ask the "
        "same question again with report(status='question'), adding that 9090 held 20% more "
        "load, and end your turn."
    )
    worker = await run.worker("Port Choice")
    first = await run.question(worker["id"])

    async def asked_again():
        tasks = (await app.get("/api/state"))["tasks"]
        mine = [t for t in tasks if t["session_id"] == worker["id"] and t.get("question")]
        question = mine[-1]["question"] if mine else None
        return question if question and question.get("updated_at") else None

    again = await wait_for("its question asked again", asked_again, 600, app)
    messages = await run.messages(worker["id"])
    progress = [m for m in messages if m["kind"] == "report"
                and m["meta"].get("status") == "progress"]  # fmt: skip
    task = await run.task(worker["id"], "waiting", timeout=60)
    run.check(
        "D4 its progress report kept the question; asked again, it is that one, updated",
        bool(progress) and again["id"] == first["id"] and again["at"] == first["at"],
        f"{len(progress)} progress report(s); question {again['id']} at {again['at']}, "
        f"updated {again.get('updated_at')}; task {task['status']}",
    )
    await app.post(f"/api/questions/{again['id']}/answer", {"text": "9090"})
    await run.task(worker["id"], *FINISHED)


async def d5_an_answer_while_it_works_and_no_second_question(run: Run):
    app = run.app
    await run.say(
        "Start a worker titled 'Host Choice' (no project). Tell it, word for word: Call "
        "report(status='question') to ask me 'Which host: staging or prod?' with the options "
        "staging and prod. Then, without ending your turn, run the shell command `sleep 45`. "
        "After it, if my answer has reached you, write the host alone into host.txt in your own "
        "folder and report done; if it has not, call report(status='blocked') saying you still "
        "need the host."
    )
    worker = await run.worker("Host Choice")
    question = await run.question(worker["id"])
    await app.post(f"/api/questions/{question['id']}/answer", {"text": "staging"})
    seen = await run.watch_turn(worker["id"], question["id"])
    messages = await run.messages(worker["id"])
    asked = [m for m in messages if m["kind"] == "report"
             and m["meta"].get("status") in {"question", "blocked"}]  # fmt: skip
    worker = await app.get(f"/api/sessions/{worker['id']}")
    run.check(
        "D5 the answer reached the turn that asked; no second question",
        bool(seen["read"]) and not seen["later"] and len(asked) == 1,
        f"read after {seen['read']:.0f}s; {len(asked)} question/blocked report(s)",
    )
    run.check(
        "D5 it used the answer",
        written(worker, "host.txt") == "staging",
        f"host.txt {written(worker, 'host.txt')!r}",
    )


async def d6_a_side_question_leaves_the_blocked_ask_open(run: Run):
    app = run.app
    await run.say(
        "Start a worker titled 'Beta Build' (no project). Tell it, word for word: You cannot go "
        "on without three things only I can give: the Apple ID, the team id and the app icon. "
        "Call report(status='blocked') now, asking me for those three, and end your turn."
    )
    worker = await run.worker("Beta Build")
    asked = await run.question(worker["id"])
    mark = max((m["id"] for m in await run.messages(worker["id"])), default=0)
    await run.say(
        "A separate question, not about those three things: ask the worker of 'Beta Build' "
        "which GitHub account 'Rusty' is in its notes, and tell me what it says."
    )

    async def answered():
        said = [m for m in await run.messages(worker["id"])
                if m["id"] > mark and m["kind"] in {"report", "assistant"}]  # fmt: skip
        return said[-1] if said else None

    said = await wait_for("its answer to the side question", answered, 600, app)
    await asyncio.sleep(15)  # Whatever the Router does with it.
    # The task it is blocked on (the Router may give the side question as a task of its own).
    tasks = [t for t in (await app.get("/api/state"))["tasks"] if t["session_id"] == worker["id"]]
    on = asked["id"].split(":", 1)[1]
    task = next((t for t in tasks if t["id"] == on), tasks[-1])
    run.check(
        "D6 a side question on the same task leaves the blocked ask under Needs you",
        task["status"] == "waiting" and (task.get("question") or {}).get("id") == asked["id"],
        f"task {task['status']}, question {(task.get('question') or {}).get('id')}; it said "
        f"{said['text'][:120]!r}",
    )


def pending_cards(run: Run) -> list[dict]:
    """Every decision card under Needs you, oldest first."""
    rows = run.db(
        "SELECT id, session_id, target FROM drafts WHERE kind='decision' AND status='pending' "
        "ORDER BY created_at"
    )
    return [{**row, "target": json.loads(row["target"])} for row in rows]


async def d7_one_choice_asked_by_two_is_one_card(run: Run) -> tuple[dict, dict, dict]:
    app = run.app
    await run.say(
        "Start two workers in parallel (no project), titled 'Invoice API' and 'Invoice Page', "
        "for one piece of work: invoice totals. Tell each, word for word: Before you write "
        "anything, ask me with ask_decision, in your own words, whether invoice totals round to "
        "cents or to whole units, with the options 'Cents' and 'Whole units', and end your turn "
        "to wait for my decision. Once it reaches you, write the rounding I picked alone into "
        "rounding.txt in your own folder and report done."
    )
    api = await run.worker("Invoice API")
    page = await run.worker("Invoice Page")

    async def both_asked():
        cards = pending_cards(run)
        askers = {a["session_id"] for c in cards for a in c["target"]["askers"]}
        return cards if {api["id"], page["id"]} <= askers else None

    cards = await wait_for("both workers' asks", both_asked, 600, app)
    subjects = (await app.get("/api/state")).get("subjects") or []
    run.check(
        "D7 the same choice asked by two workers is one card",
        len(cards) == 1 and len(cards[0]["target"]["askers"]) == 2,
        f"{len(cards)} card(s): {[c['target']['question'][:60] for c in cards]}; subjects "
        f"{[(s['title'][:40], s['asked_by']) for s in subjects]}",
    )
    return api, page, cards[0]


async def d8_a_router_note_joins_the_open_ask(run: Run, card: dict):
    app = run.app
    before = len((await app.get("/api/state")).get("updates") or [])
    # (Not in its reply to the owner: a note of its own then is refused, its reply says it.)
    await run.say(
        "Start a worker titled 'Invoice Notes' (no project) and tell it, word for word: Write "
        "notes into notes.txt in your own folder and report done. When its report reaches you, "
        "use notify_owner with needs_action=true to ask me, as a note of yours: should invoice "
        "totals round to cents or to whole units?"
    )

    async def router_asked():
        subjects = (await app.get("/api/state")).get("subjects") or []
        found = [s for s in subjects if "the Router" in s["asked_by"]]
        return found[0] if found else None

    try:
        subject = await wait_for("the Router's note joined", router_asked, 600, app)
    except TimeoutError:
        router = (await app.get("/api/state"))["router_id"]
        said = [m["text"][:200] for m in await run.messages(router) if m["kind"] == "assistant"]
        run.check("D8 the Router's note joins the open ask: no second item", False,
                  f"no note of the Router's joined; it said {said[-2:]}")  # fmt: skip
        return
    state = await app.get("/api/state")
    run.check(
        "D8 the Router's note joins the open ask: no second item",
        len(state.get("updates") or []) == before
        and any(f"draft:{card['id']}" in ask["keys"] for ask in subject["asks"]),
        f"{subject['asked_by']}; Router notes under Needs you {len(state.get('updates') or [])}",
    )


async def d9_one_answer_reaches_every_asker(run: Run, api: dict, page: dict, card: dict):
    app = run.app
    await app.post(f"/api/drafts/{card['id']}", {"action": "answer", "text": "Cents"})
    for worker in (api, page):
        await run.task(worker["id"], *FINISHED)
    api = await app.get(f"/api/sessions/{api['id']}")
    page = await app.get(f"/api/sessions/{page['id']}")
    found = run.db(
        "SELECT d.id, d.text, count(l.task_id) AS tasks FROM decisions d "
        "LEFT JOIN task_decisions l ON l.decision_id=d.id WHERE d.status='active' "
        "AND d.text LIKE '%→ Cents' GROUP BY d.id"
    )
    run.check(
        "D9 one answer reached both askers, and both used it",
        written(api, "rounding.txt") == "Cents" and written(page, "rounding.txt") == "Cents",
        f"API {written(api, 'rounding.txt')!r}, page {written(page, 'rounding.txt')!r}",
    )
    run.check(
        "D9 one decision in force, for both tasks",
        len(found) == 1 and found[0]["tasks"] >= 2 and not pending_cards(run),
        f"{found}",
    )


async def d10_a_done_report_with_an_ask_is_held(run: Run):
    app = run.app
    await run.say(
        "Start a worker titled 'Parser Ship' (no project). Tell it, word for word: Write ok "
        "into parser.txt in your own folder. Then call report(status='done') with exactly this "
        "summary: 'Shipped the parser. Want me to open a PR for it, or keep it local?' If the "
        "report is refused, do what the refusal tells you, then end your turn."
    )
    worker = await run.worker("Parser Ship")
    task = await run.task(worker["id"], "review", "done", "waiting")

    async def settled():
        state = await app.get("/api/state")
        mine = [t for t in state["tasks"] if t["session_id"] == worker["id"]]
        asked = [q for q in state.get("questions") or [] if q["session_id"] == worker["id"]]
        asked += [t["question"] for t in mine if t.get("question")]
        cards = [c for c in pending_cards(run) if c["session_id"] == worker["id"]]
        if not mine or mine[-1]["status"] == "running":
            return None
        return {"task": mine[-1], "asked": asked, "cards": cards}

    found = await wait_for("its report settled", settled, 600, app)
    held = run.db("SELECT value FROM kv WHERE key=?", (f"doneask:{task['id']}",))
    result = found["task"].get("result") or ""
    kept = bool(found["asked"] or found["cards"])
    run.check(
        "D10 the done report with an ask was held once",
        bool(held),
        f"held {held[0]['value'] if held else 'no'}",
    )
    run.check(
        "D10 the ask reached Needs you, or the report left it out",
        kept or "PR" not in result,
        f"task {found['task']['status']}; asked {[q.get('text', '')[:60] for q in found['asked']]};"
        f" cards {len(found['cards'])}; result {result[:120]!r}",
    )


async def main():
    wanted = {name.upper() for name in sys.argv[1:]}

    def runs(name: str) -> bool:
        return not wanted or name in wanted

    with tempfile.TemporaryDirectory(prefix="cos-decisions-") as temporary:
        root = Path(temporary).resolve()
        home, project, _ = setup(root)
        data = data_dir(root)
        data.parent.mkdir(parents=True, exist_ok=True)
        app = App(data, root / "log", {"COPILOT_HOME": str(home)})
        await app.start()
        run = Run(app, data)
        try:
            await app.post("/api/settings", {
                "projects": [str(project)], "model": MODEL, "max_workers": 3,
                "mac_notifications": False, "digest_time": ""})  # fmt: skip
            if runs("D1"):
                await d1_the_answer_reaches_the_turn_that_asked(run)
            if runs("D2") or runs("D3"):
                worker = await d2_asked_again_it_gets_the_answer(run)
                if runs("D3"):
                    await d3_a_reconsideration_says_when_and_keep_keeps_it(run, worker)
            if runs("D4"):
                await d4_progress_keeps_the_question(run)
            if runs("D5"):
                await d5_an_answer_while_it_works_and_no_second_question(run)
            if runs("D6"):
                await d6_a_side_question_leaves_the_blocked_ask_open(run)
            if runs("D7") or runs("D8") or runs("D9"):
                api, page, card = await d7_one_choice_asked_by_two_is_one_card(run)
                if runs("D8"):
                    await d8_a_router_note_joins_the_open_ask(run, card)
                if runs("D9"):
                    await d9_one_answer_reaches_every_asker(run, api, page, card)
            if runs("D10"):
                await d10_a_done_report_with_an_ask_is_held(run)
        except BaseException:
            router = (await app.get("/api/state"))["router_id"]
            print(transcript(await app.get(f"/api/sessions/{router}/messages")))
            raise
        finally:
            await app.stop()
            await cleanup(data)
    results = run.results
    print("\n================ DECISIONS ================")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    failed = [item for item in results if not item[1]]
    print(f"{len(results) - len(failed)}/{len(results)} as expected")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
