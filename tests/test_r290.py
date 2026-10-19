"""The R290 fits against CoolProp, and the wet-suction signature used by the AI."""

import random

import pytest

from custom_components.mango_cop_ai import r290

CP = pytest.importorskip("CoolProp.CoolProp")
F, K = "Propane", 273.15


def p_sat(t):
    return CP.PropsSI("P", "T", t + K, "Q", 1, F)


def h_vap(te, sh):
    if sh <= 0:
        return CP.PropsSI("H", "T", te + K, "Q", 1, F) / 1e3
    return CP.PropsSI("H", "P", p_sat(te), "T", te + sh + K, F) / 1e3


def test_enthalpy_fits_match_coolprop():
    rng = random.Random(4)
    worst = {"h1": 0.0, "h2": 0.0, "h3": 0.0, "dhs": 0.0}
    for _ in range(400):
        te, sh = rng.uniform(-30, 15), rng.uniform(0, 15)
        tc = rng.uniform(max(te + 12, 20), 66)
        dsh = rng.uniform(2, 60)
        tl = rng.uniform(0, 65)
        worst["h1"] = max(worst["h1"], abs(r290.h1(te, sh) - h_vap(te, sh)))
        ref_h2 = CP.PropsSI("H", "P", p_sat(tc), "T", tc + dsh + K, F) / 1e3
        worst["h2"] = max(worst["h2"], abs(r290.h2(tc, dsh) - ref_h2))
        ref_h3 = CP.PropsSI("H", "T", tl + K, "Q", 0, F) / 1e3
        worst["h3"] = max(worst["h3"], abs(r290.h3(tl) - ref_h3))
        s1 = (CP.PropsSI("S", "T", te + K, "Q", 1, F) if sh <= 0
              else CP.PropsSI("S", "P", p_sat(te), "T", te + sh + K, F))
        ref_dhs = CP.PropsSI("H", "P", p_sat(tc), "S", s1, F) / 1e3 - h_vap(te, sh)
        worst["dhs"] = max(worst["dhs"], abs(r290.dhs(te, sh, tc) - ref_dhs))
    assert worst["h1"] < 0.1 and worst["h3"] < 0.1 and worst["dhs"] < 0.1, worst
    assert worst["h2"] < 0.25, worst


def test_dry_compressor_reads_plausible_and_wet_suction_reads_implausible():
    # 19.10. night: Te -3.5, Tc 58.5, suction at saturation, DSH 7 K -> apparent efficiency ~1
    wet = r290.cycle(-3.5, 58.5, -3.5, 58.5 + 7.0, 52.0)
    assert wet.eta > 0.95
    # the same point with a dry 70 % compressor and 5 K superheat
    dsh_dry = r290.dsh_for_eta(-3.5, 58.5, 5.0, 0.70)
    assert 23 < dsh_dry < 26
    dry = r290.cycle(-3.5, 58.5, 1.5, 58.5 + dsh_dry, 52.0)
    assert abs(dry.eta - 0.70) < 0.01
    assert 2.5 < dry.cop < 3.6
    # COP from the energy balance equals CoolProp's for the same state points
    h1, h2 = h_vap(-3.5, 5.0), CP.PropsSI("H", "P", p_sat(58.5), "T", 58.5 + dsh_dry + K, F) / 1e3
    h3 = CP.PropsSI("H", "T", 54.0 + K, "Q", 0, F) / 1e3
    assert abs(dry.cop - (h2 - h3) / (h2 - h1)) < 0.01


def test_out_of_range_returns_none():
    assert r290.cycle(-3, 58, -3, 58.5, 50) is None  # DSH 0.5 K below fit range
    assert r290.cycle(10, 18, 12, 40, 15) is None  # lift below 10 K
    assert r290.dsh_for_eta(-3, 58, 0, 0) is None
