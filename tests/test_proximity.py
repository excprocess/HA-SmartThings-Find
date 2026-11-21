"""Tests for the Proximity / Direction state machine (Home / Work) and anti-false-positive safeguards."""
from datetime import datetime, timedelta, timezone

from _ha_stubs import Checker, load

st = load()
u = st.utils
check = Checker()

# Reference zones along a north-south line around Rome (~111.1 km per degree of latitude):
# Home at 41.9000, 12.5000 (radius 100m)
# Work at 41.9500, 12.5000 (~5.56 km north of Home, radius 100m)
HOME = {"entity_id": "zone.home", "latitude": 41.9000, "longitude": 12.5000, "radius": 100.0}
WORK = {"entity_id": "zone.work", "latitude": 41.9500, "longitude": 12.5000, "radius": 100.0}

t0 = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def make_tag(lat, lon, gps_date, acc=15.0, pos_unknown=False):
    return {
        "update_success": True,
        "location_found": True,
        "position_unknown": pos_unknown,
        "used_loc": {
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": acc,
            "gps_date": gps_date,
        },
    }


def active_phone(active_on=True):
    return {
        "is_fmm": True,
        "web_device_type_code": "PHONE",
        "active_location_requested": active_on,
    }


# --- 1. Device eligibility -------------------------------------------------------------------
check("SmartTag is direction-supported", u.is_direction_supported_device({"is_tracker": True}), True)
check("Phone is direction-supported", u.is_direction_supported_device(active_phone()), True)
check("Tablet is direction-supported", u.is_direction_supported_device({"is_fmm": True, "web_device_type_code": "TABLET"}), True)
check("PC is direction-supported", u.is_direction_supported_device({"is_fmm": True, "web_device_type_code": "PC"}), True)
check("Buds are NOT direction-supported", u.is_direction_supported_device({"is_fmm": True, "is_buds": True, "web_device_type_code": "BUDS"}), False)
check("Watch is NOT direction-supported", u.is_direction_supported_device({"is_fmm": True, "web_device_type_code": "WATCH"}), False)

# --- 2. Active Location OFF on phone -> unknown, distances None ------------------------------
p_off = active_phone(active_on=False)
res = u.update_proximity_state(p_off, make_tag(41.9250, 12.5000, t0), HOME, WORK, now=t0)
check("phone with Active Location off -> unknown", res["state"], "unknown")
check("phone with Active Location off -> distance_home None", res["distance_home"], None)

# --- 3. SmartTag is always active (even without active_location_requested) -------------------
tag_dev = {"is_tracker": True}
res = u.update_proximity_state(tag_dev, make_tag(41.9001, 12.5000, t0), HOME, WORK, now=t0)
check("SmartTag inside Home -> at_home", res["state"], "at_home")
check("SmartTag distance_home in meters", res["distance_home"] < 20, True)

# --- 4. Real movement towards Home (southbound from halfway between Work and Home) -----------
p = active_phone(True)
u.update_proximity_state(p, make_tag(41.9300, 12.5000, t0), HOME, WORK, now=t0)
res = u.update_proximity_state(
    p, make_tag(41.9200, 12.5000, t0 + timedelta(minutes=2)), HOME, WORK, now=t0 + timedelta(minutes=2)
)
check("southbound between Work and Home -> towards_home", res["state"], "towards_home")
check("alignment_home close to 1.0", res["alignment_home"] > 0.95, True)
check("delta_home is negative", res["delta_home"] < -900, True)

# --- 5. Unit km formatting -------------------------------------------------------------------
res_km = u.update_proximity_state(
    p, make_tag(41.9150, 12.5000, t0 + timedelta(minutes=3)), HOME, WORK, distance_unit="km", now=t0 + timedelta(minutes=3)
)
check("km unit flag", res_km["distance_unit"], "km")
check("distance_home in km (~1.67)", 1.5 <= res_km["distance_home"] <= 1.8, True)

# --- 6. GPS jitter while stationary -> stationary (no false positive towards_home) -----------
p_jit = active_phone(True)
u.update_proximity_state(p_jit, make_tag(41.9250, 12.5000, t0, acc=60.0), HOME, WORK, now=t0)
# Moves 65m south (~0.00058 deg), smaller than min_move_m = max(120, 0.75*(60+60)) = 120m
res_jit = u.update_proximity_state(
    p_jit, make_tag(41.9244, 12.5000, t0 + timedelta(minutes=2), acc=60.0), HOME, WORK, now=t0 + timedelta(minutes=2)
)
check("65m GPS jitter -> stationary, not towards_home", res_jit["state"], "stationary")

# --- 7. Perpendicular movement (eastbound) -> none -------------------------------------------
p_perp = active_phone(True)
u.update_proximity_state(p_perp, make_tag(41.9250, 12.5000, t0), HOME, WORK, now=t0)
# Moves ~1 km east
res_perp = u.update_proximity_state(
    p_perp, make_tag(41.9250, 12.5120, t0 + timedelta(minutes=2)), HOME, WORK, now=t0 + timedelta(minutes=2)
)
check("eastbound perpendicular movement -> none", res_perp["state"], "none")

# --- 8. Collinear Home & Work (moving south from north of Work -> closer target Work wins) ---
p_col = active_phone(True)
u.update_proximity_state(p_col, make_tag(42.0000, 12.5000, t0), HOME, WORK, now=t0)
res_col = u.update_proximity_state(
    p_col, make_tag(41.9900, 12.5000, t0 + timedelta(minutes=2)), HOME, WORK, now=t0 + timedelta(minutes=2)
)
check("collinear southbound north of Work -> towards_work (closer first)", res_col["state"], "towards_work")

# --- 9. Large time gap (teleport after 2 hours) resets window -> unknown ---------------------
p_gap = active_phone(True)
u.update_proximity_state(p_gap, make_tag(41.9400, 12.5000, t0), HOME, WORK, now=t0)
res_gap = u.update_proximity_state(
    p_gap, make_tag(41.9100, 12.5000, t0 + timedelta(hours=2)), HOME, WORK, now=t0 + timedelta(hours=2)
)
check("2h time gap resets buffer -> unknown", res_gap["state"], "unknown")
check("samples_used reset to 1", res_gap["samples_used"], 1)

# --- 10. Temporal decay when movement stops -> decays from towards_home to stationary --------
res_decay = u.update_proximity_state(
    p, make_tag(41.9150, 12.5000, t0 + timedelta(minutes=3)), HOME, WORK, now=t0 + timedelta(minutes=25)
)
check("stale fix after 22 min decays to stationary", res_decay["state"], "stationary")

# --- 11. Implausible speed glitch (> 250 km/h) ignored ---------------------------------------
p_spd = active_phone(True)
u.update_proximity_state(p_spd, make_tag(41.9250, 12.5000, t0), HOME, WORK, now=t0)
# 20 km in 1 minute = 1200 km/h -> ignored
res_spd = u.update_proximity_state(
    p_spd, make_tag(41.7450, 12.5000, t0 + timedelta(minutes=1)), HOME, WORK, now=t0 + timedelta(minutes=1)
)
check("1200 km/h glitch ignored -> samples_used stays 1", res_spd["samples_used"], 1)
check("1200 km/h glitch -> unknown", res_spd["state"], "unknown")

# --- 12. position_unknown hides proximity state and distances --------------------------------
res_unk = u.update_proximity_state(
    p, make_tag(41.9150, 12.5000, t0 + timedelta(minutes=4), pos_unknown=True), HOME, WORK, now=t0 + timedelta(minutes=4)
)
check("position_unknown -> unknown", res_unk["state"], "unknown")
check("position_unknown -> distance_home None", res_unk["distance_home"], None)

# --- 13. Overlapping zones: deterministic tie-break (closer center, then at_home) ------------
overlap_work = {"entity_id": "zone.work", "latitude": 41.9000, "longitude": 12.5000, "radius": 100.0}
p_ov = active_phone(True)
res_ov = u.update_proximity_state(p_ov, make_tag(41.9000, 12.5000, t0), HOME, overlap_work, now=t0)
check("identical Home & Work zones -> at_home wins", res_ov["state"], "at_home")

check.finish()
