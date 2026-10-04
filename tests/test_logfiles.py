import logging
import os
import threading
import time

from cync_lan import logfiles


def _drain():
    """Block until the writer thread has run everything queued so far."""
    done = threading.Event()
    logfiles._submit(done.set)
    assert done.wait(5)


def test_writes_happen_off_the_calling_thread(tmp_path):
    seen = []

    class Probe(logging.Handler):
        def emit(self, record):
            seen.append(threading.current_thread())

    handler = logfiles._OffLoopHandler(Probe())
    log = logging.getLogger("t.offloop")
    log.propagate = False
    log.addHandler(handler)
    log.warning("hello %s", "world")
    _drain()
    assert seen and seen[0] is not threading.current_thread()
    assert seen[0].name == "cync_lan-log-writer"


def test_handler_does_not_touch_disk_until_written(tmp_path):
    path = tmp_path / "a.log"
    handler = logfiles.make_capture_handler(path)
    assert not path.exists()  # delay=True: no open() on the caller's thread
    log = logging.getLogger("t.delay")
    log.propagate = False
    log.addHandler(handler)
    log.warning("line")
    _drain()
    assert "line" in path.read_text()


def test_message_is_frozen_at_log_time(tmp_path):
    path = tmp_path / "b.log"
    handler = logfiles.make_capture_handler(path)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("t.freeze")
    log.propagate = False
    log.addHandler(handler)
    arg = ["x"]
    log.warning("v=%s", arg)
    arg.append("y")
    _drain()
    assert path.read_text().strip() == "v=['x']"


def test_backup_count_follows_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CYNC_LOG_RETENTION_DAYS", "3")
    assert logfiles.make_capture_handler(tmp_path / "c.log").inner.backupCount == 3
    monkeypatch.setenv("CYNC_LOG_RETENTION_DAYS", "junk")
    assert logfiles.log_retention_days() == logfiles.DEFAULT_LOG_RETENTION_DAYS


def test_prune_removes_only_old_files(tmp_path):
    old, new = tmp_path / "old.log", tmp_path / "new.log"
    old.write_text("x")
    new.write_text("x")
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))
    logfiles._last_prune.clear()
    logfiles.prune_old_logs(tmp_path, days=14)
    _drain()
    assert not old.exists() and new.exists()


def test_prune_is_throttled_and_can_be_disabled(tmp_path):
    f = tmp_path / "old.log"
    f.write_text("x")
    stale = time.time() - 30 * 86400
    os.utime(f, (stale, stale))
    logfiles._last_prune.clear()
    logfiles.prune_old_logs(tmp_path, days=0)  # disabled
    _drain()
    assert f.exists()
    logfiles.prune_old_logs(tmp_path, days=14)
    _drain()
    f.write_text("x")
    os.utime(f, (stale, stale))
    logfiles.prune_old_logs(tmp_path, days=14)  # inside the throttle window
    _drain()
    assert f.exists()
