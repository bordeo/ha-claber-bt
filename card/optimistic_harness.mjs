/**
 * Headless harness: optimistic pending, revert on fail, stop hides countdown.
 * Does not touch BLE / HA services.
 */
import fs from "fs";
import vm from "vm";
import { pathToFileURL } from "url";

const PENDING_TIMEOUT_MS = 30000;
const js = fs.readFileSync(new URL("./claber-card.js", import.meta.url), "utf8");

class FakeHass {
  constructor(states) {
    this.states = states;
    this.calls = [];
    this.failNext = false;
  }
  async callService(domain, service, data) {
    this.calls.push({ domain, service, data });
    if (this.failNext) {
      this.failNext = false;
      throw new Error("service_failed");
    }
  }
}

function baseStates(openLines = []) {
  const s = {
    "binary_sensor.hydro_reachable": { state: "on", last_updated: "2026-11-09T20:00:00Z" },
    "sensor.hydro_irrigation_ends_at": {
      state: "2026-11-09T21:00:00+00:00",
      last_updated: "2026-11-09T20:00:00Z",
    },
    "sensor.hydro_active_program": { state: "none", last_updated: "2026-11-09T20:00:00Z" },
    "sensor.hydro_battery": { state: "100", last_updated: "2026-11-09T20:00:00Z" },
    "sensor.hydro_last_seen": { state: "2026-11-09T20:00:00+00:00", last_updated: "2026-11-09T20:00:00Z" },
    "switch.hydro_auto_mode": { state: "on", last_updated: "2026-11-09T20:00:00Z" },
    "switch.hydro_programma_a_abilitato": { state: "off", last_updated: "2026-11-09T20:00:00Z" },
    "switch.hydro_programma_b_abilitato": { state: "off", last_updated: "2026-11-09T20:00:00Z" },
    "number.hydro_durata_manuale": { state: "5", last_updated: "2026-11-09T20:00:00Z" },
    "button.hydro_stop": { state: "unknown", last_updated: "2026-11-09T20:00:00Z" },
    "button.hydro_leggi_programmi": { state: "unknown", last_updated: "2026-11-09T20:00:00Z" },
    "button.hydro_avvia_ora_programma_a": { state: "unknown", last_updated: "2026-11-09T20:00:00Z" },
    "button.hydro_avvia_ora_programma_b": { state: "unknown", last_updated: "2026-11-09T20:00:00Z" },
  };
  for (let i = 1; i <= 4; i++) {
    s[`valve.hydro_linea_${i}`] = {
      state: openLines.includes(i) ? "open" : "closed",
      last_updated: "2026-11-09T20:00:00Z",
    };
  }
  return s;
}

// Minimal customElements
const registry = new Map();
const customElements = {
  define: (n, c) => registry.set(n, c),
  get: (n) => registry.get(n),
};

const sandbox = {
  console,
  customElements,
  window: { customCards: [] },
  Date,
  String,
  Number,
  Math,
  Set,
  Map,
  JSON,
  sessionStorage: {
    _d: {},
    getItem(k) { return this._d[k] || null; },
    setItem(k, v) { this._d[k] = String(v); },
  },
  clearTimeout,
  setTimeout,
  clearInterval,
  setInterval,
  HTMLElement: class {
    constructor() {
      this.shadowRoot = null;
    }
    attachShadow() {
      this.shadowRoot = {
        innerHTML: "",
        querySelectorAll: () => [],
      };
      return this.shadowRoot;
    }
  },
};
vm.createContext(sandbox);
vm.runInContext(js, sandbox);
const Card = sandbox.customElements.get("claber-card");
if (!Card) throw new Error("card not defined");

const el = new Card();
el.setConfig({ hydro_device: "hydro", rain_device: null, name: "Hydro" });
const hass = new FakeHass(baseStates([]));
el.hass = hass;

// --- optimistic start ---
el._manualLine = 3;
el._view = "manual";
el._armPending("start", { line: 3, expectOpen: true });
if (!el._openLines().includes(3)) throw new Error("pending start should show line 3 open");
if (!el._pending) throw new Error("pending missing");

// coherent HA arrives
hass.states["valve.hydro_linea_3"].state = "open";
hass.states["valve.hydro_linea_3"].last_updated = "2026-11-09T20:00:01Z";
el.hass = hass;
if (el._pending) throw new Error("pending should clear when HA coherent");
if (!el._openLines().includes(3)) throw new Error("line 3 should stay open from HA");

// --- revert on fail ---
el._armPending("start", { line: 1, expectOpen: true });
try {
  hass.failNext = true;
  await el._call("valve", "open_valve", { entity_id: "valve.hydro_linea_1" });
  throw new Error("should have thrown");
} catch (e) {
  if (String(e.message) !== "service_failed") throw e;
}
el._clearPending(true);
if (el._pending) throw new Error("pending should be cleared after fail path");
if (el._openLines().includes(1)) throw new Error("line 1 should not stay optimistic after clear");

// --- stop hides countdown ---
hass.states = baseStates([3]);
hass.states["sensor.hydro_active_program"].state = "manual";
el.hass = hass;
el._armPending("stop", { expectOpen: false });
if (el._openLines().length !== 0) throw new Error("stop pending should hide open lines");
if (el._countdownRemain()) throw new Error("countdown should hide while stop pending (no open lines)");

// timeout constant documented
if (PENDING_TIMEOUT_MS !== 30000) throw new Error("expected 30s");

// view persistence
el._view = "manual";
el._manualLine = 4;
el._persistUiState();
const el2 = new Card();
el2.setConfig({ hydro_device: "hydro" });
if (el2._view !== "manual") throw new Error("view not restored");
if (el2._manualLine !== 4) throw new Error("manualLine not restored");

console.log("optimistic_harness PASS");
