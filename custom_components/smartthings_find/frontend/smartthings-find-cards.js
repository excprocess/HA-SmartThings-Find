/**
 * SmartThings Find — Integrated Lovelace Cards (Samsung One UI & Material Design 3 / Material You)
 *
 * Cards registered:
 *  1. `custom:smartthings-find-device-card`   — Single-device map widget with Refresh, Ring/Stop Ring, and Location History
 *  2. `custom:smartthings-find-carousel-card` — Multi-device map widget with scrollable device bar and Location History popup
 *
 * Integrates with Home Assistant's native `<ha-map>`, Recorder history API (stay/move grouping
 * inspired by konewka17/timeline_card + Samsung Find Location History UI), and Material You Utilities
 * (`--md-sys-color-*`, `--md-sys-shape-*`, `--md-sys-motion-*`).
 */

const CARD_VERSION = "8.5.6";

// Crisp geometric One UI / MD3 SVG icons
const ICON_REFRESH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><polyline points="21 3 21 9 15 9"/></svg>`;
const ICON_RING_BELL = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/><path d="M2 8c0-2.2.7-4.3 2-6"/><path d="M22 8a10 10 0 0 0-2-6"/></svg>`;
const ICON_RING_STOP = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2.5" fill="currentColor"/></svg>`;
const ICON_CHEVRON_RIGHT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>`;
const ICON_CHEVRON_LEFT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 6 9 12 15 18"/></svg>`;
const ICON_HISTORY = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.95" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3.2 2"/></svg>`;
const ICON_CALENDAR = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="3"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>`;
const ICON_PIN_FILLED = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5a2.5 2.5 0 1 1 0-5 2.5 2.5 0 0 1 0 5z"/></svg>`;
const ICON_CLOSE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
const ICON_DEVICE_FALLBACK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-6-5.33-6-10a6 6 0 1 1 12 0c0 4.67-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/></svg>`;

let _haMapLoaderPromise = null;

function ensureHaMapLoaded() {
  if (customElements.get("ha-map")) return Promise.resolve();
  if (!_haMapLoaderPromise) {
    _haMapLoaderPromise = (async () => {
      try {
        if (window.loadCardHelpers) {
          const helpers = await window.loadCardHelpers();
          if (helpers && helpers.createCardElement) {
            helpers.createCardElement({ type: "map", entities: [] });
          }
        }
      } catch (_err) {
        // Ignore
      }
      if (!customElements.get("ha-map")) {
        await Promise.race([
          customElements.whenDefined("ha-map"),
          new Promise((resolve) => setTimeout(resolve, 4000)),
        ]);
      }
    })();
  }
  return _haMapLoaderPromise;
}

function getSmartThingsFindTrackers(hass) {
  if (!hass || !hass.states) return [];
  const regEntities = hass.entities || {};
  const result = [];
  for (const entityId of Object.keys(hass.states)) {
    if (!entityId.startsWith("device_tracker.")) continue;
    const reg = regEntities[entityId];
    const st = hass.states[entityId];
    const isStf =
      (reg && reg.platform === "smartthings_find") ||
      (st && st.attributes && (
        st.attributes.location_type === "FMM" ||
        st.attributes.location_type === "TRACKER" ||
        "last_seen_local" in st.attributes
      ));
    if (isStf) result.push(entityId);
  }
  return result.sort((a, b) => {
    const na = (hass.states[a]?.attributes?.friendly_name || a).toLowerCase();
    const nb = (hass.states[b]?.attributes?.friendly_name || b).toLowerCase();
    return na.localeCompare(nb);
  });
}

function findCompanionEntities(hass, trackerEntityId) {
  const out = {
    updateButton: null,
    ringSwitch: null,
    batterySensor: null,
    leftBatterySensor: null,
    rightBatterySensor: null,
    directionSensor: null,
    locationSensor: null,
    mapsLinkSensor: null,
  };
  if (!hass || !trackerEntityId) return out;

  const regEntities = hass.entities || {};
  const deviceId = regEntities[trackerEntityId]?.device_id;

  if (deviceId) {
    for (const [eid, entry] of Object.entries(regEntities)) {
      if (!entry || entry.device_id !== deviceId) continue;
      if (eid.startsWith("button.") && eid.endsWith("_update_location")) out.updateButton = eid;
      else if (eid.startsWith("switch.") && eid.endsWith("_ring")) out.ringSwitch = eid;
      else if (eid.startsWith("sensor.") && eid.endsWith("_left_battery")) out.leftBatterySensor = eid;
      else if (eid.startsWith("sensor.") && eid.endsWith("_right_battery")) out.rightBatterySensor = eid;
      else if (eid.startsWith("sensor.") && eid.endsWith("_battery")) out.batterySensor = eid;
      else if (eid.startsWith("sensor.") && eid.endsWith("_direction")) out.directionSensor = eid;
      else if (eid.startsWith("sensor.") && eid.endsWith("_location")) out.locationSensor = eid;
      else if (eid.startsWith("sensor.") && eid.endsWith("_maps_link")) out.mapsLinkSensor = eid;
    }
  }

  const slug = trackerEntityId.replace(/^device_tracker\./, "");
  const fallbackMap = [
    ["updateButton", `button.${slug}_update_location`],
    ["ringSwitch", `switch.${slug}_ring`],
    ["batterySensor", `sensor.${slug}_battery`],
    ["leftBatterySensor", `sensor.${slug}_left_battery`],
    ["rightBatterySensor", `sensor.${slug}_right_battery`],
    ["directionSensor", `sensor.${slug}_direction`],
    ["locationSensor", `sensor.${slug}_location`],
    ["mapsLinkSensor", `sensor.${slug}_maps_link`],
  ];
  for (const [key, candidate] of fallbackMap) {
    if (!out[key] && hass.states && hass.states[candidate]) out[key] = candidate;
  }
  return out;
}

function formatLastUpdated(hass, trackerState, locationState) {
  const raw =
    trackerState?.attributes?.last_seen_local ||
    locationState?.attributes?.last_seen_local ||
    locationState?.attributes?.last_seen ||
    trackerState?.last_updated;
  if (!raw) return "";
  const dt = new Date(raw);
  if (Number.isNaN(dt.getTime())) return String(raw);
  const locale = hass?.locale?.language || navigator.language || "en";
  try {
    return new Intl.DateTimeFormat(locale, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(dt);
  } catch (_e) {
    return dt.toLocaleString();
  }
}

function formatBatteryText(hass, companions) {
  if (!hass?.states) return "";
  const lb = companions.leftBatterySensor ? hass.states[companions.leftBatterySensor] : null;
  const rb = companions.rightBatterySensor ? hass.states[companions.rightBatterySensor] : null;
  const validNum = (st) => st && st.state !== "unknown" && st.state !== "unavailable" && st.state !== "";
  if (validNum(lb) || validNum(rb)) {
    const parts = [];
    if (validNum(lb)) parts.push(`L ${lb.state}%`);
    if (validNum(rb)) parts.push(`R ${rb.state}%`);
    return parts.join(" · ");
  }
  const b = companions.batterySensor ? hass.states[companions.batterySensor] : null;
  if (validNum(b)) return `${b.state}%`;
  return "";
}

function fireMoreInfo(node, entityId) {
  if (!entityId) return;
  node.dispatchEvent(new CustomEvent("hass-more-info", {
    detail: { entityId },
    bubbles: true,
    composed: true,
  }));
}

function isDarkMode(hass) {
  return Boolean(hass?.themes?.darkMode);
}

function haversineMeters(lat1, lon1, lat2, lon2) {
  const R = 6371000;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(Math.max(0, 1 - a)));
}

function formatTimeHM(dateObj) {
  const h = String(dateObj.getHours()).padStart(2, "0");
  const m = String(dateObj.getMinutes()).padStart(2, "0");
  return `${h}:${m}`;
}

function toLocalDateKey(dateObj) {
  const y = dateObj.getFullYear();
  const m = String(dateObj.getMonth() + 1).padStart(2, "0");
  const d = String(dateObj.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

// Lightweight Nominatim reverse geocode cache (sessionStorage) for timeline addresses
const _geoCacheMem = new Map();
async function reverseGeocode(lat, lon) {
  const key = `${lat.toFixed(4)},${lon.toFixed(4)}`;
  if (_geoCacheMem.has(key)) return _geoCacheMem.get(key);
  try {
    const cached = sessionStorage.getItem(`stf_geo_${key}`);
    if (cached) {
      _geoCacheMem.set(key, cached);
      return cached;
    }
  } catch (_e) {
    // Ignore storage errors
  }
  try {
    const url = `https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}&zoom=17&addressdetails=1`;
    const resp = await fetch(url, { headers: { Accept: "application/json" } });
    if (!resp.ok) return null;
    const data = await resp.json();
    const addr = data.address || {};
    const road = addr.road || addr.pedestrian || addr.footway || addr.neighbourhood || addr.suburb || "";
    const num = addr.house_number ? `, ${addr.house_number}` : "";
    const pc = addr.postcode ? `, ${addr.postcode}` : "";
    const city = addr.city || addr.town || addr.village || addr.municipality || "";
    const country = addr.country || "";
    let label = "";
    if (road) {
      label = `Near ${road}${num}${pc}${city ? ` ${city}` : ""}${country ? `, ${country}` : ""}`;
    } else if (data.display_name) {
      label = data.display_name.split(",").slice(0, 3).join(",");
    }
    if (label) {
      _geoCacheMem.set(key, label);
      try { sessionStorage.setItem(`stf_geo_${key}`, label); } catch (_e) {}
      return label;
    }
  } catch (_err) {
    // Offline or blocked
  }
  return null;
}

const SHARED_THEME_CSS = `
  :host {
    display: block;
    --stf-font: var(--paper-font-body1_-_font-family, "SamsungOne", "Roboto", system-ui, -apple-system, sans-serif);
    --stf-ease-standard: var(--md-sys-motion-easing-emphasized, cubic-bezier(0.2, 0, 0, 1));
  }

  /* ==================== AUTHENTIC SAMSUNG ONE UI 6/7/8 TOKENS ==================== */
  .stf-shell.design-one_ui {
    --stf-radius: 26px;
    --stf-accent: #387AFF;
    --stf-accent-soft: #EBF2FF;
    --stf-accent-text: #2264E6;
    --stf-on-accent: #FFFFFF;
    --stf-surface: #FFFFFF;
    --stf-surface-elevated: #FCFCFE;
    --stf-surface-muted: #F2F2F5;
    --stf-surface-glass: rgba(255, 255, 255, 0.94);
    --stf-on-surface: #111111;
    --stf-on-surface-muted: #6E6E73;
    --stf-outline: rgba(0, 0, 0, 0.06);
    --stf-outline-strong: rgba(0, 0, 0, 0.14);
    --stf-rail: #D8D8DC;
    --stf-scrim-rgb: 255, 255, 255;
    --stf-shadow: 0 2px 14px rgba(0, 0, 0, 0.07);
    --stf-btn-radius: 9999px;
    --stf-fab-radius: 9999px;
    --stf-item-radius: 9999px;
    --stf-item-active-bg: #F2F2F5;
    --stf-bubble-radius: 24px;
    --stf-bubble-border: 2px solid #387AFF;
    --stf-bubble-bg: rgba(255, 255, 255, 0.96);
    --stf-ring-bg: rgba(255, 255, 255, 0.94);
    --stf-ring-color: #1A1A1A;
    --stf-refresh-bg: rgba(255, 255, 255, 0.94);
    --stf-refresh-color: #1A1A1A;
  }

  .stf-shell.design-one_ui.dark {
    --stf-accent: #5C95FF;
    --stf-accent-soft: rgba(92, 149, 255, 0.20);
    --stf-accent-text: #8CB5FF;
    --stf-surface: #17171A;
    --stf-surface-elevated: #222226;
    --stf-surface-muted: #27272B;
    --stf-surface-glass: rgba(28, 28, 32, 0.92);
    --stf-on-surface: #FAFAFC;
    --stf-on-surface-muted: #9A9AA0;
    --stf-outline: rgba(255, 255, 255, 0.08);
    --stf-outline-strong: rgba(255, 255, 255, 0.16);
    --stf-rail: #3A3A40;
    --stf-scrim-rgb: 23, 23, 26;
    --stf-shadow: 0 4px 18px rgba(0, 0, 0, 0.40);
    --stf-item-active-bg: #27272B;
    --stf-bubble-border: 2px solid #5C95FF;
    --stf-bubble-bg: rgba(28, 28, 32, 0.94);
    --stf-ring-bg: rgba(34, 34, 38, 0.94);
    --stf-ring-color: #FAFAFC;
    --stf-refresh-bg: rgba(34, 34, 38, 0.94);
    --stf-refresh-color: #FAFAFC;
  }

  /* ==================== MATERIAL DESIGN 3 / MATERIAL YOU UTILITIES ==================== */
  .stf-shell.design-md3 {
    --stf-radius: var(--md-sys-shape-corner-extra-large, var(--ha-card-border-radius, 28px));
    --stf-accent: var(--md-sys-color-primary, var(--primary-color, #0B57D0));
    --stf-accent-soft: var(--md-sys-color-primary-container, #D3E3FD);
    --stf-accent-text: var(--md-sys-color-on-primary-container, #041E49);
    --stf-on-accent: var(--md-sys-color-on-primary, #FFFFFF);
    --stf-surface: var(--md-sys-color-surface-container-low, var(--ha-card-background, #F7F9FC));
    --stf-surface-elevated: var(--md-sys-color-surface-container-high, #ECEEF4);
    --stf-surface-muted: var(--md-sys-color-surface-container-highest, #E1E3E8);
    --stf-surface-glass: var(--md-sys-color-surface-container-high, #ECEEF4);
    --stf-on-surface: var(--md-sys-color-on-surface, var(--primary-text-color, #1B1B1F));
    --stf-on-surface-muted: var(--md-sys-color-on-surface-variant, var(--secondary-text-color, #44474F));
    --stf-outline: var(--md-sys-color-outline-variant, rgba(0, 0, 0, 0.12));
    --stf-outline-strong: var(--md-sys-color-outline, rgba(0, 0, 0, 0.24));
    --stf-rail: var(--md-sys-color-outline-variant, rgba(0, 0, 0, 0.16));
    --stf-scrim-rgb: 243, 246, 252;
    --stf-shadow: var(--ha-card-box-shadow, none);
    --stf-btn-radius: var(--md-sys-shape-corner-full, 9999px);
    --stf-fab-radius: var(--md-sys-shape-corner-large, 16px);
    --stf-item-radius: 18px;
    --stf-item-active-bg: var(--md-sys-color-secondary-container, #D3E3FD);
    --stf-bubble-radius: 22px;
    --stf-bubble-border: 1.5px solid var(--md-sys-color-outline-variant, rgba(0, 0, 0, 0.12));
    --stf-bubble-bg: var(--md-sys-color-surface-container-high, #ECEEF4);
    --stf-ring-bg: var(--md-sys-color-primary-container, #D3E3FD);
    --stf-ring-color: var(--md-sys-color-on-primary-container, #041E49);
    --stf-refresh-bg: var(--md-sys-color-secondary-container, #D3E3FD);
    --stf-refresh-color: var(--md-sys-color-on-secondary-container, #041E49);
  }

  .stf-shell.design-md3.dark {
    --stf-scrim-rgb: 20, 22, 27;
  }

  ha-card.stf-shell {
    position: relative;
    overflow: hidden;
    border-radius: var(--stf-radius);
    background: var(--stf-surface);
    color: var(--stf-on-surface);
    border: 1px solid var(--stf-outline);
    box-shadow: var(--stf-shadow);
    font-family: var(--stf-font);
    padding: 0;
    transition: background-color 220ms var(--stf-ease-standard), border-color 220ms var(--stf-ease-standard);
  }

  .map-wrap {
    position: relative;
    width: 100%;
    overflow: hidden;
    background: var(--stf-surface-muted);
  }

  .map-wrap ha-map {
    display: block;
    width: 100%;
    height: 100%;
  }

  .hud-layer {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 400;
  }

  .icon-btn {
    pointer-events: auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--stf-outline-strong);
    cursor: pointer;
    padding: 0;
    outline: none;
    transition: transform 160ms var(--stf-ease-standard), background-color 180ms var(--stf-ease-standard), box-shadow 180ms var(--stf-ease-standard), border-color 180ms var(--stf-ease-standard);
    -webkit-tap-highlight-color: transparent;
  }

  .icon-btn:active {
    transform: scale(0.92);
  }

  .icon-btn svg {
    width: 20px;
    height: 20px;
    display: block;
  }

  .design-md3 .icon-btn {
    border-color: transparent;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.16);
  }

  .design-md3 .icon-btn:hover {
    filter: brightness(0.96);
  }

  .spin svg {
    animation: stf-spin 0.9s linear infinite;
  }

  @keyframes stf-spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  .empty-state {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 140px;
    padding: 16px;
    text-align: center;
    color: var(--stf-on-surface-muted);
    font-size: 14px;
  }
`;

/* ============================================================================================
 * LOCATION HISTORY POPUP MODAL (`smartthings-find-history-dialog`)
 * Faithful to Samsung Find Location History (One UI) + Material Design 3 (Material You)
 * Uses Home Assistant Recorder history API with Stay & Move grouping (konewka17/timeline_card)
 * ============================================================================================ */
class SmartThingsFindHistoryDialog extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._entityId = null;
    this._design = "one_ui";
    this._selectedDate = new Date();
    this._stops = [];
    this._selectedIdx = 0;
    this._loading = false;
    this._mapEl = null;
    this._onKeyDown = (e) => {
      if (e.key === "Escape") this.close();
    };
  }

  open(hass, entityId, design = "one_ui") {
    this._hass = hass;
    this._entityId = entityId;
    this._design = design === "md3" ? "md3" : "one_ui";
    this._selectedDate = new Date();
    this._stops = [];
    this._selectedIdx = 0;
    this._renderShell();
    document.body.appendChild(this);
    window.addEventListener("keydown", this._onKeyDown);
    this._loadDayHistory();
  }

  close() {
    window.removeEventListener("keydown", this._onKeyDown);
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  _renderShell() {
    const dark = isDarkMode(this._hass);
    const st = this._hass?.states?.[this._entityId];
    const devName = st?.attributes?.friendly_name || this._entityId || "Device";

    this.shadowRoot.innerHTML = `
      <style>
        ${SHARED_THEME_CSS}
        :host {
          position: fixed;
          inset: 0;
          z-index: 9999;
          display: flex;
          align-items: center;
          justify-content: center;
        }
        .backdrop {
          position: fixed;
          inset: 0;
          background: rgba(0, 0, 0, 0.56);
          backdrop-filter: blur(4px);
          animation: stf-fade-in 180ms ease;
        }
        @keyframes stf-fade-in {
          from { opacity: 0; }
          to { opacity: 1; }
        }
        .dialog {
          position: relative;
          z-index: 2;
          width: min(460px, 94vw);
          height: min(760px, 90vh);
          display: flex;
          flex-direction: column;
          overflow: hidden;
          border-radius: var(--stf-radius);
          background: var(--stf-surface);
          color: var(--stf-on-surface);
          border: 1px solid var(--stf-outline);
          box-shadow: 0 18px 48px rgba(0, 0, 0, 0.36);
          font-family: var(--stf-font);
          animation: stf-pop-in 220ms var(--stf-ease-standard);
        }
        @keyframes stf-pop-in {
          from { opacity: 0; transform: translateY(16px) scale(0.97); }
          to { opacity: 1; transform: translateY(0) scale(1); }
        }
        .dlg-header {
          display: flex;
          align-items: center;
          gap: 10px;
          padding: 12px 14px;
          background: var(--stf-surface);
          border-bottom: 1px solid var(--stf-outline);
          flex: 0 0 auto;
        }
        .dlg-back, .dlg-close {
          width: 38px;
          height: 38px;
          border-radius: 9999px;
          border: none;
          background: transparent;
          color: var(--stf-on-surface);
          display: inline-flex;
          align-items: center;
          justify-content: center;
          cursor: pointer;
          transition: background-color 150ms ease;
        }
        .dlg-back:hover, .dlg-close:hover {
          background: var(--stf-surface-muted);
        }
        .dlg-back svg, .dlg-close svg {
          width: 22px;
          height: 22px;
        }
        .dlg-titles {
          flex: 1;
          min-width: 0;
        }
        .dlg-title {
          font-size: 18px;
          font-weight: 700;
          line-height: 1.2;
          letter-spacing: -0.01em;
        }
        .dlg-subtitle {
          font-size: 13px;
          color: var(--stf-on-surface-muted);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          margin-top: 2px;
        }

        .dlg-map-wrap {
          position: relative;
          flex: 0 0 42%;
          min-height: 200px;
          background: var(--stf-surface-muted);
          overflow: hidden;
        }
        .dlg-map-wrap ha-map {
          display: block;
          width: 100%;
          height: 100%;
        }
        .dlg-map-fade {
          position: absolute;
          left: 0;
          right: 0;
          bottom: 0;
          height: 28px;
          pointer-events: none;
          z-index: 400;
          background: linear-gradient(to top, var(--stf-surface) 0%, transparent 100%);
          display: none;
        }
        .design-md3 .dlg-map-fade {
          display: block;
        }

        /* Bottom Sheet / Day Switcher (One UI rounded sheet top) */
        .dlg-sheet {
          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
          background: var(--stf-surface);
          border-top-left-radius: 24px;
          border-top-right-radius: 24px;
          margin-top: -16px;
          position: relative;
          z-index: 410;
          box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.08);
        }
        .design-md3 .dlg-sheet {
          margin-top: 0;
          border-radius: 0;
          box-shadow: none;
        }
        .day-bar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 12px 16px;
          border-bottom: 1px solid var(--stf-outline);
          flex: 0 0 auto;
        }
        .day-nav-btn {
          width: 36px;
          height: 36px;
          border-radius: 9999px;
          border: none;
          background: transparent;
          color: var(--stf-on-surface);
          display: inline-flex;
          align-items: center;
          justify-content: center;
          cursor: pointer;
          transition: background-color 150ms ease, opacity 150ms ease;
        }
        .design-md3 .day-nav-btn {
          background: var(--stf-surface-elevated);
        }
        .day-nav-btn:hover:not(:disabled) {
          background: var(--stf-surface-muted);
        }
        .day-nav-btn:disabled {
          opacity: 0.28;
          cursor: default;
        }
        .day-nav-btn svg {
          width: 20px;
          height: 20px;
        }
        .day-label-wrap {
          position: relative;
          display: inline-flex;
          align-items: center;
          gap: 8px;
          font-size: 15.5px;
          font-weight: 600;
          color: var(--stf-on-surface);
          cursor: pointer;
          padding: 6px 12px;
          border-radius: 9999px;
        }
        .day-label-wrap:hover {
          background: var(--stf-surface-muted);
        }
        .day-label-wrap svg {
          width: 18px;
          height: 18px;
          color: var(--stf-on-surface-muted);
        }
        .day-input {
          position: absolute;
          inset: 0;
          opacity: 0;
          cursor: pointer;
        }

        /* Timeline list (One UI & MD3) */
        .timeline-list {
          flex: 1;
          overflow-y: auto;
          padding: 14px 16px 20px;
          display: flex;
          flex-direction: column;
        }
        .tl-row {
          display: grid;
          grid-template-columns: 52px 26px 1fr;
          align-items: stretch;
          gap: 6px;
          cursor: pointer;
          -webkit-tap-highlight-color: transparent;
        }
        .tl-time {
          font-size: 12px;
          font-weight: 500;
          color: var(--stf-on-surface-muted);
          font-variant-numeric: tabular-nums;
          display: flex;
          flex-direction: column;
          justify-content: center;
          align-items: center;
          text-align: center;
          line-height: 1.25;
          padding: 6px 0;
        }
        .tl-row.active .tl-time {
          color: var(--stf-accent);
          font-weight: 600;
        }
        .tl-rail-col {
          position: relative;
          display: flex;
          align-items: center;
          justify-content: center;
        }
        .tl-rail-col::before {
          content: "";
          position: absolute;
          top: 0;
          bottom: 0;
          width: 2px;
          background: var(--stf-rail);
        }
        .tl-row:first-child .tl-rail-col::before {
          top: 50%;
        }
        .tl-row:last-child .tl-rail-col::before {
          bottom: 50%;
        }
        .tl-row:only-child .tl-rail-col::before {
          display: none;
        }
        .tl-dot {
          position: relative;
          z-index: 2;
          width: 10px;
          height: 10px;
          border-radius: 9999px;
          background: var(--stf-surface);
          border: 2px solid var(--stf-on-surface-muted);
        }
        .tl-pin {
          position: relative;
          z-index: 2;
          width: 22px;
          height: 22px;
          color: var(--stf-accent);
          background: var(--stf-surface);
          border-radius: 9999px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
        }
        .tl-pin svg {
          width: 20px;
          height: 20px;
        }
        .tl-card {
          position: relative;
          margin: 6px 0 6px 6px;
          padding: 12px 14px;
          border-radius: 16px;
          background: var(--stf-surface-muted);
          color: var(--stf-on-surface);
          transition: background-color 180ms var(--stf-ease-standard), color 180ms var(--stf-ease-standard);
        }
        /* One UI speech notch on left of timeline card */
        .design-one_ui .tl-card::before {
          content: "";
          position: absolute;
          left: -6px;
          top: 50%;
          width: 12px;
          height: 12px;
          background: inherit;
          transform: translateY(-50%) rotate(45deg);
          border-radius: 2px;
        }
        .tl-row.active .tl-card {
          background: var(--stf-accent-soft);
          color: var(--stf-accent-text);
        }
        .tl-addr {
          font-size: 14px;
          font-weight: 500;
          line-height: 1.35;
          word-break: break-word;
        }
        .tl-row.active .tl-addr {
          font-weight: 600;
        }
        .tl-meta {
          margin-top: 4px;
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 8px;
          font-size: 11.5px;
          opacity: 0.78;
        }
        .tl-maps-link {
          color: inherit;
          text-decoration: underline;
          font-weight: 600;
        }
        .tl-empty {
          padding: 36px 16px;
          text-align: center;
          color: var(--stf-on-surface-muted);
          font-size: 14px;
        }
      </style>
      <div class="backdrop"></div>
      <div class="dialog stf-shell design-${this._design}${dark ? " dark" : ""}">
        <div class="dlg-header">
          <button class="dlg-back" aria-label="Close">${ICON_CHEVRON_LEFT}</button>
          <div class="dlg-titles">
            <div class="dlg-title">Location history</div>
            <div class="dlg-subtitle">${devName}</div>
          </div>
          <button class="dlg-close" aria-label="Close">${ICON_CLOSE}</button>
        </div>
        <div class="dlg-map-wrap">
          <div class="dlg-map-fade"></div>
        </div>
        <div class="dlg-sheet">
          <div class="day-bar">
            <button class="day-nav-btn btn-prev" aria-label="Previous day">${ICON_CHEVRON_LEFT}</button>
            <label class="day-label-wrap">
              <span class="day-label-text"></span>
              ${ICON_CALENDAR}
              <input class="day-input" type="date" />
            </label>
            <button class="day-nav-btn btn-next" aria-label="Next day">${ICON_CHEVRON_RIGHT}</button>
          </div>
          <div class="timeline-list"></div>
        </div>
      </div>
    `;

    this.shadowRoot.querySelector(".backdrop").addEventListener("click", () => this.close());
    this.shadowRoot.querySelector(".dlg-back").addEventListener("click", () => this.close());
    this.shadowRoot.querySelector(".dlg-close").addEventListener("click", () => this.close());

    this._btnPrev = this.shadowRoot.querySelector(".btn-prev");
    this._btnNext = this.shadowRoot.querySelector(".btn-next");
    this._dayLabel = this.shadowRoot.querySelector(".day-label-text");
    this._dayInput = this.shadowRoot.querySelector(".day-input");
    this._listEl = this.shadowRoot.querySelector(".timeline-list");
    this._mapWrap = this.shadowRoot.querySelector(".dlg-map-wrap");

    this._btnPrev.addEventListener("click", () => this._shiftDay(-1));
    this._btnNext.addEventListener("click", () => this._shiftDay(1));
    this._dayInput.addEventListener("change", (e) => {
      if (e.target.value) {
        const [y, m, d] = e.target.value.split("-").map(Number);
        this._selectedDate = new Date(y, m - 1, d);
        this._loadDayHistory();
      }
    });

    ensureHaMapLoaded().then(() => {
      if (!this._mapEl && this._mapWrap) {
        const map = document.createElement("ha-map");
        this._mapEl = map;
        this._mapWrap.insertBefore(map, this._mapWrap.firstChild);
        this._updateMap();
      }
    });

    this._updateDayHeader();
  }

  _shiftDay(delta) {
    const next = new Date(this._selectedDate);
    next.setDate(next.getDate() + delta);
    const today = new Date();
    today.setHours(23, 59, 59, 999);
    if (next > today) return;
    this._selectedDate = next;
    this._loadDayHistory();
  }

  _updateDayHeader() {
    const locale = this._hass?.locale?.language || navigator.language || "en";
    try {
      this._dayLabel.textContent = new Intl.DateTimeFormat(locale, {
        weekday: "short",
        month: "short",
        day: "numeric",
        year: "numeric",
      }).format(this._selectedDate);
    } catch (_e) {
      this._dayLabel.textContent = this._selectedDate.toDateString();
    }
    const key = toLocalDateKey(this._selectedDate);
    const todayKey = toLocalDateKey(new Date());
    this._dayInput.value = key;
    this._dayInput.max = todayKey;
    this._btnNext.disabled = key >= todayKey;
  }

  async _loadDayHistory() {
    this._updateDayHeader();
    this._loading = true;
    this._stops = [];
    this._selectedIdx = 0;
    this._listEl.innerHTML = `<div class="tl-empty">Loading history...</div>`;

    const start = new Date(this._selectedDate);
    start.setHours(0, 0, 0, 0);
    const end = new Date(this._selectedDate);
    end.setHours(23, 59, 59, 999);

    try {
      const path = `history/period/${encodeURIComponent(start.toISOString())}?filter_entity_id=${encodeURIComponent(this._entityId)}&end_time=${encodeURIComponent(end.toISOString())}`;
      const data = await this._hass.callApi("GET", path);
      const rawStates = Array.isArray(data) && Array.isArray(data[0]) ? data[0] : [];
      this._stops = this._groupIntoStops(rawStates);
    } catch (_err) {
      this._stops = [];
    } finally {
      this._loading = false;
    }

    this._renderTimeline();
    this._updateMap();
    this._hydrateAddresses();
  }

  _groupIntoStops(rawStates) {
    // Stay & Move grouping inspired by konewka17/timeline_card:
    // Collapse points within 120m (or same zone) into a single Stay interval.
    const points = [];
    for (const s of rawStates) {
      const attrs = s.attributes || {};
      const lat = Number(attrs.latitude);
      const lon = Number(attrs.longitude);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const rawTime = attrs.last_seen_local || s.last_updated || s.last_changed;
      const dt = new Date(rawTime);
      if (Number.isNaN(dt.getTime())) continue;
      const zoneState = s.state && s.state !== "not_home" && s.state !== "unknown" && s.state !== "unavailable"
        ? s.state
        : null;
      points.push({ lat, lon, dt, zoneState, acc: Number(attrs.gps_accuracy) || 0 });
    }

    const stays = [];
    for (const pt of points) {
      const prev = stays[stays.length - 1];
      const sameZone = prev && pt.zoneState && prev.zoneState === pt.zoneState;
      const nearPrev = prev && haversineMeters(prev.lat, prev.lon, pt.lat, pt.lon) <= 120;
      if (prev && (sameZone || nearPrev)) {
        prev.endDt = pt.dt;
        prev.count += 1;
      } else {
        stays.push({
          lat: pt.lat,
          lon: pt.lon,
          startDt: pt.dt,
          endDt: pt.dt,
          zoneState: pt.zoneState,
          acc: pt.acc,
          count: 1,
          address: null,
        });
      }
    }

    // Reverse chronological order (newest at top, matching Samsung Find)
    return stays.reverse();
  }

  async _hydrateAddresses() {
    // Gently resolve outside-zone addresses (up to 8 stops per day) via Nominatim
    let fetched = 0;
    for (let i = 0; i < this._stops.length && fetched < 8; i++) {
      const stop = this._stops[i];
      if (stop.zoneState || stop.address) continue;
      const addr = await reverseGeocode(stop.lat, stop.lon);
      if (addr) {
        stop.address = addr;
        const addrEl = this._listEl.querySelector(`[data-stop-idx="${i}"] .tl-addr`);
        if (addrEl) addrEl.textContent = addr;
      }
      fetched += 1;
    }
  }

  _updateMap() {
    if (!this._mapEl || !this._hass || !this._entityId) return;
    this._mapEl.hass = this._hass;
    this._mapEl.darkMode = isDarkMode(this._hass);
    this._mapEl.autoFit = true;
    this._mapEl.fitZones = false;
    this._mapEl.interactiveZones = false;

    const now = new Date();
    const startOfDay = new Date(this._selectedDate);
    startOfDay.setHours(0, 0, 0, 0);
    const hoursBack = Math.max(24, Math.ceil((now - startOfDay) / 3600000));
    this._mapEl.hoursToShow = Math.min(hoursBack, 168);

    const stop = this._stops[this._selectedIdx];
    if (stop) {
      this._mapEl.entities = [
        { entity_id: this._entityId },
        { latitude: stop.lat, longitude: stop.lon, name: stop.zoneState || formatTimeHM(stop.endDt) },
      ];
    } else {
      this._mapEl.entities = [this._entityId];
    }
  }

  _renderTimeline() {
    if (!this._stops.length) {
      this._listEl.innerHTML = `<div class="tl-empty">No location history recorded for this day.</div>`;
      return;
    }
    this._listEl.innerHTML = "";
    this._stops.forEach((stop, idx) => {
      const isActive = idx === this._selectedIdx;
      const startHM = formatTimeHM(stop.startDt);
      const endHM = formatTimeHM(stop.endDt);
      const timeHtml =
        startHM !== endHM
          ? `<span>${endHM}</span><span>-</span><span>${startHM}</span>`
          : `<span>${endHM}</span>`;
      const label =
        stop.address ||
        (stop.zoneState ? stop.zoneState : `${stop.lat.toFixed(5)}, ${stop.lon.toFixed(5)}`);
      const durationMin = Math.max(0, Math.round((stop.endDt - stop.startDt) / 60000));
      const durText = durationMin >= 60
        ? `${Math.floor(durationMin / 60)}h ${durationMin % 60}m`
        : durationMin > 0 ? `${durationMin} min` : "Point";
      const mapsUrl = `https://www.google.com/maps/search/?api=1&query=${stop.lat},${stop.lon}`;

      const row = document.createElement("div");
      row.className = `tl-row${isActive ? " active" : ""}`;
      row.dataset.stopIdx = String(idx);
      row.innerHTML = `
        <div class="tl-time">${timeHtml}</div>
        <div class="tl-rail-col">
          ${isActive ? `<span class="tl-pin">${ICON_PIN_FILLED}</span>` : `<span class="tl-dot"></span>`}
        </div>
        <div class="tl-card">
          <div class="tl-addr">${label}</div>
          <div class="tl-meta">
            <span>${durText}</span>
            <a class="tl-maps-link" href="${mapsUrl}" target="_blank" rel="noopener">Maps</a>
          </div>
        </div>
      `;
      row.addEventListener("click", (e) => {
        if (e.target.closest("a")) return;
        this._selectedIdx = idx;
        this._renderTimeline();
        this._updateMap();
      });
      this._listEl.appendChild(row);
    });
  }
}

/* ============================================================================================
 * CARD 1: Single-Device Map Card (`custom:smartthings-find-device-card`)
 * ============================================================================================ */
class SmartThingsFindDeviceCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._mapEl = null;
    this._refreshing = false;
    this._ringPending = false;
    this._built = false;
  }

  static getConfigElement() {
    return document.createElement("smartthings-find-device-card-editor");
  }

  static getStubConfig(hass) {
    const trackers = getSmartThingsFindTrackers(hass);
    return {
      entity: trackers[0] || "",
      design: "one_ui",
      height: 200,
      default_zoom: 15,
      show_ring: true,
      show_history: true,
      show_badges: true,
    };
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("Please specify a device_tracker `entity`.");
    }
    this._config = {
      design: "one_ui",
      height: 200,
      default_zoom: 15,
      show_ring: true,
      show_history: true,
      show_badges: true,
      interactive_map: true,
      ...config,
    };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return Math.max(3, Math.round((this._config.height || 200) / 50));
  }

  _buildDom() {
    if (this._built) return;
    this._built = true;
    this.shadowRoot.innerHTML = `
      <style>
        ${SHARED_THEME_CSS}
        .bottom-scrim {
          position: absolute;
          left: 0;
          right: 0;
          bottom: 0;
          height: 54%;
          background: linear-gradient(
            to top,
            rgba(var(--stf-scrim-rgb), 0.94) 0%,
            rgba(var(--stf-scrim-rgb), 0.68) 48%,
            rgba(var(--stf-scrim-rgb), 0.0) 100%
          );
          pointer-events: none;
        }
        .top-right-stack {
          position: absolute;
          top: 12px;
          right: 12px;
          display: flex;
          flex-direction: column;
          gap: 8px;
          pointer-events: none;
        }
        .btn-refresh, .btn-history {
          width: 38px;
          height: 38px;
          border-radius: var(--stf-btn-radius);
          background: var(--stf-refresh-bg);
          color: var(--stf-refresh-color);
          backdrop-filter: blur(8px);
        }
        .design-md3 .btn-refresh, .design-md3 .btn-history {
          width: 40px;
          height: 40px;
          backdrop-filter: none;
        }
        .btn-history.hidden {
          display: none;
        }
        .btn-ring {
          position: absolute;
          bottom: 14px;
          right: 14px;
          width: 46px;
          height: 46px;
          border-radius: var(--stf-fab-radius);
          background: var(--stf-ring-bg);
          color: var(--stf-ring-color);
          backdrop-filter: blur(8px);
        }
        .design-md3 .btn-ring {
          width: 48px;
          height: 48px;
          backdrop-filter: none;
        }
        .btn-ring.ringing {
          background: var(--stf-accent);
          color: var(--stf-on-accent);
          border-color: var(--stf-accent);
          animation: stf-ring-pulse 1.5s ease-out infinite;
        }
        @keyframes stf-ring-pulse {
          0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--stf-accent) 45%, transparent); }
          70% { box-shadow: 0 0 0 11px transparent; }
          100% { box-shadow: 0 0 0 0 transparent; }
        }
        .btn-ring.hidden {
          display: none;
        }
        .info-block {
          position: absolute;
          left: 16px;
          right: 74px;
          bottom: 14px;
          pointer-events: auto;
          cursor: pointer;
          display: flex;
          flex-direction: column;
          gap: 3px;
        }
        .design-md3 .info-block {
          background: var(--stf-surface-glass);
          padding: 8px 14px;
          border-radius: 16px;
          right: auto;
          max-width: calc(100% - 92px);
          box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
        }
        .ts-line {
          font-size: 12.5px;
          font-weight: 500;
          color: var(--stf-on-surface-muted);
          font-variant-numeric: tabular-nums;
          line-height: 1.25;
        }
        .name-line {
          font-size: 15px;
          font-weight: 700;
          color: var(--stf-on-surface);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          line-height: 1.25;
        }
        .meta-line {
          font-size: 12px;
          color: var(--stf-on-surface-muted);
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          align-items: center;
          line-height: 1.2;
        }
        .badge {
          display: inline-flex;
          align-items: center;
          padding: 2px 8px;
          border-radius: 9999px;
          font-size: 11px;
          font-weight: 600;
          background: var(--stf-surface-muted);
          color: var(--stf-on-surface);
        }
        .design-md3 .badge {
          background: var(--md-sys-color-secondary-container, var(--stf-surface-muted));
          color: var(--md-sys-color-on-secondary-container, var(--stf-on-surface));
        }
      </style>
      <ha-card class="stf-shell">
        <div class="map-wrap">
          <div class="hud-layer">
            <div class="bottom-scrim"></div>
            <div class="top-right-stack">
              <button class="icon-btn btn-refresh" title="Update location" aria-label="Update location">${ICON_REFRESH}</button>
              <button class="icon-btn btn-history" title="Location history" aria-label="Location history">${ICON_HISTORY}</button>
            </div>
            <button class="icon-btn btn-ring" title="Ring device" aria-label="Ring device">${ICON_RING_BELL}</button>
            <div class="info-block">
              <div class="ts-line"></div>
              <div class="name-line"></div>
              <div class="meta-line"></div>
            </div>
          </div>
        </div>
      </ha-card>
    `;

    this._cardEl = this.shadowRoot.querySelector("ha-card");
    this._mapWrap = this.shadowRoot.querySelector(".map-wrap");
    this._btnRefresh = this.shadowRoot.querySelector(".btn-refresh");
    this._btnHistory = this.shadowRoot.querySelector(".btn-history");
    this._btnRing = this.shadowRoot.querySelector(".btn-ring");
    this._infoBlock = this.shadowRoot.querySelector(".info-block");
    this._tsEl = this.shadowRoot.querySelector(".ts-line");
    this._nameEl = this.shadowRoot.querySelector(".name-line");
    this._metaEl = this.shadowRoot.querySelector(".meta-line");

    this._btnRefresh.addEventListener("click", (e) => {
      e.stopPropagation();
      this._handleRefresh();
    });
    this._btnHistory.addEventListener("click", (e) => {
      e.stopPropagation();
      this._openHistory();
    });
    this._btnRing.addEventListener("click", (e) => {
      e.stopPropagation();
      this._handleRing();
    });
    this._infoBlock.addEventListener("click", (e) => {
      e.stopPropagation();
      fireMoreInfo(this, this._config.entity);
    });

    ensureHaMapLoaded().then(() => {
      if (!this._mapEl && this._mapWrap) {
        const map = document.createElement("ha-map");
        this._mapEl = map;
        this._mapWrap.insertBefore(map, this._mapWrap.firstChild);
        this._syncMap();
      }
    });
  }

  _openHistory() {
    if (!this._hass || !this._config.entity) return;
    const dlg = document.createElement("smartthings-find-history-dialog");
    dlg.open(this._hass, this._config.entity, this._config.design || "one_ui");
  }

  async _handleRefresh() {
    if (!this._hass || !this._config.entity || this._refreshing) return;
    const companions = findCompanionEntities(this._hass, this._config.entity);
    this._refreshing = true;
    this._btnRefresh?.classList.add("spin");
    try {
      if (companions.updateButton) {
        await this._hass.callService("button", "press", { entity_id: companions.updateButton });
      } else {
        await this._hass.callService("homeassistant", "update_entity", { entity_id: this._config.entity });
      }
    } catch (_err) {
      // Handled by HA
    } finally {
      setTimeout(() => {
        this._refreshing = false;
        this._btnRefresh?.classList.remove("spin");
      }, 900);
    }
  }

  async _handleRing() {
    if (!this._hass || !this._config.entity || this._ringPending) return;
    const companions = findCompanionEntities(this._hass, this._config.entity);
    if (!companions.ringSwitch) return;
    const sw = this._hass.states[companions.ringSwitch];
    const isRinging = Boolean(sw && sw.state === "on");
    const service = isRinging ? "turn_off" : "turn_on";
    this._ringPending = true;
    try {
      await this._hass.callService("switch", service, { entity_id: companions.ringSwitch });
    } catch (_err) {
      // Handled by HA notification
    } finally {
      this._ringPending = false;
    }
  }

  _coordsKey(entityId) {
    const st = this._hass?.states?.[entityId];
    if (!st) return "";
    return `${st.attributes?.latitude},${st.attributes?.longitude},${st.state}`;
  }

  _syncMap() {
    if (!this._mapEl || !this._hass || !this._config.entity) return;
    const entityId = this._config.entity;
    this._mapEl.hass = this._hass;
    this._mapEl.darkMode = isDarkMode(this._hass);
    this._mapEl.defaultZoom = Number(this._config.default_zoom) || 15;
    this._mapEl.autoFit = true;
    this._mapEl.fitZones = false;
    this._mapEl.interactiveZones = false;
    const key = this._coordsKey(entityId);
    if (this._lastMapEntity !== entityId || this._lastMapCoords !== key) {
      this._mapEl.entities = [entityId];
      this._lastMapEntity = entityId;
      this._lastMapCoords = key;
    }
  }

  _render() {
    this._buildDom();
    if (!this._cardEl) return;

    const design = this._config.design === "md3" ? "md3" : "one_ui";
    const dark = isDarkMode(this._hass);
    this._cardEl.className = `stf-shell design-${design}${dark ? " dark" : ""}`;

    const height = Math.max(140, Number(this._config.height) || 200);
    this._mapWrap.style.height = `${height}px`;
    this._mapWrap.style.pointerEvents = this._config.interactive_map === false ? "none" : "auto";
    this._btnHistory.classList.toggle("hidden", this._config.show_history === false);

    if (!this._hass || !this._config.entity) return;
    const st = this._hass.states[this._config.entity];
    const companions = findCompanionEntities(this._hass, this._config.entity);
    const locState = companions.locationSensor ? this._hass.states[companions.locationSensor] : null;
    const dirState = companions.directionSensor ? this._hass.states[companions.directionSensor] : null;
    const ringState = companions.ringSwitch ? this._hass.states[companions.ringSwitch] : null;

    this._tsEl.textContent = formatLastUpdated(this._hass, st, locState) || "—";
    this._nameEl.textContent = st?.attributes?.friendly_name || this._config.entity;

    const metaParts = [];
    if (st && st.state && st.state !== "unknown" && st.state !== "unavailable") {
      const formattedZone = this._hass.formatEntityState ? this._hass.formatEntityState(st) : st.state;
      metaParts.push(`<span class="badge">${formattedZone}</span>`);
    }
    if (this._config.show_badges !== false) {
      const batt = formatBatteryText(this._hass, companions);
      if (batt) metaParts.push(`<span class="badge">${batt}</span>`);
      if (dirState && dirState.state && dirState.state !== "unknown" && dirState.state !== "unavailable") {
        const dirText = this._hass.formatEntityState ? this._hass.formatEntityState(dirState) : dirState.state;
        metaParts.push(`<span class="badge">${dirText}</span>`);
      }
    }
    this._metaEl.innerHTML = metaParts.join("");

    const showRing = this._config.show_ring !== false && Boolean(companions.ringSwitch);
    const isRinging = Boolean(ringState && ringState.state === "on");
    this._btnRing.classList.toggle("hidden", !showRing);
    this._btnRing.classList.toggle("ringing", isRinging);
    this._btnRing.innerHTML = isRinging ? ICON_RING_STOP : ICON_RING_BELL;
    this._btnRing.title = isRinging ? "Stop ringing" : "Ring device";
    this._btnRing.setAttribute("aria-label", isRinging ? "Stop ringing" : "Ring device");

    this._syncMap();
  }
}

/* ============================================================================================
 * CARD 2: Multi-Device Carousel Map Card (`custom:smartthings-find-carousel-card`)
 * ============================================================================================ */
class SmartThingsFindCarouselCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = {};
    this._hass = null;
    this._mapEl = null;
    this._selectedEntity = null;
    this._refreshing = false;
    this._built = false;
  }

  static getConfigElement() {
    return document.createElement("smartthings-find-carousel-card-editor");
  }

  static getStubConfig(_hass) {
    return {
      design: "one_ui",
      map_height: 220,
      default_zoom: 15,
      show_badges: true,
      show_history: true,
    };
  }

  setConfig(config) {
    this._config = {
      design: "one_ui",
      map_height: 220,
      default_zoom: 15,
      show_badges: true,
      show_history: true,
      interactive_map: true,
      entities: [],
      ...config,
    };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return Math.max(5, Math.round(((this._config.map_height || 220) + 92) / 50));
  }

  _getEntities() {
    if (Array.isArray(this._config.entities) && this._config.entities.length > 0) {
      return this._config.entities
        .map((e) => (typeof e === "string" ? e : e?.entity))
        .filter(Boolean);
    }
    return getSmartThingsFindTrackers(this._hass);
  }

  _buildDom() {
    if (this._built) return;
    this._built = true;
    this.shadowRoot.innerHTML = `
      <style>
        ${SHARED_THEME_CSS}
        .right-actions {
          position: absolute;
          top: 12px;
          right: 12px;
          display: flex;
          flex-direction: column;
          gap: 8px;
          pointer-events: none;
        }
        .btn-more, .btn-history {
          width: 36px;
          height: 36px;
          border-radius: var(--stf-btn-radius);
          background: var(--stf-surface-glass);
          color: var(--stf-on-surface);
          border-color: var(--stf-outline-strong);
          backdrop-filter: blur(8px);
        }
        .design-md3 .btn-more, .design-md3 .btn-history {
          width: 38px;
          height: 38px;
          backdrop-filter: none;
        }
        .btn-history.hidden {
          display: none;
        }

        /* Clean Floating Top Info Pill (no static pointer arrow so panning the map looks natural!) */
        .pill-anchor {
          position: absolute;
          top: 10px;
          left: 50%;
          transform: translateX(-50%);
          max-width: calc(100% - 118px);
          pointer-events: auto;
        }
        .info-pill {
          background: var(--stf-bubble-bg);
          border: var(--stf-bubble-border);
          border-radius: var(--stf-bubble-radius);
          padding: 7px 15px;
          box-shadow: 0 3px 14px rgba(0, 0, 0, 0.14);
          backdrop-filter: blur(10px);
          text-align: center;
          cursor: pointer;
          transition: background-color 180ms var(--stf-ease-standard), border-color 180ms var(--stf-ease-standard);
        }
        .design-md3 .info-pill {
          backdrop-filter: none;
          box-shadow: 0 2px 8px rgba(0, 0, 0, 0.14);
        }
        .pill-name {
          font-size: 14px;
          font-weight: 700;
          color: var(--stf-on-surface);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          max-width: 220px;
          margin: 0 auto;
        }
        .pill-sub {
          margin-top: 2px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 5px;
          font-size: 11.5px;
          color: var(--stf-on-surface-muted);
          font-variant-numeric: tabular-nums;
          white-space: nowrap;
        }
        .pill-badges {
          margin-top: 4px;
          display: flex;
          justify-content: center;
          gap: 5px;
          flex-wrap: wrap;
        }
        .pill-badges:empty {
          display: none;
        }
        .pill-badge {
          font-size: 10.5px;
          font-weight: 600;
          padding: 1px 8px;
          border-radius: 9999px;
          background: var(--stf-surface-muted);
          color: var(--stf-on-surface);
        }
        .design-md3 .pill-badge {
          background: var(--md-sys-color-secondary-container, var(--stf-surface-muted));
          color: var(--md-sys-color-on-secondary-container, var(--stf-on-surface));
        }
        .inline-refresh {
          pointer-events: auto;
          background: transparent;
          border: none;
          padding: 2px;
          margin: -2px -2px -2px 0;
          color: var(--stf-on-surface-muted);
          cursor: pointer;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          border-radius: 9999px;
          transition: color 150ms ease;
        }
        .design-md3 .inline-refresh, .inline-refresh:hover {
          color: var(--stf-accent);
        }
        .inline-refresh svg {
          width: 14px;
          height: 14px;
        }

        /* Smooth MD3 gradient fade between map and device selector */
        .map-bottom-fade {
          position: absolute;
          left: 0;
          right: 0;
          bottom: 0;
          height: 44px;
          pointer-events: none;
          opacity: 0;
          background: linear-gradient(
            to top,
            var(--stf-surface) 0%,
            color-mix(in srgb, var(--stf-surface) 76%, transparent) 46%,
            transparent 100%
          );
          transition: opacity 200ms var(--stf-ease-standard);
        }
        .design-md3 .map-bottom-fade {
          opacity: 1;
        }

        /* Scrollable Bottom Device Strip */
        .carousel-bar {
          display: flex;
          align-items: center;
          gap: 12px;
          padding: 14px 16px;
          overflow-x: auto;
          overflow-y: hidden;
          background: var(--stf-surface);
          scroll-behavior: smooth;
          scrollbar-width: none;
          -ms-overflow-style: none;
          border-top: 1px solid var(--stf-outline);
        }
        .design-md3 .carousel-bar {
          border-top: none;
          padding-top: 8px;
        }
        .carousel-bar::-webkit-scrollbar {
          display: none;
        }
        .device-pill {
          flex: 0 0 auto;
          width: 56px;
          height: 56px;
          border-radius: var(--stf-item-radius);
          background: var(--stf-surface-muted);
          border: 2.5px solid transparent;
          padding: 8px;
          cursor: pointer;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          position: relative;
          outline: none;
          transition: transform 180ms var(--stf-ease-standard), border-color 180ms var(--stf-ease-standard), background-color 180ms var(--stf-ease-standard), border-radius 180ms var(--stf-ease-standard);
          -webkit-tap-highlight-color: transparent;
        }
        .device-pill:active {
          transform: scale(0.93);
        }
        .device-pill.active {
          border-color: var(--stf-accent);
          background: var(--stf-item-active-bg);
        }
        .design-md3 .device-pill.active {
          border-radius: 28px;
        }
        .device-pill img {
          width: 34px;
          height: 34px;
          object-fit: contain;
          pointer-events: none;
          user-select: none;
        }
        .device-pill .fallback-svg {
          width: 26px;
          height: 26px;
          color: var(--stf-on-surface-muted);
        }
        .device-pill.active .fallback-svg {
          color: var(--stf-accent);
        }
      </style>
      <ha-card class="stf-shell">
        <div class="map-wrap">
          <div class="hud-layer">
            <div class="right-actions">
              <button class="icon-btn btn-more" title="Device details" aria-label="Device details">${ICON_CHEVRON_RIGHT}</button>
              <button class="icon-btn btn-history" title="Location history" aria-label="Location history">${ICON_HISTORY}</button>
            </div>
            <div class="pill-anchor">
              <div class="info-pill" title="Click to recenter map">
                <div class="pill-name"></div>
                <div class="pill-sub">
                  <span class="pill-ts"></span>
                  <button class="inline-refresh" title="Update location" aria-label="Update location">${ICON_REFRESH}</button>
                </div>
                <div class="pill-badges"></div>
              </div>
            </div>
            <div class="map-bottom-fade"></div>
          </div>
        </div>
        <div class="carousel-bar"></div>
      </ha-card>
    `;

    this._cardEl = this.shadowRoot.querySelector("ha-card");
    this._mapWrap = this.shadowRoot.querySelector(".map-wrap");
    this._btnMore = this.shadowRoot.querySelector(".btn-more");
    this._btnHistory = this.shadowRoot.querySelector(".btn-history");
    this._pillEl = this.shadowRoot.querySelector(".info-pill");
    this._pillName = this.shadowRoot.querySelector(".pill-name");
    this._pillTs = this.shadowRoot.querySelector(".pill-ts");
    this._pillBadges = this.shadowRoot.querySelector(".pill-badges");
    this._btnRefresh = this.shadowRoot.querySelector(".inline-refresh");
    this._carouselBar = this.shadowRoot.querySelector(".carousel-bar");

    this._btnMore.addEventListener("click", (e) => {
      e.stopPropagation();
      if (this._selectedEntity) fireMoreInfo(this, this._selectedEntity);
    });
    this._btnHistory.addEventListener("click", (e) => {
      e.stopPropagation();
      this._openHistory();
    });
    this._pillEl.addEventListener("click", (e) => {
      e.stopPropagation();
      // Recenter map smoothly on the selected device
      if (this._mapEl && this._selectedEntity) {
        this._mapEl.entities = [this._selectedEntity];
        if (typeof this._mapEl.fitMap === "function") {
          this._mapEl.fitMap();
        }
      }
    });
    this._btnRefresh.addEventListener("click", (e) => {
      e.stopPropagation();
      this._handleRefresh();
    });

    ensureHaMapLoaded().then(() => {
      if (!this._mapEl && this._mapWrap) {
        const map = document.createElement("ha-map");
        this._mapEl = map;
        this._mapWrap.insertBefore(map, this._mapWrap.firstChild);
        this._syncMap();
      }
    });
  }

  _openHistory() {
    if (!this._hass || !this._selectedEntity) return;
    const dlg = document.createElement("smartthings-find-history-dialog");
    dlg.open(this._hass, this._selectedEntity, this._config.design || "one_ui");
  }

  async _handleRefresh() {
    if (!this._hass || !this._selectedEntity || this._refreshing) return;
    const companions = findCompanionEntities(this._hass, this._selectedEntity);
    this._refreshing = true;
    this._btnRefresh?.classList.add("spin");
    try {
      if (companions.updateButton) {
        await this._hass.callService("button", "press", { entity_id: companions.updateButton });
      } else {
        await this._hass.callService("homeassistant", "update_entity", { entity_id: this._selectedEntity });
      }
    } catch (_err) {
      // Handled by HA
    } finally {
      setTimeout(() => {
        this._refreshing = false;
        this._btnRefresh?.classList.remove("spin");
      }, 900);
    }
  }

  _selectEntity(entityId, pillBtn) {
    if (this._selectedEntity === entityId) return;
    this._selectedEntity = entityId;
    if (pillBtn && pillBtn.scrollIntoView) {
      pillBtn.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
    }
    this._render();
  }

  _coordsKey(entityId) {
    const st = this._hass?.states?.[entityId];
    if (!st) return "";
    return `${st.attributes?.latitude},${st.attributes?.longitude},${st.state}`;
  }

  _syncMap() {
    if (!this._mapEl || !this._hass || !this._selectedEntity) return;
    this._mapEl.hass = this._hass;
    this._mapEl.darkMode = isDarkMode(this._hass);
    this._mapEl.defaultZoom = Number(this._config.default_zoom) || 15;
    this._mapEl.autoFit = true;
    this._mapEl.fitZones = false;
    this._mapEl.interactiveZones = false;
    const key = this._coordsKey(this._selectedEntity);
    if (this._lastMapEntity !== this._selectedEntity || this._lastMapCoords !== key) {
      this._mapEl.entities = [this._selectedEntity];
      this._lastMapEntity = this._selectedEntity;
      this._lastMapCoords = key;
    }
  }

  _renderCarousel(entities) {
    const signature = entities
      .map((eid) => {
        const st = this._hass?.states?.[eid];
        return `${eid}|${st?.attributes?.entity_picture || ""}|${eid === this._selectedEntity ? 1 : 0}`;
      })
      .join(";");
    if (signature === this._lastCarouselSig) return;
    this._lastCarouselSig = signature;

    this._carouselBar.innerHTML = "";
    for (const eid of entities) {
      const st = this._hass?.states?.[eid];
      const name = st?.attributes?.friendly_name || eid;
      const pic = st?.attributes?.entity_picture;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `device-pill${eid === this._selectedEntity ? " active" : ""}`;
      btn.title = name;
      btn.setAttribute("aria-label", name);
      if (pic) {
        const img = document.createElement("img");
        img.src = pic;
        img.alt = name;
        img.loading = "lazy";
        btn.appendChild(img);
      } else {
        const span = document.createElement("span");
        span.className = "fallback-svg";
        span.innerHTML = ICON_DEVICE_FALLBACK;
        btn.appendChild(span);
      }
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        this._selectEntity(eid, btn);
      });
      this._carouselBar.appendChild(btn);
    }
  }

  _render() {
    this._buildDom();
    if (!this._cardEl) return;

    const design = this._config.design === "md3" ? "md3" : "one_ui";
    const dark = isDarkMode(this._hass);
    this._cardEl.className = `stf-shell design-${design}${dark ? " dark" : ""}`;

    const height = Math.max(150, Number(this._config.map_height) || 220);
    this._mapWrap.style.height = `${height}px`;
    this._mapWrap.style.pointerEvents = this._config.interactive_map === false ? "none" : "auto";
    this._btnHistory.classList.toggle("hidden", this._config.show_history === false);

    const entities = this._getEntities();
    if (!entities.length) {
      this._pillName.textContent = "SmartThings Find";
      this._pillTs.textContent = "No tracked devices";
      this._pillBadges.innerHTML = "";
      this._carouselBar.innerHTML = `<div class="empty-state" style="height:56px;padding:0">No SmartThings Find devices found</div>`;
      return;
    }

    if (!this._selectedEntity || !entities.includes(this._selectedEntity)) {
      this._selectedEntity = entities[0];
    }

    const st = this._hass?.states?.[this._selectedEntity];
    const companions = findCompanionEntities(this._hass, this._selectedEntity);
    const locState = companions.locationSensor ? this._hass?.states?.[companions.locationSensor] : null;
    const dirState = companions.directionSensor ? this._hass?.states?.[companions.directionSensor] : null;

    this._pillName.textContent = st?.attributes?.friendly_name || this._selectedEntity;
    const ts = formatLastUpdated(this._hass, st, locState);
    this._pillTs.textContent = ts ? `Last updated: ${ts}` : "Last updated: —";

    const badges = [];
    if (this._config.show_badges !== false) {
      const batt = formatBatteryText(this._hass, companions);
      if (batt) badges.push(`<span class="pill-badge">${batt}</span>`);
      if (dirState && dirState.state && dirState.state !== "unknown" && dirState.state !== "unavailable") {
        const dirText = this._hass.formatEntityState ? this._hass.formatEntityState(dirState) : dirState.state;
        badges.push(`<span class="pill-badge">${dirText}</span>`);
      }
    }
    this._pillBadges.innerHTML = badges.join("");

    this._renderCarousel(entities);
    this._syncMap();
  }
}

/* ============================================================================================
 * VISUAL GUI EDITORS (`ha-form` native Home Assistant editors)
 * ============================================================================================ */
class SmartThingsFindDeviceCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) this._form.hass = hass;
  }

  _render() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => schema.label || schema.name;
      this._form.addEventListener("value-changed", (ev) => {
        const newConfig = { ...this._config, ...ev.detail.value };
        this.dispatchEvent(new CustomEvent("config-changed", {
          detail: { config: newConfig },
          bubbles: true,
          composed: true,
        }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = {
      entity: this._config.entity || "",
      design: this._config.design || "one_ui",
      height: this._config.height ?? 200,
      default_zoom: this._config.default_zoom ?? 15,
      show_ring: this._config.show_ring !== false,
      show_history: this._config.show_history !== false,
      show_badges: this._config.show_badges !== false,
    };
    this._form.schema = [
      { name: "entity", label: "Device Tracker", required: true, selector: { entity: { domain: "device_tracker" } } },
      {
        name: "design",
        label: "Visual Design",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              { value: "one_ui", label: "Samsung One UI" },
              { value: "md3", label: "Material Design 3 (Material You)" },
            ],
          },
        },
      },
      { name: "height", label: "Card height (px)", selector: { number: { min: 140, max: 500, step: 10, mode: "box" } } },
      { name: "default_zoom", label: "Default map zoom", selector: { number: { min: 5, max: 19, step: 1, mode: "slider" } } },
      { name: "show_ring", label: "Show Ring / Stop Ring button", selector: { boolean: {} } },
      { name: "show_history", label: "Show Location History button", selector: { boolean: {} } },
      { name: "show_badges", label: "Show battery & direction badges", selector: { boolean: {} } },
    ];
  }
}

class SmartThingsFindCarouselCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._form) this._form.hass = hass;
  }

  _render() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => schema.label || schema.name;
      this._form.addEventListener("value-changed", (ev) => {
        const newConfig = { ...this._config, ...ev.detail.value };
        this.dispatchEvent(new CustomEvent("config-changed", {
          detail: { config: newConfig },
          bubbles: true,
          composed: true,
        }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = {
      design: this._config.design || "one_ui",
      entities: this._config.entities || [],
      map_height: this._config.map_height ?? 220,
      default_zoom: this._config.default_zoom ?? 15,
      show_history: this._config.show_history !== false,
      show_badges: this._config.show_badges !== false,
    };
    this._form.schema = [
      {
        name: "design",
        label: "Visual Design",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              { value: "one_ui", label: "Samsung One UI" },
              { value: "md3", label: "Material Design 3 (Material You)" },
            ],
          },
        },
      },
      {
        name: "entities",
        label: "Devices (leave empty to auto-discover all SmartThings Find devices)",
        selector: { entity: { domain: "device_tracker", multiple: true } },
      },
      { name: "map_height", label: "Map height (px)", selector: { number: { min: 150, max: 500, step: 10, mode: "box" } } },
      { name: "default_zoom", label: "Default map zoom", selector: { number: { min: 5, max: 19, step: 1, mode: "slider" } } },
      { name: "show_history", label: "Show Location History button", selector: { boolean: {} } },
      { name: "show_badges", label: "Show battery & direction badges", selector: { boolean: {} } },
    ];
  }
}

if (!customElements.get("smartthings-find-history-dialog")) {
  customElements.define("smartthings-find-history-dialog", SmartThingsFindHistoryDialog);
}
if (!customElements.get("smartthings-find-device-card")) {
  customElements.define("smartthings-find-device-card", SmartThingsFindDeviceCard);
}
if (!customElements.get("smartthings-find-device-card-editor")) {
  customElements.define("smartthings-find-device-card-editor", SmartThingsFindDeviceCardEditor);
}
if (!customElements.get("smartthings-find-carousel-card")) {
  customElements.define("smartthings-find-carousel-card", SmartThingsFindCarouselCard);
}
if (!customElements.get("smartthings-find-carousel-card-editor")) {
  customElements.define("smartthings-find-carousel-card-editor", SmartThingsFindCarouselCardEditor);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "smartthings-find-device-card")) {
  window.customCards.push({
    type: "smartthings-find-device-card",
    name: "SmartThings Find — Device Map",
    description: "Single-device SmartThings Find map widget in Samsung One UI or Material Design 3 (Material You).",
    preview: true,
  });
}
if (!window.customCards.some((c) => c.type === "smartthings-find-carousel-card")) {
  window.customCards.push({
    type: "smartthings-find-carousel-card",
    name: "SmartThings Find — Device Carousel Map",
    description: "Multi-device SmartThings Find map with scrollable device selector bar and Location History in One UI or MD3.",
    preview: true,
  });
}

console.info(
  `%c SMARTTHINGS-FIND-CARDS %c v${CARD_VERSION} `,
  "color:#fff;background:#387AFF;font-weight:700;padding:2px 6px;border-radius:4px 0 0 4px;",
  "color:#387AFF;background:#EBF2FF;font-weight:700;padding:2px 6px;border-radius:0 4px 4px 0;"
);
