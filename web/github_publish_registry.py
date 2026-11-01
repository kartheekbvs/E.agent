
import json
import os
import secrets
import sqlite3
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

REGISTRY_DB = Path(
    os.getenv(
        "ENTROPY_GITHUB_PUBLISH_DB",
        str(ROOT_DIR / "entropy_github_publish.sqlite3"),
    )
).resolve()


def _connect():
    connection = sqlite3.connect(REGISTRY_DB, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_publish_registry():
    REGISTRY_DB.parent.mkdir(parents=True, exist_ok=True)

    connection = _connect()
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_reviews (
                review_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                repository TEXT NOT NULL,
                branch TEXT NOT NULL,
                base_head_sha TEXT NOT NULL,
                changes_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS publish_reviews_owner_idx
            ON publish_reviews(owner_id, created_at)
            """
        )
        connection.commit()
    finally:
        connection.close()


def create_publish_review(
    *,
    owner_id: str | int,
    project_id: str,
    repository: str,
    branch: str,
    base_head_sha: str,
    changes: list[dict],
    created_at: int,
) -> str:
    review_id = secrets.token_urlsafe(32)

    connection = _connect()
    try:
        # Keep only a small number of recent reviews per user.
        connection.execute(
            """
            DELETE FROM publish_reviews
            WHERE owner_id = ? AND created_at < ?
            """,
            (str(owner_id), created_at - 1800),
        )

        connection.execute(
            """
            INSERT INTO publish_reviews (
                review_id, owner_id, project_id, repository, branch,
                base_head_sha, changes_json, created_at, consumed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                review_id,
                str(owner_id),
                project_id,
                repository,
                branch,
                base_head_sha,
                json.dumps(changes, ensure_ascii=False),
                created_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    return review_id


def consume_publish_review(
    *,
    review_id: str,
    owner_id: str | int,
    now: int,
) -> dict | None:
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")

        row = connection.execute(
            """
            SELECT *
            FROM publish_reviews
            WHERE review_id = ?
              AND owner_id = ?
              AND consumed = 0
              AND created_at >= ?
            """,
            (review_id, str(owner_id), now - 1800),
        ).fetchone()

        if row is None:
            connection.rollback()
            return None

        updated = connection.execute(
            """
            UPDATE publish_reviews
            SET consumed = 1
            WHERE review_id = ? AND owner_id = ? AND consumed = 0
            """,
            (review_id, str(owner_id)),
        )

        if updated.rowcount != 1:
            connection.rollback()
            return None

        result = dict(row)
        result["changes"] = json.loads(result.pop("changes_json"))
        connection.commit()
        return result
    finally:
        connection.close()

def get_publish_review(
    *,
    review_id: str,
    owner_id: str | int,
    now: int,
) -> dict | None:
    """Read a valid review without consuming its approval."""

    connection = _connect()
    try:
        row = connection.execute(
            """
            SELECT *
            FROM publish_reviews
            WHERE review_id = ?
              AND owner_id = ?
              AND consumed = 0
              AND created_at >= ?
            """,
            (review_id, str(owner_id), now - 1800),
        ).fetchone()

        if row is None:
            return None

        result = dict(row)
        result["changes"] = json.loads(
            result.pop("changes_json")
        )
        return result
    finally:
        connection.close()
