# Changelog

## 8.1.0

### Added
- **Different polling interval for devices inside a Home Assistant zone.** A device that is in a zone
  (Home, a workplace, ...) is polled on its own, usually much longer, interval, while every other device
  keeps the general one. New option **Update interval inside a zone**; `0` (the default) means no separate
  interval, so nothing changes unless you set it. Each device is scheduled on its own, so one at home and
  one away don't share a clock: devices that aren't due yet are not fetched at all, and Active Location
  requests follow the same schedule (a phone sitting at home is no longer woken every few minutes). A
  failed poll is retried on the next tick instead of waiting out a long in-zone interval, and the *Update
  Location* button restarts that device's schedule.
- `in_zone` and `polling_interval` attributes on the location sensor, to see which schedule a device is on.
- A `tests/` folder (run on every push by a new workflow): the parser, the per-device scheduling and the
  Find My Mobile read are tested against canned Samsung answers, with Home Assistant stubbed out.

### Fixed
- **A passive device (watch, PC, earbuds) could turn Unavailable, and stop showing new positions, when
  Samsung reported a new position for it.** The code that reads Samsung's list of positions assumed every
  entry is complete; one that wasn't (typically a position reported by nearby devices on the Find
  network, which can come without a vertical accuracy, with empty coordinates or with an unusual date)
  raised an error, the whole read was then counted as failed, and after three polls the device went
  Unavailable. Such an entry is now skipped, never marks the read as failed, and never replaces a valid
  position with an empty one; an entry whose only problem is a missing timestamp now uses the time Samsung
  recorded it instead of being dropped. What Samsung sends is only logged, once per device and shape, in
  a form that contains no coordinates (look for `Skipped a ... entry from Samsung`).
- When an answer holds no usable position at all, the last known one keeps being shown (and flagged by
  *Stale Position*) instead of the entity going blank.
- *Stale Position* judged "old" against the default interval rather than the one you configured; it now
  uses three polls of that device's own interval (never less than 30 minutes).
- The accuracy of a position no longer fails when only one of the two uncertainties is reported; this also
  applied to SmartTags.

### Notes
- If a device's positions never update even though the Samsung app shows new ones, Samsung may be
  returning them end-to-end encrypted, which can't be read here (the same limit as
  [samsung-re-find](https://github.com/charlesbel/samsung-re-find)). The log now says so, once, and the
  device stays available with its last known position.

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
