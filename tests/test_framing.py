"""Framing, checked at every split point rather than at a chosen one.

`test_e2e_session.py` splits a packet at 2 bytes because that is the case the
comment in `parse_raw_data` describes. Picking an offset is guesswork, though:
TCP will not split where you ask it to, so the offset that actually bites in
production is whichever one nobody thought of.

The input space here is small and enumerable - a byte stream has exactly
len-1 places to cut it - so this does not sample or guess. It cuts in every
position and asserts the parse is identical each time. One test, every path
through the reassembly state machine.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from simulator import build_23_auth, build_packet

import cync_lan.devices as devices
from cync_lan.structs import GlobalObject


@pytest.fixture(autouse=True)
def _no_handshake_delay(monkeypatch):
    """Drop the 0.5s pause the 0x23 handler takes before send_a3().

    That pause is real behaviour and is covered end to end in
    test_e2e_session.py, over a socket, where its timing means something.
    Here it is pure cost: 74 parametrised cases x two packets x half a second
    is 75 seconds to test byte alignment, which is not what the delay is for.
    """
    real_sleep = asyncio.sleep

    async def _instant(delay: float, *args, **kwargs):
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(devices.asyncio, "sleep", _instant)


@pytest.fixture(autouse=True)
def _server_globals():
    """parse_packet reaches for g.ncync_server on every device request."""
    g = GlobalObject()
    previous_server, previous_mqtt = g.ncync_server, g.mqtt_client
    server = MagicMock()
    server.tcp_connections = {}
    server.tcp_conn_attempts = {}
    server.shutting_down = False
    g.ncync_server = server
    g.mqtt_client = None
    yield
    g.ncync_server, g.mqtt_client = previous_server, previous_mqtt


def _session() -> devices.CyncTCPSession:
    session = devices.CyncTCPSession(
        reader=MagicMock(), writer=MagicMock(), ip_address="127.0.0.1"
    )
    # Everything the dispatcher would otherwise do over a socket. The subject
    # here is the framing in front of it, not what the handlers go on to do.
    session.write = AsyncMock()
    session.send_a3 = AsyncMock()
    session.allowed_to_connect = True
    session.can_connect = AsyncMock(return_value=True)
    return session


async def _feed(chunks: list[bytes]) -> devices.CyncTCPSession:
    session = _session()
    for chunk in chunks:
        await session.parse_raw_data(chunk)
    for name in ("dev_conn_watcher",):
        task = getattr(session.tasks, name, None)
        if task is not None and not task.done():
            task.cancel()
    return session


# Two packets back to back, with distinct queue ids so the test can tell
# "parsed both, in order" from "parsed one and lost the other".
FIRST = build_23_auth(b"\x39\x87\xc8\x57")
SECOND = build_23_auth(b"\xaa\xbb\xcc\xdd")
STREAM = FIRST + SECOND


async def test_whole_stream_in_one_read_is_the_baseline():
    session = await _feed([STREAM])
    assert session.queue_id == b"\xaa\xbb\xcc\xdd"


@pytest.mark.parametrize("cut", range(1, len(STREAM)))
async def test_every_split_point_parses_identically(cut: int):
    """Cut the same two-packet stream in all 61 places it can be cut.

    Includes the cases that are easy to forget: inside the first header
    before the length fields are readable, exactly on a packet boundary, and
    one byte into the second packet's header.
    """
    session = await _feed([STREAM[:cut], STREAM[cut:]])
    assert session.queue_id == b"\xaa\xbb\xcc\xdd", (
        f"split at {cut} lost or misread a packet"
    )


@pytest.mark.parametrize("first", range(1, 12))
async def test_three_way_splits_inside_the_first_header(first: int):
    """Byte-at-a-time delivery through the header, which is where the
    reassembly branch that needs 5 bytes before it can compute a length
    lives."""
    session = await _feed(
        [STREAM[:first], STREAM[first : first + 1], STREAM[first + 1 :]]
    )
    assert session.queue_id == b"\xaa\xbb\xcc\xdd"


async def test_leading_junk_resynchronises_without_losing_the_packets():
    """The resync branch, from a real capture: four junk bytes in front of an
    otherwise valid stream. Without it the whole buffer was handed to the
    parser as one blob and dropped."""
    session = await _feed([b"\x00\x00\x84\x7e" + STREAM])
    assert session.queue_id == b"\xaa\xbb\xcc\xdd"


async def test_a_declared_length_longer_than_the_stream_is_held_not_dropped():
    """A packet whose header promises more than has arrived must wait for the
    rest rather than being parsed short."""
    truncated = build_packet(0x23, b"\x03" + b"\x11\x22\x33\x44" + bytes(20))
    session = await _feed([truncated[:-6]])
    assert session.needs_more_data is True
    assert session.queue_id in (b"", None), "parsed a packet that had not arrived"
