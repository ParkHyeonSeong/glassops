"""Preserve recent evidence through rotation, restart and failed reopening."""
import asyncio
import os
from pathlib import Path

import pytest

from app import runtime_logs as logs


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(logs, "_open_log_inodes", lambda: set())
    monkeypatch.setattr(logs, "_reopen_writers", lambda: None)
    logs.prepare_runtime_logs(tmp_path, 2_000_000_000)
    return tmp_path


def write(path: Path, text: str, timestamp: float):
    path.write_text(text)
    os.utime(path, (timestamp, timestamp))


def test_rotation_preserves_bytes_and_repeated_rotation_never_overwrites(archive):
    write(archive / "backend.log", "before\n", 2_000_000_001)
    logs.rotate_runtime_logs(archive, 2_000_000_002)
    write(archive / "backend.log", "after\n", 2_000_000_002)
    logs.rotate_runtime_logs(archive, 2_000_000_002)
    bodies = [p.read_text() for p in archive.glob("backend.*.log")]
    assert sorted(bodies) == ["after\n", "before\n"]
    assert (archive / "backend.log").read_text() == ""


def test_cleanup_uses_age_not_size_or_count_and_preserves_unknown_files(archive):
    write(archive / "backend.log", "old\n", 1_999_395_199)
    logs.rotate_runtime_logs(archive, 1_999_395_199)
    write(archive / "backend.log", "recent\n" * 10000, 1_999_395_201)
    logs.rotate_runtime_logs(archive, 1_999_395_201)
    (archive / "operator-backup.log").write_text("keep")
    (archive / "backend.log").write_text("active")
    assert logs.prune_runtime_logs(archive, 2_000_000_000) == 1
    assert [p.read_text() for p in archive.glob("backend.*.log")] == ["recent\n" * 10000]
    assert (archive / "backend.log").read_text() == "active"
    assert (archive / "operator-backup.log").read_text() == "keep"


def test_restart_does_not_renew_old_active_files_or_quiet_channels(archive):
    write(archive / "backend.log", "expired", 1_999_000_000)
    write(archive / "agent.log", "still relevant", 1_999_900_000)
    logs.prepare_runtime_logs(archive, 2_000_000_000)
    assert not list(archive.glob("backend.*.log"))
    assert [p.read_text() for p in archive.glob("agent.*.log")] == ["still relevant"]
    assert all((archive / f"{name}.log").exists() for name in logs.CHANNELS)


def test_failed_reopen_retains_evidence_and_next_rotation_can_retry(archive, monkeypatch):
    write(archive / "backend.log", "before", 2_000_000_000)
    def fail():
        raise OSError("reopen failed")
    monkeypatch.setattr(logs, "_reopen_writers", fail)
    with pytest.raises(OSError, match="reopen failed"):
        logs.rotate_runtime_logs(archive, 2_000_000_001)
    assert [p.read_text() for p in archive.glob("backend.*.log")] == ["before"]
    monkeypatch.setattr(logs, "_reopen_writers", lambda: None)
    logs.rotate_runtime_logs(archive, 2_000_000_002)
    assert [p.read_text() for p in archive.glob("backend.*.log")] == ["before"]


def test_open_archive_and_late_writes_are_not_pruned(archive, monkeypatch):
    write(archive / "backend.log", "held open", 1_999_000_000)
    logs.rotate_runtime_logs(archive, 1_999_000_000)
    path = next(archive.glob("backend.*.log"))
    st = path.stat()
    monkeypatch.setattr(logs, "_open_log_inodes", lambda: {(st.st_dev, st.st_ino)})
    assert logs.prune_runtime_logs(archive, 2_000_000_000) == 0
    monkeypatch.setattr(logs, "_open_log_inodes", lambda: set())
    os.utime(path, (1_999_999_000, 1_999_999_000))
    assert logs.prune_runtime_logs(archive, 2_000_000_000) == 0
    assert path.read_text() == "held open"


def test_unreadable_writer_state_never_deletes_archives(archive, monkeypatch):
    write(archive / "backend.log", "keep", 1_999_000_000)
    logs.rotate_runtime_logs(archive, 1_999_000_000)
    def fail():
        raise PermissionError("cannot inspect writer")
    monkeypatch.setattr(logs, "_open_log_inodes", fail)
    with pytest.raises(PermissionError):
        logs.prune_runtime_logs(archive, 2_000_000_000)
    assert len(list(archive.glob("backend.*.log"))) == 1


def test_prepare_preserves_unknown_files_and_restricts_log_permissions(archive):
    (archive / "operator-note").write_text("keep")
    logs.prepare_runtime_logs(archive, 2_000_000_000)
    assert (archive / "operator-note").read_text() == "keep"
    assert archive.stat().st_mode & 0o007 == 0
    assert (archive / "backend.log").stat().st_mode & 0o007 == 0


async def test_maintenance_failure_is_contained_and_shutdown_joins_work(monkeypatch):
    stop = asyncio.Event()
    calls = []
    def fail(*_):
        calls.append("attempt")
        stop.set()
        raise OSError("disk full")
    monkeypatch.setattr(logs, "rotate_runtime_logs", fail)
    await asyncio.wait_for(logs.maintain_runtime_logs(stop), 2)
    assert calls == ["attempt"]
