
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
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from web.project_records import list_projects_for_chat
from connectors.github.auth import (
    build_authorization_url,
    generate_oauth_state,
    generate_pkce_pair,
    save_oauth_transaction,
    consume_oauth_transaction,
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
        # Do not expose credentials or internal exception details.
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

        answer = await agent.run_task(
            task,
            preview_base_url=(
                os.getenv("RENDER_EXTERNAL_URL")
                or str(http_request.base_url)
            ),
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

                answer = await agent.run_task(
                    agent_task,
                    on_progress=on_progress,
                    preview_base_url=(
                        os.getenv("RENDER_EXTERNAL_URL")
                        or str(http_request.base_url)
                    ),
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
