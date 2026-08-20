from pathlib import Path

from fstec_monitor.telegram.lifecycle import MessageLifecycleManager


def test_lifecycle_cleanup_never_deletes_markdown(tmp_path: Path) -> None:
    markdown = tmp_path / "report.md"
    markdown.write_bytes(b"# immutable report\n")

    assert MessageLifecycleManager.safe_delete_path(markdown) is False
    assert markdown.read_bytes() == b"# immutable report\n"


def test_lifecycle_cleanup_can_delete_non_markdown_temp_file(tmp_path: Path) -> None:
    temporary = tmp_path / "telegram.tmp"
    temporary.write_bytes(b"temporary")

    assert MessageLifecycleManager.safe_delete_path(temporary) is True
    assert not temporary.exists()
