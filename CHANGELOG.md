# Changelog

## 8.5.0

### Added
- **Proximity / Direction sensor (`<name> Direction`)**: new `ENUM` sensor with translated states (`arrived_home`, `arrived_work`, `towards_home`, `towards_work`, `away_from_home`, `away_from_work`, `away`, `stationary`, `unknown`) configured via native Home Assistant **Home** and **Work** zones and a selectable distance unit (`m` / `km`) in the integration options.
  - Created only for **SmartTags** (always active) and **active-connectable devices** (phones, tablets, PCs — active only while their `Active Location` switch is on, otherwise `unknown`); never created for passive FMM devices (Buds, watches).
  - Exposes `distance_home_<unit>`, `distance_work_<unit>`, `delta_home_<unit>`, `delta_work_<unit>`, `speed_kmh`, `bearing_deg`, `alignment_home`, `alignment_work`, `samples_used`, and `last_fix_at`.
  - Built-in anti-false-positive safeguards: Haversine + projection ratio with entry/keep hysteresis, dynamic accuracy-aware anti-jitter threshold, coarse-fix (`> 300 m`) and implausible-speed (`> 250 km/h`) filtering, time-gap window reset (`30 min`), temporal movement decay (`15 min`), collinear Home/Work tie-breaking, and `position_unknown` integration.
- **Bundled SmartThings Find Lovelace cards (One UI & Material Design 3 / Material You)**: auto-registered directly by the integration (`/smartthings_find_frontend/smartthings-find-cards.js`) with native `<ha-map>` integration and visual GUI editors:
  - `custom:smartthings-find-device-card`: single-device map widget with refresh and Ring action buttons, timestamp, zone, battery, and direction badges.
  - `custom:smartthings-find-carousel-card`: multi-device map widget with floating speech-bubble header and horizontally scrollable device selector bar (auto-discovers all SmartThings Find trackers).
  - Supports both **Samsung One UI** (`design: one_ui`) and **Material Design 3** (`design: md3`), automatically interfacing with **Material You Utilities** (`--md-sys-color-*`, `--md-sys-shape-*`, `--md-sys-motion-*`).
- **Local integration brand icons**: added `custom_components/smartthings_find/brand/{icon,icon@2x,logo,logo@2x}.png`.

### Changed
- **Mobile connection state labeling**: for phones and tablets, an online `CHECK_CONNECTION` without Wi-Fi `netType` is now reported as **`mobile`** instead of `online` (PCs continue to report `online`).
- **PC low-accuracy warning excluded**: Windows PCs routinely report ~200–500 m Wi-Fi geolocation accuracy; they no longer trigger the low-accuracy warning flag on `Location Retrieval Error`.
- **Galaxy Buds detection & `CHECK_CONNECTION` battery refresh**: broadened Buds detection (matching `subType` values like `CANAL`/`CANAL2`/`OPEN`, `HEARABLE`, and device name/model) so Buds reliably get their icon and separate `Left Battery` / `Right Battery` sensors; enabled `CHECK_CONNECTION` during opted-in active polls and manual `Update Location` for all FMM devices so Buds/Watch battery channels refresh.

### Fixed
- **Eliminated `device_tracker` `last_updated` / `last_changed` churn**: `SmartThingsDeviceTracker` now compares a snapshot of actual position/availability/mode and skips `async_write_ha_state()` when a poll returns the exact same fix, preventing passive or unchanged devices from advancing their tracker timestamps on every poll. Also removed duplicate/internal fields (`dev_id`, `name`, `update_success`, `location_found`, battery fields) from `device_tracker` attributes.
- **Diagnostics redaction**: added `user_auth_token`, `login_id`, and FMM web/request identifiers (`web_usr_id`, `usrId`, `web_dvce_id`, `dvceID`, `fmmDevId`, `reqId`) to `TO_REDACT` in `diagnostics.py`.
- **Power Saving binary sensor updates**: `DevicePowerSavingSensor` now inherits `CoordinatorEntity` so SmartTag power-saving metadata refreshes with coordinator updates instead of staying frozen at startup.
- **Reconfigure flow**: `async_step_reconfigure` now updates the existing config entry instead of creating a duplicate entry.
- **Tablet Ring switch**: `FmmRingSwitch` now uses `is_phone_device(...)` so Samsung tablets also receive the Ring switch.

## 8.3.0

### Added
- **Re-enabled battery sensors for Find My Mobile devices (`<name> Battery`)**: battery percentage is now extracted from Samsung's `CHECK_CONNECTION`, `RING`, `LOCATION`, `LASTLOC`, and `OFFLINE_LOC` operations for phones, tablets, watches, and PCs.
- **Differentiated Left / Right earbud battery sensors for Galaxy Buds (`<name> Left Battery` and `<name> Right Battery`)**: reads the independent earbud channels `extra.left.battery` and `extra.right.battery` from Samsung's FMM payload (based on `charlesbel/samsung-re-find` PR #2). Any legacy single battery entity for a Buds device is removed automatically on setup.
- **Persistent last-known battery for Galaxy Buds, watches, and SmartTags**: passive devices cannot be woken remotely when disconnected from a phone; their last known battery values (`battery_level`, `left_battery_level`, `right_battery_level`) remain fixed and are persisted across polls and Home Assistant restarts instead of dropping to `unknown` when a cycle omits battery data.
- **Samsung Account master-token fallback (`_reissue_token_from_master`)**: when a rotating `refresh_token` is rejected (e.g. after a disrupted save), the integration automatically attempts to re-issue tokens using the stored `user_auth_token` before triggering a Home Assistant reauthentication flow.

### Changed
- **Active-connectable device battery policy (smartphones, tablets, PCs)**: because Samsung's backend does not update battery during passive reads, the `<name> Battery` sensor for phones, tablets, and PCs reports `unknown` whenever the device's `Active Location` switch is off, and displays the battery level only when `Active Location` is enabled.
- **Live-verified `Connection` state parser**: updated `parse_connection_state` against real `S23 Ultra` and Windows PC responses to combine `CHECK_CONNECTION` (`extra.isConnected`, `oprnStsCd == "2800"` + `oprnResultCode == "1200"`) with `LOCATION`'s `extra.seData.netType` (`wifi` / `mobile`), and seed initial state from passive `setLastSelect.do` reads.

### Fixed
- **Eliminated startup HTTP 404 cascade on `setLastSelect.do`**: stored `devices` and `session` in `hass.data` before `async_config_entry_first_refresh()` and ensured `device/getDeviceList.do` is always called on every new `JSESSIONID` with form-encoded body `data={}` to initialize Samsung's server-side session device table.
- **Galaxy Watch Ring entity detection**: detected newer watches reported by Samsung as `deviceTypeCode: "WEARABLE"` with `subType: "WATCH"` (e.g. Galaxy Watch 8 Classic).
- **Home Assistant 2027.8 deprecation**: replaced `device_registry.async_get_device({identifier})` with `async_get_device_by_identifier` (with fallback for older HA versions).

## 8.2.0 (pre-release)

### Added
- **Last New Position Request** sensor (diagnostic, native timestamp) for every tracked device. It moves only when a read returns a fix strictly newer than any seen before; an unchanged fix, a `501` or a pending request never moves it, and neither does the first read after a restart that returns the restored position. The value survives restarts.
- **Connection** sensor (diagnostic) for phones, tablets and PCs: `wifi`, `mobile`, `online`, `offline` or `unknown`, from Samsung's `CHECK_CONNECTION` operation. It is refreshed in the opted-in Active Location cycle (never with the switch off) and before every Ring; `unknown` means the answer could not be read, not that the device is offline. The parser is a best-effort first version: Samsung does not document the payload.
- **Sanitized server structure.** The shape of what Samsung returns (every key with its value type, values only for short status-like fields; never coordinates, ids, names or session data) is logged at INFO when it changes and exposed in the Connection sensor attributes (`check_connection_structure`, `server_data_structure`), to see what the server provides and to refine the parser.
- **Exclude devices** option: selected devices get no polling, no Samsung requests and no entities (existing ones are removed on reload); they can be included again from the same option.
- **Ring for watches** (Samsung requires the watch to be online and connected).
- **Unknown position after N failed attempts** option (0 = off). For phones, tablets and PCs with Active Location on, N scheduled active attempts in a row that Samsung answered without bringing a genuinely newer fix make the tracker and location sensors show *unknown*. Local session/HTTP errors and manual presses are not counted; a genuine new fix restores the position immediately. The last position stays stored. Buds, watches, SmartTags and devices with the switch off always keep their last known position.

### Changed
- Ring on phones and watches first checks the connection and refuses with an *offline* error if Samsung reports the device offline. If the connection cannot be determined the ring is sent anyway. SmartTag ring is unchanged.

## 8.1.13

### Changed
- Added a dedicated Active Location refresh interval for Buds, PCs and watches (default 1800 seconds, minimum 30). It is one cadence both inside and outside zones while that device's Active Location switch is on.
- With Active Location off, these devices make passive reads only and continue to use the existing in-zone/out-of-zone intervals. Phones keep using those two intervals whether active requests are enabled or not.
- Removed the unconditional startup Active Location attempt for Buds. A restored-on device switch triggers its active request after setup; a switch left off causes only passive reads.

## 8.1.12

### Changed
- Active Location now runs on every scheduled poll for any Find My Mobile device whose per-device switch is enabled, including Buds and watches. A prior Samsung `501` response or a transient fetch failure no longer suppresses later active attempts; each cycle still performs the passive read afterward.
- Device trackers now write Home Assistant state only when their own per-device coordinator data is replaced. Polling another device no longer advances their `last_updated` timestamp.
- Home Assistant's native `last_changed` remains tied to a device tracker state transition (for example, entering or leaving a zone); location/attribute refreshes update `last_updated` instead.

## 8.1.11

### Fixed
- Location, battery and map-link sensors now subscribe through Home Assistant's coordinator entity lifecycle, so coordinator and manual-button updates refresh their states reliably.
- Older passive responses remain in diagnostics but can no longer overwrite a newer known-good position.
- Buds receive at most one automatic active LOCATION attempt at Home Assistant startup; later coordinator polls stay passive even if active location is enabled for that device.
- A manual Update Location press can retry active LOCATION even after an earlier `501` marked the device unsupported; a successful retry clears that temporary runtime flag.
- Location Retrieval Error is now a text diagnostic sensor showing `OK`, the Samsung result code (for example `501`), an HTTP status (for example `HTTP 404`), or the readable error. The redundant Location Retrieval Status entity has been removed, and the old binary entity is removed from the registry during setup.
- Update Location now explains that Samsung supplied no position newer than the last known fix instead of incorrectly labeling every unchanged result as a rejected or timed-out active request.

### Notes
- The log shows passive reads succeeding at the HTTP level while Buds and Watch often return an unchanged `LOCATION` timestamp. Samsung's reverse-engineered client documents that passive reads return the last available server location; requesting a new fix is a separate operation and support varies by device. These logs do not establish a supported way to force a fresh Buds/Watch fix from the integration.

## 8.1.10

### Fixed
- Last valid coordinates are now persisted across Home Assistant reloads and restarts. If Samsung rejects a new read, the previous position stays visible while the retrieval error is reported.

### Added
- The Location Retrieval Error entity exposes the latest API endpoint, HTTP status, Samsung result code, and a concise `error_code` attribute alongside its `ok` / `problem` state.

## 8.1.9

### Changed
- Device trackers expose `fmm_api_calls_last_update` and `fmm_api_calls_total`. They count actual FMM HTTP request attempts per device, including failed attempts; cache reuse, skipped operations, and response parsing are not counted. The total is for the current Home Assistant run.
- The **Update Location** button reports success only when Samsung returns a position newer than the one already shown.
- On reload, a tracker restores its last known Home Assistant coordinates if the first Samsung poll fails, keeping the device visible while retrying.

## 8.1.8

### Changed
- Full sanitized FMM request diagnostics are written to the Home Assistant log; the Location Retrieval Error entity keeps only the latest interaction and the consecutive fetch-failure counter.
- `is_buds` remains available internally for Buds-specific behavior but is no longer exposed as a device-tracker attribute.
- `last_seen_local` and `next_update_local` on the tracker and location sensor now use ISO 8601 timestamps in the Home Assistant timezone. Added `last_location_retrieved_at` to distinguish when Home Assistant last read coordinates from Samsung's GPS fix time and compare it with the entity's native `last_updated`.

## 8.1.7

### Changed
- Each Galaxy Buds device gets one active `LOCATION` attempt during Home Assistant startup, then scheduled reads stay passive unless Active Location is enabled. The attempt and outcome are recorded in request history.
- If the active attempt completes without readable coordinates, **Location Retrieval Error** now explains that Samsung returned no coordinates; the last known position remains available and the response diagnostics are retained for investigation.

## 8.1.6

### Changed
- If a passive Find My Mobile read has no readable position or only a stale position from a cached website session, the integration makes one passive read with a newly bootstrapped session per device and Home Assistant runtime. This does not send an active `LOCATION` request; the manual **Update Location** button still forces a fresh read.
- Find My Mobile request history and debug logs now summarize returned operation types, result codes, coordinate presence, encryption shape, timestamp fields, parser skips, and the selected operation without recording coordinates or credentials.
- Active-location requests skipped because the switch is off, a failure streak is active, or Samsung previously marked the device unsupported are now identified in request history.
- SmartThings location responses with HTTP/result code 429 now use exponential retry backoff, starting at five minutes and capped at one hour.

## 8.1.5

### Fixed
- After an active Find My Mobile `LOCATION` request, the integration now bootstraps a fresh website session before reading the location, matching the documented Find flow. The read uses the device ID re-resolved for that session.
- Request history records whether the post-operation read used a fresh session and whether the website device ID changed.

## 8.1.4

### Changed
- Find My Mobile active `LOCATION` requests now poll for up to 60 seconds before timing out; other operations keep their existing timeout.
- `next_update_local` now uses an ISO 8601 timestamp with the Home Assistant local UTC offset, so templates can parse it as a date/time.
- Removed `dev_name` and `icon_url` from device-tracker attributes; the icon URL remains used internally as the entity picture.
- Excluded the device-provided `friendly_name` duplicate. Home Assistant still adds its standard `friendly_name` state attribute automatically.

## 8.1.3

### Fixed
- Removed redundant FMM tracker fields and private Samsung/account identifiers, including the raw monotonic poll counter; readable `next_update_local` remains available.
- Toggling Active Location now triggers an immediate refresh for any FMM device instead of waiting for its next scheduled poll.
- Location Retrieval Error now refreshes with coordinator updates and reports low-confidence positions over 100 m without discarding those positions.

## 8.1.2

### Added
- The **Location Retrieval Error** entity now includes a bounded history of Find My Mobile request
  summaries, including HTTP status, Samsung result codes, operation status, and session-bootstrap
  outcomes. Raw response bodies, coordinates, authorization codes, cookies, and CSRF values are not
  stored in these attributes.

## 8.1.1

### Changed
- Removed duplicated and API-internal attributes from device trackers. Coordinates, accuracy,
  last-seen time, and the Maps URL remain available through the dedicated Location and Maps Link
  sensors; the tracker continues to provide its GPS position and accuracy to Home Assistant for
  zone detection.
- Reworked the Find My Mobile **Location Retrieval Error** entity to report request failures,
  unreadable or encrypted location responses, and failed active-location requests. A position being
  old by itself no longer turns this error entity on; its age remains visible on the Location sensor.
- Added the `last_seen_local` attribute to the device tracker and Location sensor, formatting the
  position timestamp in Home Assistant's configured time zone; the Location sensor's original
  `last_seen` timestamp is preserved.
- Added `next_update_local` and `next_update_mode` attributes to show the scheduled next poll time
  in Home Assistant's time zone and whether that cycle will use active or passive location.

### Fixed
- Galaxy Buds now use Samsung Find's pair icon when Samsung leaves `icon_url` empty.
- Passive location parsing now falls back to `encLocation` when the top-level coordinates are empty
  or malformed, which can otherwise discard a readable position supplied by Samsung's nearby-device
  network.
- Location errors remain visible on the diagnostic entity while the integration briefly preserves a
  last-known position after a transient request failure.

## 8.1.0

### Added
- **Different polling interval for devices inside a Home Assistant zone.** A device that is in a zone
  (Home, a workplace, ...) is polled on its own, usually much longer, interval, while every other device
  keeps the general one. New option **Update interval inside a zone**; `0` (the default) means no separate
  interval, so nothing changes unless you set it. Each device is scheduled on its own, so one at home and
  one away don't share a clock: devices that aren't due yet are not fetched at all, and Active Location
  requests follow the same schedule (a phone sitting at home is no longer woken every few minutes). A
  failed poll is retried on the next tick instead of waiting out a long in-zone interval, and the *Update
  Location* button restarts that device's schedule.
- `in_zone` and `polling_interval` attributes on the location sensor, to see which schedule a device is on.
- A `tests/` folder (run on every push by a new workflow): the parser, the per-device scheduling and the
  Find My Mobile read are tested against canned Samsung answers, with Home Assistant stubbed out.

### Fixed
- **A passive device (watch, PC, earbuds) could turn Unavailable, and stop showing new positions, when
  Samsung reported a new position for it.** The code that reads Samsung's list of positions assumed every
  entry is complete; one that wasn't (typically a position reported by nearby devices on the Find
  network, which can come without a vertical accuracy, with empty coordinates or with an unusual date)
  raised an error, the whole read was then counted as failed, and after three polls the device went
  Unavailable. Such an entry is now skipped, never marks the read as failed, and never replaces a valid
  position with an empty one; an entry whose only problem is a missing timestamp now uses the time Samsung
  recorded it instead of being dropped. What Samsung sends is only logged, once per device and shape, in
  a form that contains no coordinates (look for `Skipped a ... entry from Samsung`).
- When an answer holds no usable position at all, the last known one keeps being shown (and flagged by
  *Stale Position*) instead of the entity going blank.
- *Stale Position* judged "old" against the default interval rather than the one you configured; it now
  uses three polls of that device's own interval (never less than 30 minutes).
- The accuracy of a position no longer fails when only one of the two uncertainties is reported; this also
  applied to SmartTags.

### Notes
- If a device's positions never update even though the Samsung app shows new ones, Samsung may be
  returning them end-to-end encrypted, which can't be read here (the same limit as
  [samsung-re-find](https://github.com/charlesbel/samsung-re-find)). The log now says so, once, and the
  device stays available with its last known position.

## 8.0.0

### Added
- Location support for **Find My Mobile devices** (phones, tablets, watches, earbuds, PCs) via
  the SmartThings Find website API, bridged automatically from the account's master token
  (no cookie to paste, no manual renewal).
- Per-device **Active Location** switch (config entity) and **Update Location** button for those devices.
- **Ring** switch for phones (the website exposes no ring control for the other Find My Mobile types).
- Device icons for every device, sourced from Samsung.
- `telephony_support`, `wifi_only`, `cdma`, `ring_supported`, `offline_find_supported` attributes on
  the location sensor of Find My Mobile devices.
- A device that answers "not supported" to Active Location is not retried automatically until the next
  restart, or until its switch is turned off and back on.
- **Stale Position** binary sensor (Find My Mobile devices): turns on when a device's shown position
  either can't be actively refreshed at all (buds, most watches, a PC - no network connection of
  their own) or hasn't updated in over 3 poll cycles, so an old position is never mistaken for a
  current one. Uses the "problem" device class, so it shows as a warning badge with no dashboard
  setup needed. The location sensor also carries `active_location_supported` and `position_stale`
  as plain attributes, for anyone who wants to build their own dashboard logic instead.

### Changed
- The two global *active mode* checkboxes in the options are gone. Active Location is now a
  per-device switch instead.
- A failed poll no longer marks a device Unavailable immediately: the last known position is kept
  for up to 3 cycles before falling back to Unavailable, so a one-off network hiccup doesn't show up
  as a flicker.
- The battery sensor is no longer created for Find My Mobile devices: Samsung only updates it when the
  device itself is actively answering, so it was misleading more often than not.
- `TrackerEntity`/`SourceType` are imported from their non-deprecated path.

### Fixed
- Saving the integration's options (or a token silently refreshing in the background) no longer
  reloads the whole integration - only an actual options change does.
- `Ring`/`Update Location` controls actually register as entities: `button` was missing from the
  integration's platform list, so none of its entities ever loaded.
- Entity `entity_picture` no longer overrides the action icon on buttons/switches - the device photo
  was hiding the ring/location icon.

### Notes
- Config entries created before 8.0.0 do not have the master token this version needs for Find My
  Mobile devices. SmartTags are unaffected; a log line explains the re-login needed for the rest.
  See [Notes on authentication](README.md#notes-on-authentication).
