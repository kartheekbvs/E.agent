import asyncio
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from starlette.requests import Request

import web.app as web_app


async def run_tests():
    preview = {
        "preview_id": "preview-test-123",
        "project_id": "proj-123",
        "owner_id": "123",
        "port": 8123,
        "workspace_path": "/tmp/workspace",
    }

    def make_request(user_id):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/preview/preview-test-123",
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
        content = b"ok"

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

    # Case 1: The rightful owner can access preview.
    with (
        patch.object(web_app, "get_preview", return_value=preview),
        patch.object(
            web_app,
            "get_project_for_user",
            return_value={"id": preview["project_id"]},
        ),
        patch.object(web_app.httpx, "AsyncClient", FakeClient),
    ):
        response = await web_app.proxy_project_preview(
            make_request("123"),
            "preview-test-123",
        )
        assert response.status_code == 200
        print("OWNER ACCESS TEST PASSED")

    # Case 2: Another user cannot retrieve the preview record.
    with patch.object(web_app, "get_preview", return_value=None):
        try:
            await web_app.proxy_project_preview(
                make_request("456"),
                "preview-test-123",
            )
        except HTTPException as error:
            assert error.status_code == 404
            print("OTHER USER ACCESS TEST PASSED")
        else:
            raise AssertionError("Another user unexpectedly accessed preview")

    # Case 3: Unknown preview ID.
    with patch.object(web_app, "get_preview", return_value=None):
        try:
            await web_app.proxy_project_preview(
                make_request("123"),
                "unknown-preview",
            )
        except HTTPException as error:
            assert error.status_code == 404
            print("UNKNOWN PREVIEW TEST PASSED")
        else:
            raise AssertionError("Unknown preview unexpectedly accessed")


asyncio.run(run_tests())
