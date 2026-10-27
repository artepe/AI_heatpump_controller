"""Home Assistant side of the Mango COP AI: reads states, writes parameters, keeps the state."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .analysis import Sample, Settings, median
from .brain import Brain, Dismiss, Force, Log, Notify, Param, Write
from .const import (
    ALL_PARAMETERS,
    DEFAULT_FORCE_SCRIPT,
    DEFAULT_SENSORS,
    DOMAIN,
    MANGO_DOMAIN,
    MIN_OD,
    MODE_AUTO,
    MODE_OBSERVE,
    MODE_OFF,
    MODE_PROPOSE,
    NAME,
    PARAMETER_IDS,
    SIGNAL_UPDATE,
    SV,
)

_LOGGER = logging.getLogger(__name__)
BAD = (STATE_UNAVAILABLE, STATE_UNKNOWN, "", "none", None)
# sensors whose absence stops all decisions (Sample field -> config key)
CORE_KEYS = {"p_el": "sahko", "rps": "nopeus", "te": "hoyrystyminen", "tc": "lauhtuminen",
             "t_suc": "imukaasu", "t_dis": "kuumakaasu", "eev": "eev", "t_out": "ulko", "t_sup": "menovesi"}
# roles with a stand-in: the second entity is used when the first one does not exist
FALLBACKS = {"ulko": "ulko_varalla", "sulatus": "sulatus_varalla"}
STATUS_ENTITY = "sensor.cop_tekoaly_tilanne"
# A reading from an integration that writes its state on every poll, also when nothing changed, counts as
# missing when it has not been reported for STALE_S: the polling has stopped. Every other source (template,
# ESPHome, MQTT, ...) may stay silent for hours while its value holds, so its age says nothing.
STALE_S = 15 * 60
POLLED_PLATFORMS = {"modbus"}


def _float(state) -> float | None:
    if state is None or state.state in BAD:
        return None
    try:
        v = float(state.state)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class Runner:
    def __init__(self, hass: HomeAssistant, conf: dict, settings: Settings, store: Store, stored: dict | None):
        self.hass = hass
        self.sensors = {**DEFAULT_SENSORS, **conf.get("anturit", {})}
        self.faults = list(conf.get("vikatilat", []))
        self.overrides = dict(conf.get("parametrit", {}))
        self.force_script: str | None = conf.get("pakkosulatus")
        self.brain = Brain(settings, stored, mode=conf.get("tila"), defrost_mode=conf.get("sulatus_tila"))
        self.store = store
        self.lock = asyncio.Lock()
        self._entities: dict[str, str] = {}
        self._unsub = None
        self.missing: list[str] = []
        self.last_tick = None
        self.invalid: list[str] = []
        self._platforms: dict[str, str] = {}  # entity id -> integration that provides it ("" = unknown)

    # ------------------------------------------------------------------ lifecycle
    @callback
    def async_start(self) -> None:
        async_at_started(self.hass, self._started)

    async def _started(self, hass: HomeAssistant) -> None:
        self._unsub = async_track_time_interval(
            hass, self._async_tick, timedelta(minutes=1), name="Mango COP-tekoäly", cancel_on_shutdown=True
        )
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_stop)

    async def _async_stop(self, _event) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        await self.store.async_save(self.brain.to_dict())

    def _save(self) -> None:
        self.store.async_delay_save(self.brain.to_dict, 30)

    # ------------------------------------------------------------------ the minute loop
    async def _async_tick(self, now=None) -> None:
        if self.lock.locked() or self.hass.state is CoreState.stopping:
            return
        async with self.lock:
            ts = dt_util.utcnow().timestamp()
            try:
                cmds = self.brain.tick(ts, self._sample(ts), self._params())
                await self._execute(cmds)
            except Exception:  # never let one bad minute stop the loop
                _LOGGER.exception("Mango COP-tekoäly: virhe minuutin käsittelyssä")
            self.last_tick = ts
        self._save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def _command(self, call) -> None:
        """Run a user command and any writes it causes (e.g. restoring values) right away."""
        async with self.lock:
            ts = dt_util.utcnow().timestamp()
            self.brain.params = self._params()
            cmds = call(ts)
            cmds = cmds + self.brain.followups(ts, self._params())
            await self._execute(cmds)
        self._save()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def async_set_mode(self, mode: str) -> None:
        await self._command(lambda ts: self.brain.set_mode(mode, ts))

    async def async_approve(self) -> None:
        await self._command(self.brain.approve)

    async def async_reject(self) -> None:
        await self._command(self.brain.reject)

    async def async_rollback(self) -> None:
        await self._command(lambda ts: self.brain.rollback_all(ts, "käyttäjä"))

    async def async_set_defrost_mode(self, mode: str) -> None:
        await self._command(lambda ts: self.brain.set_defrost_mode(mode, ts))

    async def _execute(self, cmds: list) -> None:
        for _ in range(25):
            writes, forces = [], []
            for c in cmds:
                if isinstance(c, Write):
                    writes.append(c)
                elif isinstance(c, Force):
                    forces.append(c)
                elif isinstance(c, Log):
                    self._log(c.message)
                elif isinstance(c, Notify):
                    persistent_notification.async_create(
                        self.hass, c.message, title=c.title, notification_id=f"{DOMAIN}_{c.tag}"
                    )
                elif isinstance(c, Dismiss):
                    persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{c.tag}")
            if not writes and not forces:
                return
            cmds = []
            for f in forces:
                ok, error = await self._run_force(f)
                cmds += self.brain.force_result(ok, error, dt_util.utcnow().timestamp())
            for w in writes:
                ok, error = await self._write(w)
                ts = dt_util.utcnow().timestamp()
                actual = None if ok else self._params()[w.key].value
                cmds += self.brain.write_result(w, ok, error, ts, actual)
            cmds += self.brain.followups(dt_util.utcnow().timestamp(), self._params())
        _LOGGER.warning("Mango COP-tekoäly: kirjoitusjono ei tyhjentynyt, jatketaan seuraavalla minuutilla")

    def _log(self, message: str) -> None:
        _LOGGER.info("%s", message)
        self.hass.bus.async_fire(
            "logbook_entry",
            {"name": NAME, "message": message, "domain": DOMAIN, "entity_id": STATUS_ENTITY},
        )

    # ------------------------------------------------------------------ parameters
    def entity_for(self, key: str) -> str | None:
        if key in self.overrides:
            return self.overrides[key]
        cached = self._entities.get(key)
        if cached and self.hass.states.get(cached) is not None:
            return cached
        suffix = "_" + PARAMETER_IDS[key]
        registry = er.async_get(self.hass)
        for entry in registry.entities.values():
            if entry.platform == MANGO_DOMAIN and entry.domain == "number" and entry.unique_id.endswith(suffix):
                self._entities[key] = entry.entity_id
                return entry.entity_id
        return None

    def force_entity(self) -> str | None:
        """The forced defrost script: configured, the default name, or a script called "Sulatus pakotettu"."""
        get = self.hass.states.get
        if self.force_script:
            return self.force_script if get(self.force_script) is not None else None
        if get(DEFAULT_FORCE_SCRIPT) is not None:
            return DEFAULT_FORCE_SCRIPT
        for st in self.hass.states.async_all("script"):
            name = f"{st.attributes.get('friendly_name') or ''} {st.entity_id}".lower()
            if ("sulatus" in name and "pakot" in name) or ("defrost" in name and "forc" in name):
                return st.entity_id
        return None

    async def _run_force(self, f: Force) -> tuple[bool, str | None]:
        eid = self.force_entity()
        if eid is None:
            return False, "pakkosulatusskriptiä ei löydy"
        try:
            await self.hass.services.async_call("script", "turn_on", {"entity_id": eid}, blocking=True)
        except (HomeAssistantError, vol.Invalid, ValueError) as err:
            _LOGGER.warning("Mango COP-tekoäly: pakkosulatus %s epäonnistui: %s", eid, err)
            return False, str(err)[:200]
        except Exception as err:  # noqa: BLE001 - report every failure to the brain
            _LOGGER.exception("Mango COP-tekoäly: odottamaton virhe pakkosulatuksessa %s", eid)
            return False, f"odottamaton virhe: {err}"[:200]
        _LOGGER.info("Mango COP-tekoäly: pakkosulatus %s (%s)", eid, f.reason)
        return True, None

    def _params(self) -> dict[str, Param]:
        out = {}
        for key in ALL_PARAMETERS:
            eid = self.entity_for(key)
            st = self.hass.states.get(eid) if eid else None
            if st is None or st.state in BAD:
                out[key] = Param(None, False)
                continue
            raw = st.attributes.get("raw_value")
            value = raw if isinstance(raw, int) else _float(st)
            lo, hi = st.attributes.get("min"), st.attributes.get("max")
            out[key] = Param(
                None if value is None else int(round(value)), True,
                None if lo is None else int(math.ceil(lo)), None if hi is None else int(math.floor(hi)),
            )
        return out

    async def _write(self, w: Write) -> tuple[bool, str | None]:
        eid = self.entity_for(w.key)
        if eid is None:
            return False, "Mango-entiteettiä ei löydy"
        try:
            await self.hass.services.async_call(
                "number", "set_value", {"entity_id": eid, "value": w.value}, blocking=True
            )
        except (HomeAssistantError, vol.Invalid, ValueError) as err:
            _LOGGER.warning("Mango COP-tekoäly: %s → %s epäonnistui: %s", eid, w.value, err)
            return False, str(err)[:200]
        except Exception as err:  # noqa: BLE001 - report every failure to the brain, never leave it hanging
            _LOGGER.exception("Mango COP-tekoäly: odottamaton virhe kirjoituksessa %s", eid)
            return False, f"odottamaton virhe: {err}"[:200]
        # the Mango integration confirms by reading back; an unavailable entity is skipped silently
        param = self._params()[w.key]
        if not param.available or param.value != w.value:
            return False, f"laite näyttää {param.value}"
        _LOGGER.info("Mango COP-tekoäly: %s = %s (%s)", eid, w.value, w.kind)
        return True, None

    # ------------------------------------------------------------------ sensors
    @property
    def polled(self) -> set[str]:
        """Entities whose integration writes every poll, so that silence means a frozen reading."""
        return {eid for eid, platform in self._platforms.items() if platform in POLLED_PLATFORMS}

    def _platform(self, eid: str) -> str:
        if eid not in self._platforms:
            entry = er.async_get(self.hass).async_get(eid)
            self._platforms[eid] = entry.platform if entry is not None else ""
        return self._platforms[eid]

    def fresh(self, eid: str | None, st, ts: float) -> bool:
        """False only for a reading from a polling integration that has stopped reporting.

        Home Assistant refreshes last_reported whenever an integration writes a state, also when
        nothing changed. Modbus writes on every poll, so a Modbus reading silent for STALE_S is
        frozen. Template, ESPHome and MQTT entities write only when the value changes (and a template
        may write twice while starting): a long silence from them is normal, so they are never stale.
        """
        reported = getattr(st, "last_reported", None)
        if eid is None or reported is None or self._platform(eid) not in POLLED_PLATFORMS:
            return True
        return ts - reported.timestamp() <= STALE_S

    def role_entity(self, key: str) -> str | None:
        """The entity for a role: the configured one, or its stand-in when the first does not exist."""
        eid = self.sensors.get(key)
        alt = self.sensors.get(FALLBACKS.get(key, ""))
        if alt and (eid is None or self.hass.states.get(eid) is None) and self.hass.states.get(alt) is not None:
            return alt
        return eid

    def _sample(self, ts: float) -> Sample:
        get = self.hass.states.get
        s = self.sensors
        missing = []

        def num(key):
            eid = s.get(key)
            st = get(eid) if eid else None
            v = _float(st)
            if key in CORE_KEYS.values() and key != "ulko":
                # a frozen Modbus value must not look like steady running
                if v is not None and not self.fresh(eid, st, ts):
                    v = None
                if v is None:
                    missing.append(eid or key)
            return v

        def kw(key):
            eid = s.get(key)
            st = get(eid) if eid else None
            v = _float(st)
            if v is None or not self.fresh(eid, st, ts):
                return None
            unit = st.attributes.get("unit_of_measurement")
            return v / 1000 if unit == "W" or (unit is None and key == "vesilampo" and v > 100) else v

        def minutes_since(st):
            return max(0.0, (ts - st.last_changed.timestamp()) / 60)

        t_out = _float(get(s["ulko"])) if s.get("ulko") else None
        if t_out is None and s.get("ulko_varalla"):
            t_out = _float(get(s["ulko_varalla"]))
        if t_out is None:
            missing.append(s.get("ulko") or "ulko")
        comp_eid, dfr_eid = self.role_entity("kompressori"), self.role_entity("sulatus")
        comp = get(comp_eid) if comp_eid else None
        comp_on = comp is not None and comp.state == "on"
        dfr = get(dfr_eid) if dfr_eid else None
        defrost = dfr is not None and dfr.state == "on"
        for key, eid, st in (("kompressori", comp_eid, comp), ("sulatus", dfr_eid, dfr)):
            if st is None or st.state not in ("on", "off") or not self.fresh(eid, st, ts):
                missing.append(eid or key)
        p_el = kw("sahko")
        if p_el is None:
            missing.append(s.get("sahko"))
        fault = any((st := get(eid)) is not None and st.state == "on" for eid in self.faults)
        heater_state = get(s["vastus"]) if s.get("vastus") else None
        flow = num("virtaus")
        flow_st = get(s["virtaus"]) if s.get("virtaus") else None
        if flow is not None and flow_st is not None:
            unit = str(flow_st.attributes.get("unit_of_measurement") or "").lower()
            flow *= 1000 if unit in ("m³/h", "m3/h") else 60 if unit in ("l/min", "lpm") else 1
        force_eid = self.force_entity()
        force_st = get(force_eid) if force_eid else None
        sample = Sample(
            t=ts, p_el=p_el, rps=num("nopeus"), te=num("hoyrystyminen"), tc=num("lauhtuminen"),
            t_suc=num("imukaasu"), t_dis=num("kuumakaasu"), eev=num("eev"),
            sh_act=num("tulistus"), sh_tgt=num("tulistus_tavoite"), t_out=t_out,
            t_sup=num("menovesi"), t_ret=num("paluuvesi"), q_w=kw("vesilampo"), frost=num("huurre"),
            comp_on=comp_on, defrost=defrost, fault=fault,
            heater=heater_state is not None and heater_state.state == "on",
            comp_on_min=minutes_since(comp) if comp_on else None,
            since_defrost_min=None if dfr is None or dfr.state in BAD else (0.0 if defrost else minutes_since(dfr)),
            t_fin=num("lamelli"), t_amb=num("ulkoyksikko"), ffd=num("huurteeton_ero"), flow=flow,
            fan=num("puhallin"),
            defrost_changed=None if dfr is None or dfr.state in BAD else dfr.last_changed.timestamp(),
            force_ok=force_st is not None and force_st.state not in BAD,
            missing=tuple(m for m in missing if m),
        )
        self.invalid = list(dict.fromkeys(sample.missing))
        self.missing = self._missing_entities()
        return sample

    def _missing_entities(self) -> list[str]:
        """Configured entities that do not exist. A role covered by its stand-in is not missing."""
        get = self.hass.states.get
        stand_ins = set(FALLBACKS.values())
        out = []
        for key, eid in self.sensors.items():
            if not eid or key in stand_ins or get(eid) is not None:
                continue
            alt = self.sensors.get(FALLBACKS.get(key, ""))
            if alt and get(alt) is not None:
                continue
            out.append(eid)
        return out + [eid for eid in self.faults if eid and get(eid) is None]

    # ------------------------------------------------------------------ for entities
    def eta_now(self):
        pts = [p.eta for p in list(self.brain.points)[-15:] if p.stable and p.eta is not None]
        return median(pts)

    def blockers(self) -> list[dict]:
        """What keeps the AI from acting right now, most important first, and what to do about it.

        taso: virhe = needs the owner, odottaa = clears by itself, tieto = by design.
        """
        b = self.brain
        out: list[dict] = []

        def add(taso: str, koodi: str, teksti: str, korjaus: str | None = None) -> None:
            out.append({"taso": taso, "koodi": koodi, "teksti": teksti, "korjaus": korjaus})

        if self.last_tick is None:
            add("odottaa", "kaynnistyy", "Odottaa Home Assistantin käynnistymistä ja ensimmäisiä mittauksia.")
            return out
        if b.mode == MODE_OFF:
            add("tieto", "pois", "EEV-säätö on pois päältä.", "Valitse tila Tarkkailu, Ehdota tai Automaattinen.")
        elif b.mode == MODE_OBSERVE:
            add("tieto", "tarkkailu", "Tarkkailutila: tekoäly ei muuta mitään, se kertoo vain, mitä tekisi.",
                "Vaihda tilaksi Ehdota tai Automaattinen, kun suunnitelmat näyttävät järkeviltä.")
        if self.invalid:
            add("virhe", "mittaukset", "Pakollinen mittaus puuttuu tai ei päivity: " + ", ".join(self.invalid) + ".",
                "Tarkista entiteetti Kehittäjätyökaluissa. Jos nimesi on eri, vaihda se anturit:-kohtaan.")
        eev_keys = [*MIN_OD, *SV]
        found = sum(1 for k in eev_keys if k in self.overrides or k in self._entities)
        usable = sum(1 for k in eev_keys if (p := b.params.get(k)) is not None and p.available)
        if found == 0:
            add("virhe", "mango_puuttuu", "Mango Tuya Local -integraation EEV-parametreja (Heat Min OD, Heat Sv) "
                "ei löydy, joten tekoäly ei voi säätää.",
                "Asenna Mango Tuya Local ja varmista, että EEV Set -parametrit näkyvät entiteetteinä.")
        elif usable < found:
            add("odottaa" if usable else "virhe", "mango_saatavilla",
                f"Mango-parametreista saatavilla {usable}/{found}.",
                "Mango lukee parametreja kierroksittain. Jos luku ei nouse, tarkista Mango-yhteys ja ettei "
                "entiteettejä ole poistettu käytöstä.")
        if b.paused_until > b.now:
            add("odottaa", "tauko", f"Tauko {_local(b.paused_until)} asti: {b.pause_reason}.")
        if b.queue:
            add("odottaa", "palautus", "Palauttaa arvoja: "
                + ", ".join(f"{q['key'].split('|')[-1]} → {q['value']}" for q in b.queue) + ".")
        n, need = b.steady_minutes()
        last = b.points[-1] if b.points else None
        reported = self.invalid and last is not None and last.why == "puuttuu"  # already listed above
        if b.action is None and b.mode != MODE_OFF and n < need and not reported:
            why = b.waiting_reason()
            add("odottaa", "tasainen", f"Tasaista käyntiä {n}/{need} min" + (f": {why}." if why else "."),
                "Päätökset tehdään vain tasaisesta käynnistä. Lyhyiden käyntijaksojen tasaiset minuutit "
                "lasketaan yhteen samoissa oloissa.")
        if b.action is None and b.cooldown_until > b.now and b.mode in (MODE_PROPOSE, MODE_AUTO):
            add("odottaa", "jaahdyttely", f"Seuraava muutos aikaisintaan {_local(b.cooldown_until)}.")
        if not b._budget_ok() and b.mode in (MODE_PROPOSE, MODE_AUTO):
            add("odottaa", "kirjoitusraja", f"Kirjoitusraja {b.s.writes_per_day}/vrk on täynnä.")
        if b.pending:
            add("tieto", "ehdotus", f"Ehdotus odottaa hyväksyntää: {b.pending['title']}.",
                "Hyväksy tai hylkää ehdotus.")
        if (b.action is None and b.mode != MODE_OFF and n >= need and b.suggestion is None and not b.pending
                and not self.invalid):
            eta = self.eta_now()
            if eta is not None and eta >= b.wet_eta:
                add("tieto", "ei_korjattavaa", "Imukaasu on märkää, mutta säädettävää ei nyt ole: parametrit ovat "
                    "rajoillaan tai tauolla edellisen kokeilun jälkeen.")
            else:
                level = "" if eta is None else (f" (näennäinen hyötysuhde {round(eta * 100)} %, märkä yli "
                                                f"{round(b.wet_eta * 100)} %)")
                add("tieto", "ei_korjattavaa", f"Ei säädettävää: imukaasu ei ole märkää{level}.")
        d = b.defrost
        if d.mode in (MODE_PROPOSE, MODE_AUTO) and b.s.enable_defrost and self.force_entity() is None:
            add("virhe", "pakkosulatus", "Pakkosulatusskriptiä ei löydy, joten tekoäly ei voi käynnistää sulatusta.",
                "Lisää mango_cop_ai:-kohtaan rivi pakkosulatus: ja skriptisi entiteetti-id, esimerkiksi "
                "script.sulatus_pakotettu_moduuli_0.")
        if d.view.get("blocked") and d.mode != MODE_OFF:
            add("odottaa", "sulatus_estetty", f"Sulatus olisi tarpeen, mutta: {d.view['blocked']}.")
        return out


def _local(ts: float) -> str:
    """Local time of a timestamp: 14:05, or 28.10. 14:05 when not today."""
    t = dt_util.as_local(datetime.fromtimestamp(ts, UTC))
    return t.strftime("%H:%M") if t.date() == dt_util.now().date() else t.strftime("%d.%m. %H:%M")
