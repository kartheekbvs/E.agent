
import os
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
REGISTRY_DB = Path(
    os.getenv(
        "ENTROPY_PREVIEW_DB",
        str(ROOT_DIR / "entropy_previews.sqlite3"),
    )
).resolve()


def _connect():
    connection = sqlite3.connect(REGISTRY_DB, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_preview_registry():
    REGISTRY_DB.parent.mkdir(parents=True, exist_ok=True)

    connection = _connect()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS previews (
                preview_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                port INTEGER NOT NULL,
                workspace_path TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS previews_project_idx
            ON previews(project_id)
            """
        )
        connection.commit()
    finally:
        connection.close()


def register_preview(
    preview_id: str,
    project_id: str,
    owner_id: str | int,
    port: int,
    workspace_path: str,
):
    connection = _connect()
    try:
        connection.execute(
            """
            INSERT INTO previews (
                preview_id, project_id, owner_id, port, workspace_path
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(preview_id) DO UPDATE SET
                project_id = excluded.project_id,
                owner_id = excluded.owner_id,
                port = excluded.port,
                workspace_path = excluded.workspace_path
            """,
            (
                preview_id,
                project_id,
                str(owner_id),
                port,
                str(Path(workspace_path).resolve()),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def get_preview(preview_id: str, owner_id: str | int):
    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT preview_id, project_id, owner_id, port, workspace_path
            FROM previews
            WHERE preview_id = ? AND owner_id = ?
            """,
            (preview_id, str(owner_id)),
        ).fetchone()
    finally:
        connection.close()

    return dict(row) if row else None


def remove_preview(preview_id: str, owner_id: str | int):
    connection = _connect()
    try:
        connection.execute(
            """
            DELETE FROM previews
            WHERE preview_id = ? AND owner_id = ?
            """,
            (preview_id, str(owner_id)),
        )
        connection.commit()
    finally:
        connection.close()


def register_preview_from_environment(
    preview_id: str,
    port: int,
    workspace_path: str,
):
    import os

    project_id = os.environ.get("ENTROPY_PROJECT_ID")
    owner_id = os.environ.get("ENTROPY_OWNER_ID")

    if not project_id or not owner_id:
        raise RuntimeError(
            "Project ID and owner ID are required to register a preview."
        )

    register_preview(
        preview_id=preview_id,
        project_id=project_id,
        owner_id=owner_id,
        port=port,
        workspace_path=workspace_path,
    )
