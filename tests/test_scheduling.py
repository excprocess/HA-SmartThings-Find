"""Per-device polling: a device inside a Home Assistant zone is polled on the in-zone interval, any
other on the general one, and each is only fetched when it is due (fake clock, fake Samsung)."""
import asyncio
import types
from datetime import timedelta
from unittest.mock import MagicMock

from _ha_stubs import Checker, load

st = load()
check = Checker()

clock = {"t": 1000.0}
st.time = types.SimpleNamespace(monotonic=lambda: clock["t"])
calls = []                                   # (seconds since start, device name) for every real fetch
state = {"in_zone": {"A": True, "B": False}, "fail": set(), "current": None}


async def fake_location(hass, session, dev_data, entry_id):
    calls.append((clock["t"] - 1000, dev_data["name"]))
    if dev_data["name"] in state["fail"]:
        return {"update_success": False, "location_found": False}
    return {"update_success": True, "location_found": True, "who": dev_data["name"],
            "used_loc": {"latitude": 1.0, "longitude": 2.0, "gps_accuracy": 5}}


async def no_ble(*args, **kwargs):
    return {}


st.get_device_location = fake_location
st.get_device_ble_metadata = no_ble
st.update_device_maps_link = lambda *args, **kwargs: None
st.is_in_active_zone = lambda hass, loc: state["in_zone"][state["current"]]
_schedule = st.SmartThingsFindCoordinator.schedule_device


def schedule(self, dev_data, tag_data, now):          # tells the fake zone check which device it is for
    state["current"] = dev_data["name"]
    return _schedule(self, dev_data, tag_data, now)


st.SmartThingsFindCoordinator.schedule_device = schedule


def make(away, in_zone):
    devices = [{"data": {"device_id": "a", "name": "A"}}, {"data": {"device_id": "b", "name": "B"}}]
    return st.SmartThingsFindCoordinator(MagicMock(), MagicMock(), devices, away, in_zone, "e"), devices


async def tick(coordinator, advance):
    clock["t"] += advance
    coordinator.data = await coordinator._async_update_data()


def fetched_since(n):
    return [name for _, name in calls[n:]]


async def main():
    print("away=120s, in-zone=1800s (A inside a zone, B away)")
    c, devs = make(120, 1800)
    check("coordinator ticks at the shorter interval", c.update_interval, timedelta(seconds=120))
    n = len(calls); await tick(c, 0)
    check("first cycle fetches everything", fetched_since(n), ["A", "B"])
    check("A is on the in-zone interval", devs[0]["data"]["_poll_interval_s"], 1800)
    check("B is on the general interval", devs[1]["data"]["_poll_interval_s"], 120)
    check("sensor attributes for A", (c.data["a"]["in_zone"], c.data["a"]["polling_interval"]), (True, 1800))
    check("sensor attributes for B", (c.data["b"]["in_zone"], c.data["b"]["polling_interval"]), (False, 120))
    n = len(calls); await tick(c, 125)
    check("next tick: only B is due", fetched_since(n), ["B"])
    check("A kept its data without a fetch", c.data["a"]["who"], "A")
    for _ in range(13):
        await tick(c, 125)
    n = len(calls); await tick(c, 125)
    check("after ~30 min A is due again", "A" in fetched_since(n), True)

    print("A leaves its zone")
    state["in_zone"]["A"] = False
    n = len(calls); await tick(c, 1800)
    check("A is refetched once its in-zone interval has run out", "A" in fetched_since(n), True)
    check("and is now on the general interval", devs[0]["data"]["_poll_interval_s"], 120)
    n = len(calls); await tick(c, 125)
    check("so it is polled again on the very next tick", "A" in fetched_since(n), True)
    state["in_zone"]["A"] = True
    await tick(c, 125); await tick(c, 125)

    print("a failed fetch of an in-zone device")
    state["fail"] = {"A"}
    n = len(calls); await tick(c, 1900)
    check("A is fetched when due, and fails", "A" in fetched_since(n), True)
    check("its last position is kept meanwhile", c.data["a"].get("location_found"), True)
    n = len(calls); await tick(c, 125)
    check("it is retried on the very next tick, not after the in-zone interval", "A" in fetched_since(n), True)
    state["fail"] = set()
    await tick(c, 125)
    check("once working it goes back to the in-zone interval", devs[0]["data"]["_poll_interval_s"], 1800)

    print("a persistent failure is not hidden forever")
    state["fail"] = {"B"}
    for _ in range(6):
        await tick(c, 125)
    check("B shows as failed after more than 3 failures in a row", c.data["b"].get("update_success"), False)
    state["fail"] = set()
    await tick(c, 125)
    check("and recovers by itself", c.data["b"].get("update_success"), True)

    print("in-zone interval 0 (the default) = one interval for everything, as before 8.1")
    state["in_zone"] = {"A": True, "B": False}
    c, devs = make(300, 0)
    check("tick", c.update_interval, timedelta(seconds=300))
    n = len(calls); await tick(c, 0); await tick(c, 305); await tick(c, 305)
    check("every device is polled on every tick", fetched_since(n), ["A", "B"] * 3)

    print("in-zone interval shorter than the general one")
    c, devs = make(600, 60)
    check("tick follows the shorter interval", c.update_interval, timedelta(seconds=60))
    n = len(calls); await tick(c, 0); await tick(c, 65); await tick(c, 65)
    check("A (in zone) on every tick, B (away) only on the first", fetched_since(n), ["A", "B", "A", "A"])


asyncio.run(main())

print("zone check helper")
zone = __import__("sys").modules["homeassistant.components.zone"]
in_active_zone = st.utils.is_in_active_zone
position = {"latitude": 41.9, "longitude": 12.6, "gps_accuracy": 18.2}
seen = {}


def fake_zone(hass, latitude, longitude, radius=0):
    seen["args"] = (latitude, longitude, radius)
    return seen.get("result")


zone.async_active_zone = fake_zone
seen["result"] = object()
check("inside a zone", in_active_zone(MagicMock(), position), True)
check("the fix's accuracy is counted in, like the tracker does", seen["args"], (41.9, 12.6, 18.2))
seen["result"] = None
check("outside every zone", in_active_zone(MagicMock(), position), False)
check("no position = not in a zone", in_active_zone(MagicMock(), {"latitude": None, "longitude": None}), False)
check("no data = not in a zone", in_active_zone(MagicMock(), None), False)
check("a missing accuracy counts as 0", (in_active_zone(MagicMock(), {"latitude": 1, "longitude": 2}), seen["args"]),
      (False, (1, 2, 0)))


def unavailable(*args, **kwargs):
    raise RuntimeError("zones not loaded")


zone.async_active_zone = unavailable
check("a failing zone lookup = not in a zone, no exception", in_active_zone(MagicMock(), position), False)

check.finish()
