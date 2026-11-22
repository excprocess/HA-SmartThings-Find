/**
 * SmartThings Find — Integrated Lovelace Cards (One UI & Material Design 3 / Material You)
 *
 * Cards registered:
 *  1. `custom:smartthings-find-device-card`   — Single-device map widget (Samsung One UI / MD3)
 *  2. `custom:smartthings-find-carousel-card` — Multi-device map widget with scrollable device bar
 *
 * Uses Home Assistant's native `<ha-map>` component and integrates with Material You Utilities
 * (`--md-sys-color-*`, `--md-sys-shape-*`, `--md-sys-motion-*`) when `design: md3` is selected.
 */

const CARD_VERSION = "8.5.0";

const ICON_REFRESH = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><polyline points="21 3 21 9 15 9"/></svg>`;
const ICON_PHONE_RING = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/></svg>`;
const ICON_CHEVRON_RIGHT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>`;
const ICON_DEVICE_FALLBACK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-6-5.33-6-10a6 6 0 1 1 12 0c0 4.67-6 10-6 10z"/><circle cx="12" cy="11" r="2.2"/></svg>`;

let _haMapLoaderPromise = null;

function ensureHaMapLoaded() {
  if (customElements.get("ha-map")) {
    return Promise.resolve();
  }
  if (!_haMapLoaderPromise) {
    _haMapLoaderPromise = (async () => {
      try {
        if (window.loadCardHelpers) {
          const helpers = await window.loadCardHelpers();
          if (helpers && helpers.createCardElement) {
            const tmp = helpers.createCardElement({ type: "map", entities: [] });
            if (tmp && tmp.constructor && tmp.constructor.getStubConfig) {
              // Trigger lazy bundle evaluation of hui-map-card -> ha-map
            }
          }
        }
      } catch (_err) {
        // Ignore; ha-map may already be loading
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
    if (isStf) {
      result.push(entityId);
    }
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
  const trackerReg = regEntities[trackerEntityId];
  const deviceId = trackerReg?.device_id;

  if (deviceId) {
    for (const [eid, entry] of Object.entries(regEntities)) {
      if (!entry || entry.device_id !== deviceId) continue;
      if (eid.startsWith("button.") && eid.endsWith("_update_location")) {
        out.updateButton = eid;
      } else if (eid.startsWith("switch.") && eid.endsWith("_ring")) {
        out.ringSwitch = eid;
      } else if (eid.startsWith("sensor.") && eid.endsWith("_left_battery")) {
        out.leftBatterySensor = eid;
      } else if (eid.startsWith("sensor.") && eid.endsWith("_right_battery")) {
        out.rightBatterySensor = eid;
      } else if (eid.startsWith("sensor.") && eid.endsWith("_battery")) {
        out.batterySensor = eid;
      } else if (eid.startsWith("sensor.") && eid.endsWith("_direction")) {
        out.directionSensor = eid;
      } else if (eid.startsWith("sensor.") && eid.endsWith("_location")) {
        out.locationSensor = eid;
      } else if (eid.startsWith("sensor.") && eid.endsWith("_maps_link")) {
        out.mapsLinkSensor = eid;
      }
    }
  }

  // Fallback by object_id slug when hass.entities registry is not populated
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
    if (!out[key] && hass.states && hass.states[candidate]) {
      out[key] = candidate;
    }
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
  if (validNum(b)) {
    return `${b.state}%`;
  }
  return "";
}

function fireMoreInfo(node, entityId) {
  if (!entityId) return;
  const ev = new CustomEvent("hass-more-info", {
    detail: { entityId },
    bubbles: true,
    composed: true,
  });
  node.dispatchEvent(ev);
}

function isDarkMode(hass) {
  return Boolean(hass?.themes?.darkMode);
}

const SHARED_THEME_CSS = `
  :host {
    display: block;
    --stf-font: var(--paper-font-body1_-_font-family, "SamsungOne", "Roboto", system-ui, -apple-system, sans-serif);
    --stf-ease-standard: var(--md-sys-motion-easing-emphasized, cubic-bezier(0.2, 0, 0, 1));
  }

  /* ==================== ONE UI LIGHT & DARK TOKENS ==================== */
  .stf-shell.design-one_ui {
    --stf-radius: 26px;
    --stf-accent: #257AFD;
    --stf-on-accent: #FFFFFF;
    --stf-surface: #FFFFFF;
    --stf-surface-muted: #F2F3F5;
    --stf-surface-glass: rgba(255, 255, 255, 0.90);
    --stf-on-surface: #111111;
    --stf-on-surface-muted: #63666A;
    --stf-outline: rgba(0, 0, 0, 0.09);
    --stf-outline-strong: rgba(0, 0, 0, 0.16);
    --stf-scrim-rgb: 255, 255, 255;
    --stf-shadow: 0 2px 12px rgba(0, 0, 0, 0.08);
    --stf-btn-radius: 9999px;
    --stf-fab-radius: 9999px;
    --stf-item-radius: 9999px;
    --stf-item-active-bg: #F2F3F5;
    --stf-bubble-radius: 9999px;
    --stf-bubble-border: 2.5px solid #257AFD;
    --stf-bubble-bg: #FFFFFF;
    --stf-ring-bg: rgba(255, 255, 255, 0.92);
    --stf-ring-color: #111111;
    --stf-refresh-bg: rgba(255, 255, 255, 0.90);
    --stf-refresh-color: #222222;
  }

  .stf-shell.design-one_ui.dark {
    --stf-surface: #1C1C1E;
    --stf-surface-muted: #2C2C2E;
    --stf-surface-glass: rgba(28, 28, 30, 0.88);
    --stf-on-surface: #F5F5F7;
    --stf-on-surface-muted: #A1A1A6;
    --stf-outline: rgba(255, 255, 255, 0.10);
    --stf-outline-strong: rgba(255, 255, 255, 0.18);
    --stf-scrim-rgb: 24, 24, 28;
    --stf-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);
    --stf-item-active-bg: #2C2C2E;
    --stf-bubble-bg: #222226;
    --stf-ring-bg: rgba(34, 34, 38, 0.92);
    --stf-ring-color: #F5F5F7;
    --stf-refresh-bg: rgba(34, 34, 38, 0.90);
    --stf-refresh-color: #F5F5F7;
  }

  /* ==================== MATERIAL DESIGN 3 / MATERIAL YOU TOKENS ==================== */
  .stf-shell.design-md3 {
    --stf-radius: var(--md-sys-shape-corner-extra-large, var(--ha-card-border-radius, 28px));
    --stf-accent: var(--md-sys-color-primary, var(--primary-color, #0B57D0));
    --stf-on-accent: var(--md-sys-color-on-primary, #FFFFFF);
    --stf-surface: var(--md-sys-color-surface-container-low, var(--ha-card-background, #F7F9FC));
    --stf-surface-muted: var(--md-sys-color-surface-container-highest, #E1E3E8);
    --stf-surface-glass: var(--md-sys-color-surface-container-high, #ECEEF4);
    --stf-on-surface: var(--md-sys-color-on-surface, var(--primary-text-color, #1B1B1F));
    --stf-on-surface-muted: var(--md-sys-color-on-surface-variant, var(--secondary-text-color, #44474F));
    --stf-outline: var(--md-sys-color-outline-variant, rgba(0, 0, 0, 0.12));
    --stf-outline-strong: var(--md-sys-color-outline, rgba(0, 0, 0, 0.24));
    --stf-scrim-rgb: 243, 246, 252;
    --stf-shadow: var(--ha-card-box-shadow, none);
    --stf-btn-radius: var(--md-sys-shape-corner-full, 9999px);
    --stf-fab-radius: var(--md-sys-shape-corner-large, 16px);
    --stf-item-radius: 18px;
    --stf-item-active-bg: var(--md-sys-color-secondary-container, #D3E3FD);
    --stf-bubble-radius: 24px;
    --stf-bubble-border: 2px solid var(--md-sys-color-primary, var(--primary-color, #0B57D0));
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

  .map-wrap.hide-zoom ha-map::part(zoom) {
    display: none;
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
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.14);
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
            rgba(var(--stf-scrim-rgb), 0.70) 48%,
            rgba(var(--stf-scrim-rgb), 0.0) 100%
          );
          pointer-events: none;
        }
        .design-md3 .bottom-scrim {
          height: 52%;
        }
        .btn-refresh {
          position: absolute;
          top: 12px;
          right: 12px;
          width: 38px;
          height: 38px;
          border-radius: var(--stf-btn-radius);
          background: var(--stf-refresh-bg);
          color: var(--stf-refresh-color);
          backdrop-filter: blur(8px);
        }
        .design-md3 .btn-refresh {
          width: 40px;
          height: 40px;
          backdrop-filter: none;
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
          box-shadow: 0 0 0 6px color-mix(in srgb, var(--stf-accent) 24%, transparent);
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
          box-shadow: 0 1px 4px rgba(0, 0, 0, 0.10);
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
          font-weight: 600;
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
          padding: 1px 7px;
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
            <button class="icon-btn btn-refresh" title="Update location" aria-label="Update location">${ICON_REFRESH}</button>
            <button class="icon-btn btn-ring" title="Ring device" aria-label="Ring device">${ICON_PHONE_RING}</button>
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
    this._btnRing = this.shadowRoot.querySelector(".btn-ring");
    this._infoBlock = this.shadowRoot.querySelector(".info-block");
    this._tsEl = this.shadowRoot.querySelector(".ts-line");
    this._nameEl = this.shadowRoot.querySelector(".name-line");
    this._metaEl = this.shadowRoot.querySelector(".meta-line");

    this._btnRefresh.addEventListener("click", (e) => {
      e.stopPropagation();
      this._handleRefresh();
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
      // Errors are surfaced by HA toast / logs
    } finally {
      setTimeout(() => {
        this._refreshing = false;
        this._btnRefresh?.classList.remove("spin");
      }, 900);
    }
  }

  async _handleRing() {
    if (!this._hass || !this._config.entity) return;
    const companions = findCompanionEntities(this._hass, this._config.entity);
    if (!companions.ringSwitch) return;
    const sw = this._hass.states[companions.ringSwitch];
    const service = sw && sw.state === "on" ? "turn_off" : "turn_on";
    try {
      await this._hass.callService("switch", service, { entity_id: companions.ringSwitch });
    } catch (_err) {
      // Handled by HA error notification
    }
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
    if (
      !this._lastMapEntity ||
      this._lastMapEntity !== entityId ||
      this._lastMapCoords !== this._coordsKey(entityId)
    ) {
      this._mapEl.entities = [entityId];
      this._lastMapEntity = entityId;
      this._lastMapCoords = this._coordsKey(entityId);
    }
  }

  _coordsKey(entityId) {
    const st = this._hass?.states?.[entityId];
    if (!st) return "";
    return `${st.attributes?.latitude},${st.attributes?.longitude},${st.state}`;
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
    this._btnRing.classList.toggle("hidden", !showRing);
    this._btnRing.classList.toggle("ringing", Boolean(ringState && ringState.state === "on"));

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
      title: "Find",
      design: "one_ui",
      map_height: 210,
      default_zoom: 15,
      show_badges: true,
    };
  }

  setConfig(config) {
    this._config = {
      title: "Find",
      design: "one_ui",
      map_height: 210,
      default_zoom: 15,
      show_badges: true,
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
    return Math.max(5, Math.round(((this._config.map_height || 210) + 92) / 50));
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
        .header-title {
          position: absolute;
          top: 14px;
          left: 16px;
          font-size: 18px;
          font-weight: 700;
          letter-spacing: -0.01em;
          color: var(--stf-on-surface);
          text-shadow: 0 1px 6px rgba(var(--stf-scrim-rgb), 0.9);
          pointer-events: none;
        }
        .design-md3 .header-title {
          font-size: 15px;
          font-weight: 600;
          padding: 6px 14px;
          border-radius: 9999px;
          background: var(--stf-surface-glass);
          text-shadow: none;
          box-shadow: 0 1px 4px rgba(0, 0, 0, 0.10);
        }
        .btn-more {
          position: absolute;
          top: 12px;
          right: 12px;
          width: 34px;
          height: 34px;
          border-radius: var(--stf-btn-radius);
          background: var(--stf-surface-glass);
          color: var(--stf-on-surface);
          border-color: var(--stf-outline);
          backdrop-filter: blur(8px);
        }
        .design-md3 .btn-more {
          width: 38px;
          height: 38px;
          backdrop-filter: none;
        }

        /* Floating Samsung Find / MD3 Speech Bubble */
        .bubble-anchor {
          position: absolute;
          top: 14px;
          left: 50%;
          transform: translateX(-50%);
          max-width: calc(100% - 116px);
          pointer-events: auto;
          display: flex;
          flex-direction: column;
          align-items: center;
        }
        .bubble {
          position: relative;
          background: var(--stf-bubble-bg);
          border: var(--stf-bubble-border);
          border-radius: var(--stf-bubble-radius);
          padding: 8px 18px;
          box-shadow: 0 4px 14px rgba(0, 0, 0, 0.14);
          text-align: center;
          cursor: pointer;
          transition: background-color 180ms var(--stf-ease-standard), border-color 180ms var(--stf-ease-standard);
        }
        .bubble::after {
          content: "";
          position: absolute;
          left: 50%;
          bottom: -9px;
          width: 14px;
          height: 14px;
          background: var(--stf-bubble-bg);
          border-right: var(--stf-bubble-border);
          border-bottom: var(--stf-bubble-border);
          transform: translateX(-50%) rotate(45deg);
          border-bottom-right-radius: 3px;
        }
        .bubble-name {
          font-size: 14.5px;
          font-weight: 700;
          color: var(--stf-on-surface);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          max-width: 240px;
        }
        .bubble-sub {
          margin-top: 2px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 6px;
          font-size: 12px;
          color: var(--stf-on-surface-muted);
          font-variant-numeric: tabular-nums;
          white-space: nowrap;
        }
        .bubble-badges {
          margin-top: 3px;
          display: flex;
          justify-content: center;
          gap: 6px;
          flex-wrap: wrap;
        }
        .bubble-badge {
          font-size: 10.5px;
          font-weight: 600;
          padding: 1px 7px;
          border-radius: 9999px;
          background: var(--stf-surface-muted);
          color: var(--stf-on-surface);
        }
        .design-md3 .bubble-badge {
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
          transition: color 150ms ease, transform 150ms ease;
        }
        .design-md3 .inline-refresh {
          color: var(--stf-accent);
        }
        .inline-refresh:hover {
          color: var(--stf-accent);
        }
        .inline-refresh svg {
          width: 15px;
          height: 15px;
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
            <div class="header-title"></div>
            <button class="icon-btn btn-more" title="Device details" aria-label="Device details">${ICON_CHEVRON_RIGHT}</button>
            <div class="bubble-anchor">
              <div class="bubble">
                <div class="bubble-name"></div>
                <div class="bubble-sub">
                  <span class="bubble-ts"></span>
                  <button class="inline-refresh" title="Update location" aria-label="Update location">${ICON_REFRESH}</button>
                </div>
                <div class="bubble-badges"></div>
              </div>
            </div>
          </div>
        </div>
        <div class="carousel-bar"></div>
      </ha-card>
    `;

    this._cardEl = this.shadowRoot.querySelector("ha-card");
    this._mapWrap = this.shadowRoot.querySelector(".map-wrap");
    this._titleEl = this.shadowRoot.querySelector(".header-title");
    this._btnMore = this.shadowRoot.querySelector(".btn-more");
    this._bubbleEl = this.shadowRoot.querySelector(".bubble");
    this._bubbleName = this.shadowRoot.querySelector(".bubble-name");
    this._bubbleTs = this.shadowRoot.querySelector(".bubble-ts");
    this._bubbleBadges = this.shadowRoot.querySelector(".bubble-badges");
    this._btnRefresh = this.shadowRoot.querySelector(".inline-refresh");
    this._carouselBar = this.shadowRoot.querySelector(".carousel-bar");

    this._btnMore.addEventListener("click", (e) => {
      e.stopPropagation();
      if (this._selectedEntity) fireMoreInfo(this, this._selectedEntity);
    });
    this._bubbleEl.addEventListener("click", (e) => {
      e.stopPropagation();
      if (this._selectedEntity) fireMoreInfo(this, this._selectedEntity);
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

    const height = Math.max(150, Number(this._config.map_height) || 210);
    this._mapWrap.style.height = `${height}px`;
    this._mapWrap.style.pointerEvents = this._config.interactive_map === false ? "none" : "auto";
    this._titleEl.textContent = this._config.title ?? "Find";

    const entities = this._getEntities();
    if (!entities.length) {
      this._bubbleName.textContent = "SmartThings Find";
      this._bubbleTs.textContent = "No tracked devices";
      this._bubbleBadges.innerHTML = "";
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

    this._bubbleName.textContent = st?.attributes?.friendly_name || this._selectedEntity;
    const ts = formatLastUpdated(this._hass, st, locState);
    this._bubbleTs.textContent = ts ? `Last updated: ${ts}` : "Last updated: —";

    const badges = [];
    if (this._config.show_badges !== false) {
      const batt = formatBatteryText(this._hass, companions);
      if (batt) badges.push(`<span class="bubble-badge">${batt}</span>`);
      if (dirState && dirState.state && dirState.state !== "unknown" && dirState.state !== "unavailable") {
        const dirText = this._hass.formatEntityState ? this._hass.formatEntityState(dirState) : dirState.state;
        badges.push(`<span class="bubble-badge">${dirText}</span>`);
      }
    }
    this._bubbleBadges.innerHTML = badges.join("");

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
      { name: "show_ring", label: "Show Ring button (if supported)", selector: { boolean: {} } },
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
      title: this._config.title ?? "Find",
      design: this._config.design || "one_ui",
      entities: this._config.entities || [],
      map_height: this._config.map_height ?? 210,
      default_zoom: this._config.default_zoom ?? 15,
      show_badges: this._config.show_badges !== false,
    };
    this._form.schema = [
      { name: "title", label: "Header title", selector: { text: {} } },
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
      { name: "show_badges", label: "Show battery & direction badges", selector: { boolean: {} } },
    ];
  }
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
    description: "Multi-device SmartThings Find map with scrollable device selector bar in Samsung One UI or Material Design 3 (Material You).",
    preview: true,
  });
}

console.info(
  `%c SMARTTHINGS-FIND-CARDS %c v${CARD_VERSION} `,
  "color:#fff;background:#257AFD;font-weight:700;padding:2px 6px;border-radius:4px 0 0 4px;",
  "color:#257AFD;background:#E8F1FF;font-weight:700;padding:2px 6px;border-radius:0 4px 4px 0;"
);
