"""Defrost watch and control against the simulated heat pump, minute by minute, without Home Assistant."""

import json

from custom_components.mango_cop_ai.brain import Brain, Force, Param, Write
from custom_components.mango_cop_ai.const import (
    DFR_BANDS,
    DFR_COIL_END,
    DFR_COIL_START,
    DFR_DIFF,
    DFR_ENV,
    DFR_FROST_D1,
    DFR_FROST_D2,
    DFR_INTE,
    DFR_INTERVAL,
    DFR_SMART,
    DFR_SURE,
    DFR_TIME,
    DFR_TIMING,
    MODE_AUTO,
    MODE_OBSERVE,
    MODE_PROPOSE,
)

from .plant import (
    DEFROST_VALUES,
    DFR_BANDS_HW,
    DFR_COIL_END_HW,
    DFR_COIL_START_HW,
    DFR_DIFF_HW,
    DFR_ENV_HW,
    DFR_FROST_D1_HW,
    DFR_FROST_D2_HW,
    DFR_INTE_HW,
    DFR_INTERVAL_HW,
    DFR_SMART_HW,
    DFR_SURE_HW,
    DFR_TIME_HW,
    DFR_TIMING_HW,
    OWNER_VALUES,
    to_sample,
)
from .test_brain import BOUNDS, HW, Harness

DFR_HW = {
    DFR_INTERVAL: DFR_INTERVAL_HW, DFR_TIME: DFR_TIME_HW, DFR_ENV: DFR_ENV_HW, DFR_COIL_START: DFR_COIL_START_HW,
    DFR_COIL_END: DFR_COIL_END_HW, DFR_SMART: DFR_SMART_HW, DFR_FROST_D1: DFR_FROST_D1_HW,
    DFR_FROST_D2: DFR_FROST_D2_HW, DFR_SURE: DFR_SURE_HW, DFR_TIMING: DFR_TIMING_HW,
    **dict(zip(DFR_BANDS, DFR_BANDS_HW, strict=True)), **dict(zip(DFR_INTE, DFR_INTE_HW, strict=True)),
    **dict(zip(DFR_DIFF, DFR_DIFF_HW, strict=True)),
}
DFR_BOUNDS = {
    DFR_INTERVAL: (0, 255), DFR_TIME: (90, 1200), DFR_ENV: (0, 30), DFR_COIL_START: (-20, 10),
    DFR_COIL_END: (0, 30), DFR_SMART: (0, 1), DFR_FROST_D1: (-30, 30), DFR_FROST_D2: (-30, 30), DFR_SURE: (3, 30),
    DFR_TIMING: (0, 18), DFR_BANDS[0]: (-20, 15), **{k: (-20, 10) for k in DFR_BANDS[1:]},
    **{k: (0, 255) for k in DFR_INTE}, DFR_DIFF[0]: (0, 20), DFR_DIFF[1]: (0, 20),
    **{k: (-10, 20) for k in DFR_DIFF[2:]},
}
ALL_HW = {**HW, **DFR_HW}
ALL_BOUNDS = {**BOUNDS, **DFR_BOUNDS}
INTE = {i: DFR_INTE[i - 1] for i in range(1, 6)}


class DfrHarness(Harness):
    """Defrost decided by the simulated controller from the Mango defrost parameters."""

    def __init__(self, defrost_mode=MODE_AUTO, mode=MODE_OBSERVE, force_ok=True, values=None, stored=None,
                 frost_sensor=True, **plant):
        all_values = {**OWNER_VALUES, **DEFROST_VALUES, **(values or {})}
        plant.setdefault("defrost_ctrl", {"table": True})
        super().__init__(mode=mode, values=all_values, stored=stored, **plant)
        self.brain = Brain(self.settings, stored=stored, mode=mode, defrost_mode=defrost_mode)
        self.force_ok = force_ok
        self.frost_sensor = frost_sensor  # False: like the owner's system, no sensor.vilp_huurre_cop_havio
        self.forces = []

    def params(self):
        return {k: Param(self.values[hw], True, *ALL_BOUNDS[k]) for k, hw in ALL_HW.items()}

    def value(self, key):
        return self.values[ALL_HW[key]]

    def execute(self, cmds):
        for _ in range(30):
            self.collect(cmds)
            writes = [c for c in cmds if isinstance(c, Write)]
            forces = [c for c in cmds if isinstance(c, Force)]
            if not writes and not forces:
                return
            cmds = []
            for f in forces:
                self.forces.append((self.plant.minute, f.reason))
                if self.force_ok:
                    self.plant.force()
                cmds += self.brain.force_result(self.force_ok, None if self.force_ok else "ei skriptiä", self.t)
            for w in writes:
                lo, hi = ALL_BOUNDS[w.key]
                assert lo <= w.value <= hi
                self.values[ALL_HW[w.key]] = w.value
                self.writes.append((self.plant.minute, w.key, w.value, w.kind))
                cmds += self.brain.write_result(w, True, None, self.t, None)
            cmds += self.brain.followups(self.t, self.params())
        raise AssertionError("command loop did not settle")

    def run(self, minutes, until=None):
        for _ in range(minutes):
            phys = self.plant.step()
            sample = to_sample(phys, self.t, self.force_ok)
            if not self.frost_sensor:
                sample.frost = None
            cmds = self.brain.tick(self.t, sample, self.params())
            self.execute(cmds)
            self.t += 60
            if until and until(self):
                return True
        return False

    def command(self, cmds):
        """A user command, run like the runner does: its writes and the restores it queued."""
        self.execute(cmds + self.brain.followups(self.t, self.params()))

    @property
    def records(self):
        return self.brain.defrost.records


def flat(t):
    return lambda m: t


def test_frost_is_measured_and_every_defrost_is_judged():
    h = DfrHarness(defrost_mode=MODE_OBSERVE, force_ok=False, t_out=flat(-1.0), frost_rate=2.0)
    h.run(6 * 60)
    assert h.writes == [] and h.forces == []
    recs = h.records
    assert len(recs) >= 4
    assert recs[0]["verdict"] == "epäselvä"  # the first cycle started before the AI was running
    for r in recs[1:]:
        assert r["band"] == 3 and r["trigger"]["type"] == "väli" and r["trigger"]["key"] == INTE[3]
        assert 41 <= r["run"] <= 50
    true = {m: f for m, _, f in h.plant.starts}
    for r in recs[1:]:
        start_min = round((r["t"] - 1_760_000_000.0) / 60)
        assert abs(r["frost"] - true[start_min]) < 0.6, (r["frost"], true[start_min])
        assert r["verdict"] == "sopiva"
        assert 0.1 < r["e_def"] < 0.6
    assert any(m.startswith("Sulatus 3 min 00 s, käyntiä edellisestä") and "Laukaisi Dfr.Inte. 3 (40 min)" in m
               for _, m in h.logs)
    v = h.brain.defrost.view
    assert v["state"] in ("Puhdas", "Huurtuu", "Sulatus tarpeen", "Mittaa puhtaan kennon tasoa", "Sulatus käynnissä")


def test_dry_cold_defrosts_on_a_clean_coil_lengthen_the_interval_step_by_step():
    h = DfrHarness(t_out=flat(-12.0), frost_rate=0.3)
    h.run(26 * 60)
    assert {w[1] for w in h.writes} == {INTE[5]}  # only the band's own interval
    assert h.value(INTE[5]) >= 75
    assert all(h.value(INTE[i]) == DEFROST_VALUES[DFR_INTE_HW[i - 1]] for i in (1, 2, 3, 4))
    assert h.value(DFR_INTERVAL) == 45
    assert all(h.value(k) == v for k, v in ((k, OWNER_VALUES[hw]) for k, hw in HW.items()))
    accepted = [x for x in h.brain.history if x["type"] == "dfr_interval_up" and x["outcome"] == "hyväksytty"]
    assert len(accepted) >= 3
    assert h.brain.managed[INTE[5]] == h.value(INTE[5]) and h.brain.originals[INTE[5]] == 60
    later = [r for r in h.records if r["t"] > h.t - 4 * 3600]
    assert later and all(r["run"] >= 70 for r in later if r["run"] is not None)
    assert any(n[1] == "COP-tekoäly: sulatusväli muutettu" for n in h.notes)


def test_ai_defrosts_before_the_frost_gets_heavy():
    values = {DFR_INTE_HW[2]: 90}  # this unit would wait 90 min in band 3
    h = DfrHarness(values=values, t_out=flat(-1.0), frost_rate=4.0)
    h.run(8 * 60)
    forced = [r for r in h.records if r["trigger"]["type"] == "tekoäly"]
    assert len(forced) >= 4 and h.plant.forced == len(forced)
    assert all(r["verdict"] == "sopiva" for r in forced)
    assert all(3.0 <= f <= 5.5 for m, by, f in h.plant.starts if by == "force"), h.plant.starts
    assert not [r for r in h.records[2:] if r["verdict"] in ("myöhäinen", "kesken")]
    assert all(55 <= r["run"] <= 80 for r in forced)
    assert h.writes == []  # the controller's interval is not even touched
    assert any(m.startswith("Pakkosulatus: Kennossa on huurretta") for _, m in h.logs)


def test_without_the_script_late_defrosts_shorten_the_interval():
    values = {DFR_INTE_HW[2]: 90}
    h = DfrHarness(values=values, force_ok=False, t_out=flat(-1.0), frost_rate=5.0)
    h.run(20 * 60)
    assert h.forces == []
    assert 75 <= h.value(INTE[3]) <= 80  # never more than 15 min below the original
    down = [x for x in h.brain.history if x["type"] == "dfr_interval_down" and x["outcome"] == "hyväksytty"]
    assert len(down) >= 2
    assert any(r["verdict"] == "myöhäinen" for r in h.records)


def test_observe_mode_says_when_it_would_defrost_but_does_nothing():
    values = {DFR_INTE_HW[2]: 90}
    h = DfrHarness(defrost_mode=MODE_OBSERVE, values=values, t_out=flat(-1.0), frost_rate=4.0)
    h.run(6 * 60)
    assert h.forces == [] and h.writes == []
    said = [m for _, m in h.logs if m.startswith("Sulatus, tarkkailu: ei tehty mitään. Tekisin pakkosulatuksen")]
    assert 2 <= len(said) <= len(h.records)  # once per cycle, not every minute
    assert h.brain.defrost.view["need"] is not None


def test_propose_mode_defrosts_when_the_owner_approves():
    values = {DFR_INTE_HW[2]: 90}
    h = DfrHarness(defrost_mode=MODE_PROPOSE, values=values, t_out=flat(-1.0), frost_rate=4.0)
    assert h.run(5 * 60, until=lambda x: x.brain.pending is not None)
    assert h.brain.pending["type"] == "dfr_force" and h.forces == []
    assert any(n[1] == "COP-tekoälyn sulatusehdotus" for n in h.notes)
    h.command(h.brain.approve(h.t))
    assert len(h.forces) == 1
    h.run(3)
    assert h.plant.forced == 1 and h.brain.pending is None


def test_forced_defrost_that_does_not_start_is_retried_once_then_paused():
    values = {DFR_INTE_HW[2]: 90}
    h = DfrHarness(values=values, t_out=flat(-1.0), frost_rate=4.0, script_works=False)
    h.run(10 * 60)
    assert len(h.forces) == 2
    assert h.forces[1][0] - h.forces[0][0] >= 16  # 6 min to confirm + 10 min before the retry
    assert any(n[1] == "Pakkosulatus ei toimi" for n in h.notes)
    assert h.plant.forced == 0
    assert h.brain.defrost.force_block > h.t


def test_raised_interval_is_undone_when_a_defrost_turns_late():
    h = DfrHarness(force_ok=False, t_out=flat(-12.0), frost_rate=0.3)
    assert h.run(12 * 60, until=lambda x: x.brain.defrost.action is not None
                 and x.brain.defrost.action["phase"] == "verify")
    assert h.value(INTE[5]) == 65
    h.plant.frost_rate = 7.0  # wet snow: the coil frosts fast now
    assert h.run(4 * 60, until=lambda x: x.brain.defrost.action is None)
    assert h.value(INTE[5]) == 60
    last = h.brain.history[-1]
    assert last["type"] == "dfr_interval_up" and last["outcome"] == "haitta"
    assert any(n[1] == "COP-tekoäly: sulatusvälin muutos peruttiin" for n in h.notes)
    assert h.brain.backoff[INTE[5]] > h.t + 10 * 86400


def test_a_fan_change_or_a_sudden_step_is_not_taken_for_frost():
    h = DfrHarness(t_out=flat(-1.0), frost_rate=0.5)
    assert h.run(3 * 60, until=lambda x: x.brain.defrost.cycle["ref"] is not None)
    h.values[DFR_INTE_HW[2]] = 250  # no controller defrost during the rest of the test
    h.plant.fan_fn = lambda m: 420.0  # silent mode: the fan slows down, the approach grows 28 %
    h.run(60)
    assert h.brain.defrost.view["frost"] < 1.5
    h.plant.extra_appr = 3.0  # something else changes the approach at once
    h.run(30)
    assert h.brain.defrost.view["frost"] < 2.0
    assert h.forces == []
    assert h.brain.defrost.cycle["rebased"] >= 1


def test_defrost_state_survives_a_restart():
    values = {DFR_INTE_HW[2]: 90}
    h = DfrHarness(values=values, t_out=flat(-1.0), frost_rate=4.0)
    h.run(3 * 60)
    before = len(h.records)
    stored = json.loads(json.dumps(h.brain.to_dict()))
    h.brain = Brain(h.settings, stored=stored, mode=MODE_OBSERVE, defrost_mode=MODE_OBSERVE)
    assert h.brain.defrost.mode == MODE_AUTO  # the stored mode wins over the configured one
    assert len(h.records) == before and h.brain.defrost.cycle["start"] is not None
    h.run(4 * 60)
    assert len([r for r in h.records if r["trigger"]["type"] == "tekoäly"]) >= 2


def test_own_threshold_follows_what_the_melts_show():
    b = Brain(defrost_mode=MODE_AUTO)
    d = b.defrost
    b.now = 1_760_000_000.0
    out = []
    d._calibrate({"trigger": {"type": "tekoäly"}, "verdict": "turha", "force_ok": True, "need": 100}, out)
    assert d.target == 4.5 and "4,0 → 4,5 K" in out[-1].message
    d._calibrate({"trigger": {"type": "väli"}, "verdict": "myöhäinen", "force_ok": True, "need": 70}, out)
    assert d.target == 4.0
    d._calibrate({"trigger": {"type": "väli"}, "verdict": "myöhäinen", "force_ok": True, "need": 160}, out)
    assert d.target == 4.0  # the AI knew; something else stopped it
    for _ in range(20):
        d._calibrate({"trigger": {"type": "tekoäly"}, "verdict": "turha", "force_ok": True, "need": 100}, out)
    assert d.target == 8.0


def test_switching_defrost_control_off_during_a_trial_restores_the_interval():
    h = DfrHarness(force_ok=False, t_out=flat(-12.0), frost_rate=0.3)
    assert h.run(12 * 60, until=lambda x: x.brain.defrost.action is not None
                 and x.brain.defrost.action["phase"] == "verify")
    assert h.value(INTE[5]) == 65
    h.command(h.brain.set_defrost_mode("Pois", h.t))
    assert h.value(INTE[5]) == 60 and h.brain.defrost.action is None
    n = len(h.writes)
    h.run(6 * 60)
    assert len(h.writes) == n and h.forces == []
    assert h.brain.history[-1]["outcome"] == "keskeytetty"


def test_rollback_button_restores_defrost_intervals_too():
    h = DfrHarness(force_ok=False, t_out=flat(-12.0), frost_rate=0.3)
    h.run(14 * 60)
    assert h.value(INTE[5]) > 60
    h.command(h.brain.rollback_all(h.t, "käyttäjä"))
    assert h.value(INTE[5]) == 60
    assert h.brain.defrost.mode == MODE_OBSERVE and h.brain.mode == MODE_OBSERVE


def test_eev_tuning_and_defrost_control_work_side_by_side():
    h = DfrHarness(mode=MODE_AUTO, t_out=flat(-12.0), frost_rate=0.3)  # wet suction and clean-coil defrosts
    h.run(20 * 60)
    keys = {w[1] for w in h.writes if w[3] != "restore"}
    assert INTE[5] in keys and any(k.startswith("EEV Set|") for k in keys)
    assert h.value(INTE[5]) > 60
    assert any(k.startswith("EEV Set|Heat Min OD") for k in h.brain.managed)
    assert not h.brain.queue


def test_a_lasting_speed_change_keeps_the_frost_estimate_going():
    h = DfrHarness(t_out=flat(-1.0), frost_rate=3.0, boost=False)
    assert h.run(3 * 60, until=lambda x: x.brain.defrost.cycle["ref"] is not None)
    h.values[DFR_INTE_HW[2]] = 250  # the controller stays out of the way
    h.run(10)
    h.plant.base_rps = 36.0  # the load grows: the compressor runs 38 % faster from now on
    assert h.run(4 * 60, until=lambda x: x.plant.forced >= 1)  # still defrosts at the right time
    h.run(10)
    assert h.brain.defrost.records[-1]["trigger"]["type"] == "tekoäly"
    assert h.records[-1]["frost"] is not None
    forced = [f for m, by, f in h.plant.starts if by == "force"]
    assert 3.0 <= forced[0] <= 5.5, h.plant.starts


def test_a_flickering_defrost_signal_is_not_a_defrost():
    h = DfrHarness(defrost_mode=MODE_OBSERVE, t_out=flat(-1.0), frost_rate=1.0, values={DFR_INTE_HW[2]: 250})
    h.run(30)
    from custom_components.mango_cop_ai.analysis import Sample

    p = h.plant.step()
    s = to_sample(p, h.t)
    blip = Sample(**{**s.__dict__, "defrost": True, "defrost_changed": h.t - 20})
    h.execute(h.brain.tick(h.t, blip, h.params()))
    h.t += 60
    p = h.plant.step()
    s = to_sample(p, h.t)
    h.execute(h.brain.tick(h.t, Sample(**{**s.__dict__, "defrost": False, "defrost_changed": h.t - 25}), h.params()))
    assert h.records == []
    assert any("ohitettiin" in m for _, m in h.logs)


def test_the_frost_estimate_reaches_the_cop_diagnosis_when_there_is_no_frost_sensor():
    h = DfrHarness(defrost_mode=MODE_OBSERVE, force_ok=False, frost_sensor=False, t_out=flat(-1.0), frost_rate=6.0)
    assert h.run(2 * 60, until=lambda x: x.brain.defrost.cycle["ref"] is not None)
    h.values[DFR_INTE_HW[2]] = 250  # the controller does not defrost any more
    h.run(150)
    assert h.brain.points[-1].frost is not None and h.brain.points[-1].frost > 8
    assert h.brain.diagnosis["code"] in ("huurre", "hoyrystin", "tasaantuu")
    assert any(n[1] == "Kenno huurteessa" and "Tekoäly ei sulattanut: pakkosulatusskriptiä ei löydy" in n[2]
               for n in h.notes)
