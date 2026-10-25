import difflib
import hashlib
import os
import tempfile
from pathlib import Path

from .models import ProposedFile


def safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("File proposals must use project-relative paths without '..'.")
    if any(
        part in {".git", ".ssh", ".aws", ".copilot", ".venv", "__pycache__"}
        or part.startswith(".env")
        or part in {"credentials", "id_rsa", "id_ed25519"}
        for part in path.parts
    ):
        raise ValueError("Changes to credentials, runtime, or repository metadata are not allowed.")
    root = root.resolve(strict=True)
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink paths are not supported for file proposals.")
    if not current.resolve().is_relative_to(root):
        raise ValueError("File escapes the authorized project.")
    if current.exists() and not current.is_file():
        raise ValueError("The proposed path is not a regular file.")
    return current


def fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("File too large for prototype editing (1 MiB maximum).")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_files(project: str, proposals: list[ProposedFile]) -> dict:
    if not project:
        raise ValueError("Select a project before proposing changes.")
    prepared = []
    for proposal in proposals:
        path = safe_path(Path(project), proposal.path)
        original_hash = fingerprint(path)
        original = path.read_text(encoding="utf-8") if original_hash is not None else ""
        diff = "".join(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                proposal.content.splitlines(keepends=True),
                fromfile=proposal.path,
                tofile=proposal.path,
            )
        )
        prepared.append(
            {
                "path": proposal.path,
                "content": proposal.content,
                "original_hash": original_hash,
                "diff": diff,
            }
        )
    return {"project_path": project, "files": prepared}


def apply_files(payload: dict):
    root = Path(payload["project_path"])
    for file in payload["files"]:
        path = safe_path(root, file["path"])
        if fingerprint(path) != file["original_hash"]:
            raise ValueError(f"{file['path']} changed after preview. Generate a new proposal.")
    for file in payload["files"]:
        path = safe_path(root, file["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        # Recheck immediately before each replacement, in addition to whole-batch preflight.
        if fingerprint(path) != file["original_hash"]:
            raise ValueError(f"{file['path']} changed while applying the proposal.")
        fd, temporary = tempfile.mkstemp(prefix=".work-buddy-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(file["content"])
                output.flush()
                os.fsync(output.fileno())
            if path.exists():
                os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
