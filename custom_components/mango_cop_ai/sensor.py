"""Status, diagnosis and COP sensors of the Mango COP AI."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .brain import short
from .const import DOMAIN, SIGNAL_UPDATE


def _r(v, digits=2):
    return None if v is None else round(v, digits)


def _ts(t):
    return None if not t else datetime.fromtimestamp(t, UTC).isoformat()


def _optimization_advice(runner):
    """Read-only suggestions. Never command compressor, Ouman or fan hardware."""
    b = runner.brain
    p = b.points[-1] if b.points else None
    if not p or not p.stable:
        return "Odota tasaista käyntiä ja varmista mittaukset ennen optimointivertailua."
    if not b.s.power_meter_confirmed:
        return "Vahvista koko lämpöpumpun ottotehon mittaus ennen COP-vertailuja."
    if p.t_out is not None and p.t_out >= 2 and p.t_sup is not None and p.t_sup >= 50:
        return ("Tutki voiko menoveden tavoitetta alentaa 1-2 K ilman mukavuus-"
                "tai varaajarajoituksen rikkomista; AI ei muuta Oumanin asetusta.")
    return "Vertaa eri kompressorinopeuksia saman ulko- ja menolampotilan mittausjaksoissa."


def _matched_speed_quality(runner):
    """Available learned water-COP quality levels at similar outdoor/water temperature."""
    b = runner.brain
    p = b.points[-1] if b.points else None
    if not p or not p.stable or p.t_sup is None or p.t_out is None or not b.s.power_meter_confirmed:
        return {}
    from .analysis import Baselines

    speeds = {"<30": 25.0, "30-45": 35.0, ">45": 50.0}
    result = {}
    for name, representative in speeds.items():
        key = Baselines.key(p.t_out, representative, p.t_sup)
        row = b.baselines.bins.get(key, {})
        if row.get("n", 0) >= 4 and row.get("lq_w") is not None:
            result[name] = {"laatuaste": round(row["lq_w"] * 100, 1),
                            "vertailuikkunoita": row["n"]}
    return result


def _status_attrs(runner):
    b = runner.brain
    st = b.status()
    return {
        "tila": st["mode"],
        "toimenpide": st["action"],
        "ehdotus": st["pending"],
        "hyväksytty_odottaa": st["approved"],
        "tekisi": st["suggestion"],
        "kirjoituksia_24h": st["writes_24h"],
        "kirjoituksia_yhteensä": st["writes_total"],
        "kirjoitusraja_vrk": b.s.writes_per_day,
        "muutetut_arvot": st["changed"],
        "opitut_vyöhykkeet": st["zones"],
        "alarajat": {short(k): v for k, v in b.limits.items()},
        "historia": [{**h, "t": _ts(h["t"])} for h in st["history"]],
        "hylätyt_parametrit": st["failed"],
        "tauko_asti": _ts(st["paused_until"]),
        "kuivan_imun_taso": _r(b.eta_ref),
        "märkäraja": _r(b.wet_eta),
        "puuttuvat_entiteetit": runner.missing,
        "mango_parametrit_löytyi": len(runner._entities),
        "mango_parametrit_saatavilla": sum(1 for p in b.params.values() if p.available),
        "vahvistetut_parametrit": sorted(runner.trusted_parameters),
        "turvalukitus": {
            "puuttuvat_anturit": bool(runner.missing),
            "kokonaisottoteho_vahvistamatta": not b.s.power_meter_confirmed,
            "parametrivyohykkeet_vahvistamatta": not bool(runner.trusted_parameters),
        },
        "optimointi_vinkki": _optimization_advice(runner),
        "nopeusvertailu_lukutilassa": _matched_speed_quality(runner),
    }


def _cop_attrs(runner):
    c = runner.brain.cop
    return {
        "cop_kylmäpiiri": _r(c.get("cop_kp")),
        "cop_vesipuoli": _r(c.get("cop_w")),
        "carnot_cop": _r(c.get("carnot")),
        "odotettu_cop": _r(c.get("expected")),
        "poikkeama_kylmäpiiri_pros": _r(c.get("deviation_kp"), 1),
        "poikkeama_vesipuoli_pros": _r(c.get("deviation_w"), 1),
        "laatuaste_kylmäpiiri": _r(c.get("lq_kp"), 3),
        "laatuaste_vesipuoli": _r(c.get("lq_w"), 3),
        "lähde": c.get("basis"),
        "vertailuluokka": c.get("bin"),
        "tasaisia_minuutteja": c.get("minutes"),
    }


def _need(runner):
    v = runner.brain.defrost.view.get("need")
    return None if v is None else round(v)


def _need_attrs(runner):
    v = runner.brain.defrost.view
    ref = v.get("ref") or {}
    return {
        "kenno": v.get("state"),
        "huurre_k": _r(v.get("frost"), 2),
        "huurre_lamelli_k": _r(v.get("frost_fin"), 2),
        "cop_häviö_pros": _r(None if v.get("loss") is None else v["loss"] * 100, 1),
        "käynti_sulatuksesta_min": _r(v.get("run"), 0),
        "arvio_sulatustarpeeseen_min": _r(v.get("eta"), 0),
        "oma_huurrekynnys_k": _r(v.get("target"), 1),
        "puhdas_lähestymä_k": _r(ref.get("appr"), 2),
        "viite_nopeus": _r(ref.get("rps"), 1),
        "säätimen_huurre_ero_k": _r(v.get("excess"), 1),
        "kaista": v.get("band"),
        "pakkosulatus_estetty": v.get("blocked"),
        "pakkosulatus_skripti": runner.force_entity(),
    }


def _defrost_attrs(runner):
    d = runner.brain.defrost
    st = d.status()
    return {
        "tila": st["mode"],
        "toimenpide": st["action"],
        "tekisi": st["suggestion"],
        "viimeisimmät": [{
            "aika": _ts(r["t"]), "kesto_s": r["dur"], "käynti_min": r["run"], "ulko": r["t_air"],
            "kaista": r["band"], "huurre_k": r["frost"], "arvio": r["verdict"], "syy": r["why"],
            "laukaisi": r["trigger"], "kwh": r["e_def"]} for r in st["records"]],
        "sulatuksia_24h": st["count_24h"],
        "tekoälyn_sulatuksia_24h": st["forced_24h"],
        "arviot_24h": st["verdicts_24h"],
        "sulatusenergia_24h_kwh": st["energy_24h"],
        "kaistat_7vrk": st["stats"],
        "sulatusväli_kaistoittain": st["binding"],
        "säätimen_sulatusparametrit": st["params"],
        "muutetut_sulatusvälit": st["changed"],
        "suositukset": st["recommendations"],
        "oma_huurrekynnys_k": _r(st["target"], 1),
        "pakkosulatus_skripti": runner.force_entity(),
        "pakkosulatus_tauolla_asti": _ts(st["force_block"]),
    }


def _primary(runner, kp, w):
    c = runner.brain.cop
    return c.get(kp if c.get("basis", "kylmapiiri") == "kylmapiiri" else w)


SENSORS: list[tuple[str, str, str, str | None, Callable, Callable | None]] = [
    ("tilanne", "COP-tekoäly tilanne", "mdi:robot", None,
     lambda r: r.brain.status_text, _status_attrs),
    ("diagnoosi", "COP-tekoäly diagnoosi", "mdi:stethoscope", None,
     lambda r: r.brain.diagnosis["text"][:250],
     lambda r: {"koodi": r.brain.diagnosis["code"], **r.brain.diagnosis.get("details", {})}),
    ("cop", "COP-tekoäly COP", "mdi:heat-pump-outline", None,
     lambda r: _r(_primary(r, "cop_kp", "cop_w")), _cop_attrs),
    ("cop_poikkeama", "COP-tekoäly COP-poikkeama", "mdi:delta", PERCENTAGE,
     lambda r: _r(r.brain.cop.get("deviation_pct"), 1), None),
    ("cop_laatuaste", "COP-tekoäly COP-laatuaste", "mdi:gauge", PERCENTAGE,
     lambda r: _r(None if (v := _primary(r, "lq_kp", "lq_w")) is None else v * 100, 1), None),
    ("naennainen_hyotysuhde", "COP-tekoäly näennäinen hyötysuhde", "mdi:water-percent", PERCENTAGE,
     lambda r: _r(None if (v := r.eta_now()) is None else v * 100, 1),
     lambda r: {"kuivan_imun_taso_pros": _r(r.brain.eta_ref * 100, 1),
                "märkäraja_pros": _r(r.brain.wet_eta * 100, 1)}),
    ("kirjoitukset_24h", "COP-tekoäly kirjoitukset 24 h", "mdi:counter", None,
     lambda r: r.brain._writes_24h(), lambda r: {"yhteensä": r.brain.writes_total}),
    ("sulatustarve", "COP-tekoäly sulatustarve", "mdi:snowflake-alert", PERCENTAGE, _need, _need_attrs),
    ("sulatus", "COP-tekoäly sulatus", "mdi:snowflake-melt", None,
     lambda r: r.brain.defrost.status_text()[:250], _defrost_attrs),
]
TEXT_SENSORS = ("tilanne", "diagnoosi", "sulatus")


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    runner = hass.data[DOMAIN]
    async_add_entities((StatusSensor if spec[0] in TEXT_SENSORS else AiSensor)(runner, *spec)
                       for spec in SENSORS)


class AiSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, runner, key, name, icon, unit, value, attrs):
        self.runner = runner
        self._value, self._attrs = value, attrs
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        if unit is not None or key in ("cop", "kirjoitukset_24h"):
            self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self):
        return self._value(self.runner)

    @property
    def extra_state_attributes(self):
        return None if self._attrs is None else self._attrs(self.runner)


class StatusSensor(AiSensor):
    """Text sensors with large attributes: keep the attributes out of the recorder database."""

    _unrecorded_attributes = frozenset({
        "tila", "toimenpide", "ehdotus", "hyväksytty_odottaa", "tekisi", "kirjoituksia_24h",
        "kirjoituksia_yhteensä", "kirjoitusraja_vrk", "muutetut_arvot", "opitut_vyöhykkeet", "alarajat",
        "historia", "hylätyt_parametrit", "tauko_asti", "kuivan_imun_taso", "märkäraja",
        "puuttuvat_entiteetit", "mango_parametrit_löytyi", "mango_parametrit_saatavilla", "koodi",
        "näennäinen_hyötysuhde", "märkänä",
        "eev_askeleet",
        "viimeisimmät", "sulatuksia_24h", "tekoälyn_sulatuksia_24h", "arviot_24h",
        "sulatusenergia_24h_kwh", "kaistat_7vrk", "sulatusväli_kaistoittain", "säätimen_sulatusparametrit",
        "muutetut_sulatusvälit", "suositukset", "oma_huurrekynnys_k", "pakkosulatus_skripti",
        "pakkosulatus_tauolla_asti",
    })
