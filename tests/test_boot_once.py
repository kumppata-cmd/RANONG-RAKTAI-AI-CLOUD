"""boot_once — งานบูต idempotent รันครั้งเดียวต่อ instance (24 พ.ย. 2569)."""
import os

from hd_module.boot_once import run_once_per_instance


def test_runs_once_then_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("HD_BOOT_MARK_DIR", str(tmp_path))
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123def456")
    monkeypatch.delenv("HD_BOOT_ONCE", raising=False)
    calls = []
    assert run_once_per_instance("job", lambda: calls.append(1), version="7") == "ran"
    assert run_once_per_instance("job", lambda: calls.append(1), version="7") == "skipped"
    assert calls == [1]
    assert any(f.name.startswith("hd_boot_job.7.abc123def456") and f.name.endswith(".done") for f in tmp_path.iterdir())


def test_new_version_or_commit_runs_again(tmp_path, monkeypatch):
    monkeypatch.setenv("HD_BOOT_MARK_DIR", str(tmp_path))
    monkeypatch.setenv("RENDER_GIT_COMMIT", "aaaa")
    calls = []
    run_once_per_instance("job", lambda: calls.append(1), version="1")
    run_once_per_instance("job", lambda: calls.append(1), version="2")      # schema ใหม่
    monkeypatch.setenv("RENDER_GIT_COMMIT", "bbbb")
    run_once_per_instance("job", lambda: calls.append(1), version="2")      # deploy ใหม่
    assert calls == [1, 1, 1]


def test_failure_does_not_write_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HD_BOOT_MARK_DIR", str(tmp_path))
    def boom():
        raise RuntimeError("db down")
    assert run_once_per_instance("job", boom) == "failed"
    assert not list(tmp_path.glob("*.done"))
    calls = []
    assert run_once_per_instance("job", lambda: calls.append(1)) == "ran"   # worker ถัดไปลองใหม่


def test_disabled_flag_runs_every_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HD_BOOT_MARK_DIR", str(tmp_path))
    monkeypatch.setenv("HD_BOOT_ONCE", "0")
    calls = []
    assert run_once_per_instance("job", lambda: calls.append(1)) == "ran_unguarded"
    assert run_once_per_instance("job", lambda: calls.append(1)) == "ran_unguarded"
    assert calls == [1, 1]


def test_unwritable_dir_falls_back_to_unguarded(tmp_path, monkeypatch):
    monkeypatch.setenv("HD_BOOT_MARK_DIR", str(tmp_path / "missing"))
    monkeypatch.delenv("HD_BOOT_ONCE", raising=False)
    calls = []
    assert run_once_per_instance("job", lambda: calls.append(1)) == "ran_unguarded"
    assert calls == [1]
