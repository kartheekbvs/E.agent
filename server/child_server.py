

import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("entropy-child")

WORKSPACE_ROOT = Path(
    os.environ.get("ENTROPY_WORKSPACE", "workspace")
).resolve()




def resolve_workspace_path(path: str) -> Path:
    if not path:
        raise ValueError("Path cannot be empty")

    if WORKSPACE_ROOT.is_symlink():
        raise ValueError("Workspace cannot be a symbolic link")

    root = WORKSPACE_ROOT.resolve()
    requested_path = Path(path)

    if requested_path.is_absolute() or ".." in requested_path.parts:
        raise ValueError("Invalid workspace path")

    current_path = WORKSPACE_ROOT
    for part in requested_path.parts:
        current_path = current_path / part
        if current_path.is_symlink():
            raise ValueError("Symbolic links are not allowed")

    target = (root / requested_path).resolve()

    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError(
            "Path is outside the workspace"
        ) from error

    return target



@mcp.tool()
def list_file(path: str = ".") -> dict:
    """List files and directories inside the child agent's workspace."""

    target = resolve_workspace_path(path)

    if not target.is_dir():
        return {
            "success": False,
            "error": f"Not a directory: {path}",
        }

    entries = sorted(
        entry.name + ("/" if entry.is_dir() else "")
        for entry in target.iterdir()
    )

    return {
        "success": True,
        "path": path,
        "entries": entries,
    }


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a UTF-8 text file inside the child agent's workspace."""

    target = resolve_workspace_path(path)

    if not target.is_file():
        return {
            "success": False,
            "error": f"Not a file: {path}",
        }

    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return {
            "success": False,
            "error": str(error),
        }

    return {
        "success": True,
        "path": path,
        "content": content,
    }


if __name__ == "__main__":
    mcp.run()
