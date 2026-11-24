import logging
from datetime import datetime
from homeassistant.components.device_tracker import TrackerEntity as DeviceTrackerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
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

class SmartThingsDeviceTracker(DeviceTrackerEntity, RestoreEntity):
    """Representation of a SmartTag device tracker."""

    def __init__(self, hass: HomeAssistant, coordinator, device):
        """Initialize the device tracker."""

        self.coordinator = coordinator
        self.hass = hass
        self.device = device['data']
        self.device_id = self.device.get("device_id")
        self._last_tag_data = None
        self._last_snapshot = None

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
    
    async def async_added_to_hass(self) -> None:
        """Set the device's Maps link as soon as the device registry entry exists.

        The coordinator's first refresh runs before the platforms are set up, so the
        link it writes is wiped again when this entity is added with its DeviceInfo.
        Re-apply it here, otherwise the device page has no link until the next poll.
        """
        await super().async_added_to_hass()
        data = self.coordinator.data.get(self.device_id, {}) or {}
        # A reload starts with an empty coordinator cache. If Samsung's first request
        # fails, keep the last coordinates Home Assistant had instead of making a
        # previously located device disappear until the next successful poll.
        if not data.get('location_found'):
            old_state = await self.async_get_last_state()
            old_attrs = old_state.attributes if old_state else {}
            latitude = old_attrs.get('latitude')
            longitude = old_attrs.get('longitude')
            if latitude is not None and longitude is not None:
                gps_date = old_attrs.get('last_seen') or old_attrs.get('last_seen_local')
                if isinstance(gps_date, str):
                    gps_date = dt_util.parse_datetime(gps_date)
                restored_loc = {
                    'latitude': latitude,
                    'longitude': longitude,
                    'gps_accuracy': old_attrs.get('gps_accuracy', 0),
                    'gps_date': gps_date,
                }
                data = dict(data)
                data.update({
                    'used_loc': restored_loc,
                    'location_found': True,
                    'restored_position': True,
                })
                self.coordinator.data[self.device_id] = data
        if data.get('location_found') and not data.get('position_unknown'):
            update_device_maps_link(self.hass, self.device_id, data.get('used_loc'))
        self._last_tag_data = self.coordinator.data.get(self.device_id)
        self._last_snapshot = self._tracker_snapshot(self._last_tag_data)
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    def _tracker_snapshot(self, tag_data: dict | None) -> tuple:
        """Fingerprint of the tracker's actual position and availability.

        Polling metadata (retrieval time, next poll time, API call counters) changes on
        every cycle even when Samsung returned the exact same old fix. Comparing only the
        real tracker state ensures `last_updated` and `last_changed` never advance unless
        a genuinely new position, availability change, or mode change occurred.
        """
        data = tag_data or {}
        available = bool(data) and bool(data.get("update_success") or data.get("location_found"))
        pos_unknown = bool(data.get("position_unknown"))
        used_loc = data.get("used_loc") or {}
        if data.get("location_found") and not pos_unknown:
            lat = used_loc.get("latitude")
            lon = used_loc.get("longitude")
            acc = used_loc.get("gps_accuracy") or 0
            gps_date = used_loc.get("gps_date")
        else:
            lat, lon, acc, gps_date = None, None, 0, None
        return (
            available,
            pos_unknown,
            lat,
            lon,
            acc,
            gps_date,
            bool(self.device.get("active_location_requested")),
            data.get("next_update_mode"),
        )

    def _handle_coordinator_update(self) -> None:
        """Write this tracker only when its real position or availability changed."""
        tag_data = self.coordinator.data.get(self.device_id)
        if tag_data is self._last_tag_data:
            return
        snapshot = self._tracker_snapshot(tag_data)
        self._last_tag_data = tag_data
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot
        self.async_write_ha_state()

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
        if not tag_data.get('update_success') and not tag_data.get('location_found'):
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
        if data.get('location_found') and not data.get('position_unknown'):
            return data.get('used_loc', {}).get('latitude', None)
        return None

    @property
    def longitude(self):
        """Return the longitude of the device."""
        data = self.coordinator.data.get(self.device_id, {})
        if data.get('location_found') and not data.get('position_unknown'):
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
        if data.get('location_found') and not data.get('position_unknown'):
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
            "name",
            "dev_name",
            "dev_id",
            "icon_url",
            "update_success",
            "location_found",
            "proximity",
            "is_tracker",
            "is_buds",
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
            "web_sub_type",
            "used_loc",
            "latitude",
            "longitude",
            "gps_accuracy",
            "gps_date",
            "last_seen",
            "last_seen_local",
            "last_location_retrieved_at",
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
            "fmm_last_interaction",
            "next_poll",
            "new_fix_received",
            "last_new_fix_at",
            "failed_attempts",
            "active_location_attempted",
            "active_attempt_samsung_outcome",
            "battery_level",
            "left_battery_level",
            "right_battery_level",
            "position_unknown",
            "restored_position",
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
            last_seen_local.isoformat(timespec="seconds")
            if last_seen_local
            else None
        )
        retrieved_at = tag_data.get("last_location_retrieved_at")
        retrieved_at_local = (
            dt_util.as_local(retrieved_at)
            if isinstance(retrieved_at, datetime)
            else None
        )
        attrs["last_location_retrieved_at"] = (
            retrieved_at_local.isoformat(timespec="seconds")
            if retrieved_at_local
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
