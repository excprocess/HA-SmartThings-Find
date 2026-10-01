# Changelog

## 8.0.0

### Added
- Location support for **Find My Mobile devices** (phones, tablets, watches, earbuds, PCs) via
  the SmartThings Find website API, bridged automatically from the account's master token
  (no cookie to paste, no manual renewal).
- Per-device **Active Location** switch (config entity) and **Update Location** button for those devices.
- **Ring** switch for phones (the website exposes no ring control for the other Find My Mobile types).
- Device icons for every device, sourced from Samsung.
- `telephony_support`, `wifi_only`, `cdma`, `ring_supported`, `offline_find_supported` attributes on
  the location sensor of Find My Mobile devices.
- A device that answers "not supported" to Active Location is not retried automatically until the next
  restart, or until its switch is turned off and back on.
- **Stale Position** binary sensor (Find My Mobile devices): turns on when a device's shown position
  either can't be actively refreshed at all (buds, most watches, a PC - no network connection of
  their own) or hasn't updated in over 3 poll cycles, so an old position is never mistaken for a
  current one. Uses the "problem" device class, so it shows as a warning badge with no dashboard
  setup needed. The location sensor also carries `active_location_supported` and `position_stale`
  as plain attributes, for anyone who wants to build their own dashboard logic instead.

### Changed
- The two global *active mode* checkboxes in the options are gone. Active Location is now a
  per-device switch instead.
- A failed poll no longer marks a device Unavailable immediately: the last known position is kept
  for up to 3 cycles before falling back to Unavailable, so a one-off network hiccup doesn't show up
  as a flicker.
- The battery sensor is no longer created for Find My Mobile devices: Samsung only updates it when the
  device itself is actively answering, so it was misleading more often than not.
- `TrackerEntity`/`SourceType` are imported from their non-deprecated path.

### Fixed
- Saving the integration's options (or a token silently refreshing in the background) no longer
  reloads the whole integration - only an actual options change does.
- `Ring`/`Update Location` controls actually register as entities: `button` was missing from the
  integration's platform list, so none of its entities ever loaded.
- Entity `entity_picture` no longer overrides the action icon on buttons/switches - the device photo
  was hiding the ring/location icon.

### Notes
- Config entries created before 8.0.0 do not have the master token this version needs for Find My
  Mobile devices. SmartTags are unaffected; a log line explains the re-login needed for the rest.
  See [Notes on authentication](README.md#notes-on-authentication).
