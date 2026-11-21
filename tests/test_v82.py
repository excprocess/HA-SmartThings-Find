"""8.2.0 features: new-fix timestamp, connection parsing/check, sanitized structures, device classes
and the opt-in 'unknown after N failed attempts' rule."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from _ha_stubs import Checker, load

st = load()
u = st.utils
check = Checker()

# --- sanitized structure never leaks coordinates / ids, keeps status-like values -------------
op = {"oprnType": "CHECK_CONNECTION", "oprnStsCd": "2800", "latitude": "41.9", "longitude": "12.6",
      "reqId": "secret-req", "dvceID": "secret-dev", "connectionType": "WIFI", "ssid": "HomeWifi",
      "extra": {"gpsUtcDt": "20260101000000", "networkType": "LTE", "usrId": "me@example.com"}}
shape = u.sanitize_structure(op)
text = json.dumps(shape)
check("structure keeps connection value", shape["connectionType"], "WIFI")
check("structure keeps nested network value", shape["extra"]["networkType"], "LTE")
check("structure hides coordinates", "41.9" in text or "12.6" in text, False)
check("structure hides ids/ssid/user", any(s in text for s in ("secret-req", "secret-dev", "HomeWifi", "me@example")), False)

# --- connection parsing ------------------------------------------------------------------------
for label, payload, want in [
    ("wifi by type", {"connectionType": "WIFI"}, "wifi"),
    ("mobile by network", {"extra": {"network": "LTE"}}, "mobile"),
    ("offline flag", {"online": "N"}, "offline"),
    ("online flag", {"isOnline": True}, "online"),
    ("wifi flag", {"wifi": "Y", "online": "Y"}, "wifi"),
    ("capability only is not a state", {"wifiOnly": "Y"}, "unknown"),
    ("completed CHECK_CONNECTION 1200 is online", {"oprnType": "CHECK_CONNECTION", "oprnStsCd": "2800", "oprnResultCode": "1200"}, "online"),
    ("completed CHECK_CONNECTION error is offline", {"oprnType": "CHECK_CONNECTION", "oprnStsCd": "2800", "oprnResultCode": "2040"}, "offline"),
    ("S23 Ultra CHECK_CONNECTION isConnected=True", {"oprnType": "CHECK_CONNECTION", "oprnStsCd": "2800", "oprnResultCode": "1200", "extra": {"isConnected": True}}, "online"),
    ("nothing readable", {"oprnType": "CHECK_CONNECTION", "battery": "FULL"}, "unknown"),
    ("not a dict", None, "unknown"),
]:
    check(f"parse: {label}", u.parse_connection_state(payload)[0], want)
check(
    "parse: S23 Ultra CHECK_CONNECTION + LOCATION seData.netType=wifi",
    u.parse_connection_state(
        {"oprnType": "CHECK_CONNECTION", "oprnStsCd": "2800", "oprnResultCode": "1200", "extra": {"isConnected": True}},
        fallback_op={"oprnType": "LOCATION", "extra": {"seData": {"netType": "wifi"}}},
    )[0],
    "wifi",
)

# --- classify_fmm_operation_result (samsung-re-find state machine) ------------------------------
for label, op_entry, want in [
    ("2800+1200 is success", {"oprnStsCd": "2800", "oprnResultCode": "1200"}, "success"),
    ("2800+2040 is failed", {"oprnStsCd": "2800", "oprnResultCode": "2040"}, "failed"),
    ("1000 is pending", {"oprnStsCd": "1000"}, "pending"),
    ("1100 is pending", {"oprnStsCd": "1100"}, "pending"),
    ("TRACK_LOCATION_START 2100 is success", {"oprnType": "TRACK_LOCATION_START", "oprnStsCd": "2100"}, "success"),
    ("other 2100 is pending", {"oprnType": "LOCATION", "oprnStsCd": "2100"}, "pending"),
]:
    check(f"classify op: {label}", u.classify_fmm_operation_result(op_entry), want)

# --- device classes -----------------------------------------------------------------------------
def dev(code, fmm=True):
    return {"is_fmm": fmm, "web_device_type_code": code}

for code, active in [("PHONE", True), ("PHONE DEVICE", True), ("TABLET", True), ("PC", True),
                     ("WATCH", False), ("BUDS", False), ("HEARABLE", False)]:
    check(f"active-connectable {code}", u.is_active_connectable_device(dev(code)), active)
check("tag is never active-connectable", u.is_active_connectable_device(dev("PHONE", fmm=False)), False)
check("watch detected", u.is_watch_device(dev("WATCH")), True)
check("watch detected via WEARABLE + subType WATCH", u.is_watch_device({"is_fmm": True, "web_device_type_code": "WEARABLE", "web_sub_type": "WATCH"}), True)
check("watch detected via watch.svg icon", u.is_watch_device({"is_fmm": True, "web_device_type_code": "WEARABLE", "icon_url": "https://smartthingsfind.samsung.com/img/device_icon/watch.svg"}), True)

# --- register_position_fix ---------------------------------------------------------------------
t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

def reading(gps_date, ok=True):
    return {"update_success": ok, "location_found": ok,
            "used_loc": {"latitude": 1.0, "longitude": 2.0, "gps_date": gps_date}}

d = {}
check("first fix is new", u.register_position_fix(d, reading(t0)), True)
stamp = d["_last_new_fix_at"]
check("same fix is not new", u.register_position_fix(d, reading(t0)), False)
check("older fix is not new", u.register_position_fix(d, reading(t0 - timedelta(hours=1))), False)
check("timestamp untouched by non-new reads", d["_last_new_fix_at"], stamp)
check("failed read is not new", u.register_position_fix(d, reading(t0 + timedelta(hours=1), ok=False)), False)
check("newer fix is new", u.register_position_fix(d, reading(t0 + timedelta(hours=1))), True)
restored = {"_known_gps_date": t0, "_last_new_fix_at": t0}
check("restored position is not a new fix", u.register_position_fix(restored, reading(t0)), False)
check("restored timestamp kept", restored["_last_new_fix_at"], t0)

# --- check_fmm_connection -----------------------------------------------------------------------
calls = []

def fake_operation(answer, success=True):
    async def run(hass, session, entry_id, dev_data, operation, **kwargs):
        calls.append(operation)
        if "result_sink" in kwargs and answer is not None:
            kwargs["result_sink"]["operation"] = answer
            kwargs["result_sink"]["samsung_side"] = True
        return success, None
    return run

async def conn(answer, **kwargs):
    u.perform_fmm_operation = fake_operation(answer)
    data = {"name": "Phone", "device_id": "p"}
    return data, await u.check_fmm_connection(MagicMock(), MagicMock(), "e", data, **kwargs)

data, result = asyncio.run(conn({"online": "N", "oprnType": "CHECK_CONNECTION"}))
check("offline answer -> offline", result["state"], "offline")
check("structure stored", result["structure"]["online"], "N")
check("stored on device", data["_connection"] is result, True)
data, result = asyncio.run(conn(None))
check("no answer -> unknown, not offline", result["state"], "unknown")
calls.clear()
u.perform_fmm_operation = fake_operation({"connectionType": "WIFI"})
data = {"name": "Phone"}
asyncio.run(u.check_fmm_connection(MagicMock(), MagicMock(), "e", data))
asyncio.run(u.check_fmm_connection(MagicMock(), MagicMock(), "e", data))
check("cached within TTL: one request", len(calls), 1)
asyncio.run(u.check_fmm_connection(MagicMock(), MagicMock(), "e", data, use_cache=False))
check("use_cache=False asks again", len(calls), 2)

# --- 'unknown after N failed attempts' -----------------------------------------------------------
def coordinator(threshold):
    return st.SmartThingsFindCoordinator(
        MagicMock(), MagicMock(), [], 120, 120, "e", unknown_after_failed_attempts=threshold)

def cycle(c, dev_data, new_fix=False, counted=True):
    tag = {}
    c.apply_position_policy(dev_data, tag, new_fix, counted)
    return tag

c = coordinator(2)
phone = {"is_fmm": True, "web_device_type_code": "PHONE", "active_location_requested": True}
check("1st failed attempt: still known", cycle(c, phone)["position_unknown"], False)
check("2nd failed attempt: unknown", cycle(c, phone)["position_unknown"], True)
check("local error (not counted) keeps state", cycle(c, phone, counted=False)["position_unknown"], True)
check("genuine new fix restores", cycle(c, phone, new_fix=True, counted=False)["position_unknown"], False)
check("counter restarts", cycle(c, phone)["failed_attempts"], 1)
for code in ("WATCH", "BUDS", "HEARABLE"):
    other = {"is_fmm": True, "web_device_type_code": code, "active_location_requested": True}
    for _ in range(5):
        tag = cycle(c, other)
    check(f"{code} never unknown", tag["position_unknown"], False)
tracker = {"is_tracker": True, "active_location_requested": True}
for _ in range(5):
    tag = cycle(c, tracker)
check("SmartTag never unknown", tag["position_unknown"], False)
tablet = {"is_fmm": True, "web_device_type_code": "TABLET", "active_location_requested": True}
cycle(c, tablet)
check("tablet treated like a phone", cycle(c, tablet)["position_unknown"], True)
pc = {"is_fmm": True, "web_device_type_code": "PC", "active_location_requested": True}
cycle(c, pc)
check("PC follows the rule", cycle(c, pc)["position_unknown"], True)
phone["active_location_requested"] = False
check("switch off -> position valid again", cycle(c, phone)["position_unknown"], False)
phone["active_location_requested"] = True
check("switch back on starts from 0", cycle(c, phone)["failed_attempts"], 1)
off = coordinator(0)
for _ in range(10):
    tag = cycle(off, phone)
check("0 disables the rule", tag["position_unknown"], False)

# --- FMM and Buds Left/Right battery extraction (samsung-re-find PR #2) -------------------------
check("is_buds_device via is_buds flag", u.is_buds_device({"is_fmm": True, "is_buds": True}), True)
check("is_buds_device via BUDS code", u.is_buds_device({"is_fmm": True, "web_device_type_code": "BUDS"}), True)
check("is_buds_device via CANAL2 subType", u.is_buds_device({"is_fmm": True, "web_device_type_code": "WEARABLE", "web_sub_type": "CANAL2"}), True)
check("is_buds_device via Emiliano's Buds name", u.is_buds_device({"is_fmm": True, "name": "Emiliano's Buds"}), True)
check("is_buds_device false for phone", u.is_buds_device({"is_fmm": True, "web_device_type_code": "PHONE", "name": "S23 Ultra"}), False)

buds_ops = [
    {
        "oprnType": "CHECK_CONNECTION",
        "oprnStsCd": "2800",
        "oprnResultCode": "1200",
        "extra": {
            "left": {"battery": "100"},
            "right": {"battery": "87"},
        },
    }
]
check(
    "extract_fmm_battery reads Buds Left/Right battery channels and sets min fallback",
    u.extract_fmm_battery(buds_ops),
    {"battery_level": 87, "left_battery_level": 100, "right_battery_level": 87},
)

buds_json_extra_ops = [
    {
        "oprnType": "CHECK_CONNECTION",
        "oprnStsCd": "2800",
        "oprnResultCode": "1200",
        "extra": '{"left": {"battery": "92"}, "right": {"battery": "96"}}',
    }
]
check(
    "extract_fmm_battery handles JSON-encoded extra string",
    u.extract_fmm_battery(buds_json_extra_ops),
    {"battery_level": 92, "left_battery_level": 92, "right_battery_level": 96},
)

invalid_buds_ops = [
    {
        "oprnType": "CHECK_CONNECTION",
        "battery": "not-a-number",
        "extra": {
            "left": {"battery": "not-a-number"},
            "right": {"battery": None},
        },
    }
]
check(
    "extract_fmm_battery ignores invalid battery values",
    u.extract_fmm_battery(invalid_buds_ops),
    {"battery_level": None, "left_battery_level": None, "right_battery_level": None},
)

phone_ops = [
    {
        "oprnType": "CHECK_CONNECTION",
        "battery": "84",
        "extra": {"battery": "84", "isConnected": True},
    }
]
check(
    "extract_fmm_battery reads general FMM battery",
    u.extract_fmm_battery(phone_ops),
    {"battery_level": 84, "left_battery_level": None, "right_battery_level": None},
)
check(
    "extract_fmm_battery maps symbolic FULL battery",
    u.extract_fmm_battery([{"oprnType": "CHECK_CONNECTION", "battery": "FULL"}]),
    {"battery_level": 100, "left_battery_level": None, "right_battery_level": None},
)

# Active-connectable battery is unknown (None) when Active Location is off; passive devices keep last known
phone_dev_data = {
    "device_id": "phone1",
    "name": "Phone",
    "is_fmm": True,
    "web_device_type_code": "PHONE",
    "active_location_requested": False,
    "_last_battery": {"battery_level": 84},
}
check(
    "phone battery is unknown (None) when Active Location is off",
    u.resolve_device_battery_value(phone_dev_data, {"battery_level": 84}),
    None,
)
phone_dev_data["active_location_requested"] = True
check(
    "phone battery is shown when Active Location is on",
    u.resolve_device_battery_value(phone_dev_data, {"battery_level": 84}),
    84,
)

tablet_dev_data = {
    "device_id": "tab1",
    "name": "Tablet",
    "is_fmm": True,
    "web_device_type_code": "TABLET",
    "active_location_requested": False,
    "_last_battery": {"battery_level": 75},
}
check(
    "tablet battery is unknown (None) when Active Location is off",
    u.resolve_device_battery_value(tablet_dev_data, {"battery_level": 75}),
    None,
)

watch_dev_data = {
    "device_id": "watch1",
    "name": "Watch",
    "is_fmm": True,
    "web_device_type_code": "WATCH",
    "active_location_requested": False,
    "_last_battery": {"battery_level": 62},
}
check(
    "watch battery keeps last known value even with Active Location off",
    u.resolve_device_battery_value(watch_dev_data, {"battery_level": None}),
    62,
)

buds_dev_data = {
    "device_id": "buds1",
    "name": "Buds",
    "is_fmm": True,
    "is_buds": True,
    "web_device_type_code": "BUDS",
    "active_location_requested": False,
    "_last_battery": {"left_battery_level": 95, "right_battery_level": 80},
}
check(
    "Buds left battery keeps last known value passively",
    u.resolve_device_battery_value(buds_dev_data, {}, side="left"),
    95,
)
check(
    "Buds right battery keeps last known value passively",
    u.resolve_device_battery_value(buds_dev_data, {}, side="right"),
    80,
)

# Mobile 'online' -> 'mobile', PC stays 'online'
u.perform_fmm_operation = fake_operation(
    {"oprnType": "CHECK_CONNECTION", "oprnStsCd": "2800", "oprnResultCode": "1200", "extra": {"isConnected": True}}
)
mob_data = {"name": "S23 Ultra", "device_id": "s23", "is_fmm": True, "web_device_type_code": "PHONE"}
mob_res = asyncio.run(u.check_fmm_connection(MagicMock(), MagicMock(), "e", mob_data, use_cache=False))
check("mobile device online CHECK_CONNECTION maps to 'mobile'", mob_res["state"], "mobile")

pc_data = {"name": "PC", "device_id": "pc1", "is_fmm": True, "web_device_type_code": "PC"}
pc_res = asyncio.run(u.check_fmm_connection(MagicMock(), MagicMock(), "e", pc_data, use_cache=False))
check("PC device online CHECK_CONNECTION stays 'online'", pc_res["state"], "online")

check.finish()
