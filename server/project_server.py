
import os
import sys
from pathlib import Path
from web.preview_registry import (
    initialize_preview_registry,
    register_preview,
    remove_preview,
)
ROOT_DIR = Path(__file__).resolve().parent.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def register_started_preview(result: dict) -> dict:
    if not result.get("success"):
        return result

    preview_id = result.get("project_id")
    port = result.get("port")
    if not preview_id or not port:
        return result

    register_preview(
        preview_id=preview_id,
        project_id=os.environ["ENTROPY_PROJECT_ID"],
        owner_id=os.environ["ENTROPY_OWNER_ID"],
        port=int(port),
        workspace_path=os.environ["ENTROPY_WORKSPACE"],
    )
    return result


def cleanup_stopped_previews(entropy_server) -> int:
    """Remove registry records for preview processes that have stopped."""
    stopped_ids = []
    for preview_id, project in list(entropy_server.PROJECTS.items()):
        process = project.get("process")
        if process is None or process.poll() is not None:
            stopped_ids.append(preview_id)

    owner_id = os.environ.get("ENTROPY_OWNER_ID")
    removed = 0
    if not owner_id:
        return 0

    for preview_id in stopped_ids:
        if remove_preview(preview_id, owner_id):
            removed += 1
    return removed


def main():
    workspace_value = os.environ.get("ENTROPY_WORKSPACE")

    if not workspace_value:
        raise RuntimeError(
            "ENTROPY_WORKSPACE must point to the project workspace."
        )

    project_id = os.environ.get("ENTROPY_PROJECT_ID")
    owner_id = os.environ.get("ENTROPY_OWNER_ID")

    if not project_id or not owner_id:
        raise RuntimeError(
            "ENTROPY_PROJECT_ID and ENTROPY_OWNER_ID are required."
        )

    workspace = Path(workspace_value).resolve()

    if not workspace.is_dir():
        raise FileNotFoundError(
            f"Project workspace does not exist: {workspace}"
        )

    if workspace.is_symlink():
        raise RuntimeError(
            "Project workspace cannot be a symbolic link."
        )

    initialize_preview_registry()

    import server.server as entropy_server

    # Redirect the existing server's workspace without editing server.py.
    entropy_server.WORKSPACE = workspace
    entropy_server.PROJECT_ID = project_id
    entropy_server.OWNER_ID = owner_id

    original_start_project = entropy_server.start_project
    entropy_server.mcp.remove_tool("start_project")

    def project_start_project(
        command: str = "auto",
        args: list[str] | None = None,
        port: int = 0,
    ) -> dict:
        # Clean up previews that stopped since the last launch.
        cleanup_stopped_previews(entropy_server)
        result = original_start_project(
            command=command,
            args=args,
            port=port,
        )
        if result.get("success"):
            register_started_preview(result)
        return result

    entropy_server.mcp.add_tool(
        project_start_project,
        name="start_project",
        description=(
            "Start a web project from this project's isolated workspace."
        ),
    )

    # Make the MCP server's path validation use the selected workspace.
    # resolve_path() reads entropy_server.WORKSPACE when called.
    entropy_server.mcp.run()


if __name__ == "__main__":
    main()
