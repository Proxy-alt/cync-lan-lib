"""Capture-log file handling: bounded retention and no file I/O on the event loop.

Every capture log (per-connection MITM, unsupported devices, experimental
commands) used a bare TimedRotatingFileHandler: no backupCount, so rotated files
were never deleted - a HA install accumulated ~10 GB / 5000 files of MITM logs -
and every write (plus the file open at construction) ran synchronously on the
event loop, which Home Assistant flags as a blocking call.
"""

import contextlib
import logging
import logging.handlers
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

__all__ = ["make_capture_handler", "prune_old_logs", "log_retention_days"]

DEFAULT_LOG_RETENTION_DAYS = 14
_PRUNE_INTERVAL_SECONDS = 6 * 3600

_queue: "queue.SimpleQueue[Callable[[], None]]" = queue.SimpleQueue()
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
_last_prune: dict[str, float] = {}


def log_retention_days() -> int:
    """Days of capture logs to keep; 0 disables pruning. Read at call time, not
    import time, so a consumer that sets the env after importing cync_lan.const
    (the HA integration does) is still honoured."""
    try:
        return max(
            0,
            int(os.environ.get("CYNC_LOG_RETENTION_DAYS", DEFAULT_LOG_RETENTION_DAYS)),
        )
    except ValueError:
        return DEFAULT_LOG_RETENTION_DAYS


def _run() -> None:
    """Drain the queue, then exit. Deliberately not a permanent thread: Home
    Assistant's test harness fails any test that leaves a thread running, and an
    idle writer has nothing to do anyway - the next record starts a new one."""
    global _worker
    while True:
        with _worker_lock:
            if _queue.empty():
                _worker = None
                return
            job = _queue.get_nowait()
        # A logging failure must never kill the writer - it serves every log.
        with contextlib.suppress(Exception):
            job()


def _submit(job: Callable[[], None]) -> None:
    global _worker
    with _worker_lock:
        _queue.put(job)
        if _worker is None:
            _worker = threading.Thread(
                target=_run, name="cync_lan-log-writer", daemon=True
            )
            _worker.start()


class _OffLoopHandler(logging.Handler):
    """Hands each record to the shared writer thread instead of writing it on the
    caller's (event-loop) thread. The wrapped handler is built with delay=True, so
    even opening its file happens on the writer thread."""

    def __init__(self, inner: logging.Handler) -> None:
        super().__init__(level=inner.level)
        self.inner = inner

    def setFormatter(self, fmt: Optional[logging.Formatter]) -> None:
        super().setFormatter(fmt)
        self.inner.setFormatter(fmt)

    def emit(self, record: logging.LogRecord) -> None:
        # Format on the caller's thread: args may be mutated or unsafe to touch
        # later, and it freezes the message exactly as it was at log time.
        record.msg = record.getMessage()
        record.args = None
        record.exc_info = None
        _submit(lambda: self.inner.handle(record))

    def flush(self) -> None:
        """Block until everything queued so far has been written, so flush()
        keeps its normal meaning to callers that read the file straight after."""
        done = threading.Event()
        _submit(done.set)
        done.wait(5)

    def close(self) -> None:
        self.flush()
        self.inner.close()
        super().close()


def make_capture_handler(path: "str | Path") -> logging.Handler:
    """A daily-rotating file handler that keeps log_retention_days() rotations and
    writes off the event loop. Apply the formatter on the returned handler."""
    days = log_retention_days()
    inner = logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=days, delay=True
    )
    return _OffLoopHandler(inner)


def prune_old_logs(directory: "str | Path", days: Optional[int] = None) -> None:
    """Delete files in `directory` not modified for `days` days (default
    log_retention_days()). Needed on top of backupCount for the MITM logs: their
    base name embeds the date, so every day is a new base name and the handler's
    own backupCount never sees the previous days' files. Throttled to once per six
    hours per directory, and run on the writer thread."""
    days = log_retention_days() if days is None else days
    if days <= 0:
        return
    key = str(directory)
    now = time.monotonic()
    if now - _last_prune.get(key, float("-inf")) < _PRUNE_INTERVAL_SECONDS:
        return
    _last_prune[key] = now
    _submit(lambda: _prune(Path(directory), days))


def _prune(directory: Path, days: int) -> None:
    cutoff = time.time() - days * 86400
    with contextlib.suppress(OSError):
        for entry in os.scandir(directory):
            with contextlib.suppress(OSError):
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
