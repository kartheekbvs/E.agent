import os
import socket
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from server import server as entropy_server
from server.project_server import register_started_preview
from web import preview_registry


def get_free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    original_workspace = entropy_server.WORKSPACE
    original_projects = entropy_server.PROJECTS.copy()

    try:
        with tempfile.TemporaryDirectory(prefix="entropy-preview-test-") as temp:
            workspace = Path(temp)
            (workspace / "index.html").write_text(
                "<!doctype html><html><body><h1>ENTROPY_PREVIEW_OK</h1></body></html>",
                encoding="utf-8",
            )

            entropy_server.WORKSPACE = workspace
            port = get_free_port()
            preview_registry.REGISTRY_DB = workspace / "previews.sqlite3"
            preview_registry.initialize_preview_registry()

            os.environ["ENTROPY_PROJECT_ID"] = "c165d4d7-3f7d-4862-be08-8b625135343c"
            os.environ["ENTROPY_OWNER_ID"] = "123"
            os.environ["ENTROPY_WORKSPACE"] = str(workspace)

            result = entropy_server.start_project(
                command="auto",
                port=port,
            )

            try:
                assert result.get("success"), result.get("error")
                register_started_preview(result)
                preview_id = result["project_id"]
                saved = preview_registry.get_preview(preview_id, "123")
                assert saved is not None, "Preview wasn't registered"
                assert saved["port"] == port, (saved, port)
                assert Path(saved["workspace_path"]) == workspace.resolve()

                for _ in range(50):
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/",
                            timeout=1,
                        ) as response:
                            body = response.read().decode("utf-8", errors="replace")
                            assert "ENTROPY_PREVIEW_OK" in body
                            print("REAL PREVIEW START TEST PASSED")
                            print("PREVIEW REGISTRATION TEST PASSED")
                            print("ISOLATED WORKSPACE CONTENT TEST PASSED")
                            break
                    except (urllib.error.URLError, ConnectionError):
                        time.sleep(0.1)
                else:
                    raise AssertionError("Preview server did not respond")
            finally:
                project = entropy_server.PROJECTS.get(result.get("project_id"))
                if project is not None:
                    process = project.get("process")
                    if process is not None:
                        entropy_server.stop_process(process)
                    entropy_server.PROJECTS.pop(result.get("project_id"), None)
    finally:
        entropy_server.WORKSPACE = original_workspace
        entropy_server.PROJECTS.clear()
        entropy_server.PROJECTS.update(original_projects)


if __name__ == "__main__":
    main()
