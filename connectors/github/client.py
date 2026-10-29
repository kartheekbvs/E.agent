
import os
import sqlite3
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from urllib.parse import quote
from connectors.github.auth import DB_PATH
import os
from mcp import StdioServerParameters

GITHUB_API_BASE = "https://api.github.com"


def get_github_token(user_id: int) -> str | None:
    """Retrieve and decrypt the connected user's GitHub token."""
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            """
            SELECT access_token_encrypted
            FROM github_connections
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    if row is None:
        return None

    key = os.getenv("GITHUB_TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("GitHub token encryption key is missing.")

    cipher = Fernet(key.encode("ascii"))
    return cipher.decrypt(row[0].encode("ascii")).decode("utf-8")


async def list_repositories(
    user_id: int,
    page: int = 1,
    per_page: int = 20,
) -> list[dict]:
    """List one page of repositories accessible to the connected GitHub user."""
    if page < 1:
        raise ValueError("Page must be at least 1.")

    if not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100.")

    token = get_github_token(user_id)

    if token is None:
        raise ValueError("No GitHub account is connected.")

    repositories = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            response = await client.get(
                f"{GITHUB_API_BASE}/user/repos",
                params={
                    "per_page": per_page,
                    "page": page,
                    "sort": "updated",
                    "affiliation": "owner,collaborator,organization_member",
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )

            if response.status_code == 401:
                raise ValueError(
                    "GitHub authorization expired or was revoked. Reconnect GitHub."
                )

            response.raise_for_status()
            batch = response.json()

            if not batch:
                break

            repositories.extend(
                {
                    "id": repo["id"],
                    "name": repo["name"],
                    "full_name": repo["full_name"],
                    "private": repo["private"],
                    "html_url": repo["html_url"],
                    "description": repo.get("description"),
                    "default_branch": repo.get("default_branch"),
                    "updated_at": repo.get("updated_at"),
                }
                for repo in batch
            )

            break

    return repositories

async def get_repository_tree(
    user_id: int,
    owner: str,
    repo: str,
    branch: str | None = None,
) -> dict:
    token = get_github_token(user_id)

    if token is None:
        raise ValueError("No GitHub account is connected.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Verify repository access and discover its default branch.
        response = await client.get(
            f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}",
            headers=headers,
        )
        response.raise_for_status()
        repository = response.json()

        selected_branch = branch or repository["default_branch"]

        # Get the selected branch's commit and tree SHA.
        branch_response = await client.get(
            f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/"
            f"{quote(repo, safe='')}/branches/{quote(selected_branch, safe='')}",
            headers=headers,
        )
        branch_response.raise_for_status()
        branch_data = branch_response.json()
        tree_sha = branch_data["commit"]["commit"]["tree"]["sha"]

        # Fetch the recursive tree.
        tree_response = await client.get(
            f"{GITHUB_API_BASE}/repos/{quote(owner, safe='')}/"
            f"{quote(repo, safe='')}/git/trees/{quote(tree_sha, safe='')}",
            params={"recursive": "1"},
            headers=headers,
        )
        tree_response.raise_for_status()
        tree_data = tree_response.json()

    entries = [
        {
            "path": item["path"],
            "type": item["type"],
            "size": item.get("size"),
            "sha": item["sha"],
        }
        for item in tree_data.get("tree", [])
        if item["type"] in {"blob", "tree"}
    ]

    return {
        "success": True,
        "repository": repository["full_name"],
        "branch": selected_branch,
        "default_branch": repository["default_branch"],
        "truncated": tree_data.get("truncated", False),
        "count": len(entries),
        "entries": entries,
    }

import base64

async def read_repository_file(
    user_id: int,
    owner: str,
    repo: str,
    path: str,
    branch: str | None = None,
) -> dict:
    """Read a text file from an accessible GitHub repository."""

    token = get_github_token(user_id)

    if token is None:
        raise ValueError("No GitHub account is connected.")

    if not path or path.startswith("/") or "\\" in path:
        raise ValueError("Invalid repository file path.")

    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("Invalid repository file path.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Verify repository access and discover its default branch.
        repository_response = await client.get(
            f"{GITHUB_API_BASE}/repos/"
            f"{quote(owner, safe='')}/{quote(repo, safe='')}",
            headers=headers,
        )
        repository_response.raise_for_status()
        repository = repository_response.json()

        selected_branch = branch or repository["default_branch"]

        # Fetch the file metadata and content.
        file_response = await client.get(
            f"{GITHUB_API_BASE}/repos/"
            f"{quote(owner, safe='')}/{quote(repo, safe='')}/contents/"
            f"{quote(path, safe='/')}",
            params={"ref": selected_branch},
            headers=headers,
        )
        file_response.raise_for_status()
        file_data = file_response.json()

    if file_data.get("type") != "file":
        raise ValueError("The selected path is not a file.")

    if file_data.get("size", 0) > 1_000_000:
        raise ValueError("File is too large to display (maximum 1 MB).")

    encoded_content = file_data.get("content", "")
    if not encoded_content:
        raise ValueError(
            "GitHub did not return file content. The file may be empty "
            "or require a different download method."
        )

    try:
        content = base64.b64decode(encoded_content, validate=False)
        text_content = content.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError("This file is not a readable UTF-8 text file.") from error

    if "\x00" in text_content:
        raise ValueError("Binary files cannot be displayed as text.")

    return {
        "success": True,
        "repository": repository["full_name"],
        "branch": selected_branch,
        "path": path,
        "size": file_data.get("size", len(content)),
        "content": text_content,
    }
