"""Per-minute operating points, window statistics and learned zone baselines.

Nothing here talks to Home Assistant: the runner turns entity states into a Sample once a
minute and the brain keeps the derived Points in a rolling window.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import r290


@dataclass
class Settings:
    """Tunable limits. Defaults are deliberately conservative."""

    cop_basis: str = "kylmapiiri"  # or "vesipuoli": which COP the anomaly watch follows
    water_factor: float = 1.0  # multiply water-side heat by this (e.g. 0.62 if flow reads 1.6x)
    liquid_approach: float = 2.0  # K, liquid temperature = return water + this (as the kp sensor)
    eta_dry_default: float = 0.70  # apparent compressor efficiency with dry suction, until learned
    wet_margin: float = 0.15  # wet suction when the apparent efficiency is this much above the dry level
    continue_margin: float = 0.07  # after an accepted drying step, keep drying down to this margin
    dry_margin: float = 0.04  # comfortably dry when within this of the dry level
    window_min: int = 45  # minutes of steady running needed before deciding anything
    verify_min: int = 60  # steady minutes needed to judge a change
    writes_per_day: int = 12  # parameter writes in any 24 h, safety restores excluded
    discharge_max: float = 100.0  # °C, undo the change if the discharge gas gets hotter
    approach_max_rise: float = 6.0  # K, undo if outdoor - evaporating grows this much (starving)
    cop_drop_pct: float = 8.0  # undo an efficiency change if water-side quality drops this much
    anomaly_pct: float = 10.0  # warn when COP quality is this much below the learned level
    min_od_step: int = 2  # % per step for Heat Min OD
    min_od_floor: int = 8  # % absolute lowest Heat Min OD
    min_od_max_delta: int = 8  # % at most this far below the original value
    sv_step: int = 5  # raw (0.1 K) per step for Heat Sv
    sv_max_delta: int = 30  # raw, at most this far above the original value
    sv_abs_max: int = 60  # raw, never above this
    eev_full_steps: int | None = None  # EEV steps at 100 %; None = read M.EEV Max step (fallback 500)
    enable_min_od: bool = True
    enable_sv: bool = True


@dataclass
class Sample:
    """Raw readings for one minute. None = unavailable."""

    t: float
    p_el: float | None = None  # compressor electric power, kW
    rps: float | None = None
    te: float | None = None  # evaporating (saturation) temperature
    tc: float | None = None  # condensing (saturation) temperature
    t_suc: float | None = None
    t_dis: float | None = None
    eev: float | None = None  # main EEV opening, steps
    sh_act: float | None = None  # controller's measured superheat
    sh_tgt: float | None = None  # controller's superheat target
    t_out: float | None = None
    t_sup: float | None = None  # supply (flow) water
    t_ret: float | None = None  # return water
    q_w: float | None = None  # water-side heat, kW
    frost: float | None = None  # frost COP loss %, from the smart defrost sensor
    comp_on: bool = False
    defrost: bool = False
    fault: bool = False
    heater: bool = False
    comp_on_min: float | None = None  # minutes since the compressor relay turned on
    since_defrost_min: float | None = None  # minutes since the last defrost ended
    missing: tuple[str, ...] = ()


@dataclass
class Point:
    t: float
    stable: bool
    comp_on: bool
    defrost: bool
    fault: bool
    heater: bool
    rps: float | None
    p_el: float | None
    eev: float | None
    te: float | None
    tc: float | None
    t_out: float | None
    t_sup: float | None
    t_dis: float | None
    sh: float | None
    dsh: float | None
    sh_act: float | None
    sh_tgt: float | None
    eta: float | None
    cop_kp: float | None
    cop_w: float | None
    carnot: float | None
    lq_kp: float | None
    lq_w: float | None
    appr_e: float | None
    appr_c: float | None
    frost: float | None
    comp_on_min: float | None
    since_defrost_min: float | None
    missing: tuple[str, ...] = ()


CORE = ("p_el", "rps", "te", "tc", "t_suc", "t_dis", "eev", "t_out", "t_sup")


def _num(*values) -> bool:
    return all(isinstance(v, int | float) and math.isfinite(v) for v in values)


def median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def quantile(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def derive(s: Sample, settings: Settings, history) -> Point:
    """Turn a Sample into a Point. history = earlier Points, newest last."""
    cyc = None
    if _num(s.te, s.tc, s.t_suc, s.t_dis):
        cyc = r290.cycle(s.te, s.tc, s.t_suc, s.t_dis, s.t_ret, settings.liquid_approach)
    cop_w = None
    if _num(s.q_w, s.p_el) and s.p_el >= 0.2 and s.q_w > 0:
        cop_w = s.q_w * settings.water_factor / s.p_el
    carnot = None
    if _num(s.t_sup, s.t_out) and s.t_sup - s.t_out >= 5:
        carnot = (s.t_sup + 273.15) / (s.t_sup - s.t_out)
    cop_kp = cyc.cop if cyc is not None and 0.45 <= cyc.eta <= 1.3 else None

    steady = False
    if _num(s.rps):
        old = [p for p in history if p.t <= s.t - 240 and p.t >= s.t - 600 and p.rps is not None]
        recent = [p.rps for p in history if p.t >= s.t - 3600 and p.stable and p.rps is not None]
        base = median(recent)
        steady = bool(old) and abs(s.rps - old[-1].rps) <= 2.0 and (base is None or s.rps <= base + 5.0)
    stable = (
        s.comp_on and not s.defrost and not s.fault and steady
        and (s.comp_on_min or 0) >= 15
        and (s.since_defrost_min is None or s.since_defrost_min >= 20)
        and all(_num(getattr(s, name)) for name in CORE)
        and s.p_el >= 0.3 and s.rps >= 12
    )
    return Point(
        t=s.t, stable=stable, comp_on=s.comp_on, defrost=s.defrost, fault=s.fault, heater=s.heater,
        rps=s.rps, p_el=s.p_el, eev=s.eev, te=s.te, tc=s.tc, t_out=s.t_out, t_sup=s.t_sup,
        t_dis=s.t_dis,
        sh=(s.t_suc - s.te) if _num(s.t_suc, s.te) else None,
        dsh=(s.t_dis - s.tc) if _num(s.t_dis, s.tc) else None,
        sh_act=s.sh_act, sh_tgt=s.sh_tgt,
        eta=cyc.eta if cyc is not None else None,
        cop_kp=cop_kp, cop_w=cop_w, carnot=carnot,
        lq_kp=(cop_kp / carnot) if cop_kp is not None and carnot else None,
        lq_w=(cop_w / carnot) if cop_w is not None and carnot else None,
        appr_e=(s.t_out - s.te) if _num(s.t_out, s.te) else None,
        appr_c=(s.tc - s.t_sup) if _num(s.tc, s.t_sup) else None,
        frost=s.frost, comp_on_min=s.comp_on_min, since_defrost_min=s.since_defrost_min,
        missing=s.missing,
    )


EEV_TOLERANCE = 2.5  # steps: "at the floor" when this close to minimum opening x steps per %


def metrics(points, wet_eta: float, floors: dict[str, float] | None = None) -> dict:
    """Statistics over steady points. wet_eta = apparent efficiency that counts as wet suction,
    floors = {parameter key: minimum opening in steps}."""
    st = [p for p in points if p.stable and p.eta is not None]
    out: dict = {"n": len(st)}
    if not st:
        return out
    eta = [p.eta for p in st]
    eev = [p.eev for p in st if p.eev is not None]
    out.update(
        eta_med=median(eta),
        wet_frac=sum(e >= wet_eta for e in eta) / len(st),
        eev_med=median(eev),
        eev_spread=(quantile(eev, 0.9) - quantile(eev, 0.1)) if eev else None,
        lq_kp=median(p.lq_kp for p in st),
        lq_w=median(p.lq_w for p in st),
        appr_e=median(p.appr_e for p in st),
        appr_c=median(p.appr_c for p in st),
        t_dis_p95=quantile([p.t_dis for p in st], 0.95),
        sh_tgt=median(p.sh_tgt for p in st),
        sh_act=median(p.sh_act for p in st),
        t_out=median(p.t_out for p in st),
        t_sup=median(p.t_sup for p in st),
        frost=median(p.frost for p in st),
        dsh=median(p.dsh for p in st),
        te=median(p.te for p in st),
        tc=median(p.tc for p in st),
        rps=median(p.rps for p in st),
    )
    out["pinned_steps"], out["pinned_frac"] = None, 0.0
    if floors and eev:
        best, count = None, 0
        for steps in sorted(set(floors.values())):
            c = sum(1 for e in eev if abs(e - steps) <= EEV_TOLERANCE)
            if c > count:
                best, count = steps, c
        out["pinned_steps"], out["pinned_frac"] = best, count / len(eev)
    return out


class Baselines:
    """Exponentially weighted normal level per outdoor-temperature and speed bin."""

    FIELDS = ("lq_kp", "lq_w", "appr_e", "appr_c")

    def __init__(self, data: dict | None = None):
        self.bins: dict[str, dict] = data or {}

    @staticmethod
    def key(t_out: float, rps: float) -> str:
        band = int(math.floor(t_out / 2.0)) * 2
        speed = "<30" if rps < 30 else ("30-45" if rps <= 45 else ">45")
        return f"{band}|{speed}"

    def update(self, key: str, values: dict, alpha: float = 0.05) -> None:
        b = self.bins.setdefault(key, {"n": 0})
        b["n"] += 1
        a = max(alpha, 1.0 / b["n"])  # plain mean for the first updates, then slow
        for f in self.FIELDS:
            v = values.get(f)
            if v is None:
                continue
            m = b.get(f)
            b[f] = v if m is None else m + a * (v - m)

    def get(self, key: str, f: str, min_n: int = 4):
        b = self.bins.get(key)
        if not b or b.get("n", 0) < min_n:
            return None
        return b.get(f)


def hourly(points) -> dict:
    """Energy-weighted COP and median Carnot quality over steady points (normally the last 60 min)."""
    st = [p for p in points if p.stable and p.p_el is not None and p.p_el > 0]
    if not st:
        return {}
    kp = [p for p in st if p.cop_kp is not None]
    w = [p for p in st if p.cop_w is not None]
    e_kp, e_w = sum(p.p_el for p in kp), sum(p.p_el for p in w)
    return {
        "minutes": len(st),
        "cop_kp": (sum(p.cop_kp * p.p_el for p in kp) / e_kp) if e_kp > 0 else None,
        "cop_w": (sum(p.cop_w * p.p_el for p in w) / e_w) if e_w > 0 else None,
        "carnot": median(p.carnot for p in st),
        "lq_kp": median(p.lq_kp for p in st),
        "lq_w": median(p.lq_w for p in st),
    }
