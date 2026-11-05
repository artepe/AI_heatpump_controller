"""Regression checks for live Mango R290 telemetry and safe EEV decisions."""

from custom_components.mango_cop_ai import r290
from custom_components.mango_cop_ai.analysis import Sample, Settings, derive, measurement_issues

from .plant import OWNER_VALUES
from .test_brain import Harness


def live_snapshot(**overrides):
    """Status Overview: running 19.8 rps, target SH 0.2 K, actual SH 6.8 K."""
    data = dict(
        t=1_800_000_000.0, comp_on=True, comp_on_min=60.0, defrost=False,
        p_el=0.83, rps=19.8, te=4.9, tc=46.8, t_suc=11.7, t_dis=41.5,
        eev=325.0, sh_act=6.8, sh_tgt=0.2, t_out=5.6,
        t_sup=55.4, t_ret=55.0, flow=2083.0, q_w=7.99,
    )
    data.update(overrides)
    return Sample(**data)


def test_mango_snapshot_sh_matches_but_other_independent_sensors_disagree():
    sample = live_snapshot()
    point = derive(sample, Settings(), [])
    assert round(point.sh, 1) == 6.8
    assert point.sh_tgt == 0.2
    assert point.why == "ristiriita"
    assert not point.stable
    assert any("Kuumakaasu" in issue for issue in point.quality)
    assert any("Menovesi" in issue for issue in point.quality)
    assert any("lämpöteho" in issue for issue in point.quality)


def test_plausible_heating_sample_has_no_measurement_conflicts():
    sample = live_snapshot(tc=59.0, t_dis=72.0, q_w=0.97)
    assert measurement_issues(sample) == ()


def test_startup_data_not_misclassified_as_steady_failure():
    sample = live_snapshot(comp_on_min=3.0)
    assert measurement_issues(sample) == ()


def test_inconsistent_measurements_stop_wet_suction_autotuning():
    h = Harness()
    original = h.plant.step

    def contradictory():
        p = original()
        if p["minute"] >= 10 and p["comp_on"] and not p["defrost"]:
            p["t_dis"] = p["tc"] - 5.3
            p["t_sup"] = p["tc"] + 8.6
        return p

    h.plant.step = contradictory
    h.run(250)
    assert not h.writes
    assert any(p.quality for p in h.brain.points)
    assert h.brain.diagnosis["code"] in ("mittausristiriita", "sulatus", "tasaantuu", "seis")


def test_positive_suction_superheat_overrules_apparent_wetness():
    values = dict(OWNER_VALUES)
    for key in [f"5:{36 + i}" for i in range(9)]:
        values[key] = 10
    h = Harness(values=values, sh_base=4.0, defrost_every=999, boost=False)
    original = h.plant.step

    def apparent_efficiency_bias():
        p = original()
        if p["comp_on"] and not p["defrost"]:
            dry_sh = max(0.0, p["t_suc"] - p["te"])
            dsh = r290.dsh_for_eta(p["te"], p["tc"], dry_sh, 0.92)
            if dsh is not None:
                p["t_dis"] = p["tc"] + dsh
        return p

    h.plant.step = apparent_efficiency_bias
    h.run(240)
    assert not h.writes
    assert h.brain.diagnosis["code"] not in ("marka_imu", "marka_imu_minimi")
