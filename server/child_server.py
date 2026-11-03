

import os
import shlex
import subprocess
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer as FastMCP

mcp = FastMCP("entropy-child")

WORKSPACE_ROOT = Path(
    os.environ.get("ENTROPY_WORKSPACE", "workspace")
).resolve()

SAFE_COMMANDS = {
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


def _command_is_server_start(command: str) -> bool:
    """Reject commands that would start a preview or web server."""
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


def resolve_workspace_path(path: str) -> Path:
    if not path:
        raise ValueError("Path cannot be empty")

    if WORKSPACE_ROOT.is_symlink():
        raise ValueError("Workspace cannot be a symbolic link")

    root = WORKSPACE_ROOT.resolve()
    requested_path = Path(path)

    if requested_path.is_absolute() or ".." in requested_path.parts:
        raise ValueError("Invalid workspace path")

    current_path = WORKSPACE_ROOT
    for part in requested_path.parts:
        current_path = current_path / part
        if current_path.is_symlink():
            raise ValueError("Symbolic links are not allowed")

    target = (root / requested_path).resolve()

    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError(
            "Path is outside the workspace"
        ) from error

    return target



@mcp.tool()
def list_file(path: str = ".") -> dict:
    """List files and directories inside the child agent's workspace."""

    target = resolve_workspace_path(path)

    if not target.is_dir():
        return {
            "success": False,
            "error": f"Not a directory: {path}",
        }

    entries = sorted(
        entry.name + ("/" if entry.is_dir() else "")
        for entry in target.iterdir()
    )

    return {
        "success": True,
        "path": path,
        "entries": entries,
    }


@mcp.tool()
def read_file(path: str) -> dict:
    """Read a UTF-8 text file inside the child agent's workspace."""

    target = resolve_workspace_path(path)

    if not target.is_file():
        return {
            "success": False,
            "error": f"Not a file: {path}",
        }

    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return {
            "success": False,
            "error": str(error),
        }

    return {
        "success": True,
        "path": path,
        "content": content,
    }


@mcp.tool()
def read_block(path: str, start_line: int, end_line: int) -> dict:
    """Read a narrow line range to minimize prompt tokens."""
    try:
        target = resolve_workspace_path(path)
        if not target.is_file():
            return {"success": False, "error": f"Not a file: {path}"}
        if start_line < 1 or end_line < start_line:
            return {"success": False, "error": "Invalid line range."}

        lines = target.read_text(encoding="utf-8").splitlines()
        start_index = max(0, start_line - 1)
        end_index = min(len(lines), end_line)
        selected = lines[start_index:end_index]

        return {
            "success": True,
            "path": path,
            "start_line": start_line,
            "end_line": end_line,
            "lines": selected,
        }
    except (OSError, UnicodeError, ValueError) as error:
        return {"success": False, "error": str(error)}


@mcp.tool()
def write_selected(path: str, start_line: int, end_line: int, content: str) -> dict:
    """Replace a selected line range with text without rewriting the whole file."""
    try:
        target = resolve_workspace_path(path)
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
    except (OSError, UnicodeError, ValueError) as error:
        return {"success": False, "error": str(error)}


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Write text content to a file inside the child agent's workspace."""
    try:
        file_path = resolve_workspace_path(path)
        if file_path.is_dir():
            return f"Error: Path is a directory: {path}"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Successfully wrote file: {path}"
    except (OSError, ValueError) as error:
        return f"Error writing file: {error}"


@mcp.tool()
def read_memory(key: str = "default") -> dict:
    """Read a concise agent summary stored in the workspace cache."""
    cache_dir = WORKSPACE_ROOT / ".entropy-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    memory_path = cache_dir / f"{key}.txt"

    if not memory_path.exists():
        return {"success": True, "exists": False, "content": ""}

    return {"success": True, "exists": True, "content": memory_path.read_text(encoding="utf-8")}


@mcp.tool()
def write_memory(key: str = "default", content: str = "") -> dict:
    """Write a concise, token-efficient project summary to the workspace cache."""
    cache_dir = WORKSPACE_ROOT / ".entropy-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    memory_path = cache_dir / f"{key}.txt"
    memory_path.write_text(content, encoding="utf-8")
    return {"success": True, "path": str(memory_path), "key": key}


@mcp.tool()
def run_command(command: str, cwd: str = ".") -> dict:
    """Run read-only or test commands in the workspace.

    Use this only for grep, rg, ls, pwd, cat, and test execution. Do not use it to start a
    preview server or app. Use start_project only for preview startup.
    """
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
    if binary.lower() not in {item.lower() for item in SAFE_COMMANDS}:
        return {"success": False, "error": f"Command is not allowed in this workspace: {binary}", "returncode": -1}

    if _command_is_server_start(command):
        return {"success": False, "error": "This tool cannot launch a web app or preview server. Use start_project for preview startup.", "returncode": -1}

    try:
        target_dir = resolve_workspace_path(cwd)
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
        stdout = (result.stdout or "")[:4000]
        stderr = (result.stderr or "")[:2000]
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"success": False, "error": str(error), "returncode": -1}


if __name__ == "__main__":
    mcp.run()
