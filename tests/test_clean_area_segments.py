"""Regression tests for HA's native "clean area" support in vacuum.py.

- async_get_segments read cloud_coordinator.data["pmaps"], but the cloud
  coordinator keys its data by robot BLID, so no segment was ever reported and
  the area mapping dialog in Home Assistant stayed empty.
- async_clean_segments passed the cleanRoom payload as the whole service data
  of roomba_rest980.rest980_clean, whose schema requires {payload, base_url}.
- The cleanRoom payload carried no user_pmapv_id, which the robot needs to
  accept a room clean (the iRobot app and favorites always send it).
"""

import asyncio
import enum
import sys
import types
from dataclasses import dataclass

import _ha_stub_setup  # noqa: F401  (registers the shared HA stubs)

import pytest

# ---------------------------------------------------------------------------
# Extra stubs vacuum.py needs on top of _ha_stub_setup
# ---------------------------------------------------------------------------
vacuum_mod = types.ModuleType("homeassistant.components.vacuum")


class VacuumEntityFeature(enum.IntFlag):
    START = 1
    RETURN_HOME = 2
    CLEAN_SPOT = 4
    MAP = 8
    SEND_COMMAND = 16
    STATE = 32
    STOP = 64
    PAUSE = 128
    CLEAN_AREA = 256


class VacuumActivity(str, enum.Enum):
    IDLE = "idle"
    CLEANING = "cleaning"
    PAUSED = "paused"
    DOCKED = "docked"
    RETURNING = "returning"
    ERROR = "error"


class StateVacuumEntity:
    pass


@dataclass
class Segment:
    id: str
    name: str
    group: str | None = None


vacuum_mod.VacuumEntityFeature = VacuumEntityFeature
vacuum_mod.VacuumActivity = VacuumActivity
vacuum_mod.StateVacuumEntity = StateVacuumEntity
vacuum_mod.Segment = Segment

config_entries_mod = types.ModuleType("homeassistant.config_entries")
config_entries_mod.ConfigEntry = object

exceptions_mod = types.ModuleType("homeassistant.exceptions")


class ServiceValidationError(Exception):
    pass


exceptions_mod.ServiceValidationError = ServiceValidationError

sys.modules["homeassistant.components.vacuum"] = vacuum_mod
sys.modules["homeassistant.config_entries"] = config_entries_mod
sys.modules["homeassistant.exceptions"] = exceptions_mod

_ha_stub_setup._load_real_module("LegacyCompatibility")
vacuum = _ha_stub_setup._load_real_module("vacuum")

BLID = "ROBOT1"
OTHER_BLID = "ROBOT2"
BASE_URL = "http://rest980:3000"


def _pmap(pmap_id, user_pmapv_id, name, regions, zones=()):
    return {
        "pmap_id": pmap_id,
        "user_pmapv_id": user_pmapv_id,
        "active_pmapv_details": {
            "active_pmapv": {"pmap_id": pmap_id},
            "map_header": {"name": name},
            "regions": [{"id": rid, "name": rname} for rid, rname in regions],
            "zones": [{"id": zid, "name": zname} for zid, zname in zones],
        },
    }


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, service_data=None, blocking=False):
        self.calls.append((domain, service, service_data))


class FakeHass:
    def __init__(self):
        self.services = FakeServices()


def _make_vacuum(cloud_data):
    entry = types.SimpleNamespace(
        unique_id="entry1",
        title="Robot",
        data={"base_url": BASE_URL},
        runtime_data=types.SimpleNamespace(
            robot_blid=BLID,
            cloud_coordinator=types.SimpleNamespace(data=cloud_data),
            vacuum_mode="vacuum",
            mop_mode="medium",
        ),
    )
    hass = FakeHass()
    return vacuum.RoombaVacuum(hass, types.SimpleNamespace(data={}), entry), hass


CLOUD = {
    BLID: {
        "pmaps": [
            _pmap("PM1", "UV1", "Ground", [("9", "Living Room"), ("10", "Media Room")]),
            _pmap("PM2", "UV2", "Upstairs", [("1", "Bedroom")], zones=[("3", "Rug")]),
        ]
    },
    OTHER_BLID: {
        "pmaps": [_pmap("PMX", "UVX", "Other robot", [("1", "Kitchen")])]
    },
}


def test_segments_come_from_this_robots_maps_only():
    entity, _ = _make_vacuum(CLOUD)
    segments = asyncio.run(entity.async_get_segments())
    assert [(s.id, s.name, s.group) for s in segments] == [
        ("9:rid:PM1", "Living Room", "Ground"),
        ("10:rid:PM1", "Media Room", "Ground"),
        ("1:rid:PM2", "Bedroom", "Upstairs"),
        ("3:zid:PM2", "Rug", "Upstairs"),
    ]


def test_segments_empty_without_cloud_data():
    entity, _ = _make_vacuum({})
    assert asyncio.run(entity.async_get_segments()) == []


def test_clean_segments_calls_service_with_payload_and_base_url():
    entity, hass = _make_vacuum(CLOUD)
    asyncio.run(entity.async_clean_segments(["9:rid:PM1", "10:rid:PM1"]))
    assert len(hass.services.calls) == 1
    domain, service, data = hass.services.calls[0]
    assert (domain, service) == ("roomba_rest980", "rest980_clean")
    assert data["base_url"] == BASE_URL
    payload = data["payload"]
    assert payload["pmap_id"] == "PM1"
    assert payload["user_pmapv_id"] == "UV1"
    assert [(r["type"], r["region_id"]) for r in payload["regions"]] == [
        ("rid", "9"),
        ("rid", "10"),
    ]


def test_clean_segments_across_maps_is_rejected():
    entity, hass = _make_vacuum(CLOUD)
    with pytest.raises(ServiceValidationError):
        asyncio.run(entity.async_clean_segments(["9:rid:PM1", "1:rid:PM2"]))
    assert hass.services.calls == []
