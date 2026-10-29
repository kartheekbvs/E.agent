import os
import tempfile
from pathlib import Path

from server.project_server import register_started_preview
from web import preview_registry

with tempfile.TemporaryDirectory() as temp_dir:
    temp_path = Path(temp_dir)
    preview_registry.REGISTRY_DB = temp_path / "test_previews.sqlite3"
    os.environ["ENTROPY_PROJECT_ID"] = "c165d4d7-3f7d-4862-be08-8b625135343c"
    os.environ["ENTROPY_OWNER_ID"] = "123"
    os.environ["ENTROPY_WORKSPACE"] = str(temp_path / "workspace")
    (temp_path / "workspace").mkdir()

    preview_registry.initialize_preview_registry()

    result = {
        "success": True,
        "project_id": "preview-test-123",
        "port": 8123,
    }

    register_started_preview(result)

    saved = preview_registry.get_preview(
        preview_id="preview-test-123",
        owner_id="123",
    )

    assert saved is not None, "Preview was not registered"
    assert saved["project_id"] == os.environ["ENTROPY_PROJECT_ID"]
    assert saved["owner_id"] == "123"
    assert saved["port"] == 8123
    assert Path(saved["workspace_path"]) == (temp_path / "workspace").resolve()

    print("PREVIEW REGISTRATION TEST PASSED")
    print(saved)
