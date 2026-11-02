import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
Role = Literal["router", "worker", "slack", "watchdog"]
Effort = Literal["none", "low", "medium", "high", "xhigh", "max"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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
    # Copied task reports end with a short "Orchestrated with Chief of Staff" line.
    report_footer: bool = True
    digest_time: str = Field(default="09:00", pattern=r"^(|([01]\d|2[0-3]):[0-5]\d)$")
    projects: list[str] = Field(default_factory=list, max_length=30)
    # Tasks without a project get a folder here; empty means inside the data directory.
    scratch_directory: str = Field(default="", max_length=500)
    max_workers: int = Field(default=4, ge=1, le=12)
    paused: bool = False
    mac_notifications: bool = True
    watchdog_enabled: bool = True
    slack_enabled: bool = False
    slack_poll_seconds: int = Field(default=60, ge=20, le=900)
    github_enabled: bool = False
    github_repos: list[Repository] = Field(default_factory=list, max_length=10)

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
        if self.scratch_directory:
            scratch = Path(self.scratch_directory).expanduser()
            if not scratch.is_absolute():
                raise ValueError("The scratch folder must be an absolute path, e.g. ~/WorkBuddy.")
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
