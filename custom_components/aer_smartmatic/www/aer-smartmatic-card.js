/*
 * Card for Godrej Aer Smart Matic
 * A Home Assistant dashboard card for a Godrej Aer Smart Matic: its status,
 * Spray now, Auto spray and the spray schedules stored on the device.
 * Part of the aer_smartmatic integration, which serves and registers it.
 *
 * Copyright 2026 Rgitha. Licensed under the Apache License, Version 2.0.
 */

// Set by the integration in the URL it serves the card at: its version (v),
// and whether to wait for the frontend before registering (wait).
const URL_PARAMS = new URL(import.meta.url).searchParams;
const CARD_VERSION = URL_PARAMS.get("v") || "";
const MAX_SCHEDULES = 10;
// The device stores at most 144 sprays per schedule; a window is at most 24 h.
const MAX_SPRAYS = 144;
const DAY = 24 * 60;
const WEEK = 7 * DAY;
const INTERVALS = [10, 20, 40];
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABEL = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const DAY_LETTER = { mon: "M", tue: "T", wed: "W", thu: "T", fri: "F", sat: "S", sun: "S" };
const DAY_PRESETS = [
  ["Weekdays", ["mon", "tue", "wed", "thu", "fri"]],
  ["Weekends", ["sat", "sun"]],
  ["Every day", DAYS],
];
const TIME_PRESETS = [
  ["Morning", "07:00", "10:00"],
  ["Afternoon", "12:00", "15:00"],
  ["Evening", "17:00", "20:00"],
];

const toMinutes = (hhmm) => {
  const [h, m] = String(hhmm).split(":").map(Number);
  return h * 60 + m;
};
const toHHMM = (minutes) => {
  const m = Math.min(Math.max(minutes, 0), 24 * 60 - 1);
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
};
// Times are shown in the user's format (their Home Assistant profile: 12 or
// 24 hours, or their language's or browser's convention).
let timeFormat = null;
let timeFormatKey = "";
function setTimeFormat(locale) {
  const pref = locale && locale.time_format;
  const lang = (locale && locale.language) || undefined;
  const key = `${pref}|${lang}`;
  if (key === timeFormatKey) return;
  timeFormatKey = key;
  const opts = { hour: "numeric", minute: "2-digit" };
  if (pref === "12") opts.hour12 = true;
  if (pref === "24") Object.assign(opts, { hour: "2-digit", hourCycle: "h23" });
  try {
    timeFormat = new Intl.DateTimeFormat(pref === "system" ? undefined : lang, opts);
  } catch (e) {
    timeFormat = null;
  }
}
/** A time of day (minutes, or "HH:MM") as the user shows times. */
function showTime(value) {
  const minutes = typeof value === "number" ? value : toMinutes(value);
  const m = Math.min(Math.max(minutes, 0), 24 * 60 - 1);
  return timeFormat ? timeFormat.format(new Date(2000, 0, 1, Math.floor(m / 60), m % 60)) : toHHMM(m);
}
const esc = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/**
 * Sprays and the rounded end, as the device stores them (start + count x
 * interval). An end at or before the start is the next day (up to 24 h);
 * endMinutes then goes past 24:00.
 */
function rounded(start, end, interval) {
  const s = toMinutes(start);
  let e = toMinutes(end);
  if (e <= s) e += DAY;
  const sprays = Math.min(Math.floor((e - s) / interval), MAX_SPRAYS);
  return { sprays, endMinutes: s + Math.max(sprays, 0) * interval };
}

/** The rounded end as HH:MM, with "+1" when it is the next day. */
function endLabel(s) {
  const end = rounded(s.start, s.end, s.interval).endMinutes;
  return end > DAY ? `${showTime(end - DAY)} (+1)` : showTime(end);
}

/** A schedule's windows as [start, end) minutes from Monday 00:00, wrapping round the week. */
function windows(s) {
  const { endMinutes } = rounded(s.start, s.end, s.interval);
  const length = endMinutes - toMinutes(s.start);
  const out = [];
  for (const d of s.days) {
    const start = DAYS.indexOf(d) * DAY + toMinutes(s.start);
    out.push([start, Math.min(start + length, WEEK)]);
    if (start + length > WEEK) out.push([0, start + length - WEEK]);
  }
  return out;
}

/** A schedule from the sensor's attributes, in the card's own form. */
function fromAttribute(item) {
  const days = (item.days || [])
    .map((d) => String(d).slice(0, 3).toLowerCase())
    .filter((d) => DAYS.includes(d));
  return { days, start: String(item.start).slice(0, 5), end: String(item.end).slice(0, 5), interval: Number(item.interval) };
}

/** What the integration's set_schedules action expects for one schedule. */
const forAction = (s) => ({ days: [...s.days], start: s.start, end: s.end, interval: s.interval });

/** Problems with one schedule against the rest of the list (index = its place, or -1 if new). */
/** Pop-up actions that change the list or what's unfolded. */
const EDIT_ACTS = ["add", "edit", "fold", "day", "days", "time", "start", "end", "interval", "delete", "save", "discard"];

/** What's wrong with each schedule in the list (null if nothing). */
function listProblems(list) {
  return list.map((s, i) => {
    if (!s.days.length) return "Pick at least one day.";
    if (rounded(s.start, s.end, s.interval).sprays < 1) return `The window is shorter than one ${s.interval}-minute interval.`;
    // A window past midnight also counts against the next day's windows.
    const mine = windows(s);
    const clash = list.some((o, j) => j !== i && windows(o).some(([oS, oE]) => mine.some(([a, b]) => a < oE && oS < b)));
    return clash ? "Schedules are overlapping." : null;
  });
}

/** A new schedule: weekdays, in the first preset window that's still free. */
function newSchedule(list) {
  for (const [, start, end] of TIME_PRESETS) {
    const s = { days: ["mon", "tue", "wed", "thu", "fri"], start, end, interval: 20 };
    if (!listProblems([...list, s])[list.length]) return s;
  }
  return { days: ["mon", "tue", "wed", "thu", "fri"], start: TIME_PRESETS[0][1], end: TIME_PRESETS[0][2], interval: 20 };
}

function ago(iso) {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

// --- the device's entities -----------------------------------------------------

// The integration's domain.
const DOMAIN = "aer_smartmatic";
// The integration's name, as Home Assistant lists it.
const INTEGRATION = "Godrej Aer Smart Matic";

/** The integration's entities on a device, by role. */
function deviceEntities(hass, deviceId) {
  const mine = Object.values(hass.entities || {}).filter(
    (e) => e.device_id === deviceId && e.platform === DOMAIN
  );
  const find = (domain, key, suffix) => {
    const hit =
      mine.find((e) => e.entity_id.startsWith(`${domain}.`) && e.translation_key === key) ||
      mine.find((e) => e.entity_id.startsWith(`${domain}.`) && suffix && e.entity_id.endsWith(suffix));
    return hit ? hit.entity_id : null;
  };
  const byState = (test) => {
    const hit = mine.find((e) => test(e.entity_id, hass.states[e.entity_id]));
    return hit ? hit.entity_id : null;
  };
  return {
    found: mine.length > 0,
    status: find("sensor", "device_status", "_status"),
    battery: find("sensor", "battery_level", "_battery"),
    refill: find("sensor", "refill_level", "_refill_level"),
    updated: find("sensor", "updated_last", "_updated_last"),
    auto: find("select", "spray_mode", "_spray_auto"),
    spray: find("button", "spray_now", "_spray_now"),
    resetRefill: find("button", "reset_refill", "_reset_refill_level"),
    identify: byState((id, st) => id.startsWith("button.") && st && st.attributes.device_class === "identify"),
    schedules: byState((id, st) => id.startsWith("sensor.") && st && Array.isArray(st.attributes.schedules)),
  };
}

/** Devices that have the integration's entities. */
const smartMatics = (hass) => [
  ...new Set(
    Object.values(hass.entities || {})
      .filter((e) => e.platform === DOMAIN && e.device_id)
      .map((e) => e.device_id)
  ),
];

/** The next start of any schedule after now: { schedule, day }. */
function nextRun(list, now) {
  const nowMin = ((now.getDay() + 6) % 7) * DAY + now.getHours() * 60 + now.getMinutes();
  let best = null;
  for (const s of list) {
    for (const d of s.days) {
      let at = DAYS.indexOf(d) * DAY + toMinutes(s.start);
      if (at <= nowMin) at += WEEK;
      if (!best || at < best.at) best = { at, schedule: s };
    }
  }
  if (!best) return null;
  const ahead = Math.floor(best.at / DAY) - Math.floor(nowMin / DAY);
  const day = ahead === 0 ? "today" : ahead === 1 ? "tomorrow" : DAY_LABEL[DAYS[Math.floor(best.at / DAY) % 7]];
  return { schedule: best.schedule, day, inMinutes: best.at - nowMin };
}

const ICON = {
  spray: "M3,15H5V21H3V15M7,13H13V21H7V13M8,9H12V12H8V9M15,11H17V13H15V11M19,9H21V11H19V9M19,13H21V15H19V13M17,7H19V9H17V7M17,15H19V17H17V15Z",
  refresh: "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
  chevronUp: "M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z",
  dots: "M12,16A2,2 0 0,1 14,18A2,2 0 0,1 12,20A2,2 0 0,1 10,18A2,2 0 0,1 12,16M12,10A2,2 0 0,1 14,12A2,2 0 0,1 12,14A2,2 0 0,1 10,12A2,2 0 0,1 12,10M12,4A2,2 0 0,1 14,6A2,2 0 0,1 12,8A2,2 0 0,1 10,6A2,2 0 0,1 12,4Z",
};
/** Home Assistant's battery icons (Material Design), by level. */
const BATTERY_ICONS = {
  b0: "M14 20H6V6H14M14.67 4H13V2H7V4H5.33C4.6 4 4 4.6 4 5.33V20.67C4 21.4 4.6 22 5.33 22H14.67C15.4 22 16 21.4 16 20.67V5.33C16 4.6 15.4 4 14.67 4M21 7H19V13H21V8M21 15H19V17H21V15Z",
  b10: "M16,18H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  b20: "M16,17H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  b30: "M16,15H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  b40: "M16,14H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  b50: "M16,13H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  b60: "M16,12H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  b70: "M16,10H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  b80: "M16,9H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  b90: "M16,8H8V6H16M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  b100: "M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
  bunk: "M15.07,12.25L14.17,13.17C13.63,13.71 13.25,14.18 13.09,15H11.05C11.16,14.1 11.56,13.28 12.17,12.67L13.41,11.41C13.78,11.05 14,10.55 14,10C14,8.89 13.1,8 12,8A2,2 0 0,0 10,10H8A4,4 0 0,1 12,6A4,4 0 0,1 16,10C16,10.88 15.64,11.68 15.07,12.25M13,19H11V17H13M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.66C6,21.4 6.6,22 7.33,22H16.67C17.4,22 18,21.4 18,20.66V5.33C18,4.59 17.4,4 16.67,4Z",
};
/** The battery icon for a level, as Home Assistant picks it: rounded to
 * 10 %, an alert icon at 0. */
function batteryIcon(value) {
  const n = Number(value);
  if (value == null || value === "" || !Number.isFinite(n)) return BATTERY_ICONS.bunk;
  const step = Math.round(n / 10) * 10;
  return step <= 0 ? BATTERY_ICONS.b0 : BATTERY_ICONS[`b${Math.min(step, 100)}`];
}

/** The refill level: the standard outline drop (Material Design), filled
 * from the bottom. The level is measured over the clear space inside the
 * outline (y 6.91 to 18.23 of 24), not the drop's full height. */
const DROP_OUTLINE = "M12,3.77L11.25,4.61C11.25,4.61 9.97,6.06 8.68,7.94C7.39,9.82 6,12.07 6,14.23A6,6 0 0,0 12,20.23A6,6 0 0,0 18,14.23C18,12.07 16.61,9.82 15.32,7.94C14.03,6.06 12.75,4.61 12.75,4.61L12,3.77M12,6.9C12.44,7.42 12.84,7.85 13.68,9.07C14.89,10.83 16,13.07 16,14.23C16,16.45 14.22,18.23 12,18.23C9.78,18.23 8,16.45 8,14.23C8,13.07 9.11,10.83 10.32,9.07C11.16,7.85 11.56,7.42 12,6.9Z";
const DROP_FILLED = "M12,20A6,6 0 0,1 6,14C6,10 12,3.25 12,3.25C12,3.25 18,10 18,14A6,6 0 0,1 12,20Z";
const DROP_INSIDE = [6.91, 18.23];
let dropClip = 0;
function levelDrop(level, size) {
  const v = Math.max(0, Math.min(100, Number(level) || 0)) / 100;
  const [top, bottom] = DROP_INSIDE;
  const y = bottom - (bottom - top) * v;
  const id = `aer-drop-${++dropClip}`;
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true">
    <defs><clipPath id="${id}"><rect x="0" y="${y.toFixed(2)}" width="24" height="${(24 - y).toFixed(2)}"/></clipPath></defs>
    <path fill="currentColor" d="${DROP_OUTLINE}"/>
    <path fill="currentColor" clip-path="url(#${id})" d="${DROP_FILLED}"/></svg>`;
}

const svg = (path, size = 20) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="${path}"/></svg>`;

const LOW_BATTERY = 20;
const LOW_REFILL = 15;

/** "just now", "5m ago", "2h ago", "3d ago". */
function shortAgo(iso) {
  if (!iso) return "";
  const minutes = Math.floor(Math.max(0, Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

class AerSmartMaticCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._list = null; // the schedules shown (edited copy while dirty)
    this._baseline = null; // the device's list the edits started from
    this._dirty = false;
    this._open = -1; // the schedule unfolded for editing in the pop-up
    this._conflict = false;
    // Actions in progress, each by kind ("save", "refresh", "skip", "spray",
    // "auto", "reset", "identify"). Several can be under way at once: a
    // Spray pressed while Auto is being sent leaves Auto's own state alone.
    this._pending = new Set();
    this._error = null;
    this._menu = false;
    this._dialog = null; // the schedules pop-up's host, on the page
    this._dialogOpen = false;
    this._confirmClose = false;
  }

  setConfig(config) {
    if (!config || !config.device) {
      throw new Error("Pick a Smart Matic device ('device').");
    }
    this._config = config;
    this._render();
  }

  static getConfigElement() {
    return document.createElement("aer-smartmatic-card-editor");
  }

  static getStubConfig(hass) {
    const device = smartMatics(hass)[0];
    return device ? { device } : { device: "" };
  }

  disconnectedCallback() {
    // Leaving the dashboard: close without touching the history.
    this._closeDialog(true, true);
    this._menu = false;
    this._watchOutsideMenu();
  }

  /** While the ⋮ menu is open, a tap anywhere outside it closes it; not
   * while Reset refill level or Identify waits for the device. */
  _watchOutsideMenu() {
    if (this._menu && !this._outsideMenu) {
      this._outsideMenu = (ev) => {
        if (this._is("reset") || this._is("identify")) return;
        const root = this.shadowRoot;
        const inside = ev.composedPath().some((n) => n && n.classList && root &&
          (n.classList.contains("menu") || n.classList.contains("more")) && root.contains(n));
        if (inside) return;
        this._menu = false;
        this._render();
      };
      document.addEventListener("pointerdown", this._outsideMenu, true);
    } else if (!this._menu && this._outsideMenu) {
      document.removeEventListener("pointerdown", this._outsideMenu, true);
      this._outsideMenu = null;
    }
  }

  /** Sections view: half a row by default, height as needed (it doesn't
   * change by itself: a schedule starting or ending keeps the same lines). */
  getGridOptions() {
    return { columns: 6, min_columns: 4, rows: "auto" };
  }

  getCardSize() {
    return 5;
  }

  get _deviceId() {
    return this._config.device;
  }

  set hass(hass) {
    this._hass = hass;
    setTimeFormat(hass.locale);
    const ents = deviceEntities(hass, this._deviceId);
    // Redraw only when one of ours changed, so an open editor keeps its focus.
    const watched = Object.values(ents).filter((v) => typeof v === "string" && v.includes("."));
    const snapshot = [...watched.map((id) => hass.states[id]), hass.devices && hass.devices[this._deviceId], ents.found];
    const unchanged =
      this._snapshot &&
      snapshot.length === this._snapshot.length &&
      snapshot.every((s, i) => s === this._snapshot[i]);
    this._ents = ents;
    this._snapshot = snapshot;
    if (unchanged && !this._forceRender) return;
    this._forceRender = false;
    const st = this._sched;
    if (st && !this._dirty) {
      this._list = (st.attributes.schedules || []).map(fromAttribute);
      this._baseline = this._list.map((s) => ({ ...s, days: [...s.days] }));
    }
    this._render();
  }

  get _sched() {
    return this._ents && this._ents.schedules ? this._hass.states[this._ents.schedules] : null;
  }

  _state(role) {
    const id = this._ents && this._ents[role];
    return id ? this._hass.states[id] : null;
  }

  /** Why the card has no device to show, as one line; null when it has one. */
  get _problem() {
    const devices = this._hass && this._hass.devices;
    if (devices && !devices[this._config.device]) return "This device no longer exists.";
    if (!this._ents || !this._ents.found) return `${INTEGRATION} isn't set up for this device.`;
    const probe = this._state("status") || this._state("battery") || this._sched;
    if (!probe || probe.attributes.restored) return `${INTEGRATION} isn't running.`;
    return null;
  }

  /** The one-line notice over a greyed-out card. */
  get _notice() {
    return this._problem || "The device can't be reached right now.";
  }

  /** The device isn't heard, or isn't there: its readings and controls are unavailable. */
  get _unreachable() {
    if (this._problem) return true;
    const probe = this._state("battery") || this._sched;
    return !probe || probe.state === "unavailable";
  }

  /** The number of the schedule the device is running now, if any. */
  get _runningNow() {
    const state = this._sched;
    return (state && state.state !== "unavailable" && state.attributes.running) || null;
  }

  // --- device actions -------------------------------------------------------

  get _domain() {
    return DOMAIN;
  }

  get _target() {
    return { device_id: this._deviceId };
  }

  /**
   * How Refresh reads the device. Home Assistant's own update_entity always
   * exists, but it only reads the device once the integration supports it,
   * which it does when it no longer registers its own update_status action.
   * So: update_entity, unless the integration still has update_status.
   */
  get _refreshCall() {
    const services = this._hass.services && this._hass.services[this._domain];
    if (services && services.update_status) {
      return { domain: this._domain, service: "update_status", target: this._target };
    }
    return { domain: "homeassistant", service: "update_entity", target: { entity_id: this._ents.schedules } };
  }

  /** A schedule action: errors show in the card (not as a toast). */
  /** Whether an action of this kind is in progress. */
  _is(kind) {
    return this._pending.has(kind);
  }

  /** Whether any action is in progress. */
  get _any() {
    return this._pending.size > 0;
  }

  async _call(kind, service, data, domain = this._domain, target = this._target) {
    this._pending.add(kind);
    this._error = null;
    this._render();
    try {
      await this._hass.callService(domain, service, data, target, false);
      return true;
    } catch (err) {
      if (kind === "save" && /changed since they were read|schedules_changed/i.test(`${err.message} ${err.translation_key || ""}`)) {
        this._conflict = true;
      } else {
        this._error = err.message || String(err);
      }
      return false;
    } finally {
      this._pending.delete(kind);
      this._render();
    }
  }

  /** A control: Home Assistant shows any error as a toast, as on the device page. */
  async _control(kind, domain, service, data, target) {
    this._pending.add(kind);
    this._render();
    try {
      await this._hass.callService(domain, service, data, target);
    } catch (err) {
      /* shown by Home Assistant */
    } finally {
      this._pending.delete(kind);
      this._render();
    }
  }

  _moreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  /** close: the user had tried to close the pop-up; close it once saved. */
  async _save(overwrite, close = false) {
    this._conflict = false;
    const data = { schedules: this._list.map(forAction) };
    if (!overwrite) data.expected = this._baseline.map(forAction);
    if (await this._call("save", "set_schedules", data)) {
      this._dirty = false; // the read-back updates the sensor and the list
      this._open = -1;
      if (close) this._closeDialog(true);
      this._reload();
    }
  }

  _keepDevice() {
    this._conflict = false;
    this._dirty = false;
    this._reload();
  }

  /** Show the device's list again (from the sensor), dropping local edits. */
  _reload() {
    this._forceRender = true;
    this.hass = this._hass;
  }

  // --- rendering -------------------------------------------------------------

  _render() {
    this._watchOutsideMenu();
    if (!this._config || !this.shadowRoot) return;
    let html;
    if (!this._hass) {
      html = "";
    } else if (!this._config.device) {
      html = `<div class="banner warn">Pick a Smart Matic device in the card's settings.</div>`;
    } else {
      html = this._renderDevice();
    }
    this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card>${html}</ha-card>`;
    this._bind(this.shadowRoot);
    this._renderDialog();
  }

  _bind(root) {
    root.querySelectorAll("[data-act]").forEach((el) => {
      el.addEventListener(el.tagName === "INPUT" ? "change" : "click", (ev) => this._onAction(ev, el));
    });
  }

  _deviceName() {
    if (this._config.title) return this._config.title;
    const device = this._hass.devices && this._hass.devices[this._deviceId];
    return (device && (device.name_by_user || device.name)) || "Smart Matic";
  }

  _areaName() {
    const device = this._hass.devices && this._hass.devices[this._deviceId];
    const area = device && device.area_id && this._hass.areas && this._hass.areas[device.area_id];
    return area ? area.name : "";
  }

  _fmt(state) {
    if (!state) return "";
    return this._hass.formatEntityState ? this._hass.formatEntityState(state) : state.state;
  }

  _renderDevice() {
    const off = this._unreachable;
    const status = this._state("status");
    const battery = this._state("battery");
    const refill = this._state("refill");
    const updated = this._state("updated");
    const auto = this._state("auto");
    const mode = status ? status.state : "unknown";
    const sub = this._areaName();
    const read = updated && !["unknown", "unavailable"].includes(updated.state) ? updated.state : null;
    const pct = (s) => (s && !["unknown", "unavailable"].includes(s.state) ? `${Math.round(Number(s.state))}%` : "–");
    const low = battery && Number(battery.state) < LOW_BATTERY;
    const refillPct = refill ? Math.max(0, Math.min(100, Number(refill.state) || 0)) : 0;
    const refillLow = refill && Number.isFinite(Number(refill.state)) && Number(refill.state) < LOW_REFILL;
    const menu = this._menu
      ? `<div class="menu" role="menu">
          ${this._ents.resetRefill ? `<button role="menuitem" data-act="press" data-entity="${this._ents.resetRefill}" ${off || this._any ? "disabled" : ""}>${this._is("reset") ? "Resetting…" : "Reset refill level"}</button>` : ""}
          ${this._ents.identify ? `<button role="menuitem" data-act="press" data-entity="${this._ents.identify}" ${off || this._any ? "disabled" : ""}>${this._is("identify") ? "Identifying…" : "Identify"}</button>` : ""}
        </div>`
      : "";
    return `
      <div class="top">
        <div class="names"><div class="area">${esc(sub)}</div><div class="title">${esc(this._deviceName())}</div></div>
        <button class="icon" data-act="refresh" title="Read the device now" aria-label="Refresh" ${this._any || this._problem ? "disabled" : ""}>${this._is("refresh") ? `<span class="spin">${svg(ICON.refresh)}</span>` : svg(ICON.refresh)}</button>
        <button class="more" data-act="menu" aria-label="More" aria-haspopup="menu">${svg(ICON.dots)}</button>
        ${menu}
      </div>
      ${off ? `<div class="banner">${esc(this._notice)}</div>` : ""}
      ${this._error ? `<div class="banner warn">${esc(this._error)}</div>` : ""}
      <div class="strip ${off ? "dim" : ""}">
        <span class="mode m-${esc(mode)}"><i></i>${esc(this._fmt(status) || "Unknown")}</span>
        <button class="kv ${low ? "low" : ""}" data-act="info" data-entity="${esc(this._ents.battery || "")}" title="Battery">${svg(batteryIcon(battery && battery.state), 18)}${pct(battery)}</button>
        <button class="kv" data-act="info" data-entity="${esc(this._ents.refill || "")}" title="Refill level"><span class="lvl ${refillLow ? "low" : ""}">${levelDrop(refillPct, 21)}</span>${pct(refill)}</button>
        ${read ? `<span class="ago" title="Status read ${esc(ago(read))}">${esc(shortAgo(read))}</span>` : ""}
      </div>
      <div class="ctrl ${off ? "dim" : ""}">
        <button class="sprayb" data-act="press" data-entity="${esc(this._ents.spray || "")}" ${this._is("spray") ? "disabled" : ""}>${svg(ICON.spray)}Spray now</button>
        <div class="autow"><div class="tiny">Auto · minutes</div>${this._renderAuto(auto, mode === "schedule")}</div>
      </div>
      ${this._sched ? this._renderSchedules() : ""}`;
  }

  _renderAuto(auto, held) {
    const options = (auto && auto.attributes.options) || ["Off", "Every 10 min", "Every 20 min", "Every 40 min"];
    const label = (o) => (/(\d+)/.exec(o) || [null, o])[1];
    // Until the device acknowledges a choice, the group is greyed out and the
    // chosen option shows faintly selected (like Spray now while it sends).
    const sending = this._is("auto");
    return `<div class="seg4 ${held ? "held" : ""} ${sending ? "sending" : ""}" role="radiogroup" aria-label="Auto spray" aria-busy="${sending}">${options
      .map((o) => {
        const pend = sending && o === this._pendingAuto;
        const on = !sending && auto && auto.state === o;
        return `<button role="radio" aria-checked="${!!on}" class="${on ? "on" : ""} ${pend ? "pend" : ""}" data-act="auto" data-option="${esc(o)}" ${sending ? "disabled" : ""}>${esc(label(o))}</button>`;
      })
      .join("")}</div>`;
  }

  _renderSchedules() {
    const state = this._sched;
    if (!state || state.state === "unavailable") return "";
    if (this._list === null || state.state === "unknown") {
      return `<div class="sect"><b>Schedules</b></div><div class="banner">The schedules haven't been read yet.</div>`;
    }
    if (this._config.show_schedules) return this._renderInline(state, true);
    // One line, always the same height: the running schedule, or the next.
    return `
      <div class="sect"><b>Schedules</b><button class="link" data-act="dialog">Show all</button></div>
      ${this._renderSlot(state)}`;
  }

  /** The device's schedules, as last read: the card shows only these, never
   * unsaved edits from the pop-up. */
  _deviceList(state) {
    return ((state && state.attributes.schedules) || []).map(fromAttribute);
  }

  _renderSlot(state) {
    const list = this._deviceList(state);
    const runningAt = (state.attributes.running || 0) - 1;
    if (list[runningAt]) {
      const s = list[runningAt];
      return `<div class="slot"><span class="chipx">Running</span><b>${esc(showTime(s.start))} – ${esc(endLabel(s))}</b><span class="grow"></span>
        <button class="btn small" data-act="skip" ${this._any ? "disabled" : ""}>${this._is("skip") ? "Skipping…" : "Skip"}</button></div>`;
    }
    const next = nextRun(list, new Date());
    if (!next) {
      return `<div class="slot"><span class="slabel">No schedules</span><span class="grow"></span>
        <button class="btn small" data-act="add-pop">+ Add</button></div>`;
    }
    return `<div class="slot"><span class="chipn">Next</span><b>${esc(showTime(next.schedule.start))} – ${esc(endLabel(next.schedule))}</b><span class="grow"></span><span class="slabel">${esc(next.day)}</span></div>`;
  }

  /** Every schedule, read-only; tap one (or Edit) to edit it in the pop-up. */
  _renderInline(state, heading) {
    const list = this._deviceList(state);
    const runningAt = (state.attributes.running || 0) - 1;
    const nextAt = this._nextIndex(list);
    const rows = list.map((s, i) => this._renderRow(s, i, runningAt === i, "edit-pop", "", nextAt === i)).join("");
    return `
      ${heading ? `<div class="sect"><b>Schedules</b><button class="link" data-act="dialog">Edit</button></div>` : ""}
      ${rows || `<div class="slot"><span class="slabel">No schedules</span><span class="grow"></span><button class="btn small" data-act="add-pop">+ Add</button></div>`}
      ${heading ? "" : `<div class="foot"><button class="btn" data-act="dialog">Edit schedules</button></div>`}`;
  }

  /** The list index of the running schedule: the device's running one, if
   * it is still in the list unchanged (local edits may move or change it). */
  _runningIndex(state) {
    const running = state.attributes.running;
    if (!running) return -1;
    if (!this._dirty) return running - 1;
    const same = (a, b) => a && b && JSON.stringify(forAction(a)) === JSON.stringify(forAction(b));
    const original = this._baseline[running - 1];
    return this._list.findIndex((s) => same(s, original));
  }

  /** One schedule, always the same height: running shows inside it. */
  /** The list index of the schedule starting next, however far ahead. */
  _nextIndex(list = this._list) {
    const next = list ? nextRun(list, new Date()) : null;
    return next ? list.indexOf(next.schedule) : -1;
  }

  _renderRow(s, i, isRunning, act, dim = "", isNext = false, problem = null) {
    const end = endLabel(s);
    return `
        <div class="row ${dim} ${problem ? "bad" : ""}" data-act="${act}" data-index="${i}" role="button" tabindex="0" aria-label="Edit ${esc(showTime(s.start))} to ${esc(end)}">
          <b class="t">${esc(showTime(s.start))} – ${esc(end)}</b>
          <div class="days">${DAYS.map((d) => `<span class="d ${s.days.includes(d) ? "on" : ""}" title="${DAY_LABEL[d]}">${DAY_LETTER[d]}</span>`).join("")}</div>
          <div class="every">${isRunning ? `<span class="chipx">Running</span>` : isNext ? `<span class="chipn">Next</span>` : ""}every ${s.interval} min</div>
          ${isRunning ? `<button class="skipl" data-act="skip" ${this._any ? "disabled" : ""}>${this._is("skip") ? "Skipping…" : "Skip"}</button>` : "<span></span>"}
          ${problem ? `<div class="prob">${esc(problem)}</div>` : ""}
        </div>`;
  }

  /** The pop-up's list: edit in place, add, save. */
  _renderDialogList(state) {
    const runningAt = this._runningIndex(state);
    // Next is worked out from the list as it stands, edits included.
    const nextAt = this._nextIndex();
    const bad = listProblems(this._list);
    const broken = bad.some(Boolean);
    const rows = this._list
      .map((s, i) => (i === this._open ? this._renderEditor(i, bad[i], runningAt === i, nextAt === i) : this._renderRow(s, i, runningAt === i, "edit", this._is("refresh") ? "dim" : "", nextAt === i, bad[i])))
      .join("");
    // Trying to close with changes asks first; Save with a problem left
    // says to check the schedules. Editing again clears both.
    // Save finding the device's list changed asks keep or overwrite.
    const conflict = this._conflict;
    const ask = !conflict && this._confirmClose && this._dirty;
    const check = !conflict && this._check && broken;
    const text = conflict ? "Device schedules changed" : ask ? "Discard changes?" : check ? "Check the schedules" : this._dirty ? "Changes not saved" : "No changes";
    return `
      ${rows || `<div class="empty">No schedules.</div>`}
      <div class="foot">
        <button class="btn" data-act="add" ${this._list.length >= MAX_SCHEDULES || this._any ? "disabled" : ""}>+ Add schedule</button>
      </div>
      <div class="savebar ${ask || check || conflict ? "ask" : ""}">
        <span class="${this._dirty ? "" : "quiet"}">${text}${this._dirty && !ask && !check && !conflict && this._runningNow ? `<small>Saving restarts running schedule.</small>` : ""}</span>
        ${conflict ? `
        <button class="btn" data-act="keep" ${this._any ? "disabled" : ""}>Keep device's</button>
        <button class="btn primary" data-act="overwrite" ${this._any ? "disabled" : ""}>${this._is("save") ? "Saving…" : "Overwrite"}</button>` : `
        ${ask ? `<button class="btn danger" data-act="discard" ${this._any ? "disabled" : ""}>Discard</button>` : ""}
        <button class="btn primary" data-act="save" ${this._any || !this._dirty ? "disabled" : ""}>${this._is("save") ? "Saving…" : "Save"}</button>`}
      </div>`;
  }

  // --- the schedules pop-up -------------------------------------------------

  _openDialog() {
    if (!this._dialogOpen) {
      // Back (e.g. on a phone) closes the pop-up instead of the dashboard.
      history.pushState({ ...(history.state || {}), aerSmartMaticDialog: true }, "");
      this._onPop = () => this._onBack();
      window.addEventListener("popstate", this._onPop);
    }
    const opening = !this._dialogOpen;
    this._dialogOpen = true;
    this._confirmClose = false;
    this._check = false;
    this._error = null;
    // Edit from the device's current list: read it again unless the last
    // read is under a minute old. Editing waits for the read.
    const st = this._sched;
    const readAt = st && st.attributes.last_read ? new Date(st.attributes.last_read).getTime() : 0;
    if (opening && st && st.state !== "unavailable" && !this._any && Date.now() - readAt > 60000) {
      const { domain, service, target } = this._refreshCall;
      this._control("refresh", domain, service, {}, target);
    }
  }

  /** The edited list differs from the device's (Done with nothing
   * changed, or changes undone by hand, leave nothing to save). */
  _differs() {
    const key = (l) => JSON.stringify(l.map(forAction));
    return key(this._list) !== key(this._baseline);
  }

  /** Something would be lost by closing. */
  get _hasChanges() {
    return this._dirty;
  }

  /** Back pressed while the pop-up is open. */
  _onBack() {
    if (!this._dialogOpen) return;
    if (this._hasChanges) {
      // Ask first, and stay: put the pop-up's history entry back.
      history.pushState({ ...(history.state || {}), aerSmartMaticDialog: true }, "");
      this._confirmClose = true;
      this._render();
      return;
    }
    this._closeDialog(true, true);
  }

  /** Close, unless there are unsaved changes: then ask first.
   * fromHistory: Back already removed the pop-up's history entry. */
  _closeDialog(force = false, fromHistory = false) {
    if (!force && this._hasChanges) {
      this._confirmClose = true;
      this._render();
      return;
    }
    if (this._onPop) {
      window.removeEventListener("popstate", this._onPop);
      this._onPop = null;
      if (!fromHistory && history.state && history.state.aerSmartMaticDialog) history.back();
    }
    this._dialogOpen = false;
    this._confirmClose = false;
    this._open = -1;
    this._conflict = false;
    if (this._dirty) {
      this._dirty = false;
      this._reload();
    }
    if (this._dialog) {
      this._dialog.remove();
      this._dialog = null;
    }
  }

  _renderDialog() {
    const state = this._sched;
    if (!this._dialogOpen || !state) {
      if (this._dialog) this._closeDialog(true);
      return;
    }
    if (!this._dialog) {
      // On the page, not in the card: nothing around the card can clip it.
      this._dialog = document.createElement("div");
      this._dialog.attachShadow({ mode: "open" });
      this._dialog.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape") this._closeDialog();
      });
      document.body.appendChild(this._dialog);
    }
    const unreachable = state.state === "unavailable";
    const sub = [this._areaName(), this._deviceName()].filter(Boolean).join(" · ");
    const body = unreachable
      ? `<div class="banner">${esc(this._notice)}</div>`
      : this._list === null
        ? `<div class="banner">The schedules haven't been read yet.</div>`
        : this._renderDialogList(state);
    this._dialog.shadowRoot.innerHTML = `
      <style>${STYLE}${DIALOG_STYLE}</style>
      <div class="ov" data-act="backdrop">
        <div class="dlg" role="dialog" aria-modal="true" aria-label="Schedules" tabindex="-1">
          <div class="dh">
            <button class="x" data-act="close-dialog" aria-label="Close">✕</button>
            <div class="dtl"><b>Schedules</b><span>${esc(sub)}</span></div>
            <button class="icon" data-act="refresh" title="Read the schedules from the device" aria-label="Refresh" ${this._any || this._problem ? "disabled" : ""}>${this._is("refresh") ? `<span class="spin">${svg(ICON.refresh)}</span>` : svg(ICON.refresh)}</button>
          </div>
          ${this._list && !unreachable ? `<div class="used">${this._is("refresh") ? "Reading schedules…" : `${this._list.length} of ${MAX_SCHEDULES} used`}</div>` : ""}
          ${this._error ? `<div class="banner warn">${esc(this._error)}</div>` : ""}
          ${body}
        </div>
      </div>`;
    this._bind(this._dialog.shadowRoot);
    const dlg = this._dialog.shadowRoot.querySelector(".dlg");
    if (dlg && !this._dialog.contains(document.activeElement)) dlg.focus({ preventScroll: true });
  }

  /** isRunning / isNext: as on the folded row, so unfolding keeps them. */
  _renderEditor(index, problem, isRunning = false, isNext = false) {
    const s = this._list[index];
    const r = rounded(s.start, s.end, s.interval);
    const nextDay = r.endMinutes > DAY;
    const hint = r.sprays >= 1
      ? `Ends at ${showTime(nextDay ? r.endMinutes - DAY : r.endMinutes)}${nextDay ? " the next day" : ""} (${r.sprays} spray${r.sprays === 1 ? "" : "s"})`
      : "";
    return `
      <div class="editor inline ${problem ? "bad" : ""} ${this._is("refresh") ? "dim" : ""}" role="group" aria-label="Edit schedule ${index + 1}">
        <div class="etitle" data-act="fold" role="button" tabindex="0" aria-label="Fold">
          <span>${esc(showTime(s.start))} – ${esc(endLabel(s))}</span>
          <span class="ehead">
            ${isRunning ? `<span class="chipx">Running</span><button class="skipl" data-act="skip" ${this._any ? "disabled" : ""}>${this._is("skip") ? "Skipping…" : "Skip"}</button>` : isNext ? `<span class="chipn">Next</span>` : ""}
            <span class="fold" aria-hidden="true">${svg(ICON.chevronUp)}</span>
          </span>
        </div>
        <div class="lbl"><span>Days</span>${DAY_PRESETS.map(([label], i) => `<button class="chip" data-act="days" data-preset="${i}">${label}</button>`).join("")}</div>
        <div class="dayrow">${DAYS.map((d) => `<button class="dt ${s.days.includes(d) ? "on" : ""}" data-act="day" data-day="${d}" aria-pressed="${s.days.includes(d)}" title="${DAY_LABEL[d]}">${DAY_LETTER[d]}</button>`).join("")}</div>
        <div class="lbl"><span>Time</span>${TIME_PRESETS.map(([label], i) => `<button class="chip" data-act="time" data-preset="${i}">${label}</button>`).join("")}</div>
        <div class="two">
          <label>Start<input type="time" data-act="start" value="${esc(s.start)}"></label>
          <label>End<input type="time" data-act="end" value="${esc(s.end)}"></label>
        </div>
        <div class="lbl"><span>Interval</span></div>
        <div class="seg">${INTERVALS.map((n) => `<button class="${s.interval === n ? "on" : ""}" data-act="interval" data-value="${n}">${n} min</button>`).join("")}</div>
        <div class="hint">${esc(hint)}</div>
        ${problem ? `<div class="prob">${esc(problem)}</div>` : ""}
        <div class="acts">
          <button class="btn danger" data-act="delete">Delete</button>
          <span></span>
        </div>
      </div>`;
  }

  _onAction(ev, el) {
    const act = el.dataset.act;
    if (act !== "edit" && act !== "edit-pop" && act !== "backdrop") ev.stopPropagation();
    const e = this._open >= 0 ? this._list[this._open] : null;
    if (this._is("refresh") && EDIT_ACTS.includes(act)) return;
    const menuItem = act === "press" && [this._ents.resetRefill, this._ents.identify].includes(el.dataset.entity);
    if (act !== "menu" && !menuItem) this._menu = false;
    // Save or Discard after trying to close finishes closing; anything
    // else carries on editing.
    const closing = this._confirmClose;
    if (closing && act !== "close-dialog" && act !== "backdrop") this._confirmClose = false;
    if (act !== "save") this._check = false;
    // Editing again also drops a pending keep-or-overwrite question.
    if (this._conflict && act !== "keep" && act !== "overwrite" && act !== "close-dialog" && act !== "backdrop") this._conflict = false;
    switch (act) {
      case "menu":
        this._menu = !this._menu;
        break;
      case "press": {
        const id = el.dataset.entity;
        if (!id) return;
        if (menuItem) {
          // The menu stays open, showing "Resetting…" or "Identifying…",
          // until the device acknowledges; then it closes.
          if (this._any) return;
          const kind = id === this._ents.resetRefill ? "reset" : "identify";
          this._control(kind, "button", "press", { entity_id: id }).then(() => {
            this._menu = false;
            this._render();
          });
          return;
        }
        this._control(id === this._ents.spray ? "spray" : "press", "button", "press", { entity_id: id });
        return;
      }
      case "auto":
        // Sent even while a schedule runs: the integration refuses it, and
        // Home Assistant's toast says why.
        if (this._is("auto")) return;
        this._pendingAuto = el.dataset.option;
        if (this._ents.auto) this._control("auto", "select", "select_option", { entity_id: this._ents.auto, option: el.dataset.option });
        return;
      case "info":
        this._moreInfo(el.dataset.entity);
        return;
      case "dialog":
        this._openDialog();
        break;
      case "edit-pop":
      case "add-pop": {
        if (this._any) return;
        this._openDialog();
        if (act === "add-pop") this._add();
        else this._open = Number(el.dataset.index);
        break;
      }
      case "backdrop":
        if (ev.target !== el) return; // a click inside the dialog
        this._closeDialog();
        return;
      case "close-dialog":
        this._closeDialog();
        return;
      case "refresh":
        {
          // Like the other controls: a failure shows as Home Assistant's toast.
          const { domain, service, target } = this._refreshCall;
          this._control("refresh", domain, service, {}, target);
        }
        return;
      case "skip":
        this._call("skip", "stop_schedule", { schedule: "current" });
        return;
      case "add":
        if (this._any) return;
        this._add();
        break;
      case "edit":
        if (this._any) return;
        this._open = Number(el.dataset.index);
        break;
      case "fold":
        this._open = -1;
        break;
      case "day": {
        const d = el.dataset.day;
        e.days = e.days.includes(d) ? e.days.filter((x) => x !== d) : DAYS.filter((x) => x === d || e.days.includes(x));
        break;
      }
      case "days":
        e.days = [...DAY_PRESETS[Number(el.dataset.preset)][1]];
        break;
      case "time": {
        const [, start, end] = TIME_PRESETS[Number(el.dataset.preset)];
        Object.assign(e, { start, end });
        break;
      }
      case "start":
      case "end":
        if (el.value) e[act] = el.value.slice(0, 5);
        break;
      case "interval":
        e.interval = Number(el.dataset.value);
        break;
      case "delete":
        this._list.splice(this._open, 1);
        this._open = -1;
        break;
      case "discard":
        this._dirty = false;
        this._open = -1;
        if (closing) this._closeDialog(true);
        this._reload();
        return;
      case "save":
        if (listProblems(this._list).some(Boolean)) {
          this._check = true;
          break;
        }
        this._save(false, closing);
        return;
      case "overwrite":
        this._save(true);
        return;
      case "keep":
        this._keepDevice();
        return;
      default:
        return;
    }
    // Every edit is part of the list: unsaved while it differs from the device's.
    if (this._list) this._dirty = this._differs();
    this._render();
  }

  /** Add a schedule to the list, unfolded. */
  _add() {
    this._list.push(newSchedule(this._list));
    this._open = this._list.length - 1;
  }
}



const DIALOG_STYLE = `
  .spin { display: inline-flex; animation: aer-spin 1s linear infinite; }
  button[disabled]:has(.spin) { opacity: 1; }
  @keyframes aer-spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
  .ov { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; background: rgba(0, 0, 0, 0.55); }
  .dlg { display: flex; flex-direction: column; gap: 10px; box-sizing: border-box; width: 480px; max-width: calc(100vw - 32px); max-height: 88vh; overflow: auto;
         padding: 0 20px 20px; border-radius: 24px; outline: none; box-shadow: 0 12px 40px rgba(0, 0, 0, 0.45);
         background: var(--card-background-color, #fff); color: var(--primary-text-color); font-family: var(--ha-font-family-body, Roboto, sans-serif); }
  .dh { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; gap: 8px; padding: 14px 0 4px; background: var(--card-background-color, #fff); }
  .dh .x { width: 40px; height: 40px; border-radius: 20px; border: none; background: none; color: var(--primary-text-color); font-size: 20px; }
  .dtl { flex-grow: 1; min-width: 0; }
  .dtl b { display: block; font-size: 20px; font-weight: 400; }
  .dtl span { display: block; font-size: 13px; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .savebar { position: sticky; bottom: 0; }
  .savebar.ask span, .prob { color: var(--error-color); }
  .row .prob { grid-column: 1 / -1; font-size: 13px; }
  .prob { font-size: 14px; }
  .etitle { display: flex; align-items: center; justify-content: space-between; cursor: pointer; margin-bottom: 8px; }
  .fold { display: inline-flex; color: var(--secondary-text-color); }
  .ehead { display: inline-flex; align-items: center; gap: 10px; font-size: 14px; font-weight: 400; }
  @media (max-width: 600px) {
    .ov { align-items: stretch; }
    .dlg { width: 100%; max-width: none; max-height: none; height: 100%; border-radius: 0; padding: 0 16px 16px; }
  }
`;

const STYLE = `
  .spin { display: inline-flex; animation: aer-spin 1s linear infinite; }
  button[disabled]:has(.spin) { opacity: 1; }
  @keyframes aer-spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
  ha-card { padding: 16px; display: flex; flex-direction: column; gap: 10px; position: relative; container-type: inline-size; }
  .top { display: flex; align-items: flex-start; gap: 6px; position: relative; }
  .top .names { flex-grow: 1; min-width: 0; }
  .area { font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .top .title { font-size: 20px; }
  .more { width: 36px; height: 36px; border-radius: 18px; border: none; background: none; color: var(--secondary-text-color); display: inline-flex; align-items: center; justify-content: center; }
  .icon { display: inline-flex; align-items: center; justify-content: center; }
  .menu { position: absolute; right: 0; top: 40px; z-index: 2; display: flex; flex-direction: column; min-width: 180px; padding: 6px 0;
          background: var(--card-background-color); border-radius: 10px; box-shadow: 0 4px 16px rgba(0,0,0,.25); }
  .menu button { text-align: left; padding: 10px 16px; border: none; background: none; color: var(--primary-text-color); font-size: 14px; }
  .menu button:hover { background: var(--secondary-background-color); }
  .strip { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; padding: 8px 12px; border-radius: 14px; background: var(--secondary-background-color); }
  .mode { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 14px; font-size: 13px; font-weight: 500;
          background: color-mix(in srgb, var(--primary-color) 22%, var(--card-background-color)); color: var(--primary-text-color); }
  .mode i { width: 7px; height: 7px; border-radius: 4px; background: var(--primary-color); display: inline-block; }
  .mode.m-idle i { background: var(--success-color, #43a047); }
  .mode.m-disconnected i { background: var(--error-color); }
  .kv { display: inline-flex; align-items: center; gap: 4px; padding: 0; border: none; background: none; font-size: 15px; color: var(--primary-text-color); }
  .kv svg { color: var(--state-icon-color, var(--primary-color)); }
  .kv.low, .kv.low svg { color: var(--error-color); }
  /* Refill: a drop filled to the level, a little larger than the other
   * icons so the fill shows; the negative margin keeps the line height. */
  .lvl { display: inline-flex; margin: -2px -2px -2px -3px; }
  .lvl svg { color: var(--state-icon-color, var(--primary-color)); }
  .lvl.low svg { color: var(--error-color); }
  .strip .ago { margin-left: auto; font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; }
  .ctrl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; align-items: end; }
  .sprayb { min-height: 44px; padding: 0 16px; border-radius: 22px; border: none; display: flex; align-items: center; justify-content: center; gap: 8px; font-size: 15px; font-weight: 500;
            background: color-mix(in srgb, var(--primary-color) 22%, var(--card-background-color)); color: var(--primary-text-color); }
  .autow { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .tiny { font-size: 11px; color: var(--secondary-text-color); margin-left: 12px; }
  .seg4 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--divider-color); border-radius: 22px; overflow: hidden; }
  .seg4 button { min-height: 44px; border: none; border-right: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); font-size: 15px; font-weight: 500; }
  .seg4 button:last-child { border-right: none; }
  .seg4 button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .seg4.held button { opacity: 0.5; }
  .seg4.held button.on { opacity: 0.75; }
  .seg4.sending button[disabled] { opacity: 0.5; }
  .seg4.sending button.pend { background: var(--primary-color); color: var(--text-primary-color, #fff); opacity: 0.5; }
  @container (max-width: 340px) { .ctrl { grid-template-columns: 1fr; } }
  .sect { display: flex; align-items: baseline; gap: 8px; margin-top: 4px; }
  .sect b { flex-grow: 1; font-size: 15px; font-weight: 500; color: var(--primary-text-color); }
  .used { font-size: 13px; color: var(--secondary-text-color); }
  .row { display: grid; grid-template-columns: minmax(0, 1fr) auto; grid-template-rows: auto auto; gap: 4px 8px; align-items: center; min-height: 74px; box-sizing: border-box; }
  .row .t { font-size: 15px; font-weight: 500; color: var(--primary-text-color); }
  .row .every { display: flex; align-items: center; gap: 8px; height: 30px; min-width: 0; white-space: nowrap; font-size: 13px; color: var(--secondary-text-color); }
  .chipx { font-size: 13px; font-weight: 600; padding: 0 10px; border-radius: 12px; line-height: 24px; background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .chipn { font-size: 13px; font-weight: 600; padding: 0 9px; border-radius: 12px; line-height: 22px; border: 1px solid var(--primary-color); color: var(--primary-color); }
  .skipl { justify-self: end; height: 30px; padding: 0 14px; border: 1px solid var(--divider-color); border-radius: 15px; background: none; color: var(--primary-color); font-size: 14px; font-weight: 500; }
  .slot { display: flex; align-items: center; gap: 10px; min-height: 48px; padding: 0 8px 0 12px; border-radius: 12px; background: var(--secondary-background-color); font-size: 14px; }
  .slot b { font-weight: 500; font-size: 15px; color: var(--primary-text-color); }
  .slot .grow { flex-grow: 1; }
  .slot .slabel { color: var(--secondary-text-color); }
  .slot .pill { font-size: 12px; padding: 3px 10px; }
  .btn.small { min-height: 32px; padding: 0 14px; font-size: 13px; }
  .link { border: none; background: none; padding: 6px 0; color: var(--primary-color); font-size: 14px; font-weight: 500; }

  .head { display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }
  .title { font-size: 18px; font-weight: 500; color: var(--primary-text-color); }
  .sub { font-size: 13px; color: var(--secondary-text-color); }
  .note, .empty, .hint { font-size: 13px; color: var(--secondary-text-color); }
  .banner { font-size: 13px; padding: 8px 12px; border-radius: 10px; background: var(--secondary-background-color); color: var(--primary-text-color); }
  .banner.warn { border-left: 4px solid var(--error-color); }
  .row { padding: 10px 12px; border-radius: 12px;
         background: var(--secondary-background-color); cursor: pointer; }
  .dim { opacity: 0.45; pointer-events: none; }
  .editor.inline { border: 2px solid var(--primary-color); }
  .savebar .quiet { font-weight: 400; color: var(--secondary-text-color); }
  .hicon { color: var(--state-icon-color, var(--primary-color)); --mdc-icon-size: 24px; }
  .names { flex-grow: 1; display: flex; flex-direction: column; min-width: 0; }
  .days { display: flex; gap: 2px; }
  .d { width: 20px; height: 20px; border-radius: 10px; font-size: 11px; display: inline-flex; align-items: center; justify-content: center;
       border: 1px solid var(--divider-color); color: var(--secondary-text-color); }
  .d.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
  .pill { font-size: 12px; font-weight: 500; padding: 4px 10px; border-radius: 12px; background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .foot { display: flex; justify-content: flex-end; }
  .savebar { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 12px; background: var(--secondary-background-color); }
  .savebar span { flex-grow: 1; font-size: 14px; font-weight: 500; color: var(--primary-text-color); }
  .savebar small { display: block; font-size: 12px; font-weight: 400; color: var(--error-color); }
  button { font: inherit; cursor: pointer; }
  button[disabled] { opacity: 0.45; cursor: default; }
  .btn { min-height: 36px; padding: 0 14px; border-radius: 18px; border: 1px solid var(--divider-color); background: none; color: var(--primary-color); font-size: 14px; font-weight: 500; }
  .btn.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
  .btn.danger { color: var(--error-color); }
  .icon { width: 36px; height: 36px; border-radius: 18px; border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); font-size: 18px; }
  .editor { display: flex; flex-direction: column; gap: 10px; padding: 14px; border-radius: 14px; border: 1px solid var(--divider-color);
            background: var(--card-background-color); color: var(--primary-text-color); font-size: 14px; }
  .etitle { font-size: 17px; font-weight: 500; }
  .etitle small { font-size: 13px; font-weight: 400; color: var(--secondary-text-color); margin-left: 6px; }
  .lbl { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-weight: 500; }
  .lbl span { flex-grow: 1; }
  .chip { font-size: 12px; padding: 4px 10px; border-radius: 12px; border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); }
  .dayrow { display: flex; justify-content: space-between; gap: 4px; }
  .dt { width: 40px; height: 40px; border-radius: 20px; border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); font-weight: 500; }
  .dt.on { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
  .two { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .two label { display: flex; flex-direction: column; gap: 4px; min-width: 0; font-size: 13px; color: var(--secondary-text-color); }
  .two input { width: 100%; min-width: 0; box-sizing: border-box; min-height: 40px; border-radius: 10px; border: 1px solid var(--divider-color); padding: 0 10px; font: inherit; font-size: 16px;
               background: var(--card-background-color); color: var(--primary-text-color); }
  .seg { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid var(--divider-color); border-radius: 18px; overflow: hidden; }
  .seg button { min-height: 40px; border: none; border-right: 1px solid var(--divider-color); background: none; color: var(--primary-text-color); }
  .seg button:last-child { border-right: none; }
  .seg button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .acts { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .acts span { display: flex; gap: 8px; }
`;

/** The card's visual editor: pick the device, and an optional title. */
class AerSmartMaticCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  _update() {
    if (!this._config || !this._hass) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (field) => ({ device: "Device", title: "Title (optional)", show_schedules: "Show every schedule on the card" })[field.name] || field.name;
      this._form.addEventListener("value-changed", (ev) => {
        const config = { ...ev.detail.value };
        if (!config.title) delete config.title;
        this._config = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = this._config;
    this._form.schema = [
      { name: "device", required: true, selector: { device: { filter: { integration: DOMAIN } } } },
      { name: "title", selector: { text: {} } },
      { name: "show_schedules", selector: { boolean: {} } },
    ];
  }
}

/** Register the card and its editor, and offer it in the card picker. */
function register() {
  customElements.define("aer-smartmatic-card", AerSmartMaticCard);
  customElements.define("aer-smartmatic-card-editor", AerSmartMaticCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "aer-smartmatic-card",
    name: "Card for Godrej Aer Smart Matic",
    description: "Status, Spray now, Auto spray and the schedules of a Godrej Aer Smart Matic.",
    preview: true, // the card picker shows it live, with the first device
    documentationURL: "https://github.com/rgitha/aer_smartmatic#companion-card",
    // HA 2026.6+: offered under "Community" when adding a card by entity.
    getEntitySuggestion: (hass, entityId) => {
      const entry = hass.entities && hass.entities[entityId];
      return entry && entry.platform === DOMAIN && entry.device_id
        ? { label: "Card for Godrej Aer Smart Matic", config: { type: "custom:aer-smartmatic-card", device: entry.device_id } }
        : null;
    },
  });
  console.info(`%c AER-CARD %c ${CARD_VERSION} `, "color: white; background: #0E6B5C", "");
}

// Before Home Assistant 2026.11 the frontend replaced the browser's custom
// element registry while starting, and a card registered just before that
// was lost (home-assistant/frontend#53890). On those versions the
// integration asks the card to wait (wait=1) until the frontend is in place.
// Pages without the frontend, such as Cast, don't wait.
if (URL_PARAMS.get("wait") && document.querySelector("home-assistant")) {
  customElements.whenDefined("home-assistant").then(register);
} else {
  register();
}
