"""Regression tests for error/shutdown paths that used to raise NameError.

All three bugs below lived in `except` handlers or EOF branches - code that
only runs when something has already gone wrong - which is exactly why the
test suite never touched them and why they survived to a release.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from cync_lan.cloud_api import CyncCloudAPI
from cync_lan.devices import CyncTCPSession


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("network blip"),
        OSError("connection reset"),
        RuntimeError("something unexpected"),
    ],
)
async def test_send_tkn_post_returns_false_on_non_http_errors(exc):
    """A plain network failure must report a clean auth failure.

    ClientConnectorError/TimeoutError are NOT ClientResponseError, so they
    fall through to the generic handler - which referenced an undefined
    `lp` and raised NameError instead of returning False. That handler is
    on the token-refresh and OTP-submission paths, so any dropped
    connection there surfaced as a confusing NameError.
    """
    api = CyncCloudAPI(session=MagicMock())
    api.http_session = MagicMock()
    api.http_session.post = AsyncMock(side_effect=exc)

    assert await api._send_tkn_post("https://example.invalid/token", {}) is False


def _error_response(status: int, body: str) -> MagicMock:
    resp = MagicMock()
    resp.ok = False
    resp.status = status
    resp.text = AsyncMock(return_value=body)
    return resp


async def test_send_tkn_post_returns_false_on_http_error():
    api = CyncCloudAPI(session=MagicMock())
    api.http_session = MagicMock()
    api.http_session.post = AsyncMock(return_value=_error_response(401, ""))

    assert await api._send_tkn_post("https://example.invalid/token", {}) is False


async def test_auth_failure_logs_the_vendors_reason(caplog):
    """Proxy-alt/cync-lan#51: every OTP rejection logged as a bare 400, and
    the body that says which 400 it was had already been released."""
    api = CyncCloudAPI(session=MagicMock())
    api.http_session = MagicMock()
    api.http_session.post = AsyncMock(
        return_value=_error_response(
            400, '{"error": {"msg": "password error", "code": 4001007}}'
        )
    )

    with caplog.at_level("ERROR"):
        ok = await api._send_tkn_post("https://example.invalid/token", {})

    assert ok is False
    assert "HTTP 400: password error (code 4001007)" in caplog.text


async def test_an_unexpected_error_body_is_logged_raw(caplog):
    api = CyncCloudAPI(session=MagicMock())
    api.http_session = MagicMock()
    api.http_session.post = AsyncMock(
        return_value=_error_response(502, "<html>Bad Gateway</html>")
    )

    with caplog.at_level("ERROR"):
        assert await api._send_tkn_post("https://example.invalid/token", {}) is False
    assert "HTTP 502: <html>Bad Gateway</html>" in caplog.text


def _mitm_session() -> CyncTCPSession:
    session = CyncTCPSession.__new__(CyncTCPSession)
    session.lp = "test:"
    session.mitm_logger = MagicMock()
    session.mitm_bytes_from_cloud = 0
    session.ip_address = "10.0.0.5"
    session.proxy_last_packet_ts = 0.0
    session.writer = MagicMock()
    session.writer.drain = AsyncMock()
    return session


class _SpinGuard(Exception):
    """Raised by the fake reader if the proxy loop re-reads after EOF."""


async def test_cloud_proxy_task_stops_on_eof():
    """StreamReader.read() returns b"" immediately and forever once the peer
    closes. The EOF branch used to be a bare `pass`, so the loop spun at
    100% CPU for as long as MITM mode stayed enabled.

    Deliberately NOT guarded with asyncio.wait_for: an AsyncMock completes
    without ever suspending, so the old busy-loop never yielded to the event
    loop and a timeout could not fire - it hung the whole test run. Counting
    reads instead makes the regression fail fast rather than hang.
    """
    session = _mitm_session()
    calls = 0

    async def _read(_n):
        nonlocal calls
        calls += 1
        if calls > 3:
            raise _SpinGuard("proxy loop kept reading after EOF")
        return b""

    session.cloud_reader = MagicMock()
    session.cloud_reader.read = _read

    await session._cloud_proxy_task()

    assert calls == 1, f"expected a single read then break, got {calls}"
    # Returned promptly instead of spinning, and never wrote EOF downstream.
    session.writer.write.assert_not_called()


async def test_cloud_proxy_task_reraises_cancellation_cleanly():
    """Cancellation is this task's normal shutdown path (stop_mitm/close).
    The handler referenced an undefined `name`, so every clean stop raised
    NameError out of the handler instead of re-raising CancelledError."""
    session = _mitm_session()
    session.cloud_reader = MagicMock()
    session.cloud_reader.read = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await session._cloud_proxy_task()


async def test_cloud_proxy_task_forwards_data_then_stops():
    session = _mitm_session()
    session.cloud_reader = MagicMock()
    session.cloud_reader.read = AsyncMock(side_effect=[b"hello", b""])

    await asyncio.wait_for(session._cloud_proxy_task(), timeout=2)

    session.writer.write.assert_called_once_with(b"hello")
    assert session.mitm_bytes_from_cloud == len(b"hello")
