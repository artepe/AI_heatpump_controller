"""Simulated R290 monoblock for tests.

Hidden from the AI: which of the 9 EEV zones is active (ambient bands below), the opening the
evaporator needs, and the real heat output. The EEV regulates superheat to its target and is
clamped to Heat Min OD x 5 steps; when the needed opening is below that clamp the evaporator is
overfed and the suction turns wet, which lowers the discharge superheat as on the real unit
(19.10.: EEV at 90 steps, DSH ~7 K, about 4-6 % liquid).

Defrost: by default a fixed schedule (defrost_every). With defrost_ctrl the unit decides like a
typical controller from the Mango defrost parameters: compressor minutes since the last defrost
>= the band's Dfr.Inte. n (or Dfr.Interval), coil <= Dfr. Coil T, ambient <= Dfr. Env.T, held for
T. Sure Time. The melt takes 1.5 min + 0.7 min per K of frost, capped at Dfr.Time; a capped melt
leaves frost behind. force() is the owner's forced defrost script.
"""

from __future__ import annotations

import math
import random

from custom_components.mango_cop_ai import r290

# ambient band edges, warm -> cold: zone 1 above +15 °C ... zone 9 below -15 °C
ZONE_EDGES = [15, 10, 6, 2, -2, -6, -10, -15]
MIN_OD_HW = [f"5:{36 + i}" for i in range(9)]
SV_HW = [f"5:{18 + i}" for i in range(9)]
EEV_MAX_STEP_HW = "4:3"
# defrost section, group 7
DFR_INTERVAL_HW, DFR_TIME_HW, DFR_ENV_HW, DFR_COIL_START_HW, DFR_COIL_END_HW = "7:0", "7:1", "7:3", "7:4", "7:5"
DFR_SMART_HW, DFR_FROST_D1_HW, DFR_FROST_D2_HW, DFR_SURE_HW, DFR_TIMING_HW = "7:7", "7:8", "7:9", "7:10", "7:29"
DFR_BANDS_HW = [f"7:{11 + i}" for i in range(4)]
DFR_INTE_HW = [f"7:{15 + i}" for i in range(5)]
DFR_DIFF_HW = [f"7:{20 + i}" for i in range(5)]
DEFROST_VALUES = {
    DFR_INTERVAL_HW: 45, DFR_TIME_HW: 480, DFR_ENV_HW: 15, DFR_COIL_START_HW: -3, DFR_COIL_END_HW: 12,
    DFR_SMART_HW: 0, DFR_FROST_D1_HW: 3, DFR_FROST_D2_HW: 6, DFR_SURE_HW: 3, DFR_TIMING_HW: 0,
    **dict(zip(DFR_BANDS_HW, [5, 0, -5, -10], strict=True)),  # bands: >5, 0..5, -5..0, -10..-5, <=-10
    **dict(zip(DFR_INTE_HW, [60, 45, 40, 50, 60], strict=True)),
    **dict(zip(DFR_DIFF_HW, [8, 7, 6, 4, 2], strict=True)),
}
OWNER_VALUES = {
    **dict(zip(MIN_OD_HW, [25, 25, 25, 25, 18, 18, 18, 18, 18], strict=True)),
    **dict(zip(SV_HW, [0, 0, 0, 2, 2, 2, 5, 5, 5], strict=True)),
    EEV_MAX_STEP_HW: 500,
}


def zone_of(t_out: float, edges=ZONE_EDGES) -> int:
    for i, edge in enumerate(edges):
        if t_out > edge:
            return i + 1
    return 9


class Plant:
    def __init__(self, values: dict, seed: int = 1, t_out=None, sh_bias: float = 0.0,
                 defrost_every: int = 180, boost: bool = True, cycles: tuple | None = None,
                 water_bias: float = 1.6, sh_base: float = 0.3, flow_scale: float = 1.0,
                 frost_rate=0.5, hunt_below: float = 75.0, zone_edges=ZONE_EDGES,
                 defrost_ctrl: dict | None = None, fan_fn=None, amb_bias: float = 0.4,
                 script_works: bool = True):
        self.values = values  # shared parameter store, hardware keys "group:slot" -> raw
        self.rng = random.Random(seed)
        self.t_out_fn = t_out or (lambda m: -3.5 + 0.8 * math.sin(m / 240))
        self.sh_bias = sh_bias  # controller reads superheat this much too high -> wet while regulating
        self.sh_base = sh_base
        self.defrost_every = defrost_every
        self.boost = boost
        self.cycles = cycles  # (on_min, off_min) or None = runs continuously
        self.water_bias = water_bias
        self.flow_scale = flow_scale  # >1: evaporator needs more refrigerant (warmer/bigger load)
        self.frost_rate = frost_rate  # K/h approach growth between defrosts, or a function of t_out
        self.hunt_below = hunt_below  # valve hunts when its floor is below this many steps
        self.zone_edges = zone_edges
        self.minute = 0
        self.u = 120.0
        self.frost = 0.0
        self.comp_on = True
        self.comp_on_since = -120
        self.defrost_until = -1
        self.defrost_end = -100
        self.fault = False
        self.heater = False
        self.hunting = 0.0  # amplitude in steps when the valve is near its floor
        self.extra_appr = 0.0
        self.log = []
        self.base_rps = 26.0  # steady compressor speed between oil-return boosts
        self.defrost_ctrl = defrost_ctrl
        self.fan_fn = fan_fn or (lambda m: 600.0)
        self.amb_bias = amb_bias
        self.script_works = script_works
        self.force_pending = False
        self.forced = 0  # forced defrosts that started
        self.defrost_left = 0
        self.defrost_len = 0
        self.melted = True
        self.residual = 0.0
        self.run_since = 0.0  # controller's compressor minutes since the last defrost
        self.cond_min = 0
        self.fin_start = 0.0
        self.last_fin = None
        self.last_defrost = False
        self.defrost_changed = -100
        self.starts = []  # (minute, started by, frost K)

    # hidden controller tables
    def min_od(self, z):
        return self.values[MIN_OD_HW[z - 1]]

    def sv(self, z):
        return self.values[SV_HW[z - 1]]

    def force(self) -> None:
        """The owner's forced defrost script."""
        if self.script_works:
            self.force_pending = True

    def band(self, t_amb: float) -> int:
        for i, edge in enumerate(sorted((self.values[k] for k in DFR_BANDS_HW), reverse=True)):
            if t_amb > edge:
                return i + 1
        return 5

    def _controller_defrost(self, m: int, t_amb: float) -> bool:
        v = self.values
        if self.defrost_left > 0:
            self.defrost_left -= 1
            if self.defrost_left > 0:
                return True
            self.defrost_end = m
            self.frost = self.residual
            self.run_since = 0.0
            self.cond_min = 0
            return False
        start = None
        if self.force_pending:
            self.force_pending = False
            if self.comp_on:
                start = "force"
        if start is None and self.comp_on and self.last_fin is not None:
            interval = v[DFR_INTE_HW[self.band(t_amb) - 1]] if self.defrost_ctrl.get("table", True) \
                else v[DFR_INTERVAL_HW]
            cond = self.run_since >= interval and self.last_fin <= v[DFR_COIL_START_HW] and t_amb <= v[DFR_ENV_HW]
            self.cond_min = self.cond_min + 1 if cond else 0
            if self.cond_min >= max(v[DFR_SURE_HW], 1):
                start = "interval"
        if start is None:
            return False
        dur = 1.5 + 0.7 * self.frost
        cap = v[DFR_TIME_HW] / 60
        self.melted = dur <= cap
        self.residual = 0.0 if self.melted else (dur - cap) / 0.7
        self.defrost_left = self.defrost_len = max(1, math.ceil(min(dur, cap)))
        self.fin_start = self.last_fin if self.last_fin is not None else -5.0
        self.forced += start == "force"
        self.starts.append((m, start, round(self.frost, 2)))
        return True

    def step(self) -> dict:
        m = self.minute
        self.minute += 1
        t_out = self.t_out_fn(m)
        z = zone_of(t_out, self.zone_edges)

        # compressor on/off cycles, defrosts, oil-return boosts
        if self.cycles:
            on, off = self.cycles
            should = (m % (on + off)) < on
            if should != self.comp_on:
                self.comp_on = should
                if should:
                    self.comp_on_since = m
        defrost = False
        t_amb = t_out + self.amb_bias
        if self.defrost_ctrl is not None:
            defrost = self._controller_defrost(m, t_amb)
        else:
            if self.comp_on and self.defrost_every and m % self.defrost_every == self.defrost_every - 10:
                self.defrost_until = m + 6
            if m < self.defrost_until:
                defrost = True
            elif m == self.defrost_until:
                self.defrost_end = m
                self.frost = 0.0
        if defrost != self.last_defrost:
            self.last_defrost, self.defrost_changed = defrost, m
        if self.comp_on and not defrost:
            self.run_since += 1
        rps = 0.0
        if self.comp_on:
            rps = 50.0 if defrost else (36.0 if self.boost and 20 <= m % 60 < 30 else self.base_rps)

        t_sup = 54.0 + self.rng.gauss(0, 0.2)
        t_ret = t_sup - 4.5
        target = self.sh_base + self.sv(z) * 0.1
        u0 = 70.0 * (rps / 26.0 if rps else 1.0) * (1 + 0.02 * (t_out + 3.5)) * self.flow_scale
        g, z0 = 15.0, 0.5
        want = u0 * (1 - (target - self.sh_bias - z0) / g)  # opening that gives the controller its target
        floor = self.min_od(z) * self.values[EEV_MAX_STEP_HW] / 100
        u_set = min(max(want, floor), 475.0)
        if self.hunting and floor < self.hunt_below and u_set - floor < 15:
            # valve unstable at small openings: bounces on and off the floor
            u_set = floor + self.hunting * max(0.0, math.sin(m * 1.3))
        self.u += max(-40.0, min(40.0, u_set - self.u))
        if self.u > floor + 0.5:
            self.u += self.rng.gauss(0, 0.8)
        self.u = max(self.u, floor)
        zeta = z0 + g * (u0 - self.u) / u0 if self.comp_on else 0.0  # >0 superheat K, <0 ~ % liquid

        rate = self.frost_rate(t_out) if callable(self.frost_rate) else self.frost_rate
        if self.comp_on and not defrost:
            self.frost += rate / 60
        fan = self.fan_fn(m) if self.comp_on and not defrost else 0.0
        airflow = (600.0 / fan) ** 0.7 if fan else 1.0
        appr = (3.2 * rps / 26 * airflow + 0.12 * max(zeta, 0) + self.frost + self.extra_appr) if self.comp_on else 0
        te = t_out - appr if not defrost else 5.0
        tc = t_sup + 2.5 * rps / 26 + 1.0 if self.comp_on else t_sup
        lift = tc - te
        p_el = (0.55 + 0.00042 * rps * lift) if self.comp_on else 0.0
        if defrost:
            p_el = 2.0
        dsh = None
        if self.comp_on and not defrost:
            dry = r290.dsh_for_eta(te, tc, max(zeta, 0.0), 0.68)
            dsh = dry + 3.0 * min(zeta, 0.0)
        cop_true = ((tc + 273.15) / lift * 0.55 * (1 - 0.004 * max(-zeta, 0))) if self.comp_on and lift > 5 else 0
        if defrost and self.defrost_ctrl is not None:
            # the reversed cycle takes heat from the water; the coil warms towards the end temperature
            t_ret = t_sup - 4.5
            t_sup = t_ret - 2.5
            elapsed = self.defrost_len - self.defrost_left + 1
            end = self.values[DFR_COIL_END_HW] + 1 if self.melted else self.values[DFR_COIL_END_HW] - 6
            t_fin = self.fin_start + (end - self.fin_start) * elapsed / self.defrost_len
        else:
            t_fin = te + 0.8 if self.comp_on else t_amb
        self.last_fin = t_fin
        q_w = cop_true * p_el * self.water_bias * (1 + self.rng.gauss(0, 0.01)) if self.comp_on and not defrost else 0.0
        phys = {
            "minute": m, "zone": z, "t_out": t_out, "rps": rps, "p_el": p_el, "te": te, "tc": tc,
            "t_suc": te + max(zeta, 0.0) + self.rng.gauss(0, 0.05),
            "t_dis": (tc + dsh + self.rng.gauss(0, 0.2)) if dsh is not None else tc + 5,
            "eev": round(self.u), "sh_act": round(zeta + self.sh_bias, 1), "sh_tgt": round(target, 1),
            "t_sup": t_sup, "t_ret": t_ret, "q_w": q_w, "frost_pct": self.frost * 1.9,
            "comp_on": self.comp_on, "defrost": defrost, "fault": self.fault, "heater": self.heater,
            "comp_on_min": (m - self.comp_on_since) if self.comp_on else None,
            "since_defrost_min": m - self.defrost_end, "zeta": zeta, "cop_true": cop_true,
            "t_fin": t_fin, "t_amb": t_amb, "ffd": 3.2 * rps / 26 + self.amb_bias - 0.8 if rps else None,
            "flow": 1200.0, "fan": fan, "defrost_changed": self.defrost_changed,
        }
        self.log.append(phys)
        return phys


def to_sample(phys: dict, t: float, force_ok: bool = False):
    from custom_components.mango_cop_ai.analysis import Sample

    return Sample(
        t=t, p_el=phys["p_el"], rps=phys["rps"], te=phys["te"], tc=phys["tc"], t_suc=phys["t_suc"],
        t_dis=phys["t_dis"], eev=phys["eev"], sh_act=phys["sh_act"], sh_tgt=phys["sh_tgt"],
        t_out=phys["t_out"], t_sup=phys["t_sup"], t_ret=phys["t_ret"], q_w=phys["q_w"],
        frost=phys["frost_pct"], comp_on=phys["comp_on"], defrost=phys["defrost"], fault=phys["fault"],
        heater=phys["heater"], comp_on_min=phys["comp_on_min"], since_defrost_min=phys["since_defrost_min"],
        t_fin=phys["t_fin"], t_amb=phys["t_amb"], ffd=phys["ffd"], flow=phys["flow"], fan=phys["fan"],
        defrost_changed=t - (phys["minute"] - phys["defrost_changed"]) * 60, force_ok=force_ok,
    )


def to_states(phys: dict) -> dict:
    """entity_id -> (state, attributes) with the owner's entity names and units."""
    on = "on" if phys["comp_on"] else "off"
    return {
        "sensor.invt_pout_power": (f"{phys['p_el']:.2f}", {"unit_of_measurement": "kW"}),
        "sensor.hp_running_speed": (f"{phys['rps']:.1f}", {"unit_of_measurement": "rps"}),
        "sensor.kylmaaine_haihdutuksen_kyllastyslampo_evap_sat": (f"{phys['te']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.kylmaaine_lauhdutuksen_kyllastyslampo_cond_sat": (f"{phys['tc']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.kylmaaine_imukaasu_suction": (f"{phys['t_suc']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.kylmaaine_puristuskaasu_discharge": (f"{phys['t_dis']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.eev_main_opening": (str(phys["eev"]), {}),
        "sensor.hp_main_eev_superheat_actual": (f"{phys['sh_act']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.hp_subcool_superheat_controller": (f"{phys['sh_tgt']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.ouman_outdoor_temperature": (f"{phys['t_out']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.lampopumppu_menovesi": (f"{phys['t_sup']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.lampopumppu_paluuvesi": (f"{phys['t_ret']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.hp_lampoteho_laskettu": (f"{phys['q_w'] * 1000:.0f}", {"unit_of_measurement": "W"}),
        "sensor.vilp_huurre_cop_havio": (f"{phys['frost_pct']:.1f}", {"unit_of_measurement": "%"}),
        "binary_sensor.hp_m0_kompressori_rele": (on, {}),
        "binary_sensor.hp_defrost_active": ("on" if phys["defrost"] else "off", {}),
        "binary_sensor.hp_electric_heater": ("on" if phys["heater"] else "off", {}),
        "binary_sensor.hp_fault_active": ("on" if phys["fault"] else "off", {}),
        "binary_sensor.hp_serious_alarm": ("off", {}),
        "sensor.kylmaaine_lamellilampo_fin": (f"{phys['t_fin']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.hp_module_ambient_temperature": (f"{phys['t_amb']:.1f}", {"unit_of_measurement": "°C"}),
        "sensor.hp_frost_free_coil_ambient_delta": (
            f"{phys['ffd']:.1f}" if phys["ffd"] is not None else "unknown", {"unit_of_measurement": "°C"}),
        "sensor.module_0_vf_water_pump_flow": (f"{phys['flow']:.0f}", {"unit_of_measurement": "L/h"}),
        "sensor.hp_fan_1_rpm_actual": (f"{phys['fan']:.0f}", {"unit_of_measurement": "rpm"}),
    }
