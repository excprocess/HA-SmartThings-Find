import logging
from homeassistant.components.device_tracker import TrackerEntity as DeviceTrackerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
        device_data = device['data']
        self.device_id = device_data.get("device_id")

        name = device_data.get("name") or self.device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_device_tracker_{self.device_id}"
        self._attr_name = name
        self._attr_device_info = device['ha_dev_info']

        icon_url = device_data.get("icon_url")
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
