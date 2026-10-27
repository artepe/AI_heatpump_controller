"""The decision core against the simulated heat pump, minute by minute, without Home Assistant."""

import json
import re

from custom_components.mango_cop_ai.analysis import Settings
from custom_components.mango_cop_ai.brain import Brain, Dismiss, Log, Notify, Param, Write
from custom_components.mango_cop_ai.const import (
    EEV_MAX_STEP,
    MIN_OD,
    MODE_AUTO,
    MODE_OBSERVE,
    MODE_PROPOSE,
    SV,
)

from .plant import EEV_MAX_STEP_HW, MIN_OD_HW, OWNER_VALUES, SV_HW, Plant, to_sample

HW = {**dict(zip(MIN_OD, MIN_OD_HW, strict=True)), **dict(zip(SV, SV_HW, strict=True)),
      EEV_MAX_STEP: EEV_MAX_STEP_HW}
BOUNDS = {**{k: (0, 100) for k in MIN_OD}, **{k: (-100, 300) for k in SV}, EEV_MAX_STEP: (20, 9000)}
OD = {i: MIN_OD[i - 1] for i in range(1, 10)}
SVK = {i: SV[i - 1] for i in range(1, 10)}


class Harness:
    def __init__(self, mode=MODE_AUTO, settings=None, values=None, fail_keys=(), stored=None, **plant):
        self.values = dict(OWNER_VALUES) if values is None else values
        self.plant = Plant(self.values, **plant)
        self.settings = settings or Settings()
        self.brain = Brain(self.settings, stored=stored, mode=mode)
        self.t = 1_760_000_000.0
        self.writes, self.logs, self.notes = [], [], []
        self.fail_keys = set(fail_keys)

    def params(self):
        return {k: Param(self.values[hw], True, *BOUNDS[k]) for k, hw in HW.items()}

    def value(self, key):
        return self.values[HW[key]]

    def collect(self, cmds):
        for c in cmds:
            if isinstance(c, Log):
                self.logs.append((self.plant.minute, c.message))
            elif isinstance(c, Notify):
                self.notes.append((self.plant.minute, c.title, c.message))
            elif isinstance(c, Dismiss):
                self.notes.append((self.plant.minute, "dismiss", c.tag))

    def execute(self, cmds):
        for _ in range(30):
            self.collect(cmds)
            writes = [c for c in cmds if isinstance(c, Write)]
            if not writes:
                return
            cmds = []
            for w in writes:
                ok = w.key not in self.fail_keys
                lo, hi = BOUNDS[w.key]
                assert lo <= w.value <= hi
                if ok:
                    self.values[HW[w.key]] = w.value
                self.writes.append((self.plant.minute, w.key, w.value, w.kind))
                actual = None if ok else self.values[HW[w.key]]
                cmds += self.brain.write_result(w, ok, None if ok else "laite hylkäsi", self.t, actual)
            cmds += self.brain.followups(self.t, self.params())
        raise AssertionError("write loop did not settle")

    def run(self, minutes, until=None):
        for _ in range(minutes):
            phys = self.plant.step()
            cmds = self.brain.tick(self.t, to_sample(phys, self.t), self.params())
            self.execute(cmds)
            self.t += 60
            if until and until(self):
                return True
        return False

    def outcomes(self):
        return [(h["key"].split("|")[1] if h["key"] else None, h["outcome"]) for h in self.brain.history]


def eta_now(h, n=15):
    pts = [p.eta for p in h.brain.points if p.stable and p.eta is not None][-n:]
    return sorted(pts)[len(pts) // 2]


def zeta_mean(h, minutes=60):
    pts = [p for p in h.plant.log[-minutes:] if p["comp_on"] and not p["defrost"]]
    return sum(p["zeta"] for p in pts) / len(pts)


def test_observe_mode_never_writes_but_says_what_it_would_do():
    h = Harness(mode=MODE_OBSERVE)
    h.run(6 * 60)
    assert h.writes == []
    said = [m for _, m in h.logs if m.startswith("Tarkkailu")]
    assert said and "Heat Min OD 18 → 16" in said[0] and "Heat Min OD6, Heat Min OD5, Heat Min OD7" in said[0]
    assert len(said) <= 2  # not spammed every minute
    assert h.brain.diagnosis["code"] in ("marka_imu_minimi", "tasaantuu", "sulatus")
    assert "Tekisin" in h.brain.status_text or h.brain.diagnosis["code"] != "ok"


def test_auto_finds_the_active_zone_dries_the_suction_and_touches_nothing_else():
    h = Harness()
    h.run(20)
    wet_before = zeta_mean(h, 20)
    assert wet_before < -3  # pinned at 90 steps -> ~4 % liquid
    h.run(10 * 60)
    # zone 6 (-6..-2 °C) is the first guess for -3.5 °C
    assert h.writes[0][1:] == (OD[6], 16, "probe"), h.writes
    assert h.value(OD[5]) == 18 and h.value(OD[6]) <= 16
    assert all(h.value(OD[i]) == v for i, v in ((1, 25), (4, 25), (7, 18), (8, 18), (9, 18)))
    assert all(h.value(k) == OWNER_VALUES[SV_HW[i]] for i, k in enumerate(SV))
    assert ("Heat Min OD6", "hyväksytty") in h.outcomes()
    assert zeta_mean(h, 60) > wet_before + 1.5
    assert any(t == "COP-tekoäly: muutos jää voimaan" for _, t, _ in h.notes)
    assert h.brain.managed == {OD[6]: h.value(OD[6])} and h.brain.originals[OD[6]] == 18
    assert len(h.writes) <= 3


def test_wrong_first_guess_is_tried_away_and_remembered():
    # this unit's zone 7 covers -6..-2 °C: the first guess (OD6) does nothing and is restored
    h = Harness(zone_edges=[15, 10, 6, 2, 0, -2, -6, -10])
    h.run(10 * 60)
    assert [w[1:] for w in h.writes[:5]] == [(OD[6], 16, "probe"), (OD[6], 18, "revert"), (OD[5], 16, "probe"),
                                             (OD[5], 18, "revert"), (OD[7], 16, "probe")]
    assert h.value(OD[6]) == 18 and h.value(OD[7]) == 16
    assert any(m["key"] == OD[6] for m in h.brain.misses)
    assert any(x["key"] == OD[7] for x in h.brain.hits)


def test_learned_zone_is_tried_first_after_restart_and_state_survives_json():
    h = Harness()
    h.run(10 * 60)
    stored = json.loads(json.dumps(h.brain.to_dict()))
    stored["misses"] = []  # only the learned hit may steer the order
    h.brain = Brain(h.settings, stored=stored, mode=MODE_AUTO)  # Home Assistant restart
    h.collect(h.brain.rollback_all(h.t, "käyttäjä"))  # owner restores the original values
    h.run(5)
    assert h.value(OD[6]) == 18 and h.brain.mode == MODE_OBSERVE
    h.collect(h.brain.set_mode(MODE_AUTO, h.t))
    n = len(h.writes)
    h.run(3 * 60)
    probes = [w for w in h.writes[n:] if w[3] == "probe"]
    assert probes and probes[0][1] == OD[6]


def test_fault_restores_every_changed_value_and_pauses():
    h = Harness()
    h.run(10 * 60)
    assert h.value(OD[6]) < 18
    h.plant.fault = True
    h.run(5)
    assert h.value(OD[6]) == 18
    assert h.brain.managed == {}
    assert h.writes[-1][3] == "restore"
    assert "Tauko" in h.brain.status_text or h.brain.paused_until > h.t
    n = len(h.writes)
    h.plant.fault = False
    h.run(6 * 60)
    assert len(h.writes) == n  # 24 h pause


def test_value_changed_elsewhere_is_adopted_not_overwritten():
    h = Harness()
    h.run(10 * 60)
    assert OD[6] in h.brain.managed
    n = len(h.writes)
    h.values[HW[OD[6]]] = 17  # owner edits it in Mango Control
    h.run(120)
    assert h.value(OD[6]) == 17
    assert len(h.writes) == n
    assert h.brain.originals[OD[6]] == 17 and OD[6] not in h.brain.managed
    assert any("muutettiin muualta" in m for _, m in h.logs)


def test_write_budget_is_respected():
    h = Harness(settings=Settings(writes_per_day=3))
    h.run(12 * 60)
    normal = [w for w in h.writes if w[3] != "restore"]
    assert len(normal) <= 3
    assert h.value(OD[5]) == 18  # a probe is never left behind


def test_device_refusing_the_active_zone_leaves_everything_as_it_was():
    h = Harness(fail_keys={OD[6]})
    h.run(10 * 60)
    assert OD[6] in h.brain.failed
    assert all(h.values[hw] == v for hw, v in OWNER_VALUES.items())
    assert ("Heat Min OD9", "ei_tunnistettu") in h.outcomes()
    n = len(h.writes)
    h.run(10 * 60)
    assert len(h.writes) == n  # no new round of trials the same day


def test_restart_during_probe_restores_the_candidate():
    h = Harness()
    assert h.run(5 * 60, until=lambda x: x.brain.action and x.brain.action["phase"] == "probe_wait")
    key = h.brain.action["write_key"]
    assert h.value(key) == 16
    stored = json.loads(json.dumps(h.brain.to_dict()))
    h.brain = Brain(h.settings, stored=stored, mode=MODE_AUTO)
    h.run(2)
    assert h.value(key) == 18
    assert h.brain.action is None and h.brain.managed == {}


def test_defrost_during_probe_aborts_and_restores():
    h = Harness(defrost_every=0)
    assert h.run(5 * 60, until=lambda x: x.brain.action and x.brain.action["phase"] == "probe_wait")
    key = h.brain.action["write_key"]
    h.plant.defrost_until = h.plant.minute + 6
    h.run(3)
    assert h.value(key) == 18 and h.brain.action is None
    assert h.outcomes()[-1][1] == "keskeytetty"


def test_dry_unit_is_left_alone():
    values = dict(OWNER_VALUES)
    for hw in MIN_OD_HW:
        values[hw] = 10
    h = Harness(values=values)
    h.run(8 * 60)
    assert h.writes == []
    assert h.brain.diagnosis["code"] in ("ok", "tasaantuu", "sulatus")


def test_superheat_target_raised_when_wet_while_regulating_and_lowered_when_dry():
    values = dict(OWNER_VALUES)
    for hw in MIN_OD_HW:
        values[hw] = 10  # valve free to regulate
    h = Harness(values=values, sh_bias=4.0)  # controller reads superheat 4 K too high -> wet
    h.run(40)
    assert eta_now(h) >= 0.85 and h.brain.diagnosis["code"] in ("marka_imu", "tasaantuu")
    h.run(12 * 60)
    assert h.value(SVK[6]) >= 17, h.writes
    assert any(o == ("Heat Sv6", "hyväksytty") for o in h.outcomes())
    assert all(h.value(SVK[i]) == OWNER_VALUES[SV_HW[i - 1]] for i in (1, 2, 3, 4, 5, 7, 8, 9))
    assert eta_now(h) < 0.78
    raised = h.value(SVK[6])
    h.plant.sh_bias = 0.0  # sensor fixed: now the raised target only costs efficiency
    h.run(10 * 60)
    assert h.value(SVK[6]) < raised
    assert h.value(SVK[6]) >= 2


def test_lowered_minimum_is_raised_back_if_the_valve_starts_hunting():
    h = Harness(hunt_below=85)
    h.run(10 * 60)
    low = h.value(OD[6])
    assert low == 16
    h.plant.hunting = 30  # valve starts bouncing on the 80-step floor
    h.run(8 * 60)
    assert h.value(OD[6]) == 18
    assert h.outcomes()[-1] == ("Heat Min OD6", "hyväksytty")
    assert h.brain.limits[OD[6]] == 18
    n = len(h.writes)
    h.run(10 * 60)  # wet again at 18, but the hunting floor is remembered
    assert len(h.writes) == n


def test_propose_mode_waits_for_approval():
    h = Harness(mode=MODE_PROPOSE)
    h.run(3 * 60)
    assert h.writes == []
    assert h.brain.pending and any(t == "COP-tekoälyn ehdotus" for _, t, _ in h.notes)
    h.collect(h.brain.approve(h.t))
    h.run(120)
    assert h.writes and h.writes[0][1] == OD[6]
    assert h.brain.pending is None


def test_cop_drop_is_noticed_and_explained():
    values = dict(OWNER_VALUES)
    for hw in MIN_OD_HW:
        values[hw] = 10
    h = Harness(values=values, mode=MODE_OBSERVE, defrost_every=0, boost=False, frost_rate=0.0,
                t_out=lambda m: -3.0)
    h.run(8 * 60)
    assert not [n for n in h.notes if n[1] == "COP normaalia matalampi"]
    h.plant.extra_appr = 8.0  # e.g. fan failure or blocked coil
    h.run(3 * 60)
    warn = [n for n in h.notes if n[1] == "COP normaalia matalampi"]
    assert warn and "Höyrystimen lähestymä" in warn[0][2]
    assert h.brain.cop["deviation_pct"] < -10


def test_failed_write_with_unknown_result_is_restored_once_the_parameter_is_back():
    h = Harness()
    assert h.run(5 * 60, until=lambda x: x.brain.action and x.brain.action["phase"] == "probe_wait")
    key = h.brain.action["write_key"]
    h.brain.rollback_all(h.t, "testi")  # queue a restore ...
    h.brain.queue[0]["inflight"] = False
    real_params = h.params
    h.params = lambda: {**real_params(), key: Param(None, False)}  # ... while Mango is offline
    h.run(30)
    assert h.value(key) == 16 and h.brain.queue  # waits, does not burn its retries
    h.params = real_params
    h.run(2)
    assert h.value(key) == 18 and not h.brain.queue


def test_write_that_never_reports_back_is_undone():
    h = Harness()
    h.run(40)
    original = h.execute

    def lose_result(cmds):  # the runner crashes in the middle of a write
        for c in cmds:
            if isinstance(c, Write):
                h.values[HW[c.key]] = c.value
                h.writes.append((h.plant.minute, c.key, c.value, c.kind))
                h.execute = original
                return
        original(cmds)

    h.execute = lose_result
    assert h.run(60, until=lambda x: x.execute is original)
    key = h.writes[-1][1]
    h.run(15)
    assert h.value(key) == 18 and h.brain.action is None
    assert h.outcomes()[-1][1] == "keskeytetty"


def test_dry_level_is_learned_from_clearly_superheated_suction():
    values = dict(OWNER_VALUES)
    for hw in MIN_OD_HW:
        values[hw] = 10
    h = Harness(values=values, mode=MODE_OBSERVE, sh_base=4.0)  # controller keeps ~4 K superheat
    assert h.brain.eta_ref == 0.70
    h.run(6 * 60)
    assert h.brain.eta_dry["n"] >= 60
    assert abs(h.brain.eta_ref - 0.68) < 0.02  # the simulated compressor's real efficiency
    assert h.writes == []


def test_short_compressor_runs_still_lead_to_a_decision():
    """35 min on / 25 min off: each run has at most 20 steady minutes after its 15 settling minutes, so a
    single 45-minute window never holds the 27 needed. The steady minutes of earlier runs in the same
    conditions are pooled."""
    h = Harness(cycles=(35, 25))
    assert h.run(18 * 60, until=lambda h: ("Heat Min OD6", "hyväksytty") in h.outcomes())
    assert h.writes[0][1:] == (OD[6], 16, "probe")
    assert h.value(OD[6]) == 16 and all(h.value(OD[i]) == v for i, v in ((5, 18), (7, 18)))


def test_waiting_status_says_why_the_minute_is_not_steady():
    h = Harness(mode=MODE_OBSERVE, cycles=(35, 25))  # runs start at minutes 60, 120, ...
    h.run(70)
    assert h.brain.points[-1].why == "kaynnistys"
    assert re.fullmatch(r"Odottaa tasaista käyntiä \(\d+/27 min\): kompressori käynnistyi 9 min sitten, "
                        r"tasaantuu 15 min", h.brain.status_text), h.brain.status_text
    h.run(30)  # minute 99: stopped at 95
    assert h.brain.points[-1].why == "seis"
    assert h.brain.status_text.endswith(": kompressori seis")
    whys = {p.why for p in h.brain.points}
    assert {"historia", "kaynnistys", "seis", "buusti", None} <= whys


def test_mode_from_configuration_yaml_wins_only_when_it_was_changed_there():
    b = Brain()
    assert b.mode == MODE_OBSERVE and b.defrost.mode == MODE_OBSERVE
    b.set_mode(MODE_AUTO, 0.0)
    st = json.loads(json.dumps(b.to_dict()))
    assert Brain(stored=st).mode == MODE_AUTO  # the mode picked in the UI survives a restart
    b = Brain(stored=st, mode=MODE_PROPOSE)  # tila: Ehdota written in configuration.yaml
    assert b.mode == MODE_PROPOSE
    b.set_mode(MODE_AUTO, 0.0)
    st = json.loads(json.dumps(b.to_dict()))
    assert Brain(stored=st, mode=MODE_PROPOSE).mode == MODE_AUTO  # unchanged yaml: the UI choice stays
    legacy = {"mode": MODE_AUTO, "defrost": {"mode": MODE_AUTO}}  # storage written by 1.1.1
    b = Brain(stored=legacy, mode=MODE_OBSERVE, defrost_mode=MODE_OBSERVE)
    assert (b.mode, b.defrost.mode) == (MODE_AUTO, MODE_AUTO)
