try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as FastMCP

from pathlib import Path
from itertools import islice
import shlex
import subprocess
import json
import shutil
import tempfile
import uuid
import os
import re
import sys
import asyncio
import time

from typing import Literal

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from model.multi_agent import MultiAgent
from planner.planner import Planner
mcp = FastMCP("ENTROPY MCP")
planner = Planner()
WORKSPACE = ROOT_DIR / "workspace"
WORKSPACE.mkdir(parents = True,exist_ok = True)
child_agent = MultiAgent()

@mcp.tool()
def create_plan(goal: str) -> dict:
    """Create a plan for a coding or research goal."""
    plan = planner.create_plan(goal)
    return planner.plan_to_dict(plan.plan_id)

@mcp.tool()
def add_task(
    plan_id: str,
    description: str,
    dependencies: list[str] | None = None,
) -> dict:
    """Add a task to a plan, optionally depending on existing tasks."""
    task = planner.add_task(
        plan_id=plan_id,
        description=description,
        dependencies=dependencies,
    )

    return planner.task_to_dict(task)

@mcp.tool()
def get_plan(plan_id: str) -> dict:
    """Get a plan and all its tasks with their current statuses."""
    return planner.plan_to_dict(plan_id)

@mcp.tool()
def get_next_task(plan_id: str) -> dict:
    """Get the next pending task whose dependencies are completed."""
    task = planner.get_next_task(plan_id)

    if task is None:
        return {
            "task": None,
            "message": "No task is currently ready to execute.",
        }

    return {
        "task": planner.task_to_dict(task),
        "message": "Next executable task found.",
    }

    def update_task(
        self,
        plan_id: str,
        task_id: str,
        status: TaskStatus,
        result: str | None = None,
    ) -> Task:
        plan = self.get_plan(plan_id)

        if plan.status != "active":
            raise ValueError("Cannot update a task in an inactive plan.")

        task = next(
            (item for item in plan.tasks if item.task_id == task_id),
            None,
        )

        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        allowed_statuses = {
            "pending",
            "in_progress",
            "completed",
            "failed",
            "blocked",
        }
        if status not in allowed_statuses:
            raise ValueError(f"Invalid task status: {status}")

        if status == "in_progress":
            if task.status != "pending":
                raise ValueError("Only pending tasks can be started.")

            for dependency_id in task.dependencies:
                dependency = next(
                    item for item in plan.tasks
                    if item.task_id == dependency_id
                )
                if dependency.status != "completed":
                    raise ValueError(
                        f"Dependency is not complete: {dependency_id}"
                    )

        elif status == "completed":
            if task.status != "in_progress":
                raise ValueError(
                    "Only in-progress tasks can be completed."
                )

            if result is None or not result.strip():
                raise ValueError(
                    "A non-empty result is required to complete a task."
                )

        elif status in {"failed", "blocked"}:
            if task.status != "in_progress":
                raise ValueError(
                    "Only in-progress tasks can be failed or blocked."
                )

        elif status == "pending":
            raise ValueError(
                "Tasks cannot be reset to pending through update_task."
            )

        task.status = status

        if result is not None:
            task.result = result.strip()

        self._refresh_plan_status(plan)
        return task


def resolve_path(path: str) -> Path:
    if not path:
        raise ValueError("Path cannot be empty")

    if WORKSPACE.is_symlink():
        raise ValueError("Workspace cannot be a symbolic link")

    root = WORKSPACE.resolve()
    requested_path = Path(path)

    if requested_path.is_absolute() or ".." in requested_path.parts:
        raise ValueError("Invalid workspace path")

    current_path = WORKSPACE
    for part in requested_path.parts:
        current_path = current_path / part
        if current_path.is_symlink():
            raise ValueError("Symbolic links are not allowed")

    resolved_path = (WORKSPACE / requested_path).resolve()
    try:
        resolved_path.relative_to(root)
    except ValueError as error:
        raise ValueError("Path is outside the workspace") from error

    return resolved_path

PROJECTS = {}

def wait_for_server(
    host: str,
    port: int,
    timeout: float = 300.0,
    poll_interval: float = 0.2,
) -> bool:
    import socket
    import time

    """Wait until a server accepts TCP connections.

    A preview server may take a little while to compile assets or boot a dev
    dependency chain, so the default lifecycle is longer than a quick startup
    check. The caller can still override this for faster local validation.
    """
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (host, port),
                timeout=0.5,
            ):
                return True
        except OSError:
            time.sleep(poll_interval)

    return False


def get_public_preview_base_url() -> str:
    """Return the public base URL for preview links.

    In local development we keep loopback URLs. In deployed or proxied setups we
    prefer explicit public host env vars, including custom domains such as
    https://entropy.twss.shop, and otherwise fall back to localhost only for local
    debugging.
    """
    for env_name in (
        "PUBLIC_BASE_URL",
        "APP_PUBLIC_URL",
        "PUBLIC_URL",
        "APP_URL",
        "BASE_URL",
        "ENTROPY_PUBLIC_BASE_URL",
        "RENDER_EXTERNAL_URL",
        "RENDER_URL",
        "HOST",
        "APP_HOST",
        "DOMAIN",
        "ENTROPY_HOST",
    ):
        value = os.getenv(env_name)
        if value:
            value = value.strip().rstrip("/")
            if not value.startswith(("http://", "https://")):
                if value.startswith(("localhost", "127.0.0.1", "0.0.0.0")):
                    return f"http://{value}"
                return f"https://{value}"
            return value

    if (
        os.getenv("RENDER")
        or os.getenv("DYNO")
        or os.getenv("DEPLOYMENT_MODE") == "production"
    ):
        return "http://127.0.0.1"

    return "http://127.0.0.1"


def choose_port(requested_port: int) -> int:
    import socket

    """Select an available local port."""
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", requested_port))
        return port_socket.getsockname()[1]


def detect_project_command(port: int):
    """Detect common web project types and construct a startup command."""
    root = WORKSPACE.resolve()

    package_file = root / "package.json"

    # Node.js projects, including Vite and Next.js.
    if package_file.is_file():
        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"Could not read package.json: {error}")

        scripts = package.get("scripts", {})
        dependencies = {
            **package.get("dependencies", {}),
            **package.get("devDependencies", {}),
        }

        node = shutil.which("node")
        for script_name in ("start", "dev"):
            script = scripts.get(script_name, "")
            if node and isinstance(script, str) and re.fullmatch(
                r"\s*node(?:\.exe)?\s+[A-Za-z0-9_./-]+(?:\s+[A-Za-z0-9_./-]+)*\s*",
                script,
                re.IGNORECASE,
            ):
                # Avoid an npm wrapper that can exit while its preview child is still needed.
                return node, script.split()[1:], "node"

        npm = shutil.which("npm")
        if not npm:
            raise ValueError("npm is not installed or is not on PATH.")

        if "start" in scripts:
            return npm, ["run", "start"], "node"

        if "dev" in scripts:
            if "vite" in dependencies:
                args = [
                    "run", "dev", "--",
                    "--host", "127.0.0.1",
                    "--port", str(port),
                ]
            elif "next" in dependencies:
                args = [
                    "run", "dev", "--",
                    "--hostname", "127.0.0.1",
                    "--port", str(port),
                ]
            else:
                # Generic scripts may read PORT from their environment.
                args = ["run", "dev"]

            return npm, args, "node"

        raise ValueError(
            "package.json has no dev or start script."
        )

    # FastAPI projects with a conventional app variable.
    for filename in ("main.py", "app.py"):
        source_file = root / filename

        if not source_file.is_file():
            continue

        try:
            source = source_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue

        if "FastAPI" in source and "app" in source:
            module = source_file.stem

            if (root / "pyproject.toml").is_file() and shutil.which("uv"):
                return (
                    shutil.which("uv"),
                    [
                        "run", "uvicorn", f"{module}:app",
                        "--host", "127.0.0.1",
                        "--port", str(port),
                    ],
                    "python",
                )

            return (
                sys.executable,
                [
                    "-m", "uvicorn", f"{module}:app",
                    "--host", "127.0.0.1",
                    "--port", str(port),
                ],
                "python",
            )

    # Static HTML projects.
    if (root / "index.html").is_file():
        return (
            sys.executable,
            [
                "-m", "http.server", str(port),
                "--bind", "127.0.0.1",
                "--directory", str(root),
            ],
            "static",
        )

    # Go projects.
    if (root / "go.mod").is_file() and shutil.which("go"):
        return shutil.which("go"), ["run", "."], "go"

    # Rust projects.
    if (root / "Cargo.toml").is_file() and shutil.which("cargo"):
        return shutil.which("cargo"), ["run"], "rust"

    raise ValueError(
        "Could not automatically detect a supported web project. "
        "Supply an explicit command and arguments."
    )


def stop_process(process):
    """Stop a process and its child processes where supported."""
    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            subprocess.run(
                [
                    "taskkill", "/PID", str(process.pid),
                    "/T", "/F",
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            import signal
            os.killpg(process.pid, signal.SIGTERM)

        process.wait(timeout=5)

    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def _command_is_server_start(command: str) -> bool:
    """Reject shell loops that would start a preview or app server instead of testing code."""
    lowered = command.lower()
    patterns = (
        "npm run dev",
        "npm run start",
        "npm start",
        "node server",
        "node app",
        "python app.py",
        "python -m http.server",
        "python -m uvicorn",
        "uvicorn",
        "gunicorn",
        "flask run",
        "vite",
        "next dev",
        "next start",
        "npx vite",
        "live-server",
        "serve",
    )
    return any(pattern in lowered for pattern in patterns)


@mcp.tool()
def start_project(
    command: str = "auto",
    args: list[str] | None = None,
    port: int = 0,
    startup_timeout: float = 300.0,
) -> dict[str, str | int | bool]:
    """Start a web project only for previewing.

    The server is kept alive for a longer startup window so build/dev servers can
    finish booting before the tool returns. The default window is 300 seconds
    (5 minutes), enough for generated apps and local previews to initialize.

    Use this tool only when the goal is to launch a local preview server for the generated app.
    Do not use it for file search, code reading, or general testing commands.
    Use run_command only for read-only inspections or test execution; never to start a server.
    """
    args = args or []

    if port < 0 or port > 65535:
        return {
            "success": False,
            "error": "Port must be between 0 and 65535.",
        }

    root = WORKSPACE.resolve()

    if WORKSPACE.is_symlink():
        return {
            "success": False,
            "error": "Workspace cannot be a symbolic link.",
        }

    try:
        if port == 0:
            port = choose_port(0)
        else:
            # Fail early if the requested port is already occupied.
            choose_port(port)

        if command == "auto":
            executable, launch_args, project_type = (
                detect_project_command(port)
            )
        else:
            # Explicit commands support installed runtimes and tools.
            executable = shutil.which(command)

            if executable is None:
                return {
                    "success": False,
                    "error": (
                        f"Executable not found on PATH: {command}"
                    ),
                }

            launch_args = [
                str(arg).replace("{port}", str(port))
                for arg in args
            ]
            project_type = "custom"

        # Avoid duplicate tracked projects on the same port.
        for project in PROJECTS.values():
            running = project["process"].poll() is None

            if running and project["port"] == port:
                return {
                    "success": False,
                    "error": f"Port {port} is already in use.",
                }

        project_id = uuid.uuid4().hex[:12]

        environment = os.environ.copy()
        environment["PORT"] = str(port)
        environment["HOST"] = "127.0.0.1"
        environment["HOSTNAME"] = "127.0.0.1"

        # Keep runtime logs outside the generated project directory.
        log_handle = tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f"entropy-{project_id}-",
            suffix=".log",
            delete=False,
        )
        log_path = log_handle.name

        try:
            process_options = {
                "cwd": str(root),
                "env": environment,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
                "shell": False,
            }

            if os.name == "nt":
                # The MCP stdio process exits after each task.
                # Keep preview servers detached from its console.
                process_options["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                )
            else:
                process_options["start_new_session"] = True

            process = subprocess.Popen(
                [executable, *launch_args],
                **process_options,
            )

        except OSError as error:
            log_handle.close()
            return {
                "success": False,
                "error": f"Could not start project: {error}",
            }

        log_handle.flush()

        PROJECTS[project_id] = {
            "process": process,
            "port": port,
            "command": executable,
            "args": launch_args,
            "project_type": project_type,
            "log_path": log_path,
            "log_handle": log_handle,
            "startup_timeout": float(startup_timeout),
            "started_at": time.monotonic(),
        }

        # Wait until the server actually accepts connections.
        if not wait_for_server(
            "127.0.0.1",
            port,
            timeout=max(5.0, float(startup_timeout)),
        ):
            returncode = process.poll()

            stop_process(process)
            log_handle.flush()
            log_handle.close()

            try:
                with open(log_path, "rb") as log_file:
                    log_tail = log_file.read()[-4000:].decode(
                        "utf-8", errors="replace"
                    )
            except OSError:
                log_tail = ""

            PROJECTS.pop(project_id, None)

            return {
                "success": False,
                "project_id": project_id,
                "error": (
                    "The project did not become ready on the selected "
                    f"port. Exit code: {returncode}. "
                    f"Log tail: {log_tail}"
                ),
            }

        public_base = get_public_preview_base_url()
        preview_url = (
            f"{public_base}/preview/{project_id}/"
            if public_base != "http://127.0.0.1"
            else f"http://127.0.0.1:{port}/"
        )

        return {
            "success": True,
            "project_id": project_id,
            "pid": process.pid,
            "project_type": project_type,
            "port": port,
            "preview_url": preview_url,
            "startup_timeout": float(startup_timeout),
            "message": "Project server is accepting connections.",
        }

    except Exception as error:
        import traceback

        traceback.print_exc()

        return {
        "success": False,
        "error": f"{type(error).__name__}: {error}",
    }

@mcp.tool()
def get_project_status(project_id: str) -> dict[str, str | int | bool]:
    """Check whether a previously launched project is still running."""
    project = PROJECTS.get(project_id)

    if project is None:
        return {
            "success": False,
            "error": f"Unknown project_id: {project_id}",
        }

    process = project["process"]
    returncode = process.poll()

    if returncode is None:
        public_base = get_public_preview_base_url()
        preview_url = (
            f"{public_base}/preview/{project_id}/"
            if public_base != "http://127.0.0.1"
            else f"http://127.0.0.1:{project['port']}/"
        )

        return {
            "success": True,
            "running": True,
            "project_id": project_id,
            "pid": process.pid,
            "port": project["port"],
            "preview_url": preview_url,
            "startup_timeout": float(project.get("startup_timeout", 300.0)),
        }

    log_path = project.get("log_path", "")
    log_tail = ""

    if log_path:
        try:
            with open(log_path, "rb") as log_file:
                log_tail = log_file.read()[-4000:].decode(
                    "utf-8", errors="replace"
                )
        except OSError as error:
            log_tail = f"Could not read logs: {error}"

    return {
        "success": True,
        "running": False,
        "project_id": project_id,
        "returncode": returncode,
        "log_tail": log_tail,
    }


@mcp.tool()
def list_file(path: str = ".") -> list[str]:
    """List files in a directory."""
    try:
        list_path = resolve_path(path)
        if not list_path.exists():
            return [f"Error: Path does not exist: {path}"]
        if not list_path.is_dir():
            return [f"Error: Path is not a directory: {path}"]
        return sorted(entry.name for entry in list_path.iterdir())
    except (OSError, ValueError) as error:
        return [f"Error: {error}"]


@mcp.tool()
def read_file(path: str) -> str:
    """Read a file."""
    try:
        read_path = resolve_path(path)
        if not read_path.exists():
            return f"Error: Path does not exist: {path}"
        if not read_path.is_file():
            return f"Error: Path is not a file: {path}"
        return read_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as error:
        return f"Error reading file: {error}"


@mcp.tool()
def read_block(path: str, start_line: int, end_line: int) -> list[str]:
    """Read a block of lines from a file that reduces memory usage instead of reading the entire file."""
    try:
        read_path = resolve_path(path)
        if not read_path.exists():
            return [f"Error: Path does not exist: {path}"]
        if not read_path.is_file():
            return [f"Error: Path is not a file: {path}"]

        if start_line < 1 or end_line < start_line:
            return ["Error: Line numbers must be positive and end_line must not precede start_line"]
        with read_path.open(encoding="utf-8") as source_file:
            lines = list(islice(source_file, start_line - 1, end_line))
        if len(lines) != end_line - start_line + 1:
            return ["Error: Requested line range extends beyond the end of the file"]
        return lines
    except (OSError, UnicodeError, ValueError) as error:
        return [f"Error reading file: {error}"]


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write text content to a file"""
    try:
        file_path = resolve_path(path)
        if file_path.is_dir():
            return f"Error: Path is a directory: {path}"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Successfully wrote file: {path}"
    except (OSError, ValueError) as error:
        return f"Error writing file: {error}"


@mcp.tool()
def write_selected(path: str, start_line: int, end_line: int, content: str) -> dict:
    """Replace only a selected line range to minimize token and edit churn."""
    try:
        target = resolve_path(path)
        if target.is_dir():
            return {"success": False, "error": f"Path is a directory: {path}"}
        if start_line < 1 or end_line < start_line:
            return {"success": False, "error": "Invalid line range."}
        if not target.exists():
            return {"success": False, "error": f"File does not exist: {path}"}

        existing = target.read_text(encoding="utf-8").splitlines(keepends=True)
        start_index = max(0, start_line - 1)
        end_index = min(len(existing), end_line)
        replacement = content.splitlines(keepends=True)
        updated = existing[:start_index] + replacement + existing[end_index:]
        target.write_text("".join(updated), encoding="utf-8")

        return {
            "success": True,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "updated": True,
        }
    except (OSError, ValueError, UnicodeError) as error:
        return {"success": False, "error": str(error)}


@mcp.tool()
def read_memory(key: str = "default") -> dict:
    """Read a concise summary from the workspace cache to reduce re-reading context."""
    cache_dir = WORKSPACE / ".entropy-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    memory_path = cache_dir / f"{key}.txt"

    if not memory_path.exists():
        return {"success": True, "exists": False, "content": ""}

    return {
        "success": True,
        "exists": True,
        "content": memory_path.read_text(encoding="utf-8"),
    }


@mcp.tool()
def write_memory(key: str = "default", content: str = "") -> dict:
    """Persist a short project summary for future tasks and context reuse."""
    cache_dir = WORKSPACE / ".entropy-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    memory_path = cache_dir / f"{key}.txt"
    memory_path.write_text(content, encoding="utf-8")
    return {"success": True, "key": key, "path": str(memory_path)}


@mcp.tool()
def run_command(command: str, cwd: str = ".") -> dict[str, str | int]:
    """Run read-only or test commands in the workspace.

    This tool is for quick inspection and validation only, such as grep, rg, ls, pwd, cat,
    or test execution. It must not be used to launch preview servers, background apps, or dev
    processes. Use start_project only for preview startup; use run_command only for searching,
    checking, and testing code paths.
    """
    allowed = {
        "python",
        "python3",
        "python.exe",
        "pip",
        "pip3",
        "uv",
        "pytest",
        "py",
        "git",
        "ls",
        "pwd",
        "grep",
        "rg",
        "find",
        "cat",
    }

    try:
        command_parts = shlex.split(command)
    except ValueError as error:
        return {"success": False, "error": str(error), "returncode": -1}

    if not command_parts:
        return {"success": False, "error": "Command cannot be empty.", "returncode": -1}

    if any(part.lower() in {"bash", "sh", "powershell", "pwsh", "cmd"} for part in command_parts):
        return {"success": False, "error": "Shell wrappers are not allowed; use direct commands only.", "returncode": -1}

    blocked_tokens = {"&&", "||", ";", "|", ">", "<", "*", "$", "`", "cd"}
    if any(token.lower() in blocked_tokens for token in command_parts):
        return {"success": False, "error": "Command chaining, shell redirection, and cd are not allowed in run_command.", "returncode": -1}

    binary = Path(command_parts[0]).name
    if binary.lower() not in {item.lower() for item in allowed}:
        return {"success": False, "error": f"Command is not allowed: {binary}", "returncode": -1}

    if _command_is_server_start(command):
        return {"success": False, "error": "This tool cannot launch a web app or preview server. Use start_project for preview startup.", "returncode": -1}

    try:
        target_dir = resolve_path(cwd)
    except ValueError as error:
        return {"success": False, "error": str(error), "returncode": -1}

    try:
        result = subprocess.run(
            command_parts,
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": (result.stdout or "")[:4000],
            "stderr": (result.stderr or "")[:2000],
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"success": False, "error": str(error), "returncode": -1}


@mcp.tool()
async def multi_agent(
    task: str,
    context: dict | None = None,
    effort: str = "medium",
    coding_level: str = "college",
) -> dict:
    """Delegate a specific task to Entropy's separate child LLM.

    Supply the task, relevant context, effort, and coding level.
    The child uses its own MCP server and restricted toolset,
    then returns its result to the main agent.
    """

    if not task.strip():
        return {
            "success": False,
            "error": "Task cannot be empty.",
        }

    if effort not in {"low", "medium", "max"}:
        return {
            "success": False,
            "error": "effort must be low, medium, or max.",
        }

    if coding_level not in {
        "basic", "medium", "college", "advanced"
    }:
        return {
            "success": False,
            "error": (
                "coding_level must be basic, medium, "
                "college, or advanced."
            ),
        }

    try:
        result = await child_agent.run_task(
            task=task,
            context=context or {},
            effort=effort,
            coding_level=coding_level,
        )

        return {
            "success": True,
            "task": task,
            "result": result,
        }

    except Exception as error:
        import traceback

        return {
        "success": False,
        "error": "".join(
            traceback.format_exception(error)
        ),
        "exception_type": type(error).__name__,
        "details": [
            {
                "type": type(inner).__name__,
                "message": str(inner),
            }
            for inner in getattr(error, "exceptions", [])
        ],
    }
