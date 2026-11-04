import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
Role = Literal["router", "worker", "slack", "watchdog"]
Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]


def inline_refs(schema: dict) -> dict:
    """A JSON schema with its $defs written in place of each $ref."""
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                extra = {key: value for key, value in node.items() if key != "$ref"}
                return resolve({**defs[node["$ref"].rsplit("/", 1)[-1]], **extra})
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


class NestedArguments(BaseModel):
    """Tool arguments with nested objects: the schema models see has no $ref to follow."""

    @classmethod
    def model_json_schema(cls, *args, **kwargs) -> dict:  # pyright: ignore[reportIncompatibleMethodOverride]
        return inline_refs(super().model_json_schema(*args, **kwargs))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def private_folders(home: Path) -> list[Path]:
    """Never writable by workers: SSH keys, and Copilot's settings, sign-ins, session logs and
    plugin code (hooks run from there outside the sandbox). A plugin's own data folder, such as
    a journal in ~/.copilot/<name>, may be opened."""
    copilot = home / ".copilot"
    return [
        home / ".ssh",
        home / "Library" / "Caches" / "copilot",
        *(
            copilot / name
            for name in (
                "session-state",
                "mcp-oauth-config",
                "installed-plugins",
                "servers",
                "ide",
                "logs",
                "pkg",
                "marketplaces",
            )
        ),
        *copilot.glob("*.json"),
        *copilot.glob("*.db*"),
        *copilot.glob("*.lock"),
    ]


class Settings(StrictModel):
    copilot_command: str = "copilot"
    model: str = Field(default="", max_length=100)
    role_models: dict[Role, Annotated[str, Field(max_length=100)]] = Field(default_factory=dict)
    # Applied when the session's model supports that effort; otherwise the model's default.
    role_efforts: dict[Role, Effort] = Field(default_factory=dict)
    worker_credit_limit: float = Field(default=0, ge=0, le=100000)
    worker_idle_hours: int = Field(default=24, ge=1, le=720)
    # Workers act without permission checks; they still follow their instructions.
    worker_yolo: bool = False
    # Workers' shell commands run in an OS sandbox: they write only to their own folders.
    worker_sandbox: bool = True
    # Folders sandboxed workers may also write to (a Copilot plugin's own folder, say).
    sandbox_writable: list[str] = Field(default_factory=list, max_length=10)
    # Sandboxed workers use the owner's dependency and build caches (Gradle, Maven, npm…), as
    # sessions in a terminal do, instead of downloading and building everything again.
    shared_caches: bool = True
    # Copied task reports end with a short "Orchestrated with Chief of Staff" line.
    report_footer: bool = True
    digest_time: str = Field(default="09:00", pattern=r"^(|([01]\d|2[0-3]):[0-5]\d)$")
    # Where the owner's repositories live (e.g. ~/workspace). Each git repository in it is a
    # project; one a task needs that is not on this machine is cloned into it; each worker works
    # in its own folder in .chief/ inside it.
    code_folder: str = Field(default="", max_length=500)
    # Folders outside the code folder that workers may also work in.
    projects: list[str] = Field(default_factory=list, max_length=30)
    # Tasks without a project get a folder here; empty means inside the data directory.
    scratch_directory: str = Field(default="", max_length=500)
    max_workers: int = Field(default=4, ge=1, le=12)
    paused: bool = False
    mac_notifications: bool = True
    watchdog_enabled: bool = True
    slack_enabled: bool = False
    slack_poll_seconds: int = Field(default=60, ge=20, le=900)
    # Reacting with this emoji to any Slack message hands it to Chief of Staff; empty turns it off.
    slack_delegate_emoji: str = Field(
        default="robot_face", max_length=60, pattern=r"^[a-z0-9_+'-]*$"
    )
    github_enabled: bool = False
    github_repos: list[Repository] = Field(default_factory=list, max_length=10)

    @field_validator("slack_delegate_emoji", mode="before")
    @classmethod
    def emoji_code(cls, value: object) -> object:
        return value.strip().strip(":").lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_paths(self):
        projects = []
        for raw in self.projects:
            path = Path(raw).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"Project is not an existing directory: {raw}")
            if path == Path.home() or path == Path(path.anchor):
                raise ValueError("Choose project directories, not your home or filesystem root.")
            if str(path) not in projects:
                projects.append(str(path))
        self.projects = projects
        writable = []
        for raw in self.sandbox_writable:
            if not raw.strip():
                continue
            path = Path(raw.strip()).expanduser()
            if not path.is_absolute():
                raise ValueError(f"Use an absolute folder for the sandbox: {raw}")
            path = path.resolve()
            home = Path.home().resolve()
            if path == Path(path.anchor) or home.is_relative_to(path):
                raise ValueError("The sandbox cannot open your whole home or a folder above it.")
            if path == home / ".copilot" or any(
                private.is_relative_to(path) or path.is_relative_to(private)
                for private in private_folders(home)
            ):
                raise ValueError(f"The sandbox cannot open {path}: it holds keys or settings.")
            if str(path) not in writable:
                writable.append(str(path))
        self.sandbox_writable = writable
        if self.code_folder:
            path = Path(self.code_folder).expanduser().resolve()
            if not path.is_dir():
                raise ValueError(f"The code folder does not exist: {self.code_folder}")
            if path == Path.home() or path == Path(path.anchor):
                raise ValueError("Choose the folder your repositories are in, like ~/workspace.")
            if (path / ".git").exists():
                # Its task folders would land inside the repository as untracked files.
                raise ValueError(
                    f"{path} is a repository. The code folder is the folder your repositories "
                    "are in, like its parent; add a single repository under Other project folders."
                )
            self.code_folder = str(path)
        if self.scratch_directory:
            scratch = Path(self.scratch_directory).expanduser()
            if not scratch.is_absolute():
                raise ValueError(
                    "The scratch folder must be an absolute path, e.g. ~/ChiefOfStaff."
                )
            scratch = scratch.resolve()
            if scratch == Path.home() or scratch == Path(scratch.anchor):
                raise ValueError("Choose a scratch folder, not your home or filesystem root.")
            self.scratch_directory = str(scratch)
        self.github_repos = list(dict.fromkeys(repo.lower() for repo in self.github_repos))
        if self.github_enabled and not self.github_repos:
            raise ValueError("Choose repositories before enabling review-request monitoring.")
        return self


class OwnerMessage(StrictModel):
    text: str = Field(min_length=1, max_length=20000)
    # From the Router chat, addressed to one worker: it goes to that worker directly.
    to: str = Field(default="", max_length=64)


class FocusRequest(StrictModel):
    # 0 ends focus now.
    minutes: int = Field(ge=0, le=600)


class ProjectPath(StrictModel):
    path: str = Field(min_length=1, max_length=1000)


class TaskNote(StrictModel):
    text: str = Field(default="", max_length=20000)


class DraftDecision(StrictModel):
    # mark_sent: a draft whose sending was interrupted turned out to be delivered.
    action: Literal["send", "reject", "choose", "answer", "mark_sent"]
    # For a form a session showed the owner: field name -> value.
    content: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)
    # For a workspace question: the chosen folder, or "" for no project.
    choice: str = Field(default="", max_length=1000)
    text: str = Field(default="", max_length=12000)
    reason: str = Field(default="", max_length=2000)


class SlackToken(StrictModel):
    token: str = Field(max_length=500)

    @field_validator("token")
    @classmethod
    def user_token(cls, value: str) -> str:
        if not re.fullmatch(r"xoxp-[A-Za-z0-9-]+", value):
            raise ValueError(
                "That is not a Slack user token (xoxp-…). Bot tokens (xoxb-) do not work "
                "with Slack's MCP server."
            )
        return value


class SessionModel(StrictModel):
    model: str = Field(default="", max_length=100)
    effort: Literal["", "none", "low", "medium", "high", "xhigh", "max"] = ""
    context: Literal["", "long_context"] = ""


class SetupNotes(StrictModel):
    text: str = Field(default="", max_length=30000)
    # What the owner saw when they started editing; their changes apply against it.
    initial: str = Field(default="", max_length=30000)


class SessionYolo(StrictModel):
    mode: Literal["", "on", "off"] = ""  # "" follows Settings.


class OpenFolder(StrictModel):
    app: Literal["finder", "vscode"]


class ProfileText(StrictModel):
    text: str = Field(default="", max_length=20000)
