"""End to end in a real Home Assistant core: the AI writes through the real Mango Tuya Local
integration (simulated Tuya transport) while the simulated heat pump reads the same parameters."""

from datetime import timedelta
from pathlib import Path

import yaml
from homeassistant.components import frontend, persistent_notification
from homeassistant.core import callback
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.mango_cop_ai.card import CARD_URL
from custom_components.mango_cop_ai.const import DOMAIN
from custom_components.mango_cop_ai.runner import STALE_S

from .plant import DEFROST_VALUES, DFR_INTE_HW, Plant, to_states

DATA = {"host": "127.0.0.1", "device_id": "TEST_DEVICE", "local_key": "0000000000000000"}
OD6, OD5 = "5:41", "5:40"
ENTITIES = [
    "sensor.cop_tekoaly_mittausten_tila",
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
        self.change_only = set()  # entities written only when they change (template, ESPHome, MQTT)
        self.rename = {}  # the owner's entity id -> the id this installation uses
        hass.bus.async_listen("logbook_entry", lambda e: self.logbook.append(e.data["message"]))

    def push(self):
        # every other entity is re-written every minute like a polled Modbus register
        for eid, (state, attrs) in to_states(self.plant.step()).items():
            eid = self.rename.get(eid, eid)
            if eid in self.frozen:
                continue
            old = self.hass.states.get(eid)
            if eid in self.change_only and old is not None and old.state == state:
                continue
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
    assert await rig.run(180, until=lambda r: r.runner.brain.suggestion is not None)
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
    await rig.run(3)  # Modbus re-reports the unchanged EEV opening on every poll
    assert "sensor.eev_main_opening" in rig.runner.polled
    rig.frozen = {"sensor.eev_main_opening"}  # then the reading gets stuck
    await rig.run(2 * 60)
    assert hardware.writes == 0
    assert "sensor.eev_main_opening" in rig.state("sensor.cop_tekoaly_diagnoosi").state
    blockers = rig.state("sensor.cop_tekoaly_tilanne").attributes["esteet"]
    assert any(b["koodi"] == "mittaukset" and "sensor.eev_main_opening" in b["teksti"] for b in blockers)


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


async def test_binary_sensors_that_report_only_changes_do_not_block(hass, freezer, mocked_client, hardware):
    """1.1.1 regression: a template, ESPHome or MQTT binary sensor writes its state only when it changes,
    so a defrost flag that stays off for hours looked stale and every minute was rejected."""
    rig = await Rig(hass, freezer, hardware).start({"tila": "Automaattinen"})
    comp, dfr = rig.runner.sensors["kompressori"], rig.runner.sensors["sulatus"]
    rig.change_only = {comp, dfr}
    assert await rig.run(4 * 60, until=lambda r: r.hw.values[OD6] == 16)
    assert comp not in rig.runner.polled and dfr not in rig.runner.polled
    assert hass.states.get(dfr).last_reported.timestamp() < rig.runner.last_tick - STALE_S  # silent, still valid
    assert not rig.runner.invalid
    assert rig.state("sensor.cop_tekoaly_mittausten_tila").state == "Kunnossa"


async def test_polled_reading_that_stops_reporting_blocks_until_it_returns(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start(None)
    speed = rig.runner.sensors["nopeus"]
    await rig.run(3)
    assert speed in rig.runner.polled
    rig.frozen.add(speed)
    await rig.run(17)
    assert speed in rig.runner.invalid
    assert rig.runner._sample(rig.runner.last_tick).rps is None
    assert rig.state("sensor.cop_tekoaly_mittausten_tila").state == "Puutteellinen"
    assert not rig.runner.brain.points[-1].stable
    rig.frozen.clear()
    await rig.run(1)
    assert not rig.runner.invalid
    assert rig.state("sensor.cop_tekoaly_mittausten_tila").state == "Kunnossa"
    assert hardware.writes == 0


async def test_defrost_valve_stands_in_for_a_missing_defrost_sensor(hass, freezer, mocked_client, hardware):
    """The owner's Modbus defrost valve, binary_sensor.hp_m0_defrost_venttiili, is used when
    binary_sensor.hp_defrost_active does not exist."""
    rig = Rig(hass, freezer, hardware)
    rig.rename = {"binary_sensor.hp_defrost_active": "binary_sensor.hp_m0_defrost_venttiili"}
    await rig.start({"tila": "Automaattinen"})
    assert rig.runner.role_entity("sulatus") == "binary_sensor.hp_m0_defrost_venttiili"
    await rig.run(4 * 60)
    assert hardware.values[OD6] == 16
    assert not rig.runner.invalid
    assert "binary_sensor.hp_defrost_active" not in rig.runner.missing
    assert any(p.defrost for p in rig.runner.brain.points)  # defrosts are seen through the stand-in


async def test_no_defrost_signal_at_all_blocks_and_says_which(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start({
        "tila": "Automaattinen",
        "anturit": {"sulatus": "binary_sensor.ei_ole", "sulatus_varalla": "binary_sensor.ei_ole_kumpikaan"}})
    await rig.run(90)
    assert hardware.writes == 0
    assert "binary_sensor.ei_ole" in rig.runner.invalid
    assert rig.runner.missing.count("binary_sensor.ei_ole") == 1
    assert "binary_sensor.ei_ole_kumpikaan" not in rig.runner.missing  # the stand-in is not listed twice
    blockers = rig.state("sensor.cop_tekoaly_tilanne").attributes["esteet"]
    assert blockers[0]["koodi"] == "mittaukset" and blockers[0]["taso"] == "virhe"
    assert "binary_sensor.ei_ole" in blockers[0]["teksti"]


async def test_blockers_explain_why_nothing_is_written(hass, freezer, mocked_client, hardware):
    rig = await Rig(hass, freezer, hardware).start(None)
    await rig.run(5)
    attrs = rig.state("sensor.cop_tekoaly_tilanne").attributes
    codes = [b["koodi"] for b in attrs["esteet"]]
    assert codes[0] == "tarkkailu" and "tasainen" in codes
    waiting = next(b for b in attrs["esteet"] if b["koodi"] == "tasainen")
    assert "kompressori käynnistyi" in waiting["teksti"]  # the compressor relay changed at start-up
    assert attrs["tarvitaan_min"] == 27 and attrs["tasaisia_min"] == 0
    assert rig.state("sensor.cop_tekoaly_tilanne").state.startswith("Odottaa tasaista käyntiä (0/27 min): ")
    assert await rig.run(180, until=lambda r: r.runner.brain.suggestion is not None)
    codes = [b["koodi"] for b in rig.state("sensor.cop_tekoaly_tilanne").attributes["esteet"]]
    assert codes == ["tarkkailu"]  # only the observe mode keeps it from acting now


async def test_cycling_compressor_is_tuned_end_to_end(hass, freezer, mocked_client, hardware):
    """An oversized unit in mild weather runs 40 min and stops for 20: every run starts with 15 settling
    minutes, so no 45-minute window ever held 27 steady minutes and the AI never decided anything."""
    rig = await Rig(hass, freezer, hardware, cycles=(40, 20)).start({"tila": "Automaattinen"})
    accepted = lambda r: any(h["outcome"] == "hyväksytty" for h in r.runner.brain.history)  # noqa: E731
    assert await rig.run(10 * 60, until=accepted)
    assert hardware.values[OD6] == 16


async def test_mode_changed_in_configuration_yaml_is_applied(hass, freezer, mocked_client, hardware, hass_storage):
    hass_storage[DOMAIN] = {"version": 1, "key": DOMAIN, "data": {
        "mode": "Tarkkailu", "yaml_mode": None, "defrost": {"mode": "Tarkkailu", "yaml_mode": None}}}
    rig = await Rig(hass, freezer, hardware).start({"tila": "Automaattinen", "sulatus_tila": "Ehdota"})
    assert rig.state("select.cop_tekoaly_tila").state == "Automaattinen"
    assert rig.state("select.cop_tekoaly_sulatustila").state == "Ehdota"


async def test_upgrade_keeps_the_mode_chosen_in_the_ui(hass, freezer, mocked_client, hardware, hass_storage):
    """Storage from 1.1.1 has no yaml_mode: the mode picked in the UI must not be reset by the example
    configuration's tila: Tarkkailu."""
    hass_storage[DOMAIN] = {"version": 1, "key": DOMAIN, "data": {"mode": "Automaattinen"}}
    rig = await Rig(hass, freezer, hardware).start({"tila": "Tarkkailu"})
    assert rig.state("select.cop_tekoaly_tila").state == "Automaattinen"


async def test_card_is_served_and_finds_the_entities(hass, freezer, mocked_client, hardware, hass_client,
                                                     hass_ws_client):
    rig = await Rig(hass, freezer, hardware).start(None)
    assert CARD_URL in hass.data[frontend.DATA_EXTRA_MODULE_URL].urls
    client = await hass_client()
    resp = await client.get(CARD_URL)
    assert resp.status == 200
    js = await resp.text()
    assert 'registry.define("cop-tekoaly-card", CopTekoalyCard)' in js
    # defined only after the app has swapped in its scoped element registry (see the comment in the card)
    assert 'whenDefined("home-assistant").then(register)' in js
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "mango_cop_ai/entities"})
    msg = await ws.receive_json()
    assert msg["success"]
    entities = msg["result"]["entities"]
    assert sorted(entities.values()) == sorted(ENTITIES)
    assert entities["sensor.tilanne"] == "sensor.cop_tekoaly_tilanne"
    assert entities["button.palauta"] == "button.cop_tekoaly_palauta_alkuperaiset"
    assert rig.state("sensor.cop_tekoaly_tilanne").attributes["versio"] == msg["result"]["version"]


def test_example_configuration_is_valid():
    from custom_components.mango_cop_ai import CONFIG_SCHEMA

    conf = yaml.safe_load((EXAMPLES / "configuration.yaml").read_text(encoding="utf-8"))
    validated = CONFIG_SCHEMA(conf)[DOMAIN]
    assert validated["anturit"]["sulatus_varalla"] == "binary_sensor.hp_m0_defrost_venttiili"
    assert validated["tila"] == "Tarkkailu"
    card = yaml.safe_load((EXAMPLES / "kortti.yaml").read_text(encoding="utf-8"))
    assert card["cards"][0] == {"type": "custom:cop-tekoaly-card"}
