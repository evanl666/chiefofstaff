from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Settings(StrictModel):
    backend: Literal["copilot", "demo"] = "copilot"
    copilot_command: str = "copilot"
    model: str = "auto"
    project_path: str = ""
    profile: str = Field(default="", max_length=12000)
    max_workers: int = Field(default=2, ge=1, le=4)
    timeout_seconds: int = Field(default=300, ge=30, le=1800)
    paused: bool = False
    slack_enabled: bool = False
    slack_owner: str = ""
    slack_channels: list[str] = Field(default_factory=list, max_length=20)
    slack_keyword: str = Field(default="", max_length=100)
    github_enabled: bool = False
    github_repos: list[Repository] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_paths(self):
        self.github_repos = list(dict.fromkeys(repo.lower() for repo in self.github_repos))
        if self.project_path:
            path = Path(self.project_path).expanduser().resolve()
            if not path.is_dir():
                raise ValueError("Project path must be an existing directory.")
            if path == Path.home() or path == Path(path.anchor):
                raise ValueError("Select a project directory, not your home or filesystem root.")
            self.project_path = str(path)
        if self.slack_enabled and not self.slack_owner:
            raise ValueError("Slack requires the owner's Slack member ID.")
        if self.github_enabled and not self.github_repos:
            raise ValueError("Choose repositories before enabling review-request monitoring.")
        return self


class NewTask(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=20000)


class DailyBrief(StrictModel):
    text: str = Field(min_length=1, max_length=20000)


class Assignment(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=12000)


class ProposedFile(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=100000)


class MemoryCandidate(StrictModel):
    text: str = Field(min_length=1, max_length=3000)
    category: Literal["fact", "preference", "workflow"]
    scope: Literal["project", "general"] = "project"
    evidence: str = Field(min_length=1, max_length=3000)


class AgentReply(StrictModel):
    action: Literal["complete", "delegate", "decision", "ignore"]
    message: str = Field(min_length=1, max_length=50000)
    assignments: list[Assignment] = Field(default_factory=list, max_length=4)
    options: list[str] = Field(default_factory=list, max_length=4)
    files: list[ProposedFile] = Field(default_factory=list, max_length=10)
    slack_reply: str = Field(default="", max_length=12000)
    github_review: str = Field(default="", max_length=20000)
    memories: list[MemoryCandidate] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def consistent_action(self):
        if self.action == "delegate" and not self.assignments:
            raise ValueError("Delegation requires assignments.")
        if self.action != "delegate" and self.assignments:
            raise ValueError("Assignments require delegation.")
        if self.action == "decision":
            if not 2 <= len(self.options) <= 4 or len(set(self.options)) != len(self.options):
                raise ValueError("A decision requires 2-4 distinct choices.")
            if any(not choice or len(choice) > 500 for choice in self.options):
                raise ValueError("Choices must contain 1-500 characters.")
        elif self.options:
            raise ValueError("Options require a decision.")
        if self.action != "complete" and (
            self.files or self.slack_reply or self.github_review or self.memories
        ):
            raise ValueError("Only complete results may propose side effects.")
        if len({file.path for file in self.files}) != len(self.files):
            raise ValueError("File paths must be unique.")
        return self


class Resolution(StrictModel):
    choice: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=5000)


class Feedback(StrictModel):
    text: str = Field(min_length=1, max_length=5000)
    remember: bool = False
    scope: str = Field(default="project", pattern="^(project|general)$")


class MemoryInput(StrictModel):
    text: str = Field(min_length=1, max_length=5000)
    scope: str = Field(default="project", pattern="^(project|general)$")


class Distillation(StrictModel):
    material: str = Field(default="", max_length=30000)
    source_files: list[str] = Field(default_factory=list, max_length=20)


class SlackCredentials(StrictModel):
    bot_token: str = Field(pattern=r"^xoxb-.+", max_length=500)
    app_token: str = Field(pattern=r"^xapp-.+", max_length=500)
