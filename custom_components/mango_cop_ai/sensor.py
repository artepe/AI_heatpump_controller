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
]


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    runner = hass.data[DOMAIN]
    async_add_entities((StatusSensor if spec[0] in ("tilanne", "diagnoosi") else AiSensor)(runner, *spec)
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
    })
