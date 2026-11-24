"""Autopilot as the page shows it (scripts/ui_edges.py, ui_quality.py): its line ticked, an
earlier run ended with its card, and a run on now with what it did and set aside, made through
the record and the cards the page reads (no model, no verification run)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import call_tool, settle


async def spawned(runtime, title: str) -> dict:
    router = runtime.router_id()
    result = await call_tool(
        runtime, "spawn_worker", router, title=title, instructions=title, why_new="test"
    )
    await settle(runtime)
    return {**json.loads(result.text_result_for_llm), "title": title}


async def ticked(runtime, **changes):
    old = runtime.store.settings()
    new = old.model_copy(update={"autopilot": True, **changes})
    runtime.store.save_settings(new)
    await runtime.settings_changed(old, new)
    await settle(runtime)


def decision_card(runtime, worker: dict, question: str, options: list[str]) -> dict:
    target = {
        "question": question,
        "why": "",
        "options": [{"choice": option, "effect": ""} for option in options],
        "recommendation": "",
        "askers": [
            {
                "session_id": worker["session_id"],
                "task_id": worker["task_id"],
                "title": worker.get("title", ""),
            }
        ],
        "precedents": [],
    }
    card = runtime.store.create_draft(worker["session_id"], "decision", target, question)
    runtime.add_message(worker["session_id"], "draft", question,
                        {"draft_id": card["id"], "label": "Decision"})  # fmt: skip
    return card


def verification(commit: str) -> dict:
    return {
        "repository": "acme/api", "branch": "chief/fix-login", "base": "origin/main",
        "commit": commit, "tree": "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "tree_after": True,
        "integrity": "eligible",
        "steps": [{"command": "uv run pytest -q", "exit": 0, "seconds": 3.1, "sandboxed": True,
                   "output": "........\n8 passed in 0.29s\n", "tests": []}],
    }  # fmt: skip


async def seed_page(runtime, *, current: bool = True) -> dict:
    """Its line ticked; an earlier run, ended by the owner's Stop, with its card in the Router's
    chat; and (`current`) a run on now: 4 done, 1 set aside under Needs you."""
    store = runtime.store
    autopilot = runtime.autopilot
    book = autopilot.book
    await ticked(runtime)
    login = await spawned(runtime, "Fix the flaky login test")
    client = await spawned(runtime, "Write the client")
    sessions = await spawned(runtime, "Session store")
    owner = runtime.add_message(sessions["session_id"], "owner", "Postgres for v1, please.",
                                {"ingress": "page"})  # fmt: skip
    moment = datetime.now().astimezone()
    earlier = await autopilot.start(moment + timedelta(hours=2), by="home")
    run = earlier["run"]
    commit = "8e41c07d2f5b6a7c8d9e0f1a2b3c4d5e6f708192"
    accepted = book.append(
        run, "accepted", "Accepted “Fix the flaky login test”: its checks pass at 8e41c07",
        subject=f"task:{login['task_id']}", task_id=login["task_id"],
        why={"rule": "verified", "verification": "Chief's own run, offline"},
        evidence=verification(commit), payload={"commit": commit},
    )  # fmt: skip
    store.execute("UPDATE tasks SET status='done', ended_at=? WHERE id=?",
                  (datetime.now(UTC).isoformat(), login["task_id"]))  # fmt: skip
    store.set(f"autopilot:accepted:{login['task_id']}", accepted["id"])
    released = book.append(
        run, "released", "Started “Write the client” (it waited on that result)",
        of=accepted["id"], subject=f"task:{client['task_id']}", task_id=client["task_id"],
        why={"rule": "dependency"},
    )  # fmt: skip
    book.append(run, "after", "then: committed 2 times, ran tests 3 times (passed)",
                of=released["id"], subject=f"session:{client['session_id']}",
                why={"settings": "asked first"})  # fmt: skip
    card = decision_card(runtime, sessions, "Which store for sessions?", ["Postgres", "Redis"])
    decided = book.append(
        run, "decided", "“Which store for sessions?” → Postgres",
        subject=f"card:{card['id']}", task_id=sessions["task_id"],
        why={"rule": "owner-basis", "basis": [{"id": f"message:{owner['id']}",
                                               "quote": "Postgres for v1"}],
             "judge": "The owner chose Postgres for v1 in this task's chat."},
        payload={"question": "Which store for sessions?", "choice": "Postgres"},
    )  # fmt: skip
    book.decide(run_id=run, entry_id=decided["id"], card_id=card["id"],
                question="Which store for sessions?",
                options=[{"choice": "Postgres"}, {"choice": "Redis"}], choice="Postgres",
                basis=[{"id": f"message:{owner['id']}", "quote": "Postgres for v1"}],
                askers=card["target"]["askers"])  # fmt: skip
    store.update_draft(card["id"], status="answered", outcome="Decided by Autopilot: Postgres")
    book.append(run, "after",
                "then: committed 3 times, ran tests 14 times (passed), pushed chief/sessions-store",
                of=decided["id"], subject=f"session:{sessions['session_id']}",
                why={"settings": "asked first"})  # fmt: skip
    runtime.add_message(
        sessions["session_id"], "notice",
        "Autopilot decided: Postgres, because you said “Postgres for v1” (Mon 10:02 AM).",
        {"autopilot": {"entry": decided["id"], "run": run}, "task_id": sessions["task_id"]},
    )  # fmt: skip
    migration = decision_card(runtime, client, "Push the migration to main?", ["Yes", "Not yet"])
    parked = book.append(
        run, "parked", "Set aside “Push the migration to main?”: only you can allow that",
        state="parked", subject=f"card:{migration['id']}", task_id=client["task_id"],
        why={"rule": "parked", "reason": "it asks for a permission only you can give"},
    )  # fmt: skip
    store.set(f"autopilot:setaside:{migration['id']}",
              json.dumps({"why": "it asks for a permission only you can give",
                          "entry": parked["id"]}))  # fmt: skip
    autopilot.spend(12)
    await autopilot.stop(by="you", why="you pressed Stop")
    await settle(runtime)
    found = {"earlier": run, "login": login, "client": client, "sessions": sessions,
             "migration": migration}  # fmt: skip
    if not current:
        return found
    now = await autopilot.start(moment + timedelta(hours=3), by="home")
    for title, kind in (
        ("Answered “Which port for the dev server?”: 8080", "answered"),
        ("Accepted “Rate-limit login attempts”: its checks pass at 1c9a77e", "accepted"),
        ("Started “Document the limits” (it waited on that result)", "released"),
        ("Asked “Compare feature-flag services” where it stands, once more", "nudged"),
    ):
        book.append(now["run"], kind, title, why={"rule": "owner-basis"})
    await settle(runtime)
    found["run"] = now["run"]
    return found
