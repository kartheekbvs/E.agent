
import asyncio
import json
import os
import re
from pathlib import Path
from connectors.github.client import (
    list_repositories,
    get_repository_tree,
     read_repository_file,
)
import time

from web.github_publish_registry import (
    initialize_publish_registry,
    create_publish_review,
    consume_publish_review,
    get_publish_review,
)
import logging
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from web.project_records import list_projects_for_chat
from connectors.github.client import (
    list_repositories,
    get_repository_tree,
    read_repository_file,
    get_branch_head,
    publish_file_changes,
)
from connectors.github.auth import (
    generate_oauth_state,
    generate_pkce_pair,
    save_oauth_transaction,
    consume_oauth_transaction,
    build_authorization_url,
    exchange_code_for_token,
    save_github_connection,
)
import httpx
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from client.client import MCPToolDiscovery
from web.auth import (
    get_user_id,
    initialize_database,
    login_user,
    register_user,
)
from web.preview_registry import get_preview
from web.project_isolation import (
    create_project_runtime,
    get_project_runtime,
)
from web.project_sync import restore_project, save_project
from web.workspace import (
    clear_workspace,
    list_workspace_files,
    resolve_workspace_file,
    read_workspace_file,
)
from web.project_records import (
    create_chat,
    create_project,
    get_project_for_user,
    list_chats_for_user,
    list_projects_for_chat,
    update_chat_title,
)
from server.server import PROJECTS


# Load environment variables
load_dotenv()

app = FastAPI(title="Entropy")


def _request_public_base_url(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto")
    forwarded_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    request_base = (
        f"{forwarded_proto}://{forwarded_host}"
        if forwarded_proto and forwarded_host
        else str(request.base_url).rstrip("/")
    )

    if "localhost" in request_base or "127.0.0.1" in request_base:
        return request_base

    configured = (
        os.getenv("PUBLIC_BASE_URL")
        or os.getenv("APP_PUBLIC_URL")
        or os.getenv("RENDER_EXTERNAL_URL")
    )
    if configured:
        return configured.rstrip("/")

    return request_base


def _rewrite_preview_urls(
    content: bytes,
    content_type: str,
    preview_prefix: str,
) -> bytes:
    if not any(
        content_type.startswith(media_type)
        for media_type in (
            "text/html",
            "text/css",
            "application/javascript",
            "text/javascript",
        )
    ):
        return content

    text = content.decode("utf-8", errors="replace")

    if content_type.startswith("text/html"):
        text = re.sub(
            r'(\b(?:href|src|action|poster)\s*=\s*["\'])/(?!/)',
            rf"\1{preview_prefix}/",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"(<script\b[^>]*>)([\s\S]*?)(</script\s*>)",
            lambda match: (
                match.group(1)
                + _rewrite_javascript_urls(match.group(2), preview_prefix)
                + match.group(3)
            ),
            text,
            flags=re.IGNORECASE,
        )

    if content_type.startswith("text/css"):
        text = re.sub(
            r"url\(\s*/(?!/)",
            f"url({preview_prefix}/",
            text,
            flags=re.IGNORECASE,
        )

    if "javascript" in content_type:
        text = _rewrite_javascript_urls(text, preview_prefix)

    return text.encode("utf-8")


def _rewrite_javascript_urls(text: str, preview_prefix: str) -> str:
    return re.sub(
        r"""(["'`])/(?!/)([^"'`\s]*)\1""",
        lambda match: (
            f"{match.group(1)}{preview_prefix}/"
            f"{match.group(2)}{match.group(1)}"
        ),
        text,
    )

# Configure session authentication
session_secret = os.getenv("SESSION_SECRET")

if not session_secret:
    raise RuntimeError("SESSION_SECRET is missing from the .env file")

app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie="entropy_session",
    max_age=60 * 60 * 24,
    same_site="lax",
    https_only=os.getenv("SESSION_COOKIE_SECURE", "").lower()
    in {"1", "true", "yes"},
)
class GitHubPublishPreviewRequest(BaseModel):
    project_id: str
    repository: str
    branch: str | None = None
class GitHubPublishConfirmRequest(BaseModel):
    review_id: str
    confirmed: bool
    commit_message: str = "Publish changes from Entropy"

# Request models

class TaskRequest(BaseModel):
    task: str
    effort: str = "medium"
    coding_level: str = "college"
    github_repository: dict | None = None
    project_id: str | None = None

class AuthRequest(BaseModel):
    email: str
    password: str


# Initialize the user database
initialize_database()

initialize_publish_registry()
# Authentication dependency
def require_login(request: Request):
    user_id = request.session.get("user_id")

    if user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Please log in.",
        )

    return user_id


def prepare_project_runtime(project_id: str, user_id: str | int) -> dict:
    """Verify ownership, create the isolated runtime, and restore its files."""
    try:
        project = get_project_for_user(
            user_id=user_id,
            project_id=project_id,
        )
    except PermissionError as error:
        raise HTTPException(
            status_code=404,
            detail="Project not found.",
        ) from error

    try:
        runtime = get_project_runtime(project["id"])
    except FileNotFoundError:
        runtime = create_project_runtime(project["id"])

    # Restoration must succeed before the workspace is considered ready.
    # If Supabase restoration fails, propagate the error instead of treating
    # the workspace as a new project.
    restore_project(
        user_id=user_id,
        project_id=project["id"],
    )
    workspace_path = Path(runtime["workspace_dir"])
    if workspace_path.is_symlink() or not workspace_path.is_dir():
        raise RuntimeError("Project workspace is missing or invalid.")
    return {
        "project": project,
        "runtime": runtime,
        "workspace_path": workspace_path,
    }


# -------------------------
# Authentication endpoints
# -------------------------

@app.post("/api/auth/signup")
async def signup(data: AuthRequest, request: Request):
    success, message = register_user(data.email, data.password)

    if not success:
        return {"success": False, "message": message}

    user_id = get_user_id(data.email)
    request.session["user_id"] = user_id

    return {"success": True, "message": message}


@app.post("/api/auth/login")
async def login(data: AuthRequest, request: Request):
    success, message = login_user(data.email, data.password)

    if not success:
        return {"success": False, "message": message}

    user_id = get_user_id(data.email)
    request.session["user_id"] = user_id

    return {"success": True, "message": message}


@app.post("/api/auth/logout")
async def logout(request: Request):
    request.session.clear()

    return {
        "success": True,
        "message": "Logged out successfully.",
    }


@app.get("/api/auth/me")
async def get_current_user(request: Request):
    user_id = request.session.get("user_id")

    if user_id is None:
        return {
            "authenticated": False,
            "message": "Please log in.",
        }

    return {
        "authenticated": True,
        "user_id": user_id,
    }

# -------------------------
# GitHub connector OAuth
# -------------------------

@app.get("/github/login")
async def github_login(request: Request):
    user_id = request.session.get("user_id")

    # GitHub connections belong to an authenticated Entropy user.
    if user_id is None:
        return RedirectResponse(url="/login", status_code=303)

    try:
        state = generate_oauth_state()
        verifier, challenge = generate_pkce_pair()

        # Bind the OAuth transaction to this browser session.
        browser_nonce = generate_oauth_state()
        request.session["github_oauth_nonce"] = browser_nonce

        save_oauth_transaction(
            state=state,
            verifier=verifier,
            user_id=user_id,
            browser_nonce=browser_nonce,
        )

        authorization_url = build_authorization_url(
            state=state,
            challenge=challenge,
        )

        return RedirectResponse(
            url=authorization_url,
            status_code=302,
        )

    except Exception:
        logging.exception("Failed to start GitHub OAuth")
        raise HTTPException(
        status_code=500,
        detail="Could not start GitHub authorization.",
    )


@app.get("/github/callback")
async def github_callback(request: Request):
    # Handle authorization denial or cancellation.
    if request.query_params.get("error"):
        request.session.pop("github_oauth_nonce", None)
        return {"success": False, "message": "GitHub authorization was cancelled or denied."}

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    browser_nonce = request.session.pop("github_oauth_nonce", None)

    if not code or not state or not browser_nonce:
        raise HTTPException(
            status_code=400,
            detail="Missing OAuth callback parameters or session.",
        )

    transaction = consume_oauth_transaction(state, browser_nonce)
    if transaction is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid, expired, or reused OAuth state.",
        )

    try:
        token_data = await exchange_code_for_token(
            code=code,
            code_verifier=transaction["verifier"],
        )

        access_token = token_data["access_token"]

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            github_user = response.json()

        # Persist the encrypted token against the Entropy user
        # associated with the validated OAuth transaction.
        save_github_connection(
            user_id=transaction["user_id"],
            github_user=github_user,
            token_data=token_data,
        )

        # Store only non-sensitive identity details in the session.
        request.session["github_user"] = {
            "id": github_user["id"],
            "login": github_user["login"],
        }

        return {
            "success": True,
            "message": "GitHub connected successfully.",
            "github_user": request.session["github_user"],
        }

    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="Could not complete GitHub authorization.",
        )

# -------------------------
# Chat and project endpoints
# -------------------------

class CreateChatRequest(BaseModel):
    title: str = "New Chat"


class CreateProjectRequest(BaseModel):
    chat_id: str
    summary: str | None = None


class UpdateChatTitleRequest(BaseModel):
    title: str


@app.get("/api/chats")
async def list_chats_api(request: Request):
    user_id = require_login(request)
    try:
        chats = list_chats_for_user(user_id)
        return {
            "success": True,
            "chats": chats,
        }
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Could not load chat history.",
        ) from error


@app.post("/api/chats")
async def create_chat_api(
    data: CreateChatRequest,
    request: Request,
):
    user_id = require_login(request)

    try:
        chat = create_chat(user_id=user_id, title=data.title)
        return {"success": True, "chat": chat}

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Could not create chat.",
        ) from error


@app.post("/api/github/publish/confirm")
async def confirm_github_publish(
    data: GitHubPublishConfirmRequest,
    request: Request,
):
    user_id = require_login(request)

    if data.confirmed is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit confirmation is required before publishing.",
        )

    commit_message = data.commit_message.strip()
    if not commit_message or len(commit_message) > 200:
        raise HTTPException(
            status_code=400,
            detail="Commit message must contain 1–200 characters.",
        )

    # Consume the approval atomically. Reviews are user-bound,
    # expire after 30 minutes, and can only be used once.
    review = consume_publish_review(
        review_id=data.review_id,
        owner_id=user_id,
        now=int(time.time()),
    )

    if review is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This review is invalid, expired, or already used. "
                "Generate a new publishing preview."
            ),
        )

    repository = review["repository"]
    parts = repository.split("/")

    if len(parts) != 2 or not all(parts):
        raise HTTPException(
            status_code=400,
            detail="The saved review contains an invalid repository.",
        )

    owner, repo = parts

    # Validate the saved snapshot before contacting GitHub.
    changes = review["changes"]
    if not isinstance(changes, list) or not changes:
        raise HTTPException(
            status_code=400,
            detail="The saved review contains no publishable changes.",
        )

    publish_changes = []
    seen_paths = set()

    for item in changes:
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail="The saved review contains an invalid change.",
            )

        path = item.get("path")
        content = item.get("content")
        status = item.get("status")

        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise HTTPException(
                status_code=400,
                detail="The saved review contains an invalid file path.",
            )

        path_parts = path.split("/")
        filename = path_parts[-1].lower()

        if (
            any(part.lower() in PUBLISH_EXCLUDED_DIRS for part in path_parts)
            or filename in PUBLISH_EXCLUDED_NAMES
            or filename.startswith(".env.")
            or filename.endswith(tuple(PUBLISH_EXCLUDED_SUFFIXES))
            or any(
                marker in filename
                for marker in (
                    "credentials",
                    "credential",
                    "secret",
                    "private_key",
                    "private-key",
                )
            )
        ):
            raise HTTPException(
                status_code=400,
                detail="The saved review contains a protected file path.",
            )

        if path in seen_paths:
            raise HTTPException(
                status_code=400,
                detail="The saved review contains duplicate file paths.",
            )

        if status not in {"added", "modified"}:
            raise HTTPException(
                status_code=400,
                detail="The saved review contains an unsupported change type.",
            )

        if not isinstance(content, str) or "\x00" in content:
            raise HTTPException(
                status_code=400,
                detail="The saved review contains invalid file content.",
            )

        if len(content.encode("utf-8")) > 1_000_000:
            raise HTTPException(
                status_code=400,
                detail="A reviewed file exceeds the 1 MB publishing limit.",
            )

        seen_paths.add(path)
        publish_changes.append({
            "path": path,
            "content": content,
        })

    try:
        # Confirm that the project still belongs to this user.
        project_context = prepare_project_runtime(
            project_id=review["project_id"],
            user_id=user_id,
        )

        remote_tree = await get_repository_tree(
            user_id=user_id,
            owner=owner,
            repo=repo,
            branch=review["branch"],
        )

        if remote_tree.get("truncated"):
            raise HTTPException(
                status_code=409,
                detail="GitHub returned an incomplete tree. Generate a new preview.",
            )

        if remote_tree["commit_sha"] != review["base_head_sha"]:
            raise HTTPException(
                status_code=409,
                detail="The GitHub branch changed. Generate a new preview.",
            )

        _validate_publish_snapshot(
            workspace_path=project_context["workspace_path"],
            reviewed_changes=changes,
            remote_tree=remote_tree,
        )

        # Recheck GitHub's current branch head before publishing.
        current_head = await get_branch_head(
            user_id=user_id,
            owner=owner,
            repo=repo,
            branch=review["branch"],
        )

        if current_head["commit_sha"] != review["base_head_sha"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The GitHub branch changed after the preview. "
                    "Generate and review a new preview before publishing."
                ),
            )

        result = await publish_file_changes(
            user_id=user_id,
            owner=owner,
            repo=repo,
            branch=review["branch"],
            expected_head_sha=review["base_head_sha"],
            changes=publish_changes,
            message=commit_message,
        )

        return {
            "success": True,
            "repository": result["repository"],
            "branch": result["branch"],
            "commit_sha": result["commit_sha"],
            "commit_url": result["commit_url"],
            "files_published": result["files_published"],
            "message": "Reviewed changes were published successfully.",
        }

    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(
            status_code=404,
            detail="Project not found.",
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=409,
            detail=str(error),
        ) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code

        if status_code in {401, 403, 404}:
            raise HTTPException(
                status_code=status_code,
                detail=(
                    "GitHub denied the publishing request. "
                    "Check repository access and permissions."
                ),
            ) from error

        raise HTTPException(
            status_code=502,
            detail="GitHub could not complete the publishing request.",
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to GitHub.",
        ) from error
    except Exception as error:
        logging.exception("GitHub publishing failed")
        raise HTTPException(
            status_code=500,
            detail="GitHub publishing failed unexpectedly.",
        ) from error

@app.patch("/api/chats/{chat_id}")
def rename_chat(
    chat_id: str,
    payload: UpdateChatTitleRequest,
    request: Request,
):
    user_id = require_login(request)
    try:
        chat = update_chat_title(
            user_id=user_id,
            chat_id=chat_id,
            title=payload.title,
        )
        return {"success": True, "chat": chat}
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/chats/{chat_id}/projects")
async def list_projects_for_chat_api(
    chat_id: str,
    request: Request,
):
    user_id = require_login(request)
    try:
        projects = list_projects_for_chat(
            user_id=user_id,
            chat_id=chat_id,
        )
        return {
            "success": True,
            "projects": projects,
        }
    except PermissionError as error:
        raise HTTPException(
            status_code=404,
            detail="Chat not found.",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Could not load projects.",
        ) from error

import hashlib


PUBLISH_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

PUBLISH_EXCLUDED_NAMES = {
    ".env",
    "memory.entropy",
}

PUBLISH_EXCLUDED_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite",
    ".db",
}


def _publishable_workspace_files(workspace_path: Path) -> dict[str, str]:
    """Read safe, UTF-8 text files from one project's workspace."""
    files = {}

    root = workspace_path.resolve(strict=True)

    for path in root.rglob("*"):
        # Never follow or publish symbolic links.
        if path.is_symlink():
            continue

        if not path.is_file():
            continue

        relative = path.relative_to(root)
        parts = relative.parts
        filename = path.name.lower()

        if any(part.lower() in PUBLISH_EXCLUDED_DIRS for part in parts):
            continue

        if filename in PUBLISH_EXCLUDED_NAMES:
            continue

        if filename.startswith(".env."):
            continue

        if filename.endswith(tuple(PUBLISH_EXCLUDED_SUFFIXES)):
            continue

        # Exclude common private-key and credential files.
        if any(
            marker in filename
            for marker in (
                "credentials",
                "credential",
                "secret",
                "private_key",
                "private-key",
            )
        ):
            continue

        if path.stat().st_size > 1_000_000:
            continue

        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        if "\x00" in content:
            continue

        files[relative.as_posix()] = content

    return files


def _git_blob_sha(content: str) -> str:
    """Calculate Git's SHA-1 for a UTF-8 file blob."""
    encoded = content.encode("utf-8")
    header = f"blob {len(encoded)}\0".encode("ascii")
    return hashlib.sha1(header + encoded).hexdigest()

def _validate_publish_snapshot(
    workspace_path: Path,
    reviewed_changes: list[dict],
    remote_tree: dict,
) -> None:
    """Verify the complete local change set still matches the review."""
    current_files = _publishable_workspace_files(workspace_path)
    remote_files = {
        item["path"]: item["sha"]
        for item in remote_tree["entries"]
        if item["type"] == "blob"
    }
    current_changes = {}
    for path, content in current_files.items():
        local_sha = _git_blob_sha(content)
        remote_sha = remote_files.get(path)
        if remote_sha is None:
            status = "added"
        elif remote_sha != local_sha:
            status = "modified"
        else:
            continue
        current_changes[path] = {
            "path": path,
            "status": status,
            "content": content,
        }
    reviewed = {
        item["path"]: {
            "path": item["path"],
            "status": item["status"],
            "content": item["content"],
        }
        for item in reviewed_changes
    }
    if current_changes != reviewed:
        raise HTTPException(
            status_code=409,
            detail=(
                "The project's files or changes differ from the reviewed "
                "snapshot. Generate a new preview and review it again."
            ),
        )



@app.post("/api/github/publish/preview")
async def preview_github_publish(
    data: GitHubPublishPreviewRequest,
    request: Request,
):
    user_id = require_login(request)

    repository = data.repository.strip()
    parts = repository.split("/")

    if (
        len(parts) != 2
        or not all(parts)
        or any(part in {".", ".."} for part in parts)
    ):
        raise HTTPException(
            status_code=400,
            detail="Repository must use the owner/repo format.",
        )

    owner, repo = parts

    try:
        project_context = prepare_project_runtime(
            project_id=data.project_id,
            user_id=user_id,
        )
        workspace_path = project_context["workspace_path"]

        remote_tree = await get_repository_tree(
            user_id=user_id,
            owner=owner,
            repo=repo,
            branch=data.branch,
        )

        if remote_tree.get("truncated"):
            raise HTTPException(
                status_code=502,
                detail="GitHub returned an incomplete tree. Preview was stopped.",
            )

        head = await get_branch_head(
            user_id=user_id,
            owner=owner,
            repo=repo,
            branch=remote_tree["branch"],
        )
        if remote_tree["commit_sha"] != head["commit_sha"]:
            raise HTTPException(
        status_code=409,
        detail=(
            "The GitHub branch changed while preparing the preview. "
            "Please generate the preview again."
        ),
    )
        local_files = _publishable_workspace_files(workspace_path)

        remote_files = {
            item["path"]: item["sha"]
            for item in remote_tree["entries"]
            if item["type"] == "blob"
        }

        # Keep file contents in the server-side review only.
        review_changes = []
        public_changes = []

        for path, content in sorted(local_files.items()):
            local_sha = _git_blob_sha(content)
            remote_sha = remote_files.get(path)

            if remote_sha is None:
                status = "added"
            elif remote_sha != local_sha:
                status = "modified"
            else:
                continue

            review_changes.append({
                "path": path,
                "status": status,
                "content": content,
            })

            public_changes.append({
                "path": path,
                "status": status,
                "content": content,
            })

        # Missing local files do not imply remote deletions.
        review_id = None

        if review_changes:
            review_id = create_publish_review(
                owner_id=user_id,
                project_id=project_context["project"]["id"],
                repository=remote_tree["repository"],
                branch=remote_tree["branch"],
                base_head_sha=head["commit_sha"],
                changes=review_changes,
                created_at=int(time.time()),
            )

        return {
            "success": True,
            "project_id": project_context["project"]["id"],
            "repository": remote_tree["repository"],
            "branch": remote_tree["branch"],
            "base_head_sha": head["commit_sha"],
            "review_id": review_id,
            "changes": public_changes,
            "change_count": len(public_changes),
            "message": (
                "Review snapshot saved. No GitHub changes have been published."
                if review_id
                else "No file changes to publish. No GitHub changes have been published."
            ),
        }

    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(
            status_code=404,
            detail="Project not found.",
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code

        if status_code in {401, 403, 404}:
            raise HTTPException(
                status_code=status_code,
                detail=(
                    "GitHub denied access to the repository or branch. "
                    "Check your connection and permissions."
                ),
            ) from error

        raise HTTPException(
            status_code=502,
            detail="GitHub failed to prepare the publishing preview.",
        ) from error
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to GitHub.",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Could not prepare the publishing preview.",
        ) from error


@app.post("/api/chats/{chat_id}/projects")
async def create_project_api(
    chat_id: str,
    data: CreateProjectRequest,
    request: Request,
):
    user_id = require_login(request)

    if data.chat_id != chat_id:
        raise HTTPException(
            status_code=400,
            detail="Chat ID in the URL and body must match.",
        )

    try:
        project = create_project(
            user_id=user_id,
            chat_id=chat_id,
            summary=data.summary,
        )
        return {"success": True, "project": project}

    except PermissionError as error:
        raise HTTPException(
            status_code=404,
            detail="Chat not found.",
        ) from error

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Could not create project.",
        ) from error


@app.get("/api/projects/{project_id}")
async def get_project_api(
    project_id: str,
    request: Request,
):
    user_id = require_login(request)

    try:
        project = get_project_for_user(
            user_id=user_id,
            project_id=project_id,
        )
        return {"success": True, "project": project}

    except PermissionError as error:
        raise HTTPException(
            status_code=404,
            detail="Project not found.",
        ) from error

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Could not load project.",
        ) from error


@app.post("/api/projects/{project_id}/resume")
async def resume_project_api(
    project_id: str,
    request: Request,
):
    user_id = require_login(request)
    try:
        project_context = prepare_project_runtime(
            project_id=project_id,
            user_id=user_id,
        )
        project = project_context["project"]
        workspace_path = project_context["workspace_path"]
        memory_path = workspace_path / "memory.entropy"

        if memory_path.is_symlink():
            raise HTTPException(
                status_code=400,
                detail="Project memory cannot be a symbolic link.",
            )

        memory = ""
        if memory_path.is_file():
            memory = memory_path.read_text(encoding="utf-8")

        files = list_workspace_files(workspace_dir=workspace_path)

        return {
            "success": True,
            "project": project,
            "workspace_path": str(workspace_path),
            "memory": memory,
            "has_memory": bool(memory.strip()),
            "has_files": bool(files),
            "file_count": len(files),
        }
    except PermissionError as error:
        raise HTTPException(
            status_code=404,
            detail="Project not found.",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Could not resume project.",
        ) from error


# -------------------------
# Agent task endpoints
# -------------------------

@app.post(
    "/task",
    dependencies=[Depends(require_login)],
)
async def receive_task(
    http_request: Request,
    task_request: TaskRequest,
):
    task = task_request.task
    if not task.strip():
        return {
            "status": "error",
            "message": "Task cannot be empty.",
        }

    user_id = require_login(http_request)
    project_id = task_request.project_id
    project_context = None

    try:
        if project_id:
            project_context = prepare_project_runtime(
                project_id=project_id,
                user_id=user_id,
            )
            runtime = project_context["runtime"]
            agent = MCPToolDiscovery(
                project_id=project_context["project"]["id"],
                owner_id=user_id,
                workspace_path=runtime["workspace_dir"],
            )
        else:
            agent = MCPToolDiscovery()

        public_base_url = _request_public_base_url(http_request)

        answer = await agent.run_task(
            task,
            preview_base_url=public_base_url,
            effort=task_request.effort,
            coding_level=task_request.coding_level,
        )

        if project_context:
            save_project(
                user_id=user_id,
                project_id=project_context["project"]["id"],
            )

        return {
            "status": "success",
            "task": task,
            "answer": answer,
        }

    except Exception as error:
        return {
            "status": "error",
            "message": str(error),
        }


@app.post(
    "/task/stream",
    dependencies=[Depends(require_login)],
)
async def stream_task(
    http_request: Request,
    task_request: TaskRequest,
):
    task = task_request.task

    async def event_stream():
        if not task.strip():
            yield f"data: {json.dumps({
                'type': 'error',
                'message': 'Task cannot be empty.',
            })}\n\n"
            return

        queue = asyncio.Queue()

        def on_progress(message):
            if isinstance(message, dict) and message.get("type") == "preview":
                queue.put_nowait(message)
            else:
                queue.put_nowait({
                    "type": "activity",
                    "message": str(message),
                })

        async def run_agent():
            try:
                user_id = require_login(http_request)
                project_context = None
                project_id = task_request.project_id

                if project_id:
                    project_context = prepare_project_runtime(
                        project_id=project_id,
                        user_id=user_id,
                    )
                    runtime = project_context["runtime"]
                    agent = MCPToolDiscovery(
                        project_id=project_context["project"]["id"],
                        owner_id=user_id,
                        workspace_path=runtime["workspace_dir"],
                    )
                else:
                    agent = MCPToolDiscovery()

                agent_task = task
                memory_summary = ""

                if project_context:
                    workspace_dir = project_context["runtime"]["workspace_dir"]
                    memory_path = Path(workspace_dir) / "memory.entropy"
                    if memory_path.is_file() and not memory_path.is_symlink():
                        memory_summary = memory_path.read_text(encoding="utf-8").strip()

                repository = task_request.github_repository
                if repository:
                    full_name = repository.get("full_name")
                    default_branch = repository.get("default_branch")

                    if full_name:
                        agent_task = (
                            f"{task}\n\n"
                            "GitHub repository context:\n"
                            f"Repository: {full_name}\n"
                            f"Default branch: {default_branch or 'unknown'}\n"
                            "The repository is remote. Do not assume its files exist "
                            "in the local workspace. Use available GitHub tools to "
                            "inspect remote files when needed."
                        )

                if memory_summary:
                    agent_task = (
                        f"{agent_task}\n\nProject memory summary:\n{memory_summary}"
                    )

                public_base_url = _request_public_base_url(http_request)

                answer = await agent.run_task(
                    agent_task,
                    on_progress=on_progress,
                    preview_base_url=public_base_url,
                    effort=task_request.effort,
                    coding_level=task_request.coding_level,
                )

                if project_context:
                    save_project(
                        user_id=user_id,
                        project_id=project_context["project"]["id"],
                    )

                await queue.put({
                    "type": "result",
                    "status": "success",
                    "answer": answer,
                })

            except Exception as error:
                await queue.put({
                    "type": "error",
                    "message": str(error),
                })

            finally:
                await queue.put(None)

        agent_task = asyncio.create_task(run_agent())

        try:
            while True:
                event = await queue.get()

                if event is None:
                    break

                yield f"data: {json.dumps(event)}\n\n"

        finally:
            if not agent_task.done():
                agent_task.cancel()

            await asyncio.gather(
                agent_task,
                return_exceptions=True,
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# -------------------------
# Pages
# -------------------------

@app.get("/")
async def home(request: Request):
    if request.session.get("user_id") is None:
        return RedirectResponse(
            url="/login",
            status_code=303,
        )

    index_file = (
        Path(__file__).resolve().parent.parent
        / "templates"
        / "index.html"
    )

    return FileResponse(index_file)


@app.get("/workspace")
@app.get("/workspace/{file_path:path}")
async def workspace_preview(request: Request, file_path: str = ""):
    if request.session.get("user_id") is None:
        return RedirectResponse(
            url="/login",
            status_code=303,
        )

    preview_path = file_path or "index.html"

    try:
        workspace_file = resolve_workspace_file(preview_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Workspace preview file not found.",
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    return FileResponse(workspace_file)


@app.get("/login")
async def login_page():
    auth_file = (
        Path(__file__).resolve().parent.parent
        / "templates"
        / "auth.html"
    )

    return FileResponse(auth_file)


@app.api_route(
    "/preview/{project_id}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    dependencies=[Depends(require_login)],
)
@app.api_route(
    "/preview/{project_id}/{preview_path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    dependencies=[Depends(require_login)],
)
async def proxy_project_preview(
    request: Request,
    project_id: str,
    preview_path: str = "",
):
    user_id = request.session.get("user_id")

    preview = get_preview(project_id, user_id)
    if preview is None:
        raise HTTPException(
            status_code=404,
            detail="Project preview is not running.",
        )

    try:
        get_project_for_user(user_id=user_id, project_id=preview["project_id"])
    except PermissionError as error:
        raise HTTPException(
            status_code=404,
            detail="Project preview is not running.",
        ) from error

    preview_prefix = f"/preview/{project_id}"
    upstream_path = preview_path.lstrip("/")
    upstream_url = (
        f"http://127.0.0.1:{preview['port']}/"
        f"{upstream_path}"
    )
    hop_by_hop_headers = {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
    request_headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in hop_by_hop_headers
        and name.lower() != "accept-encoding"
    }

    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                upstream_url,
                params=request.query_params,
                content=await request.body(),
                headers=request_headers,
            )
    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail="Could not reach project preview.",
        ) from error

    content_type = upstream.headers.get("content-type", "")
    body = _rewrite_preview_urls(
        upstream.content,
        content_type,
        preview_prefix,
    )
    response = Response(
        content=b"" if request.method == "HEAD" else body,
        status_code=upstream.status_code,
    )

    excluded_response_headers = hop_by_hop_headers | {
        "content-encoding",
        "content-md5",
        "etag",
    }
    for name, value in upstream.headers.multi_items():
        lower_name = name.lower()
        if lower_name in excluded_response_headers:
            continue
        if lower_name == "location" and value.startswith("/"):
            value = f"{preview_prefix}{value}"
        elif lower_name == "set-cookie":
            value = re.sub(
                r";\s*Path=/($|;)",
                rf"; Path={preview_prefix}/\1",
                value,
                flags=re.IGNORECASE,
            )
        response.raw_headers.append(
            (
                lower_name.encode("latin-1"),
                value.encode("latin-1"),
            )
        )

    return response


@app.get("/health")
def get_health():
    return {
        "status": "ok",
        "app": "Entropy",
    }


# -------------------------
# Workspace endpoints
# -------------------------

@app.get(
    "/api/files",
    dependencies=[Depends(require_login)],
)
def get_workspace_files(
    request: Request,
    project_id: str | None = None,
):
    try:
        if project_id:
            user_id = require_login(request)
            project_context = prepare_project_runtime(
                project_id=project_id,
                user_id=user_id,
            )
            workspace_dir = project_context["runtime"]["workspace_dir"]
            return {
                "files": list_workspace_files(workspace_dir=workspace_dir),
            }

        return {
            "files": list_workspace_files(),
        }

    except PermissionError as error:
        raise HTTPException(
            status_code=403,
            detail=str(error),
        ) from error


@app.get(
    "/api/file",
    dependencies=[Depends(require_login)],
)
def get_workspace_file(
    request: Request,
    path: str,
    project_id: str | None = None,
):
    try:
        if project_id:
            user_id = require_login(request)
            project_context = prepare_project_runtime(
                project_id=project_id,
                user_id=user_id,
            )
            workspace_dir = project_context["runtime"]["workspace_dir"]
            content = read_workspace_file(path, workspace_dir=workspace_dir)
        else:
            content = read_workspace_file(path)

        return {
            "path": path,
            "content": content,
        }

    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="File not found.",
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    except PermissionError as error:
        raise HTTPException(
            status_code=403,
            detail=str(error),
        ) from error


@app.post(
    "/api/workspace/clear",
    dependencies=[Depends(require_login)],
)
def clear_workspace_api():
    try:
        clear_workspace()

        return {
            "status": "success",
            "message": "Workspace cleared.",
        }

    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Could not clear workspace.",
        )

@app.get(
    "/api/github/repos",
    dependencies=[Depends(require_login)],
)
async def get_github_repositories(
    request: Request,
    page: int = 1,
    per_page: int = 20,
):
    user_id = request.session.get("user_id")

    try:
        repositories = await list_repositories(
            user_id,
            page=page,
            per_page=per_page,
        )
        return {
            "success": True,
            "count": len(repositories),
            "page": page,
            "per_page": per_page,
            "repositories": repositories,
        }

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        if status_code == 403:
            raise HTTPException(
                status_code=403,
                detail="GitHub denied repository access. Check the app's permissions and repository installation.",
            ) from error

        raise HTTPException(
            status_code=502,
            detail="GitHub repository request failed.",
        ) from error

    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to GitHub.",
        ) from error

@app.get(
    "/api/github/repos/{owner}/{repo}/tree",
    dependencies=[Depends(require_login)],
)
async def get_github_repository_tree(
    request: Request,
    owner: str,
    repo: str,
    branch: str | None = None,
):
    user_id = request.session.get("user_id")

    try:
        return await get_repository_tree(
            user_id=user_id,
            owner=owner,
            repo=repo,
            branch=branch,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code

        if status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="Repository or branch not found, or access is unavailable.",
            ) from error

        if status_code == 401:
            raise HTTPException(
                status_code=401,
                detail="GitHub authorization expired or was revoked. Reconnect GitHub.",
            ) from error

        if status_code == 403:
            raise HTTPException(
                status_code=403,
                detail="GitHub denied access or rate-limited the request.",
            ) from error

        raise HTTPException(
            status_code=502,
            detail="GitHub repository tree request failed.",
        ) from error

    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to GitHub.",
        ) from error

@app.get(
    "/api/github/repos/{owner}/{repo}/file",
    dependencies=[Depends(require_login)],
)
async def get_github_repository_file(
    request: Request,
    owner: str,
    repo: str,
    path: str,
    branch: str | None = None,
):
    user_id = request.session.get("user_id")

    try:
        return await read_repository_file(
            user_id=user_id,
            owner=owner,
            repo=repo,
            path=path,
            branch=branch,
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error

    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code

        if status_code == 404:
            raise HTTPException(
                status_code=404,
                detail="Repository or file not found, or access denied.",
            ) from error

        if status_code == 403:
            raise HTTPException(
                status_code=403,
                detail="GitHub denied access to this file.",
            ) from error

        raise HTTPException(
            status_code=502,
            detail="GitHub file request failed.",
        ) from error

    except httpx.RequestError as error:
        raise HTTPException(
            status_code=502,
            detail="Could not connect to GitHub.",
        ) from error

@app.get("/api/chats/{chat_id}/projects")
async def list_chat_projects_api(
    chat_id: str,
    request: Request,
):
    user_id = require_login(request)

    try:
        projects = list_projects_for_chat(
            user_id=user_id,
            chat_id=chat_id,
        )

        return {
            "success": True,
            "projects": projects,
        }

    except PermissionError as error:
        raise HTTPException(
            status_code=404,
            detail="Chat not found.",
        ) from error

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Could not load chat projects.",
        ) from error

@app.get("/api/github/publish/review/{review_id}")
async def get_github_publish_review(
    review_id: str,
    request: Request,
):
    user_id = require_login(request)

    review = get_publish_review(
        review_id=review_id,
        owner_id=user_id,
        now=int(time.time()),
    )

    if review is None:
        raise HTTPException(
            status_code=404,
            detail="Review not found, expired, or already used.",
        )

    # Return only the details needed to inspect the proposed changes.
    public_changes = [
        {
            "path": item["path"],
            "status": item["status"],
            "content": item["content"],
        }
        for item in review["changes"]
    ]

    return {
        "success": True,
        "review_id": review["review_id"],
        "project_id": review["project_id"],
        "repository": review["repository"],
        "branch": review["branch"],
        "base_head_sha": review["base_head_sha"],
        "change_count": len(public_changes),
        "changes": public_changes,
        "message": "Review loaded for inspection. Nothing has been published.",
    }
