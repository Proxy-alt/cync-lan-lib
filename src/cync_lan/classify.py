"""What a device type *is*, independent of how you talk to it.

Every one of these answers comes from `metadata/model_info.py`'s static
per-type data, so none of it depends on a transport, a connection, or a
running session - and none of it imports Home Assistant. That is the whole
reason it lives here: both Home Assistant integrations need the same
answers, an integration cannot import another integration, and a library is
the only channel they share.

It was written twice before this. `cync_ble/classify.py` said so in its own
docstring - "mirrors cync_lan.devices.CyncDevice's is_light/is_switch logic
exactly" - and it did, for `is_light`: checked against all 157 known types,
the two implementations agreed on every one. `is_dimmable` had drifted on
13 of them, which is the story below.

`CyncDevice`'s properties still exist and still take their per-instance
overrides; they defer here for the computation rather than repeating it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from cync_lan.metadata.model_info import (
    DeviceClassification,
    DeviceTypeInfo,
    device_type_map,
)


def type_info(dev_type: int) -> Optional[DeviceTypeInfo]:
    return device_type_map.get(dev_type)


def is_light_info(info: Optional[DeviceTypeInfo]) -> bool:
    """Whether this type belongs on Home Assistant's `light` platform.

    Not the same question as "is it classified LIGHT". A dimmable switch is
    dimming a light, and Cync sells fan speed control as its own dedicated
    Fan Controller product, so any *other* dimmable switch is safe to treat
    as a light dimmer. Routing those to `switch` instead loses dimming
    outright - that domain has no brightness concept - which is exactly what
    happened when the wired-switch family was reclassified to SWITCH and a
    real user reported their dimmable switches had become binary ones.
    """
    if info is None:
        return False
    if info.type == DeviceClassification.LIGHT:
        return True
    if info.type == DeviceClassification.SWITCH:
        caps = info.capabilities
        return bool(caps and caps.dimmable and not caps.fan and not caps.plug)
    return False


def is_switch_info(info: Optional[DeviceTypeInfo]) -> bool:
    """A plain binary switch, and nothing else.

    Mirrors is_light_info's carve-out from the other side: whatever claims
    the light platform must not also claim this one, or the same physical
    device gets two entities. Fan controllers get their own richer entity on
    the fan platform.
    """
    if info is None or info.type != DeviceClassification.SWITCH:
        return False
    return not is_light_info(info) and not is_fan_controller_info(info)


def is_plug_info(info: Optional[DeviceTypeInfo]) -> bool:
    if info is None or info.type != DeviceClassification.SWITCH:
        return False
    return bool(info.capabilities and info.capabilities.plug)


def is_fan_controller_info(info: Optional[DeviceTypeInfo]) -> bool:
    if info is None or info.type != DeviceClassification.SWITCH:
        return False
    return bool(info.capabilities and info.capabilities.fan)


def is_dimmable_info(info: Optional[DeviceTypeInfo]) -> bool:
    """Whether the hardware can dim at all - a capability, not a category.

    The two copies disagreed here, on 13 of 157 types: every dimmable
    switch (36, 37, 48, 49, 54, 55, 56, 96, 112, 116, 117, 124, 125).
    `CyncDevice.is_dimmable` additionally required the type be classified
    LIGHT, `cync_ble` went by the capability alone, and nothing reconciled
    them because they lived in different repositories.

    The capability reading wins, because the narrow one made its own callers
    unsatisfiable. `is_dimmable and not is_light` appears four times in the
    Home Assistant integration to mean "a dimmer switch" - and with
    is_dimmable gated to LIGHT, while is_light is True for every dimmable
    switch by the carve-out above, no device type could satisfy it. Two
    entity classes behind that condition had never been created for anyone.
    Use `is_dimmer_switch_info` for what those callers meant.
    """
    return bool(info and info.capabilities and info.capabilities.dimmable)


def is_dimmer_switch_info(info: Optional[DeviceTypeInfo]) -> bool:
    """A switch *product* that dims - the thing `is_dimmable and not
    is_light` was reaching for and never found.

    These live on the light platform (is_light_info is True for them), so
    "not a light" was never the way to ask. What distinguishes them is the
    product category underneath, which survives the platform routing.
    """
    if info is None or info.type != DeviceClassification.SWITCH:
        return False
    caps = info.capabilities
    return bool(caps and caps.dimmable and not caps.fan and not caps.plug)


def is_sol_lamp_info(info: Optional[DeviceTypeInfo]) -> bool:
    """Older XLink Wi-Fi-direct hardware (e.g. C by GE Sol, type 80), which
    wants 0xD2 for brightness and 0xE2 for CCT rather than the 0xF0 family."""
    return bool(info and info.opcodes.sol_lamp)


def supports_rgb_info(info: Optional[DeviceTypeInfo]) -> bool:
    if info is None or info.type != DeviceClassification.LIGHT:
        return False
    return bool(info.capabilities and info.capabilities.color)


def supports_temperature_info(info: Optional[DeviceTypeInfo]) -> bool:
    if info is None or info.type != DeviceClassification.LIGHT:
        return False
    return bool(info.capabilities and info.capabilities.tunable_white)


@dataclass(frozen=True)
class LightFeatures:
    """What a light can do, with no Home Assistant types in sight.

    Deliberately not a set of `ColorMode`s. Importing
    `homeassistant.components.light` here would put Home Assistant's release
    cadence in front of a library the CLI and the MQTT add-on also depend
    on, to save each integration about five lines. Each side maps this to
    its own platform's vocabulary instead - which is also what lets
    `cync_ble` advertise less than the hardware claims while colour temp and
    RGB are unconfirmed on that transport.
    """

    dimmable: bool = False
    color_temp: bool = False
    rgb: bool = False
    min_kelvin: Optional[int] = None
    max_kelvin: Optional[int] = None


def light_features(dev_type: int) -> LightFeatures:
    info = type_info(dev_type)
    if info is None:
        return LightFeatures()
    characteristics = getattr(info, "characteristics", None)
    return LightFeatures(
        dimmable=is_dimmable_info(info),
        color_temp=supports_temperature_info(info),
        rgb=supports_rgb_info(info),
        min_kelvin=getattr(characteristics, "min_kelvin", None) or None,
        max_kelvin=getattr(characteristics, "max_kelvin", None) or None,
    )


def model_name(dev_type: int) -> Optional[str]:
    info = type_info(dev_type)
    return info.model_name if info else None


# Cync speaks 0-100 everywhere; Home Assistant's light platform speaks
# 0-255. Both integrations were doing this arithmetic inline, which is
# harmless right up until one of them rounds differently from the other for
# the same bulb.
def to_ha_brightness(percent: int) -> int:
    """0-100 as the device reports it -> 0-255 as Home Assistant wants it."""
    return round(max(0, min(100, percent)) * 255 / 100)


def from_ha_brightness(value: int) -> int:
    """0-255 from Home Assistant -> the 0-100 the device expects."""
    return round(max(0, min(255, value)) * 100 / 255)


# Type-int conveniences. The *_info functions take metadata because
# CyncDevice already holds it; these take the type id because an integration
# working from a cloud export has that and nothing else.
def is_light(dev_type: int) -> bool:
    return is_light_info(type_info(dev_type))


def is_switch(dev_type: int) -> bool:
    return is_switch_info(type_info(dev_type))


def is_plug(dev_type: int) -> bool:
    return is_plug_info(type_info(dev_type))


def is_fan_controller(dev_type: int) -> bool:
    return is_fan_controller_info(type_info(dev_type))


def is_dimmable(dev_type: int) -> bool:
    return is_dimmable_info(type_info(dev_type))


def is_dimmer_switch(dev_type: int) -> bool:
    return is_dimmer_switch_info(type_info(dev_type))


def is_sol_lamp(dev_type: int) -> bool:
    return is_sol_lamp_info(type_info(dev_type))
