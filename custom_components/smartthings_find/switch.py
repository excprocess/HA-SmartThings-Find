import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, RING_TIMEOUT_SECONDS
from .utils import ring_device, stop_ring_device, format_ring_error, ring_fmm_device, stop_ring_fmm_device

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SmartThings Find switch entities."""
    devices = hass.data[DOMAIN][entry.entry_id]["devices"]
    entities = []
    for device in devices:
        if device["data"].get("is_tracker"):
            entities.append(RingSwitch(hass, entry.entry_id, device))
        if device["data"].get("is_fmm"):
            entities.append(ActiveLocationSwitch(hass, entry.entry_id, device))
            if device["data"].get("web_device_type_code") in ("PHONE", "PHONE DEVICE"):
                # Ring is only exposed by the website itself for phones - watches/buds/PC
                # don't show a ring control there either (buds/watches also empirically 501
                # on the LOCATION operation - no network connection of their own).
                entities.append(FmmRingSwitch(hass, entry.entry_id, device))
    async_add_entities(entities)


class RingSwitch(SwitchEntity):
    """Optimistic switch for starting/stopping a tracker ring."""

    _attr_assumed_state = True

    def __init__(self, hass: HomeAssistant, entry_id: str, device: dict) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.device = device["data"]
        device_id = self.device.get("device_id")
        name = self.device.get("name") or device_id or "SmartThings Find"

        self._attr_unique_id = f"stf_ring_switch_{device_id}"
        self._attr_name = f"{name} Ring"
        self._attr_icon = "mdi:bell-ring"
        self._attr_device_info = device["ha_dev_info"]
        # No entity_picture here on purpose: it takes priority over _attr_icon in the UI,
        # so setting it would replace the ring icon with the device's photo instead.

        self._is_on = False
        self._auto_off_cancel = None

    @property
    def is_on(self) -> bool:
        return self._is_on

    def _cancel_auto_off(self) -> None:
        if self._auto_off_cancel:
            self._auto_off_cancel()
            self._auto_off_cancel = None

    def _schedule_auto_off(self) -> None:
        self._cancel_auto_off()
        self._auto_off_cancel = async_call_later(
            self.hass,
            RING_TIMEOUT_SECONDS,
            self._handle_auto_off,
        )

    async def _handle_auto_off(self, _now) -> None:
        self._auto_off_cancel = None
        if not self._is_on:
            return
        await self._async_auto_off()

    async def _async_auto_off(self) -> None:
        session = self.hass.data[DOMAIN][self.entry_id]["session"]
        ok, err = await stop_ring_device(self.hass, session, self.entry_id, self.device)
        if not ok:
            _LOGGER.error(
                "Auto ring stop failed for %s: %s",
                self.device.get("name") or self.device.get("device_id"),
                err,
            )
        self._is_on = False
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        session = self.hass.data[DOMAIN][self.entry_id]["session"]
        ok, err = await ring_device(self.hass, session, self.entry_id, self.device)
        if ok:
            self._is_on = True
            self.async_write_ha_state()
            self._schedule_auto_off()
            return

        message = format_ring_error(err)
        _LOGGER.error(
            "Failed to ring device %s: %s",
            self.device.get("name") or self.device.get("device_id"),
            message,
        )
        raise HomeAssistantError(message)

    async def async_turn_off(self, **kwargs) -> None:
        self._cancel_auto_off()
        session = self.hass.data[DOMAIN][self.entry_id]["session"]
        ok, err = await stop_ring_device(self.hass, session, self.entry_id, self.device)
        if not ok:
            message = format_ring_error(err)
            _LOGGER.error(
                "Failed to stop ringing for %s: %s",
                self.device.get("name") or self.device.get("device_id"),
                message,
            )
            raise HomeAssistantError(message)
        self._is_on = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_auto_off()


class FmmRingSwitch(SwitchEntity):
    """Optimistic switch for starting/stopping the ring on a phone (FMM device), via the
    website's RING operation with status start/stop - the same toggle pattern as
    RingSwitch above, just over the legacy website API instead of the SmartThings OAuth
    tracker API."""

    _attr_assumed_state = True

    def __init__(self, hass: HomeAssistant, entry_id: str, device: dict) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.device = device["data"]
        device_id = self.device.get("device_id")
        name = self.device.get("name") or device_id or "SmartThings Find"

        self._attr_unique_id = f"stf_fmm_ring_switch_{device_id}"
        self._attr_name = f"{name} Ring"
        self._attr_icon = "mdi:volume-high"
        self._attr_device_info = device["ha_dev_info"]
        # No entity_picture here either - same reason as RingSwitch above.

        self._is_on = False
        self._auto_off_cancel = None

    @property
    def is_on(self) -> bool:
        return self._is_on

    def _cancel_auto_off(self) -> None:
        if self._auto_off_cancel:
            self._auto_off_cancel()
            self._auto_off_cancel = None

    def _schedule_auto_off(self) -> None:
        self._cancel_auto_off()
        self._auto_off_cancel = async_call_later(
            self.hass,
            RING_TIMEOUT_SECONDS,
            self._handle_auto_off,
        )

    async def _handle_auto_off(self, _now) -> None:
        self._auto_off_cancel = None
        if not self._is_on:
            return
        await self._async_auto_off()

    async def _async_auto_off(self) -> None:
        session = self.hass.data[DOMAIN][self.entry_id]["session"]
        success, reason = await stop_ring_fmm_device(self.hass, session, self.entry_id, self.device)
        if not success:
            _LOGGER.error(
                "Auto ring stop failed for %s: resultCode=%s",
                self.device.get("name") or self.device.get("device_id"),
                reason,
            )
        self._is_on = False
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        session = self.hass.data[DOMAIN][self.entry_id]["session"]
        dev_name = self.device.get("name") or self.device.get("device_id")
        if not self.device.get("web_dvce_id"):
            raise HomeAssistantError(f"Website device id for '{dev_name}' not resolved yet - try again in a bit")

        success, reason = await ring_fmm_device(self.hass, session, self.entry_id, self.device)
        if success:
            self._is_on = True
            self.async_write_ha_state()
            self._schedule_auto_off()
            return

        raise HomeAssistantError(f"Couldn't ring '{dev_name}' (resultCode={reason})")

    async def async_turn_off(self, **kwargs) -> None:
        self._cancel_auto_off()
        session = self.hass.data[DOMAIN][self.entry_id]["session"]
        dev_name = self.device.get("name") or self.device.get("device_id")
        success, reason = await stop_ring_fmm_device(self.hass, session, self.entry_id, self.device)
        if not success:
            raise HomeAssistantError(f"Couldn't stop ring for '{dev_name}' (resultCode={reason})")
        self._is_on = False
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_auto_off()


class ActiveLocationSwitch(SwitchEntity, RestoreEntity):
    """Per-device toggle for whether get_fmm_device_location() should force a fresh
    'LOCATION' push (dm/addOperation.do) before its normal passive read, every poll
    cycle (at the integration's configured update_interval) - replacing the old global
    'active mode' options that used to apply to every FMM device at once. State survives
    HA restarts via RestoreEntity, and is mirrored live onto the shared device dict
    (dev_data['active_location_requested']) so the coordinator's fetch functions, which
    only see that dict rather than this entity, can read it on every cycle."""

    def __init__(self, hass: HomeAssistant, entry_id: str, device: dict) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.device = device["data"]
        device_id = self.device.get("device_id")
        name = self.device.get("name") or device_id or "SmartThings Find"

        self._attr_unique_id = f"stf_active_location_switch_{device_id}"
        self._attr_name = f"{name} Active Location"
        self._attr_icon = "mdi:map-marker-radius"
        self._attr_device_info = device["ha_dev_info"]
        self._attr_entity_category = EntityCategory.CONFIG

        self._is_on = False

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        self._is_on = bool(last_state and last_state.state == "on")
        self.device["active_location_requested"] = self._is_on

    async def async_turn_on(self, **kwargs) -> None:
        self._is_on = True
        self.device["active_location_requested"] = True
        # Give it a fresh chance even if a past attempt this runtime got a "not
        # supported" (501) rejection - the user re-enabling it manually is a deliberate
        # signal to retry, not just another automatic cycle.
        self.device.pop('_active_location_unsupported', None)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._is_on = False
        self.device["active_location_requested"] = False
        self.async_write_ha_state()
