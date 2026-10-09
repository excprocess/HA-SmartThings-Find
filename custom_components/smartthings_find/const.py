DOMAIN = "smartthings_find"

CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_IOT_ACCESS_TOKEN = "iot_access_token"
CONF_IOT_REFRESH_TOKEN = "iot_refresh_token"
CONF_USER_ID = "user_id"
CONF_USER_EMAIL = "user_email"
CONF_AUTH_SERVER_URL = "auth_server_url"
CONF_DEVICE_ID = "device_id"
CONF_ST_USER_UUID = "st_user_uuid"
CONF_INSTALLED_APP_ID = "installed_app_id"

# Polling interval, in seconds. CONF_UPDATE_INTERVAL is the general one: it applies to every
# device that is not inside a Home Assistant zone. CONF_UPDATE_INTERVAL_IN_ZONE overrides it for
# devices that are inside one (0 = no override, same interval as everywhere else).
CONF_UPDATE_INTERVAL = "update_interval"
CONF_UPDATE_INTERVAL_DEFAULT = 120
CONF_UPDATE_INTERVAL_IN_ZONE = "update_interval_in_zone"
CONF_UPDATE_INTERVAL_IN_ZONE_DEFAULT = 0
MIN_UPDATE_INTERVAL = 30

# Keep location fixes even when this uncertain, but flag them for the user.
LOW_ACCURACY_THRESHOLD_M = 100

RING_TIMEOUT_SECONDS = 120

CLIENT_ID_FIND = "27zmg0v1oo"
CLIENT_ID_AUTH = "yfrtglt53o"
CLIENT_ID_ONECONNECT = "6iado3s6jc"
SCOPE_FIND = "offline.access"
SCOPE_AUTH = "serviceType"

# Client id/scope of the smartthingsfind.samsung.com *website* itself (as opposed to the
# SmartThings Cloud "installed app" API used for trackers). Confirmed against the pre-OAuth
# fork's login URL, which hardcoded this same client_id for the website login.
WEB_FIND_CLIENT_ID = "ntly6zvfpn"
WEB_FIND_SCOPE = "iot.client"

CONF_USER_AUTH_TOKEN = "user_auth_token"
CONF_LOGIN_ID = "login_id"

BATTERY_LEVELS = {
    'FULL': 100,
    'MEDIUM': 50,
    'LOW': 15,
    'VERY_LOW': 5
}

# How many consecutive failed fetch cycles to bridge over with the last known good
# position before letting a device go Unavailable for real. Keeps one-off session/HTTP
# blips invisible without permanently masking an actual persistent problem.
MAX_STALE_FALLBACK_CYCLES = 3
