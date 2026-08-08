"""One command surface, whichever wire the command goes out on.

Both transports already existed here - `CyncDevice` speaks TCP to a Wi-Fi
device, `BleMeshSession` speaks Bluetooth to the mesh - and both already had
the same four commands. What they did not have was the same *shape*:

    CyncDevice.set_power(state: int, sub_id=None)
    BleMeshSession.set_power(target: int, on: bool)

    CyncDevice.set_temperature(temp: int, sub_id=None)
    BleMeshSession.set_colour_temp(target, colour_temp, *, is_sol_lamp=False)

A device object on one side, a session plus an explicit target on the other;
`int` against `bool`; one name that does not match; and `is_sol_lamp` a
caller's problem on BLE while TCP works it out itself. Every consumer that
wanted to be transport-agnostic had to bridge that itself, which is most of
what the adapters in a shared Home Assistant entity layer were doing.

**Units are the contract here**, stated once because getting them wrong is
silent. Brightness and colour temperature are both **0-100**, as the wire
speaks them - not kelvin, not 0-255. `cync_lan.classify` has the converters
for callers that speak something else. This is not hypothetical tidiness:
`CyncDevice.set_temperature` refuses anything above 100, and the Home
Assistant integration spent its whole life passing kelvin into it and having
every colour change dropped. Writing the contract down is what found that.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from cync_lan.classify import LightFeatures, is_sol_lamp, light_features


@runtime_checkable
class CyncTransport(Protocol):
    """What every transport can do to one device.

    Deliberately small. This is the set both wires actually implement today;
    anything one can do and the other cannot belongs on the concrete class,
    not here, where it would become a method that raises on half its
    implementations.
    """

    @property
    def features(self) -> LightFeatures:
        """What the target can do, from its static type metadata."""
        ...

    async def set_power(self, on: bool) -> None: ...

    async def set_brightness(self, percent: int) -> None:
        """0-100."""
        ...

    async def set_temperature(self, cync_temp: int) -> None:
        """0-100, warm to cool. Use classify.kelvin_to_cync if you have
        kelvin."""
        ...

    async def set_rgb(self, red: int, green: int, blue: int) -> None: ...


class TcpTransport:
    """A `CyncDevice`, which is already addressed at one device.

    Thin by construction - the TCP side was the shape the protocol was
    modelled on, so this mostly renames and re-types.
    """

    def __init__(self, device: Any, sub_id: Optional[int] = None) -> None:
        self._device = device
        self._sub_id = sub_id

    @property
    def features(self) -> LightFeatures:
        device = self._device
        characteristics = getattr(device.metadata, "characteristics", None)
        # From the device's own properties rather than its type id: these
        # carry per-instance overrides a cloud export can set.
        return LightFeatures(
            dimmable=bool(device.is_dimmable),
            color_temp=bool(device.supports_temperature),
            rgb=bool(device.supports_rgb),
            min_kelvin=getattr(characteristics, "min_kelvin", None) or None,
            max_kelvin=getattr(characteristics, "max_kelvin", None) or None,
        )

    async def set_power(self, on: bool) -> None:
        await self._device.set_power(1 if on else 0, self._sub_id)

    async def set_brightness(self, percent: int) -> None:
        await self._device.set_brightness(percent, self._sub_id)

    async def set_temperature(self, cync_temp: int) -> None:
        await self._device.set_temperature(cync_temp, self._sub_id)

    async def set_rgb(self, red: int, green: int, blue: int) -> None:
        await self._device.set_rgb(red, green, blue, self._sub_id)


class BleTransport:
    """A `BleMeshSession` plus the mesh address it is being pointed at.

    The session is shared across every device on the mesh, so the target has
    to be bound here - that binding is the whole difference in shape between
    the two transports.

    `is_sol_lamp` stops being the caller's problem. It is a fact about the
    device type, the session takes it as a keyword because it has no device
    to ask, and every caller was therefore working it out and passing it
    down. It is resolved here, once.
    """

    def __init__(self, session: Any, target: int, dev_type: int) -> None:
        self._session = session
        self._target = target
        self._dev_type = dev_type
        self._is_sol_lamp = is_sol_lamp(dev_type)

    @property
    def features(self) -> LightFeatures:
        return light_features(self._dev_type)

    async def set_power(self, on: bool) -> None:
        await self._session.set_power(self._target, on)

    async def set_brightness(self, percent: int) -> None:
        await self._session.set_brightness(
            self._target, percent, is_sol_lamp=self._is_sol_lamp
        )

    async def set_temperature(self, cync_temp: int) -> None:
        # The name that did not match: set_colour_temp there,
        # set_temperature here, same 0-100 value either way.
        await self._session.set_colour_temp(
            self._target, cync_temp, is_sol_lamp=self._is_sol_lamp
        )

    async def set_rgb(self, red: int, green: int, blue: int) -> None:
        await self._session.set_rgb(self._target, red, green, blue)


def protocol(kind: str, **kwargs: Any) -> CyncTransport:
    """`protocol("ble", session=..., target=..., dev_type=...)`.

    A factory rather than two imports, so a caller can be handed the name of
    a transport - from config, from a test parameter - without knowing which
    class implements it.
    """
    kinds = {"tcp": TcpTransport, "wifi": TcpTransport, "ble": BleTransport}
    try:
        cls = kinds[kind.strip().casefold()]
    except KeyError:
        raise ValueError(
            f"unknown transport {kind!r}; expected one of {sorted(set(kinds))}"
        ) from None
    return cls(**kwargs)
