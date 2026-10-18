import logging
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .utils import get_power_saving_state

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up SmartThings Find binary sensor entities."""
    devices = hass.data[DOMAIN][entry.entry_id]["devices"]
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for device in devices:
        if device['data'].get("is_tracker"):
            entities += [DevicePowerSavingSensor(hass, coordinator, device)]
        if device['data'].get("is_fmm"):
            entities += [DeviceStalePositionSensor(hass, coordinator, device)]
    async_add_entities(entities)


class DevicePowerSavingSensor(BinarySensorEntity):
    """The tag's power saving mode ("Energiesparmodus"), read-only.

    Read from bleD2D.metadata.activeMode.mode on the SmartThings device API. This is a
    binary_sensor rather than a switch because only the read path is confirmed - the
    setting is written through the SmartThings app's own tracker-metadata update, which
    is not reproducible from here yet.
    """

    _attr_icon = "mdi:battery-heart-variant"

    def __init__(self, hass: HomeAssistant, coordinator, device):
        """Initialize the sensor."""
        self.coordinator = coordinator
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_power_saving_{device_id}"
        self._attr_name = f"{name} Power Saving"
        self.hass = hass
        self.device = device['data']
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']

    def _metadata(self) -> dict | None:
        tag_data = self.coordinator.data.get(self.device_id, {}) or {}
        return tag_data.get('ble_metadata')

    @property
    def available(self) -> bool:
        """Unavailable while the settings blob could not be read."""
        return get_power_saving_state(self._metadata()) is not None

    @property
    def is_on(self) -> bool | None:
        return get_power_saving_state(self._metadata())

    @property
    def extra_state_attributes(self):
        metadata = self._metadata() or {}
        firmware = metadata.get('firmware') or {}
        connection = metadata.get('lastKnownConnection') or {}
        vendor = metadata.get('vendor') or {}
        return {
            'firmware_version': firmware.get('version'),
            'model_name': vendor.get('modelName'),
            'battery_level': (metadata.get('battery') or {}).get('level'),
            'searching_status': metadata.get('searchingStatus'),
            'e2e_encryption': (metadata.get('e2eEncryption') or {}).get('enabled'),
            'remote_ring': (metadata.get('remoteRing') or {}).get('enabled'),
            'd2d_status': connection.get('d2dStatus'),
            'nearby': connection.get('nearby'),
        }


class DeviceStalePositionSensor(CoordinatorEntity, BinarySensorEntity):
    """Report errors while retrieving an FMM device's location.

    Position age remains informational on the Location sensor and does not turn this
    entity on. This entity instead surfaces failed requests, unreadable/encrypted data,
    and failed active-location requests.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:map-marker-alert"

    def __init__(self, hass: HomeAssistant, coordinator, device):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.coordinator = coordinator
        device_id = device['data'].get("device_id")
        name = device['data'].get("name") or device_id or "SmartThings Find"
        self._attr_unique_id = f"stf_stale_position_{device_id}"
        self._attr_name = f"{name} Location Retrieval Error"
        self.hass = hass
        self.device = device['data']
        self.device_id = device_id
        self._attr_device_info = device['ha_dev_info']

    def _tag_data(self) -> dict:
        return self.coordinator.data.get(self.device_id, {}) or {}

    @property
    def is_on(self) -> bool:
        tag_data = self._tag_data()
        return bool(self._error_message(tag_data))

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
        # A bounded history can contain several polling cycles. Restrict the
        # diagnostic code to the newest interaction so an old failure does not
        # remain visible after a later successful read.
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
                # Poll-result records are semantic summaries rather than requests; use
                # their LOCATION code when it explains an active/retrieval error.
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

        error_endpoint = error_source if error_code else None
        error_http_status = http_status if error_code else None
        error_outcome = outcome if error_code else None

        return {
            'error_code': error_code,
            'error_endpoint': error_endpoint,
            'error_http_status': error_http_status,
            'error_outcome': error_outcome,
            'last_api_endpoint': last_api.get('endpoint'),
            'last_api_http_status': last_api.get('http_status'),
            'last_api_result_code': last_api.get('result_code'),
            'last_api_outcome': last_api.get('outcome'),
        }

    @property
    def extra_state_attributes(self):
        tag_data = self._tag_data()
        request_history = tag_data.get('fmm_request_history') or []
        attrs = {
            'position_stale': bool(tag_data.get('position_stale')),
            'active_location_supported': tag_data.get('active_location_supported'),
            'error': self._error_message(tag_data),
            'fetch_error': tag_data.get('fetch_error'),
            'active_location_error': tag_data.get('active_location_error'),
            'position_error': tag_data.get('position_error'),
            'consecutive_fetch_failures': tag_data.get('consecutive_fetch_failures', 0),
            'fmm_last_interaction': (
                request_history[-1] if request_history
                else tag_data.get('fmm_last_interaction')
            ),
        }
        attrs.update(self._request_diagnostics(request_history))
        return attrs
