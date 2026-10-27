/* COP-tekoäly dashboard card. Served by the mango_cop_ai integration, no other dependencies.
 *
 *   type: custom:cop-tekoaly-card
 *   title: COP-tekoäly        # optional
 *   show_controls: true       # mode switches, approve / reject, restore
 *   show_defrost: true
 *   show_history: true
 */
(() => {
  const DOMAIN = "mango_cop_ai";
  const DEFAULTS = {
    "sensor.tilanne": "sensor.cop_tekoaly_tilanne",
    "sensor.mittausten_tila": "sensor.cop_tekoaly_mittausten_tila",
    "sensor.diagnoosi": "sensor.cop_tekoaly_diagnoosi",
    "sensor.cop": "sensor.cop_tekoaly_cop",
    "sensor.cop_poikkeama": "sensor.cop_tekoaly_cop_poikkeama",
    "sensor.cop_laatuaste": "sensor.cop_tekoaly_cop_laatuaste",
    "sensor.naennainen_hyotysuhde": "sensor.cop_tekoaly_naennainen_hyotysuhde",
    "sensor.kirjoitukset_24h": "sensor.cop_tekoaly_kirjoitukset_24_h",
    "sensor.sulatustarve": "sensor.cop_tekoaly_sulatustarve",
    "sensor.sulatus": "sensor.cop_tekoaly_sulatus",
    "select.tila": "select.cop_tekoaly_tila",
    "select.sulatustila": "select.cop_tekoaly_sulatustila",
    "button.hyvaksy": "button.cop_tekoaly_hyvaksy_ehdotus",
    "button.hylkaa": "button.cop_tekoaly_hylkaa_ehdotus",
    "button.palauta": "button.cop_tekoaly_palauta_alkuperaiset",
  };
  const MODES = [["Pois", "Pois"], ["Tarkkailu", "Tarkkailu"], ["Ehdota", "Ehdota"], ["Automaattinen", "Auto"]];
  const MODE_HELP = {
    Pois: "ei tee mitään",
    Tarkkailu: "kertoo, mitä tekisi, ei muuta mitään",
    Ehdota: "muuttaa vasta, kun hyväksyt",
    Automaattinen: "muuttaa itse ja peruu, jos tulos huononee",
  };
  const TYPES = {
    min_od_down: "Heat Min OD alas", sv_up: "Heat Sv ylös", min_od_up: "Heat Min OD takaisin ylös",
    sv_down: "Heat Sv takaisin alas", dfr_interval_up: "Sulatusväli pidemmäksi",
    dfr_interval_down: "Sulatusväli lyhyemmäksi",
  };
  const OUTCOMES = {
    "hyväksytty": ["jäi voimaan", "ok"], ei_hyotya: ["ei hyötyä, peruttu", "warn"],
    haitta: ["haitta, peruttu", "err"], keskeytetty: ["keskeytetty", "muted"],
    ei_tunnistettu: ["vyöhykettä ei löytynyt", "muted"], ei_varmistettu: ["ei varmistettu, peruttu", "warn"],
  };
  const PHASES = {
    probe_write: "kirjoittaa kokeilua", probe_wait: "odottaa säätimen reaktiota", probe_revert: "palauttaa kokeilun",
    apply_write: "kirjoittaa", verify: "varmistaa vaikutusta", revert: "peruu muutosta", write: "kirjoittaa",
  };
  const LEVEL = { virhe: "err", odottaa: "warn", tieto: "info" };
  const LEVEL_TEXT = { virhe: "Korjaa", odottaa: "Odottaa", tieto: "Tieto" };

  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const has = st => Boolean(st && !["unknown", "unavailable"].includes(st.state));
  const numFmt = {};
  const num = (value, digits = 0) => {
    const v = Number(value);
    if (value === null || value === undefined || value === "" || !Number.isFinite(v)) return "–";
    numFmt[digits] ??= new Intl.NumberFormat("fi-FI", { minimumFractionDigits: digits, maximumFractionDigits: digits });
    return numFmt[digits].format(v);
  };
  const clock = iso => {
    if (!iso) return "–";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "–";
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    return d.toDateString() === new Date().toDateString() ? hm : `${d.getDate()}.${d.getMonth() + 1}. ${hm}`;
  };
  const short = key => String(key ?? "").split("|").pop();
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  const CSS = `
    :host {
      --cop-ok: var(--success-color, #43a047);
      --cop-warn: var(--warning-color, #ffa600);
      --cop-err: var(--error-color, #db4437);
      --cop-info: var(--info-color, #039be5);
      --cop-liquid: var(--info-color, #039be5);
      --cop-muted: var(--secondary-text-color, #727272);
      --cop-line: var(--divider-color, rgba(127, 127, 127, 0.25));
      --cop-raised: color-mix(in srgb, var(--primary-text-color, #212121) 6%, transparent);
    }
    ha-card { padding: 16px 16px 12px; container-type: inline-size; }
    .head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
    h2 { margin: 0; font-size: 16px; font-weight: 500; line-height: 24px; color: var(--primary-text-color); }
    .pill { font-size: 12px; line-height: 20px; padding: 0 8px; border-radius: 10px; white-space: nowrap;
      color: var(--cop-muted); box-shadow: inset 0 0 0 1px var(--cop-line); }
    .pill.auto { color: var(--cop-ok); box-shadow: inset 0 0 0 1px var(--cop-ok); }
    .pill.propose { color: var(--cop-info); box-shadow: inset 0 0 0 1px var(--cop-info); }
    .status { margin: 8px 0 2px; font-size: 18px; font-weight: 500; line-height: 24px; color: var(--primary-text-color);
      overflow-wrap: anywhere; }
    .diag { margin: 0; font-size: 14px; line-height: 20px; color: var(--cop-muted); }
    section { margin-top: 16px; }
    h3 { margin: 0 0 6px; font-size: 13px; font-weight: 500; line-height: 18px; color: var(--cop-muted); }
    ul { list-style: none; margin: 0; padding: 0; }
    .blockers li { display: grid; grid-template-columns: 4.6em 1fr; gap: 0 10px; padding: 6px 0 6px 10px;
      border-left: 3px solid var(--cop-line); font-size: 14px; line-height: 20px; }
    .blockers li + li { margin-top: 4px; }
    .blockers li.err { border-left-color: var(--cop-err); }
    .blockers li.warn { border-left-color: var(--cop-warn); }
    .blockers li.info { border-left-color: var(--cop-info); }
    .blockers .lvl { font-size: 12px; color: var(--cop-muted); padding-top: 1px; }
    .blockers .err .lvl { color: var(--cop-err); }
    .blockers .fix { grid-column: 2; color: var(--cop-muted); font-size: 13px; line-height: 18px; margin-top: 2px; }

    .glass-row { display: grid; grid-template-columns: 1fr auto; align-items: end; gap: 12px; }
    .glass-value { font-size: 22px; line-height: 26px; font-variant-numeric: tabular-nums; text-align: right;
      color: var(--primary-text-color); }
    .glass-value small { display: block; font-size: 12px; line-height: 16px; color: var(--cop-muted); }
    .glass-value.wet small { color: var(--cop-liquid); }
    .tube:focus-visible, .bar:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
    .tube { position: relative; height: 22px; border-radius: 11px; overflow: hidden; cursor: pointer;
      background: var(--cop-raised); box-shadow: inset 0 1px 3px rgba(0, 0, 0, 0.25), inset 0 0 0 1px var(--cop-line); }
    .tube .wetzone { position: absolute; top: 0; bottom: 0; right: 0;
      background: repeating-linear-gradient(135deg, transparent 0 5px,
        color-mix(in srgb, var(--cop-liquid) 16%, transparent) 5px 7px); }
    .tube .fill { position: absolute; top: 0; bottom: 0; left: 0; border-radius: 11px 0 0 11px;
      background: color-mix(in srgb, var(--cop-muted) 45%, transparent); transition: width 0.6s ease; }
    .tube.wet .fill { background: linear-gradient(180deg,
      color-mix(in srgb, var(--cop-liquid) 70%, white) 0%, var(--cop-liquid) 55%,
      color-mix(in srgb, var(--cop-liquid) 80%, black) 100%); }
    .tube .shine { position: absolute; left: 8px; right: 8px; top: 3px; height: 5px; border-radius: 3px;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.45), rgba(255, 255, 255, 0)); pointer-events: none; }
    .tube .mark { position: absolute; top: 3px; bottom: 3px; width: 2px; margin-left: -1px; border-radius: 1px;
      background: var(--primary-text-color); opacity: 0.55; }
    .bubble { position: absolute; bottom: -6px; width: 5px; height: 5px; border-radius: 50%;
      background: rgba(255, 255, 255, 0.7); animation: rise 3.2s linear infinite; }
    @keyframes rise { 0% { transform: translateY(0); opacity: 0; } 20% { opacity: 1; }
      100% { transform: translateY(-26px); opacity: 0; } }
    @media (prefers-reduced-motion: reduce) { .bubble { animation: none; display: none; }
      .tube .fill, .bar .fill { transition: none; } }
    .scale { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0 12px; margin-top: 4px;
      font-size: 12px; line-height: 16px; color: var(--cop-muted); }

    .readouts { display: grid; grid-template-columns: repeat(4, 1fr); }
    .readouts button { all: unset; cursor: pointer; padding: 2px 8px; border-left: 1px solid var(--cop-line);
      border-radius: 2px; }
    .readouts button:first-child { border-left: 0; padding-left: 0; }
    .readouts button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
    .readouts b { display: block; font-size: 22px; line-height: 28px; font-weight: 400;
      font-variant-numeric: tabular-nums; color: var(--primary-text-color); white-space: nowrap; }
    .readouts b small { font-size: 13px; color: var(--cop-muted); margin-left: 2px; }
    .readouts span { display: block; font-size: 12px; line-height: 16px; color: var(--cop-muted); }
    .readouts .neg b { color: var(--cop-warn); }

    .modes { display: grid; grid-template-columns: auto 1fr; gap: 8px 12px; align-items: center; }
    .modes .lbl { font-size: 14px; color: var(--primary-text-color); }
    .seg { display: grid; grid-template-columns: repeat(4, 1fr); border-radius: 8px; overflow: hidden;
      box-shadow: inset 0 0 0 1px var(--cop-line); }
    .seg button { all: unset; box-sizing: border-box; text-align: center; padding: 6px 4px; font-size: 13px;
      line-height: 18px; cursor: pointer; color: var(--primary-text-color); border-left: 1px solid var(--cop-line); }
    .seg button:first-child { border-left: 0; }
    .seg button[aria-pressed="true"] { background: var(--primary-color); color: var(--text-primary-color, #fff); }
    .seg button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: -2px; }
    .seg button:disabled { cursor: progress; opacity: 0.6; }
    .seg .short { display: none; }

    .proposal { padding: 10px 12px; border-radius: 8px;
      background: color-mix(in srgb, var(--cop-info) 10%, transparent); }
    .proposal p { margin: 0 0 4px; font-size: 14px; line-height: 20px; color: var(--primary-text-color); }
    .proposal .why { font-size: 13px; line-height: 18px; color: var(--cop-muted); }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .btn { all: unset; box-sizing: border-box; cursor: pointer; padding: 6px 14px; border-radius: 8px;
      font-size: 14px; line-height: 20px; color: var(--primary-color); box-shadow: inset 0 0 0 1px var(--cop-line); }
    .btn.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); box-shadow: none; }
    .btn.danger { color: var(--cop-err); }
    .btn.danger.armed { background: var(--cop-err); color: #fff; box-shadow: none; }
    .btn:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
    .btn:disabled { cursor: progress; opacity: 0.6; }

    .bar { position: relative; height: 8px; border-radius: 4px; background: var(--cop-raised); overflow: hidden;
      cursor: pointer; }
    .bar .fill { position: absolute; top: 0; bottom: 0; left: 0; background: var(--cop-ok); transition: width 0.6s ease; }
    .bar .fill.warn { background: var(--cop-warn); }
    .bar .fill.err { background: var(--cop-err); }
    .bar .mark { position: absolute; top: 0; bottom: 0; left: 50%; width: 2px; margin-left: -1px;
      background: var(--primary-text-color); opacity: 0.55; }
    .line { display: flex; justify-content: space-between; gap: 12px; font-size: 14px; line-height: 20px;
      color: var(--primary-text-color); margin-bottom: 6px; }
    .line .num { font-variant-numeric: tabular-nums; white-space: nowrap; }
    .sub { margin: 6px 0 0; font-size: 13px; line-height: 18px; color: var(--cop-muted); }

    table { width: 100%; border-collapse: collapse; font-size: 13px; line-height: 18px; }
    td { padding: 3px 8px 3px 0; vertical-align: top; color: var(--primary-text-color); }
    td.t { color: var(--cop-muted); white-space: nowrap; font-variant-numeric: tabular-nums; width: 1%; }
    td.v { white-space: nowrap; font-variant-numeric: tabular-nums; }
    .nw { white-space: nowrap; font-variant-numeric: tabular-nums; }
    table.changes td.v { text-align: right; padding-right: 0; }
    td.ok { color: var(--cop-ok); } td.warn { color: var(--cop-warn); } td.err { color: var(--cop-err); }
    td.muted { color: var(--cop-muted); }
    .empty { font-size: 13px; color: var(--cop-muted); margin: 0; }

    .foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 16px; }
    .foot small { font-size: 12px; color: var(--cop-muted); }
    .error { margin-top: 10px; font-size: 13px; color: var(--cop-err); }
    .setup p { margin: 8px 0 0; font-size: 14px; line-height: 20px; color: var(--primary-text-color); }
    .setup code { font-size: 13px; }

    @container (max-width: 430px) {
      .seg .long { display: none; } .seg .short { display: inline; }
      .blockers li { grid-template-columns: 1fr; } .blockers .fix { grid-column: 1; }
    }
    @container (max-width: 340px) {
      .readouts { grid-template-columns: repeat(2, 1fr); gap: 8px 0; }
      .readouts button:nth-child(3) { border-left: 0; padding-left: 0; }
    }
  `;

  const resolved = new WeakMap(); // connection -> Promise<{entities, version}>

  class CopTekoalyCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._config = {};
      this._ids = { ...DEFAULTS };
      this._seen = [];
      this._busy = new Set();
      this._armed = false;
      this._error = "";
      this._version = "";
      this.shadowRoot.addEventListener("click", event => this._click(event));
      this.shadowRoot.addEventListener("keydown", event => {
        if ((event.key === "Enter" || event.key === " ") && event.target.matches("div[data-action]")) {
          event.preventDefault();
          this._click(event);
        }
      });
    }

    static getStubConfig() {
      return {};
    }

    setConfig(config) {
      this._config = { title: "COP-tekoäly", show_controls: true, show_defrost: true, show_history: true, ...config };
      this._seen = [];
      if (this._hass) this._render();
    }

    set hass(hass) {
      this._hass = hass;
      this._resolve();
      const seen = Object.values(this._ids).map(id => hass.states[id]);
      if (seen.length !== this._seen.length || seen.some((st, i) => st !== this._seen[i])) {
        this._seen = seen;
        this._render();
      }
    }

    getCardSize() {
      return 10;
    }

    getGridOptions() {
      return { columns: 12, min_columns: 6 };
    }

    _resolve() {
      const conn = this._hass.connection;
      if (!conn || this._resolving === conn) return;
      this._resolving = conn;
      if (!resolved.has(conn)) {
        resolved.set(conn, this._hass.callWS({ type: `${DOMAIN}/entities` }).catch(() => null));
      }
      resolved.get(conn).then(result => {
        if (!result) return;
        this._ids = { ...DEFAULTS, ...result.entities };
        this._version = result.version || "";
        this._seen = [];
        if (this._hass) this.hass = this._hass;
      });
    }

    _st(key) {
      return this._hass?.states[this._ids[key]];
    }

    _attr(key, name) {
      return this._st(key)?.attributes?.[name];
    }

    async _call(busyKey, domain, service, data) {
      this._busy.add(busyKey);
      this._error = "";
      this._render();
      try {
        await this._hass.callService(domain, service, data);
      } catch (err) {
        this._error = `Toiminto epäonnistui: ${err?.message || err}`;
      } finally {
        this._busy.delete(busyKey);
        this._render();
      }
    }

    _click(event) {
      const el = event.target.closest("[data-action]");
      if (!el || el.disabled) return;
      const { action, key, option } = el.dataset;
      if (action === "mode") {
        this._call(key, "select", "select_option", { entity_id: this._ids[key], option });
      } else if (action === "press") {
        this._call(key, "button", "press", { entity_id: this._ids[key] });
      } else if (action === "restore") {
        if (!this._armed) {
          this._armed = true;
          clearTimeout(this._armTimer);
          this._armTimer = setTimeout(() => { this._armed = false; this._render(); }, 6000);
          this._render();
          return;
        }
        this._armed = false;
        clearTimeout(this._armTimer);
        this._call("button.palauta", "button", "press", { entity_id: this._ids["button.palauta"] });
      } else if (action === "more") {
        this.dispatchEvent(new CustomEvent("hass-more-info", {
          detail: { entityId: this._ids[key] }, bubbles: true, composed: true,
        }));
      }
    }

    _render() {
      if (!this._hass) return;
      const c = this._config;
      const status = this._st("sensor.tilanne");
      let body;
      if (!status) {
        body = `
          <div class="head"><h2>${esc(c.title)}</h2></div>
          <div class="setup">
            <p>COP-tekoäly ei ole käynnissä.</p>
            <p>Lisää <code>configuration.yaml</code>-tiedostoon rivi <code>mango_cop_ai:</code> ja käynnistä
              Home Assistant uudelleen.</p>
          </div>`;
      } else {
        body = [
          this._header(status),
          c.show_controls ? this._proposal(status) : "",
          this._blockers(status),
          this._glass(),
          this._readouts(),
          c.show_controls ? this._modes() : "",
          c.show_defrost ? this._defrost() : "",
          this._changes(status),
          c.show_history ? this._history(status) : "",
          this._footer(status),
        ].join("");
      }
      this.shadowRoot.innerHTML = `<style>${CSS}</style><ha-card>${body}</ha-card>`;
    }

    _header(status) {
      const mode = this._st("select.tila")?.state || status.attributes.tila || "";
      const cls = mode === "Automaattinen" ? "auto" : mode === "Ehdota" ? "propose" : "";
      const diag = this._st("sensor.diagnoosi");
      // the reason for waiting is listed below, the headline keeps the progress only
      let text = has(status) ? status.state : "Ei tietoa";
      const waiting = text.match(/^(Odottaa tasaista käyntiä \(\d+\/\d+ min\)):/);
      if (waiting) text = waiting[1];
      const plain = ["kerää", "tasaantuu", "seis", "ei_dataa", "sulatus"].includes(diag?.attributes?.koodi);
      return `
        <div class="head"><h2>${esc(this._config.title)}</h2>
          <span class="pill ${cls}" title="EEV-säädön tila">${esc(mode || "–")}</span></div>
        <p class="status">${esc(text)}</p>
        ${has(diag) && !plain ? `<p class="diag">${esc(diag.state)}</p>` : ""}`;
    }

    _blockers(status) {
      let items = status.attributes.esteet;
      if (!Array.isArray(items)) return "";
      if (this._config.show_controls) items = items.filter(b => b.koodi !== "ehdotus"); // shown with its buttons
      if (!items.length) return "";
      const title = status.attributes.toimenpide ? "Huomioita" : "Miksi tekoäly ei säädä nyt";
      return `<section class="blockers"><h3>${title}</h3><ul>${items.map(b => `
        <li class="${LEVEL[b.taso] || "info"}"><span class="lvl">${esc(LEVEL_TEXT[b.taso] || b.taso)}</span>
          <span>${esc(b.teksti)}</span>${b.korjaus ? `<span class="fix">${esc(b.korjaus)}</span>` : ""}</li>`
      ).join("")}</ul></section>`;
    }

    _glass() {
      const st = this._st("sensor.naennainen_hyotysuhde");
      const value = has(st) ? Number(st.state) : null;
      const dry = Number(st?.attributes?.kuivan_imun_taso_pros ?? 70);
      const wet = Number(st?.attributes?.märkäraja_pros ?? dry + 15);
      const lo = 50;
      const hi = 110;
      const pos = v => clamp((v - lo) / (hi - lo) * 100, 0, 100);
      const isWet = value !== null && value >= wet;
      const verdict = value === null ? "ei tasaista käyntiä" : isWet ? "märkä" : value >= dry + (wet - dry) / 2
        ? "märän rajalla" : "kuiva";
      const bubbles = isWet ? [12, 31, 47, 63, 80].map((x, i) =>
        `<i class="bubble" style="left:${(x * pos(value)) / 100}%;animation-delay:${i * 0.65}s"></i>`).join("") : "";
      return `
        <section>
          <h3>Imukaasun märkyys</h3>
          <div class="glass-row">
            <div>
              <div class="tube ${isWet ? "wet" : ""}" data-action="more" data-key="sensor.naennainen_hyotysuhde"
                   tabindex="0" role="meter" aria-label="Kompressorin näennäinen hyötysuhde" aria-valuemin="${lo}"
                   aria-valuemax="${hi}" aria-valuenow="${value ?? ""}" aria-valuetext="${esc(verdict)}">
                <div class="wetzone" style="left:${pos(wet)}%"></div>
                ${value === null ? "" : `<div class="fill" style="width:${pos(value)}%">${bubbles}</div>
                  <div class="shine"></div>`}
                <div class="mark" style="left:${pos(dry)}%"></div>
                <div class="mark" style="left:${pos(wet)}%"></div>
              </div>
              <div class="scale">
                <span>kuivan imun taso ${num(dry)} %</span><span>märkä yli ${num(wet)} %</span>
              </div>
            </div>
            <div class="glass-value ${isWet ? "wet" : ""}">${value === null ? "–" : `${num(value)} %`}
              <small>${esc(verdict)}</small></div>
          </div>
        </section>`;
    }

    _readouts() {
      const cop = this._st("sensor.cop");
      const quality = this._st("sensor.cop_laatuaste");
      const dev = this._st("sensor.cop_poikkeama");
      const writes = this._st("sensor.kirjoitukset_24h");
      const limit = this._attr("sensor.tilanne", "kirjoitusraja_vrk");
      const devValue = has(dev) ? Number(dev.state) : null;
      const cell = (key, value, unit, label, cls = "") => `
        <button class="${cls}" data-action="more" data-key="${key}" title="${esc(label)}">
          <b>${value}${unit ? `<small>${unit}</small>` : ""}</b><span>${esc(label)}</span></button>`;
      return `<section class="readouts">
        ${cell("sensor.cop", has(cop) ? num(cop.state, 2) : "–", "", "COP, tunti")}
        ${cell("sensor.cop_laatuaste", has(quality) ? num(quality.state, 0) : "–", has(quality) ? "%" : "",
          "laatuaste")}
        ${cell("sensor.cop_poikkeama", devValue === null ? "–" : (devValue > 0 ? "+" : "") + num(devValue, 0),
          devValue === null ? "" : "%", "opitusta", devValue !== null && devValue <= -10 ? "neg" : "")}
        ${cell("sensor.kirjoitukset_24h", has(writes) ? num(writes.state) : "–", limit ? `/${limit}` : "",
          "kirjoitusta 24 h")}
      </section>`;
    }

    _modes() {
      const row = (key, label) => {
        const st = this._st(key);
        if (!st) return "";
        const busy = this._busy.has(key);
        return `
          <span class="lbl">${label}</span>
          <div class="seg" role="group" aria-label="${esc(label)}">${MODES.map(([option, shortName]) => `
            <button data-action="mode" data-key="${key}" data-option="${option}" ${busy ? "disabled" : ""}
              aria-pressed="${st.state === option}" title="${esc(`${option}: ${MODE_HELP[option]}`)}">
              <span class="long">${option}</span><span class="short">${shortName}</span></button>`).join("")}
          </div>`;
      };
      return `<section class="modes">${row("select.tila", "EEV-säätö")}${row("select.sulatustila", "Sulatus")}</section>`;
    }

    _proposal(status) {
      const p = status.attributes.ehdotus;
      if (!p) {
        const approved = status.attributes["hyväksytty_odottaa"];
        return approved ? `<section class="proposal"><p>Hyväksytty: ${esc(approved)}</p>
          <p class="why">Toteutetaan, kun tilanne on sama kuin ehdotettaessa.</p></section>` : "";
      }
      const busy = this._busy.has("button.hyvaksy") || this._busy.has("button.hylkaa");
      return `
        <section class="proposal">
          <p>Ehdotus: <strong>${esc(p.otsikko)}</strong></p>
          <p class="why">${esc(p.syy)}</p>
          <div class="actions">
            <button class="btn primary" data-action="press" data-key="button.hyvaksy" ${busy ? "disabled" : ""}>
              Hyväksy ehdotus</button>
            <button class="btn" data-action="press" data-key="button.hylkaa" ${busy ? "disabled" : ""}>
              Hylkää</button>
          </div>
        </section>`;
    }

    _defrost() {
      const need = this._st("sensor.sulatustarve");
      const last = this._st("sensor.sulatus");
      if (!need && !last) return "";
      const a = need?.attributes || {};
      const value = has(need) ? Number(need.state) : null;
      const cls = value === null ? "" : value >= 150 ? "err" : value >= 75 ? "warn" : "";
      const coil = a.kenno || "–";
      const details = [];
      if (a.huurre_k !== null && a.huurre_k !== undefined) details.push(`huurretta ${num(a.huurre_k, 1)} K`);
      if (a["cop_häviö_pros"]) details.push(`COP-häviö ${num(a["cop_häviö_pros"], 0)} %`);
      if (a["arvio_sulatustarpeeseen_min"]) details.push(`sulatus tarpeen noin ${num(a["arvio_sulatustarpeeseen_min"])} min päästä`);
      if (a.pakkosulatus_estetty) details.push(`ei sulateta: ${a.pakkosulatus_estetty}`);
      const recent = (last?.attributes?.["viimeisimmät"] || [])[0];
      const trigger = String(recent?.laukaisi ?? "").replace(/^(.+) (\d+) min$/, "$1 ($2 min)");
      const lastText = recent
        ? `Viimeisin sulatus ${clock(recent.aika)}: ${recent.arvio}, kesto ${num(recent.kesto_s / 60, 1)} min, `
          + `laukaisi ${trigger}.`
        : "Ei sulatuksia vielä.";
      const act = last?.attributes?.toimenpide;
      if (act) details.unshift(`${PHASES[act.vaihe] || act.vaihe}: ${act.muutos}`);
      return `
        <section>
          <h3>Ulkoyksikön kenno</h3>
          <div class="line"><span>${esc(coil)}</span>
            ${value === null ? "" : `<span class="num">sulatustarve ${num(value)} %</span>`}</div>
          ${value === null ? "" : `
          <div class="bar" data-action="more" data-key="sensor.sulatustarve" tabindex="0" role="meter"
               aria-label="Sulatustarve" aria-valuemin="0" aria-valuemax="200" aria-valuenow="${value}">
            <div class="fill ${cls}" style="width:${clamp(value / 2, 0, 100)}%"></div>
            <div class="mark" title="100 % = tekoälyn sulatuskynnys"></div>
          </div>`}
          ${details.length ? `<p class="sub">${esc(details.join(", "))}.</p>` : ""}
          <p class="sub">${esc(lastText)}</p>
        </section>`;
    }

    _changes(status) {
      const changed = { ...(status.attributes.muutetut_arvot || {}),
        ...(this._attr("sensor.sulatus", "muutetut_sulatusvälit") || {}) };
      const rows = Object.entries(changed);
      if (!rows.length) return "";
      return `<section><h3>Tekoälyn muuttamat arvot</h3><table class="changes">${rows.map(([name, v]) => `
        <tr><td>${esc(name)}</td><td class="v">${esc(v["alkuperäinen"] ?? "–")} → ${esc(v.nyt ?? "–")}</td></tr>`
      ).join("")}</table></section>`;
    }

    _history(status) {
      const action = status.attributes.toimenpide;
      const items = [...(status.attributes.historia || [])].reverse().slice(0, 6);
      const current = action ? `<tr><td class="t">nyt</td><td>${esc(TYPES[action.tyyppi] || action.tyyppi)}
          ${action.parametri ? `<br>${esc(short(action.parametri))}` : ""}</td>
          <td class="v">${esc(PHASES[action.vaihe] || action.vaihe)}${action.vaihe === "verify"
            ? ` ${num(action.varmistus_min)} min` : ""}</td></tr>` : "";
      if (!items.length && !current) {
        return `<section><h3>Päätökset</h3><p class="empty">Ei vielä päätöksiä.</p></section>`;
      }
      return `<section><h3>Päätökset</h3><table>${current}${items.map(h => {
        const [text, cls] = OUTCOMES[h.outcome] || [h.outcome, "muted"];
        const change = h.key ? `${short(h.key)} ${h.from ?? "–"} → ${h.to ?? "–"}` : "";
        return `<tr><td class="t">${esc(clock(h.t))}</td>
          <td>${esc(TYPES[h.type] || h.type)}${change ? `<br><span class="nw">${esc(change)}</span>` : ""}</td>
          <td class="v ${cls}">${esc(text)}</td></tr>`;
      }).join("")}</table></section>`;
    }

    _footer(status) {
      const version = this._version || status.attributes.versio || "";
      const err = this._error ? `<div class="error" role="alert">${esc(this._error)}</div>` : "";
      const restorable = Object.keys(status.attributes.muutetut_arvot || {}).length || status.attributes.toimenpide
        || this._attr("sensor.sulatus", "toimenpide");
      if (!this._config.show_controls || !this._st("button.palauta") || !restorable) {
        return `${err}<div class="foot"><span></span><small>${esc(version && `v${version}`)}</small></div>`;
      }
      const busy = this._busy.has("button.palauta");
      return `${err}<div class="foot">
        <button class="btn danger ${this._armed ? "armed" : ""}" data-action="restore" ${busy ? "disabled" : ""}>
          ${this._armed ? "Vahvista: palauta kaikki alkuperäiset" : "Palauta alkuperäiset"}</button>
        <small>${esc(version && `v${version}`)}</small></div>`;
    }
  }

  // Home Assistant's app bundle replaces window.customElements with a scoped-registry polyfill when it
  // starts. This small module is imported together with the app and can run first: an element defined
  // in the replaced registry stays invisible to dashboards ("Custom element doesn't exist"). So the card
  // is defined once the app has defined its root element, in the registry that is current then.
  const register = () => {
    const registry = window.customElements;
    if (!registry.get("cop-tekoaly-card")) {
      registry.define("cop-tekoaly-card", CopTekoalyCard);
    }
  };
  if (window.customElements.get("home-assistant")) {
    register();
  } else {
    window.customElements.whenDefined("home-assistant").then(register);
    setTimeout(register, 15000); // a page without the Home Assistant app shell
  }
  window.customCards = window.customCards || [];
  if (!window.customCards.some(card => card.type === "cop-tekoaly-card")) {
    window.customCards.push({
      type: "cop-tekoaly-card",
      name: "COP-tekoäly",
      description: "Mitä lämpöpumpun tekoäly tekee, miksi se ei säädä, tilat ja ehdotukset.",
      preview: true,
      documentationURL: "https://github.com/artepe/AI_heatpump_controller#kojelautakortti",
    });
  }
})();
