from __future__ import annotations

from PySide6.QtCore import QLockFile
import pytest

from componentpress.execution.job_store import JobStore


def test_initialize_releases_lock_and_removes_partial_job_when_metadata_write_fails(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(store, "write", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        store.initialize("partial-job", "project")
    assert not store.job_directory("partial-job").exists()
    lock = QLockFile(str(store.locks_root / "partial-job.lock"))
    lock.setStaleLockTime(0)
    assert lock.tryLock(0)
    lock.unlock()


def test_initialize_releases_lock_when_job_directory_creation_fails(tmp_path, monkeypatch):
    store = JobStore(tmp_path)
    original_mkdir = type(tmp_path).mkdir
    directory = store.job_directory("mkdir-failure")

    def fail_job_directory(path, *args, **kwargs):
        if path == directory:
            raise OSError("disk full")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), "mkdir", fail_job_directory)
    with pytest.raises(OSError, match="disk full"):
        store.initialize("mkdir-failure", "project")
    lock = QLockFile(str(store.locks_root / "mkdir-failure.lock"))
    lock.setStaleLockTime(0)
    assert lock.tryLock(0)
    lock.unlock()
