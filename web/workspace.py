from pathlib import Path
import shutil
BASE_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = BASE_DIR / "workspace"

def clear_workspace():
    if WORKSPACE_DIR.is_symlink():
        raise RuntimeError("Workspace cannot be a symbolic link")

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

    for item in WORKSPACE_DIR.iterdir():
        if item.is_symlink() or item.is_file():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)
def list_workspace_files():
    files = []

    for item in WORKSPACE_DIR.rglob("*"):
        if item.is_symlink():
            continue

        if item.is_file():
            relative_path = item.relative_to(WORKSPACE_DIR)

            files.append({
                "name": item.name,
                "path": relative_path.as_posix(),
                "type": "file",
            })

    return files


def resolve_workspace_file(relative_path: str) -> Path:
    if not relative_path:
        raise ValueError("File path cannot be empty")
    if WORKSPACE_DIR.is_symlink():
        raise ValueError("Workspace cannot be a symbolic link")

    root = WORKSPACE_DIR.resolve()
    requested_path = Path(relative_path)

    if requested_path.is_absolute() or ".." in requested_path.parts:
        raise ValueError("Invalid workspace file path")

    file_path = WORKSPACE_DIR / requested_path

    # Resolve the path and ensure it stays inside workspace.
    resolved_path = file_path.resolve()
    try:
        resolved_path.relative_to(root)
    except ValueError:
        raise ValueError("File must be inside workspace")

    # Do not read through symbolic links.
    current_path = WORKSPACE_DIR
    for part in requested_path.parts:
        current_path = current_path / part
        if current_path.is_symlink():
            raise ValueError("Symbolic links are not allowed")

    if not resolved_path.is_file():
        raise FileNotFoundError("Workspace file not found")

    return resolved_path


def read_workspace_file(relative_path: str) -> str:
    return resolve_workspace_file(relative_path).read_text(encoding="utf-8")
