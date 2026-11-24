import logging
import json
import asyncio
import pytz
import base64
import aiohttp
import random
import string
import html
import hashlib
import os
import secrets
import urllib.parse
import uuid
import re
import math
from datetime import datetime, timedelta
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry, entity_registry

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

from .const import (
    DOMAIN, BATTERY_LEVELS,
    CLIENT_ID_AUTH, CLIENT_ID_FIND, CLIENT_ID_ONECONNECT, SCOPE_FIND,
    CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN, CONF_AUTH_SERVER_URL, CONF_USER_ID,
    CONF_IOT_ACCESS_TOKEN, CONF_IOT_REFRESH_TOKEN, CONF_DEVICE_ID,
    CONF_INSTALLED_APP_ID, CONF_ST_USER_UUID,
    WEB_FIND_CLIENT_ID, WEB_FIND_SCOPE, CONF_USER_AUTH_TOKEN, CONF_LOGIN_ID,
    CONF_UPDATE_INTERVAL_DEFAULT, LOW_ACCURACY_THRESHOLD_M, CONNECTION_CACHE_SECONDS,
    PROXIMITY_MIN_MOVE_M, PROXIMITY_ACCURACY_FACTOR, PROXIMITY_MAX_ACCURACY_M,
    PROXIMITY_MIN_ALIGNMENT_ENTER, PROXIMITY_MIN_ALIGNMENT_KEEP, PROXIMITY_COLLINEAR_MARGIN,
    PROXIMITY_MAX_GAP_S, PROXIMITY_DECAY_S, PROXIMITY_MAX_SPEED_KMH, PROXIMITY_HISTORY_MAX_POINTS,
)

_LOGGER = logging.getLogger(__name__)
FMM_LOCATION_POLL_SECONDS = 60
FMM_CONNECTION_POLL_SECONDS = 20
ALLOWED_FMM_OPERATIONS = frozenset({
    "CHECK_CONNECTION",
    "LOCATION",
    "RING",
    "TRACK_LOCATION_START",
    "TRACK_LOCATION_STOP",
})
PENDING_OPERATION_STATUS_CODES = frozenset({
    "1000", "1100", "2100", "PENDING", "IN_PROGRESS", "RUNNING",
})


def classify_fmm_operation_result(op: dict | None, default_operation: str = "") -> str:
    """Classify a Samsung FMM operation entry as 'success', 'pending', or 'failed'
    (ported from samsung-re-find's _sanitize_operation contract)."""
    if not isinstance(op, dict):
        return "pending"
    status_raw = op.get("status_code") or op.get("oprnStsCd") or op.get("statusCode")
    result_raw = op.get("result_code") or op.get("oprnResultCode") or op.get("resultCode")
    status_code = str(status_raw) if status_raw is not None else ""
    result_code = str(result_raw) if result_raw is not None else ""
    op_type = str(op.get("oprnType") or op.get("type") or default_operation)
    if (
        (op_type == "TRACK_LOCATION_START" and status_code == "2100")
        or (status_code == "2800" and result_code == "1200")
        or status_code in {"200", "SUCCESS", "00"}
    ):
        return "success"
    if status_code in PENDING_OPERATION_STATUS_CODES:
        return "pending"
    if status_code:
        return "failed"
    return "pending"

URL_ENTRY_POINT = 'https://account.samsung.com/accounts/ANDROIDSDK/getEntryPoint'
SMARTTHINGS_APP_VERSION = "1.8.21.28"
SMARTTHINGS_DEVICE_MODEL = "Google Pixel 8 Pro"
SMARTTHINGS_OS = "Android 14"
SMARTTHINGS_USER_AGENT = (
    f"Android/OneApp/{SMARTTHINGS_APP_VERSION}/Main "
    f"({SMARTTHINGS_DEVICE_MODEL}; Android 14/14) SmartKit/4.423.1"
)


def get_random_string(length):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))


def _mask_secret(value: str | None, keep: int = 4) -> str:
    """Return a partially-masked version of a sensitive string, safe to log."""
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}...({len(value)} chars)"


def _html_unescape(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    current = value
    for _ in range(3):
        decoded = html.unescape(current)
        if decoded == current:
            break
        current = decoded
    return current


def parse_redirect_url(redirect_url: str) -> dict[str, str]:
    """Extract the query parameters from the ms-app:// URL the user pastes in.

    The URL is copied by hand out of the browser, so it arrives in whatever shape
    devtools rendered it. In particular the Network/Console panes hand out an
    HTML-escaped URL (`&amp;` instead of `&`), which would otherwise turn every
    parameter after the first into a bogus `amp;<name>` key and leave us with only
    `code`. Surrounding whitespace/quotes and a missing scheme are tolerated too.
    """
    url = _html_unescape((redirect_url or "").strip().strip('"').strip("'"))
    parsed = urllib.parse.urlparse(url)
    query = parsed.query
    if not query and parsed.fragment:
        query = parsed.fragment
    if not query and "?" in url:
        # No recognisable scheme, e.g. the user pasted only "host?code=..."
        query = url.split("?", 1)[1]
    params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items() if v}
    if parsed.fragment and parsed.query:
        params.update(
            {k: v[0] for k, v in urllib.parse.parse_qs(parsed.fragment).items() if v}
        )
    return params


def format_ring_error(err: str | None) -> str:
    if not err:
        return "Ring failed"
    if err == "unsupported_device":
        return "Ring not supported for this device."
    if err.startswith("app_error_"):
        return f"Ring failed: {err}"
    if err.startswith("http_"):
        return f"Ring failed: {err}"
    return f"Ring failed: {err}"

def get_ha_device(hass: HomeAssistant, device_id: str):
    """Look up a device in the Home Assistant device registry without triggering the
    2027.8 deprecation warning on async_get_device(identifiers=...)."""
    registry = device_registry.async_get(hass)
    identifier = (DOMAIN, device_id)
    get_by_id = getattr(registry, "async_get_device_by_identifier", None)
    if callable(get_by_id):
        try:
            return get_by_id(identifier)
        except TypeError:
            pass
    devices = getattr(registry, "devices", None)
    if isinstance(devices, dict):
        for dev in devices.values():
            if identifier in getattr(dev, "identifiers", set()):
                return dev
    return registry.async_get_device(identifiers={identifier})


def _sync_entity_names(hass: HomeAssistant, device_id: str, name: str) -> None:
    """Follow a rename made in the Samsung app through to the entity names.

    Entity names carry a suffix ("<name> Battery"), so they can never equal the bare
    device name - the previous version compared them directly and therefore only ever
    renamed the tracker, and only to undo HTML escaping. Build the expected name per
    entity instead. A name the user set themselves (entry.name) is never touched.
    """
    if not device_id or not name:
        return
    registry = entity_registry.async_get(hass)
    expected_names = (
        ("device_tracker", f"stf_device_tracker_{device_id}", name),
        ("sensor", f"stf_device_battery_{device_id}", f"{name} Battery"),
        ("sensor", f"stf_device_battery_left_{device_id}", f"{name} Left Battery"),
        ("sensor", f"stf_device_battery_right_{device_id}", f"{name} Right Battery"),
        ("sensor", f"stf_device_location_{device_id}", f"{name} Location"),
        ("sensor", f"stf_device_maps_link_{device_id}", f"{name} Maps Link"),
        ("sensor", f"stf_direction_{device_id}", f"{name} Direction"),
        ("binary_sensor", f"stf_power_saving_{device_id}", f"{name} Power Saving"),
        ("switch", f"stf_ring_switch_{device_id}", f"{name} Ring"),
        ("button", f"stf_ring_button_{device_id}", f"{name} Ring"),
        ("button", f"stf_ring_stop_button_{device_id}", f"{name} Stop Ring"),
    )
    for domain, unique_id, expected in expected_names:
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        if not entity_id:
            continue
        entry = registry.async_get(entity_id)
        if not entry or entry.name:
            continue
        if (entry.original_name or "") == expected:
            continue
        _LOGGER.debug("Renaming entity '%s' to '%s'", entity_id, expected)
        registry.async_update_entity(entity_id, original_name=expected)


def generate_code_verifier():
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b'=').decode('utf-8')


def generate_code_challenge(verifier):
    digest = hashlib.sha256(verifier.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode('utf-8')


def _get_auth_data(hass: HomeAssistant) -> dict:
    return hass.data.setdefault(DOMAIN, {}).setdefault("auth_data", {})


def _get_or_create_device_id(hass: HomeAssistant) -> str:
    auth_data = _get_auth_data(hass)
    device_id = auth_data.get("device_id")
    if not device_id:
        device_id = secrets.token_hex(16)
        auth_data["device_id"] = device_id
    return device_id


def _decrypt_auth_value(value: str, key: str) -> str | None:
    try:
        key_bytes = key.encode("utf-8")
        if len(key_bytes) < 16:
            key_bytes = key_bytes.ljust(16, b"\0")
        else:
            key_bytes = key_bytes[:16]
        cipher = Cipher(algorithms.AES(key_bytes), modes.ECB(), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(bytes.fromhex(value)) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        return plaintext.decode("utf-8")
    except Exception as exc:
        _LOGGER.debug("Failed to decrypt auth value: %s", exc)
        return None


def _get_timezone_offset() -> str:
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return "UTC+00:00"
    total_minutes = int(offset.total_seconds() / 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _get_accept_language(hass: HomeAssistant) -> str:
    language = getattr(hass.config, "language", None) or "en"
    country = getattr(hass.config, "country", None)
    if country:
        return f"{language}-{country}"
    return language


def _get_smartthings_headers(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    data_store = hass.data[DOMAIN][entry_id]
    token = data_store.get(CONF_IOT_ACCESS_TOKEN)
    correlation_id = data_store.get("correlation_id")
    if not correlation_id:
        correlation_id = str(uuid.uuid4())
        data_store["correlation_id"] = correlation_id
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.smartthings+json;v=1",
        "Accept-Language": _get_accept_language(hass),
        "User-Agent": SMARTTHINGS_USER_AGENT,
        "X-St-Client-Appversion": SMARTTHINGS_APP_VERSION,
        "X-St-Client-Devicemodel": SMARTTHINGS_DEVICE_MODEL,
        "X-St-Client-Os": SMARTTHINGS_OS,
        "X-St-Correlation": correlation_id,
    }
    return headers


async def _smartthings_get_json(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    url: str
) -> tuple[int, dict | None]:
    headers = _get_smartthings_headers(hass, entry_id)
    async with session.get(url, headers=headers) as res:
        if res.status in [401, 403]:
            await refresh_iot_token(hass, session, entry_id)
            headers = _get_smartthings_headers(hass, entry_id)
            async with session.get(url, headers=headers) as retry_res:
                return retry_res.status, await retry_res.json()
        return res.status, await res.json()


# Investigation note, so this is not repeated. The SmartThings app's per-tag settings
# live behind the "chaser" API (https://client.smartthings.com/chaser), whose paths were
# recovered from the app's dex string tables:
#   /trackers/{id}/{metadata,searchingstatus,button/options,timer,category,firmware}
# Probed read-only against a SmartTag2 (UWB_TAG) with our IoT bearer token: only the two
# global endpoints (/trackers/categories, /utsconfig) return 200. Every per-tag endpoint
# is 403 (metadata, with Accept v1 and v6 alike - a real permission boundary) or 405
# (resource exists, GET not allowed), and Samsung sends no Allow header, so the accepted
# verb cannot be discovered without sending one. No power-saving setting is reachable
# there; it lives in bleD2D.metadata below.
#
# Writing the setting is not reproducible: the app does it through its own
# tracker-metadata update, and a re-signed build of the app cannot be used to capture
# that request because Samsung rejects it server-side with AUT_1708 (invalid client -
# the signing certificate is validated against a registered whitelist).


async def get_device_ble_metadata(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    device_id: str
) -> dict | None:
    """Return the tag's bleD2D.metadata blob from the public SmartThings device API.

    This is where the per-tag settings actually live. Confirmed by A/B test against a
    SmartTag2: toggling "Energiesparmodus" in the SmartThings app flips
    `activeMode.mode` between 1 (power saving on) and 0 (off), and nothing else in the
    whole payload changes. The blob also carries battery.level, firmware.version,
    searchingStatus, e2eEncryption, remoteRing and lastKnownConnection.

    Note the SmartThings *capability* values (tag.uwbActivation and friends) are all
    null for this tag and did not move when the setting was toggled, so they are not a
    usable source for this.
    """
    if not device_id:
        return None
    url = f"{SMARTTHINGS_API_BASE}/devices/{device_id}"
    try:
        headers = _get_smartthings_headers(hass, entry_id)
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
        ) as res:
            if res.status in (401, 403):
                await refresh_iot_token(hass, session, entry_id)
                headers = _get_smartthings_headers(hass, entry_id)
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
                ) as retry:
                    if retry.status != 200:
                        _LOGGER.debug(
                            "BLE metadata fetch failed after refresh [%s]", retry.status
                        )
                        return None
                    body = await retry.json()
            elif res.status != 200:
                _LOGGER.debug("BLE metadata fetch failed [%s]", res.status)
                return None
            else:
                body = await res.json()
    except Exception as exc:
        _LOGGER.debug("BLE metadata fetch error: %s", exc)
        return None

    ble = (body or {}).get("bleD2D") or {}
    metadata = ble.get("metadata")
    return metadata if isinstance(metadata, dict) else None


def get_power_saving_state(metadata: dict | None) -> bool | None:
    """True when the tag's power saving mode ("Energiesparmodus") is on.

    `activeMode.mode` names which mode is active: 0 = normal, 1 = power saving.
    Returns None when the field is absent, so callers can tell "off" from "unknown".
    """
    if not isinstance(metadata, dict):
        return None
    mode = (metadata.get("activeMode") or {}).get("mode")
    if mode is None:
        return None
    try:
        return int(mode) == 1
    except (TypeError, ValueError):
        return None


# Public SmartThings API. A tag's settings screen renders its capability list (that is
# why "Battery" shows up there), and capability values are readable from here.
SMARTTHINGS_API_BASE = "https://api.smartthings.com/v1"


async def _ensure_smartthings_user_info(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str
) -> tuple[str | None, str | None]:
    data_store = hass.data[DOMAIN][entry_id]
    if data_store.get(CONF_ST_USER_UUID):
        return data_store.get(CONF_ST_USER_UUID), data_store.get("st_country_code")

    status, data = await _smartthings_get_json(
        hass,
        session,
        entry_id,
        "https://auth.api.smartthings.com/users/me"
    )
    if status != 200 or not data:
        _LOGGER.error("Failed to fetch SmartThings user info: %s", data)
        return None, None

    user_uuid = data.get("uuid")
    country_code = data.get("countryCode")
    data_store[CONF_ST_USER_UUID] = user_uuid
    if country_code:
        data_store["st_country_code"] = country_code

    entry = hass.config_entries.async_get_entry(entry_id)
    if entry:
        new_data = entry.data.copy()
        new_data[CONF_ST_USER_UUID] = user_uuid
        hass.config_entries.async_update_entry(entry, data=new_data)

    return user_uuid, country_code


async def _get_installed_app_id(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str
) -> str | None:
    data_store = hass.data[DOMAIN][entry_id]
    cached = data_store.get(CONF_INSTALLED_APP_ID)
    if cached:
        return cached

    user_uuid, _ = await _ensure_smartthings_user_info(hass, session, entry_id)
    if not user_uuid:
        return None

    url = "https://api.smartthings.com/installedapps?allowed=true"
    all_items = []
    while url:
        status, data = await _smartthings_get_json(hass, session, entry_id, url)
        if status != 200 or not data:
            _LOGGER.error("Failed to fetch installed apps: %s", data)
            return None
        items = data.get("items", [])
        all_items.extend(items)
        url = (data.get("_links", {}) or {}).get("next", {}) or {}
        url = url.get("href")

    plugin_id = "com.samsung.android.plugin.fme"
    app_id = None
    for item in all_items:
        ui = item.get("ui", {})
        owner = item.get("owner", {})
        if ui.get("pluginId") == plugin_id and owner.get("ownerId") == user_uuid:
            app_id = item.get("installedAppId")
            break
    if not app_id:
        for item in all_items:
            ui = item.get("ui", {})
            if ui.get("pluginId") == plugin_id:
                app_id = item.get("installedAppId")
                break

    if app_id:
        data_store[CONF_INSTALLED_APP_ID] = app_id
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry:
            new_data = entry.data.copy()
            new_data[CONF_INSTALLED_APP_ID] = app_id
            hass.config_entries.async_update_entry(entry, data=new_data)

    return app_id


def _encode_base64_json(payload: dict | list | str | None) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        raw = payload.encode("utf-8")
    else:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return base64.b64encode(raw).decode("utf-8")


async def _build_installed_apps_request(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    method: str,
    uri: str,
    extra_uri: str | None = None,
    extra_params: dict | None = None,
    headers: dict | None = None,
    body: dict | None = None
) -> dict | None:
    data_store = hass.data[DOMAIN][entry_id]
    device_id = data_store.get(CONF_DEVICE_ID) or _get_or_create_device_id(hass)
    data_store[CONF_DEVICE_ID] = device_id
    token = data_store.get(CONF_IOT_ACCESS_TOKEN)
    user_id = data_store.get(CONF_USER_ID)
    if not user_id:
        return None

    request_body = {
        "client": {
            "displayMode": "LIGHT",
            "language": _get_accept_language(hass),
            "mobileDeviceId": device_id,
            "os": "Android",
            "samsungAccountId": data_store.get(CONF_USER_ID),
            "supportedTemplates": [
                "BASIC_V1", "BASIC_V2", "BASIC_V3", "BASIC_V4",
                "BASIC_V5", "BASIC_V6", "BASIC_V7"
            ],
            "timeZoneOffset": _get_timezone_offset(),
            "version": SMARTTHINGS_APP_VERSION
        },
        "parameters": {
            "requester": user_id,
            "requesterToken": token,
            "clientType": "aPlugin",
            "clientVersion": "1",
            "method": method,
            "uri": uri,
            "extraUri": extra_uri,
            "encodedHeaders": _encode_base64_json(headers),
            "encodedBody": _encode_base64_json(body),
        }
    }

    if extra_params:
        request_body["parameters"].update(extra_params)

    return request_body


async def _execute_installed_app(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    method: str,
    uri: str,
    extra_uri: str | None = None,
    extra_params: dict | None = None,
    headers: dict | None = None,
    body: dict | None = None
) -> tuple[int, dict | None]:
    app_id = await _get_installed_app_id(hass, session, entry_id)
    if not app_id:
        return 500, {"error": "missing_installed_app_id"}

    request_body = await _build_installed_apps_request(
        hass,
        session,
        entry_id,
        method,
        uri,
        extra_uri=extra_uri,
        extra_params=extra_params,
        headers=headers,
        body=body
    )
    if request_body is None:
        return 500, {"error": "missing_user_info"}

    exec_url = f"https://api.smartthings.com/installedapps/{app_id}/execute"

    st_headers = _get_smartthings_headers(hass, entry_id)
    st_headers["Content-Type"] = "application/json"

    async with session.post(exec_url, json=request_body, headers=st_headers) as res:
        if res.status in [401, 403]:
            await refresh_iot_token(hass, session, entry_id)
            request_body = await _build_installed_apps_request(
                hass,
                session,
                entry_id,
                method,
                uri,
                extra_uri=extra_uri,
                extra_params=extra_params,
                headers=headers,
                body=body
            )
            st_headers = _get_smartthings_headers(hass, entry_id)
            st_headers["Content-Type"] = "application/json"
            async with session.post(exec_url, json=request_body, headers=st_headers) as retry_res:
                return retry_res.status, await retry_res.json()
        return res.status, await res.json()


def _parse_installed_apps_response(response: dict | None) -> tuple[int | None, dict | None, str | None]:
    if not response:
        return None, None, "empty_response"
    status_code = response.get("statusCode")
    message = response.get("message")
    error_code = response.get("errorCode")
    return status_code, message, error_code


def encrypt_svc_param(svc_param_json, chk_do_num, public_key):
    chk_do_num_str = str(chk_do_num)
    chk_do_num_hash = hashlib.sha256(chk_do_num_str.encode('utf-8')).digest()

    key = os.urandom(16)

    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        base64.b64encode(chk_do_num_hash),
        key,
        chk_do_num,
        dklen=16,
    )

    svc_enc_ky = public_key.encrypt(
        base64.b64encode(derived_key),
        asym_padding.PKCS1v15()
    )
    svc_enc_ky_b64 = base64.b64encode(svc_enc_ky).decode('utf-8')

    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES(derived_key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    
    padder = padding.PKCS7(128).padder()
    padded_data = padder.update(svc_param_json.encode('utf-8')) + padder.finalize()
    
    svc_enc_param = encryptor.update(padded_data) + encryptor.finalize()
    svc_enc_param_b64 = base64.b64encode(svc_enc_param).decode('utf-8')

    payload_dict = {
        "chkDoNum": chk_do_num_str,
        "svcEncParam": svc_enc_param_b64,
        "svcEncKY": svc_enc_ky_b64,
        "svcEncIV": iv.hex(),
    }
    payload_json = json.dumps(payload_dict)
    payload_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("utf-8")
    return urllib.parse.quote(payload_b64)


async def do_login_stage_one(hass: HomeAssistant) -> tuple:
    session = async_get_clientsession(hass)
    
    # 1. Get Entry Point
    async with session.get(URL_ENTRY_POINT) as res:
        if res.status != 200:
            return None, "Failed to get entry point"
        data = await res.json()
        
    sign_in_uri = data['signInURI']
    pki_public_key = data['pkiPublicKey']
    chk_do_num = int(data['chkDoNum'])

    # 2. Generate SVC Param
    state = get_random_string(20)
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    
    auth_data = _get_auth_data(hass)
    device_id = _get_or_create_device_id(hass)
    auth_data.update({
        "state": state,
        "code_verifier": code_verifier
    })
    if _LOGGER.isEnabledFor(logging.DEBUG):
        _LOGGER.debug(
            "Auth debug: state=%s code_verifier=%s device_id=%s",
            _mask_secret(state),
            _mask_secret(code_verifier),
            _mask_secret(device_id)
        )

    svc_param = {
        "clientId": CLIENT_ID_AUTH,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "competitorDeviceYNFlag": "Y",
        "countryCode": (hass.config.country or "us").lower(),
        "deviceInfo": "Google|com.android.chrome",
        "deviceModelID": "Pixel 8 Pro",
        "deviceName": "Google Pixel 8 Pro",
        "deviceOSVersion": "35",
        "devicePhysicalAddressText": f"ANID:{device_id}",
        "deviceType": "APP",
        "deviceUniqueID": device_id,
        "redirect_uri": "ms-app://s-1-15-2-4027708247-2189610-1983755848-2937435718-1578786913-2158692839-1974417358",
        "replaceableClientConnectYN": "N",
        "replaceableClientId": "",
        "replaceableDevicePhysicalAddressText": "",
        "responseEncryptionType": "1",
        "responseEncryptionYNFlag": "Y",
        "scope": "",
        "state": state,
        "svcIptLgnID": "",
        "iosYNFlag": "Y",
    }
    
    svc_param_json = json.dumps(svc_param)
    
    # 3. Encrypt Payload
    try:
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        pub_key_bytes = base64.b64decode(pki_public_key)
        try:
            public_key = load_der_public_key(pub_key_bytes, backend=default_backend())
        except:
             # Fallback usually not needed if standard DER
              pass
        
        if 'public_key' not in locals():
             raise Exception("Failed to load public key")
        
        svc_param_value = encrypt_svc_param(svc_param_json, chk_do_num, public_key)

    except Exception as e:
        _LOGGER.error(f"Encryption failed: {e}")
        return None, f"Encryption failed: {e}"

    login_url = f"{sign_in_uri}?locale=en&svcParam={svc_param_value}&mode=C"

    _LOGGER.info(f"Generated Login URL: {login_url}")
    
    return login_url, None



async def do_login_stage_two(
    hass: HomeAssistant,
    redirect_url: str
) -> tuple[dict | None, dict | None, str | None, str | None, str | None]:
    session = async_get_clientsession(hass)
    auth_data = hass.data.get(DOMAIN, {}).get('auth_data')
    if not auth_data:
        return None, None, None, None, None, None, "Auth data missing. Please restart flow."
    
    state_orig = auth_data['state']
    code_verifier = auth_data['code_verifier']
    device_id = auth_data.get("device_id") or _get_or_create_device_id(hass)

    # Parse parameters from redirect URL
    params = parse_redirect_url(redirect_url)
    _LOGGER.debug("Redirect URL parameters found: %s", sorted(params))

    # Parameters needed: code, auth_server_url, state, retValue (the login id)
    auth_server_url = params.get('auth_server_url', '')
    code = params.get('code', '')
    state_param = params.get('state', '')
    ret_value = params.get('retValue', '')

    missing = [
        name for name, value in (
            ("code", code),
            ("state", state_param),
            ("auth_server_url", auth_server_url),
            ("retValue", ret_value),
        ) if not value
    ]
    if missing:
        return None, None, None, None, None, None, (
            f"Redirect URL is missing these parameters: {', '.join(missing)}. "
            "Copy the complete ms-app:// URL (everything up to the end of the line) "
            "from the Network/Console tab and paste it unmodified."
        )

    # Samsung encrypts the response parameters (responseEncryptionYNFlag=Y): `state`
    # is AES-encrypted with the state we sent in stage one, and its plaintext is the
    # key for the remaining values.
    if state_param != state_orig:
        decrypted_state = _decrypt_auth_value(state_param, state_orig)
        if not decrypted_state:
            return None, None, None, None, None, None, (
                "Could not decrypt the redirect URL. It belongs to a different login "
                "attempt than the one this dialog started - open the login link shown "
                "above again and paste the URL from that login."
            )
        decrypted = {}
        for name, value in (
            ("auth_server_url", auth_server_url),
            ("code", code),
            ("retValue", ret_value),
        ):
            plain = _decrypt_auth_value(value, decrypted_state)
            if not plain:
                return None, None, None, None, None, None, (
                    f"Could not decrypt '{name}' from the redirect URL. "
                    "Please restart the login and paste the URL unmodified."
                )
            decrypted[name] = plain
        auth_server_url = decrypted["auth_server_url"]
        code = decrypted["code"]
        ret_value = decrypted["retValue"]

    if not auth_server_url.startswith("http"):
        auth_server_url = f"https://{auth_server_url}"

    _LOGGER.debug(
        "Stage two: auth_server_url=%s code=%s username=%s",
        auth_server_url,
        _mask_secret(code),
        _mask_secret(ret_value)
    )
    
    async with session.post(
        f"{auth_server_url}/auth/oauth2/authenticate",
        data={
            "grant_type": "authorization_code",
            "serviceType": "M",
            "client_id": CLIENT_ID_AUTH,
            "code": code,
            "code_verifier": code_verifier,
            "username": ret_value,
            "physical_address_text": device_id,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    ) as res:
        if res.status != 200:
             return None, None, None, None, None, None, f"Token exchange failed: HTTP {res.status} - {_mask_secret(await res.text(), keep=40)}"
        data = await res.json()

    user_auth_token = data.get('userauth_token') or data.get('userAuthToken')
    user_id = data.get('userId') or data.get('user_id')
    login_id = data.get('loginId') or data.get('login_id') or ret_value
    if not user_auth_token or not user_id:
        return None, None, None, None, None, None, "Authenticate response missing user token or user id"

    async def _authorize_and_token(
        client_id: str,
        scope: str
    ) -> tuple[dict | None, str | None]:
        new_verifier = generate_code_verifier()
        new_challenge = generate_code_challenge(new_verifier)

        params_auth = {
            "response_type": "code",
            "client_id": client_id,
            "scope": scope,
            "code_challenge": new_challenge,
            "code_challenge_method": "S256",
            "userauth_token": user_auth_token,
            "serviceType": "M",
            "childAccountSupported": "Y",
            "physical_address_text": device_id,
            "login_id": login_id,
        }

        async with session.get(
            f"{auth_server_url}/auth/oauth2/v2/authorize",
            params=params_auth
        ) as res:
            if res.status != 200:
                 return None, f"Authorize failed: HTTP {res.status} - {_mask_secret(await res.text(), keep=40)}"
            auth_data = await res.json()

        auth_code = auth_data.get('code')
        if not auth_code and auth_data.get('privacyAccepted') == "N" and params_auth.get("login_id"):
            params_auth.pop("login_id", None)
            async with session.get(
                f"{auth_server_url}/auth/oauth2/v2/authorize",
                params=params_auth
            ) as res:
                if res.status != 200:
                     return None, f"Authorize failed: HTTP {res.status} - {_mask_secret(await res.text(), keep=40)}"
                auth_data = await res.json()
            auth_code = auth_data.get('code')

        if not auth_code:
            return None, "Authorize response missing code"

        async with session.post(
            f"{auth_server_url}/auth/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": auth_code,
                "code_verifier": new_verifier,
                "physical_address_text": device_id,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        ) as res:
            if res.status != 200:
                 return None, f"Token exchange failed: HTTP {res.status} - {_mask_secret(await res.text(), keep=40)}"
            token_data = await res.json()

        return token_data, None
    
    find_token, err = await _authorize_and_token(CLIENT_ID_FIND, SCOPE_FIND)
    if not find_token:
        return None, None, None, None, None, None, f"Find authorization failed: {err}"

    iot_token, err = await _authorize_and_token(CLIENT_ID_ONECONNECT, "iot.client")
    if not iot_token:
        return None, None, None, None, None, None, f"SmartThings authorization failed: {err}"

    return find_token, iot_token, user_id, auth_server_url, user_auth_token, login_id, None




async def _reissue_token_from_master(
    session: aiohttp.ClientSession,
    auth_server_url: str,
    user_auth_token: str,
    device_id: str,
    login_id: str | None,
    client_id: str,
    scope: str,
) -> dict | None:
    """Reissue a Find/IoT token pair from the persistent master userauth_token when a
    rotating refresh_token has expired or been invalidated (ported from samsung-re-find)."""
    if not auth_server_url or not user_auth_token or not device_id:
        return None
    verifier = generate_code_verifier()
    challenge = generate_code_challenge(verifier)
    params = {
        "response_type": "code",
        "serviceType": "M",
        "client_id": client_id,
        "code_challenge_method": "S256",
        "childAccountSupported": "Y",
        "userauth_token": user_auth_token,
        "code_challenge": challenge,
        "physical_address_text": device_id,
        "scope": scope,
    }
    if login_id:
        params["login_id"] = login_id
    async with session.get(f"{auth_server_url}/auth/oauth2/v2/authorize", params=params) as res:
        if res.status != 200:
            return None
        auth_data = await res.json(content_type=None)
    code = auth_data.get("code") if isinstance(auth_data, dict) else None
    if not code and isinstance(auth_data, dict) and auth_data.get("privacyAccepted") == "N" and "login_id" in params:
        params.pop("login_id", None)
        async with session.get(f"{auth_server_url}/auth/oauth2/v2/authorize", params=params) as res:
            if res.status != 200:
                return None
            auth_data = await res.json(content_type=None)
        code = auth_data.get("code") if isinstance(auth_data, dict) else None
    if not code:
        return None
    async with session.post(
        f"{auth_server_url}/auth/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "physical_address_text": device_id,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ) as res:
        if res.status != 200:
            return None
        return await res.json(content_type=None)


async def _refresh_token(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    refresh_token_key: str,
    access_token_key: str,
    client_id: str,
    scope: str = SCOPE_FIND,
) -> str:
    """Refreshes the access token using the given refresh token key, falling back to
    reissuing from the master userauth_token if the refresh token has expired."""
    data_store = hass.data[DOMAIN][entry_id]
    refresh_token = data_store.get(refresh_token_key)
    auth_server_url = data_store.get(CONF_AUTH_SERVER_URL)

    if not auth_server_url:
        raise ConfigEntryAuthFailed("Auth URL missing")

    try:
        data = None
        if refresh_token:
            url = f"{auth_server_url}/auth/oauth2/token"
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id
            }
            headers = {'Content-Type': 'application/x-www-form-urlencoded'}

            async with session.post(url, data=payload, headers=headers) as res:
                if res.status == 200:
                    data = await res.json()
                else:
                    _LOGGER.warning(
                        "Token refresh failed (HTTP %s for %s), attempting master-token reissue",
                        res.status, access_token_key,
                    )
        if not data or not data.get("access_token") or not data.get("refresh_token"):
            data = await _reissue_token_from_master(
                session,
                auth_server_url,
                data_store.get(CONF_USER_AUTH_TOKEN),
                data_store.get(CONF_DEVICE_ID),
                data_store.get(CONF_LOGIN_ID),
                client_id,
                scope,
            )
            if not data:
                raise ConfigEntryAuthFailed("Token refresh and master-token reissue both failed")

        new_access_token = data.get('access_token')
        new_refresh_token = data.get('refresh_token')

        if not new_access_token or not new_refresh_token:
            raise ConfigEntryAuthFailed("Invalid refresh response")

        # Update hass.data
        data_store[access_token_key] = new_access_token
        data_store[refresh_token_key] = new_refresh_token

        # Update Config Entry
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry:
            new_data = entry.data.copy()
            new_data[access_token_key] = new_access_token
            new_data[refresh_token_key] = new_refresh_token
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info("Successfully refreshed token for %s", access_token_key)
        return new_access_token

    except Exception as e:
        _LOGGER.error(f"Error refreshing token: {e}")
        raise ConfigEntryAuthFailed(f"Error refreshing token: {e}")


async def refresh_iot_token(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str
) -> str:
    return await _refresh_token(
        hass,
        session,
        entry_id,
        CONF_IOT_REFRESH_TOKEN,
        CONF_IOT_ACCESS_TOKEN,
        CLIENT_ID_ONECONNECT,
        WEB_FIND_SCOPE,
    )


LOCATION_OP_TYPES = ("LOCATION", "LASTLOC", "OFFLINE_LOC")

# (device, kind-of-problem, shape) combinations already reported, so a persistent oddity in
# Samsung's data is explained once in the log instead of on every poll.
_REPORTED_SKIPS: set = set()
MAX_FMM_REQUEST_HISTORY = 30


def _record_fmm_request(
    history: list | None,
    endpoint: str,
    *,
    http_status: int | None = None,
    result_code: str | int | None = None,
    outcome: str | None = None,
    details: dict | None = None,
    response: dict | None = None,
) -> None:
    """Keep a short, coordinate- and credential-free summary of an FMM response."""
    if history is None:
        return
    record = {
        "time": datetime.now(pytz.UTC).isoformat(timespec="seconds"),
        "endpoint": endpoint,
        "http_status": http_status,
        "result_code": result_code,
        "outcome": outcome,
        # Mark only actual HTTP request attempts. Cache reuse, parser summaries,
        # and skipped operations are diagnostic events, not API calls.
        "api_call": endpoint in {
            "authorize", "authorize_retry", "getState.do", "login.do",
            "chkLogin.do", "getDeviceList.do", "setLastSelect.do",
            "addOperation.do", "getOperationResult.do",
        } and outcome != "skipped",
    }
    if details:
        record.update(details)
    if isinstance(response, dict):
        if result_code is None:
            record["result_code"] = response.get("resultCode")
        operations = response.get("operation")
        if isinstance(operations, list):
            record["returned_operations"] = [
                _summarize_location_operation(op)
                for op in operations
                if isinstance(op, dict)
            ][:10]
            record["operation_count"] = len(operations)
    history.append(record)
    del history[:-MAX_FMM_REQUEST_HISTORY]


def _to_float(value) -> float | None:
    """float(value), or None for None / "" / anything that isn't a number. Never raises."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_stf_date_safe(value) -> datetime | None:
    """Like parse_stf_date, but returns None for a missing or unrecognised value instead
    of raising. Also accepts ISO 8601, in case Samsung uses it on some entry types."""
    if value is None or value == "":
        return None
    text = str(value)
    try:
        return parse_stf_date(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=pytz.UTC)


def _describe_value(value) -> str:
    if value is None:
        return "None"
    if isinstance(value, str):
        return "str" if value else "empty-str"
    if isinstance(value, dict):
        return "{" + ",".join(sorted(str(k) for k in value)) + "}"
    if isinstance(value, (list, tuple)):
        return f"list[{len(value)}]"
    return type(value).__name__


def _summarize_op(op: dict) -> str:
    """Coordinate-free description of an operation entry (which keys it has, what type each
    value is, and the date strings), so an unexpected shape can be diagnosed from a log
    without the log containing anyone's position."""
    shape = {k: _describe_value(v) for k, v in op.items()}
    dates = {k: op[k] for k in ("oprnCrtDate", "oprnDoneDate") if op.get(k)}
    extra = op.get("extra")
    if isinstance(extra, dict) and extra.get("gpsUtcDt"):
        dates["extra.gpsUtcDt"] = extra["gpsUtcDt"]
    return f"keys={shape} dates={dates}"


def _summarize_location_operation(op: dict) -> dict:
    """Return a coordinate-free summary explaining why a location was usable or skipped."""
    nested = op.get("encLocation")
    nested_dict = nested if isinstance(nested, dict) else {}
    extra = op.get("extra") if isinstance(op.get("extra"), dict) else {}
    timestamps = {
        key: value
        for key, value in (
            ("extra.gpsUtcDt", extra.get("gpsUtcDt")),
            ("gpsUtcDt", op.get("gpsUtcDt")),
            ("oprnDoneDate", op.get("oprnDoneDate")),
            ("oprnCrtDate", op.get("oprnCrtDate")),
        )
        if value not in (None, "")
    }
    return {
        "type": op.get("oprnType"),
        "status": op.get("oprnStsCd"),
        "result_code": op.get("oprnResultCode"),
        "has_top_level_coordinates": (
            op.get("latitude") is not None and op.get("longitude") is not None
        ),
        "has_nested_coordinates": (
            nested_dict.get("latitude") is not None
            and nested_dict.get("longitude") is not None
        ),
        "enc_location_shape": _describe_value(nested) if "encLocation" in op else "absent",
        "encrypted": bool(nested_dict.get("encrypted")) or (
            "encLocation" in op and not isinstance(nested, dict) and nested not in (None, "")
        ),
        "timestamp_fields": timestamps,
        "keys": sorted(str(key) for key in op),
    }


def _report_skip_once(dev_name: str, op: dict, reason: str, level: int = logging.WARNING) -> None:
    signature = (dev_name, op.get("oprnType"), reason, tuple(sorted(op)))
    if signature in _REPORTED_SKIPS:
        _LOGGER.debug("[%s] Skipped %s entry: %s", dev_name, op.get("oprnType"), reason)
        return
    _REPORTED_SKIPS.add(signature)
    _LOGGER.log(
        level,
        "[%s] Skipped a %s entry from Samsung (%s). Reported once per device and shape. %s",
        dev_name, op.get("oprnType"), reason, _summarize_op(op),
    )


def extract_best_location(
    operations: list, dev_name: str, diagnostics: dict | None = None
) -> tuple[dict | None, dict | None]:
    """
    Extracts the newest usable location from the list of operations.
    Returns (used_op, used_loc), or (None, None) when there is none.

    Never raises, and never lets an unusable entry win: an entry with no readable coordinates
    is skipped (it can't replace a valid older position with an empty one), and so is one whose
    values don't parse. This is what keeps one odd entry - e.g. a position reported by nearby
    devices on the Find network - from turning a device Unavailable or wiping its position.
    """
    best_op = None
    best_loc = None
    saw_encrypted = False

    for op in operations or []:
        if not isinstance(op, dict):
            continue
        op_type = op.get("oprnType")
        if op_type not in LOCATION_OP_TYPES:
            continue

        source = op
        lat = _to_float(source.get("latitude"))
        lon = _to_float(source.get("longitude"))
        if lat is None or lon is None:
            nested = op.get("encLocation")
            if isinstance(nested, dict):
                if nested.get("encrypted"):
                    saw_encrypted = True
                    if diagnostics is not None:
                        diagnostics["encrypted"] = True
                    _LOGGER.debug("[%s] Ignoring encrypted %s location", dev_name, op_type)
                    continue
                source = nested
                lat = _to_float(source.get("latitude"))
                lon = _to_float(source.get("longitude"))
            elif nested:
                # Present but not an object (e.g. an opaque encrypted blob): unreadable here.
                saw_encrypted = True
                if diagnostics is not None:
                    diagnostics["encrypted"] = True
                _report_skip_once(dev_name, op, "encLocation is not a readable object")
                continue
        if lat is None or lon is None:
            if diagnostics is not None:
                diagnostics.setdefault("skipped", set()).add("no_usable_coordinates")
            # Routine for operations that carry no position at all (e.g. a failed request).
            _report_skip_once(dev_name, op, "no usable coordinates", level=logging.INFO)
            continue

        extra = op.get("extra") if isinstance(op.get("extra"), dict) else {}
        utc_date = _parse_stf_date_safe(
            extra.get("gpsUtcDt") or source.get("gpsUtcDt") or op.get("gpsUtcDt"))
        if utc_date is None:
            if diagnostics is not None:
                diagnostics.setdefault("skipped", set()).add("no_usable_date")
            # The fix has coordinates but no timestamp of its own: use the time Samsung
            # recorded the operation rather than throw a real position away.
            for key in ("oprnDoneDate", "oprnCrtDate"):
                utc_date = _parse_stf_date_safe(op.get(key))
                if utc_date is not None:
                    break
        if utc_date is None:
            _report_skip_once(dev_name, op, "coordinates but no usable date")
            continue

        if best_loc and best_loc["gps_date"] >= utc_date:
            _LOGGER.debug("[%s] Ignoring older location (%s)", dev_name, op_type)
            continue

        best_loc = {
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": calc_gps_accuracy(
                source.get("horizontalUncertainty", op.get("horizontalUncertainty")),
                source.get("verticalUncertainty", op.get("verticalUncertainty"))),
            "gps_date": utc_date,
        }
        best_op = op

    if best_op is None:
        if saw_encrypted:
            _report_skip_once(
                dev_name, {"oprnType": "encrypted"},
                "only end-to-end encrypted positions were returned, which can't be read here")
        return None, None
    return best_op, best_loc


async def _get_web_client(hass: HomeAssistant, entry_id: str, force_new: bool = False) -> aiohttp.ClientSession:
    """Returns a dedicated aiohttp session for smartthingsfind.samsung.com calls, isolated
    from HA's long-lived shared session (which stays in use for the SmartThings OAuth
    tracker API and lives for as long as HA runs, reused across totally unrelated calls
    for days). Observed behavior: the web bridge works once right after a fresh login/
    reload, then fails permanently on every subsequent cycle despite every bootstrap step
    structurally succeeding - consistent with the shared connection itself becoming
    unusable for these specific endpoints over time. samsung-re-find deliberately creates
    a throwaway HTTP client per web session instead of reusing one long-lived connection;
    this mirrors that. Recycled (closed + replaced) whenever we force a fresh bootstrap.
    """
    data_store = hass.data[DOMAIN][entry_id]
    client_key = "_web_client"
    existing = data_store.get(client_key)
    if existing and not existing.closed and not force_new:
        return existing
    if existing and not existing.closed:
        try:
            await existing.close()
        except Exception:
            pass
    new_client = aiohttp.ClientSession()
    data_store[client_key] = new_client
    return new_client


async def get_web_session_cookie(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    force_refresh: bool = False,
    request_history: list | None = None,
) -> tuple[str | None, str | None]:
    """Bootstrap (or reuse) a smartthingsfind.samsung.com web session (JSESSIONID + CSRF)
    from the master userauth_token captured at login.

    This mirrors the bridge documented by the MIT-licensed samsung-re-find project: the
    userauth_token obtained during the normal Samsung Account login can authorize the
    *website's own* OAuth client (WEB_FIND_CLIENT_ID) to get a 'code', which is then fed
    into the website's own getState.do/login.do bootstrap sequence to mint a JSESSIONID -
    all without the user ever pasting a cookie by hand. This is diagnostic scaffolding:
    it caches the cookie in memory for this HA run only (not persisted across restarts).
    Returns (jsessionid, csrf) or (None, None) on any failure, with the reason logged.
    The 'session' param (HA's shared session) is only used for the initial 'authorize'
    call against auth_server_url - every smartthingsfind.samsung.com call after that goes
    through a dedicated, isolated client (see _get_web_client) so the actual API calls
    that use jsessionid/csrf never share a connection with HA's other long-lived traffic.
    Fetch that dedicated client with _get_web_client(hass, entry_id) to make those calls.
    """
    data_store = hass.data[DOMAIN][entry_id]
    cache_key = "_web_session_cache"
    cached = data_store.get(cache_key)
    if cached and not force_refresh:
        _record_fmm_request(request_history, "web_session_cache", outcome="reused")
        return cached.get("jsessionid"), cached.get("csrf")

    user_auth_token = data_store.get(CONF_USER_AUTH_TOKEN)
    login_id = data_store.get(CONF_LOGIN_ID)
    auth_server_url = data_store.get(CONF_AUTH_SERVER_URL)
    device_id = data_store.get(CONF_DEVICE_ID) or _get_or_create_device_id(hass)

    if not user_auth_token or not auth_server_url:
        _record_fmm_request(
            request_history, "authorize", outcome="skipped", details={"reason": "missing_auth_data"}
        )
        if not data_store.get("_warned_missing_web_token"):
            data_store["_warned_missing_web_token"] = True
            _LOGGER.warning(
                "Phones, tablets, watches, earbuds and PCs need a Samsung login that stores "
                "the account's master token, which this config entry doesn't have yet "
                "(entries created by older versions of this integration don't). SmartTags keep "
                "working; to enable the other devices, go to Settings > Devices & services > "
                "SmartThings Find > Reconfigure and log in again."
            )
        _LOGGER.debug(
            "[diag] Web session bridge: missing user_auth_token=%s auth_server_url=%s, cannot bootstrap",
            bool(user_auth_token), bool(auth_server_url),
        )
        return None, None
    if not auth_server_url.startswith("http"):
        auth_server_url = f"https://{auth_server_url}"

    web_client = await _get_web_client(hass, entry_id, force_new=True)

    params_auth = {
        "response_type": "code",
        "serviceType": "M",
        "client_id": WEB_FIND_CLIENT_ID,
        "childAccountSupported": "Y",
        "userauth_token": user_auth_token,
        "physical_address_text": device_id,
        "scope": WEB_FIND_SCOPE,
    }
    if login_id:
        params_auth["login_id"] = login_id

    try:
        async with session.get(f"{auth_server_url}/auth/oauth2/v2/authorize", params=params_auth) as res:
            if res.status != 200:
                _record_fmm_request(request_history, "authorize", http_status=res.status, outcome="http_error")
                _LOGGER.debug("[diag] Web session bridge: authorize HTTP %s", res.status)
                return None, None
            auth_data = await res.json(content_type=None)
        _record_fmm_request(
            request_history,
            "authorize",
            http_status=res.status,
            outcome="success" if auth_data.get("code") else "no_authorization_code",
            details={"authorization_code_received": bool(auth_data.get("code"))},
        )

        code = auth_data.get("code")
        if not code and auth_data.get("privacyAccepted") == "N" and "login_id" in params_auth:
            params_auth.pop("login_id", None)
            async with session.get(f"{auth_server_url}/auth/oauth2/v2/authorize", params=params_auth) as res:
                if res.status != 200:
                    _record_fmm_request(request_history, "authorize_retry", http_status=res.status, outcome="http_error")
                    _LOGGER.debug("[diag] Web session bridge: authorize retry HTTP %s", res.status)
                    return None, None
                auth_data = await res.json(content_type=None)
            _record_fmm_request(
                request_history,
                "authorize_retry",
                http_status=res.status,
                outcome="success" if auth_data.get("code") else "no_authorization_code",
                details={"authorization_code_received": bool(auth_data.get("code"))},
            )
            code = auth_data.get("code")
        if not code:
            _record_fmm_request(request_history, "authorize", outcome="no_authorization_code")
            _LOGGER.debug("[diag] Web session bridge: authorize response missing code: %s", auth_data)
            return None, None

        auth_host = auth_server_url.split("://", 1)[-1]

        async with web_client.get(
            "https://smartthingsfind.samsung.com/getState.do",
            params={"payload": "hound"},
        ) as res:
            if res.status != 200:
                _record_fmm_request(request_history, "getState.do", http_status=res.status, outcome="http_error")
                _LOGGER.debug("[diag] Web session bridge: getState.do HTTP %s", res.status)
                return None, None
            state_data = await res.json(content_type=None)
        login_state = state_data.get("state")
        _record_fmm_request(
            request_history,
            "getState.do",
            http_status=res.status,
            outcome="success" if login_state else "missing_state",
            details={"state_received": bool(login_state)},
        )
        if not login_state:
            _LOGGER.debug("[diag] Web session bridge: getState.do missing state: %s", state_data)
            return None, None

        async with web_client.get(
            "https://smartthingsfind.samsung.com/login.do",
            params={
                "auth_server_url": auth_host,
                "api_server_url": auth_host,
                "code": code,
                "code_expires_in": auth_data.get("code_expires_in", 300),
                "state": login_state,
            },
            allow_redirects=True,
        ) as res:
            login_status = res.status
            # Read from this dedicated client's own jar (fresh - nothing else has ever
            # used it), not res.cookies: if login.do 302-redirects internally
            # (allow_redirects=True follows it), the Set-Cookie can land on an
            # intermediate hop that res.cookies (the final response only) never sees,
            # while the jar accumulates Set-Cookie headers across the whole redirect
            # chain correctly.
            jar_cookies = web_client.cookie_jar.filter_cookies("https://smartthingsfind.samsung.com")
            jsessionid = jar_cookies["JSESSIONID"].value if "JSESSIONID" in jar_cookies else None
        _record_fmm_request(
            request_history,
            "login.do",
            http_status=login_status,
            outcome="success" if jsessionid else "missing_session_cookie",
            details={"session_cookie_received": bool(jsessionid)},
        )

        if not jsessionid:
            _LOGGER.debug(
                "[diag] Web session bridge: login.do returned no JSESSIONID (http_status=%s)", login_status
            )
            return None, None

        async with web_client.get(
            "https://smartthingsfind.samsung.com/chkLogin.do",
            cookies={"JSESSIONID": jsessionid},
        ) as res:
            csrf = res.headers.get("_csrf") if res.status == 200 else None
        _record_fmm_request(
            request_history,
            "chkLogin.do",
            http_status=res.status,
            outcome="success" if csrf else "missing_csrf",
            details={"csrf_received": bool(csrf)},
        )
        if not csrf:
            _LOGGER.debug("[diag] Web session bridge: chkLogin.do validation failed")
            return None, None

        data_store[cache_key] = {"jsessionid": jsessionid, "csrf": csrf}
        _LOGGER.debug("[diag] Web session bridge: bootstrap succeeded, csrf present=%s", bool(csrf))

        # Confirmed against samsung-re-find (MIT): it never reuses a dvceID across
        # different login sessions - every operation there re-bootstraps AND re-resolves
        # the device list within that same fresh session before doing anything else.
        # Our web_dvce_id/web_usr_id were otherwise only ever resolved once, at startup,
        # under whatever session existed then - explaining exactly the observed pattern
        # (works until the first time the session gets refreshed, then every device
        # fails with an empty 404 despite the new bootstrap itself succeeding: the
        # dvceID we kept using belonged to a session Samsung no longer recognizes it
        # under). Re-resolve them now, under this session, before anything uses them.
        try:
            all_devices = [d["data"] for d in data_store.get("devices", [])]
            await enrich_devices_with_web_metadata(
                hass, session, entry_id, all_devices, request_history=request_history
            )
        except Exception as e:
            _LOGGER.debug("[diag] Web session bridge: re-enrichment after fresh bootstrap raised: %s", e, exc_info=True)

        return jsessionid, csrf
    except Exception as e:
        _LOGGER.debug("[diag] Web session bridge raised: %s", e, exc_info=True)
        return None, None


STF_BASE_URL = "https://smartthingsfind.samsung.com"

# Ported from https://github.com/1bobby-git/HA-SmartThings-Find (device_tracker.py) - maps
# the website's own deviceTypeCode/subType fields to the icon files it serves itself.
DEVICE_TYPE_ICON_MAP: dict[str, str] = {
    "PHONE": "phone",
    "PHONE DEVICE": "phone",
    "TAB": "tablet",
    "TAB DEVICE": "tablet",
    "PC": "laptop",
    "PC DEVICE": "laptop",
    "SPEN": "spen_pro",
    "VR": "vr",
    "AR": "ar",
}
BUDS_SUBTYPE_ICON_MAP: dict[str, str] = {
    "CANAL": "buds_pair",
    "CANAL2": "attic_pair",
    "CANAL3": "buds3_pair",
    "CANAL4": "buds4_pair",
    "OPEN": "bean_pair",
    "TWS_3RD_PARTY": "tws_pair",
}
WATCH_SUBTYPE_ICON_MAP: dict[str, str] = {
    "WATCH": "watch",
    "FIT": "band",
    "RING": "ring",
}


def _get_website_device_icon_url(website_device: dict) -> str | None:
    """Resolve a device icon URL from a device/getDeviceList.do entry (deviceTypeCode,
    subType, and - for TAG - the icons.coloredIcon field already hosted by Samsung)."""
    device_type = str(website_device.get("deviceTypeCode") or "").upper()
    sub_type = str(website_device.get("subType") or "").upper()
    icons = website_device.get("icons") or {}
    colored_icon = icons.get("coloredIcon")

    if device_type == "TAG":
        if colored_icon:
            if colored_icon.startswith("http"):
                return colored_icon
            if colored_icon.startswith("/"):
                return f"{STF_BASE_URL}{colored_icon}"
        return None
    model_name = str(website_device.get("modelName") or website_device.get("nickName") or "").upper()
    if (
        device_type in {"BUDS", "BUDS DEVICE", "EARBUDS", "HEARABLE", "HEADSET", "TWS"}
        or "BUD" in device_type
        or "HEARABLE" in device_type
        or sub_type in BUDS_SUBTYPE_ICON_MAP
        or "BUD" in sub_type
        or (device_type in {"", "WEARABLE", "ACCESSORY", "AUDIO"} and "BUDS" in model_name)
    ):
        return f"{STF_BASE_URL}/img/device_icon/{BUDS_SUBTYPE_ICON_MAP.get(sub_type, 'buds_pair')}.svg"
    if device_type == "WATCH":
        return f"{STF_BASE_URL}/img/device_icon/{WATCH_SUBTYPE_ICON_MAP.get(sub_type, 'watch')}.svg"
    if device_type == "WEARABLE":
        return f"{STF_BASE_URL}/img/device_icon/{WATCH_SUBTYPE_ICON_MAP.get(sub_type, 'ring')}.svg"
    if device_type in DEVICE_TYPE_ICON_MAP:
        return f"{STF_BASE_URL}/img/device_icon/{DEVICE_TYPE_ICON_MAP[device_type]}.svg"
    return None


async def enrich_devices_with_web_metadata(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    all_devices: list,
    request_history: list | None = None,
) -> None:
    """Cross-reference every device (tags included) against the website's own device
    list (device/getDeviceList.do), matched by name, for two purposes:
      - FMM devices (phone/wearable): resolve 'web_dvce_id'/'web_usr_id', which
        get_device_location() needs to fetch their position via the legacy website API
        (the OAuth SmartThings tracker API has no visibility into these devices).
      - All devices: resolve 'icon_url' from deviceTypeCode/subType (and, for TAG, the
        icons.coloredIcon field), matching what github.com/1bobby-git/HA-SmartThings-Find
        shows - the OAuth /devices listing doesn't carry icons at all.
    Mutates each dict in all_devices in place. Non-fatal: on any failure devices are
    simply left without web ids/icons (FMM devices fall back to 'location not found';
    devices keep whatever icon_url, if any, they already had).
    """
    # Always call getDeviceList.do on the current web session, even if all_devices is
    # empty: Samsung's web backend populates the session's device table during
    # getDeviceList.do, and subsequent setLastSelect.do calls return HTTP 404 unless
    # getDeviceList.do has run in that JSESSIONID.
    jsessionid, csrf = await get_web_session_cookie(hass, session, entry_id)
    if not jsessionid or not csrf:
        _LOGGER.debug("[diag] Skipping web metadata enrichment: no web session available")
        return
    web_client = await _get_web_client(hass, entry_id)

    try:
        async with web_client.post(
            "https://smartthingsfind.samsung.com/device/getDeviceList.do",
            params={"_csrf": csrf},
            data={},
            headers={"Accept": "application/json"},
            cookies={"JSESSIONID": jsessionid},
        ) as res:
            status = res.status
            body = await res.json(content_type=None) if status == 200 else await res.text()
    except Exception as e:
        _LOGGER.debug("[diag] Web getDeviceList.do raised: %s", e, exc_info=True)
        return

    _record_fmm_request(
        request_history,
        "getDeviceList.do",
        http_status=status,
        result_code=body.get("resultCode") if isinstance(body, dict) else None,
        outcome="success" if status == 200 and isinstance(body, dict) else "http_error",
        details={
            "device_count": len(body.get("deviceList", [])) if isinstance(body, dict) else 0,
        },
    )
    if status != 200 or not isinstance(body, dict):
        _LOGGER.debug("[diag] Web getDeviceList.do failed: http_status=%s", status)
        return

    web_devices = body.get("deviceList", [])
    by_name = {}
    for wd in web_devices:
        raw_name = wd.get("modelName") or wd.get("nickName") or ""
        # The website double-HTML-escapes names (e.g. "Emiliano&amp;#39;s Buds"),
        # so unescape twice before comparing.
        wname = html.unescape(html.unescape(str(raw_name))).casefold()
        if wname:
            by_name.setdefault(wname, wd)

    for dev in all_devices:
        name = (dev.get("original_name") or dev.get("name") or "").casefold()
        match = by_name.get(name) or by_name.get((dev.get("name") or "").casefold())
        if not match:
            _LOGGER.debug("[%s] No matching website device found", dev.get("name"))
            continue

        icon_url = _get_website_device_icon_url(match)
        if icon_url:
            dev["icon_url"] = icon_url

        # Needed for the active-location trigger (dm/addOperation.do) regardless of
        # device type - trackers included, not just FMM.
        dev["web_dvce_id"] = match.get("dvceID")
        dev["web_usr_id"] = match.get("usrId")
        # PHONE is the only deviceTypeCode the website itself exposes a Ring button for
        # (confirmed against the real site - PC/watch/buds have no ring UI there either).
        dev["web_device_type_code"] = str(match.get("deviceTypeCode") or "").upper()
        dev["web_sub_type"] = str(match.get("subType") or "").upper()
        dev["is_buds"] = is_buds_device(dev)

        if dev.get("is_fmm"):
            _LOGGER.debug(
                "[%s] Resolved website device id for location lookup: dvceID=%s type=%s subType=%s is_buds=%s icon=%s",
                dev.get("name"),
                match.get("dvceID"),
                dev["web_device_type_code"],
                dev["web_sub_type"],
                dev["is_buds"],
                icon_url,
            )
        else:
            _LOGGER.debug(
                "[%s] Resolved website device id=%s icon=%s", dev.get("name"), match.get("dvceID"), icon_url
            )


async def get_devices(hass: HomeAssistant, session: aiohttp.ClientSession, entry_id: str) -> list:
    """
    Retrieves a list of SmartThings Find devices via the SmartThings installed app API.

    Args:
        hass (HomeAssistant): Home Assistant instance.
        session (aiohttp.ClientSession): The current session.

    Returns:
        list: A list of devices if successful, empty list otherwise.
    """
    try:
        status, response = await _execute_installed_app(
            hass,
            session,
            entry_id,
            "GET",
            "/devices"
        )
        if status != 200:
            _LOGGER.error("Failed to retrieve devices [%s]: %s", status, response)
            return []

        status_code, message, error_code = _parse_installed_apps_response(response)
        if status_code == 401 or error_code == "UnauthorizedError":
            await refresh_iot_token(hass, session, entry_id)
            status, response = await _execute_installed_app(
                hass,
                session,
                entry_id,
                "GET",
                "/devices"
            )
            if status != 200:
                _LOGGER.error("Failed to retrieve devices after refresh [%s]: %s", status, response)
                return []
            status_code, message, error_code = _parse_installed_apps_response(response)
        if status_code != 200 or message is None:
            _LOGGER.error("Device list error [%s/%s]: %s", status_code, error_code, response)
            return []

        devices_data = message.get("devices", [])

    except ConfigEntryAuthFailed:
        raise
    except Exception as e:
        _LOGGER.error(f"Error listing devices: {e}")
        return []
    devices = []
    for device in devices_data:
        device_id = (
            device.get("stDid")
            or device.get("deviceId")
            or device.get("fmmDevId")
            or device.get("id")
        )
        if not device_id:
            continue
        location_type = device.get("locationType") or device.get("deviceType") or ""
        location_type_norm = str(location_type).upper()
        is_tracker = location_type_norm == "TRACKER"
        is_fmm = location_type_norm == "FMM"
        # --- DIAGNOSTIC PATCH step 2: also let FMM devices (phones/wearables) through.
        # They use a different id/name field set than trackers (fmmDevId/fmmDevName
        # instead of stDid/stDevName), so we widen the lookups below accordingly.
        if not is_tracker and not is_fmm:
            _LOGGER.debug(
                "[diag] skipping device with unhandled locationType=%r deviceType=%r raw_keys=%s",
                device.get("locationType"), device.get("deviceType"), list(device.keys()),
            )
            continue
        name = (
            device.get("stDevName")
            or device.get("deviceName")
            or device.get("name")
            or device.get("label")
            or device.get("fmmDevName")
            or device.get("fmmDevModel")
        )
        if isinstance(name, str):
            name = _html_unescape(name)
        if not name:
            if location_type:
                name = f"{location_type} {device_id}"
            else:
                name = device_id or "SmartThings Find"
        icon_url = (
            device.get("iconUrl")
            or device.get("iconURL")
            or device.get("imageUrl")
            or device.get("imgUrl")
        )
        identifier = (DOMAIN, device_id)
        registry = device_registry.async_get(hass)
        ha_dev = get_ha_device(hass, device_id)
        if ha_dev and ha_dev.disabled:
             _LOGGER.debug(
                f"Ignoring disabled device: '{name}' (disabled by {ha_dev.disabled_by})")
             continue
        # Follow renames made in the Samsung app. The previous condition also required
        # the current name to unescape to the new one, so it only ever undid HTML
        # escaping and a real rename never propagated. A name the user set in HA
        # (name_by_user) still wins.
        if ha_dev and not ha_dev.name_by_user and (ha_dev.name or "") != name:
            _LOGGER.info("Renaming device '%s' to '%s'", ha_dev.name, name)
            registry.async_update_device(ha_dev.id, name=name)
        _sync_entity_names(hass, device_id, name)
        name_original = name

        ha_dev_info = DeviceInfo(
            identifiers={identifier},
            manufacturer="Samsung",
            name=name,
            model=location_type or "SmartThings Find",
            # Trackers get a live Google Maps link instead, set on each update by
            # update_device_maps_link().
            configuration_url=None if is_tracker else "https://smartthingsfind.samsung.com/"
        )
        devices += [{
            "data": {
                "device_id": device_id,
                "name": name,
                "original_name": name_original,
                "icon_url": icon_url,
                "location_type": location_type,
                "is_tracker": is_tracker,
                "is_fmm": is_fmm,
                "owner_id": device.get("stOwnerId") or device.get("ownerId"),
                "sa_guid": device.get("saGuid"),
                "fmm_device_id": device.get("fmmDevId"),
                "st_device_id": device.get("stDid") or device.get("deviceId") or device.get("fmmDevId"),
                "share_geolocation": device.get("shareGeolocation"),
                "mutual_agreement": device.get("mutualAgreement"),
                "raw_device": device,
            },
            "ha_dev_info": ha_dev_info
        }]
        _LOGGER.debug(f"Adding device: {name}")

    # Cross-reference every device against the website's own list: FMM devices (phone/
    # wearable) need their website device id to fetch location via device/setLastSelect.do
    # (the OAuth SmartThings tracker API has no visibility into them - see
    # get_device_location); all devices, tags included, pick up a real icon_url this way,
    # since the OAuth /devices listing doesn't carry icons at all.
    all_devices = [d["data"] for d in devices]
    try:
        await enrich_devices_with_web_metadata(hass, session, entry_id, all_devices)
    except Exception as e:
        _LOGGER.debug("[diag] Web metadata enrichment raised at top level: %s", e, exc_info=True)

    return devices


async def perform_fmm_operation(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    dev_data: dict,
    operation: str,
    extra_payload: dict | None = None,
    poll_seconds: int = 20,
    poll_interval: int = 3,
    request_history: list | None = None,
    result_sink: dict | None = None,
) -> tuple[bool, str | None]:
    """Trigger an operation (LOCATION, RING, ...) for a device (tag or FMM) via the
    legacy website API (dm/addOperation.do), then poll dm/getOperationResult.do until
    it completes or poll_seconds elapses.

    Ported from the addOperation/getOperationResult protocol documented by the MIT-licensed
    samsung-re-find project. Best-effort: any failure is logged. Returns
    (success, reject_reason) - reject_reason is the resultCode/oprnResultCode when
    Samsung explicitly rejected or failed the request, useful for surfacing a clear
    error to the user (e.g. via a button press), or None on a plain timeout/no-response.
    """
    if type(operation) is not str or operation not in ALLOWED_FMM_OPERATIONS:
        _LOGGER.error("Refusing disallowed FMM operation: %r", operation)
        return False, "disallowed_operation"
    dev_name = dev_data.get("name") or "SmartThings Find"
    web_dvce_id = dev_data.get("web_dvce_id")
    web_usr_id = dev_data.get("web_usr_id")
    if not web_dvce_id:
        _record_fmm_request(
            request_history, "addOperation.do", outcome="skipped", details={"reason": "missing_web_device_id"}
        )
        _LOGGER.debug("[%s] %s requested but no website device id resolved", dev_name, operation)
        return False, None

    jsessionid, csrf = await get_web_session_cookie(
        hass, session, entry_id, request_history=request_history
    )
    if not jsessionid or not csrf:
        _record_fmm_request(request_history, "addOperation.do", outcome="skipped", details={"reason": "web_session_unavailable"})
        _LOGGER.debug("[%s] %s requested but web session is unavailable", dev_name, operation)
        return False, None
    web_client = await _get_web_client(hass, entry_id)

    payload = {"dvceId": web_dvce_id, "operation": operation, "usrId": web_usr_id}
    if extra_payload:
        payload.update(extra_payload)

    try:
        async with web_client.post(
            "https://smartthingsfind.samsung.com/dm/addOperation.do",
            params={"_csrf": csrf},
            json=payload,
            headers={"Accept": "application/json"},
            cookies={"JSESSIONID": jsessionid},
        ) as res:
            if res.status != 200:
                _record_fmm_request(
                    request_history,
                    "addOperation.do",
                    http_status=res.status,
                    outcome="http_error",
                    details={"operation": operation},
                )
                _LOGGER.debug("[%s] addOperation.do (%s) HTTP %s", dev_name, operation, res.status)
                return False, None
            accepted_data = await res.json(content_type=None)

        _record_fmm_request(
            request_history,
            "addOperation.do",
            http_status=res.status,
            result_code=accepted_data.get("resultCode"),
            outcome="accepted" if accepted_data.get("resultCode") == "00" else "rejected",
            details={"operation": operation, "request_id_received": bool(accepted_data.get("reqId"))},
        )
        if isinstance(result_sink, dict):
            # Samsung itself answered (accepted or rejected), as opposed to a local
            # session/HTTP/exception problem: only Samsung-side outcomes are counted
            # towards the 'unknown position after N failed attempts' option.
            result_sink["samsung_side"] = True
        if accepted_data.get("resultCode") != "00":
            reason = accepted_data.get("resultCode")
            _LOGGER.debug("[%s] %s request rejected: resultCode=%s", dev_name, operation, reason)
            return False, reason
        req_id = accepted_data.get("reqId")
        if not req_id:
            _LOGGER.debug("[%s] %s accepted but no reqId returned", dev_name, operation)
            return False, None

        deadline = hass.loop.time() + max(0, poll_seconds)
        while hass.loop.time() <= deadline:
            await asyncio.sleep(min(poll_interval, max(0, poll_seconds)))
            async with web_client.post(
                "https://smartthingsfind.samsung.com/dm/getOperationResult.do",
                params={"_csrf": csrf},
                json={"dvceId": web_dvce_id, "operation": [operation], "userId": web_usr_id},
                headers={"Accept": "application/json"},
                cookies={"JSESSIONID": jsessionid},
            ) as res:
                if res.status != 200:
                    _record_fmm_request(
                        request_history,
                        "getOperationResult.do",
                        http_status=res.status,
                        outcome="http_error",
                        details={"operation": operation},
                    )
                    continue
                result_data = await res.json(content_type=None)
            candidates = [
                op for op in result_data.get("operation", [])
                if op.get("oprnType") == operation and str(op.get("reqId")) == str(req_id)
            ]
            if candidates:
                latest = candidates[-1]
                op_state = classify_fmm_operation_result(latest, operation)
                if isinstance(result_sink, dict):
                    result_sink["operation"] = latest
                    result_sink["operation_state"] = op_state
                _record_fmm_request(
                    request_history,
                    "getOperationResult.do",
                    http_status=res.status,
                    result_code=latest.get("oprnResultCode"),
                    outcome="completed" if op_state in {"success", "failed"} else "pending",
                    details={
                        "operation": operation,
                        "operation_status": latest.get("oprnStsCd"),
                        "operation_result": op_state,
                    },
                )
                if op_state in {"success", "failed"}:
                    success = op_state == "success"
                    reject_code = latest.get("oprnResultCode") or latest.get("oprnStsCd")
                    _LOGGER.debug(
                        "[%s] %s %s (oprnStsCd=%s oprnResultCode=%s)",
                        dev_name, operation, "succeeded" if success else "failed",
                        latest.get("oprnStsCd"), latest.get("oprnResultCode"),
                    )
                    return success, (None if success else str(reject_code) if reject_code is not None else None)
            else:
                _record_fmm_request(
                    request_history,
                    "getOperationResult.do",
                    http_status=res.status,
                    outcome="no_matching_operation",
                    details={"operation": operation},
                )
        _LOGGER.debug("[%s] %s request timed out after %ss", dev_name, operation, poll_seconds)
        _record_fmm_request(
            request_history, "getOperationResult.do", outcome="timeout", details={"operation": operation}
        )
        return False, None
    except Exception as e:
        _record_fmm_request(
            request_history,
            "fmm_operation",
            outcome="exception",
            details={"exception_type": type(e).__name__, "operation": operation},
        )
        _LOGGER.debug("[%s] %s request raised: %s", dev_name, operation, e, exc_info=True)
        return False, None


# ---------------------------------------------------------------------------------------------
# Device classes and connection state (Find My Mobile devices)
# ---------------------------------------------------------------------------------------------
PC_DEVICE_TYPES = {"PC", "PC DEVICE", "WINDOWS_PC", "PERSONAL_COMPUTER"}


def _device_type_code(dev_data: dict) -> str:
    return str(dev_data.get("web_device_type_code") or "").upper()


def is_pc_device(dev_data: dict) -> bool:
    code = _device_type_code(dev_data)
    return bool(
        dev_data.get("is_fmm")
        and (code in PC_DEVICE_TYPES or "COMPUTER" in code or "LAPTOP" in code)
    )


def is_phone_device(dev_data: dict) -> bool:
    """Phones and tablets: both can be reached actively and rung (`TAB` / `TABLET`)."""
    code = _device_type_code(dev_data)
    return bool(dev_data.get("is_fmm") and ("PHONE" in code or "TAB" in code))


def is_watch_device(dev_data: dict) -> bool:
    if not dev_data.get("is_fmm"):
        return False
    code = _device_type_code(dev_data)
    sub_type = str(dev_data.get("web_sub_type") or "").upper()
    icon = str(dev_data.get("icon_url") or "").lower()
    return "WATCH" in code or "WATCH" in sub_type or icon.endswith("/watch.svg")


def is_buds_device(dev_data: dict) -> bool:
    if not dev_data.get("is_fmm"):
        return False
    if dev_data.get("is_buds"):
        return True
    code = _device_type_code(dev_data)
    sub_type = str(dev_data.get("web_sub_type") or "").upper()
    icon = str(dev_data.get("icon_url") or "").lower()
    raw = dev_data.get("raw_device") if isinstance(dev_data.get("raw_device"), dict) else {}
    names = " ".join(
        str(v or "")
        for v in (
            dev_data.get("original_name"),
            dev_data.get("name"),
            raw.get("fmmDevName"),
            raw.get("fmmDevModel"),
            raw.get("deviceName"),
            raw.get("modelName"),
        )
    ).upper()
    return bool(
        code in {"BUDS", "BUDS DEVICE", "EARBUDS", "HEARABLE", "HEADSET", "TWS"}
        or "BUD" in code
        or "HEARABLE" in code
        or sub_type in BUDS_SUBTYPE_ICON_MAP
        or "BUD" in sub_type
        or "CANAL" in sub_type
        or any(tok in icon for tok in ("/buds", "/attic_pair", "/bean_pair", "/tws_pair"))
        or "BUDS" in names
        or "EARBUDS" in names
    )


def is_active_connectable_device(dev_data: dict) -> bool:
    """Devices with a network connection of their own that Samsung can actually reach
    (phones, tablets, PCs). Passive FMM devices (Buds, watches) and tags are excluded: they
    are routinely offline when away from their phone, so their last position always counts."""
    return is_phone_device(dev_data) or is_pc_device(dev_data)


def is_direction_supported_device(dev_data: dict) -> bool:
    """Devices eligible for the Proximity / Direction sensor:
    - SmartTags (`is_tracker`): always active.
    - Active-connectable FMM devices (`is_active_connectable_device`: phones, tablets, PCs):
      created, but active only while `Active Location` is on (otherwise `unknown`).
    - Passive FMM devices (Buds, watches): not eligible (entity is not created).
    """
    return bool(dev_data.get("is_tracker") or is_active_connectable_device(dev_data))


# Sanitized-structure helpers. Samsung's FMM payloads are undocumented; to see what the server
# actually exposes (and for future features) we log/expose the *shape* of its answers: every key
# with its value type, and the value itself only for short, status-like fields that cannot
# identify a person, a place, or a session.
_STRUCT_BLOCKED_KEY = re.compile(
    r"lat|lon|coord|(?<!firstshown)loc(?!ationtype)|addr|ssid|ip$|^ip|mac|imei|phone|number|name|"
    r"mail|token|id$|^id|user|usr|session|csrf|cookie|key|uncertain|serial|msisdn|iccid|imsi",
    re.I,
)
_STRUCT_VALUE_KEY = re.compile(
    r"conn|net|online|offline|wifi|wi_fi|cell|mobile|state|status|type|result|code|sts|support|"
    r"mode|ring|cdma|tel|only|remotecontrol|config|paired|encrypted|firstshownloc|flag|"
    r"battery|left|right",
    re.I,
)


def sanitize_structure(value, depth: int = 0):
    """Coordinate- and identity-free description of a JSON value (see comment above)."""
    if isinstance(value, dict):
        if depth >= 4:
            return f"dict[{len(value)}]"
        out = {}
        for key, item in list(value.items())[:60]:
            key = str(key)
            if isinstance(item, (dict, list)):
                out[key] = sanitize_structure(item, depth + 1)
            elif item is None:
                out[key] = None
            elif _STRUCT_BLOCKED_KEY.search(key):
                out[key] = type(item).__name__
            elif isinstance(item, (bool, int, float)):
                out[key] = item if _STRUCT_VALUE_KEY.search(key) else type(item).__name__
            elif isinstance(item, str):
                if _STRUCT_VALUE_KEY.search(key) and 0 < len(item) <= 32:
                    out[key] = item
                else:
                    out[key] = "str" if item else "empty-str"
            else:
                out[key] = type(item).__name__
        return out
    if isinstance(value, list):
        if depth >= 4:
            return f"list[{len(value)}]"
        return [sanitize_structure(item, depth + 1) for item in value[:5]]
    return type(value).__name__


_TRUE_VALUES = {"y", "yes", "true", "1", "on", "online", "connected"}
_FALSE_VALUES = {"n", "no", "false", "0", "off", "offline", "disconnected", "none"}
_MOBILE_HINTS = ("mobile", "cell", "lte", "3g", "4g", "5g", "data", "gsm", "umts")


def parse_connection_state(
    op: dict | None, fallback_op: dict | None = None
) -> tuple[str, str | None]:
    """Read a CHECK_CONNECTION operation (and optionally the accompanying LOCATION operation's
    extra.seData.netType) to return ('wifi'|'mobile'|'online'|'offline'|'unknown', field_name).

    Confirmed from live S23 Ultra & Windows PC payloads:
      - CHECK_CONNECTION carries `extra.isConnected` (bool) and `oprnStsCd=='2800'` +
        `oprnResultCode=='1200'` when the device is reachable online;
      - LOCATION carries `extra.seData.netType` ('wifi' / 'mobile' / ...) and `extra.seData.wifi`.
    Anything unreadable returns 'unknown' - never 'offline' by default."""
    if not isinstance(op, dict):
        return "unknown", None
    found: list[tuple[str, str]] = []

    def walk(data: dict, depth: int = 0) -> None:
        for key, value in data.items():
            if isinstance(value, dict):
                if depth < 2:
                    walk(value, depth + 1)
                continue
            if isinstance(value, (list, tuple)) or value is None:
                continue
            lower = str(key).lower()
            if "only" in lower or _STRUCT_BLOCKED_KEY.search(str(key)) and "net" not in lower:
                continue
            text = str(value).strip().lower()
            if "wifi" in lower or "wi-fi" in lower or "wlan" in lower:
                if text in _TRUE_VALUES:
                    found.append(("wifi", str(key)))
                elif text not in _FALSE_VALUES and "wifi" in text.replace("-", ""):
                    found.append(("wifi", str(key)))
            elif any(hint in lower for hint in ("mobile", "cell", "lte")):
                if text in _TRUE_VALUES:
                    found.append(("mobile", str(key)))
            elif "online" in lower or "connected" in lower:
                if text in _TRUE_VALUES:
                    found.append(("online", str(key)))
                elif text in _FALSE_VALUES:
                    found.append(("offline", str(key)))
            elif "conn" in lower or "net" in lower:
                compact = text.replace("-", "").replace("_", "")
                if "wifi" in compact or "wlan" in compact:
                    found.append(("wifi", str(key)))
                elif any(hint in compact for hint in _MOBILE_HINTS):
                    found.append(("mobile", str(key)))
                elif text in _FALSE_VALUES:
                    found.append(("offline", str(key)))
                elif text in _TRUE_VALUES:
                    found.append(("online", str(key)))

    walk(op)
    for wanted in ("wifi", "mobile", "offline"):
        for state, source in found:
            if state == wanted:
                return state, source
    # Determine whether the device is online from explicit flags (`extra.isConnected`) or
    # from a completed CHECK_CONNECTION (`oprnStsCd == '2800'`).
    online_source = next((src for st, src in found if st == "online"), None)
    if online_source is None and op.get("oprnType") == "CHECK_CONNECTION" and str(op.get("oprnStsCd") or "") == "2800":
        result_code = str(op.get("oprnResultCode") or "")
        if result_code == "1200":
            online_source = "oprnResultCode"
        elif result_code and result_code not in {"00", "200"}:
            return "offline", "oprnResultCode"
    if online_source is not None:
        # When the device is online, check if a companion operation (e.g. LOCATION's
        # `extra.seData.netType`) specifies whether it is on Wi-Fi or mobile data.
        if isinstance(fallback_op, dict):
            found.clear()
            walk(fallback_op)
            for wanted in ("wifi", "mobile"):
                for state, source in found:
                    if state == wanted:
                        return state, f"{online_source}+{source}"
        return "online", online_source
    return "unknown", None


async def check_fmm_connection(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    dev_data: dict,
    *,
    use_cache: bool = True,
) -> dict:
    """Ask Samsung whether an FMM device is connected right now (CHECK_CONNECTION, same
    addOperation/getOperationResult flow and website session as every other operation).

    Never raises for a failed check: the state is then 'unknown'. The result is kept in
    dev_data['_connection'] (shared by the Connection sensor and the Ring pre-check) together
    with the sanitized structure of Samsung's answer."""
    now = datetime.now(pytz.UTC)
    cached = dev_data.get("_connection")
    if (
        use_cache
        and isinstance(cached, dict)
        and cached.get("checked_at")
        and (now - cached["checked_at"]).total_seconds() < CONNECTION_CACHE_SECONDS
    ):
        return cached
    dev_name = dev_data.get("name") or dev_data.get("device_id")
    history = dev_data.setdefault("_fmm_request_history", [])
    sink: dict = {}
    success, reason = False, None
    try:
        success, reason = await perform_fmm_operation(
            hass, session, entry_id, dev_data, "CHECK_CONNECTION",
            poll_seconds=FMM_CONNECTION_POLL_SECONDS,
            request_history=history,
            result_sink=sink,
        )
    except ConfigEntryAuthFailed:
        raise
    except Exception:
        _LOGGER.debug("[%s] CHECK_CONNECTION raised", dev_name, exc_info=True)
    op = sink.get("operation")
    state, source = parse_connection_state(op)
    if state == "unknown" and sink.get("samsung_side"):
        # Samsung accepted our live CHECK_CONNECTION request for this device: if it completed
        # with 1200 the device answered (online); if it timed out or finished with a non-success
        # code, the device did not answer the connection check (offline).
        if success:
            state, source = "online", "operation_result"
        elif reason != "501":
            state, source = "offline", "operation_result"
    if state == "online" and is_phone_device(dev_data):
        # On Samsung mobile devices (phones/tablets), an online CHECK_CONNECTION without
        # Wi-Fi netType indicates a cellular mobile data connection.
        state = "mobile"
    structure = sanitize_structure(op) if isinstance(op, dict) else None
    if isinstance(op, dict):
        live_batt = extract_fmm_battery([op])
        last_batt = dev_data.setdefault("_last_battery", {})
        for b_key, b_val in live_batt.items():
            if b_val is not None:
                last_batt[b_key] = b_val
    result = {
        "state": state,
        "source_field": source,
        "checked_at": now,
        "success": bool(success),
        "result_code": reason,
        "samsung_answered": bool(sink.get("samsung_side")),
        "structure": structure,
    }
    dev_data["_connection"] = result
    _record_fmm_request(
        history, "connection_check", outcome=state,
        details={"source_field": source, "operation_returned": isinstance(op, dict)},
    )
    signature = json.dumps(structure, sort_keys=True, default=str)
    if dev_data.get("_connection_structure_logged") != signature:
        dev_data["_connection_structure_logged"] = signature
        _LOGGER.info(
            "[%s] CHECK_CONNECTION result: state=%s (field=%s, success=%s, code=%s). "
            "Sanitized structure of Samsung's answer: %s",
            dev_name, state, source, success, reason, structure,
        )
    return result


def register_position_fix(dev_data: dict, tag_data: dict, now: datetime | None = None) -> bool:
    """Track whether this read brought a genuinely newer fix (its gps_date is strictly later
    than the newest one ever seen for this device). Only then dev_data['_last_new_fix_at'] is
    set to the current time; an unchanged fix, a 501, or the first read after a restart that
    returns the position restored from disk never counts. Returns True for a new fix."""
    loc = tag_data.get("used_loc") or {}
    gps_date = loc.get("gps_date")
    if not (
        tag_data.get("update_success")
        and tag_data.get("location_found")
        and isinstance(gps_date, datetime)
    ):
        return False
    if gps_date.tzinfo is None:
        gps_date = gps_date.replace(tzinfo=pytz.UTC)
    known = dev_data.get("_known_gps_date")
    if known is not None and gps_date <= known:
        return False
    dev_data["_known_gps_date"] = gps_date
    dev_data["_last_new_fix_at"] = now or datetime.now(pytz.UTC)
    return True


async def ring_fmm_device(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    dev_data: dict,
) -> tuple[bool, str | None]:
    """Make an FMM device (phone/tablet/watch/buds) ring, via the same website RING
    operation used by the official Find My Mobile app/site."""
    return await perform_fmm_operation(
        hass, session, entry_id, dev_data, "RING", extra_payload={"status": "start"}
    )


async def stop_ring_fmm_device(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    dev_data: dict,
) -> tuple[bool, str | None]:
    """Stop an FMM device from ringing."""
    return await perform_fmm_operation(
        hass, session, entry_id, dev_data, "RING", extra_payload={"status": "stop"}
    )


async def get_fmm_device_location(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    dev_data: dict,
    entry_id: str,
    force_active: bool = False,
) -> dict:
    """Fetch location for an FMM device (phone/wearable) via the legacy smartthingsfind.samsung.com
    website API, using the master-token web-session bridge (get_web_session_cookie). The OAuth
    SmartThings tracker API has no visibility into these devices - see the investigation notes
    around get_device_location. Returns the same shape as the tracker path so callers don't need
    to special-case it.
    """
    dev_id = dev_data.get('device_id')
    dev_name = dev_data.get('name') or dev_id or "SmartThings Find"
    web_dvce_id = dev_data.get("web_dvce_id")
    request_history = dev_data.setdefault("_fmm_request_history", [])

    fail = {
        "dev_name": dev_name,
        "dev_id": dev_id,
        "update_success": False,
        "location_found": False,
        "fetch_error": None,
        "position_error": None,
        "fmm_request_history": request_history,
    }

    if not web_dvce_id:
        # Not an error - the name-matching enrichment step just hasn't resolved this
        # device yet (or the web bridge is unavailable this cycle).
        _LOGGER.debug("[%s] No website device id resolved yet, cannot fetch FMM location", dev_name)
        _record_fmm_request(
            request_history, "setLastSelect.do", outcome="skipped", details={"reason": "missing_web_device_id"}
        )
        return {**fail, "update_success": True, "fetch_error": "Website device ID is not resolved"}

    active_location_error = None
    active_location_attempted = False
    active_sink: dict = {}
    if force_active or dev_data.get("active_location_requested"):
        # active_location_requested is set live by ActiveLocationSwitch (switch.py).
        # An enabled switch opts this device into active retrieval on every poll,
        # including Buds and watches; prior 501 responses and transient failures do not
        # suppress later retries.
        active_location_attempted = True
        # Run CHECK_CONNECTION for every opted-in FMM device (or manual Update Location):
        # for phones/tablets/PCs it feeds the Connection sensor and battery; for Buds and
        # watches it is the operation that populates `extra.left.battery` / `extra.right.battery`
        # and `battery` on Samsung's backend (per charlesbel/samsung-re-find PR #2), whereas
        # `LOCATION` on Buds is rejected with 501. Never sent when the switch is off.
        await check_fmm_connection(
            hass, session, entry_id, dev_data, use_cache=not force_active
        )
        active_sink = {}
        active_success, active_reason = await perform_fmm_operation(
            hass,
            session,
            entry_id,
            dev_data,
            "LOCATION",
            poll_seconds=FMM_LOCATION_POLL_SECONDS,
            request_history=request_history,
            result_sink=active_sink,
        )
        if active_success:
            # A deliberate manual retry can prove that an operation previously
            # marked unsupported is now accepted again by Samsung.
            dev_data.pop('_active_location_unsupported', None)
        active_location_error = None if active_success else (
            f"Samsung active location request failed (resultCode={active_reason})"
            if active_reason else "Samsung did not complete the active location request (timeout or no response)"
        )
        if not active_success and active_reason == "501":
            dev_data['_active_location_unsupported'] = True
            _LOGGER.info(
                "[%s] Samsung rejected this active location request as unsupported; "
                "the next opted-in poll will retry",
                dev_name,
            )
        # Whether or not it succeeded/timed out, fall through to the normal passive read
        # below - extract_best_location() picks the newest fix regardless of how the
        # freshest one got there, and a passive read still returns the best data we have.
    else:
        skip_reason = "active_location_switch_off"
        _record_fmm_request(
            request_history,
            "active_location",
            outcome="skipped",
            details={"reason": skip_reason, "manual_request": force_active},
        )
    fail["active_location_error"] = active_location_error

    async def _fetch(force_refresh: bool):
        jsessionid, csrf = await get_web_session_cookie(
            hass,
            session,
            entry_id,
            force_refresh=force_refresh,
            request_history=request_history,
        )
        if not jsessionid or not csrf:
            _record_fmm_request(
                request_history,
                "setLastSelect.do",
                outcome="skipped",
                details={"reason": "web_session_unavailable", "force_refresh": force_refresh},
            )
            return None, None
        current_web_dvce_id = dev_data.get("web_dvce_id") or web_dvce_id
        device_id_refreshed = current_web_dvce_id != web_dvce_id
        web_client = await _get_web_client(hass, entry_id)
        async with web_client.post(
            "https://smartthingsfind.samsung.com/device/setLastSelect.do",
            params={"_csrf": csrf},
            json={"dvceId": current_web_dvce_id, "removeDevice": []},
            headers={"Accept": "application/json"},
            cookies={"JSESSIONID": jsessionid},
        ) as res:
            status = res.status
            if status == 200:
                body = await res.json(content_type=None)
            else:
                body = None
        _record_fmm_request(
            request_history,
            "setLastSelect.do",
            http_status=status,
            outcome="success" if status == 200 and isinstance(body, dict) else "http_error",
            details={
                "force_refresh": force_refresh,
                "device_id_refreshed": device_id_refreshed,
                "refresh_after_active_location": active_location_attempted and force_refresh,
            },
            response=body if isinstance(body, dict) else None,
        )
        return status, body

    try:
        session_was_cached = bool(
            hass.data.get(DOMAIN, {}).get(entry_id, {}).get("_web_session_cache")
        )
        # Match the documented Find web flow: after an active LOCATION operation, create
        # a fresh website session before reading the resulting location. An explicit
        # manual press also gets a fresh passive read if active LOCATION was previously
        # marked unsupported for this device.
        status, body = await _fetch(active_location_attempted or force_active)
        if status != 200 or body is None:
            # The cached web session may have gone stale - bootstrap a fresh one and retry once.
            status, body = await _fetch(True)
        if status != 200 or body is None:
            _LOGGER.warning(
                "[%s] FMM location fetch failed (http_status=%s)",
                dev_name, status,
            )
            reason = "Samsung Find web session is unavailable" if status is None else f"HTTP status {status}"
            return {**fail, "fetch_error": f"Samsung Find location request failed: {reason}"}
        if not isinstance(body, dict):
            _LOGGER.warning(
                "[%s] FMM location fetch returned an unexpected payload (%s)", dev_name, type(body).__name__)
            return {**fail, "fetch_error": f"Unexpected Samsung response format: {type(body).__name__}"}
        if body.get("resultCode") not in (None, "00"):
            _LOGGER.warning("[%s] FMM location fetch rejected: resultCode=%s", dev_name, body.get("resultCode"))
            return {**fail, "fetch_error": f"Samsung rejected the location request (resultCode={body.get('resultCode')})"}

        # From here on the read itself has worked. What's in the answer is a separate
        # question: an entry we can't make sense of must never turn the device Unavailable
        # (update_success is about reaching Samsung, not about how clean its data is).
        operations = body.get("operation")
        if not isinstance(operations, list):
            _LOGGER.warning("[%s] Samsung location response has no operation list", dev_name)
            operations = []
        location_diagnostics = {}
        try:
            used_op, used_loc = extract_best_location(operations, dev_name, location_diagnostics)
        except Exception:
            # extract_best_location is written not to raise. If it ever does, that's a gap
            # in how we read Samsung's data, so say so once and carry on with what we knew.
            if not dev_data.get('_parse_error_logged'):
                dev_data['_parse_error_logged'] = True
                _LOGGER.warning(
                    "[%s] Couldn't interpret the location data Samsung returned, showing the last "
                    "known position instead", dev_name, exc_info=True)
            used_op, used_loc = None, None
            location_diagnostics["parser_error"] = True

        previous_good = dev_data.get('_last_good_loc')
        previous_gps_date = (
            (previous_good[1] or {}).get('gps_date')
            if isinstance(previous_good, tuple) and len(previous_good) == 2
            else None
        )
        current_gps_date = (used_loc or {}).get('gps_date')
        stale_after_seconds = max(
            int(dev_data.get('_poll_interval_s') or CONF_UPDATE_INTERVAL_DEFAULT) * 3,
            1800,
        )
        is_old_position = bool(
            current_gps_date
            and (datetime.now(pytz.UTC) - current_gps_date).total_seconds() > stale_after_seconds
        )
        no_new_position = bool(
            used_loc is None
            or (previous_gps_date and current_gps_date and current_gps_date <= previous_gps_date)
            or is_old_position
        )

        # A successful but stale passive response can be a stale website-session view,
        # unlike an HTTP error which is already retried above. Try one fresh-session
        # passive read per device runtime; never send an active LOCATION operation here.
        # The explicit Update Location button remains the user's way to force another try.
        if (
            not active_location_attempted
            and not force_active
            and session_was_cached
            and no_new_position
            and not dev_data.get("_passive_session_retry_done")
        ):
            retry_reason = (
                "no_readable_position" if used_loc is None
                else "position_unchanged" if previous_gps_date and current_gps_date <= previous_gps_date
                else "position_is_stale"
            )
            dev_data["_passive_session_retry_done"] = True
            _record_fmm_request(
                request_history,
                "passive_session_refresh",
                outcome="attempted",
                details={"reason": retry_reason, "active_request_sent": False},
            )
            retry_status, retry_body = await _fetch(True)
            if (
                retry_status == 200
                and isinstance(retry_body, dict)
                and retry_body.get("resultCode") in (None, "00")
            ):
                retry_operations = retry_body.get("operation")
                if not isinstance(retry_operations, list):
                    retry_operations = []
                retry_diagnostics = {}
                retry_op, retry_loc = extract_best_location(
                    retry_operations, dev_name, retry_diagnostics
                )
                retry_date = (retry_loc or {}).get("gps_date")
                first_date = (used_loc or {}).get("gps_date")
                if retry_loc and (not used_loc or (retry_date and first_date and retry_date > first_date)):
                    body = retry_body
                    operations = retry_operations
                    used_op, used_loc = retry_op, retry_loc
                    location_diagnostics = retry_diagnostics
                    current_gps_date = retry_date
                    _record_fmm_request(
                        request_history,
                        "passive_session_refresh",
                        outcome="position_available",
                        details={"position_time": retry_date.isoformat() if retry_date else None},
                    )
                else:
                    # Prefer a readable location from the fresh response only when it
                    # is newer. If refresh returns no coordinates, keep the usable
                    # location from the initial passive response rather than erasing it.
                    if not used_loc or (
                        retry_loc
                        and retry_date
                        and first_date
                        and retry_date > first_date
                    ):
                        body = retry_body
                        operations = retry_operations
                        used_op, used_loc = retry_op, retry_loc
                        location_diagnostics = retry_diagnostics
                        current_gps_date = retry_date
                    _record_fmm_request(
                        request_history,
                        "passive_session_refresh",
                        outcome="no_new_position",
                        details={"position_time": retry_date.isoformat() if retry_date else None},
                    )
            else:
                _record_fmm_request(
                    request_history,
                    "passive_session_refresh",
                    http_status=retry_status,
                    outcome="refresh_read_failed",
                    details={"active_request_sent": False},
                )

        # Samsung can return a valid but older LOCATION entry even when a newer
        # fix was already read in a previous cycle. Keep that response in the
        # request diagnostics, but never let it replace the last known good fix.
        if (
            used_loc
            and previous_good
            and previous_gps_date
            and current_gps_date
            and current_gps_date <= previous_gps_date
        ):
            used_op, used_loc = previous_good

        position_error = None
        if not used_loc:
            if location_diagnostics.get("parser_error"):
                position_error = "The integration could not interpret Samsung's location data"
            elif location_diagnostics.get("encrypted"):
                position_error = "Samsung returned only end-to-end encrypted locations; this integration cannot decode them"
            elif location_diagnostics.get("skipped"):
                position_error = "Samsung returned location entries without usable coordinates or timestamps"
            else:
                position_error = "Samsung returned no readable location for this device"
            if active_location_attempted and active_location_error is None:
                active_location_error = (
                    "Samsung completed the active location request but returned no readable coordinates"
                )

        gps_accuracy = (used_loc or {}).get('gps_accuracy')
        # Windows PCs rely on Wi-Fi/IP geolocation and routinely report ~200-500 m accuracy,
        # so do not flag low-accuracy warnings for PC devices.
        low_accuracy = (
            not is_pc_device(dev_data)
            and gps_accuracy is not None
            and gps_accuracy > LOW_ACCURACY_THRESHOLD_M
        )
        if low_accuracy:
            position_error = (
                f"Samsung returned a position with low accuracy ({gps_accuracy:.1f} m; "
                f"warning threshold {LOW_ACCURACY_THRESHOLD_M} m)"
            )

        current_gps_date = (used_loc or {}).get('gps_date')
        received_new_fix = bool(used_loc)
        if received_new_fix and previous_gps_date:
            received_new_fix = current_gps_date > previous_gps_date
        if request_history is not None:
            response_has_coordinates = any(
                isinstance(op, dict) and (
                    (op.get('latitude') is not None and op.get('longitude') is not None)
                    or (
                        isinstance(op.get('encLocation'), dict)
                        and op['encLocation'].get('latitude') is not None
                        and op['encLocation'].get('longitude') is not None
                    )
                )
                for op in operations
            )
            operation_type_counts = {}
            for op in operations:
                if isinstance(op, dict):
                    op_type = str(op.get("oprnType") or "UNKNOWN")
                    operation_type_counts[op_type] = operation_type_counts.get(op_type, 0) + 1
            location_entries = [
                _summarize_location_operation(op)
                for op in operations
                if isinstance(op, dict) and op.get("oprnType") in LOCATION_OP_TYPES
            ][:8]
            parser_diagnostics = {
                key: sorted(value) if isinstance(value, set) else value
                for key, value in location_diagnostics.items()
            }
            _record_fmm_request(
                request_history,
                "location_result",
                outcome=(
                    "low_accuracy" if low_accuracy
                    else "position_updated" if received_new_fix
                    else "no_new_fix"
                ),
                details={
                    "position_time": current_gps_date.isoformat() if current_gps_date else None,
                    "position_age_seconds": max(
                        0, int((datetime.now(pytz.UTC) - current_gps_date).total_seconds())
                    ) if current_gps_date else None,
                    "response_contains_coordinates": response_has_coordinates,
                    "operation_count": len(operations),
                    "operation_type_counts": operation_type_counts,
                    "location_entries": location_entries,
                    "parser_diagnostics": parser_diagnostics,
                    "selected_operation": used_op.get("oprnType") if isinstance(used_op, dict) else None,
                    "gps_accuracy_m": gps_accuracy,
                    "accuracy_warning_threshold_m": LOW_ACCURACY_THRESHOLD_M,
                    "position_updated": received_new_fix,
                },
            )

        if used_loc:
            dev_data['_last_good_loc'] = (used_op, used_loc)
        elif dev_data.get('_last_good_loc'):
            # Nothing usable in this answer (an entry we can't read, or Samsung's history no
            # longer holds a position). Keep showing the last position we did have - its own
            # age is what flags it as stale below - rather than blanking the entity.
            used_op, used_loc = dev_data['_last_good_loc']
            _LOGGER.debug("[%s] No usable position in this read, keeping the last known one", dev_name)

        _LOGGER.debug(
            "[%s] Passive read newest fix timestamp: gps_date=%s (compare across cycles to tell whether "
            "the server actually has fresher data, vs. the active push just not waking the device)",
            dev_name, (used_loc or {}).get("gps_date"),
        )
        _LOGGER.debug(
            "[%s] FMM passive response summary: operations=%s location_entries=%s parser=%s selected=%s",
            dev_name,
            operation_type_counts if request_history is not None else len(operations),
            location_entries if request_history is not None else "not recorded",
            parser_diagnostics if request_history is not None else location_diagnostics,
            used_op.get("oprnType") if isinstance(used_op, dict) else None,
        )

        # Extract battery level (and Left/Right earbud channels for Galaxy Buds, per
        # charlesbel/samsung-re-find PR #2: extra.left.battery / extra.right.battery).
        # For active-connectable devices (smartphones, tablets, PCs), passive reads do not
        # refresh the battery on Samsung's backend, so battery is only updated/exposed when
        # active reading is enabled (`active_location_requested` / `active_location_attempted`);
        # without active reading, their battery sensor stays `unknown` (None).
        # For passive FMM devices (watches, Buds), the last known battery is always preserved.
        last_battery = dev_data.setdefault("_last_battery", {})
        active_connectable = is_active_connectable_device(dev_data)
        if not active_connectable or active_location_attempted:
            extracted_battery = extract_fmm_battery(operations)
            for b_key, b_val in extracted_battery.items():
                if b_val is not None:
                    last_battery[b_key] = b_val
        expose_battery = (not active_connectable) or bool(
            dev_data.get("active_location_requested") or active_location_attempted
        )

        # A device that can't be actively woken up (no network of its own - buds, most
        # watches, PCs when off/asleep) can only ever show whatever it last reported on
        # its own, which can be hours or days old with nothing wrong going on. Flag that
        # clearly instead of presenting it identically to a device that just updated,
        # so a stale position is never mistaken for a fresh one. "Old" is judged against
        # this device's own polling interval (set by the coordinator), as three polls.
        active_location_supported = not dev_data.get('_active_location_unsupported')
        gps_date = (used_loc or {}).get("gps_date")
        position_stale = False
        if gps_date:
            interval = dev_data.get('_poll_interval_s') or CONF_UPDATE_INTERVAL_DEFAULT
            stale_after = timedelta(seconds=max(interval * 3, 1800))
            position_stale = (datetime.now(pytz.UTC) - gps_date) > stale_after

        # Extra diagnostic info the website's setLastSelect.do already includes in the
        # same response - no extra request needed. Field semantics beyond their literal
        # names aren't documented anywhere (undocumented API), so these are passed
        # through close to raw rather than reinterpreted/relabeled.
        last_selected = body.get("lastSelectedDevice")
        if not isinstance(last_selected, dict):
            last_selected = {}
        menu_flat = {}
        for entry in body.get("menu") or []:
            if isinstance(entry, dict):
                menu_flat.update(entry)
        web_capabilities = {
            "telephony_support": last_selected.get("telsupport") == "Y",
            "wifi_only": last_selected.get("wifiOnly") == "Y",
            "cdma": last_selected.get("cdma") == "Y",
            "ring_supported": menu_flat.get("ring") == "Y",
            "offline_find_supported": menu_flat.get("offlineFind") == "Y",
        }

        # What the server actually tells us, as sanitized shapes (no coordinates, ids or
        # session data): kept on the Connection sensor and logged whenever it changes, so
        # new fields Samsung adds can be spotted and used later.
        server_structure = {
            "last_selected_device": sanitize_structure(last_selected),
            "menu": sanitize_structure(body.get("menu")),
            "operations": {},
        }
        passive_check_op = None
        passive_loc_op = None
        for op in operations:
            if isinstance(op, dict):
                op_key = str(op.get("oprnType") or "UNKNOWN")
                if op_key == "CHECK_CONNECTION":
                    passive_check_op = op
                elif op_key == "LOCATION":
                    passive_loc_op = op
                if op_key not in server_structure["operations"] and len(server_structure["operations"]) < 12:
                    server_structure["operations"][op_key] = sanitize_structure(op)
        existing_conn = dev_data.get("_connection")
        if passive_check_op is not None:
            p_state, p_source = parse_connection_state(passive_check_op, fallback_op=passive_loc_op or used_op)
            if p_state == "online" and is_phone_device(dev_data):
                p_state = "mobile"
            p_done = _parse_stf_date_safe(
                passive_check_op.get("oprnDoneDate") or passive_check_op.get("oprnCrtDate")
            )
            if not isinstance(existing_conn, dict) or existing_conn.get("state") in (None, "unknown"):
                dev_data["_connection"] = {
                    "state": p_state,
                    "source_field": f"passive:{p_source}" if p_source else None,
                    "checked_at": p_done,
                    "success": classify_fmm_operation_result(passive_check_op, "CHECK_CONNECTION") == "success",
                    "result_code": passive_check_op.get("oprnResultCode"),
                    "samsung_answered": True,
                    "structure": sanitize_structure(passive_check_op),
                }
            elif existing_conn.get("state") in ("online", "mobile") and p_state in ("wifi", "mobile"):
                # Enrich a live 'online'/'mobile' check with Wi-Fi / mobile network type from LOCATION's seData.netType
                existing_conn["state"] = p_state
                existing_conn["source_field"] = p_source
        structure_signature = json.dumps(server_structure, sort_keys=True, default=str)
        if dev_data.get("_server_structure_logged") != structure_signature:
            dev_data["_server_structure_logged"] = structure_signature
            _LOGGER.info("[%s] Sanitized structure of Samsung's Find response: %s", dev_name, server_structure)
        dev_data["_server_structure"] = server_structure

        return {
            "dev_name": dev_name,
            "dev_id": web_dvce_id,
            "active_location_attempted": active_location_attempted,
            "active_attempt_samsung_outcome": bool(active_location_attempted and active_sink.get("samsung_side")),
            "update_success": True,
            "fetch_error": None,
            "position_error": position_error,
            "active_location_error": active_location_error,
            "fmm_request_history": list(request_history),
            "location_found": bool(
                used_loc and used_loc.get("latitude") is not None and used_loc.get("longitude") is not None
            ),
            "used_loc": used_loc or {
                "latitude": None, "longitude": None, "gps_accuracy": None, "gps_date": None
            },
            "battery_level": last_battery.get("battery_level") if expose_battery else None,
            "left_battery_level": last_battery.get("left_battery_level"),
            "right_battery_level": last_battery.get("right_battery_level"),
            "raw_item": used_op,
            "web_capabilities": web_capabilities,
            "active_location_supported": active_location_supported,
            "position_stale": position_stale,
        }
    except ConfigEntryAuthFailed:
        raise
    except Exception as e:
        _LOGGER.error("[%s] Exception fetching FMM location: %s", dev_name, e, exc_info=True)
        return {**fail, "fetch_error": f"Location request failed: {type(e).__name__}"}


async def get_device_location(hass: HomeAssistant, session: aiohttp.ClientSession, dev_data: dict, entry_id: str) -> dict:
    """
    Retrieves the current location data for the specified device via the SmartThings API.

    Args:
        hass (HomeAssistant): Home Assistant instance.
        session (aiohttp.ClientSession): The current session.
        dev_data (dict): The device information obtained from get_devices.

    Returns:
        dict: The device location data.
    """
    dev_id = dev_data.get('device_id')
    dev_name = dev_data.get('name') or dev_id or "SmartThings Find"
    st_device_id = dev_data.get("st_device_id") or dev_id

    if dev_data.get("is_fmm"):
        return await get_fmm_device_location(hass, session, dev_data, entry_id)

    if not dev_data.get("is_tracker"):
        return {
            "dev_name": dev_name,
            "dev_id": dev_id,
            "update_success": True,
            "location_found": False
        }
    if not st_device_id:
        _LOGGER.error("[%s] Missing device id for location fetch", dev_name)
        return {
            "dev_name": dev_name,
            "dev_id": dev_id,
            "update_success": False,
            "location_found": False
        }

    # NOTE: empirically confirmed the website's LOCATION operation always rejects
    # SmartTags with resultCode=01 (see investigation notes) - tags have no network
    # connection of their own to push a fresh fix to, unlike phones/wearables. So we
    # never attempt an active location request for them (the Update Location button and
    # Active Location switch are only created for FMM devices, see button.py/switch.py).

    try:
        status, response = await _execute_installed_app(
            hass,
            session,
            entry_id,
            "GET",
            "/trackers/geolocation",
            extra_params={"stDids": st_device_id}
        )

        if status != 200:
            _LOGGER.error("[%s] Failed to fetch location data: %s", dev_name, response)
            return {
                "dev_name": dev_name,
                "dev_id": dev_id,
                "update_success": False,
                "location_found": False,
                "fetch_error": f"SmartThings location request failed (HTTP {status})",
                "http_status": status,
                "rate_limited": status == 429,
            }

        status_code, message, error_code = _parse_installed_apps_response(response)
        if status_code == 401 or error_code == "UnauthorizedError":
            await refresh_iot_token(hass, session, entry_id)
            status, response = await _execute_installed_app(
                hass,
                session,
                entry_id,
                "GET",
                "/trackers/geolocation",
                extra_params={"stDids": st_device_id}
            )
            if status != 200:
                _LOGGER.error("[%s] Failed to fetch location data after refresh: %s", dev_name, response)
                return {
                    "dev_name": dev_name,
                    "dev_id": dev_id,
                    "update_success": False,
                    "location_found": False
                }
            status_code, message, error_code = _parse_installed_apps_response(response)
        if status_code != 200 or message is None:
            _LOGGER.error("[%s] Location response error [%s/%s]: %s", dev_name, status_code, error_code, response)
            return {
                "dev_name": dev_name,
                "dev_id": dev_id,
                "update_success": False,
                "location_found": False,
                "fetch_error": f"SmartThings location response failed ({status_code}/{error_code})",
                "http_status": status_code,
                "rate_limited": status_code == 429,
            }

        items = message.get("items", [])
        item = next(
            (
                entry for entry in items
                if entry.get("deviceId") == st_device_id
                or entry.get("stDid") == st_device_id
                or entry.get("fmmDevId") == st_device_id
            ),
            None
        )
        if not item and items:
            item = items[0]
        if not item:
            _LOGGER.warning("[%s] No location item found", dev_name)
            return {
                "dev_name": dev_name,
                "dev_id": dev_id,
                "update_success": False,
                "location_found": False
            }

        if item.get("resultCode") == 403:
            _LOGGER.warning("[%s] Location access not allowed", dev_name)
            return {
                "dev_name": dev_name,
                "dev_id": dev_id,
                "update_success": False,
                "location_found": False
            }

        geo_locations = item.get("geolocations") or item.get("geoLocations") or item.get("geoLocation") or []
        if isinstance(geo_locations, dict):
            geo_locations = [geo_locations]
        used_loc = {
            "latitude": None,
            "longitude": None,
            "gps_accuracy": None,
            "gps_date": None
        }
        battery_level = None
        if geo_locations:
            # The API can return several geolocations for one device (different
            # reporting phones). Picking [0] blindly could pin the device to a stale
            # fix, so prefer the most recently updated one, like extract_best_location
            # does for the operations list.
            def _last_update(entry: dict):
                raw = entry.get("lastUpdateTime") or entry.get("lastUpdateAt")
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return -1

            if len(geo_locations) > 1:
                _LOGGER.debug(
                    "[%s] %d geolocations returned, using the newest",
                    dev_name, len(geo_locations)
                )
            geo = max(geo_locations, key=_last_update)
            try:
                used_loc["latitude"] = float(geo.get("latitude")) if geo.get("latitude") else None
                used_loc["longitude"] = float(geo.get("longitude")) if geo.get("longitude") else None
            except (TypeError, ValueError):
                pass
            try:
                used_loc["gps_accuracy"] = float(geo.get("accuracy")) if geo.get("accuracy") else None
            except (TypeError, ValueError):
                pass
            last_update = geo.get("lastUpdateTime") or geo.get("lastUpdateAt")
            if last_update:
                try:
                    last_update_value = int(last_update)
                except (TypeError, ValueError):
                    last_update_value = None
                if last_update_value:
                    used_loc["gps_date"] = datetime.fromtimestamp(
                        last_update_value / 1000, tz=pytz.UTC
                    )
            battery_raw = geo.get("battery")
            if battery_raw is None:
                battery_raw = geo.get("batteryLevel")
            battery_level = parse_battery_value(battery_raw)
            if battery_level is not None:
                dev_data.setdefault("_last_battery", {})["battery_level"] = battery_level
            else:
                battery_level = dev_data.get("_last_battery", {}).get("battery_level")

        return {
            "dev_name": dev_name,
            "dev_id": st_device_id,
            "update_success": True,
            "location_found": used_loc["latitude"] is not None and used_loc["longitude"] is not None,
            "used_loc": used_loc,
            "battery_level": battery_level,
            "raw_item": item
        }

    except ConfigEntryAuthFailed:
        raise
    except Exception as e:
        _LOGGER.error(
            f"[{dev_name}] Exception occurred while fetching location data for tag '{dev_name}': {e}", exc_info=True)
    return {
        "dev_name": dev_name,
        "dev_id": dev_id,
        "update_success": False,
        "location_found": False
    }


async def _ring_command(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    device_id: str,
    command: str
) -> tuple[bool, str | None]:
    if not device_id:
        return False, "missing_device_id"

    command = (command or "").lower()
    if command not in ("start", "stop"):
        return False, "invalid_command"

    status, response = await _execute_installed_app(
        hass,
        session,
        entry_id,
        "PUT",
        "/trackerapi",
        extra_uri=f"/trackers/{device_id}/ring",
        body={"command": command}
    )

    if status != 200:
        return False, f"http_{status}: {response}"

    status_code, message, error_code = _parse_installed_apps_response(response)
    if status_code != 200:
        return False, f"app_error_{status_code}/{error_code}: {message or response}"

    return True, None


async def ring_device(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    dev_data: dict
) -> tuple[bool, str | None]:
    if dev_data.get("is_tracker"):
        device_id = dev_data.get("st_device_id") or dev_data.get("device_id")
        return await _ring_command(hass, session, entry_id, device_id, "start")
    return False, "unsupported_device"


async def stop_ring_device(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    dev_data: dict
) -> tuple[bool, str | None]:
    if dev_data.get("is_tracker"):
        device_id = dev_data.get("st_device_id") or dev_data.get("device_id")
        return await _ring_command(hass, session, entry_id, device_id, "stop")
    return False, "unsupported_device"


def is_in_active_zone(hass: HomeAssistant, used_loc: dict | None) -> bool:
    """True when a position falls inside one of Home Assistant's zones.

    It is the same test the device_tracker uses to decide its own state (home / a zone's name /
    not_home): the closest non-passive zone that contains the point, with the fix's accuracy
    counted in. Used to pick which polling interval a device gets. Anything that stops it
    giving an answer (no position, zones not loaded) counts as "not in a zone", which is the
    safe side: the device is then polled at the general interval.
    """
    try:
        latitude = (used_loc or {}).get("latitude")
        longitude = (used_loc or {}).get("longitude")
        if latitude is None or longitude is None:
            return False
        from homeassistant.components import zone
        return zone.async_active_zone(
            hass, latitude, longitude, (used_loc or {}).get("gps_accuracy") or 0) is not None
    except Exception:
        _LOGGER.debug("Couldn't check zone membership, treating the device as outside any zone", exc_info=True)
        return False


def google_maps_url(latitude, longitude) -> str | None:
    """Build a Google Maps link for a coordinate pair, or None if it is incomplete."""
    if latitude is None or longitude is None:
        return None
    return f"https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"


def update_device_maps_link(hass: HomeAssistant, device_id: str, used_loc: dict | None) -> None:
    """Point the device page's 'Visit' link at the device's current coordinates.

    A device_tracker state can only ever be a zone name and HA renders neither
    states nor attributes as links, so the device registry's configuration_url is
    the only place a clickable link fits without a custom dashboard card. Only
    written when it actually changes, to keep the registry from being re-saved on
    every poll.
    """
    if not device_id or not used_loc:
        return
    url = google_maps_url(used_loc.get("latitude"), used_loc.get("longitude"))
    if not url:
        return
    registry = device_registry.async_get(hass)
    ha_dev = get_ha_device(hass, device_id)
    if not ha_dev or ha_dev.configuration_url == url:
        return
    registry.async_update_device(ha_dev.id, configuration_url=url)


def calc_gps_accuracy(hu, vu) -> float | None:
    """
    Calculate the GPS accuracy using the Pythagorean theorem.
    Returns the combined GPS accuracy based on the horizontal
    and vertical uncertainties provided by the API.

    Either value may be missing (positions reported by other devices often carry only a
    horizontal one): the one that's there is used on its own. None if neither is usable.

    Args:
        hu: Horizontal uncertainty.
        vu: Vertical uncertainty.

    Returns:
        float | None: Calculated GPS accuracy.
    """
    horizontal = _to_float(hu)
    vertical = _to_float(vu)
    if horizontal is None and vertical is None:
        return None
    if vertical is None:
        return round(horizontal, 1)
    if horizontal is None:
        return round(vertical, 1)
    return round((horizontal ** 2 + vertical ** 2) ** 0.5, 1)


def parse_stf_date(datestr: str) -> datetime:
    """
    Parses a date string in the format "%Y%m%d%H%M%S" to a datetime object.
    This is the format, the SmartThings Find API uses.

    Args:
        datestr (str): The date string in the format "%Y%m%d%H%M%S".

    Returns:
        datetime: A datetime object representing the input date string.
    """
    return datetime.strptime(datestr, "%Y%m%d%H%M%S").replace(tzinfo=pytz.UTC)


def parse_battery_value(raw) -> int | None:
    """Parse a battery value from Samsung's FMM or SmartTag payloads into 0..100."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        cleaned = raw.strip().upper()
        if not cleaned:
            return None
        if cleaned in BATTERY_LEVELS:
            return BATTERY_LEVELS[cleaned]
        try:
            val = int(cleaned)
        except ValueError:
            try:
                val = int(float(cleaned))
            except ValueError:
                return None
    elif isinstance(raw, (int, float)):
        val = int(raw)
    else:
        return None
    return val if 0 <= val <= 100 else None


def extract_fmm_battery(operations: list | None) -> dict[str, int | None]:
    """Extract general and earbud (Left/Right) battery levels from FMM operations.

    Matches charlesbel/samsung-re-find PR #2:
      - General battery: `op['battery']`, `op['battery_percent']`, `op['extra']['battery']`,
        or `op['encLocation']['battery']`.
      - Earbud channels (Galaxy Buds): `op['extra']['left']['battery']` and
        `op['extra']['right']['battery']`.
    """
    result: dict[str, int | None] = {
        "battery_level": None,
        "left_battery_level": None,
        "right_battery_level": None,
    }
    if not isinstance(operations, list):
        return result

    priority_order = {
        "CHECK_CONNECTION": 0,
        "RING": 1,
        "LOCATION": 2,
        "LASTLOC": 3,
        "OFFLINE_LOC": 4,
    }
    def _as_dict(val) -> dict:
        if isinstance(val, dict):
            return val
        if isinstance(val, str):
            stripped = val.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    loaded = json.loads(stripped)
                    if isinstance(loaded, dict):
                        return loaded
                except Exception:
                    pass
        return {}

    def _op_timestamp_num(op: dict) -> int:
        raw_ts = str(op.get("oprnDoneDate") or op.get("oprnCrtDate") or "")
        digits = "".join(ch for ch in raw_ts if ch.isdigit())
        return int(digits) if digits else 0

    def _extract_side(container: dict, side: str) -> int | None:
        if not isinstance(container, dict):
            return None
        node = container.get(side)
        if isinstance(node, dict):
            for k in ("battery", "batteryLevel", "battery_percent", "level"):
                parsed = parse_battery_value(node.get(k))
                if parsed is not None:
                    return parsed
        else:
            parsed = parse_battery_value(node)
            if parsed is not None:
                return parsed
        for alt_key in (f"{side}Battery", f"{side}_battery", f"battery_{side}", f"battery{side.capitalize()}"):
            if alt_key in container:
                parsed = parse_battery_value(container.get(alt_key))
                if parsed is not None:
                    return parsed
        return None

    valid_ops = [op for op in operations if isinstance(op, dict)]
    valid_ops.sort(
        key=lambda op: (
            priority_order.get(str(op.get("oprnType") or "").upper(), 99),
            -_op_timestamp_num(op),
        )
    )
    for op in valid_ops:
        extra = _as_dict(op.get("extra"))
        extra_dm = _as_dict(op.get("extraFromDM"))
        enc_loc = _as_dict(op.get("encLocation"))

        if result["battery_level"] is None:
            for candidate in (
                op.get("battery"),
                op.get("battery_percent"),
                op.get("batteryLevel"),
                extra.get("battery"),
                extra.get("batteryLevel"),
                extra_dm.get("battery"),
                enc_loc.get("battery"),
            ):
                parsed = parse_battery_value(candidate)
                if parsed is not None:
                    result["battery_level"] = parsed
                    break

        if result["left_battery_level"] is None:
            for container in (extra, extra_dm, op, enc_loc):
                parsed_left = _extract_side(container, "left")
                if parsed_left is not None:
                    result["left_battery_level"] = parsed_left
                    break

        if result["right_battery_level"] is None:
            for container in (extra, extra_dm, op, enc_loc):
                parsed_right = _extract_side(container, "right")
                if parsed_right is not None:
                    result["right_battery_level"] = parsed_right
                    break

    if result["battery_level"] is None:
        lr = [v for v in (result["left_battery_level"], result["right_battery_level"]) if v is not None]
        if lr:
            result["battery_level"] = min(lr)

    return result


def resolve_device_battery_value(
    dev_data: dict, tag_data: dict | None = None, side: str | None = None
) -> int | None:
    """Resolve the battery value to display on a sensor.

    - Active-connectable devices (smartphones, tablets, PCs) without Active Location enabled
      (`active_location_requested` is False) always return `None` (`unknown` in Home Assistant),
      because passive reads do not refresh battery on Samsung's backend.
    - Passive devices (watches, Buds, SmartTags) and active-connectable devices with Active
      Location enabled return the latest reading or the last known value (`_last_battery`).
    """
    data = tag_data or {}
    last_batt = dev_data.get("_last_battery") or {}
    if side in ("left", "right"):
        key = f"{side}_battery_level"
        val = data.get(key)
        return val if val is not None else last_batt.get(key)
    if is_active_connectable_device(dev_data) and not dev_data.get("active_location_requested"):
        return None
    val = data.get("battery_level")
    return val if val is not None else last_batt.get("battery_level")


# ---------------------------------------------------------------------------------------------
# Proximity & Direction (Home / Work)
# ---------------------------------------------------------------------------------------------
DIRECTION_STATES = (
    "arrived_home",
    "arrived_work",
    "towards_home",
    "towards_work",
    "away_from_home",
    "away_from_work",
    "away",
    "stationary",
    "unknown",
)


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in meters between two WGS84 coordinates."""
    r_earth = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    return 2.0 * r_earth * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return initial compass bearing in degrees [0, 360) from point 1 to point 2."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def resolve_ha_zone(hass: HomeAssistant, zone_entity_id: str | None) -> dict | None:
    """Read latitude, longitude and radius of a configured Home Assistant zone entity."""
    if not zone_entity_id or not isinstance(zone_entity_id, str):
        return None
    states = getattr(hass, "states", None)
    if states is None or not hasattr(states, "get"):
        return None
    st = states.get(zone_entity_id)
    if st is None:
        return None
    attrs = getattr(st, "attributes", None) or {}
    lat = _to_float(attrs.get("latitude"))
    lon = _to_float(attrs.get("longitude"))
    radius = _to_float(attrs.get("radius"))
    if lat is None or lon is None:
        return None
    return {
        "entity_id": zone_entity_id,
        "latitude": lat,
        "longitude": lon,
        "radius": max(0.0, radius or 0.0),
    }


def _format_distance(meters: float | None, unit: str) -> float | int | None:
    if meters is None:
        return None
    if unit == "km":
        return round(meters / 1000.0, 2)
    return round(meters)


def update_proximity_state(
    dev_data: dict,
    tag_data: dict,
    home_zone: dict | None,
    work_zone: dict | None,
    *,
    distance_unit: str = "m",
    now: datetime | None = None,
) -> dict:
    """Compute proximity distances and movement direction towards Home / Work."""
    unit = "km" if distance_unit == "km" else "m"
    now_utc = now or datetime.now(pytz.UTC)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=pytz.UTC)

    base = {
        "state": "unknown",
        "distance_unit": unit,
        "distance_home": None,
        "distance_work": None,
        "delta_home": None,
        "delta_work": None,
        "speed_kmh": None,
        "bearing_deg": None,
        "alignment_home": None,
        "alignment_work": None,
        "home_zone": home_zone.get("entity_id") if isinstance(home_zone, dict) else None,
        "work_zone": work_zone.get("entity_id") if isinstance(work_zone, dict) else None,
        "samples_used": 0,
        "last_fix_at": None,
    }

    if not home_zone and not work_zone:
        tag_data["proximity"] = base
        return base

    if not is_direction_supported_device(dev_data):
        tag_data["proximity"] = base
        return base

    # Active-connectable devices (phones, tablets, PCs) require Active Location ON.
    if is_active_connectable_device(dev_data) and not dev_data.get("active_location_requested"):
        dev_data["_proximity_history"] = []
        dev_data.pop("_proximity_last_state", None)
        tag_data["proximity"] = base
        return base

    if tag_data.get("position_unknown") or not tag_data.get("location_found"):
        tag_data["proximity"] = base
        return base

    used_loc = tag_data.get("used_loc") or {}
    lat = _to_float(used_loc.get("latitude"))
    lon = _to_float(used_loc.get("longitude"))
    if lat is None or lon is None:
        tag_data["proximity"] = base
        return base

    acc = max(0.0, _to_float(used_loc.get("gps_accuracy")) or 0.0)
    gps_date = used_loc.get("gps_date")
    if isinstance(gps_date, datetime) and gps_date.tzinfo is None:
        gps_date = gps_date.replace(tzinfo=pytz.UTC)
    elif not isinstance(gps_date, datetime):
        gps_date = now_utc

    dist_home = (
        haversine_distance_m(lat, lon, home_zone["latitude"], home_zone["longitude"])
        if home_zone else None
    )
    dist_work = (
        haversine_distance_m(lat, lon, work_zone["latitude"], work_zone["longitude"])
        if work_zone else None
    )
    base["distance_home"] = _format_distance(dist_home, unit)
    base["distance_work"] = _format_distance(dist_work, unit)
    base["last_fix_at"] = gps_date

    # Check zone containment first (deterministic tie-break if Home and Work overlap).
    in_home = dist_home is not None and dist_home <= home_zone["radius"]
    in_work = dist_work is not None and dist_work <= work_zone["radius"]
    if in_home and in_work:
        zone_state = "arrived_home" if dist_home <= dist_work else "arrived_work"
    elif in_home:
        zone_state = "arrived_home"
    elif in_work:
        zone_state = "arrived_work"
    else:
        zone_state = None

    history: list[dict] = dev_data.setdefault("_proximity_history", [])
    last_sample = history[-1] if history else None
    is_strictly_newer = last_sample is None or gps_date > last_sample["gps_date"]

    if is_strictly_newer and acc <= PROXIMITY_MAX_ACCURACY_M:
        accept_sample = True
        if last_sample is not None:
            dt_s = (gps_date - last_sample["gps_date"]).total_seconds()
            if dt_s > PROXIMITY_MAX_GAP_S:
                history.clear()
            elif dt_s > 0:
                step_m = haversine_distance_m(
                    last_sample["lat"], last_sample["lon"], lat, lon
                )
                speed_kmh = (step_m / dt_s) * 3.6
                if speed_kmh > PROXIMITY_MAX_SPEED_KMH:
                    accept_sample = False
        if accept_sample:
            history.append({
                "lat": lat,
                "lon": lon,
                "acc": acc,
                "gps_date": gps_date,
                "dist_home": dist_home,
                "dist_work": dist_work,
            })
            if len(history) > PROXIMITY_HISTORY_MAX_POINTS:
                del history[:-PROXIMITY_HISTORY_MAX_POINTS]

    base["samples_used"] = len(history)

    if zone_state is not None:
        base["state"] = zone_state
        dev_data["_proximity_last_state"] = zone_state
        tag_data["proximity"] = base
        return base

    fix_age_s = max(0.0, (now_utc - gps_date).total_seconds())
    if fix_age_s > PROXIMITY_DECAY_S:
        base["state"] = "stationary" if len(history) >= 2 else "unknown"
        dev_data["_proximity_last_state"] = base["state"]
        tag_data["proximity"] = base
        return base

    if len(history) < 2:
        base["state"] = "unknown"
        dev_data["_proximity_last_state"] = "unknown"
        tag_data["proximity"] = base
        return base

    curr = history[-1]
    ref = None
    for candidate in history[:-1]:
        dt_cand = (curr["gps_date"] - candidate["gps_date"]).total_seconds()
        if 0 < dt_cand <= PROXIMITY_MAX_GAP_S:
            ref = candidate
            break
    if ref is None:
        base["state"] = "unknown"
        dev_data["_proximity_last_state"] = "unknown"
        tag_data["proximity"] = base
        return base

    dt_s = (curr["gps_date"] - ref["gps_date"]).total_seconds()
    delta_s = haversine_distance_m(ref["lat"], ref["lon"], curr["lat"], curr["lon"])
    min_move_m = max(
        PROXIMITY_MIN_MOVE_M,
        PROXIMITY_ACCURACY_FACTOR * (ref["acc"] + curr["acc"]),
    )

    delta_home = (
        (curr["dist_home"] - ref["dist_home"])
        if curr["dist_home"] is not None and ref["dist_home"] is not None
        else None
    )
    delta_work = (
        (curr["dist_work"] - ref["dist_work"])
        if curr["dist_work"] is not None and ref["dist_work"] is not None
        else None
    )
    base["delta_home"] = _format_distance(delta_home, unit)
    base["delta_work"] = _format_distance(delta_work, unit)
    base["speed_kmh"] = round((delta_s / dt_s) * 3.6, 1) if dt_s > 0 else 0.0

    if delta_s < min_move_m:
        base["state"] = "stationary"
        dev_data["_proximity_last_state"] = "stationary"
        tag_data["proximity"] = base
        return base

    base["bearing_deg"] = round(
        initial_bearing_deg(ref["lat"], ref["lon"], curr["lat"], curr["lon"]), 1
    )

    align_home = (-delta_home / delta_s) if delta_home is not None else None
    align_work = (-delta_work / delta_s) if delta_work is not None else None
    base["alignment_home"] = round(align_home, 2) if align_home is not None else None
    base["alignment_work"] = round(align_work, 2) if align_work is not None else None

    prev_state = dev_data.get("_proximity_last_state")
    thresh_home = (
        PROXIMITY_MIN_ALIGNMENT_KEEP if prev_state == "towards_home" else PROXIMITY_MIN_ALIGNMENT_ENTER
    )
    thresh_work = (
        PROXIMITY_MIN_ALIGNMENT_KEEP if prev_state == "towards_work" else PROXIMITY_MIN_ALIGNMENT_ENTER
    )

    home_ok = (
        delta_home is not None
        and delta_home <= -min_move_m
        and align_home is not None
        and align_home >= thresh_home
    )
    work_ok = (
        delta_work is not None
        and delta_work <= -min_move_m
        and align_work is not None
        and align_work >= thresh_work
    )

    if home_ok and work_ok:
        if abs(align_home - align_work) > PROXIMITY_COLLINEAR_MARGIN:
            state = "towards_home" if align_home > align_work else "towards_work"
        else:
            state = "towards_home" if curr["dist_home"] <= curr["dist_work"] else "towards_work"
    elif home_ok:
        state = "towards_home"
    elif work_ok:
        state = "towards_work"
    else:
        away_thresh_home = (
            PROXIMITY_MIN_ALIGNMENT_KEEP if prev_state == "away_from_home" else PROXIMITY_MIN_ALIGNMENT_ENTER
        )
        away_thresh_work = (
            PROXIMITY_MIN_ALIGNMENT_KEEP if prev_state == "away_from_work" else PROXIMITY_MIN_ALIGNMENT_ENTER
        )
        home_away = (
            delta_home is not None
            and delta_home >= min_move_m
            and align_home is not None
            and align_home <= -away_thresh_home
        )
        work_away = (
            delta_work is not None
            and delta_work >= min_move_m
            and align_work is not None
            and align_work <= -away_thresh_work
        )
        if home_away and not work_away:
            state = "away_from_home"
        elif work_away and not home_away:
            state = "away_from_work"
        else:
            # Moving away from both zones, or moving laterally/neutrally.
            state = "away"

    base["state"] = state
    dev_data["_proximity_last_state"] = state
    tag_data["proximity"] = base
    return base
