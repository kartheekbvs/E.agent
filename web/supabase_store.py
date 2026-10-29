
import os
from functools import lru_cache
from pathlib import PurePosixPath

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

BUCKET_NAME = os.getenv(
    "SUPABASE_BUCKET",
    "entropy-projects",
)


@lru_cache(maxsize=1)
def get_supabase():
    """Create one reusable server-side Supabase client."""

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SECRET_KEY")

    if not url:
        raise RuntimeError("SUPABASE_URL is missing")

    if not key:
        raise RuntimeError("SUPABASE_SECRET_KEY is missing")

    return create_client(url, key)


def build_object_path(
    storage_prefix: str,
    relative_path: str,
) -> str:
    """Build a safe object path inside a project's storage prefix."""

    if not storage_prefix or not relative_path:
        raise ValueError("Storage prefix and file path are required")

    prefix = PurePosixPath(storage_prefix)
    file_path = PurePosixPath(relative_path)

    for value in (prefix, file_path):
        if value.is_absolute() or "\\" in str(value):
            raise ValueError("Invalid storage path")

        if any(part in ("", ".", "..") for part in value.parts):
            raise ValueError("Invalid storage path")

    return f"{prefix.as_posix()}/{file_path.as_posix()}"


def upload_project_file(
    storage_prefix: str,
    relative_path: str,
    content: bytes,
) -> str:
    """Upload file bytes to private project storage."""

    if not isinstance(content, bytes):
        raise TypeError("File content must be bytes")

    object_path = build_object_path(
        storage_prefix,
        relative_path,
    )

    get_supabase().storage.from_(BUCKET_NAME).upload(
        object_path,
        content,
        file_options={
            "upsert": "true",
        },
    )

    return object_path


def download_project_file(
    storage_prefix: str,
    relative_path: str,
) -> bytes:
    """Download file bytes from private project storage."""

    object_path = build_object_path(
        storage_prefix,
        relative_path,
    )

    result = get_supabase().storage.from_(
        BUCKET_NAME
    ).download(object_path)

    return result

def delete_project_file(
    storage_prefix: str,
    relative_path: str,
) -> None:
    """Delete one file from a project's private storage prefix."""

    object_path = build_object_path(
        storage_prefix,
        relative_path,
    )

    get_supabase().storage.from_(BUCKET_NAME).remove(
        [object_path]
    )
