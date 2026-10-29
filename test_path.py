
from web.preview_registry import (
    initialize_preview_registry,
    register_preview,
    get_preview,
)

initialize_preview_registry()

register_preview(
    preview_id="preview-test-001",
    project_id="project-test-001",
    owner_id=123,
    port=8123,
    workspace_path="runtime_projects/isolation_test/workspace",
)

preview = get_preview("preview-test-001", 123)
assert preview is not None
assert preview["port"] == 8123
assert preview["owner_id"] == "123"

# An unrelated user must not retrieve this preview.
assert get_preview("preview-test-001", 456) is None

print("PREVIEW REGISTRY TEST PASSED")
