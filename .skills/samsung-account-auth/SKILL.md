---
name: samsung-account-auth
description: Bootstrap and maintain Samsung Account PKCE authentication, rotating Find/IoT tokens, master userauth_token reissue, and the SmartThings Find website session bridge safely.
version: 0.3.0
author: Charles Bel, Hermes Agent (adapted for HA-SmartThings-Find)
license: MIT
platforms: [linux, macos, windows]
---

# Samsung Account Authentication & Token Lifecycle

Reference skill adapted from `charlesbel/samsung-re-find` (`.skills/samsung-account-auth/SKILL.md` and `src/samsung_find/auth.py`) for `HA-SmartThings-Find`.

This is an unofficial reverse-engineered flow and is not affiliated with Samsung.

## Architecture in `HA-SmartThings-Find`

1. **Stage 1 (`do_login_stage_one`)**:
   - Calls `https://account.samsung.com/accounts/ANDROIDSDK/getEntryPoint`.
   - Generates a PKCE `code_verifier`/`code_challenge` (`S256`), a random state, and a persistent 32-hex `device_id` (`physical_address_text`).
   - Encrypts `svcParam` with Samsung's public key and builds the sign-in URL.
2. **Stage 2 (`do_login_stage_two`)**:
   - Parses the `ms-app://` callback URL pasted by the user (`code`, `state`, `auth_server_url`, encrypted `userauth_token`, `login_id`).
   - Exchanges the master `userauth_token` via `/auth/oauth2/v2/authorize` + `/auth/oauth2/token` for two scoped token pairs:
     - **Find token** (`CLIENT_ID_FIND = "27zmg0v1oo"`, scope `"offline.access"`)
     - **OneConnect / IoT token** (`CLIENT_ID_ONECONNECT = "6iado3s6jc"`, scope `"iot.client"`)
   - Persists both token pairs plus `user_auth_token`, `login_id`, `user_id`, `device_id`, and `auth_server_url` in the Home Assistant `ConfigEntry`.
3. **Token Refresh & Master-Token Reissue (`_refresh_token` / `_reissue_token_from_master`)**:
   - Samsung rotates refresh tokens on every refresh (`grant_type=refresh_token`). Both `access_token` and `refresh_token` must be updated in `hass.data` and `entry.data`.
   - Updating `entry.data` triggers Home Assistant's update listener; `_async_update_listener` in `__init__.py` compares `entry.options` against `_last_options` so routine token refreshes **never** reload the integration.
   - If a rotating `refresh_token` expires or is invalidated, `_reissue_token_from_master` re-authorizes a fresh token pair using the stored `user_auth_token` (with automatic retry dropping `login_id` if `privacyAccepted == "N"`). Only if both refresh and master reissue fail should `ConfigEntryAuthFailed` be raised to trigger Home Assistant reauth.
4. **Master-Token-to-Web-Session Bridge (`get_web_session_cookie`)**:
   - Authorizes `WEB_FIND_CLIENT_ID = "ntly6zvfpn"` (`scope = "iot.client"`) using `user_auth_token`.
   - Follows the exact website sequence in an isolated HTTP client:
     `getState.do` (obtains opaque state + initial cookie) $\rightarrow$ `login.do` $\rightarrow$ `chkLogin.do` (validates session and extracts `_csrf` header).
   - **Session-scoped `dvceID` rule**: Samsung's website `dvceID` values are scoped to the login session that resolved them. Whenever `get_web_session_cookie` bootstraps a fresh web session, it **must** immediately run `enrich_devices_with_web_metadata` (`device/getDeviceList.do`) on `hass.data[DOMAIN][entry_id]["devices"]` to refresh every device's `web_dvce_id`.

## Security & Privacy Pitfalls

- Never log, store in entity attributes, or include in diagnostics:
  - `redirect_url` / `ms-app://` callback URLs
  - `user_auth_token`, `access_token`, `refresh_token`, `iot_access_token`, `iot_refresh_token`
  - `JSESSIONID` cookies, `_csrf` tokens, or raw OAuth authorization codes
- Do not run concurrent manual credential renewals on the same token family because rotating refresh tokens are single-use.
