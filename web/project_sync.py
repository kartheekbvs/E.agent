
import os
from pathlib import Path, PurePosixPath
import json
from web.project_isolation import get_project_runtime
from web.project_records import get_project_for_user
from web.supabase_store import (
    get_supabase,
    upload_project_file,
    download_project_file,
    delete_project_file,
)


def _safe_workspace_path(workspace: Path, relative_path: str) -> Path:
    """Resolve a relative path and prevent escaping the workspace."""

    relative = PurePosixPath(relative_path)

    if (
        not relative_path
        or relative.is_absolute()
        or "\\" in relative_path
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise ValueError("Invalid project file path")

    root = workspace.resolve()
    destination = root.joinpath(*relative.parts)

    # Resolve existing parent directories as well, preventing symlink escapes.
    resolved = destination.resolve(strict=False)

    if not resolved.is_relative_to(root):
        raise ValueError("Project file path escapes its workspace")

    return resolved


def _list_storage_files(storage_prefix: str) -> list[str]:
    """List files stored beneath a project's prefix."""

    client = get_supabase()
    files = []
    folders = [storage_prefix]

    while folders:
        current = folders.pop()

        entries = (
            client.storage
            .from_(os.getenv("SUPABASE_BUCKET", "entropy-projects"))
            .list(current)
        )

        for entry in entries:
            name = entry.get("name")

            if not name or name in (".", ".."):
                continue

            object_path = f"{current}/{name}"

            # Supabase Storage represents folders with metadata entries.
            if entry.get("id") is None and entry.get("metadata") is None:
                folders.append(object_path)
            else:
                files.append(object_path[len(storage_prefix) + 1:])

    return files


def _manifest_path(workspace: Path) -> Path:
    """Return the path of the sync manifest for this workspace."""
    return workspace / ".entropy-sync-manifest.json"


def _read_manifest(workspace: Path) -> set[str]:
    """Read the last successfully restored file list."""
    path = _manifest_path(workspace)

    if not path.exists():
        return set()

    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, list) or not all(
        isinstance(item, str) for item in data
    ):
        raise ValueError("Invalid project sync manifest")

    return set(data)


def _write_manifest(workspace: Path, paths: set[str]) -> None:
    """Atomically write the list of restored project files."""
    path = _manifest_path(workspace)
    temporary = path.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(sorted(paths), indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def restore_project(user_id: str | int, project_id: str) -> Path:
    """Restore a user's saved project and record its file manifest."""

    project = get_project_for_user(user_id, project_id)
    runtime = get_project_runtime(project_id)

    workspace = Path(runtime["workspace_dir"]).resolve()
    storage_prefix = project["storage_prefix"]

    restored_paths: set[str] = set()

    for relative_path in _list_storage_files(storage_prefix):
        destination = _safe_workspace_path(workspace, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)

        content = download_project_file(storage_prefix, relative_path)
        destination.write_bytes(content)
        restored_paths.add(relative_path)

    # Write the manifest only after all downloads succeed.
    _write_manifest(workspace, restored_paths)

    return workspace



def save_project(user_id: str | int, project_id: str) -> int:
    """Upload changed files and reconcile previously tracked deletions."""

    project = get_project_for_user(user_id, project_id)
    runtime = get_project_runtime(project_id)

    workspace = Path(runtime["workspace_dir"]).resolve()
    storage_prefix = project["storage_prefix"]

    # Read the previous manifest before changing anything.
    previously_tracked = _read_manifest(workspace)

    current_files: set[str] = set()
    uploaded = 0

    for current_dir, dirs, filenames in os.walk(
        workspace,
        followlinks=False,
    ):
        current = Path(current_dir)

        # Never traverse symlinked directories.
        dirs[:] = [
            name
            for name in dirs
            if not (current / name).is_symlink()
        ]

        for filename in filenames:
            source = current / filename

            # Ignore symlinks and non-regular files.
            if source.is_symlink() or not source.is_file():
                continue

            relative_path = source.relative_to(workspace).as_posix()

            # The manifest is internal metadata, not a project file.
            if relative_path == ".entropy-sync-manifest.json":
                continue

            _safe_workspace_path(workspace, relative_path)
            current_files.add(relative_path)

            upload_project_file(
                storage_prefix,
                relative_path,
                source.read_bytes(),
            )
            uploaded += 1

    # Delete only files explicitly tracked by the previous manifest.
    deleted_files = previously_tracked - current_files

    for relative_path in sorted(deleted_files):
        _safe_workspace_path(workspace, relative_path)
        delete_project_file(storage_prefix, relative_path)

    # Update the manifest after all upload and delete operations succeed.
    _write_manifest(workspace, current_files)

    return uploaded
