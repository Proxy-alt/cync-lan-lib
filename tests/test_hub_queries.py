"""Tests for the hub query/delete commands.

Every one has a confirmed op_code and request payload read from the
decompiled app, and a PREDICTED cmd_code. These tests pin the parts that are
confirmed - the opcode and the exact bytes on the wire - so a refactor cannot
quietly change what gets sent to someone's hardware.
"""

from __future__ import annotations

import asyncio
import datetime
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

import cync_lan.devices as devices
from cync_lan.packet import PacketBuilder
from cync_lan.structs import GlobalObject


@pytest.fixture(autouse=True)
def _reset_miss_counters():
    """The consecutive-miss counters are module-global, so a test that times
    out would otherwise change how the next one logs."""
    devices._HUB_QUERY_MISSES.clear()
    yield
    devices._HUB_QUERY_MISSES.clear()


@pytest.fixture
def no_tcp_pool(monkeypatch):
    """A server with no eligible sessions - nothing can be sent."""
    g = GlobalObject()
    prev = g.ncync_server
    g.ncync_server = MagicMock()
    g.ncync_server.get_dev_tcp_pool = AsyncMock(return_value=[])
    yield
    g.ncync_server = prev


@pytest.fixture
def sent(monkeypatch):
    """Capture what each command puts on the wire."""
    calls: list[dict] = []
    real = PacketBuilder.build_control_packet

    def _capture(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(PacketBuilder, "build_control_packet", staticmethod(_capture))

    session = MagicMock()
    session.ready_to_control = True
    session.mitm_mode = False
    session.queue_id = b"\x00\x00\x00\x00"
    session.get_ctrl_msg_id_bytes = MagicMock(return_value=(1, 0))
    session.node = None
    session.write = AsyncMock()

    g = GlobalObject()
    prev_server, prev_mqtt = g.ncync_server, g.mqtt_client
    g.ncync_server = MagicMock()
    g.ncync_server.get_dev_tcp_pool = AsyncMock(return_value=[session])
    g.mqtt_client = MagicMock()
    g.mqtt_client.publish = AsyncMock()
    yield calls
    g.ncync_server, g.mqtt_client = prev_server, prev_mqtt


def _reply(op_code: int, payload: bytes):
    """Resolve the pending wait for `op_code` as if the hub had answered."""

    async def _resolve():
        for _ in range(50):
            fut = devices._PENDING_XLINK_RESPONSES.get(op_code)
            if fut is not None and not fut.done():
                fut.set_result(payload)
                return
            await asyncio.sleep(0)

    return _resolve()


# ---------------------------------------------------------------------------
# fire-and-forget deletes
# ---------------------------------------------------------------------------


async def test_delete_automation_sends_the_confirmed_frame(sent):
    await devices.delete_automation(0x1234)

    assert sent[0]["op_code"] == 0x97
    # 6-byte buffer, only the u16 written - the 4 trailing zeros are really
    # on the wire, not an artifact of how the app allocates.
    assert sent[0]["command_payload"] == struct.pack("<H", 0x1234) + bytes(4)
    assert len(sent[0]["command_payload"]) == 6


async def test_delete_group_sends_the_group_mesh_address(sent):
    await devices.delete_group(32770)

    assert sent[0]["op_code"] == 0x32
    assert sent[0]["command_payload"] == struct.pack("<H", 32770)


async def test_delete_group_rejects_an_out_of_range_address(sent):
    """The field is a UShort; a caller passing a group *index* instead of a
    MeshAddress would otherwise silently truncate."""
    await devices.delete_group(70000)

    assert sent == []


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


async def test_query_hub_info_decodes_four_fixed_fields(sent):
    payload = b"".join(
        f.encode().ljust(16, b"\x00") for f in ("1", "2.3", "AABBCCDDEEFF", "SETUP123")
    )
    task = asyncio.create_task(devices.query_hub_info(timeout=2))
    await _reply(0x4B, payload)
    result = await task

    assert sent[0]["op_code"] == 0x4B
    assert sent[0]["command_payload"] == bytes(64)
    assert result == {
        "firmware_version": "1.2.3",
        "mac": "AABBCCDDEEFF",
        "setup_code": "SETUP123",
    }


async def test_query_device_time_decodes_the_xlink_layout(sent):
    """The Xlink layout belongs to the Sol / C-Reach branch specifically."""
    payload = struct.pack("<H", 2026) + bytes([7, 25, 14, 30, 5])
    task = asyncio.create_task(devices.query_device_time(timeout=2, hub_product=True))
    await _reply(0x46, payload)
    result = await task

    assert sent[0]["op_code"] == 0x46
    assert result == datetime.datetime(2026, 7, 25, 14, 30, 5)


async def test_query_device_time_rejects_an_impossible_date(sent):
    """A hub with a corrupt clock must not raise out of a diagnostic read."""
    payload = struct.pack("<H", 2026) + bytes([13, 45, 99, 99, 99])
    task = asyncio.create_task(devices.query_device_time(timeout=2, hub_product=True))
    await _reply(0x46, payload)

    assert await task is None


async def test_query_sol_config_decodes_three_booleans(sent):
    task = asyncio.create_task(devices.query_sol_config(timeout=2))
    await _reply(0xAD, bytes([1, 0, 1]))
    result = await task

    assert sent[0]["op_code"] == 0xAD
    assert result == {
        "show_clock": True,
        "show_timer": False,
        "show_mic_privacy_mode_light": True,
    }


@pytest.mark.parametrize(
    ("fn", "op_code"),
    [
        ("query_hub_info", 0x4B),
        ("query_device_time", 0x46),
        ("query_sol_config", 0xAD),
    ],
)
async def test_queries_return_none_on_timeout(sent, fn, op_code):
    """A timeout is an expected outcome, not an error - whether this
    notification channel rides the intercepted TCP relay is unresolved.

    query_device_time is pinned to its hub_product branch here; its default
    branch has its own timeout coverage below."""
    kwargs = {"hub_product": True} if fn == "query_device_time" else {}
    assert await getattr(devices, fn)(timeout=0.05, **kwargs) is None
    assert sent[0]["op_code"] == op_code


@pytest.mark.parametrize(
    ("fn", "op_code", "short"),
    [
        ("query_hub_info", 0x4B, b"too short"),
        ("query_device_time", 0x46, b"\x01\x02"),
        ("query_sol_config", 0xAD, b"\x01"),
    ],
)
async def test_queries_return_none_on_a_short_reply(sent, fn, op_code, short):
    kwargs = {"hub_product": True} if fn == "query_device_time" else {}
    task = asyncio.create_task(getattr(devices, fn)(timeout=2, **kwargs))
    await _reply(op_code, short)

    assert await task is None


async def test_every_query_sends_an_all_zero_request(sent):
    """The app writes nothing into these buffers, so the allocation size is
    the wire size."""
    for coro in (
        devices.query_hub_info(timeout=0.05),
        devices.query_device_time(timeout=0.05, hub_product=True),
        devices.query_sol_config(timeout=0.05),
    ):
        await coro
    for call in sent:
        assert call["command_payload"] == bytes(64)


# ---------------------------------------------------------------------------
# nothing sent / repeated misses
# ---------------------------------------------------------------------------


async def test_query_gives_up_immediately_when_nothing_was_sent(no_tcp_pool):
    """With no eligible session the request never goes out, so there is
    nothing to wait for. Waiting the full timeout here blocked a polled Home
    Assistant sensor for 10s every poll while devices were reconnecting."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await devices.query_device_time(timeout=30)
    elapsed = loop.time() - started

    assert result is None
    assert elapsed < 1, f"waited {elapsed:.2f}s for a request that was never sent"


async def test_nothing_sent_is_not_reported_as_an_unanswered_query(no_tcp_pool, caplog):
    """An empty pool is a local condition, not evidence about the transport."""
    with caplog.at_level("DEBUG", logger=devices.logger.name):
        await devices.query_device_time(timeout=0.05)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert devices._HUB_QUERY_MISSES == {}


async def test_repeated_timeouts_warn_once_then_drop_to_debug(sent, caplog):
    """These are polled, so an unsupported command family would otherwise
    warn forever - ~5,700 lines a day on real hardware."""
    with caplog.at_level("DEBUG", logger=devices.logger.name):
        for _ in range(6):
            assert await devices.query_device_time(timeout=0.01) is None

    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "No response" in r.message
    ]
    # One on the first miss, one more announcing the switch to debug.
    assert len(warnings) == 2
    assert "logged at debug level only" in warnings[1].message
    assert devices._HUB_QUERY_MISSES["query_device_time"] == 6


async def test_a_later_success_resets_the_miss_counter(sent):
    assert await devices.query_device_time(timeout=0.01, hub_product=True) is None
    assert devices._HUB_QUERY_MISSES["query_device_time"] == 1

    task = asyncio.create_task(devices.query_device_time(timeout=2, hub_product=True))
    await _reply(0x46, struct.pack("<H", 2026) + bytes([8, 1, 3, 30, 0]))
    assert await task == datetime.datetime(2026, 8, 1, 3, 30, 0)

    assert "query_device_time" not in devices._HUB_QUERY_MISSES


# ---------------------------------------------------------------------------
# query_device_time: the 0x8E / 0xE8 branch
#
# QueryDeviceTimeCommand.N() branches on ProductType.f31219d, true only for
# Sol and CReach. Everything else takes the mesh-relay branch, which is what
# these pin.
# ---------------------------------------------------------------------------


def _telink_reply(op_code: int, params: bytes):
    """Resolve a pending Telink wait as if the mesh had answered.

    Wraps params in the frame shape the resolver scans for: opcode, then the
    0x11 0x02 vendor id, then par[0..9].
    """

    async def _resolve():
        for _ in range(50):
            fut = devices._PENDING_TELINK_NOTIFICATIONS.get(op_code)
            if fut is not None and not fut.done():
                frame = bytes([0, 0, 0, 0, 0, 0, 0, op_code]) + b"\x11\x02" + params
                assert devices.try_resolve_telink_notification(frame) is True
                return
            await asyncio.sleep(0)

    return _resolve()


def _time_params(year=2026, month=8, day=1, hour=3, minute=30, second=15):
    return struct.pack("<H", year) + bytes(
        [month, day, hour, minute, second, 0xA1, 0x05, 0x00]
    )


async def test_query_device_time_defaults_to_the_mesh_relay_form(sent):
    """0x46 is correct only for a Sol lamp or C-Reach hub. The default has to
    be the branch every other product takes."""
    task = asyncio.create_task(devices.query_device_time(timeout=2))
    await _telink_reply(devices.TELINK_NOTIFY_QUERY_TIME, _time_params())

    assert await task == datetime.datetime(2026, 8, 1, 3, 30, 15)
    assert sent[0]["op_code"] == 0x8E
    assert sent[0]["command_payload"] == bytes([0xE8, 0x11, 0x02, 0x10])
    # cmd_ is 7 + len(payload), as for every other 0x8E command here.
    assert sent[0]["cmd_code"] == 0x0B
    assert sent[0]["repeat_op_code"] is False


async def test_query_device_time_uses_the_hub_envelope_only_when_asked(sent):
    """hub_product=True is the Sol / C-Reach branch."""
    task = asyncio.create_task(devices.query_device_time(timeout=2, hub_product=True))
    await _reply(0x46, struct.pack("<H", 2026) + bytes([8, 1, 3, 30, 15]))

    assert await task == datetime.datetime(2026, 8, 1, 3, 30, 15)
    assert sent[0]["op_code"] == 0x46


async def test_telink_resolver_finds_the_frame_by_vendor_id(sent):
    """Located by the 0x11 0x02 vendor id rather than a fixed offset, so a
    real relayed packet with a wrapper in front of it still resolves. Prefix
    below is from a captured 0x83."""
    task = asyncio.create_task(devices.query_device_time(timeout=2))
    for _ in range(50):
        if devices._PENDING_TELINK_NOTIFICATIONS.get(devices.TELINK_NOTIFY_QUERY_TIME):
            break
        await asyncio.sleep(0)
    packet = (
        bytes.fromhex("1e 00 00 00 fa db 13 00 15 35 11 b7 00 b7 00".replace(" ", ""))
        + bytes([devices.TELINK_NOTIFY_QUERY_TIME])
        + b"\x11\x02"
        + _time_params()
    )
    assert devices.try_resolve_telink_notification(packet) is True
    assert await task == datetime.datetime(2026, 8, 1, 3, 30, 15)


async def test_telink_resolver_ignores_frames_nobody_awaits(sent):
    frame = bytes([0, 0xE9]) + b"\x11\x02" + _time_params()
    assert devices.try_resolve_telink_notification(frame) is False


async def test_telink_resolver_ignores_a_truncated_parameter_block(sent):
    task = asyncio.create_task(devices.query_device_time(timeout=0.2))
    for _ in range(50):
        if devices._PENDING_TELINK_NOTIFICATIONS.get(devices.TELINK_NOTIFY_QUERY_TIME):
            break
        await asyncio.sleep(0)
    short = bytes([0, devices.TELINK_NOTIFY_QUERY_TIME]) + b"\x11\x02" + b"\x01\x02"
    assert devices.try_resolve_telink_notification(short) is False
    assert await task is None
