
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
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


# Load environment variables
load_dotenv()

app = FastAPI(title="Entropy")

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
        answer = await agent.run_task(task)

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
