from datetime import datetime, timedelta, timezone
import logging
import time
import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from homeassistant.const import Platform
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.config_entries import ConfigEntry

from .const import (
    DOMAIN,
    CONF_ACCESS_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_IOT_ACCESS_TOKEN,
    CONF_IOT_REFRESH_TOKEN,
    CONF_USER_ID,
    CONF_AUTH_SERVER_URL,
    CONF_DEVICE_ID,
    CONF_ST_USER_UUID,
    CONF_USER_AUTH_TOKEN,
    CONF_LOGIN_ID,
    CONF_INSTALLED_APP_ID,
    CONF_UPDATE_INTERVAL,
    CONF_UPDATE_INTERVAL_DEFAULT,
    CONF_UPDATE_INTERVAL_IN_ZONE,
    CONF_UPDATE_INTERVAL_IN_ZONE_DEFAULT,
    MAX_STALE_FALLBACK_CYCLES,
)
from .utils import (
    is_in_active_zone,
    get_devices,
    get_device_location,
    update_device_maps_link,
    get_device_ble_metadata,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.DEVICE_TRACKER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the SmartThings Find component."""
    hass.data[DOMAIN] = {}
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SmartThings Find from a config entry."""
    
    hass.data[DOMAIN][entry.entry_id] = {}

    # Load the tokens from the config
    access_token = entry.data.get(CONF_ACCESS_TOKEN)
    refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    iot_access_token = entry.data.get(CONF_IOT_ACCESS_TOKEN)
    iot_refresh_token = entry.data.get(CONF_IOT_REFRESH_TOKEN)
    auth_server_url = entry.data.get(CONF_AUTH_SERVER_URL)
    user_id = entry.data.get(CONF_USER_ID)
    device_id = entry.data.get(CONF_DEVICE_ID)
    st_user_uuid = entry.data.get(CONF_ST_USER_UUID)
    installed_app_id = entry.data.get(CONF_INSTALLED_APP_ID)
    user_auth_token = entry.data.get(CONF_USER_AUTH_TOKEN)
    login_id = entry.data.get(CONF_LOGIN_ID)
    
    # Store in hass.data so utils can access it
    hass.data[DOMAIN][entry.entry_id].update({
        CONF_ACCESS_TOKEN: access_token,
        CONF_REFRESH_TOKEN: refresh_token,
        CONF_IOT_ACCESS_TOKEN: iot_access_token,
        CONF_IOT_REFRESH_TOKEN: iot_refresh_token,
        CONF_AUTH_SERVER_URL: auth_server_url,
        CONF_USER_ID: user_id,
        CONF_DEVICE_ID: device_id,
        CONF_ST_USER_UUID: st_user_uuid,
        CONF_INSTALLED_APP_ID: installed_app_id,
        # DIAGNOSTIC PATCH: these were already saved into entry.data by config_flow.py
        # but never copied into the runtime dict utils.py actually reads from.
        CONF_USER_AUTH_TOKEN: user_auth_token,
        CONF_LOGIN_ID: login_id
    })

    session = async_get_clientsession(hass)
    # Active-mode is now a per-device switch (ActiveLocationSwitch, switch.py) rather
    # than these two global options - see config_flow.py's options flow.

    # No CSRF fetch needed for OAuth
    # await fetch_csrf(hass, session, entry.entry_id)
    
    # Load all SmartThings-Find devices from the users account
    devices = await get_devices(hass, session, entry.entry_id)
    
    # Create an update coordinator. This is responsible to regularly
    # fetch data from STF and update the device_tracker and sensor
    # entities
    update_interval = entry.options.get(CONF_UPDATE_INTERVAL, CONF_UPDATE_INTERVAL_DEFAULT)
    # 0 (the default) = no separate in-zone interval: everything uses the general one.
    in_zone_interval = entry.options.get(
        CONF_UPDATE_INTERVAL_IN_ZONE, CONF_UPDATE_INTERVAL_IN_ZONE_DEFAULT) or update_interval
    coordinator = SmartThingsFindCoordinator(
        hass, session, devices, update_interval, in_zone_interval, entry.entry_id)

    # This is what makes the whole integration slow to load (around 10-15
    # seconds for my 15 devices) but it is the right way to do it. Only if
    # it succeeds, the integration will be marked as successfully loaded.
    await coordinator.async_config_entry_first_refresh()
    
    hass.data[DOMAIN][entry.entry_id].update({
        "session": session,
        "coordinator": coordinator,
        "devices": devices,
        # Baseline for _async_update_listener below, so it can tell "options actually
        # changed" apart from "entry.data changed" (e.g. a token refresh persisting a
        # new token via async_update_entry) - both fire this same listener, but only
        # the former should trigger a full reload.
        "_last_options": dict(entry.options),
    })

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    hass.async_create_task(
        hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    )
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_success = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_success:
        # Close the dedicated web-bridge client (_get_web_client in utils.py) if one was
        # ever created - it's isolated from HA's shared session on purpose, so nothing
        # else closes it for us.
        web_client = hass.data[DOMAIN][entry.entry_id].get("_web_client")
        if web_client and not web_client.closed:
            await web_client.close()
        hass.data[DOMAIN].pop(entry.entry_id)
    else:
        _LOGGER.error(f"Unload failed: {unload_success}")
    return unload_success


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates - but only those. add_update_listener fires on ANY entry
    update, including entry.data changes like a token refresh persisting a new access/
    refresh token (see _refresh_token in utils.py) - that happens every 30-60 minutes
    and never needs a full reload. Only reload when entry.options itself actually
    changed from what we last saw."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if store is None:
        return
    current_options = dict(entry.options)
    if store.get("_last_options") == current_options:
        return
    store["_last_options"] = current_options
    await hass.config_entries.async_reload(entry.entry_id)


# A device whose next poll falls within this many seconds of the current tick is polled now,
# so ticks that arrive a moment early don't push it a whole tick later.
DUE_SLACK_SECONDS = 2


class SmartThingsFindCoordinator(DataUpdateCoordinator):
    """Class to manage fetching SmartThings Find data.

    Every device has its own schedule. A device inside one of Home Assistant's zones (Home,
    a workplace, ...) is polled every ``in_zone_interval`` seconds, any other device every
    ``update_interval`` seconds - typically the other way round from how much you care: a
    device that is somewhere it is expected to be needs checking far less often than one
    that has left. The coordinator itself ticks at the shorter of the two, and on each tick
    only the devices that are due are fetched; the rest keep their last data untouched.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        devices,
        update_interval: int,
        in_zone_interval: int,
        entry_id: str
    ):
        """Initialize the coordinator."""
        self.session = session
        self.devices = devices
        self.hass = hass
        self.entry_id = entry_id
        self.away_interval = update_interval
        self.in_zone_interval = in_zone_interval or update_interval
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=min(self.away_interval, self.in_zone_interval))
        )

    def schedule_device(self, dev_data: dict, tag_data: dict, now: float) -> None:
        """Work out when a device is due next, from where it is now.

        ``now`` is a time.monotonic() reading; a fetch that failed must not call this (it
        should be retried on the next tick instead, see _async_update_data).
        """
        in_zone = bool(tag_data.get('location_found')) and is_in_active_zone(
            self.hass, tag_data.get('used_loc'))
        interval = self.in_zone_interval if in_zone else self.away_interval
        dev_data['_poll_interval_s'] = interval
        dev_data['_next_poll'] = now + interval
        seconds_until_next_poll = max(0, dev_data['_next_poll'] - time.monotonic())
        tag_data['next_update_at'] = datetime.now(timezone.utc) + timedelta(
            seconds=seconds_until_next_poll
        )
        tag_data['next_update_mode'] = (
            'active'
            if dev_data.get('is_fmm')
            and dev_data.get('active_location_requested')
            and dev_data.get('web_dvce_id')
            and not dev_data.get('_active_location_unsupported')
            and not dev_data.get('_consecutive_fetch_failures')
            else 'passive'
        )
        # Shown on the location sensor, so you can see which schedule a device is on.
        tag_data['in_zone'] = in_zone
        tag_data['polling_interval'] = interval

    @staticmethod
    def _is_due(dev_data: dict, previous: dict, now: float) -> bool:
        if dev_data['device_id'] not in previous:
            return True
        next_poll = dev_data.get('_next_poll')
        return next_poll is None or now + DUE_SLACK_SECONDS >= next_poll

    async def _async_update_data(self):
        """Fetch data from SmartThings Find."""
        try:
            tags = {}
            previous = self.data or {}
            # Measured at the start of the cycle (not after each fetch) so a long cycle
            # doesn't make a device skip the tick it was meant to be polled on.
            now = time.monotonic()
            _LOGGER.debug("Updating locations...")
            for device in self.devices:
                dev_data = device['data']
                dev_name = dev_data.get('name') or dev_data['device_id']
                if not self._is_due(dev_data, previous, now):
                    tags[dev_data['device_id']] = previous[dev_data['device_id']]
                    _LOGGER.debug(
                        "[%s] Not due for another %ss", dev_name,
                        round(dev_data.get('_next_poll', now) - now))
                    continue
                tag_data = await get_device_location(self.hass, self.session, dev_data, self.entry_id)
                fetch_ok = bool(tag_data.get('update_success'))
                if fetch_ok:
                    dev_data['_consecutive_fetch_failures'] = 0
                    self.schedule_device(dev_data, tag_data, now)
                else:
                    # This cycle's fetch failed outright (session hiccup, transient HTTP
                    # error, ...) rather than just "no fresher position available yet".
                    # Retry on the very next tick, whatever interval the device is on: a
                    # failure must not wait out a long in-zone interval.
                    dev_data['_next_poll'] = 0
                    # Bridge over a SHORT run of these with the last known good position,
                    # so a one-off blip doesn't make the entity flash Unavailable - but
                    # only for a bounded number of consecutive cycles, and every cycle
                    # (masked or not) still goes through get_device_location's own retry
                    # with a full fresh session bootstrap. If it's still failing after
                    # MAX_STALE_FALLBACK_CYCLES in a row, stop masking it: a real,
                    # persistent problem should surface as Unavailable, not be hidden
                    # forever behind an increasingly stale position with no visible sign
                    # anything is wrong.
                    failures = dev_data.get('_consecutive_fetch_failures', 0) + 1
                    dev_data['_consecutive_fetch_failures'] = failures
                    prev_tag_data = previous.get(dev_data['device_id'])
                    failed_tag_data = tag_data
            failed_tag_data['next_update_at'] = datetime.now(timezone.utc) + timedelta(
                        seconds=min(self.away_interval, self.in_zone_interval)
                    )
                    failed_tag_data['next_update_mode'] = 'passive'
                    if (
                        failures <= MAX_STALE_FALLBACK_CYCLES
                        and prev_tag_data
                        and prev_tag_data.get('location_found')
                    ):
                        _LOGGER.debug(
                            "[%s] Fetch failed this cycle (%s/%s) - keeping last known position",
                            dev_name, failures, MAX_STALE_FALLBACK_CYCLES,
                        )
                        tag_data = dict(prev_tag_data)
                        tag_data['fetch_error'] = failed_tag_data.get(
                            'fetch_error', 'Location fetch failed'
                        )
                        tag_data['consecutive_fetch_failures'] = failures
                        tag_data['next_update_at'] = failed_tag_data['next_update_at']
                        tag_data['next_update_mode'] = failed_tag_data['next_update_mode']
                        tag_data['fmm_request_history'] = failed_tag_data.get(
                            'fmm_request_history', tag_data.get('fmm_request_history', [])
                        )
                    elif failures > MAX_STALE_FALLBACK_CYCLES:
                        _LOGGER.warning(
                            "[%s] Fetch has failed %s cycles in a row - showing as unavailable "
                            "instead of continuing to mask it with a stale position",
                            dev_name, failures,
                        )
                tags[dev_data['device_id']] = tag_data
                if tag_data.get('location_found'):
                    update_device_maps_link(
                        self.hass, dev_data['device_id'], tag_data.get('used_loc'))
                # Per-tag settings live in the SmartThings device API rather than the
                # Find API, so they need their own call.
                if dev_data.get('is_tracker'):
                    tag_data['ble_metadata'] = await get_device_ble_metadata(
                        self.hass,
                        self.session,
                        self.entry_id,
                        dev_data.get('st_device_id') or dev_data['device_id'],
                    )
            _LOGGER.debug("Fetched %s locations", len(tags))
            return tags
        except ConfigEntryAuthFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Error fetching data: {err}")
