"""Regression guards for refrigerant and Home Assistant AI decisions."""

from custom_components.mango_cop_ai import r290
from custom_components.mango_cop_ai.analysis import Baselines, Settings
from custom_components.mango_cop_ai.brain import Brain, Param
from custom_components.mango_cop_ai.const import MIN_OD, MODE_AUTO


def test_production_requires_verified_total_power():
    assert Settings().power_meter_confirmed is False
    assert Settings().require_water_cop is True
    assert Settings().sh_guard_k > 0


def test_untrusted_mango_parameter_cannot_be_probed():
    b = Brain(Settings(), mode=MODE_AUTO)
    b.now = 1_760_000_000.0
    b.params = {MIN_OD[0]: Param(18, True, 0, 100, trusted=False)}
    assert b._limits(MIN_OD[0], -1) is None


def test_suspected_wet_vapour_is_not_fit_as_dry_inlet():
    assert r290.cycle(5.1, 56.1, 4.7, 68.6, 51.7) is None


def test_baselines_keep_supply_temperature_separate():
    assert Baselines.key(5.8, 25, 55) != Baselines.key(5.8, 25, 45)


def test_change_cannot_pass_without_confirmed_positive_superheat():
    b = Brain(Settings(power_meter_confirmed=True), mode=MODE_AUTO)
    b.now = 1_760_000_000.0
    b.points.clear()
    proposal = {"type": "min_od_down", "settle_until": b.now}
    original = {"wet_frac": 1.0, "eta_med": 1.0, "lq_w": 0.60}
    after = {"n": 40, "wet_frac": 0.0, "eta_med": 0.65, "lq_w": 0.65}
    accepted, reason = b._judge(proposal, original, after)
    assert not accepted and "tulistus" in reason


def test_water_cop_drop_rejects_even_when_drying_metric_improves():
    b = Brain(Settings(power_meter_confirmed=True, sh_guard_k=0.0), mode=MODE_AUTO)
    proposal = {"type": "min_od_down", "settle_until": 0.0}
    original = {"wet_frac": 1.0, "eta_med": 1.0, "lq_w": 0.60}
    after = {"n": 40, "wet_frac": 0.1, "eta_med": 0.65, "lq_w": 0.55}
    accepted, reason = b._judge(proposal, original, after)
    assert not accepted and "COP" in reason
