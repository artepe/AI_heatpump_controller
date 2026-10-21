"""End to end in a real Home Assistant core: the AI writes through the real Mango Tuya Local
integration (simulated Tuya transport) while the simulated heat pump reads the same parameters."""

from datetime import timedelta
from pathlib import Path

import yaml
from homeassistant.components import persistent_notification
from homeassistant.core import callback
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.mango_cop_ai.const import DOMAIN

from .plant import DEFROST_VALUES, DFR_INTE_HW, Plant, to_states

DATA = {"host": "127.0.0.1", "device_id": "TEST_DEVICE", "local_key": "0000000000000000"}
OD6, OD5 = "5:41", "5:40"
ENTITIES = [
    "select.cop_tekoaly_tila", "sensor.cop_tekoaly_tilanne", "sensor.cop_tekoaly_diagnoosi",
    "sensor.cop_tekoaly_cop", "sensor.cop_tekoaly_cop_poikkeama", "sensor.cop_tekoaly_cop_laatuaste",
    "sensor.cop_tekoaly_naennainen_hyotysuhde", "sensor.cop_tekoaly_kirjoitukset_24_h",
    "button.cop_tekoaly_hyvaksy_ehdotus", "button.cop_tekoaly_hylkaa_ehdotus",
    "button.cop_tekoaly_palauta_alkuperaiset",
    "select.cop_tekoaly_sulatustila", "sensor.cop_tekoaly_sulatustarve", "sensor.cop_tekoaly_sulatus",
]


class Rig:
    def __init__(self, hass, freezer, hardware, **plant):
        self.hass, self.freezer, self.hw = hass, freezer, hardware
        self.plant = Plant(hardware.values, **plant)
        self.logbook = []
        self.frozen = set()  # entities whose integration stopped updating
        hass.bus.async_listen("logbook_entry", lambda e: self.logbook.append(e.data["message"]))

    def push(self):
        for eid in (
            "binary_sensor.hp_fault_low_pressure",
            "binary_sensor.hp_fault_high_pressure",
            "binary_sensor.hp_fault_discharge_temperature_high",
        ):
            if eid not in self.frozen:
                self.hass.states.async_set(eid, "off")
        for eid, (state, attrs) in to_states(self.plant.step()).items():
            if eid not in self.frozen:
                self.hass.states.async_set(eid, state, attrs)

    async def start(self, conf=None):
        entry = MockConfigEntry(domain="mango_tuya_local", unique_id="TEST_DEVICE", title="Mango test", data=DATA)
        entry.add_to_hass(self.hass)
        assert await self.hass.config_entries.async_setup(entry.entry_id)
        await self.hass.async_block_till_done(wait_background_tasks=True)
        self.push()
        assert await async_setup_component(self.hass, DOMAIN, {DOMAIN: conf})
        await self.hass.async_block_till_done()
        self.runner = self.hass.data[DOMAIN]
        # Legacy plant tests deliberately use simulated, proven raw parameter routes.
        # Production does NOT trust these routes without explicit owner approval.
        from custom_components.mango_cop_ai.const import ALL_PARAMETERS
        self.runner.trusted_parameters.update(ALL_PARAMETERS)
        self.runner.brain.s.power_meter_confirmed = True
        self.runner.brain.s.sh_guard_k = 0.0
        self.runner.brain.s.require_water_cop = False
        return self

    async def run(self, minutes, until=None):
        for _ in range(minutes):
            self.push()
            for _ in range(2):  # 30 s steps keep the Mango poller on its normal cadence
                self.freezer.tick(timedelta(seconds=30))
                async_fire_time_changed(self.hass)
                await self.hass.async_block_till_done()
            if until and until(self):
                return True
        return False

    def state(self, eid):
        return self.hass.states.get(eid)

    def notifications(self):
        return persistent_notification._async_get_or_create_notifications(self.hass)

    async def press(self, eid):
        await self.hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
        await self.hass.async_block_till_done()


async def test_observe_mode_uses_the_real_entities_and_never_writes(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start(None)
    await rig.run(135)  # ends in steady running, between oil returns and before the defrost
    assert hardware.writes == 0
    assert sorted(e for e in hass.states.async_entity_ids() if "cop_tekoaly" in e) == sorted(ENTITIES)
    assert rig.state("select.cop_tekoaly_tila").state == "Tarkkailu"
    status = rig.state("sensor.cop_tekoaly_tilanne")
    assert status.attributes["mango_parametrit_löytyi"] == 43  # 19 EEV + 24 defrost parameters
    assert status.attributes["tekisi"] == "Heat Min OD 18 → 16 (aktiivinen vyöhyke)"
    assert any(m.startswith("Tarkkailu, ei muutettu mitään") for m in rig.logbook)
    assert "Imukaasu märkää" in rig.state("sensor.cop_tekoaly_diagnoosi").state
    assert float(rig.state("sensor.cop_tekoaly_naennainen_hyotysuhde").state) > 85
    assert 2 < float(rig.state("sensor.cop_tekoaly_cop").state) < 5


async def test_auto_mode_tunes_the_active_zone_through_mango_and_keeps_it(
        hass, freezer, mocked_client, hardware, hass_storage):
    rig = await Rig(hass, freezer, hardware).start({"tila": "Automaattinen"})
    await rig.run(4 * 60)
    assert hardware.values[OD6] == 16
    assert all(hardware.values[f"5:{36 + i}"] == v for i, v in enumerate([25, 25, 25, 25, 18]))
    assert all(hardware.values[f"5:{42 + i}"] == 18 for i in range(3))
    assert hardware.writes == 1
    assert "mango_cop_ai_toimenpide" in rig.notifications()
    assert any(m.startswith("Hyväksytty: Heat Min OD6: 18 → 16") for m in rig.logbook)
    attrs = rig.state("sensor.cop_tekoaly_tilanne").attributes
    assert attrs["muutetut_arvot"] == {"Heat Min OD6": {"alkuperäinen": 18, "nyt": 16}}
    stored = hass_storage[DOMAIN]["data"]
    assert stored["managed"] == {"EEV Set|Heat Min OD6": 16}
    assert stored["hits"][0]["key"] == "EEV Set|Heat Min OD6"


async def test_rollback_button_restores_and_returns_to_observe(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start({"tila": "Automaattinen"})
    await rig.run(3 * 60)
    assert hardware.values[OD6] == 16
    await rig.press("button.cop_tekoaly_palauta_alkuperaiset")
    assert hardware.values[OD6] == 18
    assert rig.state("select.cop_tekoaly_tila").state == "Tarkkailu"
    writes = hardware.writes
    await rig.run(2 * 60)
    assert hardware.writes == writes


async def test_propose_mode_waits_for_the_approve_button(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start({"tila": "Ehdota"})
    await rig.run(2 * 60)
    assert hardware.writes == 0
    assert "mango_cop_ai_ehdotus" in rig.notifications()
    assert rig.state("sensor.cop_tekoaly_tilanne").state == "Ehdotus odottaa hyväksyntää"
    await rig.press("button.cop_tekoaly_hyvaksy_ehdotus")
    assert "mango_cop_ai_ehdotus" not in rig.notifications()
    assert await rig.run(3 * 60, until=lambda r: r.hw.values[OD6] == 16)


async def test_fault_restores_values_and_pauses(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start({"tila": "Automaattinen"})
    await rig.run(3 * 60)
    assert hardware.values[OD6] == 16
    rig.plant.fault = True
    await rig.run(3)
    assert hardware.values[OD6] == 18
    assert rig.state("sensor.cop_tekoaly_tilanne").state.startswith("Tauko")
    assert "mango_cop_ai_tauko" in rig.notifications()


async def test_switching_off_during_a_trial_restores_the_candidate(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start({"tila": "Automaattinen"})
    assert await rig.run(2 * 60, until=lambda r: r.runner.brain.action is not None
                         and r.runner.brain.action["phase"] == "probe_wait")
    assert hardware.values[OD6] == 16
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": "select.cop_tekoaly_tila", "option": "Pois"}, blocking=True)
    await hass.async_block_till_done()
    assert hardware.values[OD6] == 18
    assert rig.state("sensor.cop_tekoaly_tilanne").state == "Pois päältä"


async def test_device_refusing_a_write_is_handled(hass, freezer, mocked_client, hardware):
    hardware.ignore_target = True  # the first write is not confirmed: Mango restores and raises
    rig = await Rig(hass, freezer, hardware).start({"tila": "Automaattinen"})
    await rig.run(3 * 60)
    attrs = rig.state("sensor.cop_tekoaly_tilanne").attributes
    assert "Heat Min OD6" in attrs["hylätyt_parametrit"]
    assert hardware.values[OD6] == 18
    assert all(hardware.values[f"5:{40 + i}"] == 18 for i in range(5))


async def test_missing_sensor_blocks_decisions(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start(
        {"tila": "Automaattinen", "anturit": {"eev": "sensor.ei_ole_olemassa"}})
    await rig.run(90)
    assert hardware.writes == 0
    assert "sensor.ei_ole_olemassa" in rig.state("sensor.cop_tekoaly_diagnoosi").state
    assert "sensor.ei_ole_olemassa" in rig.state("sensor.cop_tekoaly_tilanne").attributes["puuttuvat_entiteetit"]


async def test_yaml_settings_reach_the_brain(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start({
        "tila": "Ehdota",
        "asetukset": {"kirjoituksia_vrk": 4, "varmistus_min": 30, "saada_sv": False, "cop_lahde": "vesipuoli",
                      "vesipuolen_kerroin": 0.62, "eev_askeleet": 500},
        "vikatilat": ["binary_sensor.hp_fault_active"],
        "parametrit": {"EEV Set|Heat Min OD6": "number.mango_test_eev_set_heat_min_od6_raw"},
    })
    s = rig.runner.brain.s
    assert (s.writes_per_day, s.verify_min, s.enable_sv, s.cop_basis, s.water_factor, s.eev_full_steps) == (
        4, 30, False, "vesipuoli", 0.62, 500)
    assert rig.runner.entity_for("EEV Set|Heat Min OD6") == "number.mango_test_eev_set_heat_min_od6_raw"
    assert hass.states.get("number.mango_test_eev_set_heat_min_od6_raw").state == "18.0"
    assert rig.state("select.cop_tekoaly_tila").state == "Ehdota"


async def test_frozen_modbus_reading_stops_decisions(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start({"tila": "Automaattinen"})
    rig.frozen = {"sensor.eev_main_opening"}  # stuck at its last value
    await rig.run(2 * 60)
    assert hardware.writes == 0
    assert "sensor.eev_main_opening" in rig.state("sensor.cop_tekoaly_diagnoosi").state


EXAMPLES = Path(__file__).parents[1] / "examples"


def render_cards(hass) -> list[str]:
    """Every markdown section of both dashboard cards, rendered like the frontend asks Home Assistant to."""
    out = []
    for name in ("kortti.yaml", "kortti_tekninen.yaml"):
        card = yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))
        for c in card["cards"]:
            if c["type"] == "markdown":
                out.append(Template(c["content"], hass).async_render(parse_result=False))
    return out


def table_rows(text: str, header: str) -> list[str]:
    lines = text.splitlines()
    i = lines.index(header)
    rows = []
    for line in lines[i + 2:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    assert lines[i + 1].startswith("|:"), lines[i + 1]
    return rows


async def _with_script(hass, rig):
    @callback
    def force(call):
        rig.plant.force()

    hass.services.async_register("test", "force_defrost", force)
    assert await async_setup_component(hass, "script", {"script": {"sulatus_pakotettu_moduuli_0": {
        "alias": "Sulatus pakotettu (moduuli 0)", "sequence": [{"action": "test.force_defrost"}]}}})
    await hass.async_block_till_done()


async def test_ai_defrosts_through_the_owners_script(hass, freezer, mocked_client, hardware):
    hardware.values.update({**DEFROST_VALUES, DFR_INTE_HW[2]: 90})  # the controller would wait 90 min
    rig = Rig(hass, freezer, hardware, defrost_ctrl={"table": True}, t_out=lambda m: -1.0, frost_rate=4.0)
    await _with_script(hass, rig)
    await rig.start({"sulatus_tila": "Automaattinen"})
    assert rig.state("select.cop_tekoaly_sulatustila").state == "Automaattinen"
    assert rig.state("select.cop_tekoaly_tila").state == "Tarkkailu"
    seen_need = []

    def watch(r):
        need = r.state("sensor.cop_tekoaly_sulatustarve").state
        if need not in ("unknown", "unavailable"):
            seen_need.append(float(need))
        return r.plant.forced >= 2

    assert await rig.run(5 * 60, until=watch)
    assert max(seen_need) >= 100
    assert any(m.startswith("Pakkosulatus: Kennossa on huurretta") for m in rig.logbook)
    await rig.run(10)
    attrs = rig.state("sensor.cop_tekoaly_sulatus").attributes
    assert attrs["viimeisimmät"][0]["laukaisi"] == "tekoäly"
    assert attrs["pakkosulatus_skripti"] == "script.sulatus_pakotettu_moduuli_0"
    assert attrs["säätimen_sulatusparametrit"]["Dfr.Inte. 3"] == 90
    assert hardware.writes == 0  # forced defrosts need no parameter writes
    need = rig.state("sensor.cop_tekoaly_sulatustarve")
    assert need.attributes["kenno"] in ("Puhdas", "Mittaa puhtaan kennon tasoa", "Huurtuu", "Sulatus käynnissä")
    cards = render_cards(hass)
    dfr = next(c for c in cards if "Sulatus · Automaattinen" in c)
    recent = table_rows(dfr, "| aika | kesto | käynti | ulko | K | huurre | arvio | laukaisi |")
    assert len(recent) == len(attrs["viimeisimmät"]) and "tekoäly" in recent[0]
    bands = table_rows(dfr, "| kaista | ulko | väli | huom |")
    assert len(bands) == 6 and bands[2].startswith("| 3 | -5…0 °C | 90 min")
    history = next(c for c in cards if "### Päätökset" in c)
    assert "Ei vielä päätöksiä" in history


async def test_clean_coil_defrosts_lengthen_the_interval_through_mango(hass, freezer, mocked_client, hardware):
    hardware.values.update({**DEFROST_VALUES, DFR_INTE_HW[4]: 35})  # band 5, short interval
    rig = Rig(hass, freezer, hardware, defrost_ctrl={"table": True}, t_out=lambda m: -12.0, frost_rate=0.3)
    await rig.start({"sulatus_tila": "Automaattinen"})
    assert await rig.run(7 * 60, until=lambda r: r.hw.values[DFR_INTE_HW[4]] == 40)
    assert hardware.writes == 1
    assert hass.states.get("number.mango_test_defro_set_dfr_inte_5_raw").state == "40.0"
    attrs = rig.state("sensor.cop_tekoaly_sulatus").attributes
    assert attrs["toimenpide"]["muutos"] == "Dfr.Inte. 5 35 → 40 min"
    assert attrs["muutetut_sulatusvälit"] == {"Dfr.Inte. 5": {"alkuperäinen": 35, "nyt": 40}}
    assert attrs["sulatusväli_kaistoittain"] == {"5": "Dfr.Inte. 5"}
    dfr = next(c for c in render_cards(hass) if "Sulatus · Automaattinen" in c)
    assert "Varmistaa Dfr.Inte. 5 35 → 40 min" in dfr
    assert table_rows(dfr, "| kaista | ulko | väli | huom |")[4] == "| 5 | ≤ -10 °C | 40 min | 35→40, ohjaa sulatuksia |"
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": "select.cop_tekoaly_sulatustila", "option": "Tarkkailu"},
                                   blocking=True)
    await hass.async_block_till_done()
    assert hardware.values[DFR_INTE_HW[4]] == 35  # the unverified trial is undone
