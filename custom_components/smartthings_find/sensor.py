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
from .utils import (
    DIRECTION_STATES,
    google_maps_url,
    is_active_connectable_device,
    is_buds_device,
    is_direction_supported_device,
    is_phone_device,
    resolve_device_battery_value,
)
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
        dev_data = device['data']
        device_id = dev_data.get('device_id')
        if is_buds_device(dev_data):
            # Galaxy Buds report independent Left and Right earbud battery channels in
            # `extra.left.battery` and `extra.right.battery` (charlesbel/samsung-re-find PR #2).
            old_batt_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"stf_device_battery_{device_id}"
            )
            if old_batt_id:
                registry.async_remove(old_batt_id)
            entities += [
                DeviceBudsBatterySensor(hass, coordinator, device, "left"),
                DeviceBudsBatterySensor(hass, coordinator, device, "right"),
            ]
        elif dev_data.get("is_tracker") or dev_data.get("is_fmm"):
            entities += [DeviceBatterySensor(hass, coordinator, device)]
        # DeviceLocationSensor/DeviceMapsLinkSensor are format-agnostic (they just read
        # used_loc/location_found off the coordinator), and get_fmm_device_location now
        # returns that same shape for phones/wearables too - so these are no longer
        # tracker-only.
        if device['data'].get("is_tracker") or device['data'].get("is_fmm"):
            entities += [
                DeviceLocationSensor(hass, coordinator, device),
                DeviceMapsLinkSensor(hass, coordinator, device),
                DeviceLastNewFixSensor(hass, coordinator, device),
            ]
        if is_active_connectable_device(device['data']):
            # Phones, tablets and PCs only: passive devices (Buds, watches, tags) are
            # offline whenever they are away from their phone, so this tells nothing there.
            entities += [DeviceConnectionSensor(hass, coordinator, device)]
        proximity_enabled = bool(
            getattr(coordinator, "proximity_home_zone", None)
            or getattr(coordinator, "proximity_work_zone", None)
        )
        if proximity_enabled and is_direction_supported_device(dev_data):
            entities += [DeviceDirectionSensor(hass, coordinator, device)]
        else:
            old_dir_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"stf_direction_{device_id}"
            )
            if old_dir_id:
                registry.async_remove(old_dir_id)
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
    """Representation of a Device battery sensor (SmartTags and non-Buds FMM devices)."""

    _attr_native_unit_of_measurement = "%"

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

    def _current_value(self):
        data = (self.coordinator.data or {}).get(self.device_id, {}) or {}
        return resolve_device_battery_value(self.device, data)

    @property
    def available(self) -> bool:
        """Keep the sensor available when the last update succeeded or a saved battery/location is held."""
        tag_data = (self.coordinator.data or {}).get(self.device_id, {})
        if not tag_data:
            return False
        return bool(
            tag_data.get("update_success")
            or tag_data.get("location_found")
            or self._current_value() is not None
        )

    @property
    def unit_of_measurement(self) -> str:
        return "%"

    @property
    def native_value(self):
        return self._current_value()

    @property
    def state(self):
        return self._current_value()


class DeviceBudsBatterySensor(CoordinatorEntity, SensorEntity):
    """Left or Right earbud battery sensor for Galaxy Buds (`extra.left.battery` / `extra.right.battery`)."""

    _attr_native_unit_of_measurement = "%"

    def __init__(self, hass: HomeAssistant, coordinator, device, side: str):
        super().__init__(coordinator)
        self.coordinator = coordinator
        self._side = "left" if str(side).lower() == "left" else "right"
        side_label = "Left" if self._side == "left" else "Right"
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_device_battery_{self._side}_{device_id}"
        self._attr_name = f"{name} {side_label} Battery"
        self._attr_icon = "mdi:earbuds"
        self.hass = hass
        self.device = device['data']
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT

    def _current_value(self):
        data = (self.coordinator.data or {}).get(self.device_id, {}) or {}
        return resolve_device_battery_value(self.device, data, side=self._side)

    @property
    def available(self) -> bool:
        tag_data = (self.coordinator.data or {}).get(self.device_id, {})
        if not tag_data:
            return False
        return bool(
            tag_data.get("update_success")
            or tag_data.get("location_found")
            or self._current_value() is not None
        )

    @property
    def unit_of_measurement(self) -> str:
        return "%"

    @property
    def native_value(self):
        return self._current_value()

    @property
    def state(self):
        return self._current_value()

    @property
    def extra_state_attributes(self):
        data = (self.coordinator.data or {}).get(self.device_id, {}) or {}
        last_batt = self.device.get("_last_battery") or {}
        combined = data.get("battery_level")
        if combined is None:
            combined = last_batt.get("battery_level")
        attrs = {"earbud_side": self._side}
        if combined is not None:
            attrs["combined_battery_level"] = combined
        return attrs


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
        if not tag_data.get('location_found') or tag_data.get('position_unknown'):
            # position_unknown: the opt-in "unknown after N failed attempts" rule hides the
            # position; the last known fix stays stored and returns with the next new fix.
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
        if tag_data.get('position_unknown'):
            attrs['position_unknown'] = True
        if tag_data.get('failed_attempts'):
            attrs['failed_attempts'] = tag_data.get('failed_attempts')
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
        if not tag_data.get('location_found') or tag_data.get('position_unknown'):
            return None
        used_loc = tag_data.get('used_loc') or {}
        url = google_maps_url(used_loc.get('latitude'), used_loc.get('longitude'))
        # A sensor state is capped at 255 characters; these URLs are ~70, but never
        # let an unexpected value break the whole entity.
        if url and len(url) > 255:
            return None
        return url


class DeviceLastNewFixSensor(CoordinatorEntity, SensorEntity):
    """When Home Assistant last received a genuinely newer position for this device.

    Native timestamp. It moves only when a read brought a fix whose own time is later than
    every fix seen before; an unchanged fix, a 501 or a pending request never moves it. It is
    different from the fix's own time (Samsung's `last_seen`) and from the last time
    coordinates were merely read (`last_location_retrieved_at`). Survives restarts.
    """

    _attr_icon = "mdi:map-clock"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, coordinator, device):
        super().__init__(coordinator)
        self.coordinator = coordinator
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_last_new_fix_{device_id}"
        self._attr_name = f"{name} Last New Position Request"
        self.hass = hass
        self.device = device['data']
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        value = self.device.get('_last_new_fix_at')
        return value if isinstance(value, datetime) else None


class DeviceConnectionSensor(CoordinatorEntity, SensorEntity):
    """Connection state of a phone/tablet/PC as reported by Samsung's CHECK_CONNECTION.

    States: wifi, mobile, online (connected, type not stated), offline, unknown. It is refreshed
    on the opted-in active cycle (Active Location switch on) and by the Ring pre-check; with
    the switch off it keeps the last answer. 'unknown' means the check could not be read - not
    that the device is offline. The attributes carry the sanitized structure of what the server
    returned (no coordinates, ids or session data) to show which information it provides.
    """

    _attr_icon = "mdi:lan-connect"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["wifi", "mobile", "online", "offline", "unknown"]
    _unrecorded_attributes = frozenset({"check_connection_structure", "server_data_structure"})
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, coordinator, device):
        super().__init__(coordinator)
        self.coordinator = coordinator
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_connection_{device_id}"
        self._attr_name = f"{name} Connection"
        self.hass = hass
        self.device = device['data']
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        connection = self.device.get('_connection') or {}
        state = connection.get('state')
        if state == "online" and is_phone_device(self.device):
            state = "mobile"
        return state if state in self._attr_options else "unknown"

    @property
    def extra_state_attributes(self):
        connection = self.device.get('_connection') or {}
        checked_at = connection.get('checked_at')
        return {
            'checked_at': (
                dt_util.as_local(checked_at).isoformat(timespec="seconds")
                if isinstance(checked_at, datetime) else None
            ),
            'source_field': connection.get('source_field'),
            'check_result_code': connection.get('result_code'),
            'samsung_answered': connection.get('samsung_answered'),
            'check_connection_structure': connection.get('structure'),
            'server_data_structure': self.device.get('_server_structure'),
        }


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


class DeviceDirectionSensor(CoordinatorEntity, SensorEntity):
    """Proximity and movement direction relative to configured Home / Work zones."""

    _attr_icon = "mdi:compass-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(DIRECTION_STATES)

    def __init__(self, hass: HomeAssistant, coordinator, device):
        super().__init__(coordinator)
        self.coordinator = coordinator
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_direction_{device_id}"
        self._attr_name = f"{name} Direction"
        self.hass = hass
        self.device = device['data']
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']

    def _proximity(self) -> dict:
        if is_active_connectable_device(self.device) and not self.device.get("active_location_requested"):
            return {}
        tag_data = (self.coordinator.data or {}).get(self.device_id, {}) or {}
        if tag_data.get("position_unknown"):
            return {}
        return tag_data.get("proximity") or {}

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        prox = self._proximity()
        state = prox.get("state")
        return state if state in self._attr_options else "unknown"

    @property
    def extra_state_attributes(self):
        prox = self._proximity()
        unit = prox.get("distance_unit") or getattr(self.coordinator, "proximity_distance_unit", "m")
        last_fix = prox.get("last_fix_at")
        return {
            "distance_unit": unit,
            f"distance_home_{unit}": prox.get("distance_home"),
            f"distance_work_{unit}": prox.get("distance_work"),
            f"delta_home_{unit}": prox.get("delta_home"),
            f"delta_work_{unit}": prox.get("delta_work"),
            "speed_kmh": prox.get("speed_kmh"),
            "bearing_deg": prox.get("bearing_deg"),
            "alignment_home": prox.get("alignment_home"),
            "alignment_work": prox.get("alignment_work"),
            "home_zone": prox.get("home_zone") or getattr(self.coordinator, "proximity_home_zone", None),
            "work_zone": prox.get("work_zone") or getattr(self.coordinator, "proximity_work_zone", None),
            "samples_used": prox.get("samples_used", 0),
            "last_fix_at": (
                dt_util.as_local(last_fix).isoformat(timespec="seconds")
                if isinstance(last_fix, datetime) else None
            ),
        }
