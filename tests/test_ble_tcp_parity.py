"""The same command, decomposed the same way, on both wires.

None of the ported BLE commands has been confirmed on hardware, and this
does not pretend otherwise. What it does establish is the one thing that
*can* be checked without a radio: that the bytes BLE puts in its packet are
exactly the bytes TCP puts in its payload, cut at the right place.

The cut is the whole translation. Over TCP the 0x8E family rides a
"mesh relay" envelope whose job is to get a Wi-Fi device to forward the
command into the mesh, and the payload inside it begins

    0xF7  0x11 0x02  <sub-command> ...
    opcode  vendor    data

which is precisely `build_command`'s bytes 7, 8-9 and 10 onwards. Here we
are already on the mesh, so the envelope goes and those bytes are the
packet. Anything that does not ride 0x8E carries its opcode as the outer op
instead, with the vendor id leading the payload - same decomposition, one
byte further along.

These assert the relationship mechanically rather than by eye, because
reading it correctly is exactly what nobody managed the first time: the
same four bytes were once misread as an outer op plus a payload, and
shipped a command that did nothing at all on real hardware.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cync_lan import ble_mesh
from cync_lan.ble_mesh import VENDOR_ID, BleMeshSession
from cync_lan.devices import CyncDevice
from cync_lan.structs import GlobalObject

VENDOR_LE = bytes([VENDOR_ID & 0xFF, (VENDOR_ID >> 8) & 0xFF])
TARGET = 7


@pytest.fixture(autouse=True)
def _no_mqtt():
    """Several commands build an MQTT callback while assembling their
    payload; the payload is what is under test, not the callback."""
    g = GlobalObject()
    previous = g.mqtt_client
    g.mqtt_client = AsyncMock()
    yield
    g.mqtt_client = previous


async def _tcp(method: str, *args, **kwargs):
    """Run a CyncDevice command, returning (op, payload, repeat_op_code)."""
    device = CyncDevice(dev_id=1, dev_type=55)
    captured = {}

    async def _send_command(op, cmd_, sub, payload, cb, lp, repeat_op_code=True):
        captured.setdefault("calls", []).append((op, bytes(payload), repeat_op_code))

    device.send_command = _send_command
    await getattr(device, method)(*args, **kwargs)
    return captured.get("calls", [])


async def _ble(method: str, *args, **kwargs):
    """Run a BleMeshSession command, returning (opcode, data) per packet."""
    session = BleMeshSession.__new__(BleMeshSession)
    calls = []

    async def _send(target, opcode, data, **kw):
        calls.append((target, opcode, bytes(data)))

    session.send = _send
    await getattr(session, method)(TARGET, *args, **kwargs)
    return calls


def _assert_same_command(tcp_call, ble_call):
    """The core relationship, for a command that rides the 0x8E relay."""
    op, payload, repeat = tcp_call
    _, opcode, data = ble_call

    assert op == 0x8E, f"expected the mesh-relay envelope, got {op:#x}"
    assert repeat is False, "the 0x8E family never repeats its op byte"
    assert payload == bytes([opcode]) + VENDOR_LE + data, (
        f"TCP payload {payload.hex(' ')} is not "
        f"opcode {opcode:#x} + vendor + data {data.hex(' ')}"
    )


def _assert_same_command_outer_op(tcp_call, ble_call):
    """For a command whose opcode is the outer op rather than the payload's
    first byte."""
    op, payload, repeat = tcp_call
    _, opcode, data = ble_call

    assert op == opcode, f"opcode differs: TCP {op:#x} vs BLE {opcode:#x}"
    assert payload == VENDOR_LE + data, (
        f"TCP payload {payload.hex(' ')} is not vendor + data {data.hex(' ')}"
    )


async def test_indicator_led_is_the_same_command_on_both_wires():
    """The one whose TCP form was confirmed on hardware, which is why its
    payload shape can be trusted even though this transport cannot yet."""
    tcp = await _tcp("set_indicator_led", mode=2, color=1, brightness=50)
    ble = await _ble("set_indicator_led", mode=2, color=1, brightness=50)
    assert len(tcp) == len(ble) == 1
    _assert_same_command(tcp[0], ble[0])


async def test_dimmer_led_mode_is_the_same_command():
    tcp = await _tcp("set_dimmer_led_mode", 2)
    ble = await _ble("set_dimmer_led_mode", 2)
    assert len(tcp) == len(ble) == 1
    _assert_same_command(tcp[0], ble[0])


async def test_dimmer_led_brightness_sends_the_same_two_packets():
    """Preview then save - a single packet leaves the setting untouched
    once the device times out, so the pair is part of the command."""
    tcp = await _tcp("set_dimmer_led_brightness", 40)
    ble = await _ble("set_dimmer_led_brightness", 40)
    assert len(tcp) == len(ble) == 2, "the preview/save pair did not survive"
    for tcp_call, ble_call in zip(tcp, ble, strict=True):
        _assert_same_command(tcp_call, ble_call)


async def test_multicolor_gradient_mode_is_the_same_command():
    tcp = await _tcp("set_multicolor_gradient_mode", True)
    ble = await _ble("set_multicolor_gradient_mode", True)
    assert len(tcp) == len(ble) == 1
    _assert_same_command(tcp[0], ble[0])


async def test_multicolor_segment_count_is_the_same_command():
    tcp = await _tcp("set_multicolor_segment_count", 6)
    ble = await _ble("set_multicolor_segment_count", 6)
    assert len(tcp) == len(ble) == 1
    _assert_same_command(tcp[0], ble[0])


async def test_gradient_and_segment_count_share_a_sub_command_and_differ_by_a_flag():
    """Both are 0x4E; 0x00 selects gradient and 0xFF selects segment count.
    Getting that byte the wrong way round would silently reconfigure a
    strip, so it is worth pinning separately."""
    gradient = (await _ble("set_multicolor_gradient_mode", True))[0][2]
    segments = (await _ble("set_multicolor_segment_count", 6))[0][2]
    assert gradient[0] == segments[0] == ble_mesh.SUB_MULTICOLOR
    assert gradient[1] == 0x00
    assert segments[1] == 0xFF


async def test_light_effect_carries_its_opcode_outside_the_payload():
    """Not a 0x8E command: 0xE2 is the outer op on TCP, so the cut is one
    byte further along."""
    from cync_lan.const import LIGHT_RUN_MODE_EFFECTS

    name = "candle"
    mode_code, index, nonce = LIGHT_RUN_MODE_EFFECTS[name]
    tcp = await _tcp("set_light_effect", name)
    ble = await _ble("set_light_effect", mode_code=mode_code, index=index, nonce=nonce)
    assert len(tcp) == 1
    op, payload, repeat = tcp[0]
    assert op == 0xE2
    assert repeat is True, "this family does repeat its op byte"
    # The payload's own shape, independent of which preset was chosen.
    assert payload[:3] == VENDOR_LE + bytes([0x07])
    _, opcode, data = ble[0]
    assert opcode == 0xE2
    assert data[0] == 0x07


async def test_the_ble_packet_puts_the_vendor_id_in_its_own_field():
    """The structural difference between the two wires, stated once: TCP
    inlines the vendor id at the head of the payload, BLE gives it bytes 8
    and 9. Everything above depends on that being true."""
    packet = ble_mesh.build_command(counter=1, target=TARGET, opcode=0xF7, data=b"\x06")
    assert packet[7] == 0xF7
    assert bytes(packet[8:10]) == VENDOR_LE
    assert packet[10] == 0x06


async def test_every_ported_command_validates_its_inputs():
    """A mesh command is fire-and-forget with no ack, so a bad argument is
    invisible unless it is refused here."""
    session = BleMeshSession.__new__(BleMeshSession)
    session.send = AsyncMock()

    for kwargs in (
        {"mode": 5, "color": 0, "brightness": 50},
        {"mode": 0, "color": 9, "brightness": 50},
        {"mode": 0, "color": 0, "brightness": 0},
        {"mode": 0, "color": 0, "brightness": 101},
    ):
        with pytest.raises(ble_mesh.BleMeshError):
            await session.set_indicator_led(TARGET, **kwargs)

    for level in (-1, 101):
        with pytest.raises(ble_mesh.BleMeshError):
            await session.set_dimmer_led_brightness(TARGET, level)

    # The parity test found this one missing from the port: devices.py
    # refuses an out-of-range mode and this did not.
    for mode in (0, 3, 99):
        with pytest.raises(ble_mesh.BleMeshError):
            await session.set_dimmer_led_mode(TARGET, mode)

    session.send.assert_not_awaited()
