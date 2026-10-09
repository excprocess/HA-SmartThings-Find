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
    CONF_UPDATE_INTERVAL_DEFAULT,
)

_LOGGER = logging.getLogger(__name__)

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
        ("sensor", f"stf_device_location_{device_id}", f"{name} Location"),
        ("sensor", f"stf_device_maps_link_{device_id}", f"{name} Maps Link"),
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


def _country_to_iso3(country: str | None) -> str:
    if not country:
        return "USA"
    country = country.upper()
    if len(country) == 3:
        return country
    if len(country) != 2:
        return country
    try:
        import pycountry  # type: ignore
        entry = pycountry.countries.get(alpha_2=country)
        if entry and entry.alpha_3:
            return entry.alpha_3
    except Exception:
        pass
    fallback = {
        "US": "USA",
        "DE": "DEU",
        "GB": "GBR",
        "UK": "GBR",
        "AT": "AUT",
        "CH": "CHE",
        "NL": "NLD",
        "FR": "FRA",
        "ES": "ESP",
        "IT": "ITA",
        "PL": "POL",
    }
    return fallback.get(country, country)


def _get_find_headers(hass: HomeAssistant, entry_id: str) -> dict[str, str]:
    data_store = hass.data[DOMAIN][entry_id]
    auth_server_url = str(data_store.get(CONF_AUTH_SERVER_URL, ""))
    if auth_server_url.startswith("https://"):
        auth_server_url = auth_server_url[len("https://"):]
    elif auth_server_url.startswith("http://"):
        auth_server_url = auth_server_url[len("http://"):]
    return {
        "X-Sec-Sa-Userid": str(data_store.get(CONF_USER_ID, "")),
        "X-Sec-Sa-Countrycode": _country_to_iso3(hass.config.country),
        "X-Sec-Sa-Authserverurl": auth_server_url,
        "X-Sec-Sa-Authtoken": str(data_store.get(CONF_ACCESS_TOKEN, "")),
        "Accept": "application/json",
    }


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




async def _refresh_token(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str,
    refresh_token_key: str,
    access_token_key: str,
    client_id: str
) -> str:
    """Refreshes the access token using the given refresh token key."""
    data_store = hass.data[DOMAIN][entry_id]
    refresh_token = data_store.get(refresh_token_key)
    auth_server_url = data_store.get(CONF_AUTH_SERVER_URL)

    if not refresh_token or not auth_server_url:
        raise ConfigEntryAuthFailed("Refresh token or Auth URL missing")

    try:
        url = f"{auth_server_url}/auth/oauth2/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id
        }
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}

        async with session.post(url, data=payload, headers=headers) as res:
            if res.status != 200:
                _LOGGER.error(
                    "Token refresh failed: %s - %s",
                    res.status,
                    _mask_secret(await res.text(), keep=40)
                )
                raise ConfigEntryAuthFailed("Token refresh failed")

            data = await res.json()

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


async def refresh_find_token(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    entry_id: str
) -> str:
    return await _refresh_token(
        hass,
        session,
        entry_id,
        CONF_REFRESH_TOKEN,
        CONF_ACCESS_TOKEN,
        CLIENT_ID_FIND
    )


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
        CLIENT_ID_ONECONNECT
    )


async def authenticated_request(hass: HomeAssistant, session: aiohttp.ClientSession, entry_id: str, url: str, json_data: dict = None, data: dict = None) -> tuple[int, str]:
    """
    Helper to perform an authenticated request with automatic token refresh.
    
    Returns:
        tuple: (status_code, response_text)
    """
    headers = _get_find_headers(hass, entry_id)
    
    async def _do_req(auth_headers: dict[str, str]):
        # We prefer JSON if provided, else data (which might be empty dict for get_devices)
        if json_data is not None:
             async with session.post(url, json=json_data, headers=auth_headers) as resp:
                 return resp.status, await resp.text()
        else:
             async with session.post(url, data=data or {}, headers=auth_headers) as resp:
                 return resp.status, await resp.text()

    status, text = await _do_req(headers)
    
    if status in [401, 403]:
        _LOGGER.info(f"Request to {url.split('/')[-1]} returned {status}, refreshing token...")
        try:
            new_token = await refresh_find_token(hass, session, entry_id)
        except Exception as e:
            _LOGGER.error(f"Failed to refresh token: {e}")
            raise ConfigEntryAuthFailed("Token refresh failed")
            
        headers["X-Sec-Sa-Authtoken"] = f"{new_token}"
        status, text = await _do_req(headers)
        
        if status in [401, 403]:
             raise ConfigEntryAuthFailed(f"Auth failed after refresh: {status}")
             
    return status, text


LOCATION_OP_TYPES = ("LOCATION", "LASTLOC", "OFFLINE_LOC")

# (device, kind-of-problem, shape) combinations already reported, so a persistent oddity in
# Samsung's data is explained once in the log instead of on every poll.
_REPORTED_SKIPS: set = set()


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
        return cached.get("jsessionid"), cached.get("csrf")

    user_auth_token = data_store.get(CONF_USER_AUTH_TOKEN)
    login_id = data_store.get(CONF_LOGIN_ID)
    auth_server_url = data_store.get(CONF_AUTH_SERVER_URL)
    device_id = data_store.get(CONF_DEVICE_ID) or _get_or_create_device_id(hass)

    if not user_auth_token or not auth_server_url:
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
                _LOGGER.debug("[diag] Web session bridge: authorize HTTP %s", res.status)
                return None, None
            auth_data = await res.json(content_type=None)

        code = auth_data.get("code")
        if not code and auth_data.get("privacyAccepted") == "N" and "login_id" in params_auth:
            params_auth.pop("login_id", None)
            async with session.get(f"{auth_server_url}/auth/oauth2/v2/authorize", params=params_auth) as res:
                if res.status != 200:
                    _LOGGER.debug("[diag] Web session bridge: authorize retry HTTP %s", res.status)
                    return None, None
                auth_data = await res.json(content_type=None)
            code = auth_data.get("code")
        if not code:
            _LOGGER.debug("[diag] Web session bridge: authorize response missing code: %s", auth_data)
            return None, None

        auth_host = auth_server_url.split("://", 1)[-1]

        async with web_client.get(
            "https://smartthingsfind.samsung.com/getState.do",
            params={"payload": "hound"},
        ) as res:
            if res.status != 200:
                _LOGGER.debug("[diag] Web session bridge: getState.do HTTP %s", res.status)
                return None, None
            state_data = await res.json(content_type=None)
        login_state = state_data.get("state")
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
            if all_devices:
                await enrich_devices_with_web_metadata(hass, session, entry_id, all_devices)
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
    if device_type == "BUDS":
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
    if not all_devices:
        return
    jsessionid, csrf = await get_web_session_cookie(hass, session, entry_id)
    if not jsessionid or not csrf:
        _LOGGER.debug("[diag] Skipping web metadata enrichment: no web session available")
        return
    web_client = await _get_web_client(hass, entry_id)

    try:
        async with web_client.post(
            "https://smartthingsfind.samsung.com/device/getDeviceList.do",
            params={"_csrf": csrf},
            cookies={"JSESSIONID": jsessionid},
        ) as res:
            status = res.status
            body = await res.json(content_type=None) if status == 200 else await res.text()
    except Exception as e:
        _LOGGER.debug("[diag] Web getDeviceList.do raised: %s", e, exc_info=True)
        return

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
        name = (dev.get("name") or "").casefold()
        match = by_name.get(name)
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

        if dev.get("is_fmm"):
            _LOGGER.debug(
                "[%s] Resolved website device id for location lookup: dvceID=%s icon=%s",
                dev.get("name"), match.get("dvceID"), icon_url,
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
        ha_dev = registry.async_get_device({identifier})
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
    dev_name = dev_data.get("name") or "SmartThings Find"
    web_dvce_id = dev_data.get("web_dvce_id")
    web_usr_id = dev_data.get("web_usr_id")
    if not web_dvce_id:
        _LOGGER.debug("[%s] %s requested but no website device id resolved", dev_name, operation)
        return False, None

    jsessionid, csrf = await get_web_session_cookie(hass, session, entry_id)
    if not jsessionid or not csrf:
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
                _LOGGER.debug("[%s] addOperation.do (%s) HTTP %s", dev_name, operation, res.status)
                return False, None
            accepted_data = await res.json(content_type=None)

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
                    continue
                result_data = await res.json(content_type=None)
            candidates = [
                op for op in result_data.get("operation", [])
                if op.get("oprnType") == operation and str(op.get("reqId")) == str(req_id)
            ]
            if candidates:
                latest = candidates[-1]
                if latest.get("oprnStsCd") == "2800":
                    success = latest.get("oprnResultCode") == "1200"
                    _LOGGER.debug(
                        "[%s] %s %s (oprnResultCode=%s)",
                        dev_name, operation, "succeeded" if success else "failed", latest.get("oprnResultCode"),
                    )
                    return success, (None if success else latest.get("oprnResultCode"))
        _LOGGER.debug("[%s] %s request timed out after %ss", dev_name, operation, poll_seconds)
        return False, None
    except Exception as e:
        _LOGGER.debug("[%s] %s request raised: %s", dev_name, operation, e, exc_info=True)
        return False, None


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

    fail = {
        "dev_name": dev_name,
        "dev_id": dev_id,
        "update_success": False,
        "location_found": False,
        "fetch_error": None,
        "position_error": None,
    }

    if not web_dvce_id:
        # Not an error - the name-matching enrichment step just hasn't resolved this
        # device yet (or the web bridge is unavailable this cycle).
        _LOGGER.debug("[%s] No website device id resolved yet, cannot fetch FMM location", dev_name)
        return {**fail, "update_success": True, "fetch_error": "Website device ID is not resolved"}

    active_location_error = None
    if dev_data.get('_active_location_unsupported'):
        # Samsung explicitly rejected LOCATION for this device as unsupported
        # (resultCode=501 - seen consistently for buds/non-LTE watches, which have no
        # network connection of their own to push a fix to) at some point this runtime.
        # No point spending ~7 requests worth of polling on that again every cycle;
        # retried fresh only after the next restart/reload, in case that ever changes.
        pass
    elif force_active or (dev_data.get("active_location_requested") and not dev_data.get('_consecutive_fetch_failures')):
        # active_location_requested is set live by ActiveLocationSwitch (switch.py) -
        # a per-device toggle, replacing the old global "active mode" options that used
        # to live in the integration's options flow. The automatic (non-force_active)
        # path is skipped while a failure streak is already in progress
        # (_consecutive_fetch_failures > 0) - piling an extra ~7 requests worth of
        # active-push polling onto a session that's already failing (likely rate-limited)
        # only makes things worse; let the passive read's own retry-with-fresh-bootstrap
        # do its job first. A manual button press (force_active=True) always goes through
        # regardless, since that's an explicit one-off request from the user.
        active_success, active_reason = await perform_fmm_operation(
            hass, session, entry_id, dev_data, "LOCATION"
        )
        active_location_error = None if active_success else (
            f"Samsung active location request failed (resultCode={active_reason})"
            if active_reason else "Samsung did not complete the active location request (timeout or no response)"
        )
        if not active_success and active_reason == "501":
            dev_data['_active_location_unsupported'] = True
            _LOGGER.info(
                "[%s] Samsung reports active location is not supported for this device - "
                "won't keep retrying it until the next restart",
                dev_name,
            )
        # Whether or not it succeeded/timed out, fall through to the normal passive read
        # below - extract_best_location() picks the newest fix regardless of how the
        # freshest one got there, and a passive read still returns the best data we have.
    fail["active_location_error"] = active_location_error

    async def _fetch(force_refresh: bool):
        jsessionid, csrf = await get_web_session_cookie(hass, session, entry_id, force_refresh=force_refresh)
        if not jsessionid or not csrf:
            return None, None
        web_client = await _get_web_client(hass, entry_id)
        async with web_client.post(
            "https://smartthingsfind.samsung.com/device/setLastSelect.do",
            params={"_csrf": csrf},
            json={"dvceId": web_dvce_id, "removeDevice": []},
            headers={"Accept": "application/json"},
            cookies={"JSESSIONID": jsessionid},
        ) as res:
            status = res.status
            if status == 200:
                body = await res.json(content_type=None)
            else:
                body = None
        return status, body

    try:
        status, body = await _fetch(False)
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

        # No battery_level here: DeviceBatterySensor is only created for trackers
        # (sensor.py) since FMM battery data has the same background-wake unreliability
        # as location, so we don't bother parsing CHECK_CONNECTION operations for it.

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

        return {
            "dev_name": dev_name,
            "dev_id": web_dvce_id,
            "update_success": True,
            "fetch_error": None,
            "position_error": position_error,
            "active_location_error": active_location_error,
            "location_found": bool(
                used_loc and used_loc.get("latitude") is not None and used_loc.get("longitude") is not None
            ),
            "used_loc": used_loc or {
                "latitude": None, "longitude": None, "gps_accuracy": None, "gps_date": None
            },
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
                "location_found": False
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
                "location_found": False
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
            if battery_raw is not None:
                if isinstance(battery_raw, str):
                    battery_level = BATTERY_LEVELS.get(battery_raw, None)
                    if battery_level is None:
                        try:
                            battery_level = int(battery_raw)
                        except ValueError:
                            battery_level = None
                else:
                    try:
                        battery_level = int(battery_raw)
                    except (TypeError, ValueError):
                        battery_level = None

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
    ha_dev = registry.async_get_device(identifiers={(DOMAIN, device_id)})
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


def get_battery_level(dev_name: str, ops: list) -> int:
    """
    Try to extract the device battery level from the received operation

    Args:
        dev_name (str): The name of the device.
        ops (list): List of operations from the API.

    Returns:
        int: The battery level if found, None otherwise.
    """
    for op in ops:
        if op['oprnType'] == 'CHECK_CONNECTION' and 'battery' in op:
            batt_raw = op['battery']
            batt = BATTERY_LEVELS.get(batt_raw, None)
            if batt is None:
                try:
                    batt = int(batt_raw)
                except ValueError:
                    _LOGGER.warn(
                        f"[{dev_name}]: Received invalid battery level: {batt_raw}")
            return batt
    return None



