import logging
import time
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MAX_STALE_FALLBACK_CYCLES
from .utils import get_fmm_device_location, update_device_maps_link

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up SmartThings Find button entities."""
    devices = hass.data[DOMAIN][entry.entry_id]["devices"]
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for device in devices:
        if device["data"].get("is_fmm"):
            entities += [UpdateLocationButton(hass, entry.entry_id, device, coordinator)]
    async_add_entities(entities)

# NOTE: Ring/Stop Ring for SmartTags and for phones both live in switch.py now
# (RingSwitch / FmmRingSwitch) as toggles, matching how the website itself presents
# ring as an on/off control rather than two separate one-shot buttons. Platform.BUTTON
# was never registered in __init__.py's PLATFORMS list before this investigation, so
# none of these entities (tag ring included) actually loaded until that was fixed.


class UpdateLocationButton(ButtonEntity):
    """Forces a fresh GPS/network location fix for a phone/wearable (FMM device),
    instead of just waiting for the next passive poll to read back whatever it last
    reported on its own. Updates only this one device's coordinator data on completion,
    rather than triggering a full refresh of every device (which could take well over a
    minute if e.g. a BT-only watch is always timing out on its own LOCATION attempt)."""

    def __init__(self, hass: HomeAssistant, entry_id: str, device, coordinator):
        """Initialize the button."""
        self.hass = hass
        self.entry_id = entry_id
        self.device = device['data']
        self.coordinator = coordinator
        device_id = self.device.get("device_id")
        name = self.device.get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_update_location_button_{device_id}"
        self._attr_name = f"{name} Update Location"
        self._attr_icon = 'mdi:crosshairs-gps'
        self._attr_device_info = device['ha_dev_info']
        # No entity_picture here either - it would override the crosshairs icon above
        # with the device's photo, which is what was happening before this fix.

    async def async_press(self):
        """Handle the button press: force a fresh fix and push just this device's
        result into the coordinator, without waiting on every other device too."""
        dev_name = self.device.get("name") or self.device.get("device_id")
        device_id = self.device.get("device_id")
        session = self.hass.data[DOMAIN][self.entry_id]["session"]
        previous_request_ids = {
            id(request) for request in self.device.get('_fmm_request_history', [])
        }

        if not self.device.get("web_dvce_id"):
            raise HomeAssistantError(
                f"Website device id for '{dev_name}' not resolved yet - try again in a bit"
            )

        tag_data = await get_fmm_device_location(
            self.hass, session, self.device, self.entry_id, force_active=True
        )
        request_history = tag_data.get('fmm_request_history') or []
        new_api_calls = sum(
            1 for request in request_history
            if id(request) not in previous_request_ids and request.get('api_call')
        )
        self.device['_fmm_api_calls_total'] = (
            self.device.get('_fmm_api_calls_total', 0) + new_api_calls
        )
        tag_data['fmm_api_calls_last_update'] = new_api_calls
        tag_data['fmm_api_calls_total'] = self.device['_fmm_api_calls_total']
        previous_data = (self.coordinator.data or {}).get(device_id, {}) or {}
        previous_loc = previous_data.get('used_loc') or {}
        result_loc = tag_data.get('used_loc') or {}
        previous_time = previous_loc.get('gps_date')
        result_time = result_loc.get('gps_date')
        # Reaching Samsung successfully is not evidence that this press produced a
        # fresh fix: LOCATION=501 commonly returns coordinates from an older report.
        attempt_succeeded = bool(
            tag_data.get('update_success')
            and tag_data.get('location_found')
            and result_time
            and (not previous_time or result_time > previous_time)
        )

        if tag_data.get('update_success'):
            self.device['_consecutive_fetch_failures'] = 0
            # A manual refresh counts as this device's poll: its next scheduled one starts
            # from now, on whichever interval (in zone / elsewhere) its new position gives.
            self.coordinator.schedule_device(self.device, tag_data, time.monotonic())
        else:
            # Same bounded fallback as the coordinator's normal cycle (see __init__.py):
            # bridge over a short run of failures with the last known good position
            # rather than blanking the entity, but only up to MAX_STALE_FALLBACK_CYCLES -
            # a persistent problem should still surface as Unavailable eventually, not
            # hide forever behind an increasingly stale position.
            failures = self.device.get('_consecutive_fetch_failures', 0) + 1
            self.device['_consecutive_fetch_failures'] = failures
            prev_tag_data = (self.coordinator.data or {}).get(device_id)
            if failures <= MAX_STALE_FALLBACK_CYCLES and prev_tag_data and prev_tag_data.get('location_found'):
                _LOGGER.debug(
                    "[%s] Manual refresh failed (%s/%s) - keeping last known position",
                    dev_name, failures, MAX_STALE_FALLBACK_CYCLES,
                )
                tag_data = prev_tag_data
                # tag_data now has location_found=True from the fallback, but this
                # specific button press did NOT get a fresh fix - attempt_succeeded
                # (computed above, before the fallback) correctly stays False so the
                # message below doesn't lie about what just happened.

        if tag_data.get('location_found'):
            update_device_maps_link(self.hass, device_id, tag_data.get('used_loc'))

        # Push just this device's fresh result into the coordinator so listening entities
        # update immediately, instead of waiting for (or triggering) a full multi-device cycle.
        new_data = dict(self.coordinator.data or {})
        new_data[device_id] = tag_data
        self.coordinator.async_set_updated_data(new_data)
        used_loc = tag_data.get('used_loc') or {}
        if (
            self.coordinator.position_store
            and tag_data.get('update_success')
            and tag_data.get('location_found')
            and used_loc.get('latitude') is not None
            and used_loc.get('longitude') is not None
        ):
            gps_date = used_loc.get('gps_date')
            retrieved_at = tag_data.get('last_location_retrieved_at')
            saved_positions = dict(self.coordinator.saved_positions)
            saved_positions[device_id] = {
                'used_loc': {
                    **used_loc,
                    'gps_date': gps_date.isoformat() if gps_date else None,
                },
                'last_location_retrieved_at': (
                    retrieved_at.isoformat() if retrieved_at else None
                ),
            }
            try:
                await self.coordinator.position_store.async_save(saved_positions)
            except Exception:
                _LOGGER.warning("Unable to save refreshed location for %s", dev_name, exc_info=True)
            else:
                self.coordinator.saved_positions = saved_positions

        if not attempt_succeeded:
            last_position = (
                result_time.isoformat(timespec="seconds")
                if result_time else "unknown"
            )
            reason = (
                tag_data.get('fetch_error')
                or tag_data.get('active_location_error')
                or tag_data.get('position_error')
            )
            detail = f" Samsung reported: {reason}." if reason else ""
            _LOGGER.warning(
                "No newer location was confirmed for %s (latest fix: %s).%s "
                "Keeping the last known position.",
                dev_name, last_position, detail,
            )
            raise HomeAssistantError(
                f"No newer location was received for '{dev_name}'. The latest fix is "
                f"{last_position}.{detail} The last known position remains available."
            )
        _LOGGER.info("Successfully forced a fresh location fix for %s", dev_name)
