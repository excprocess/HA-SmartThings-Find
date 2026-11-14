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

check.finish()
