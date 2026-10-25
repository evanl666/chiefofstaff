"""Real CLI + MCP smoke test with synthetic Slack. Consumes Copilot model usage."""

import asyncio
import secrets
import socket
import tempfile
from pathlib import Path

import httpx
import uvicorn

from work_buddy.models import Settings
from work_buddy.web import create_app

PROJECT = Path(__file__).resolve().parents[1]


class SyntheticSlack:
    def __init__(self):
        self.posts = []
        self.reads = 0

    async def conversations_replies(self, **kwargs):
        self.reads += 1
        return {
            "messages": [
                {"user": "UTEAM", "text": "Compatibility checks are still pending.", "ts": "100"}
            ]
            + [
                {"user": "UBOT", "text": post["text"], "ts": str(101 + index)}
                for index, post in enumerate(self.posts)
                if post["channel"] == "C_SMOKE"
            ],
            "has_more": False,
        }

    async def conversations_history(self, **kwargs):
        return await self.conversations_replies(**kwargs)

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": str(100 + len(self.posts))}


async def main():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="work-buddy-native-smoke-") as directory:
        token = secrets.token_urlsafe(32)
        app = create_app(Path(directory), token, port)
        store = app.state.store
        store.save_settings(Settings(project_path=str(PROJECT)))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(20):
                while not server.started:  # noqa: ASYNC110 -- observe Uvicorn startup
                    await asyncio.sleep(0.05)
            slack = SyntheticSlack()
            app.state.engine.slack.client = slack
            settings = store.settings()
            settings.slack_enabled = True
            settings.slack_owner = "UOWNER"
            settings.slack_channels = ["C_SMOKE"]
            store.save_settings(settings)

            async with httpx.AsyncClient(
                base_url=base,
                headers={"Origin": base},
                timeout=30,
            ) as client:
                (await client.post("/api/auth", json={"token": token})).raise_for_status()
                response = await client.post(
                    "/api/daily",
                    json={
                        "text": (
                            "Use actual MCP tools, not a JSON plan. Call spawn_agent twice: "
                            "one agent reads pyproject.toml and reports the Python requirement; "
                            "the other reads the README introduction and reports "
                            "the product purpose. Inspect their results. Then read Slack thread "
                            "C_SMOKE at timestamp 100 and prepare a short, accurate reply using "
                            "slack_reply. These Slack messages are synthetic smoke-test fixtures. "
                            "Do not propose files or memories. After the owner approves the reply, "
                            "report completion without sending another reply."
                        )
                    },
                )
                response.raise_for_status()
                task_id = response.json()["id"]

                async def wait_for(predicate):
                    async with asyncio.timeout(240):
                        while True:
                            task = store.task(task_id)
                            if task["status"] in {"failed", "interrupted"}:
                                raise RuntimeError(task["error"])
                            if predicate(task):
                                return task
                            await asyncio.sleep(0.2)

                await wait_for(
                    lambda task: (
                        task["status"] == "waiting_approval"
                        and all(child["status"] == "completed" for child in store.children(task_id))
                    )
                )
                assert len(store.children(task_id)) == 2
                assert slack.reads > 0
                assert not any(post["channel"] == "C_SMOKE" for post in slack.posts)
                proposal = store.one(
                    """SELECT id FROM approvals
                    WHERE task_id=? AND kind='slack' AND status='pending'""",
                    (task_id,),
                )
                assert proposal
                approved = await client.post(
                    f"/api/approvals/{proposal['id']}",
                    json={
                        "choice": "Approve",
                        "reason": "",
                    },
                )
                approved.raise_for_status()
                await wait_for(lambda task: task["status"] == "completed")
                assert len([post for post in slack.posts if post["channel"] == "C_SMOKE"]) == 1
                tool_names = {
                    item["message"]
                    for item in store.all(
                        "SELECT message FROM events WHERE task_id=? AND kind='tool'",
                        (task_id,),
                    )
                }
                assert {"spawn_agent", "slack_read_thread", "slack_reply"} <= tool_names
                print(
                    "Real Lead used native MCP: 2 CLI agents, Slack read, approved reply, resume."
                )

                response = await client.post(
                    "/api/distill",
                    json={
                        "material": (
                            "For this check, extract just this explicit general preference: "
                            "I prefer replies that separate verified facts from assumptions. "
                            "Persist it through memory_candidate, not just a promise to learn."
                        ),
                    },
                )
                response.raise_for_status()
                task_id = response.json()["id"]
                await wait_for(lambda task: task["status"] == "waiting_approval")
                candidates = store.all(
                    "SELECT id FROM approvals WHERE task_id=? AND kind='memory'",
                    (task_id,),
                )
                assert candidates
                assert not store.all("SELECT * FROM memories")
                for item in candidates:
                    result = await client.post(
                        f"/api/approvals/{item['id']}",
                        json={
                            "choice": "Approve",
                            "reason": "",
                        },
                    )
                    result.raise_for_status()
                assert (Path(directory) / "memory/confirmed.json").exists()
                print(
                    "Real Lead persisted memory candidates via MCP; confirmation produced export."
                )
        finally:
            server.should_exit = True
            await server_task
            listener.close()


if __name__ == "__main__":
    asyncio.run(main())
