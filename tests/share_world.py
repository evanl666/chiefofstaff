"""A GitHub for share links' tests: gh as shares.py calls it (accounts signed in, their tokens,
`api user`, gists made, listed and deleted) and GitHub's API as the viewer and an import read a
gist (an httpx transport), with the faults a test asks for. And a finished card to share, and a
worker's task finished twice with its reports in its chat and on Home."""

import json
import secrets
from pathlib import Path
from types import SimpleNamespace

import httpx

from chiefofstaff.sources.github import GitHubError

# A made-up token, put together so that no secret scanner takes this file for a leak.
TOKEN = "gh" + "p_" + "EXAMPLEexampleEXAMPLEexample01234567"
RESULT = (
    "Fixed the flaky login test; all tests pass.\n\n"
    "- Cause: token expiry read the real clock\n"
    "- Reproduced it first on staging, http://10.20.4.17:8080/login, with "
    f"`API_TOKEN={TOKEN}`\n"
    "- Maya (maya.chen@example.com) confirmed the timeouts\n"
    "- `uv run pytest -q`: 8 passed"
)
TIMED_OUT = RuntimeError("GitHub request timed out.")
SLOW = GitHubError(
    "GitHub request failed: gh: You have exceeded a secondary rate limit. (HTTP 403)",
    '{"message": "You have exceeded a secondary rate limit."}',
)
NO_SCOPE = GitHubError(
    "GitHub request failed: gh: Not Found (HTTP 404)", '{"message": "Not Found"}'
)


class FakeGitHub:
    def __init__(self, accounts=(("jordan", 4242001),)):
        self.accounts = {
            login: {"id": user_id, "token": f"tok-{login}", "active": index == 0, "in": True}
            for index, (login, user_id) in enumerate(accounts)
        }
        self.gists: dict[str, dict] = {}
        self.calls: list[tuple[tuple, str]] = []
        self.create_fault: Exception | None = None
        self.created_anyway = False  # The fault comes after the gist was made (no answer).
        self.delete_fault: Exception | None = None
        self.private: set[str] = set()
        self.reads: list[str] = []
        self.read_fault: Exception | int | None = None

    def sign_out(self, login: str):
        self.accounts[login]["in"] = False

    def by_token(self, token: str) -> tuple[str, dict]:
        return next((login, a) for login, a in self.accounts.items() if a["token"] == token)

    def made(self, login: str, user_id: int, blob: str, description: str = "") -> str:
        gist_id = secrets.token_hex(16)
        self.gists[gist_id] = {
            "id": gist_id,
            "public": False,
            "description": description,
            "owner": {"login": login, "id": user_id},
            "files": {"share.txt": {"filename": "share.txt", "truncated": False, "content": blob}},
        }
        return gist_id

    async def __call__(self, *arguments, payload=None, token=""):
        self.calls.append((arguments, token))
        args = list(arguments)
        if args[:2] == ["auth", "status"]:
            hosts = [
                {"login": login, "active": a["active"], "state": "success"}
                for login, a in self.accounts.items()
                if a["in"]
            ]
            return json.dumps({"hosts": {"github.com": hosts}})
        if args[:2] == ["auth", "token"]:
            account = self.accounts.get(args[-1])
            if not account or not account["in"]:
                raise RuntimeError(f"GitHub request failed: no oauth token found for {args[-1]}")
            return account["token"] + "\n"
        if args[:2] == ["api", "user"]:
            login, account = self.by_token(token)
            return json.dumps({"login": login, "id": account["id"]})
        if args[:2] == ["api", "gists"] and "POST" in args:
            login, account = self.by_token(token)
            if self.create_fault and not self.created_anyway:
                raise self.create_fault
            gist_id = self.made(
                login,
                account["id"],
                payload["files"]["share.txt"]["content"],
                payload["description"],
            )
            assert payload["public"] is False
            if self.create_fault:
                raise self.create_fault
            return json.dumps(self.gists[gist_id])
        if args[:3] == ["api", "--method", "DELETE"]:
            if self.delete_fault:
                raise self.delete_fault
            gist_id = args[3].removeprefix("gists/")
            login, _ = self.by_token(token)
            if gist_id not in self.gists:
                raise GitHubError("GitHub request failed: gh: Not Found (HTTP 404)")
            assert self.gists[gist_id]["owner"]["login"] == login, "deleted as its owner"
            del self.gists[gist_id]
            return ""
        if args[:2] == ["api", "gists?per_page=30"]:
            login, _ = self.by_token(token)
            mine = [g for g in self.gists.values() if g["owner"]["login"] == login]
            return json.dumps([{"id": g["id"], "description": g["description"]} for g in mine])
        if args[:1] == ["api"] and args[1].startswith("repos/"):
            return "true\n" if args[1].removeprefix("repos/") in self.private else "false\n"
        raise RuntimeError(f"unexpected gh call {arguments}")

    def transport(self) -> httpx.MockTransport:
        """GitHub's API as an import reads a gist: no sign-in, past its cache."""

        def answer(request: httpx.Request) -> httpx.Response:
            self.reads.append(str(request.url))
            if isinstance(self.read_fault, Exception):
                raise self.read_fault
            if self.read_fault == 403:
                return httpx.Response(
                    403,
                    json={"message": "API rate limit exceeded"},
                    headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1795000000"},
                )
            gist = self.gists.get(request.url.path.removeprefix("/gists/"))
            if not gist:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=gist)

        return httpx.MockTransport(answer)


async def finished_task(harness, title="Fix the flaky login test", result=RESULT) -> dict:
    """A worker's task, done and waiting for review, with its tests seen failing then passing."""
    from conftest import call_tool, settle

    runtime = harness.runtime
    spawned = json.loads(
        (
            await call_tool(
                runtime,
                "spawn_worker",
                harness.router_id,
                title=title,
                instructions=title,
                why_new="test",
                done_when=["`uv run pytest -q` passes"],
            )
        ).text_result_for_llm
    )
    session = harness.session(spawned["session_id"])
    for call, code in (("c1", 1), ("c2", 0)):
        session.emit(
            "tool.execution_start",
            tool_name="bash",
            tool_call_id=call,
            arguments={"command": "uv run pytest -q"},
        )
        session.emit(
            "tool.execution_complete",
            tool_call_id=call,
            success=True,
            result=SimpleNamespace(
                content=f"{'8 passed' if not code else '1 failed'}\n<exited with exit code {code}>"
            ),
        )
    await settle(runtime)
    said = await call_tool(
        runtime,
        "report",
        spawned["session_id"],
        status="done",
        summary=result,
        task_id=spawned["task_id"],
        criteria=[
            {
                "criterion": "`uv run pytest -q` passes",
                "result": "met",
                "evidence": "`uv run pytest -q`: 8 passed",
            }
        ],
    )
    assert said.result_type == "success", said.text_result_for_llm
    await settle(runtime)
    task = runtime.tasks.get(spawned["task_id"])
    assert task["status"] == "review"
    return task


SHARE_REPORT_TITLE = "Speed up the nightly export"
SHARE_REPORT_FIRST = "\n".join(
    ["Made the nightly export faster by batching its queries.", ""]
    + [f"- Step {n}: batched the {name} query" for n, name in enumerate(
        ("orders", "refunds", "invoices", "customers", "addresses", "payments", "coupons",
         "shipments", "returns", "notes", "taxes", "currencies", "regions", "audits"), 1)]
    + ["", "The export took 41 minutes; it now takes 9."]
)  # fmt: skip
SHARE_REPORT_SECOND = (
    "Made the nightly export faster with one streaming query instead of batches.\n\n"
    "- The export took 41 minutes; it now takes 4.\n- Memory stays under 300 MB."
)


def share_report_seeded(runtime) -> dict:
    """A worker's task finished twice (its first result replaced by its second), its reports in
    its chat and its line on Home; beside them, a progress report and a report of no task."""
    store = runtime.store
    router = store.get("router_id")
    session = "wsharereport"
    store.create_session(session, "worker", SHARE_REPORT_TITLE, "idle", cwd=str(Path.home()))
    task = runtime.tasks.create(session, SHARE_REPORT_TITLE, SHARE_REPORT_TITLE, status="running")
    done = {"status": "done", "task_id": task["id"]}
    runtime.add_message(session, "report", "Profiling the export's queries first.",
                        {"status": "progress", "task_id": task["id"]})  # fmt: skip
    runtime.add_message(session, "report", SHARE_REPORT_FIRST, done)
    runtime.add_message(session, "report", SHARE_REPORT_SECOND, done)
    runtime.add_message(session, "report", "Checked the disk: 40 GB free, nothing to do.",
                        {"status": "done", "task_id": None})  # fmt: skip
    runtime.add_message(router, "inbound", SHARE_REPORT_SECOND,
                        {"from": session, "title": SHARE_REPORT_TITLE, **done})  # fmt: skip
    runtime.tasks.move(task["id"], "review", result=SHARE_REPORT_SECOND)
    runtime.bus.publish("state")
    return {"session": session, "task": task["id"]}
