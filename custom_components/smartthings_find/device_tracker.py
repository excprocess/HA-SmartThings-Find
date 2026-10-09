import logging
from datetime import datetime
from homeassistant.components.device_tracker import TrackerEntity as DeviceTrackerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .utils import update_device_maps_link

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up SmartThings Find device tracker entities."""
    devices = hass.data[DOMAIN][entry.entry_id]["devices"]
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for device in devices:
        entities += [SmartThingsDeviceTracker(hass, coordinator, device)]
    async_add_entities(entities)

class SmartThingsDeviceTracker(DeviceTrackerEntity):
    """Representation of a SmartTag device tracker."""

    def __init__(self, hass: HomeAssistant, coordinator, device):
        """Initialize the device tracker."""

        self.coordinator = coordinator
        self.hass = hass
        self.device = device['data']
        self.device_id = self.device.get("device_id")

        name = self.device.get("name") or self.device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_device_tracker_{self.device_id}"
        self._attr_name = name
        self._attr_device_info = device['ha_dev_info']

        icon_url = self.device.get("icon_url")
        if not icon_url and self.device.get("web_device_type_code") == "HEARABLE":
            # Samsung's Find website uses a pair icon for Galaxy Buds, but its device
            # list currently leaves icon_url empty for this category.
            icon_url = "https://smartthingsfind.samsung.com/img/device_icon/attic_pair.svg"
        if icon_url:
            self._attr_entity_picture = icon_url
        self.async_update = coordinator.async_add_listener(self.async_write_ha_state)
    
    async def async_added_to_hass(self) -> None:
        """Set the device's Maps link as soon as the device registry entry exists.

        The coordinator's first refresh runs before the platforms are set up, so the
        link it writes is wiped again when this entity is added with its DeviceInfo.
        Re-apply it here, otherwise the device page has no link until the next poll.
        """
        await super().async_added_to_hass()
        data = self.coordinator.data.get(self.device_id, {}) or {}
        if data.get('location_found'):
            update_device_maps_link(self.hass, self.device_id, data.get('used_loc'))

    def async_write_ha_state(self):
        if not self.enabled:
            _LOGGER.debug(f"Ignoring state write request for disabled entity '{self.entity_id}'")
            return
        return super().async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return true if the device is available."""
        tag_data = self.coordinator.data.get(self.device_id, {})
        if not tag_data:
            _LOGGER.info(f"tag_data none for '{self.name}'; rendering state unavailable")
            return False
        if not tag_data.get('update_success'):
            _LOGGER.info(f"Last update for '{self.name}' failed; rendering state unavailable")
            return False
        return True
    
    @property
    def source_type(self) -> str:
        return SourceType.GPS
    
    @property
    def latitude(self):
        """Return the latitude of the device."""
        data = self.coordinator.data.get(self.device_id, {})
        if data.get('location_found'):
            return data.get('used_loc', {}).get('latitude', None)
        return None

    @property
    def longitude(self):
        """Return the longitude of the device."""
        data = self.coordinator.data.get(self.device_id, {})
        if data.get('location_found'):
            return data.get('used_loc', {}).get('longitude', None)
        return None
    
    @property
    def location_accuracy(self):
        """Return the location accuracy of the device.

        Home Assistant computes the zone with `zone_dist - location_accuracy`, so
        returning None raises a TypeError and leaves the entity without a state.
        The API omits `accuracy` for some devices, so fall back to 0.
        """
        data = self.coordinator.data.get(self.device_id, {})
        if data.get('location_found'):
            return data.get('used_loc', {}).get('gps_accuracy') or 0
        return 0

    # battery_level intentionally NOT overridden here - it's deprecated on
    # BaseTrackerEntity (removed in HA Core 2027.7) and battery is already exposed via
    # its own DeviceBatterySensor entity (sensor.py), so nothing is lost by dropping it.

    @property
    def extra_state_attributes(self):
        """Return useful device and polling details without repeating location data.

        Coordinates, accuracy, last-seen time, map links, zone schedule, and Find My
        Mobile diagnostics are exposed by their dedicated entities. Keep the remaining
        device/coordinator attributes for callers that use tracker metadata.
        """
        tag_data = self.coordinator.data.get(self.device_id, {}) or {}
        redundant = {
            "friendly_name",
            "dev_name",
            "icon_url",
            "is_tracker",
            "is_fmm",
            "original_name",
            "device_id",
            "fmm_device_id",
            "st_device_id",
            "owner_id",
            "sa_guid",
            "share_geolocation",
            "mutual_agreement",
            "web_dvce_id",
            "web_usr_id",
            "web_device_type_code",
            "used_loc",
            "latitude",
            "longitude",
            "gps_accuracy",
            "gps_date",
            "last_seen",
            "google_maps_url",
            "in_zone",
            "polling_interval",
            "web_capabilities",
            "active_location_supported",
            "position_stale",
            "fetch_error",
            "position_error",
            "active_location_error",
            "consecutive_fetch_failures",
            "next_update_at",
            "fmm_request_history",
            "next_poll",
        }
        attrs = {
            key: value
            for key, value in self.device.items()
            if key != "raw_device" and key not in redundant and not key.startswith("_")
        }
        attrs.update(
            {
                key: value
                for key, value in tag_data.items()
                if key != "raw_item" and key not in redundant and not key.startswith("_")
            }
        )
        used_loc = tag_data.get("used_loc") or {}
        last_seen = used_loc.get("gps_date")
        last_seen_local = (
            dt_util.as_local(last_seen) if isinstance(last_seen, datetime) else None
        )
        attrs["last_seen_local"] = (
            last_seen_local.strftime("%Y-%m-%d %H:%M:%S %Z %z")
            if last_seen_local
            else None
        )
        next_update_at = tag_data.get("next_update_at")
        next_update_local = (
            dt_util.as_local(next_update_at)
            if isinstance(next_update_at, datetime)
            else None
        )
        attrs["next_update_local"] = (
            next_update_local.isoformat(timespec="seconds")
            if next_update_local
            else None
        )
        attrs["next_update_mode"] = tag_data.get("next_update_mode")
        return attrs
