r"""A worker given the owner's checkout only to read, on Windows (where its shell is PowerShell,
and Codex wraps its commands in PowerShell or cmd): a command that writes there is refused as
on macOS and Linux, a read goes through, and anything else run there, or naming it, asks; either
way the worker gets a copy of its own to run it in (Runtime.own_copy). CI 44694864500: on
Windows, `echo y >> README.md` and `./run_tests.sh` in that checkout were let through.

The reading runs everywhere (as on Windows: policy.WINDOWS_SHELLS); what only Windows itself
does (its paths for edits) runs there."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Harness
from test_repos import copies, decided, owner_bytes, spawn, to_read

from chiefofstaff import policy
from chiefofstaff.models import Settings
from chiefofstaff.winshell import path_key, plain_path

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows itself")

# The owner's checkout given to read (K in the lines below) and the worker's own folder (O),
# as Windows names them.
KEPT = r"C:\Users\owner\code\app"
OWN = r"C:\Users\owner\code\.chief\task\ws"


@pytest.fixture(autouse=True)
def as_on_windows(monkeypatch):
    monkeypatch.setattr(policy, "WINDOWS_SHELLS", True)


# A PowerShell line run in K or O, and what checkout_write says of it: it writes in K (W), a
# program not known runs there or the line names it (A), or nothing written there ("").
POWERSHELL = [
    ("Get-Content README.md", "K", ""), ("gc README.md | Select-String TODO", "K", ""),
    ("cat README.md; type README.md; dir; ls -Force", "K", ""), ("git status", "K", ""),
    ("git log --oneline -5; git diff HEAD~1 -- src", "K", ""),
    (f"Get-Content {KEPT}\\README.md > {OWN}\\first.txt", "O", ""),
    ("Get-ChildItem -Recurse -Filter *.py | Measure-Object", "K", ""),
    ("Get-ChildItem | Where-Object { $_.Length -gt 1kb } | ForEach-Object { $_.FullName }",
     "K", ""),
    ("Test-Path x; Get-Location; whoami; hostname", "K", ""),
    ("Get-Content x > $null; Get-Content x 2>&1", "K", ""), ("(Get-Content x).Length", "K", ""),
    ("npm --version; python --version", "K", ""), ("Start-Sleep 5", "K", ""),
    ("Remove-Item x -WhatIf", "K", ""), ("# Set-Content x y", "K", ""),
    (f"Copy-Item {KEPT}\\README.md {OWN}\\r.md", "O", ""),
    ("echo y >> README.md", "K", "W"), ("Write-Output y > notes.txt", "K", "W"),
    ("Set-Content notes.txt y", "K", "W"), ("Add-Content -Path notes.txt -Value y", "K", "W"),
    ("Get-Content a | Out-File b", "K", "W"), ("'y' | Tee-Object -FilePath log.txt", "K", "W"),
    ("New-Item -ItemType Directory -Path build", "K", "W"), ("mkdir build; ni x", "K", "W"),
    ("Remove-Item -Recurse -Force src", "K", "W"), ("rm x; del y; rd z", "K", "W"),
    ("Move-Item a b", "K", "W"), ("Rename-Item a -NewName b", "K", "W"),
    (f"Copy-Item {OWN}\\r.md {KEPT}\\README.md", "O", "W"),
    (f"Copy-Item -Path {OWN}\\r.md -Destination {KEPT}", "O", "W"),
    (f"Set-Content {KEPT}\\notes.txt y", "O", "W"), (f"Out-File -FilePath {KEPT}\\x", "O", "W"),
    (f"Expand-Archive {OWN}\\a.zip -DestinationPath {KEPT}", "O", "W"),
    (f"Invoke-WebRequest https://acme.dev/x -OutFile {KEPT}\\x", "O", "W"),
    ("git commit -am x", "K", "W"), ("git checkout -b fix", "K", "W"),
    ("npm install; npm test", "K", "W"), ("pytest; python -m pytest -q", "K", "W"),
    ("uv run pytest", "K", "W"), ("dotnet build", "K", "W"),
    ("cd ..\\app; Set-Content x y", "..\\app-sibling", "W"),
    (f"Set-Location {KEPT}; Remove-Item x", "O", "W"), (f"Push-Location {KEPT}; npm ci", "O", "W"),
    ("if (Test-Path x) { Remove-Item x }", "K", "W"),
    ("function Clean { Remove-Item out -Recurse }; Clean", "K", "W"),
    ("$c = Get-Content a; $c -replace 'x','y' | Set-Content a", "K", "W"),
    ("$x = Remove-Item a", "K", "W"), ("$x=Remove-Item a", "K", "W"),
    ("Invoke-Expression 'Set-Content x y'", "K", "W"),
    ("@'\nline\n'@ | Set-Content x", "K", "W"),
    ("./run_tests.sh", "K", "A"), (".\\build.ps1", "K", "A"), ("& .\\tool.exe", "K", "A"),
    (f"python {KEPT}\\script.py", "O", "A"), ("python script.py", "K", "A"),
    ("node -e \"require('fs').writeFileSync('x', 'y')\"", "K", "A"),
    ("$f = Get-Item README.md; $f.Delete()", "K", "A"),
    ("[IO.File]::WriteAllText('x', 'y')", "K", "A"),
    ("Get-ChildItem | ForEach-Object { Remove-Item $_ }", "K", "A"),
    ("Remove-Item $env:TEMP\\x", "K", "A"), ("& $tool", "K", "A"),
    ("Start-Process notepad x", "K", "A"), (f"Start-Process notepad {KEPT}\\x", "O", "A"),
    ("bash -c 'touch x'", "K", "A"), ("mytool --flag", "K", "A"),
    ("Get-ChildItem *.log | Remove-Item", "K", "A"),
    ("Get-Item x | Set-Content -Value y", "K", "A"),
    (f"$p = @{{Path='{KEPT}\\x'; Value='y'}}; Set-Content @p", "O", "A"),
    (f"Set-Content \u2013Path {KEPT}\\x \u2013Value y", "O", "W"), ("Re`move-Item x", "K", "W"),
    ("[void](Set-Content x y)", "K", "W"), ("$null = Remove-Item x", "K", "W"),
    ("Set-Content ..\\..\\..\\app\\x y", "O", "W"), ("& git commit -m x", "K", "W"),
    ("git.exe commit -m x", "K", "W"), (f"(New-Object IO.StreamWriter '{KEPT}\\x').Write('y')",
                                        "O", "A"),
    ("mytool ..\\..\\..\\app\\x", "O", "A"), ("mytool --out=..\\..\\..\\app\\x", "O", "A"),
    ("python script.py; mytool x", "O", ""), ("Remove-Item $env:TEMP\\x", "O", ""),
]  # fmt: skip

# How a line is spelled: what PowerShell (and cmd) make of it is what is read.
SPELLINGS = [
    ("Set-Content `\n  x y", "K", "W"), ("<# a note #> Set-Content x y", "K", "W"),
    ("Get-Content x # > y", "K", ""), ("Get-Content x\nSet-Content y z", "K", "W"),
    ('@"\nhi $name\n"@ | Set-Content x', "K", "W"), ('@"\n$(Remove-Item x)\n"@', "K", "A"),
    ('Set-Content "$env:TEMP\\x" y', "O", ""), ('Set-Content "$env:TEMP\\x" y', "K", "A"),
    ('Write-Output "$(Remove-Item x)"', "K", "W"), ("$a = @(Get-ChildItem); $a.Count", "K", ""),
    ("Get-Content $(Join-Path . x)", "K", ""), ("Get-Content (x", "K", "A"),
    ("Get-Content (x", "O", ""), ("Get-Content < x", "K", "A"), ("Get-Content x *> log", "K", "W"),
    ("Get-Content x 2> err.txt", "K", "W"), ("Set-Content 'it''s' y", "K", "W"),
    ("Set-Content \u201cnotes.txt\u201d y", "K", "W"), ('Write-Output "a`"b" > x', "K", "W"),
    (f"Push-Location {KEPT}; Pop-Location; Set-Content x y", "O", ""),
    ("Set-Location -Path ..\\..\\..\\app; Set-Content x y", "O", "W"),
    ("cd; Set-Content x y", "O", ""), ("cd $env:TEMP; Set-Content x y", "O", "A"),
    ("D:; Set-Content x y", "O", "A"), (f"cd {KEPT} || Set-Content x y", "O", "W"),
    ("Get-ChildItem | ForEach-Object { cd $_ }; Set-Content x y", "O", "A"),
    (". .\\setup.ps1", "K", "A"), ("exit 0", "K", ""), ("$x = 5", "K", ""),
    ("& { Get-Content x }", "K", ""), ("& { Remove-Item x }", "K", "W"),
    ("powershell -File build.ps1", "K", "A"),
    (f"pwsh -WorkingDirectory {KEPT} -Command Set-Content x y", "O", "W"),
    ("pwsh -Command -", "K", "A"), ("pwsh -EncodedCommand !!!", "K", "A"),
    ("Get-Content HKLM:\\Software\\x; Set-Item Env:FOO bar", "K", ""),
    ("Set-Content C:x y", "K", "W"), ("Set-Content D:x y", "K", "A"),
    ("Set-Content \\x y", "K", ""), ("Set-Content \\\\server\\share\\x y", "K", ""),
    ("cmd", "K", ""), ("cmd /q /k", "K", ""),
]  # fmt: skip

# cmd's lines (Codex in cmd, `cmd /c` in PowerShell).
CMD = [
    ("type README.md & dir /s & findstr /s TODO *.py", "K", ""),
    (f"copy {KEPT}\\README.md {OWN}\\", "O", ""), ("echo y > nul", "K", ""),
    ("echo y > notes.txt", "K", "W"), ("echo y>>notes.txt", "K", "W"), ("del /q x", "K", "W"),
    ("md build && rd /s /q build", "K", "W"), (f"copy x {KEPT}\\", "O", "W"),
    (f"cd /d {KEPT} && del x", "O", "W"), (f"xcopy src {KEPT}\\dst /e /i", "O", "W"),
    (f"robocopy {OWN} {KEPT} /mir", "O", "W"), (f"robocopy {KEPT} {OWN} /mir", "O", ""),
    (f"robocopy {KEPT} {OWN} /move", "O", "W"), ("attrib +r x", "K", "W"), ("npm test", "K", "W"),
    ("if exist x del x", "K", "A"), ("for %f in (*) do type %f", "K", "A"),
    ("call build.cmd", "K", "A"), ("%TOOL% x", "K", "A"), ("^d^e^l x", "K", "W"),
    (f"type nul > {KEPT}\\x", "O", "W"), ('echo "%PATH%" > x', "K", "W"),
    ("@echo off & type x", "K", ""), ("dir | sort", "K", ""), ("type x < y", "K", ""),
    (f"sort /o {KEPT}\\x y", "O", "W"), ("set X=1 & type x", "K", ""),
    (f"takeown /f {KEPT}\\x", "O", "W"), (f"mklink link {OWN}\\x", "K", "W"),
    (f"pushd {KEPT} & del x & popd", "O", "W"), (f"pushd {KEPT} & popd & del x", "O", ""),
]  # fmt: skip


def place(where: str) -> str:
    return {"K": KEPT, "O": OWN}.get(where) or rf"{KEPT}\{where}"


@pytest.mark.parametrize(("line", "where", "said"), POWERSHELL)
def test_a_powershell_line_that_writes_in_the_owners_checkout_is_refused(line, where, said):
    expected = {"W": policy.CHECKOUT_WRITES, "A": policy.CHECKOUT_ASKS, "": ""}[said]
    assert policy.checkout_write(line, place(where), (KEPT,)) == expected, line


@pytest.mark.parametrize(("line", "where", "said"), SPELLINGS)
def test_a_powershell_line_is_read_as_powershell_spells_it(line, where, said):
    expected = {"W": policy.CHECKOUT_WRITES, "A": policy.CHECKOUT_ASKS, "": ""}[said]
    assert policy.checkout_write(line, place(where), (KEPT,)) == expected, line


@pytest.mark.parametrize(("line", "where", "said"), CMD)
def test_a_cmd_line_that_writes_in_the_owners_checkout_is_refused(line, where, said):
    expected = {"W": policy.CHECKOUT_WRITES, "A": policy.CHECKOUT_ASKS, "": ""}[said]
    assert policy.checkout_write(line, place(where), (KEPT,), "cmd") == expected, line
    wrapped = 'cmd /c "' + line.replace('"', '""') + '"'
    assert policy.checkout_write(wrapped, place(where), (KEPT,)) == expected, wrapped


@pytest.mark.parametrize(
    "line",
    [
        # As Codex runs a worker's command on Windows: in PowerShell (its path quoted, as a
        # shell would quote it, or as Codex joins its words), or in cmd.
        r'"C:\Program Files\PowerShell\7\pwsh.exe" -NoProfile -Command "Set-Content x y"',
        r"'C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe' -NoProfile -Command "
        r"'Set-Content x y'",
        "powershell.exe -NoLogo -ExecutionPolicy Bypass -c Set-Content x y",
        # -EncodedCommand: "Set-Content x y", as UTF-16 in base64.
        "pwsh -NoProfile -EncodedCommand UwBlAHQALQBDAG8AbgB0AGUAbgB0ACAAeAAgAHkA",
        r'C:\Windows\System32\cmd.exe /d /s /c "echo y > x"',
    ],
)
def test_codexs_wrapped_lines_are_read_inside_their_shell(line):
    assert policy.checkout_write(line, KEPT, (KEPT,)) == policy.CHECKOUT_WRITES
    reading = line.replace("Set-Content x y", "Get-Content x").replace("echo y > x", "type x")
    if "Encoded" not in line:
        assert policy.checkout_write(reading, KEPT, (KEPT,)) == "", reading


@pytest.mark.parametrize(
    "target",
    [
        rf"{KEPT}\notes.txt",
        rf"{KEPT.upper()}\NOTES.TXT",
        KEPT.replace("\\", "/") + "/notes.txt",
        rf"\\?\{KEPT}\notes.txt",
        rf"{KEPT}.\notes.txt",
        rf"{KEPT} \notes.txt",
        rf"{KEPT}\src\..\notes.txt",
        r"C:\Users\owner\code\ws\..\app\notes.txt",
        rf"FileSystem::{KEPT}\notes.txt",
        rf"{KEPT}\README.md:stream",
    ],
)
def test_a_path_names_the_checkout_however_windows_spells_it(target):
    r"""Drive letters in any case, `/` or `\`, `\\?\`, the trailing dots and spaces Windows
    drops from a name, `..`, PowerShell's provider prefix, a file's stream."""
    writes = policy.checkout_write(f"Set-Content '{target}' y", OWN, (KEPT,))
    assert writes == policy.CHECKOUT_WRITES
    assert policy.checkout_write(f"Get-Content '{target}'", OWN, (KEPT,)) == ""
    if "::" not in target:
        assert path_key(target).startswith(path_key(KEPT) + "\\")


def test_plain_paths_are_as_windows_takes_them():
    assert plain_path(r"\\?\C:\a\b") == r"C:\a\b"
    assert plain_path(r"\\?\UNC\srv\share\x") == r"\\srv\share\x"
    assert plain_path("C:/a/b./c ") == r"C:\a\b\c"
    assert plain_path(r"C:\a\..\b") == r"C:\b"
    assert path_key(r"C:\App\X") == path_key(r"c:\app\x")


@pytest.mark.parametrize(
    ("line", "where", "said"),
    [
        ("Set-Location $env:TEMP; Set-Content notes.txt y", "O", ""),
        ("Set-Location $d; .\\build.ps1", "O", ""),
        ("npm test", "O", ""),
        (f"Set-Content {KEPT}\\notes.txt y", "O", "W"),
        (f"Set-Location {KEPT}; echo y >> README.md", "O", "W"),
        ("echo y >> README.md", "K", "W"),
    ],
)
def test_the_checkout_a_workers_worktree_was_made_from_is_read_where_it_is_named(line, where, said):
    """The owner's checkout a worker's own worktree was made from (live r77 F1): a line naming
    it, or run in it, that writes there is refused as in one given to read; a folder not known
    is not taken for it (the worker works in its worktree), where it is for one given to read."""
    expected = {"W": policy.CHECKOUT_WRITES, "": ""}[said]
    assert policy.checkout_write(line, place(where), (KEPT,), "", False) == expected, line
    if where == "O" and not said and "$" in line:
        assert policy.checkout_write(line, place(where), (KEPT,)) == policy.CHECKOUT_ASKS


async def test_a_windows_workers_command_in_the_owners_checkout_runs_in_its_own_copy(tmp_path):
    """Through Chief of Staff's own permission check (Copilot's and Codex's alike): refused,
    a copy of its own made, and sent there; reads let through; the owner's checkout as it was."""
    app = to_read(tmp_path)
    before = owner_bytes(app)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    info = await spawn(runtime, harness.router_id, title="Run the tests", instructions="run",
                       project=str(app))  # fmt: skip
    worker = info["session_id"]
    reads = ["Get-Content README.md", "Get-ChildItem -Recurse | Select-Object -First 3",
             "git status", "Select-String -Path *.md -Pattern app"]  # fmt: skip
    for line in reads:
        assert await decided(runtime, worker, line, str(app)) == "PermissionDecisionApproveOnce"
    said = await decided(runtime, worker, "Set-Content notes.txt y", str(app))
    [copy] = copies(harness, worker)
    assert said == policy.OWN_COPY.format(path=copy["path"], left_out=""), said
    for line in ["Remove-Item README.md", ".\\run_tests.sh", 'cmd /c "del README.md"']:
        assert await decided(runtime, worker, line, str(app)) == said, line
    assert len(copies(harness, worker)) == 1
    assert (Path(copy["path"]) / "README.md").read_text() == "# app, edited\n"
    assert owner_bytes(app) == before


@windows
@pytest.mark.parametrize(
    "spelled",
    [
        lambda path: path,
        lambda path: path.upper(),
        lambda path: path.replace("\\", "/"),
        lambda path: "\\\\?\\" + path,
        lambda path: path.replace("\\app\\", "\\app.\\"),
    ],
)
async def test_a_windows_workers_edit_in_the_owners_checkout_is_refused(tmp_path, spelled):
    """An edit through its file tool, however Windows spells the path, is refused there."""
    app = to_read(tmp_path)
    harness = Harness(tmp_path / "data", Settings(projects=[str(app)]))
    runtime = await harness.connect()
    info = await spawn(runtime, harness.router_id, title="Read", instructions="read",
                       project=str(app))  # fmt: skip
    target = spelled(str(app.resolve() / "notes.txt"))
    request = SimpleNamespace(kind="write", file_name=target, resolved_path=target)
    decision = await runtime.events.on_permission(request, {"session_id": info["session_id"]})
    assert "owner's own checkout" in getattr(decision, "feedback", ""), (target, decision)
    own = runtime.workspaces.workspace(harness.store.session(info["session_id"]))
    elsewhere = SimpleNamespace(kind="write", file_name=str(own / "n.txt"),
                                resolved_path=str(own / "n.txt"))  # fmt: skip
    decision = await runtime.events.on_permission(elsewhere, {"session_id": info["session_id"]})
    assert type(decision).__name__ == "PermissionDecisionApproveOnce"
