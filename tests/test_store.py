import json
import sqlite3

from work_buddy.store import Store


def test_settings_carry_over_from_the_previous_work_buddy(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    legacy = {
        "backend": "copilot",
        "copilot_command": "copilot",
        "model": "auto",
        "project_path": str(project),
        "slack_enabled": True,
        "slack_owner": "U1",
        "github_enabled": True,
        "github_repos": ["Acme/App"],
    }
    with sqlite3.connect(data / "work-buddy.db") as db:
        db.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO settings VALUES (1, ?)", (json.dumps(legacy),))

    settings = Store(data).settings()
    assert settings.projects == [str(project.resolve())]
    assert settings.model == ""
    assert settings.github_enabled and settings.github_repos == ["acme/app"]
    assert settings.slack_enabled is False, "the old bot integration is not the MCP one"
