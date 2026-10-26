from mcp.server.fastmcp import FastMCP
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
mcp = FastMCP("ENTROPY MCP")

WORKSPACE = Path(__file__).resolve().parent.parent/"workspace"
WORKSPACE.mkdir(parents = True,exist_ok = True)


def resolve_path(path: str) -> Path:
    requested_path = (WORKSPACE / path).resolve()
    if not requested_path.is_relative_to(WORKSPACE):
        raise ValueError("Path is outside the workspace")
    return requested_path

PROJECTS = {}

def wait_for_server(
    host: str,
    port: int,
    timeout: float = 20.0,
) -> bool:
    import socket
    import time

    """Wait until a server accepts TCP connections."""
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (host, port),
                timeout=0.5,
            ):
                return True
        except OSError:
            time.sleep(0.2)

    return False


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


@mcp.tool()
def start_project(
    command: str = "auto",
    args: list[str] | None = None,
    port: int = 0,
) -> dict[str, str | int | bool]:
    """Start a web project from the workspace.

    Use command='auto' to detect common project types.
    Otherwise provide an installed executable and its arguments.
    Put {port} in an argument wherever the selected port is needed.
    The project must start a web server for preview readiness to succeed.
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
        }

        # Wait until the server actually accepts connections.
        if not wait_for_server("127.0.0.1", port, timeout=20):
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

        return {
            "success": True,
            "project_id": project_id,
            "pid": process.pid,
            "project_type": project_type,
            "port": port,
            "preview_url": f"http://127.0.0.1:{port}/",
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
        return {
            "success": True,
            "running": True,
            "project_id": project_id,
            "pid": process.pid,
            "port": project["port"],
            "preview_url": f"http://127.0.0.1:{project['port']}/",
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


# @mcp.tool()
# def run_command(command: str) -> dict[str, str | int]:
#     """Run a command in the workspace and return its output."""
#     restricted_commands = ["rm", "del", "rmdir", "shutdown", "reboot", "format", "mkfs", "fdisk", "diskpart"]
#     try:
#         command_parts = shlex.split(command)
#     except ValueError as error:
#         return {"output": "", "error": str(error), "returncode": -1}
#     if not command_parts:
#         return {"output": "", "error": "Command cannot be empty", "returncode": -1}
#     if command_parts[0].lower() in restricted_commands:
#         return {
#             "output": "",
#             "error": f"Command '{command_parts[0]}' is restricted and cannot be executed.",
#             "returncode": -1,
#         }
#     try:
#         result = subprocess.run(
#             command,
#             shell=True,
#             capture_output=True,
#             text=True,
#             cwd=WORKSPACE,
#         )
#         return {
#             "output": result.stdout,
#             "error": result.stderr,
#             "returncode": result.returncode,
#         }
#     except OSError as error:
#         return {"output": "", "error": str(error), "returncode": -1}


@mcp.tool()
def multi_agent():
    """Report that the multi-agent tool is not implemented yet."""
    return "multi_agent is not implemented yet."