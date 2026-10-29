
from pathlib import Path
import shutil

BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = BASE_DIR / "workspace"


def _get_workspace_dir(workspace_dir=None) -> Path:
    """Return a workspace root, defaulting to the legacy shared workspace."""
    root = Path(workspace_dir) if workspace_dir is not None else WORKSPACE_DIR

    if root.is_symlink():
        raise ValueError("Workspace cannot be a symbolic link")

    return root


def clear_workspace():
    """Clear only the legacy shared workspace."""
    root = _get_workspace_dir()
    root.mkdir(parents=True, exist_ok=True)

    for item in root.iterdir():
        if item.is_symlink() or item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)


def list_workspace_files(workspace_dir=None):
    """List files from the selected workspace, without following symlinks."""
    root = _get_workspace_dir(workspace_dir)
    files = []

    if not root.exists():
        return files

    for item in root.rglob("*"):
        if item.is_symlink():
            continue

        if item.is_file():
            relative_path = item.relative_to(root)

            files.append({
                "name": item.name,
                "path": relative_path.as_posix(),
                "type": "file",
            })

    return sorted(files, key=lambda item: item["path"].lower())


def resolve_workspace_file(relative_path: str, workspace_dir=None) -> Path:
    """Resolve a file safely inside the selected workspace."""
    if not relative_path:
        raise ValueError("File path cannot be empty")

    root_dir = _get_workspace_dir(workspace_dir)
    root = root_dir.resolve()
    requested_path = Path(relative_path)

    if requested_path.is_absolute() or ".." in requested_path.parts:
        raise ValueError("Invalid workspace file path")

    file_path = root_dir / requested_path

    # Reject symlinks in every component of the requested path.
    current_path = root_dir
    for part in requested_path.parts:
        current_path = current_path / part
        if current_path.is_symlink():
            raise ValueError("Symbolic links are not allowed")

    resolved_path = file_path.resolve()

    try:
        resolved_path.relative_to(root)
    except ValueError:
        raise ValueError("File must be inside workspace")

    if not resolved_path.is_file():
        raise FileNotFoundError("Workspace file not found")

    return resolved_path


def read_workspace_file(relative_path: str, workspace_dir=None) -> str:
    """Read a file from the selected workspace."""
    return resolve_workspace_file(
        relative_path,
        workspace_dir=workspace_dir,
    ).read_text(encoding="utf-8")
