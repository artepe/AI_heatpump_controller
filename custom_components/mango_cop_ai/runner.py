"""Home Assistant side of the Mango COP AI: reads states, writes parameters, keeps the state."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import timedelta

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
from .brain import Brain, Dismiss, Log, Notify, Param, Write
from .const import (
    DEFAULT_SENSORS,
    DOMAIN,
    EEV_MAX_STEP,
    MANGO_DOMAIN,
    MIN_OD,
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
STATUS_ENTITY = "sensor.cop_tekoaly_tilanne"
STALE_S = 15 * 60  # a core reading not refreshed for this long counts as missing


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
        self.brain = Brain(settings, stored, mode=conf.get("tila"))
        self.store = store
        self.lock = asyncio.Lock()
        self._entities: dict[str, str] = {}
        self._unsub = None
        self.missing: list[str] = []
        self.last_tick = None

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

    async def _execute(self, cmds: list) -> None:
        for _ in range(25):
            writes = []
            for c in cmds:
                if isinstance(c, Write):
                    writes.append(c)
                elif isinstance(c, Log):
                    self._log(c.message)
                elif isinstance(c, Notify):
                    persistent_notification.async_create(
                        self.hass, c.message, title=c.title, notification_id=f"{DOMAIN}_{c.tag}"
                    )
                elif isinstance(c, Dismiss):
                    persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{c.tag}")
            if not writes:
                return
            cmds = []
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

    def _params(self) -> dict[str, Param]:
        out = {}
        for key in [*MIN_OD, *SV, EEV_MAX_STEP]:
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
    def _sample(self, ts: float) -> Sample:
        get = self.hass.states.get
        s = self.sensors
        missing = []

        def num(key):
            eid = s.get(key)
            st = get(eid) if eid else None
            v = _float(st)
            if key in CORE_KEYS.values() and key != "ulko":
                # a frozen Modbus value must not look like steady running: Home Assistant refreshes
                # last_reported on every poll even when the value does not change
                reported = getattr(st, "last_reported", None) or getattr(st, "last_updated", None)
                if v is not None and reported is not None and ts - reported.timestamp() > STALE_S:
                    v = None
                if v is None:
                    missing.append(eid or key)
            return v

        def kw(key):
            eid = s.get(key)
            st = get(eid) if eid else None
            v = _float(st)
            if v is None:
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
        comp = get(s["kompressori"]) if s.get("kompressori") else None
        comp_on = comp is not None and comp.state == "on"
        dfr = get(s["sulatus"]) if s.get("sulatus") else None
        defrost = dfr is not None and dfr.state == "on"
        p_el = kw("sahko")
        if p_el is None:
            missing.append(s.get("sahko"))
        fault = any((st := get(eid)) is not None and st.state == "on" for eid in self.faults)
        heater_state = get(s["vastus"]) if s.get("vastus") else None
        sample = Sample(
            t=ts, p_el=p_el, rps=num("nopeus"), te=num("hoyrystyminen"), tc=num("lauhtuminen"),
            t_suc=num("imukaasu"), t_dis=num("kuumakaasu"), eev=num("eev"),
            sh_act=num("tulistus"), sh_tgt=num("tulistus_tavoite"), t_out=t_out,
            t_sup=num("menovesi"), t_ret=num("paluuvesi"), q_w=kw("vesilampo"), frost=num("huurre"),
            comp_on=comp_on, defrost=defrost, fault=fault,
            heater=heater_state is not None and heater_state.state == "on",
            comp_on_min=minutes_since(comp) if comp_on else None,
            since_defrost_min=None if dfr is None or dfr.state in BAD else (0.0 if defrost else minutes_since(dfr)),
            missing=tuple(m for m in missing if m),
        )
        self.missing = [eid for eid in [*self.sensors.values(), *self.faults] if eid and get(eid) is None]
        return sample

    # ------------------------------------------------------------------ for entities
    def eta_now(self):
        pts = [p.eta for p in list(self.brain.points)[-15:] if p.stable and p.eta is not None]
        return median(pts)
