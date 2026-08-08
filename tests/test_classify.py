"""One implementation of "what is this device", checked against all of them.

These run over the whole of `device_type_map` rather than a handful of
chosen ids, because the bug this module exists to end was a disagreement on
13 of 157 types that no single-example test would have found.
"""

from __future__ import annotations

from cync_lan import classify
from cync_lan.devices import CyncDevice
from cync_lan.metadata.model_info import DeviceClassification, device_type_map


def _device(dev_type: int) -> CyncDevice:
    return CyncDevice(dev_id=1, dev_type=dev_type)


def test_cync_device_and_classify_agree_on_every_known_type():
    """CyncDevice's properties defer to classify; nothing may re-derive.

    A second implementation is how the drift started, so this asserts there
    is not one rather than asserting a list of expected values.
    """
    disagreements = []
    for dev_type in device_type_map:
        device = _device(dev_type)
        for name, from_device, from_classify in (
            ("is_light", device.is_light, classify.is_light(dev_type)),
            ("is_switch", device.is_switch, classify.is_switch(dev_type)),
            ("is_plug", device.is_plug, classify.is_plug(dev_type)),
            (
                "is_fan_controller",
                device.is_fan_controller,
                classify.is_fan_controller(dev_type),
            ),
            ("is_dimmable", device.is_dimmable, classify.is_dimmable(dev_type)),
            ("is_sol_lamp", device.is_sol_lamp, classify.is_sol_lamp(dev_type)),
        ):
            if from_device != from_classify:
                disagreements.append((dev_type, name, from_device, from_classify))
    assert not disagreements


def test_a_dimmable_switch_is_a_light_and_not_a_switch():
    """The carve-out a real user report produced: routing these to the
    switch platform loses dimming, because that domain has no brightness."""
    dimmable_switches = [
        t
        for t, i in device_type_map.items()
        if i.type == DeviceClassification.SWITCH
        and i.capabilities
        and i.capabilities.dimmable
        and not i.capabilities.fan
        and not i.capabilities.plug
    ]
    assert dimmable_switches, "the fixture data no longer contains any"
    for dev_type in dimmable_switches:
        assert classify.is_light(dev_type) is True
        assert classify.is_switch(dev_type) is False, (
            f"type {dev_type} would get two entities for one device"
        )


def test_nothing_claims_two_platforms():
    """is_light, is_switch and is_fan_controller partition the types they
    match - anything in two of them produces duplicate entities."""
    for dev_type in device_type_map:
        claims = [
            classify.is_light(dev_type),
            classify.is_switch(dev_type),
            classify.is_fan_controller(dev_type),
        ]
        assert sum(claims) <= 1, f"type {dev_type} claims {claims}"


def test_is_dimmable_follows_the_capability_not_the_category():
    """The drift, pinned. `CyncDevice.is_dimmable` required the type be
    classified LIGHT while cync_ble went by capability; they disagreed on
    every dimmable switch, and the narrow reading made its own callers
    unsatisfiable."""
    dimmable_switches = [
        t
        for t, i in device_type_map.items()
        if i.type == DeviceClassification.SWITCH
        and i.capabilities
        and i.capabilities.dimmable
    ]
    assert len(dimmable_switches) == 11, (
        "the drifted set changed size - it was 11 dimmable switches, plus "
        "types 96 and 112 which are SENSOR-classified with a stray "
        "dimmable=True, making 13 disagreements in all"
    )
    for dev_type in dimmable_switches:
        assert classify.is_dimmable(dev_type) is True
        assert _device(dev_type).is_dimmable is True


def test_is_dimmer_switch_is_satisfiable_and_means_switch_product():
    """`is_dimmable and not is_light` matched no device type at all, which
    is why two entity classes behind it were never created. This is what
    those callers meant, and it has to actually match something."""
    matches = [t for t in device_type_map if classify.is_dimmer_switch(t)]
    assert matches, "the replacement idiom is as unsatisfiable as the old one"

    for dev_type in matches:
        info = device_type_map[dev_type]
        assert info.type == DeviceClassification.SWITCH
        assert classify.is_dimmable(dev_type) is True
        # They live on the light platform, which is precisely why "not a
        # light" was never the way to ask the question.
        assert classify.is_light(dev_type) is True
        assert not info.capabilities.fan
        assert not info.capabilities.plug


def test_per_instance_overrides_still_win():
    """The properties carry setters that parsing uses; deferring to
    classify must not take those away."""
    device = _device(next(iter(device_type_map)))
    device.is_light = True
    device.is_switch = False
    assert device.is_light is True
    assert device.is_switch is False


def test_an_unknown_type_claims_nothing():
    unknown = max(device_type_map) + 1000
    assert classify.type_info(unknown) is None
    for fn in (
        classify.is_light,
        classify.is_switch,
        classify.is_plug,
        classify.is_fan_controller,
        classify.is_dimmable,
        classify.is_dimmer_switch,
        classify.is_sol_lamp,
    ):
        assert fn(unknown) is False, fn.__name__
    assert classify.model_name(unknown) is None
    assert classify.light_features(unknown) == classify.LightFeatures()


def test_light_features_carries_no_home_assistant_types():
    """The module must stay importable without Home Assistant - the CLI and
    the MQTT add-on depend on this package too."""
    features = classify.light_features(next(iter(device_type_map)))
    assert isinstance(features.dimmable, bool)

    # Parsed rather than grepped: the module's own docstrings name
    # `homeassistant.components.light` to explain why it is not imported,
    # and a substring check calls that a violation.
    import ast

    tree = ast.parse(open(classify.__file__).read())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [m for m in imported if m.split(".")[0] == "homeassistant"], (
        "classify.py imported Home Assistant, which puts HA's release "
        f"cadence in front of the CLI and the add-on: {imported}"
    )


def test_brightness_round_trips_within_a_step():
    """Both integrations did this arithmetic inline. 0-100 -> 0-255 -> 0-100
    cannot land more than a percent away, or a slider drifts as it is used."""
    for percent in range(0, 101):
        assert (
            abs(
                classify.from_ha_brightness(classify.to_ha_brightness(percent))
                - percent
            )
            <= 1
        )
    assert classify.to_ha_brightness(0) == 0
    assert classify.to_ha_brightness(100) == 255
    assert classify.from_ha_brightness(255) == 100


def test_brightness_helpers_clamp_rather_than_raise():
    """A device reporting nonsense must not take an entity's state with it."""
    assert classify.to_ha_brightness(-5) == 0
    assert classify.to_ha_brightness(1000) == 255
    assert classify.from_ha_brightness(-1) == 0
    assert classify.from_ha_brightness(9999) == 100


# ---------------------------------------------------------------------------
# Colour temperature: kelvin outside, 0-100 on the wire
# ---------------------------------------------------------------------------


def test_kelvin_converts_into_the_range_the_wire_accepts():
    """`CyncDevice.set_temperature` refuses anything over 100, so a kelvin
    value passed straight through is not merely imprecise - it is dropped,
    with an error logged and no packet sent. That is exactly what the Home
    Assistant integration did for every colour-temperature change."""
    features = classify.LightFeatures(color_temp=True, min_kelvin=2000, max_kelvin=7000)
    for kelvin in range(2000, 7001, 100):
        cync = classify.kelvin_to_cync(kelvin, features)
        assert 0 <= cync <= 100, f"{kelvin}K produced {cync}, which the wire rejects"


def test_kelvin_round_trips_close_enough_to_be_stable():
    """A slider that moves on its own when nothing changed is worse than one
    that is slightly coarse. 0-100 over a 5000K span is 50K per step, so a
    round trip has to land inside one step."""
    features = classify.LightFeatures(color_temp=True, min_kelvin=2000, max_kelvin=7000)
    step = (7000 - 2000) / 100
    for kelvin in range(2000, 7001, 137):
        back = classify.cync_to_kelvin(
            classify.kelvin_to_cync(kelvin, features), features
        )
        assert abs(back - kelvin) <= step, f"{kelvin}K -> {back}K"


def test_kelvin_clamps_outside_the_declared_range():
    features = classify.LightFeatures(color_temp=True, min_kelvin=2700, max_kelvin=6500)
    assert classify.kelvin_to_cync(1000, features) == 0
    assert classify.kelvin_to_cync(9000, features) == 100
    assert classify.cync_to_kelvin(-5, features) == 2700
    assert classify.cync_to_kelvin(500, features) == 6500


def test_kelvin_uses_the_documented_defaults_when_a_type_declares_none():
    """Seven known types carry min_kelvin with no max; four carry both."""
    assert classify.cync_to_kelvin(0) == classify.DEFAULT_MIN_KELVIN
    assert classify.cync_to_kelvin(100) == classify.DEFAULT_MAX_KELVIN
    partial = classify.LightFeatures(color_temp=True, min_kelvin=2700, max_kelvin=None)
    assert classify.cync_to_kelvin(0, partial) == 2700
    assert classify.cync_to_kelvin(100, partial) == classify.DEFAULT_MAX_KELVIN


def test_nonsense_metadata_does_not_divide_by_zero():
    """A type whose max is at or below its min must not take the entity down.

    It falls back to the documented defaults rather than propagating the
    broken range, so the answers stay inside what the wire accepts - the
    point is that nothing raises and nothing lands outside 0-100.
    """
    broken = classify.LightFeatures(color_temp=True, min_kelvin=5000, max_kelvin=5000)
    for kelvin in (1000, 4000, 6000, 9000):
        assert 0 <= classify.kelvin_to_cync(kelvin, broken) <= 100
    for cync in (-5, 0, 50, 100, 500):
        assert classify.cync_to_kelvin(cync, broken) > 0
    # and it agrees with the default range, rather than inventing a third one
    assert classify.kelvin_to_cync(4000, broken) == classify.kelvin_to_cync(4000)
