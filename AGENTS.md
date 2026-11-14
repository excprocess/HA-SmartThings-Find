# Agent handoff: HA-SmartThings-Find

Updated 2026-11-14. Read this before changing the integration; it captures the user's stability requirements and the operational lessons from recent releases.

## Repository and checkout map

- GitHub repository: `excprocess/HA-SmartThings-Find`; default branch: `main`.
- Active project root opened in VS Code: `C:\Users\coraz\Code\py\HA-SmartThings-Find\HA-SmartThings-Find`.
- Current branch is `main`, at `291f34b` (`fix: Update test to ensure parser errors do not affect device availability`), tag `v8.1.13`; `origin/main` points to the same commit. Manifest version is **8.1.13**.
- At the time this handoff was updated, the only local change was the untracked `AGENTS.md`; preserve other user changes if they appear. The obsolete `CLAUDE.md` guidance has been compared and its non-conflicting operational notes are incorporated below; it is being removed at the user's request.
- The sibling checkout under `Documents\Codex\...\work` is a separate, stale task workspace. Do not treat its files or branch state as the active project.
- The handoff is local until it is committed and pushed. Do not claim it exists on GitHub before that happens.

## Product architecture

- `custom_components/smartthings_find` is a Home Assistant custom integration.
- SmartTags use the SmartThings OAuth / installed-app API. Find My Mobile devices (phones/tablets, PCs, watches, Buds) use a separate, reverse-engineered SmartThings Find website API and session (`JSESSIONID`/CSRF). Do not assume fixes for one API family apply to the other.
- Samsung's FMM API is undocumented and changes server behavior. A successful HTTP request can still return an old location, `501`, pending operations, no coordinates, or a timeout. Clearly distinguish observations from hypotheses and do not label an unchanged fix as a transport failure without evidence.

## Implementation map and retained project notes

- **`utils.py`** contains most Samsung-facing logic: OAuth/PKCE login and token refresh, SmartTag installed-app calls, Find My Mobile website session setup, FMM operations, location parsing, maps links, and BLE metadata. For FMM requests, website `dvceID` values are session-scoped: after bootstrapping a new website session, resolve the device list and IDs again in that same session before using them.
- The auth flow imitates Samsung's native app: PKCE state/code verifier and a device identity are created before the user login; token exchange yields separate SmartThings Find and OneConnect tokens plus the master `user_auth_token`/login identity used by the FMM website bridge. SmartTag/OAuth calls and FMM website calls are separate services; FMM-only devices are not locatable through the SmartTag tracker endpoint.
- FMM website bootstrap exchanges the master token for the website client authorization, then follows Samsung's website session flow (`getState.do` → `login.do` → `chkLogin.do`) to obtain session cookies/CSRF. Passive reads use `setLastSelect.do`; active requests use `addOperation.do` followed by polling `getOperationResult.do`. After a fresh bootstrap, call `getDeviceList.do`/metadata enrichment again within that session before using its `dvceID`.
- SmartTag requests attach the Find access token. A 401/403 should trigger one token refresh/retry; a refresh failure should raise `ConfigEntryAuthFailed` so Home Assistant starts reauth.
- **`config_flow.py`** implements the two-stage Samsung login and options flow. The login starts with PKCE, then asks the user to paste the redirected `ms-app://...` URL obtained from the browser's developer tools. Older config entries predating FMM support may need reauthentication to acquire the master token fields required by the FMM bridge. Never include pasted auth URLs or tokens in logs or diagnostics.
- **`__init__.py`** creates the coordinator and schedules work per device. Keep the coordinator tick and each device's due time separate: a not-due device should retain its own data and should not trigger an API read. `_next_poll` is monotonic/internal and must not be exposed as a human timestamp. A failed read should be retried promptly rather than waiting through a long in-zone interval. Apply the 8.1.13 active cadence contract above when choosing the next schedule.
- **`device_tracker.py`**, **`sensor.py`**, **`switch.py`**, **`button.py`**, and **`binary_sensor.py`** expose coordinator data as entities. Device trackers represent HA zones; dedicated location sensors carry coordinates and Maps Link entities carry the URL. A device's poll should update only that device's entities. Action entities should retain their action icons; assigning the device photo as `entity_picture` can hide those icons.
- **`diagnostics.py`** should redact access/refresh tokens, account identifiers, and coordinates. Recheck redaction whenever new fields are added. Diagnostics should inspect already-held data and must not make extra Samsung requests.
- Device/entity names imported from Samsung are synchronized during setup only when the user has not customized them; a Samsung rename may therefore need an integration reload to appear.
- SmartTag Ring and FMM Ring are optimistic controls because the APIs do not report actual ring state. The documented FMM Ring support is phones only. The power-saving mode exposed for SmartTags is read-only; prior investigation found no confirmed write route, so do not present it as a controllable switch.
- `sensor.py` includes battery reporting for SmartTags; FMM battery readings are omitted because Samsung only reports them when the device answers and the value is often stale. The Maps Link also updates the device registry's configuration URL only when the URL changes.
- Entity photo is intentional on the device tracker, but action switches/buttons should use their own icons rather than the device photo. This avoids the photo masking Ring/Update Location controls in Home Assistant.

### CI and local validation

- GitHub Actions runs `.github/workflows/validate.yaml` (HACS structure validation) and `.github/workflows/tests.yaml` (the lightweight `tests/test_*.py` suite). There is no build step or general linter.
- The tests use Home Assistant stubs and canned Samsung responses. They cover parsing, scheduling, and FMM reads; they do not prove live Home Assistant behavior or Samsung endpoint behavior. When a server payload shape changes, add a sanitized fixture/case.
- The historical test environment needs `aiohttp`, `cryptography`, and `pytz`. A missing `pytz` is an environment/dependency failure, not evidence of an integration bug. CI previously exposed incomplete nested `homeassistant.util` stubs and syntax/indentation errors; inspect the traceback and parse/import the relevant modules before attributing failures.
- The documented Linux test invocation is `for t in tests/test_*.py; do python3 "$t" || exit 1; done` after installing `aiohttp`, `cryptography`, and `pytz`.
- To enable integration debug logs in Home Assistant:

  ```yaml
  logger:
    default: info
    logs:
      custom_components.smartthings_find: debug
  ```

- A static/syntax check is not live validation. State exactly which checks were run; run the suite when the user asks for verification or when it is necessary to validate an implementation change.

### Authentication and reload behavior

- OAuth access/refresh tokens refresh on 401/403. A failed token refresh should surface `ConfigEntryAuthFailed` and Home Assistant's reauthentication flow, not be hidden as a location failure.
- Updating tokens in config-entry data can invoke the integration update listener. The listener must compare options against its prior snapshot and reload only for actual options changes; a routine token refresh must not reload the integration.
- Entity names and device-registry names are user-customizable. Respect user-provided names and do not overwrite them during Samsung metadata sync.

## User decisions and stability contracts

### Polling and Active Location

- Active Location is per-device. With the switch off, do not send an active request; perform passive reads only.
- Phones use the configured in-zone/out-of-zone polling intervals. Buds, PCs, and watches have a separate configurable active cadence when their Active Location switch is on; the same dedicated cadence applies in and out of zones. With the switch off, they use passive reads on the ordinary zone/out-of-zone schedule.
- Keep active requests serialized and reasonably spaced; the user explicitly rejected aggressive repeated requests. The 8.1.13 contract supersedes earlier proposals/behaviors that made unconditional Buds active requests at startup: a switch left off must not wake the device. A restored-on switch can initiate an active request after setup.
- Manual **Update Location** acts on one device and should report success only when Samsung returns a genuinely newer readable position, not merely when the request was accepted.
- A `501` or transient failure must not permanently disable future active attempts when the switch remains on; evaluate each scheduled cycle as specified in 8.1.12. Manual retry should also be possible after a prior `501`.
- Older code/documentation said to suppress active requests during a failure streak or indefinitely after a `501` until restart/toggle. That behavior is superseded: while a device's Active Location switch is on, scheduled cycles must continue to follow the 8.1.12/8.1.13 contract. Do not restore the old suppression rule.
- Earlier notes described an unconditional Buds active attempt at startup. That was superseded in 8.1.13: do not send it when the switch is off.

### Preserve state and isolate device updates

- Never make a device unavailable or erase a usable position solely because the latest response has no new fix, contains a malformed optional field, or triggers a parser exception. Preserve the newest known-good fix, keep diagnostics visible, and keep retrying according to that device's schedule.
- Persist/restore last known coordinates across reloads/restarts. A passive response with an older timestamp must not overwrite a newer saved position.
- Parse entries independently. Skip malformed/unusable Samsung entries without failing the whole poll; nearby-device sightings can have partial accuracy, blank coordinates, unusual timestamps, or encrypted fields.
- A poll for one device must not update every other device tracker's state timestamps. Tracker entities should write only when their own coordinator data changes. Home Assistant's native `last_changed` tracks state changes such as zone transitions; position refreshes belong in an explicit position-time attribute and update `last_updated`.
- Do not confuse “Samsung read succeeded but there is no newer fix” with HTTP/auth/session/parser errors. Clear recovered diagnostic problems when the next poll succeeds.

### Diagnostics and attributes

- Keep tracker attributes useful and non-duplicative. Do not expose internal IDs, account identifiers, raw monotonic counters, duplicate coordinates/name/icon fields, or implementation-only flags. Keep coordinates and detailed metadata on dedicated location/diagnostic entities as appropriate.
- Format position and next-update timestamps as ISO 8601 with Home Assistant's configured timezone/offset; keep last Samsung fix time distinct from the time HA last read/updated coordinates.
- Location Retrieval Error is intended to expose actionable state/result details (e.g. result code, HTTP status, concise error) and recover to OK. Avoid maintaining redundant diagnostic entities for the same signal.
- Count actual per-device FMM HTTP calls consistently if exposed. Do not count cache reuse or local parsing as HTTP calls.
- Detailed FMM request logs/history must be bounded and sanitized. Never log/store session cookies, CSRF values, authorization codes, raw response bodies, or coordinates. Prefer detailed sanitized logs and only the latest interaction plus counters in entity attributes.
- Low accuracy can be reported as a warning while preserving a returned fix; it should not be discarded just because it exceeds the warning threshold.

## Stability work captured from releases 8.1.0–8.1.13

The following is the history of behavior decisions/fixes in the current GitHub changelog, not a claim that the stale local `main` contains these changes:

- **8.1.0:** Per-device scheduling and in-zone intervals; retries after failed cycles; parser hardening for incomplete/odd nearby-device sightings; preserve last position when there is no usable new entry; stale threshold follows configured poll cadence; partial accuracy accepted. Added parser, scheduling, and FMM canned-response tests.
- **8.1.1:** Removed duplicate/internal tracker attributes; clarified retrieval errors vs position age; added HA-local timestamps; handled Buds icon fallback and nested/encrypted location parsing; retained last-known position briefly after transient failures.
- **8.1.2:** Added bounded, sanitized FMM request/response summaries.
- **8.1.3–8.1.4:** Immediate poll when Active Location toggles; diagnostics refresh with coordinator data; low-confidence fixes stay applied but are flagged; human-readable local ISO timestamp for next poll; longer active-operation wait; omit duplicate friendly/device/icon fields.
- **8.1.5–8.1.6:** After active requests, bootstrap a fresh website session and resolve that session's device ID again before reading; one fresh-session passive retry for stale/unreadable cache results; richer sanitized parser/operation diagnostics; backoff on HTTP/result 429.
- **8.1.7–8.1.8:** Buds startup attempt and outcomes were instrumented, then entity diagnostics were reduced while sanitized request details moved to HA logs. `is_buds` remains internal. Tracker timestamps are ISO 8601 local times and distinguish Samsung fix time from HA retrieval time.
- **8.1.9–8.1.10:** Expose per-device FMM API-call counters; update button success only for a newer fix; restore last position after reload/poll failure; expose latest API endpoint/status/result/error code.
- **8.1.11:** Fix sensor coordinator lifecycle updates; do not let older passive results overwrite newer fixes; allow manual active retry after `501`; explain no-new-position accurately; replace redundant diagnostic status/binary entities with a text retrieval-error result.
- **8.1.12:** Attempt active location on each scheduled poll while the per-device switch is on, including Buds/watches, while still doing the passive read; isolate tracker state writes per device; preserve HA `last_changed` as state/zone-transition time.
- **8.1.13:** Introduce dedicated active polling cadence for Buds/PCs/watches (default 1800 seconds, minimum 30); switch off means passive-only with normal zone/out-of-zone cadence; phones retain zone/out-of-zone cadence; remove unconditional Buds active startup attempt. A switch restored on can cause an active request after setup.

## CI and failure lessons

- GitHub Actions runs HACS validation and the small `tests/test_*.py` suite. Read `.github/workflows/` and `tests/_ha_stubs.py` before changing test import behavior.
- Prior CI failures exposed missing nested `homeassistant.util` stubs and Python syntax/indentation mistakes in `__init__.py`. Run syntax checks and the test suite when asked to validate; do not infer they pass from code inspection.
- `get_fmm_device_location` is expected to keep a prior usable fix if parsing the latest response fails. A regression test that expected the old exact coordinate to change after a parser exception was corrected to assert availability and preservation of the previous known-good position (the coordinate may therefore differ from the canned fresh response).
- A local test run previously could not start because the local Python environment lacked `pytz`; distinguish missing test dependencies from code failures.
- Before release, check manifest version, changelog, README, HACS metadata, and GitHub workflow together. Do not make a HACS release tag/version claim based on manifest alone.

## 8.2.0 (pre-release) decisions

- **Last New Position Request** (`DeviceLastNewFixSensor`): timestamp of the read that brought a fix strictly newer than `_known_gps_date` (`register_position_fix` in `utils.py`, shared by the coordinator and the Update Location button). Seeded from `saved_positions` (`last_new_fix_at`) so a restart does not fake a new fix. Distinct from `last_seen` (Samsung fix time) and `last_location_retrieved_at`.
- **Connection** sensor (`DeviceConnectionSensor`) only for `is_active_connectable_device` (PHONE/TABLET/PC). Uses `check_fmm_connection` (`CHECK_CONNECTION` via `perform_fmm_operation`, result in `dev_data['_connection']`, 60 s cache). It runs only when Active Location is on, or before a Ring. `parse_connection_state` is a heuristic over field *names*; Samsung's payload is undocumented and was not verified live. **Pre-release goal:** read the INFO log lines "CHECK_CONNECTION result" / "Sanitized structure of Samsung's Find response" and the Connection sensor attributes, then replace the heuristic with the real fields. `unknown` is never treated as offline.
- `sanitize_structure` is the only sanctioned way to log server payload shapes: key names/types plus values only for short status-like keys; blocked key patterns cover coordinates, ids, names, ssid, tokens. Extend `_STRUCT_BLOCKED_KEY` if a new sensitive field appears.
- **Exclude devices** (`excluded_devices` option): filtered in `async_setup_entry` before the coordinator exists, and removed from the device registry. `hass.data[DOMAIN][entry_id]['all_devices']` feeds the options flow picker.
- **Ring** for watches (`is_watch_device`) plus phones. Before ringing, `check_fmm_connection(use_cache=False)`: `offline` raises an error without sending RING; `unknown` rings anyway. Does not apply to SmartTags. Watch ring works only if the watch is online/connected (user-confirmed).
- **Unknown after N failed attempts** (`unknown_after_failed_attempts`, `Coordinator.apply_position_policy`). **Deliberate, opt-in exception** to "never erase a usable position", valid only for active devices (phone/tablet/PC) with Active Location on. The rule "last position is always good" still holds for all passive devices (Buds, watches, SmartTags) and for any device with the switch off. Counted: scheduled active attempts that Samsung answered (reject/failed/pending/timeout) with no genuinely newer fix. Not counted: local session/HTTP/exception errors (`active_attempt_samsung_outcome` false) and manual presses. It only hides the position in the tracker/location/maps entities (`position_unknown`); data and persisted position are untouched. Counter is in memory.
- Tests: `tests/test_v82.py`. Live Samsung/HA behavior of all of the above is still unvalidated.

## Known unresolved behavior / next investigation

- Users observed Buds/Watch passive reads frequently returning an unchanged old `extra.gpsUtcDt` (for example with `LOCATION` result `501`) even when opening Samsung Find and pressing refresh produced a new location. This supports separating “passive fetch of server's last fix” from “request a new fix,” but does not prove every FMM device requires active requests. Device support varies.
- The Windows PC case produced accepted active `LOCATION` operations that stayed pending (`operation_status 1000`) until timeout while passive reads returned the same low-accuracy fix. A fresh web session after active requests was chosen as a documented-flow experiment; validate with sanitized live logs before attributing causality.
- Buds can disappear from readable responses or return coordinates only after a manual refresh from the website. Keep one clear active-attempt policy, preserve the previous position, and expose whether Samsung returned coordinates/usable timestamps. Do not silently replace a good location with an empty/unreadable response.
- Ringing a Galaxy Watch may be supported by Samsung for some models, but this integration's current documented Ring switch is for SmartTags and FMM phones. Do not widen it without confirming the website/API capability and model eligibility.
- Related repositories were compared: `fapgomes/ha-smartthings-find` is a broad competing SmartThings Find integration; `Drongo76/ha-samsung-find-native` is a narrower SmartTag/Android-bridge approach. Recheck their current implementations before relying on that comparison.

## Working rules for the next agent

1. Start with `git status --short --branch`, `git log`, current manifest/changelog, and confirm which worktree VS Code has opened.
2. Preserve the user's dirty 8.1.3 changes in `fix/options-flow-500`; inspect diffs before edits.
3. Do not call static checks proof of Samsung/Home Assistant runtime behavior. State what was run and what it does not validate.
4. Do not push, publish, or create a GitHub release without explicit authorization. This handoff is local and currently uncommitted.
