"""Bug bash round 8: Settings checks and messages, skills and plugins."""

from pathlib import Path

import pytest
from conftest import settle
from pydantic import ValidationError
from test_web import ORIGIN, app_client, sign_in  # noqa: F401 (app_client: a fixture)

import chiefofstaff.abilities as abilities_module
from chiefofstaff.models import AS_SAVED, Settings, SlackToken

GIT_LOG = (
    "Failed to install plugin: Error: Request plugins.install failed with message: Failed to "
    "fetch GitHub plugin acme/plugin: Command failed: git clone --depth 1 --progress --config "
    "core.longpaths=true https://github.com/acme/plugin.git "
    f"{Path.home()}/.copilot/Library/Caches/copilot/marketplaces/.copilot-clone-tlhWUV/repo\n"
    f"Cloning into '{Path.home()}/.copilot/Library/Caches/copilot/marketplaces/"
    ".copilot-clone-tlhWUV/repo'...\n"
    "remote: Repository not found.\n"
    "fatal: repository 'https://github.com/acme/plugin.git/' not found\n"
)


def problems(values: dict, context: dict | None = None) -> str:
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate(values, context=context)
    return " | ".join(problem["msg"] for problem in raised.value.errors())


def save(http, **changes):
    return http.post("/api/settings", json=changes, headers=ORIGIN)


def test_auto_in_any_case_is_copilots_choice():
    """S2: "Auto" was saved as written, and Copilot refused the model "Auto"."""
    settings = Settings(model="Auto", role_models={"worker": "AUTO", "router": "gpt-5"})
    assert settings.model == "auto"
    assert settings.role_models == {"worker": "auto", "router": "gpt-5"}


async def test_a_skill_with_a_taken_name_is_refused_not_replaced(harness):
    """S3: a second release-notes replaced the first without a word."""
    runtime = await harness.connect()
    first = "---\nname: release-notes\ndescription: Our release notes\n---\nShort.\n"
    await runtime.abilities.add_skill(first)
    with pytest.raises(ValueError, match=r"A skill named release-notes exists: remove it first\."):
        await runtime.abilities.add_skill(first.replace("Short.", "Long."))
    with pytest.raises(ValueError, match=r"You already have this skill: release-notes\."):
        await runtime.abilities.add_skill(first)
    assert (runtime.abilities.skills_dir / "release-notes" / "SKILL.md").read_text() == first


def test_a_skill_with_a_taken_name_says_so_in_settings(app_client):  # noqa: F811
    sign_in(app_client)
    skill = {"content": "---\nname: release-notes\ndescription: Notes\n---\nShort.\n"}
    assert app_client.post("/api/skills", json=skill, headers=ORIGIN).status_code == 200
    skill["content"] = skill["content"].replace("Short.", "Long.")
    again = app_client.post("/api/skills", json=skill, headers=ORIGIN)
    assert again.status_code == 400
    assert again.json()["detail"] == "A skill named release-notes exists: remove it first."


def test_relative_folders_are_refused_not_taken_from_the_servers_folder(tmp_path, monkeypatch):
    """S4: "docs" was saved as the docs folder in whatever folder Chief of Staff started in."""
    (tmp_path / "docs").mkdir()
    monkeypatch.chdir(tmp_path)
    for field in ("code_folder", "projects", "sandbox_writable"):
        value = ["docs"] if field != "code_folder" else "docs"
        said = problems({field: value})
        assert "give the folder as an absolute path, e.g." in said and "(not docs)" in said


async def test_skill_front_matter_is_read_as_yaml_and_the_name_saved_as_listed(harness):
    """S5: a folded description was listed as ">", "My Skill" stayed "My Skill" in the file
    (the worker saw "My-Skill"), and a BOM made a valid file "not front matter"."""
    runtime = await harness.connect()
    abilities = runtime.abilities
    added = await abilities.add_skill(
        "\ufeff---\nname: My Skill\ndescription: >\n  Writes release notes\n  as we like them\n"
        "---\nBody.\n"
    )
    assert added == {"name": "my-skill", "description": "Writes release notes as we like them"}
    assert abilities.view()["skills"] == [added]
    saved = (abilities.skills_dir / "my-skill" / "SKILL.md").read_text()
    assert saved.startswith("---\nname: my-skill\ndescription: >\n"), saved
    quoted = await abilities.add_skill(
        '---\nname: \'notes\'\ndescription: "Say \\"hi\\" first"\n---\n'
    )
    assert quoted["description"] == 'Say "hi" first'


async def test_skill_front_matter_errors_say_which_field_and_what_is_allowed(harness):
    runtime = await harness.connect()
    with pytest.raises(ValueError, match="name: ") as raised:
        await runtime.abilities.add_skill("---\nname: my_skill\n---\nBody.\n")
    said = str(raised.value)
    assert "name: use lowercase letters, digits and hyphens, like my-skill (not my_skill)" in said
    assert "description: missing" in said
    with pytest.raises(ValueError, match="description: empty"):
        await runtime.abilities.add_skill("---\nname: notes\ndescription: >\n---\nBody.\n")


def test_settings_errors_say_what_is_allowed_not_a_pattern(app_client, tmp_path):  # noqa: F811
    """S6: "String should match pattern '^[a-z0-9_+'-]*$'" and "Dictionary should have at
    most 10 items after validation"."""
    sign_in(app_client)
    cases = {
        "slack_delegate_emoji": ("👍", "Delegate emoji: the emoji's name as Slack writes it"),
        "plugins": (
            ["git@github.com:acme/plugin.git"],
            "use its https URL, https://github.com/acme/plugin.git",
        ),
        "github_repos": (["acme"], "GitHub repository acme: write it as owner/name"),
        "mcp_servers": (
            {"my.server": {"command": "probe"}},
            "MCP server name my.server: use letters, digits, - and _",
        ),
    }
    for field, (value, expected) in cases.items():
        refused = save(app_client, **{field: value})
        assert refused.status_code == 422, field
        [problem] = refused.json()["detail"]
        assert problem["type"] == "value_error" and expected in problem["msg"], problem
    servers = {f"s{n}": {"command": "probe"} for n in range(11)}
    [problem] = save(app_client, mcp_servers=servers).json()["detail"]
    assert problem["msg"] == "At most 10 MCP servers.", problem
    saved = save(app_client, github_repos=["https://github.com/acme/app.git"])
    assert saved.status_code == 200 and saved.json()["settings"]["github_repos"] == ["acme/app"]


def test_the_same_plugin_twice_is_kept_once():
    """S7: "3 plugins" in the summary, 2 listed."""
    settings = Settings(plugins=["acme/tools", "acme/notes", "acme/tools"])
    assert settings.plugins == ["acme/tools", "acme/notes"]


async def test_a_failed_plugin_install_says_why_in_a_line(harness):
    """S8: the last 500 characters of git's log, from mid-word ("thub.com/acme/…")."""
    harness.store.save_settings(Settings(plugins=["acme/plugin"]))
    runtime = await harness.connect()

    async def install(source):
        raise RuntimeError(GIT_LOG)

    runtime.abilities.install = install
    await runtime.abilities.sync()
    await settle(runtime)
    error = runtime.abilities.plugin_status()["acme/plugin"]["error"]
    assert error == (
        "Repository not found. fatal: repository 'https://github.com/acme/plugin.git/' not found"
    )
    long = abilities_module.install_error(
        f"fatal: could not read {Path.home()}/a/b/c/d/e/f " + "word " * 80
    )
    assert long.startswith("fatal: could not read ~/…/e/f word") and long.endswith("word…")
    assert len(long) <= 300


def test_a_slack_token_with_a_space_inside_says_so():
    """S9b: "xoxp-1234 5678" was "not a Slack user token … Bot tokens (xoxb-)…"."""
    # Made-up tokens, low in entropy: a secret scanner takes a random-looking one for a key.
    fake = "xoxp-" + "test-test"
    assert SlackToken.model_validate({"token": f"  {fake}\n"}).token == fake
    with pytest.raises(ValidationError, match=r"A Slack token has no spaces: paste it again\."):
        SlackToken.model_validate({"token": "xoxp-" + "test test"})


def test_task_folders_inside_a_repository_are_refused_when_chosen(git_repo):
    """S12: the code folder refuses a repository; task folders took one."""
    said = problems({"scratch_directory": str(git_repo / "tasks")})
    assert f"is in the repository {git_repo.resolve()}" in said
    kept = Settings.model_validate({"scratch_directory": str(git_repo / "tasks")}, context=AS_SAVED)
    assert kept.scratch_directory == str((git_repo / "tasks").resolve()), "saved earlier: kept"


def test_a_code_folder_holding_the_data_directory_is_refused(app_client, tmp_path):  # noqa: F811
    """S12: the first-run card refused it; Settings saved it."""
    sign_in(app_client)
    refused = save(app_client, code_folder=str(tmp_path))
    assert refused.status_code == 400
    assert "does not contain Chief of Staff's own data" in refused.json()["detail"]


def test_also_writable_folders_must_exist_and_task_folders_are_named_so(app_client, tmp_path):  # noqa: F811
    """S14: a missing folder was accepted as writable; errors said "scratch folder"."""
    sign_in(app_client)
    missing = tmp_path / "journal"
    refused = save(app_client, sandbox_writable=[str(missing)])
    assert refused.status_code == 422
    assert refused.json()["detail"][0]["msg"] == f"Value error, Folder not found: {missing}"
    missing.mkdir()
    assert save(app_client, sandbox_writable=[str(missing)]).status_code == 200
    missing.rmdir()
    assert save(app_client, worker_idle_hours=12).status_code == 200, "saved: it stays"
    assert "Task folders: choose a folder" in problems({"scratch_directory": str(Path.home())})
    file = tmp_path / "a-file"
    file.write_text("x")
    refused = save(app_client, scratch_directory=str(file / "inside"))
    assert refused.status_code == 400 and refused.json()["detail"].startswith("Task folders: ")
