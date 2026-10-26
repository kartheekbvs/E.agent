
import asyncio
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
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
from web.workspace import (
    clear_workspace,
    list_workspace_files,
    resolve_workspace_file,
    read_workspace_file,
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
# Agent task endpoints
# -------------------------

@app.post(
    "/task",
    dependencies=[Depends(require_login)],
)
async def receive_task(request: TaskRequest):
    task = request.task.strip()

    if not task:
        return {
            "status": "error",
            "message": "Task cannot be empty.",
        }

    try:
        agent = MCPToolDiscovery()
        answer = await agent.run_task(
            task,
            preview_base_url=(
                os.getenv("RENDER_EXTERNAL_URL")
                or str(request.base_url)
            ),
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
async def stream_task(request: TaskRequest):
    task = request.task.strip()

    async def event_stream():
        # Reject empty tasks before starting the agent.
        if not task:
            event = {
                "type": "error",
                "message": "Task cannot be empty.",
            }

            yield f"data: {json.dumps(event)}\n\n"
            return

        queue = asyncio.Queue()

        # Called by the agent whenever it reports progress.


        def on_progress(message):
            if isinstance(message, dict) and message.get("type") == "preview":
                queue.put_nowait(message)
            else:
                queue.put_nowait({
            "type": "activity",
            "message": str(message),
        })


        # Run the agent separately so its progress can be streamed.
        async def run_agent():
            try:
                agent = MCPToolDiscovery()

                answer = await agent.run_task(
                    task,
                    on_progress=on_progress,
                    preview_base_url=(
                        os.getenv("RENDER_EXTERNAL_URL")
                        or str(request.base_url)
                    ),
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
                # Signal that the stream has finished.
                await queue.put(None)

        agent_task = asyncio.create_task(run_agent())

        try:
            while True:
                event = await queue.get()

                if event is None:
                    break

                # Send one JSON event to the browser.
                yield f"data: {json.dumps(event)}\n\n"

        finally:
            # Avoid leaving the agent running if the client disconnects.
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
    project = PROJECTS.get(project_id)
    if project is None or project["process"].poll() is not None:
        raise HTTPException(
            status_code=404,
            detail="Project preview is not running.",
        )

    preview_prefix = f"/preview/{project_id}"
    upstream_path = preview_path.lstrip("/")
    upstream_url = (
        f"http://127.0.0.1:{project['port']}/"
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
            detail=f"Could not reach project preview: {error}",
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
            (lower_name.encode("latin-1"), value.encode("latin-1"))
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
def get_workspace_files():
    return {
        "files": list_workspace_files(),
    }


@app.get(
    "/api/file",
    dependencies=[Depends(require_login)],
)
def get_workspace_file(path: str):
    try:
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
