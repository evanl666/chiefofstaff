import json
import sqlite3
from contextlib import closing

from chiefofstaff.store import Store


def test_settings_carry_over_from_the_previous_chiefofstaff(tmp_path):
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
    with closing(sqlite3.connect(data / "work-buddy.db")) as db:
        db.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO settings VALUES (1, ?)", (json.dumps(legacy),))
        db.commit()

    settings = Store(data).settings()
    assert settings.projects == [str(project.resolve())]
    assert settings.model == ""
    assert settings.github_enabled and settings.github_repos == ["acme/app"]
    assert settings.slack_enabled is False, "the old bot integration is not the MCP one"


def test_a_database_from_before_the_rename_is_kept(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with closing(sqlite3.connect(data / "work-buddy-sessions.db")) as db:
        db.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO kv VALUES ('router_id', 'r1')")
        db.commit()
    store = Store(data)
    assert store.get("router_id") == "r1"
    assert not (data / "work-buddy-sessions.db").exists()
