from unittest.mock import Mock, patch

from server.project_server import cleanup_stopped_previews


def main():
    running_process = Mock()
    running_process.poll.return_value = None

    stopped_process = Mock()
    stopped_process.poll.return_value = 0

    fake_server = Mock()
    fake_server.PROJECTS = {
        "preview-running": {"process": running_process},
        "preview-stopped": {"process": stopped_process},
    }

    with (
        patch.dict("os.environ", {"ENTROPY_OWNER_ID": "123"}),
        patch(
            "server.project_server.remove_preview",
            side_effect=lambda preview_id, owner_id: (
                preview_id == "preview-stopped" and owner_id == "123"
            ),
        ) as remove_mock,
    ):
        removed = cleanup_stopped_previews(fake_server)

    assert removed == 1, f"Expected 1 removal, got {removed}"
    remove_mock.assert_called_once_with("preview-stopped", "123")
    print("PREVIEW CLEANUP TEST PASSED")


main()
