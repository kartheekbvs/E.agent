import asyncio
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import ClientSession
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.requests import Request

import web.app as web_app
from web import preview_registry


def get_free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def main():
    with tempfile.TemporaryDirectory(prefix="entropy-proxy-e2e-") as temp:
        workspace = Path(temp)
        (workspace / "index.html").write_text(
            "<!doctype html><html><body><h1>ENTROPY_PROXY_E2E_OK</h1></body></html>",
            encoding="utf-8",
        )

        preview_registry.REGISTRY_DB = workspace / "previews.sqlite3"
        preview_registry.initialize_preview_registry()

        env = os.environ.copy()
        env["ENTROPY_PROJECT_ID"] = "c165d4d7-3f7d-4862-be08-8b625135343c"
        env["ENTROPY_OWNER_ID"] = "123"
        env["ENTROPY_WORKSPACE"] = str(workspace)
        env["ENTROPY_PREVIEW_DB"] = str(preview_registry.REGISTRY_DB)

        server_params = StdioServerParameters(
            command="uv",
            args=["run", "python", "-m", "server.project_server"],
            env=env,
        )

        preview_result = None
        preview_id = None
        preview_pid = None

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                available_tools = await session.list_tools()
                tool_names = {tool.name for tool in available_tools.tools}
                assert "start_project" in tool_names, (
                    "Project server did not register start_project"
                )

                tool_result = await session.call_tool(
                    "start_project",
                    {"command": "auto", "port": 0},
                )
                assert not tool_result.isError, str(tool_result.content)

                text_parts = [
                    item.text
                    for item in (tool_result.content or [])
                    if hasattr(item, "text")
                ]
                preview_result = json.loads("\n".join(text_parts))
                assert preview_result.get("success"), preview_result

                preview_id = preview_result["project_id"]
                preview_pid = preview_result["pid"]
                saved = preview_registry.get_preview(preview_id, "123")
                assert saved is not None, "Project-server wrapper did not register preview"
                assert saved["port"] == preview_result["port"]
                assert saved["project_id"] == env["ENTROPY_PROJECT_ID"]
                assert Path(saved["workspace_path"]) == workspace.resolve()

                print("MCP START_PROJECT TEST PASSED")
                print("SQLITE REGISTRATION TEST PASSED")

        def make_request(user_id):
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            scope = {
                "type": "http",
                "method": "GET",
                "path": f"/preview/{preview_id}",
                "headers": [],
                "query_string": b"",
                "client": ("testclient", 123),
                "server": ("testserver", 80),
                "scheme": "http",
                "root_path": "",
                "http_version": "1.1",
                "state": {},
                "session": {"user_id": user_id},
                "app": web_app.app,
            }
            return Request(scope, receive=receive)

        class FakeResponse:
            status_code = 200
            content = b"<html><body>ENTROPY_PROXY_E2E_OK</body></html>"

            def __init__(self):
                self.raw_headers = []
                self.headers = httpx.Headers({"content-type": "text/plain"})

            def multi_items(self):
                return self.headers.multi_items()

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def request(self, *args, **kwargs):
                return FakeResponse()

        try:
            with (
                patch.object(web_app, "get_preview", return_value=saved),
                patch.object(
                    web_app,
                    "get_project_for_user",
                    return_value={"id": env["ENTROPY_PROJECT_ID"]},
                ),
                patch.object(web_app.httpx, "AsyncClient", FakeClient),
            ):
                response = await web_app.proxy_project_preview(
                    make_request("123"),
                    preview_id,
                )
                assert response.status_code == 200
                print("OWNER PREVIEW PROXY TEST PASSED")

            with patch.object(web_app, "get_preview", return_value=None):
                try:
                    await web_app.proxy_project_preview(
                        make_request("456"),
                        preview_id,
                    )
                except Exception as error:
                    from fastapi import HTTPException

                    assert isinstance(error, HTTPException)
                    assert error.status_code == 404
                    print("OTHER USER BLOCKED TEST PASSED")
                else:
                    raise AssertionError("Other user unexpectedly accessed preview")

            print("PREVIEW PROXY E2E TEST PASSED")
        finally:
            # Stop the preview process tree before deleting its workspace.
            if preview_pid:
                try:
                    if os.name == "nt":
                        subprocess.run(
                            [
                                "taskkill",
                                "/PID",
                                str(preview_pid),
                                "/T",
                                "/F",
                            ],
                            capture_output=True,
                            check=False,
                            timeout=15,
                        )
                    else:
                        os.kill(preview_pid, signal.SIGTERM)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if preview_id:
                preview_registry.remove_preview(
                    preview_id,
                    "123",
                )
            # Give Windows time to release process and file handles.
            if preview_pid:
                time.sleep(1)


asyncio.run(main())
