# SmartThings Find Integration for Home Assistant

Adds Samsung **SmartThings Find** to Home Assistant: SmartTags, and also the phones, tablets,
watches, earbuds and PCs registered in *Find My Mobile*.

This project is a fork in a long chain: [Vedeneb/HA-SmartThings-Find](https://github.com/Vedeneb/HA-SmartThings-Find)
(the original, archived by its author) → [herisanuadrian/HA-SmartThings-Find](https://github.com/herisanuadrian/HA-SmartThings-Find)
→ [Rain92/HA-SmartThings-Find](https://github.com/Rain92/HA-SmartThings-Find), which merged the OAuth 2.0/PKCE
login rework from [PixelShober/HA-SmartThings-Find](https://github.com/PixelShober/HA-SmartThings-Find).
See [Credits](#credits) for everything this fork builds on.

## What's new in 8.0

Previous versions could only *locate SmartTags*: phones, watches, earbuds and PCs showed up
(at best) without a position. Samsung serves those two families of devices through two different
backends, and the OAuth API used for tags has no access to the second one. Version 8 adds a second
channel for them:

- **Location for phones, tablets, watches, earbuds and PCs**, read from the SmartThings Find
  website API. No cookie to paste and nothing to renew by hand: the master token stored at login
  opens the website session automatically, and it is refreshed by itself.
- **Active Location** switch per device and an **Update Location** button.
- **Ring** for phones, as a toggle like the one SmartTags have.
- **Device icons**, taken from Samsung, for every device.
- Removed the two global *active mode* checkboxes from the options: it is now decided per device.
- Fixed the whole integration reloading each time a token was refreshed.
- Fixed devices going *Unavailable* after a single failed poll.

See the [changelog](CHANGELOG.md) for the details.

## Entities

**SmartTags**

| Entity | Notes |
|---|---|
| `device_tracker` | Location of the tag |
| `sensor` Battery | Battery level |
| `sensor` `<name> Location` | `latitude, longitude`, with `latitude`, `longitude`, `gps_accuracy`, `last_seen`, `google_maps_url` attributes |
| `sensor` `<name> Maps Link` | Google Maps URL (Diagnostic) |
| `switch` `<name> Ring` | Optimistic ring toggle, turns itself off after 120 s |
| `binary_sensor` `<name> Power Saving` | Read-only, with firmware, model, battery and connection state as attributes |

**Phones, tablets, watches, earbuds, PCs** (Find My Mobile devices)

| Entity | Notes |
|---|---|
| `device_tracker` | Location of the device |
| `sensor` `<name> Location` / `Maps Link` | Same as for tags, plus `telephony_support`, `wifi_only`, `cdma`, `ring_supported`, `offline_find_supported` attributes as Samsung reports them |
| `switch` `<name> Active Location` | Configuration entity. When on, every poll first asks the device for a fresh fix |
| `button` `<name> Update Location` | Asks the device for a fresh fix right now, and updates only that device |
| `switch` `<name> Ring` | Phones only (the website shows no ring for the other types) |

There is deliberately **no battery sensor** for these devices: Samsung only reports it when the
device itself answers, so it was almost always stale.

This integration does **not** let you react to button presses on a SmartTag. There are other ways to do that.

## ⚠️ Warning / Disclaimer

- **Undocumented API.** This is reverse engineered, and Samsung can change it at any time.
- **Not affiliated with Samsung or SmartThings.**
- Everything is limited to what the [SmartThings Find website](https://smartthingsfind.samsung.com/) offers.
  If something doesn't work there, it can't work here.

## What to expect from Find My Mobile devices

Read this before opening an issue, most of it comes from how Samsung's service behaves, not from the integration.

- **A device only has a fresh position if it reported one.** Without *Active Location*, the integration reads
  back the last position Samsung has, and a phone that isn't being asked won't necessarily send anything new
  in the background.
- **Active Location asks the device to report now**, which wakes it up: it costs battery and can light the
  screen of a phone. Enable it only on the devices where you need it, and leave the update interval
  reasonable. Each active request can take up to ~20 seconds, so with several devices switched on a poll cycle
  takes longer.
- **Earbuds and watches without their own connection can't be asked.** In testing, earbuds and a
  non-LTE watch answered *not supported* or never answered. The integration stops retrying a device that
  says *not supported* until the next restart. SmartTags reject it too, which is why the switch and button
  only exist for Find My Mobile devices.
- **Ring only works for phones**, and only if the phone can be reached.

## Notes on authentication

Login is an OAuth 2.0 flow with PKCE, the same one the official Samsung apps use, with a persistent session
that refreshes on its own. The same login also stores the account's master token, which is what opens the
website session used for phones, watches and the other Find My Mobile devices.

**Upgrading from another version of this integration:** entries created before version 8 don't have that master token.
SmartTags keep working, and the log will tell you the other devices need a new login. Go to
*Settings → Devices & services → SmartThings Find → Reconfigure* and log in again. You don't lose your entities.

## Notes on the location

A Home Assistant `device_tracker` can only ever have a zone as its state, so the tracker entity
shows `home`/`not_home`/a zone name rather than coordinates. The coordinates are available as
attributes on that entity, and additionally as a dedicated `sensor.<name>_location` entity whose
state is `latitude, longitude`.

Both carry a `google_maps_url` attribute. For a link you can actually click, the device page's
**Visit** button is kept pointing at the device's current coordinates, so it opens Google Maps at
the device's position. You will find it at the bottom of the **Device info** card, the top-left card
on the device's page (Settings → Devices & services → Devices → pick the device). It only appears
once a location has been received at least once.

Attributes themselves are not clickable, so for a link at the top level of a dashboard, add a
Markdown card. This one lists every device with a known position and needs no entity IDs - it picks up
new devices automatically and skips the ones that have not reported a location yet:

```yaml
type: markdown
title: SmartThings Find
content: |-
  {% for s in states.sensor
       | selectattr('attributes.google_maps_url', 'defined')
       | selectattr('attributes.google_maps_url', 'string')
       | sort(attribute='name') %}
  **[{{ s.name | replace(' Location', '') }}]({{ s.attributes.google_maps_url }})**
  {{ s.state }}{% if s.attributes.last_seen %} · seen {{ relative_time(s.attributes.last_seen) }} ago{% endif %}
  {% endfor %}
```

## Notes on power saving mode (SmartTags)

The tag's power saving mode is exposed read-only, as a `binary_sensor`. It is read from
`bleD2D.metadata.activeMode.mode` on the SmartThings device API (`0` = normal, `1` = power
saving). Writing it is not supported, and is unlikely to become supported: every per-tag endpoint of the
`chaser` API is 403 or 405 for our token. Toggle it in the SmartThings app; Home Assistant will show the new
state on the next poll.

## Renaming

Renaming a device in the SmartThings app propagates to the Home Assistant device and its entities
the next time the integration loads (restart Home Assistant, or reload it from the integration
page). A name you set yourself in Home Assistant always wins and is never overwritten.

## Diagnostics

The device page's **Download diagnostics** button returns the raw, untouched API payload for your
devices (`raw_device`), which is the only way to tell what a given account actually exposes - Samsung's
API is undocumented and differs between device generations and regions. Access tokens, account
identifiers and coordinates are redacted. This is the right thing to attach to a bug report.

## Notes on connection to SmartTags

Letting a SmartTag ring depends on a phone/tablet nearby which forwards your request via Bluetooth. If your
phone is not near your tag, you can't make it ring. The location should still update if any Galaxy device is nearby.

If ringing your tag does not work, first try to let it ring from the
[SmartThings Find website](https://smartthingsfind.samsung.com/). If it does not work from there, it can not work
from Home Assistant either. Ringing it from the SmartThings mobile app is not the same as the website, so always
use the website for your tests.

## Installation

### Using HACS

1. Add this repository as a custom repository in HACS (category *Integration*):
   `https://github.com/YOUR_GITHUB_USER/HA-SmartThings-Find`, or use this button:

   [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=YOUR_GITHUB_USER&repository=HA-SmartThings-Find&category=integration)

2. Search for "SmartThings Find" in HACS and install it
3. Restart Home Assistant
4. Continue with the [setup](#setup)

If you had this integration installed from another repository (for example `Rain92/HA-SmartThings-Find`),
remove that repository from HACS first (the integration files can stay where they are), then add this one and
update. Your config entry keeps working, see [authentication](#notes-on-authentication) for the re-login.

### Manual

1. Copy the `custom_components/smartthings_find` directory into your Home Assistant `config/custom_components/`
2. Restart Home Assistant
3. Continue with the [setup](#setup)

## Setup

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=smartthings_find)

1. Go to the Integrations page
2. Search for "SmartThings Find" (**do not confuse this with the built-in SmartThings integration!**)
3. Follow the on-screen wizard:
   - **Login**: open the link and log in to your Samsung account.
   - **Redirect**: after logging in, the browser tries to open a `ms-app://...` link. Cancel the external app prompt if it appears.
   - **Copy URL**: with the developer tools (F12), copy the full `ms-app://...` URL from Network or Console (not the visible error page URL).
   - **Paste**: paste the copied URL back into the Home Assistant dialog.
4. The integration checks the token and loads your devices.

The only option is the **update interval** (120 seconds by default; raise it if you use Active Location on several devices).

## Debugging

```yaml
logger:
  default: info
  logs:
    custom_components.smartthings_find: debug
```

## Contributing

Issues and pull requests are welcome. Logs at `debug` level and the diagnostics download make a bug report
much easier to act on.

## License

MIT, see [LICENSE](LICENSE). The license and copyright notice of the original project are kept, as it requires.

## Credits

- **[tomskra](https://github.com/tomskra)** and **[Vedeneb](https://github.com/Vedeneb)** for the original integration.
- **[herisanuadrian](https://github.com/herisanuadrian)** for keeping the fork alive after the original was archived.
- **[Rain92](https://github.com/Rain92)** for the fork this one is based on.
- **[PixelShober](https://github.com/PixelShober)** for the OAuth 2.0/PKCE login rework.
- **[charlesbel/samsung-re-find](https://github.com/charlesbel/samsung-re-find)** (MIT) for documenting how the master token opens a website
  session and how the website's `LOCATION` / `RING` operations work. The Find My Mobile support in version 8 follows that protocol.
- **[KieronQuinn](https://github.com/KieronQuinn)** for the [uTag](https://github.com/KieronQuinn/uTag) project and documenting the authentication protocol.

This is a third-party integration, not affiliated with or endorsed by Samsung or SmartThings.
