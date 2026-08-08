"""Both transports, driven through one interface.

The point of these is not that the wrappers forward calls - that is obvious
from reading them. It is that the two ends agree about *units* and about who
resolves what, because those were the two things that differed and both
differences were silent.
"""

from __future__ import annotations

import inspect

import pytest

from cync_lan import transport
from cync_lan.ble_mesh import BleMeshSession
from cync_lan.classify import light_features
from cync_lan.devices import CyncDevice
from cync_lan.metadata.model_info import device_type_map
from cync_lan.transport import BleTransport, CyncTransport, TcpTransport


class _RecordingDevice:
    """Stands in for CyncDevice, recording what the wire would receive."""

    def __init__(self, dev_type: int = 55):
        self.calls: list[tuple] = []
        self.metadata = device_type_map[dev_type]
        self.is_dimmable = True
        self.supports_temperature = True
        self.supports_rgb = True

    async def set_power(self, state, sub_id=None):
        self.calls.append(("power", state, sub_id))

    async def set_brightness(self, bri, sub_id=None, callback=None):
        self.calls.append(("brightness", bri, sub_id))

    async def set_temperature(self, temp, sub_id=None):
        self.calls.append(("temperature", temp, sub_id))

    async def set_rgb(self, red, green, blue, sub_id=None):
        self.calls.append(("rgb", (red, green, blue), sub_id))


class _RecordingSession:
    """Stands in for BleMeshSession."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def set_power(self, target, on):
        self.calls.append(("power", target, on))

    async def set_brightness(self, target, brightness, *, is_sol_lamp=False):
        self.calls.append(("brightness", target, brightness, is_sol_lamp))

    async def set_colour_temp(self, target, colour_temp, *, is_sol_lamp=False):
        self.calls.append(("temperature", target, colour_temp, is_sol_lamp))

    async def set_rgb(self, target, red, green, blue):
        self.calls.append(("rgb", target, (red, green, blue)))


def test_both_wrappers_satisfy_the_protocol():
    assert isinstance(TcpTransport(_RecordingDevice()), CyncTransport)
    assert isinstance(
        BleTransport(_RecordingSession(), target=5, dev_type=55), CyncTransport
    )


def test_the_protocol_only_promises_what_both_wires_have():
    """A method one transport cannot implement is worse here than absent -
    it becomes something that raises on half its implementations."""
    promised = {
        name
        for name, _ in inspect.getmembers(CyncTransport, inspect.isfunction)
        if not name.startswith("_")
    }
    assert promised == {"set_power", "set_brightness", "set_temperature", "set_rgb"}

    # Each concrete transport really does have something for all four, under
    # whatever name - which is what makes the wrappers thin rather than
    # inventive.
    assert {"set_power", "set_brightness", "set_rgb"} <= set(dir(CyncDevice))
    assert "set_temperature" in dir(CyncDevice)
    assert {"set_power", "set_brightness", "set_rgb"} <= set(dir(BleMeshSession))
    assert "set_colour_temp" in dir(BleMeshSession)


async def test_power_is_a_bool_on_both_and_becomes_what_each_wire_wants():
    """int on TCP, bool on BLE - the mismatch every caller used to bridge."""
    device, session = _RecordingDevice(), _RecordingSession()
    await transport.protocol("tcp", device=device).set_power(True)
    await transport.protocol("ble", session=session, target=5, dev_type=55).set_power(
        True
    )
    assert device.calls == [("power", 1, None)]
    assert session.calls == [("power", 5, True)]

    await transport.protocol("tcp", device=device).set_power(False)
    assert device.calls[-1] == ("power", 0, None)


async def test_brightness_and_temperature_are_0_100_on_both():
    """The unit contract, which is the reason this module exists.

    `CyncDevice.set_temperature` refuses anything above 100, so a transport
    that quietly took kelvin on one side would drop every command on that
    side and work on the other.
    """
    device, session = _RecordingDevice(), _RecordingSession()
    tcp = transport.protocol("tcp", device=device)
    ble = transport.protocol("ble", session=session, target=5, dev_type=55)

    for value in (0, 50, 100):
        await tcp.set_brightness(value)
        await ble.set_brightness(value)
        await tcp.set_temperature(value)
        await ble.set_temperature(value)

    assert [c[1] for c in device.calls if c[0] == "brightness"] == [0, 50, 100]
    assert [c[2] for c in session.calls if c[0] == "brightness"] == [0, 50, 100]
    assert [c[1] for c in device.calls if c[0] == "temperature"] == [0, 50, 100]
    assert [c[2] for c in session.calls if c[0] == "temperature"] == [0, 50, 100]


async def test_the_name_mismatch_is_absorbed():
    """set_temperature here, set_colour_temp on the session."""
    session = _RecordingSession()
    await transport.protocol(
        "ble", session=session, target=9, dev_type=55
    ).set_temperature(42)
    assert session.calls == [("temperature", 9, 42, False)]


async def test_is_sol_lamp_stops_being_the_callers_problem():
    """The session takes it as a keyword because it has no device to ask, so
    every caller worked it out and passed it down. Resolved once, here."""
    sol = [t for t, i in device_type_map.items() if i.opcodes.sol_lamp]
    assert sol, "no sol-lamp types in the map any more"

    session = _RecordingSession()
    await transport.protocol(
        "ble", session=session, target=1, dev_type=sol[0]
    ).set_brightness(40)
    assert session.calls[-1] == ("brightness", 1, 40, True)

    session = _RecordingSession()
    await transport.protocol(
        "ble", session=session, target=1, dev_type=55
    ).set_brightness(40)
    assert session.calls[-1] == ("brightness", 1, 40, False)


async def test_the_tcp_side_carries_its_sub_id_through():
    """Multi-entity devices address a sub-device; BLE has no equivalent, so
    it is bound at construction rather than in the protocol."""
    device = _RecordingDevice()
    await transport.protocol("tcp", device=device, sub_id=3).set_power(True)
    assert device.calls == [("power", 1, 3)]


def test_features_come_from_the_device_on_tcp_and_the_type_on_ble():
    """TCP has an object whose properties carry per-instance overrides from
    the cloud export; BLE has only a type id."""
    device = _RecordingDevice()
    device.supports_rgb = False
    assert transport.protocol("tcp", device=device).features.rgb is False

    ble = transport.protocol("ble", session=_RecordingSession(), target=1, dev_type=55)
    assert ble.features == light_features(55)


def test_an_unknown_transport_says_so():
    with pytest.raises(ValueError, match="unknown transport"):
        transport.protocol("zigbee", device=_RecordingDevice())


def test_the_factory_accepts_the_names_people_will_use():
    device = _RecordingDevice()
    for name in ("tcp", "TCP", " wifi ", "WiFi"):
        assert isinstance(transport.protocol(name, device=device), TcpTransport)


def test_transport_needs_no_home_assistant():
    """Same rule as classify.py - the CLI and the add-on import this
    package."""
    import ast

    tree = ast.parse(open(transport.__file__).read())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [m for m in imported if m.split(".")[0] == "homeassistant"]


async def test_no_sub_id_means_the_argument_is_not_passed_at_all():
    """`set_power(1, None)` and `set_power(1)` reach the same wire but are
    not the same call to anything mocking the device. A facade that rewrites
    every consumer's call signature makes work for them for nothing - three
    shipped tests failed on exactly that difference."""
    calls = []

    class _Strict:
        metadata = device_type_map[55]
        is_dimmable = supports_temperature = supports_rgb = True

        async def set_power(self, state, sub_id=None):
            calls.append(("power",) + ((state,) if sub_id is None else (state, sub_id)))

        async def set_rgb(self, red, green, blue, sub_id=None):
            calls.append(("rgb", red, green, blue) + ((sub_id,) if sub_id else ()))

    device = _Strict()
    await TcpTransport(device).set_power(True)
    await TcpTransport(device).set_rgb(10, 20, 30)
    assert calls == [("power", 1), ("rgb", 10, 20, 30)]

    calls.clear()
    await TcpTransport(device, sub_id=2).set_power(True)
    assert calls == [("power", 1, 2)]
