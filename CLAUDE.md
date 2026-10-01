# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

This repo is a fork of `Rain92/HA-SmartThings-Find`, itself the continuation of a chain:
`Vedeneb/HA-SmartThings-Find` (the original, archived by its author) → `herisanuadrian/HA-SmartThings-Find`
→ `Rain92/HA-SmartThings-Find` (merged in the OAuth2/PKCE login rework from
`PixelShober/HA-SmartThings-Find`, replacing the old JSESSIONID web-login scraping after Samsung
redesigned their account login page and broke it) → here. Version 8.0.0 of this fork added Find My
Mobile device support (phones/tablets/watches/earbuds/PCs) via a second, separate bridge to
`smartthingsfind.samsung.com` documented under "Find My Mobile bridge" below - see `CHANGELOG.md` for
the full list. There is no CI beyond HACS structural validation — no test suite, linter, or build step
exists in this repo.

## What this is

A Home Assistant custom integration (`custom_components/smartthings_find`) that adds Samsung
SmartThings Find support (SmartTags, phones, tablets, watches, earbuds) to Home Assistant via
`device_tracker`, `sensor` (battery + coordinates), and `switch` (optimistic ring toggle) entities. It works by reverse-engineered calls to Samsung's undocumented SmartThings Find / SmartThings
Cloud APIs — there is no official public API, so behavior is fragile and can break whenever Samsung
changes their backend.

## Commands

There is no build, lint, or test tooling in this repo. The only automated check is
`.github/workflows/validate.yaml`, which runs the `hacs/action` structural validator against the
`custom_components/smartthings_find` integration on push/PR. Development is verified manually by
installing the integration into a real Home Assistant instance (via HACS custom repo or by copying
`custom_components/smartthings_find` into HA's `config/custom_components/`) and exercising the config
flow / entities live. Enable debug logs via HA's `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.smartthings_find: debug
```

## Architecture

All logic lives in `custom_components/smartthings_find/`:

- **`utils.py`** — the core of the integration and the first place to look when API behavior breaks.
  Three parts:
  - **Login** (`do_login_stage_one` / `do_login_stage_two`): a PKCE-based OAuth2 flow that mimics
    Samsung's native mobile apps rather than the web login page. Stage one fetches Samsung's
    entry-point config, builds a `svcParam` JSON payload (client id, device identity, PKCE
    `code_challenge`, etc.), RSA-encrypts it with the server-provided public key
    (`cryptography.hazmat`), and returns a `signInURI` for the user to open in a browser. That login
    completes with a redirect to an `ms-app://...` URI (never actually opened) which the user must
    copy out of devtools and paste back in — stage two parses `code`/`auth_server_url`/`state` out of
    that URL (decrypting them if wrapped), then exchanges the code at
    `{auth_server_url}/auth/oauth2/authenticate` → `/v2/authorize` → `/token` for both a SmartThings
    Find token and a separate OneConnect/IoT token (`CLIENT_ID_FIND` vs `CLIENT_ID_ONECONNECT`). It
    also captures `user_auth_token`/`login_id`, the account's master token, needed by the Find My
    Mobile bridge below. This flow was adopted from `PixelShober/HA-SmartThings-Find` after Samsung's
    account web login (the old JSESSIONID/QR-code scraping approach) broke.
  - **SmartTag calls** (`get_devices`, `get_device_location`, `ring_device`, `stop_ring_device`,
    etc.): use `authenticated_request`, which attaches `X-Sec-Sa-Authtoken` from the stored access
    token and, on a 401/403, transparently calls `refresh_find_token`/`refresh_iot_token` (via
    `_refresh_token`) and retries once before raising `ConfigEntryAuthFailed` (which triggers HA's
    reauth flow). Tags are discovered through the SmartThings *installed app* API
    (`_get_installed_app_id`, `_execute_installed_app`, `_build_installed_apps_request`), and their
    location comes from `/trackers/geolocation`. This OAuth "chaser" API has no visibility into
    non-tracker (Find My Mobile) devices at all — it structurally rejects them (`resultCode=01` on
    tracker geolocation for a non-tracker id), confirmed by direct testing, not assumed.
  - **Find My Mobile bridge** (`get_web_session_cookie`, `_get_web_client`,
    `enrich_devices_with_web_metadata`, `get_fmm_device_location`, `perform_fmm_operation`,
    `ring_fmm_device`/`stop_ring_fmm_device`): phones/tablets/watches/earbuds/PCs are a completely
    separate Samsung system (`locationType: 'FMM'` in the `/devices` listing), reachable only through
    the `smartthingsfind.samsung.com` *website* API, not the OAuth one. `get_web_session_cookie`
    exchanges the master `user_auth_token` for a website-client `code` (`WEB_FIND_CLIENT_ID`,
    `WEB_FIND_SCOPE`) via `/auth/oauth2/v2/authorize`, then feeds that into the website's own
    `getState.do` → `login.do` → `chkLogin.do` bootstrap to mint a `JSESSIONID` + CSRF — the same
    bridge documented by the MIT-licensed
    [samsung-re-find](https://github.com/charlesbel/samsung-re-find) project, ported to aiohttp. Two
    non-obvious things this bridge depends on, found by extended live testing rather than
    documentation (there is none):
    - `_get_web_client` gives these calls their own aiohttp session, isolated from the one HA shares
      for everything else. This turned out not to be what fixed the failure mode below, but is kept
      since a long-lived connection reused for days across unrelated traffic is a reasonable thing to
      avoid regardless.
    - **Website device ids (`dvceID`) are scoped to the session that resolved them, not global.**
      `enrich_devices_with_web_metadata` (which calls `device/getDeviceList.do` to map each device to
      its `dvceID`/`web_usr_id` and icon) was originally called once, at `get_devices()` time. That
      device id then stopped working the moment the cached web session got replaced by a fresh one
      (the observed symptom: every bootstrap step, including `chkLogin.do`, kept succeeding, but the
      very next real call - `setLastSelect.do`/`addOperation.do` - 404'd with an empty body, every
      time, forever, until a full HA reload re-ran `get_devices()`). `samsung-re-find` never caches a
      `dvceID` across sessions — every one of its operations re-bootstraps and re-resolves the device
      list within that same fresh session before using it. `get_web_session_cookie` now does the same:
      every time it performs an actual new bootstrap (not a cache hit), it immediately re-runs
      `enrich_devices_with_web_metadata` under that new session before returning. If FMM devices start
      404ing again, check this invariant first before reaching for a new hypothesis.

    `get_fmm_device_location` does a passive read (`device/setLastSelect.do`, parsed by the existing
    `extract_best_location` - the same "operations" list shape the pre-OAuth fork used to parse from
    a completely different, now-dead endpoint), and — only for FMM devices, since SmartTags reject it
    with `resultCode=01` — can additionally trigger an active `LOCATION` push
    (`dm/addOperation.do` + poll `dm/getOperationResult.do`) beforehand via `perform_fmm_operation`,
    gated by `dev_data['active_location_requested']` (set by the per-device `ActiveLocationSwitch` in
    `switch.py`) or by `force_active=True` (the `UpdateLocationButton` in `button.py`, which also
    writes only that one device's result into the coordinator via `async_set_updated_data` instead of
    waiting for a full multi-device refresh). `ring_fmm_device`/`stop_ring_fmm_device` use the same
    `perform_fmm_operation` machinery with `operation="RING"` and `status: start|stop` — but the
    website only shows a ring control for `deviceTypeCode` `PHONE`/`PHONE DEVICE`
    (`dev_data['web_device_type_code']`, also set by `enrich_devices_with_web_metadata`), so that's
    the only type `switch.py` creates `FmmRingSwitch` for.

    A device that gets an explicit "not supported" answer (`resultCode=501` - seen consistently for
    earbuds and a non-LTE watch, which have no network connection of their own to push a fix to) sets
    `dev_data['_active_location_unsupported']`, so the integration stops spending ~7 requests a cycle
    polling for something that structurally can't work, until the next restart or until the switch is
    turned off and back on.

    The coordinator (`__init__.py`) bridges over up to `MAX_STALE_FALLBACK_CYCLES` (3) consecutive
    fetch failures per device with its last known good position instead of going straight to
    Unavailable — `dev_data['_consecutive_fetch_failures']` tracks the streak, reset on any success.
    Past that, it does go Unavailable rather than mask a real problem forever.

- **`config_flow.py`** — drives the OAuth2 flow as a two-step HA config flow: `async_step_user` calls
  stage one and shows the generated `signInURI`; `async_step_auth_code` collects the pasted-back
  `ms-app://...` redirect URL from the user and calls stage two, storing the resulting access/refresh
  token pairs (`CONF_ACCESS_TOKEN`, `CONF_REFRESH_TOKEN`, `CONF_IOT_ACCESS_TOKEN`,
  `CONF_IOT_REFRESH_TOKEN`), `CONF_USER_ID`, `CONF_AUTH_SERVER_URL`, and `CONF_DEVICE_ID` on the
  config entry. Also implements reauth (`async_step_reauth`) and the options flow
  (`SmartThingsFindOptionsFlowHandler`) for update interval and active/passive mode per device class.
  In-progress PKCE state (`state`, `code_verifier`, `device_id`) is stashed in
  `hass.data[DOMAIN]["auth_data"]` between stage one and stage two, not on the flow instance.

- **`__init__.py`** — `async_setup_entry` loads the stored tokens, loads the device list, and creates
  `SmartThingsFindCoordinator` (a `DataUpdateCoordinator`) that polls `get_device_location` for every
  device on `update_interval` seconds. Devices disabled in the HA device registry are skipped when
  building the device list.

- **`device_tracker.py`**, **`sensor.py`**, **`switch.py`**, **`button.py`** — entity platforms
  reading from the shared coordinator's data. `sensor.py` holds `DeviceBatterySensor` (trackers
  only — see below), `DeviceLocationSensor` and `DeviceMapsLinkSensor` (both: any device with a
  location, tracker or FMM — `used_loc`/`location_found` have the same shape either way). The
  latter two exist because a `device_tracker` state can only ever be a zone
  (`home`/`not_home`/zone name), so raw coordinates and a `google_maps_url` attribute (built by
  `google_maps_url()` in `utils.py`) are surfaced on a sensor instead; `DeviceMapsLinkSensor`
  exposes the same URL as a standalone `EntityCategory.DIAGNOSTIC` entity. The coordinator also
  calls `update_device_maps_link()` after each poll, which rewrites the device registry entry's
  `configuration_url` (the device page's "Visit" link) to the current Maps URL — skipped when the
  URL is unchanged, so the registry isn't re-saved every poll. `switch.py` has `RingSwitch` (trackers)
  and `FmmRingSwitch` (phones only) — both optimistic (auto-turn off after `RING_TIMEOUT_SECONDS`)
  since neither API exposes actual ring status — plus `ActiveLocationSwitch` (FMM devices, a config
  entity, `RestoreEntity`-backed so its state survives restarts). `button.py` has
  `UpdateLocationButton` (FMM devices only — see the Find My Mobile bridge notes above for why
  SmartTags can't use it). Entities set `_attr_icon` but deliberately do **not** set
  `_attr_entity_picture` on any of these action entities — it takes priority over the icon in the
  frontend, so setting it would silently replace the action icon (ring/crosshairs) with the
  device's photo. `device_tracker.py` is the one place `entity_picture` is intentional.

- **`diagnostics.py`** — `async_get_config_entry_diagnostics` dumps the config entry, the built
  device list (including each tag's untouched `raw_device` payload) and the coordinator data, with
  tokens, account identifiers and coordinates redacted via `TO_REDACT`. This is the fastest way to
  see what a given account's API actually returns. It issues no requests of its own — it only dumps
  state the integration already holds.

- **`binary_sensor.py`** — `DevicePowerSavingSensor`, the tag's power saving mode
  ("Energiesparmodus"), read from `bleD2D.metadata.activeMode.mode` via
  `get_device_ble_metadata()` / `get_power_saving_state()` in `utils.py`. It is a binary_sensor
  and not a switch because only the read path is confirmed. The comment above
  `get_device_ble_metadata()` records what was ruled out — the chaser per-tag endpoints are all
  403/405, and a re-signed app cannot be used to capture the write because Samsung rejects it with
  `AUT_1708`. Do not re-run that investigation. The coordinator fetches the metadata blob per
  tracker on each poll and stores it under `ble_metadata`.

  `DeviceStalePositionSensor` (FMM devices only) is unrelated to the tag sensor above - it's a
  `device_class: problem` badge, `is_on` when `position_stale` or `not active_location_supported`
  on that device's coordinator data (both set in `get_fmm_device_location`, `utils.py`). It reads
  existing coordinator data, no API call of its own.

- **`const.py`** — all config keys, Samsung client IDs/scopes (`CLIENT_ID_FIND`, `CLIENT_ID_AUTH`,
  `CLIENT_ID_ONECONNECT`, `SCOPE_FIND`, `SCOPE_AUTH`), defaults (e.g.
  `CONF_UPDATE_INTERVAL_DEFAULT = 120`, `RING_TIMEOUT_SECONDS = 120`), and the `BATTERY_LEVELS`
  mapping (STF reports coarse battery buckets like `FULL`/`MEDIUM`/`LOW`/`VERY_LOW` rather than a
  percentage; `sensor.py` maps these to numeric values).

## Key behavioral notes

- **Active vs. passive mode** is a per-device concern now, not a global option: `ActiveLocationSwitch`
  (FMM devices only - SmartTags reject the active `LOCATION` push outright, `resultCode=01`) and the
  automatic skip while a device is mid failure-streak or flagged `_active_location_unsupported` (see
  the Find My Mobile bridge notes above). There is no longer a global "active mode" checkbox in the
  options flow - it was two booleans (`CONF_ACTIVE_MODE_SMARTTAGS`/`_OTHERS`) that applied to every
  device of a class at once; removed in 8.0.0 in favor of the per-device switch.
- A poll failure doesn't immediately go Unavailable - see `MAX_STALE_FALLBACK_CYCLES` in
  `__init__.py`, above.
- Ringing a device depends on a nearby Galaxy phone/tablet relaying via Bluetooth (SmartTags) or the
  device itself being reachable (phones) - if it doesn't work on the official SmartThings Find
  website, it cannot work through this integration either.
- Login requires a manual step the user can't fully automate: after signing in, Samsung redirects to
  an `ms-app://...` URI that the browser can't open; the user has to grab that exact URL from devtools
  (Network or Console tab) and paste it into the HA config flow. This login also captures the master
  `user_auth_token`/`login_id` the Find My Mobile bridge needs - a config entry from before 8.0.0
  doesn't have them, and needs a reauth/reconfigure (not just an upgrade) before FMM devices work.
- Renames made in the Samsung app propagate on setup: `get_devices` renames the device registry
  entry and `_sync_entity_names` renames each entity's `original_name` using a per-entity suffix
  table. Both bail out if the user named the device/entity themselves (`name_by_user` / `entry.name`).
  Because `get_devices` only runs at setup, a rename shows up after a restart or reload, not on the
  next poll.
- Access/refresh tokens are refreshed automatically on 401/403 (see `authenticated_request`); a
  refresh failure raises `ConfigEntryAuthFailed`, surfacing HA's reauth flow rather than a silent
  failure. `_refresh_token` persists the new tokens via `hass.config_entries.async_update_entry(entry,
  data=...)`, which also fires the same `add_update_listener` callback used for options changes
  (`_async_update_listener` in `__init__.py`) - that listener compares against a stored
  `_last_options` snapshot and only reloads the integration when `entry.options` itself changed, not
  on every token refresh. If a full reload starts happening again on every token refresh, this is the
  first thing to check.
