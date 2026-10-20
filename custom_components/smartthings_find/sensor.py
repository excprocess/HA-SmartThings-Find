import logging
from datetime import datetime
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .utils import google_maps_url
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up SmartThings Find sensor entities."""
    devices = hass.data[DOMAIN][entry.entry_id]["devices"]
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    # Migrate the former binary Location Retrieval Error and remove the separate
    # text Status entity introduced in 8.1.10. The replacement text entity keeps the
    # error entity's unique ID, but now belongs to the sensor domain so its state can
    # display an HTTP/Samsung code directly.
    from homeassistant.helpers import entity_registry as er
    registry = er.async_get(hass)
    entities = []
    for device in devices:
        # Non-tracker (FMM: phone/wearable) battery only ever reflects whatever the
        # device last actively reported - same background-wake limitation as location,
        # confirmed against samsung-re-find's own findings ("battery depends on
        # device/backend response", no workaround documented) - so it's almost always
        # stale unless you've just opened the Samsung app yourself. Not worth exposing
        # as a sensor for these; keep it for trackers, where it's reliable.
        if device['data'].get("is_tracker"):
            entities += [DeviceBatterySensor(hass, coordinator, device)]
        # DeviceLocationSensor/DeviceMapsLinkSensor are format-agnostic (they just read
        # used_loc/location_found off the coordinator), and get_fmm_device_location now
        # returns that same shape for phones/wearables too - so these are no longer
        # tracker-only.
        if device['data'].get("is_tracker") or device['data'].get("is_fmm"):
            entities += [
                DeviceLocationSensor(hass, coordinator, device),
                DeviceMapsLinkSensor(hass, coordinator, device),
            ]
        if device['data'].get("is_fmm"):
            device_id = device['data'].get('device_id')
            for old_domain, old_unique_id in (
                ("binary_sensor", f"stf_stale_position_{device_id}"),
                ("sensor", f"stf_location_retrieval_status_{device_id}"),
            ):
                old_entity_id = registry.async_get_entity_id(
                    old_domain, DOMAIN, old_unique_id
                )
                if old_entity_id:
                    registry.async_remove(old_entity_id)
            entities += [DeviceLocationRetrievalErrorSensor(coordinator, device)]
    async_add_entities(entities)


class DeviceBatterySensor(CoordinatorEntity, SensorEntity):
    """Representation of a Device battery sensor."""

    def __init__(self, hass: HomeAssistant, coordinator, device):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_device_battery_{device_id}"
        self._attr_name = f"{name} Battery"
        self._state = None
        self.hass = hass
        self.device = device['data']
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self) -> bool:
        """
        Makes the entity show unavailable state if no data was received
        or there was an error during last update
        """
        tag_data = self.coordinator.data.get(self.device_id, {})
        if not tag_data:
            _LOGGER.info(f"battery sensor: tag_data none for '{self.name}'; rendering state unavailable")
            return False
        if not tag_data.get('update_success'):
            _LOGGER.info(f"Last update for battery sensor '{self.name}' failed; rendering state unavailable")
            return False
        return True
    
    @property
    def unit_of_measurement(self) -> str:
        return '%'
    
    @property
    def state(self):
        data = self.coordinator.data.get(self.device_id, {})
        return data.get('battery_level')


class DeviceLocationSensor(CoordinatorEntity, SensorEntity):
    """Exposes the raw coordinates of a device, plus a ready-made Google Maps link.

    The device_tracker entity can only ever have a zone as its state ('home',
    'not_home', ...), so the actual coordinates are surfaced here instead.
    """

    _attr_icon = "mdi:map-marker"

    def __init__(self, hass: HomeAssistant, coordinator, device):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_device_location_{device_id}"
        self._attr_name = f"{name} Location"
        self.hass = hass
        self.device = device['data']
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']

    def _used_loc(self) -> dict:
        tag_data = self.coordinator.data.get(self.device_id, {}) or {}
        if not tag_data.get('location_found'):
            return {}
        return tag_data.get('used_loc') or {}

    @property
    def available(self) -> bool:
        """Mirror the battery sensor: unavailable if the last update failed."""
        tag_data = self.coordinator.data.get(self.device_id, {})
        if not tag_data:
            _LOGGER.info(f"location sensor: tag_data none for '{self.name}'; rendering state unavailable")
            return False
        if not tag_data.get('update_success') and not tag_data.get('location_found'):
            _LOGGER.info(f"Last update for location sensor '{self.name}' failed; rendering state unavailable")
            return False
        return True

    @property
    def native_value(self):
        used_loc = self._used_loc()
        latitude = used_loc.get('latitude')
        longitude = used_loc.get('longitude')
        if latitude is None or longitude is None:
            return None
        return f"{latitude}, {longitude}"

    @property
    def extra_state_attributes(self):
        tag_data = self.coordinator.data.get(self.device_id, {}) or {}
        used_loc = self._used_loc()
        latitude = used_loc.get('latitude')
        longitude = used_loc.get('longitude')
        last_seen = used_loc.get('gps_date')
        last_seen_local = dt_util.as_local(last_seen) if last_seen else None
        next_update_at = tag_data.get('next_update_at')
        next_update_local = (
            dt_util.as_local(next_update_at)
            if isinstance(next_update_at, datetime)
            else None
        )
        attrs = {
            'latitude': latitude,
            'longitude': longitude,
            'gps_accuracy': used_loc.get('gps_accuracy'),
            'last_seen': last_seen,
            'last_seen_local': (
                last_seen_local.isoformat(timespec="seconds")
                if last_seen_local else None
            ),
            'last_location_retrieved_at': (
                dt_util.as_local(tag_data['last_location_retrieved_at']).isoformat(timespec="seconds")
                if isinstance(tag_data.get('last_location_retrieved_at'), datetime)
                else None
            ),
            'next_update_local': (
                next_update_local.isoformat(timespec="seconds")
                if next_update_local else None
            ),
            'next_update_mode': tag_data.get('next_update_mode'),
            'google_maps_url': google_maps_url(latitude, longitude),
        }
        # Only present for FMM devices (phone/wearable) - straight from the website's
        # own setLastSelect.do response, not reinterpreted (undocumented API).
        web_capabilities = tag_data.get('web_capabilities')
        if web_capabilities:
            attrs.update(web_capabilities)
        # Also FMM-only: whether this position can be actively refreshed at all (some
        # device types have no network connection of their own to push a fix to - see
        # binary_sensor.py's Stale Position sensor, which badges this visually) and
        # whether it's old enough to no longer be trusted as current.
        if 'active_location_supported' in tag_data:
            attrs['active_location_supported'] = tag_data.get('active_location_supported')
        if 'position_stale' in tag_data:
            attrs['position_stale'] = tag_data.get('position_stale')
        # Which schedule this device is on: inside a Home Assistant zone it is polled on the
        # in-zone interval, anywhere else on the general one (see the integration options).
        if 'polling_interval' in tag_data:
            attrs['in_zone'] = tag_data.get('in_zone')
            attrs['polling_interval'] = tag_data.get('polling_interval')
        return attrs


class DeviceMapsLinkSensor(CoordinatorEntity, SensorEntity):
    """Exposes the Google Maps link as an entity of its own.

    Listed under Diagnostic on the device page so the link is one click away
    instead of being buried in another entity's attributes.
    """

    _attr_icon = "mdi:map-search"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, coordinator, device):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_device_maps_link_{device_id}"
        self._attr_name = f"{name} Maps Link"
        self.hass = hass
        self.device = device['data']
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']

    @property
    def available(self) -> bool:
        tag_data = self.coordinator.data.get(self.device_id, {})
        return bool(tag_data) and bool(
            tag_data.get('update_success') or tag_data.get('location_found')
        )

    @property
    def native_value(self):
        tag_data = self.coordinator.data.get(self.device_id, {}) or {}
        if not tag_data.get('location_found'):
            return None
        used_loc = tag_data.get('used_loc') or {}
        url = google_maps_url(used_loc.get('latitude'), used_loc.get('longitude'))
        # A sensor state is capped at 255 characters; these URLs are ~70, but never
        # let an unexpected value break the whole entity.
        if url and len(url) > 255:
            return None
        return url


class DeviceLocationRetrievalErrorSensor(CoordinatorEntity, SensorEntity):
    """Expose OK or the current location error code as a readable state."""

    _attr_icon = "mdi:map-marker-alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device):
        super().__init__(coordinator)
        self.coordinator = coordinator
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_stale_position_{device_id}"
        self._attr_name = f"{name} Location Retrieval Error"
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']

    @staticmethod
    def _error_message(tag_data: dict) -> str | None:
        return (
            tag_data.get('fetch_error')
            or tag_data.get('active_location_error')
            or tag_data.get('position_error')
        )

    @staticmethod
    def _request_diagnostics(request_history: list) -> dict:
        """Extract the latest API response and a useful non-success code."""
        cycle_start = next(
            (index for index in range(len(request_history) - 1, -1, -1)
             if request_history[index].get('endpoint') == 'active_location'),
            0,
        )
        cycle_history = request_history[cycle_start:]
        last_api = next(
            (record for record in reversed(cycle_history) if record.get('api_call')),
            {},
        )
        endpoint = last_api.get('endpoint')
        outcome = last_api.get('outcome')
        status = last_api.get('http_status')
        try:
            http_status = int(status) if status is not None else None
        except (TypeError, ValueError):
            http_status = None

        error_code = None
        error_source = endpoint
        if http_status is not None and http_status >= 400:
            error_code = f"HTTP {http_status}"
        else:
            operations = last_api.get('returned_operations') or []
            location_operation = next(
                (operation for operation in reversed(operations)
                 if operation.get('type') == 'LOCATION'),
                None,
            )
            result_code = (
                location_operation.get('result_code') if location_operation else None
            ) or last_api.get('result_code')
            if result_code is not None and str(result_code) not in {'00', '1200'} and not (
                str(result_code) == '200' and outcome == 'pending'
            ):
                error_code = str(result_code)
            elif outcome in {'timeout', 'http_error', 'rejected'}:
                error_code = outcome
            else:
                for record in reversed(cycle_history):
                    if record.get('endpoint') != 'location_result':
                        continue
                    entries = record.get('location_entries') or []
                    location_entry = next(
                        (operation for operation in reversed(entries)
                         if operation.get('type') == 'LOCATION'),
                        None,
                    )
                    result_code = location_entry.get('result_code') if location_entry else None
                    if result_code is not None and str(result_code) not in {'00', '1200'}:
                        error_code = str(result_code)
                        error_source = 'location_result'
                    break

        return {
            'error_code': error_code,
            'error_endpoint': error_source if error_code else None,
            'error_http_status': http_status if error_code else None,
            'error_outcome': outcome if error_code else None,
            'last_api_endpoint': last_api.get('endpoint'),
            'last_api_http_status': last_api.get('http_status'),
            'last_api_result_code': last_api.get('result_code'),
            'last_api_outcome': last_api.get('outcome'),
        }

    @property
    def native_value(self):
        tag_data = self.coordinator.data.get(self.device_id, {}) or {}
        error = self._error_message(tag_data)
        if not error:
            return "OK"

        history = tag_data.get('fmm_request_history') or []
        error_code = self._request_diagnostics(history).get('error_code')
        return str(error_code or error)[:255]

    @property
    def extra_state_attributes(self):
        tag_data = self.coordinator.data.get(self.device_id, {}) or {}
        error = self._error_message(tag_data)
        attrs = {
            'error': error,
            'fetch_error': tag_data.get('fetch_error'),
            'active_location_error': tag_data.get('active_location_error'),
            'position_error': tag_data.get('position_error'),
            'consecutive_fetch_failures': tag_data.get('consecutive_fetch_failures', 0),
            'position_stale': bool(tag_data.get('position_stale')),
            'active_location_supported': tag_data.get('active_location_supported'),
            'fmm_last_interaction': (
                (tag_data.get('fmm_request_history') or [])[-1]
                if tag_data.get('fmm_request_history')
                else tag_data.get('fmm_last_interaction')
            ),
        }
        diagnostics = self._request_diagnostics(tag_data.get('fmm_request_history') or [])
        if not error:
            diagnostics.update({
                'error_code': None,
                'error_endpoint': None,
                'error_http_status': None,
                'error_outcome': None,
            })
        attrs.update(diagnostics)
        return attrs
