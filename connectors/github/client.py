
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
        commit_sha = branch_data["commit"]["sha"]

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
    "commit_sha": commit_sha,
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

async def get_branch_head(
    user_id: int,
    owner: str,
    repo: str,
    branch: str,
) -> dict:
    """Return the current branch commit and tree SHAs."""
    token = get_github_token(user_id)

    if token is None:
        raise ValueError("No GitHub account is connected.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    url = (
        f"{GITHUB_API_BASE}/repos/"
        f"{quote(owner, safe='')}/{quote(repo, safe='')}/branches/"
        f"{quote(branch, safe='')}"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

    return {
        "branch": data["name"],
        "commit_sha": data["commit"]["sha"],
        "tree_sha": data["commit"]["commit"]["tree"]["sha"],
    }


async def publish_file_changes(
    user_id: int,
    owner: str,
    repo: str,
    branch: str,
    expected_head_sha: str,
    changes: list[dict],
    message: str,
) -> dict:
    """
    Publish reviewed file changes as one commit.

    Each change must contain:
        {"path": "relative/file.py", "content": "file contents"}

    This function refuses to publish if the branch has changed
    since the review snapshot was created.
    """
    import re

    token = get_github_token(user_id)

    if token is None:
        raise ValueError("No GitHub account is connected.")

    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_head_sha):
        raise ValueError("Invalid reviewed branch commit SHA.")

    if not changes:
        raise ValueError("There are no file changes to publish.")

    message = message.strip()
    if not message or len(message) > 200:
        raise ValueError("Commit message must contain 1–200 characters.")

    seen_paths = set()
    for item in changes:
        path = item.get("path")
        content = item.get("content")

        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError("A change contains an invalid file path.")

        if path in seen_paths:
            raise ValueError(f"Duplicate file path: {path}")

        if not isinstance(content, str):
            raise ValueError(f"File content must be text: {path}")

        if "\x00" in content:
            raise ValueError(f"Binary files are not supported: {path}")

        seen_paths.add(path)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    base = (
        f"{GITHUB_API_BASE}/repos/"
        f"{quote(owner, safe='')}/{quote(repo, safe='')}"
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Recheck the branch immediately before creating Git objects.
        branch_response = await client.get(
            f"{base}/branches/{quote(branch, safe='')}",
            headers=headers,
        )
        branch_response.raise_for_status()
        branch_data = branch_response.json()

        current_head = branch_data["commit"]["sha"]
        base_tree_sha = branch_data["commit"]["commit"]["tree"]["sha"]

        if current_head != expected_head_sha:
            raise ValueError(
                "The GitHub branch changed after review. "
                "Refresh the preview and review the changes again."
            )

        # Create one Git blob for each reviewed file.
        tree_items = []

        for item in changes:
            blob_response = await client.post(
                f"{base}/git/blobs",
                headers=headers,
                json={
                    "content": item["content"],
                    "encoding": "utf-8",
                },
            )
            blob_response.raise_for_status()
            blob_sha = blob_response.json()["sha"]

            tree_items.append({
                "path": item["path"],
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            })

        # Build a new tree on top of the existing tree so unrelated
        # files in the remote repository remain untouched.
        tree_response = await client.post(
            f"{base}/git/trees",
            headers=headers,
            json={
                "base_tree": base_tree_sha,
                "tree": tree_items,
            },
        )
        tree_response.raise_for_status()
        new_tree_sha = tree_response.json()["sha"]

        # Create a single commit whose parent is the reviewed head.
        commit_response = await client.post(
            f"{base}/git/commits",
            headers=headers,
            json={
                "message": message,
                "tree": new_tree_sha,
                "parents": [current_head],
            },
        )
        commit_response.raise_for_status()
        commit_sha = commit_response.json()["sha"]

        # Move the branch only if this is still a fast-forward update.
        # If another commit raced with us, GitHub should reject it.
        update_response = await client.patch(
            f"{base}/git/refs/heads/{quote(branch, safe='')}",
            headers=headers,
            json={
                "sha": commit_sha,
                "force": False,
            },
        )
        update_response.raise_for_status()

    return {
        "success": True,
        "repository": f"{owner}/{repo}",
        "branch": branch,
        "commit_sha": commit_sha,
        "commit_url": (
            f"https://github.com/{owner}/{repo}/commit/{commit_sha}"
        ),
        "files_published": len(changes),
    }
