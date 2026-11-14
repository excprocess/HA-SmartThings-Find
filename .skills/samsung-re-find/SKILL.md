---
name: samsung-re-find
description: Protocol rules, operation state machine, capability model, and privacy guardrails for Samsung Find / Find My Mobile devices.
version: 0.2.2
author: Charles Bel, Hermes Agent (adapted for HA-SmartThings-Find)
license: MIT
platforms: [linux, macos, windows]
---

# Samsung Find Protocol & Operation Skill

Reference skill adapted from `charlesbel/samsung-re-find` (`.skills/samsung-re-find/SKILL.md`, `docs/technical-findings.md`, and `src/samsung_find/api.py`) for `HA-SmartThings-Find`.

This is an unofficial reverse-engineered integration, not affiliated with Samsung or SmartThings; it deliberately exposes no lock, wipe, payment-lock, or arbitrary-request capability.

## Allowed Operations Allowlist

`perform_fmm_operation` in `utils.py` enforces a closed allowlist (`ALLOWED_FMM_OPERATIONS`):
- `CHECK_CONNECTION`
- `LOCATION`
- `RING` (only `status: "start"` or `"stop"`)
- `TRACK_LOCATION_START`
- `TRACK_LOCATION_STOP`

Destructive operations (`LOCK`, `WIPE`, lost mode, payment lock) are never exposed or dispatched.

## Direct Active Location & Session Flow

```text
POST /dm/addOperation.do
POST /dm/getOperationResult.do
POST /device/setLastSelect.do
```

- **Request filtering**: `getOperationResult.do` results must be filtered by **both** `oprnType == operation` and `str(op.get("reqId")) == str(req_id)`. Never substitute an old completed operation when the current `reqId` has not completed yet.
- **Operation state classification (`classify_fmm_operation_result`)**:
  - `success`:
    - `oprnStsCd == "2800"` and `oprnResultCode == "1200"`, or
    - `oprnStsCd` in `{"200", "SUCCESS", "00"}`, or
    - `oprnType == "TRACK_LOCATION_START"` and `oprnStsCd == "2100"`.
  - `pending`: `oprnStsCd` in `{"1000", "1100", "2100", "PENDING", "IN_PROGRESS", "RUNNING"}` (non-terminal; continue polling until deadline).
  - `failed`: any other non-empty `oprnStsCd`, or `oprnStsCd == "2800"` with `oprnResultCode != "1200"` (terminal; stop polling early and return the result code).
- **Freshness verification**:
  - A successful or accepted `LOCATION` operation does **not** prove a fresh fix was returned.
  - Always compare the timestamp (`gps_date` / `extra.gpsUtcDt`) against the newest previously known fix (`register_position_fix` in `utils.py`).
  - Only a strictly newer `gps_date` advances `DeviceLastNewFixSensor` (`_last_new_fix_at`) or reports success on the manual `UpdateLocationButton`.

## Connection Check (`CHECK_CONNECTION`)

- `CHECK_CONNECTION` is supported both as an active operation (`check_fmm_connection`) and as a passive entry inside `setLastSelect.do`'s `operation` list.
- When `oprnStsCd == "2800"` and `oprnResultCode == "1200"`, the device answered the check and is **online** (or `wifi` / `mobile` when a specific network field is present).
- When `oprnStsCd == "2800"` with an error `oprnResultCode`, or when a live `CHECK_CONNECTION` accepted by Samsung (`samsung_side == True`) fails/times out (excluding `501`), the device is **offline**.
- When the check could not be performed or parsed (e.g., local HTTP/session error), the state is **`unknown`** — never conflate a transport/parser limitation with the device being offline.
- Before sending `RING` on a phone or watch (`FmmRingSwitch`), run `check_fmm_connection(use_cache=False)`: abort with an offline error if `offline`, proceed if `online`/`wifi`/`mobile`/`unknown`.

## Passive vs. Active Device Rules

- **Active-connectable devices** (`is_active_connectable_device`: phones, tablets, PCs):
  - Have a `DeviceConnectionSensor`.
  - Participate in the opt-in `unknown_after_failed_attempts` policy when their per-device Active Location switch is `on` (counting only Samsung-side active outcomes that did not yield a newer fix).
- **Passive FMM devices & SmartTags** (Buds, watches, trackers) and any device with Active Location `off`:
  - Are routinely offline when disconnected from a phone.
  - Never mark their position as `unknown` after failed attempts; always preserve the last known good position.

## Battery & Earbud Left/Right Channels (PR #2)

- `extract_fmm_battery` in `utils.py` extracts:
  - General battery (`battery_level`) from `op['battery']`, `op['battery_percent']`, `op['extra']['battery']`, or `op['encLocation']['battery']` (supporting numeric strings `0..100` and symbolic `BATTERY_LEVELS`).
  - Earbud channels (`left_battery_level`, `right_battery_level`) from `op['extra']['left']['battery']` and `op['extra']['right']['battery']`.
- In `sensor.py`, Galaxy Buds (`is_buds_device`) expose `<name> Left Battery` and `<name> Right Battery` (`DeviceBudsBatterySensor`), while phones, tablets, watches, PCs, and SmartTags expose `<name> Battery` (`DeviceBatterySensor`).
- For active-connectable devices (`is_active_connectable_device`: phones, tablets, PCs) with Active Location `off`, the battery sensor reports `unknown` (`None`) because passive reads do not update their battery on Samsung's server; for watches, Buds, and SmartTags the last known battery is always retained.

## Privacy & Sanitized Structure Logging

- Never log or store raw coordinates, device serials/IDs, account IDs, SSIDs, or tokens in FMM request history or structure logs.
- Use `sanitize_structure` in `utils.py` to inspect undocumented Samsung payload shapes safely.
