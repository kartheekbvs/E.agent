
from pathlib import Path
from uuid import UUID, uuid4

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / "runtime_projects"


def create_project_runtime(project_id: str | None = None) -> dict:
    """Create a new, isolated runtime workspace for a project."""

    project_id = project_id or uuid4().hex

    try:
        project_id = UUID(hex=project_id).hex
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("Invalid project ID") from error

    if RUNTIME_DIR.is_symlink():
        raise RuntimeError("Runtime directory cannot be a symbolic link")

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    root = RUNTIME_DIR.resolve()

    project_dir = root / project_id
    workspace_dir = project_dir / "workspace"

    if project_dir.exists() or project_dir.is_symlink():
        raise FileExistsError(
            f"Runtime already exists for project {project_id}"
        )

    workspace_dir.mkdir(parents=True, exist_ok=False)

    return {
        "project_id": project_id,
        "runtime_dir": str(project_dir),
        "workspace_dir": str(workspace_dir.resolve()),
    }


def get_project_runtime(project_id: str) -> dict:
    """Locate an existing runtime without creating or deleting files."""

    try:
        project_id = UUID(hex=project_id).hex
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("Invalid project ID") from error

    if RUNTIME_DIR.is_symlink():
        raise RuntimeError("Runtime directory cannot be a symbolic link")

    root = RUNTIME_DIR.resolve()
    project_dir = root / project_id
    workspace_dir = project_dir / "workspace"

    if project_dir.is_symlink() or workspace_dir.is_symlink():
        raise RuntimeError("Project runtime cannot contain symlinks")

    if not workspace_dir.is_dir():
        raise FileNotFoundError("Project workspace not found")

    resolved_workspace = workspace_dir.resolve()
    resolved_workspace.relative_to(root)

    return {
        "project_id": project_id,
        "runtime_dir": str(project_dir),
        "workspace_dir": str(resolved_workspace),
    }
