"""R290 (propane) cycle properties from polynomial fits of CoolProp data.

The coefficients are the same ones used by the "VILP COP kylmäpiiri" template sensor, so the
refrigerant-side COP computed here matches that sensor. Fit errors are below 0.25 kJ/kg inside
the validity ranges (checked against CoolProp in tests/test_r290.py).

  h1(te, sh)       suction vapour enthalpy        te -35..20 °C, sh 0..20 K
  h2(tc, dsh)      discharge gas enthalpy         tc  15..68 °C, dsh 1..70 K
  h3(tl)           liquid enthalpy                tl -10..70 °C
  dhs(te, sh, tc)  isentropic enthalpy rise       sh 0..15 K, tc - te >= 10 K

All enthalpies are in kJ/kg.
"""

from __future__ import annotations

from dataclasses import dataclass


def h1(te: float, sh: float) -> float:
    x, y = te / 10, sh / 10
    return (
        574.8503755 + 17.42082102 * y + 0.03712239893 * y**2 + 0.01180384897 * y**3 + 11.01595714 * x
        + 0.8892909685 * x * y - 0.02863314658 * x * y**2 - 0.1949240309 * x**2
        + 0.04128535182 * x**2 * y - 0.01250835273 * x**3
    )


def h2(tc: float, dsh: float) -> float:
    u, w = tc / 10, max(dsh, 0.0) ** 0.5
    return (
        574.8193712 + 0.1877680043 * w + 1.657679751 * w**2 - 0.001030019756 * w**3
        + 0.003038837302 * w**4 - 9.994057905e-05 * w**5 + 10.98473547 * u - 0.08443090298 * u * w
        + 0.191714746 * u * w**2 - 0.02239740753 * u * w**3 + 0.001032035372 * u * w**4
        - 0.1601091101 * u**2 - 0.06855974584 * u**2 * w + 0.01685189898 * u**2 * w**2
        - 0.0004242075528 * u**2 * w**3 - 0.008649407082 * u**3 + 0.006893490925 * u**3 * w
        - 0.00136262492 * u**3 * w**2 - 0.003695606123 * u**4 + 0.0008734550244 * u**4 * w
        - 0.0001255202582 * u**5
    )


def h3(tl: float) -> float:
    a = tl / 10
    return 199.980324 + 25.01007382 * a + 0.4101916015 * a**2 - 0.00547895813 * a**3 + 0.003502437708 * a**4


def dhs(te: float, sh: float, tc: float) -> float:
    x, v, u = te / 10, min(sh, 15.0) / 10, tc / 10
    return (
        -0.07223289942 + 14.08231746 * u - 0.4357347543 * u**2 + 0.00481304837 * u**3
        + 0.03547820541 * v + 0.7055932937 * v * u - 0.001653095038 * v * u**2
        - 0.006101672243 * v**2 - 0.01731964693 * v**2 * u + 0.006157708393 * v**3
        - 13.99262495 * x - 0.1164122911 * x * u - 0.0001436116102 * x * u**2 - 0.7310924628 * x * v
        + 0.008970605419 * x * v * u + 0.003994861394 * x * v**2 + 0.5158071947 * x**2
        + 0.01751812885 * x**2 * u + 0.01682173913 * x**2 * v - 0.03152756388 * x**3
    )


@dataclass(frozen=True)
class Cycle:
    """One evaluated operating point."""

    cop: float  # heating COP from the compressor energy balance, (h2 - h3) / (h2 - h1)
    eta: float  # apparent isentropic efficiency, dhs / (h2 - h1); > 0.85 means wet suction
    sh: float  # suction superheat used (clamped to 0..20 K)
    dsh: float  # discharge superheat
    tl: float  # liquid temperature used for h3


def in_range(te: float, tc: float, dsh: float) -> bool:
    return -35 <= te <= 20 and 15 <= tc <= 68 and tc - te >= 10 and 1 <= dsh <= 70


def cycle(
    te: float, tc: float, t_suction: float, t_discharge: float, t_return: float | None,
    liquid_approach: float = 2.0,
) -> Cycle | None:
    """Evaluate the cycle from measured temperatures, or return None outside the fit ranges.

    Liquid temperature = return water + liquid_approach, at most the condensing temperature.
    A wet suction cannot be seen from the suction temperature (it reads saturation), so h1 is
    then too high: the COP reads too high and the apparent efficiency rises towards or above 1.
    """
    sh = min(max(t_suction - te, 0.0), 20.0)
    dsh = t_discharge - tc
    if not in_range(te, tc, dsh):
        return None
    tl = tc if t_return is None else min(t_return + liquid_approach, tc)
    a, b, c = h1(te, sh), h2(tc, dsh), h3(tl)
    work = b - a
    if work <= 1.0:
        return None
    return Cycle(cop=(b - c) / work, eta=dhs(te, sh, tc) / work, sh=sh, dsh=dsh, tl=tl)


def dsh_for_eta(te: float, tc: float, sh: float, eta: float) -> float | None:
    """Discharge superheat that a dry compressor with this isentropic efficiency would show."""
    if not (-35 <= te <= 20 and 15 <= tc <= 68 and tc - te >= 10) or eta <= 0:
        return None
    target = h1(te, sh) + dhs(te, sh, tc) / eta
    lo, hi = 0.0, 70.0
    if h2(tc, hi) < target:
        return None
    for _ in range(50):
        mid = (lo + hi) / 2
        if h2(tc, mid) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
