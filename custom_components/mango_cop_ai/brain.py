"""Decision core of the Mango COP AI. Pure Python: Home Assistant glue lives in runner.py.

Once a minute the runner calls tick() with fresh readings and the current parameter values.
The brain
  1. derives COP, Carnot quality and the apparent compressor efficiency (wet-suction signature),
  2. learns a normal level per outdoor-temperature/speed bin and diagnoses deviations,
  3. when the data show a problem it can fix, proposes one small parameter change:
       min_od_down  Heat Min OD of the active zone -2 %   wet suction, EEV stuck at its minimum
       sv_up        Heat Sv of the active zone +0.5 K     wet suction while the EEV regulates
       min_od_up    undo own Min OD lowering one step     EEV bounces on the lowered minimum
       sv_down      undo own Sv raise one step            suction has stayed dry for 3 h
  4. finds the active zone of the 9-zone table by trying candidates one at a time and watching
     the controller react (EEV floor moves / superheat target moves), then verifies the effect
     over 60 steady minutes and keeps it, or restores the previous value.

Commands come back as Write / Notify / Dismiss / Log objects. Writes are reported back with
write_result(); followups() returns the next write of a multi-step sequence.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

from .analysis import (
    EEV_TOLERANCE,
    Baselines,
    Point,
    Sample,
    Settings,
    derive,
    hourly,
    median,
    metrics,
    quantile,
    why_text,
)
from .const import (
    EEV_MAX_STEP,
    MIN_OD,
    MODE_AUTO,
    MODE_OBSERVE,
    MODE_OFF,
    MODE_PROPOSE,
    MODES,
    SV,
    TABLES,
    initial_mode,
)
from .defrost import Defrost

MINUTE, HOUR, DAY = 60.0, 3600.0, 86400.0

TYPES = {
    "min_od_down": {"table": "min_od", "direction": -1, "probe": True, "purpose": "kuivatus"},
    "sv_up": {"table": "sv", "direction": 1, "probe": True, "purpose": "kuivatus"},
    "min_od_up": {"table": "min_od", "direction": 1, "probe": False, "purpose": "vakautus"},
    "sv_down": {"table": "sv", "direction": -1, "probe": True, "purpose": "tehostus"},
}
PROBE_WAIT = {"min_od": (4 * MINUTE, 12 * MINUTE), "sv": (3 * MINUTE, 15 * MINUTE)}
SETTLE = 5 * MINUTE
COOLDOWN = HOUR
BACKOFF_NO_GAIN = 7 * DAY
BACKOFF_HARM = 14 * DAY
BACKOFF_NOT_FOUND = DAY
FAULT_PAUSE = DAY
OBSERVE_LOG_EVERY = 6 * HOUR
PROPOSAL_TTL = 12 * HOUR
APPROVAL_TTL = DAY
ANOMALY_REPEAT = 6 * HOUR
FAILED_RETRY = 7 * DAY
# steady minutes of earlier compressor runs (up to LOOKBACK x window_min back, same conditions) top up
# the decision window when the unit runs in short cycles
LOOKBACK = 4


@dataclass
class Param:
    value: int | None
    available: bool = True
    lower: int | None = None
    upper: int | None = None


@dataclass
class Write:
    key: str
    value: int
    kind: str  # probe | apply | revert | restore
    reason: str


@dataclass
class Notify:
    title: str
    message: str
    tag: str = "toimenpide"


@dataclass
class Dismiss:
    tag: str


@dataclass
class Log:
    message: str


@dataclass
class Force:
    """Run the owner's forced defrost script."""

    reason: str


def short(key: str) -> str:
    return key.split("|", 1)[-1]


def index_of(key: str) -> int:
    m = re.search(r"(\d+)$", key)
    return int(m.group(1)) if m else 0


def fmt(value, digits=2, unit=""):
    if value is None:
        return "–"
    text = f"{value:.{digits}f}".replace(".", ",")
    return f"{text} {unit}".rstrip()


def pct(value):
    return "–" if value is None else f"{round(value * 100)} %"


class Brain:
    def __init__(self, settings: Settings | None = None, stored: dict | None = None,
                 mode: str | None = None, defrost_mode: str | None = None):
        """mode / defrost_mode: the modes written in configuration.yaml (None = not written)."""
        self.s = settings or Settings()
        st = stored or {}
        self.mode, self.yaml_mode = initial_mode(st, mode)
        self.points: deque[Point] = deque(maxlen=420)
        self.originals: dict[str, int] = {k: int(v) for k, v in st.get("originals", {}).items()}
        self.managed: dict[str, int] = {k: int(v) for k, v in st.get("managed", {}).items()}
        self.hits: list[dict] = list(st.get("hits", []))[-200:]
        self.misses: list[dict] = list(st.get("misses", []))[-200:]
        self.backoff: dict[str, float] = dict(st.get("backoff", {}))
        self.failed: dict[str, dict] = {k: v for k, v in st.get("failed", {}).items() if isinstance(v, dict)}
        self.limits: dict[str, int] = {k: int(v) for k, v in st.get("limits", {}).items()}
        self.action: dict | None = st.get("action")
        self.queue: list[dict] = list(st.get("queue", []))
        self.pending: dict | None = st.get("pending")
        self.approved: dict | None = st.get("approved")
        self.history: list[dict] = list(st.get("history", []))[-100:]
        self.write_log: list[float] = [float(t) for t in st.get("write_log", [])]
        self.writes_total = int(st.get("writes_total", 0))
        self.cooldown_until = float(st.get("cooldown_until", 0.0))
        self.paused_until = float(st.get("paused_until", 0.0))
        self.pause_reason = st.get("pause_reason", "")
        self.observed: dict[str, float] = dict(st.get("observed", {}))
        self.anomaly_notified: dict[str, float] = dict(st.get("anomaly_notified", {}))
        self.baselines = Baselines(st.get("baselines"))
        self.eta_dry: dict = dict(st.get("eta_dry", {}))
        self.next_id = int(st.get("next_id", 1))
        self.resume = self.action is not None or bool(self.queue)
        self.last_baseline = 0.0
        self.anomaly_since: float | None = None
        self.params: dict[str, Param] = {}
        self.now = 0.0
        self.diagnosis: dict = {"code": "kerää", "text": "Kerää dataa", "details": {}}
        self.cop: dict = {}
        self.suggestion: dict | None = None
        self.budget_logged = 0.0
        self.status_text = "Kerää dataa"
        self.defrost = Defrost(self, st.get("defrost"), mode=defrost_mode)

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "yaml_mode": self.yaml_mode, "originals": self.originals, "managed": self.managed,
            "hits": self.hits, "misses": self.misses, "backoff": self.backoff, "failed": self.failed,
            "limits": self.limits,
            "action": self.action, "queue": self.queue, "pending": self.pending,
            "approved": self.approved, "history": self.history, "write_log": self.write_log,
            "writes_total": self.writes_total, "cooldown_until": self.cooldown_until,
            "paused_until": self.paused_until, "pause_reason": self.pause_reason,
            "observed": self.observed, "anomaly_notified": self.anomaly_notified,
            "baselines": self.baselines.bins, "next_id": self.next_id, "eta_dry": self.eta_dry,
            "defrost": self.defrost.to_dict(),
        }

    # ------------------------------------------------------------------ public API
    def tick(self, now: float, sample: Sample, params: dict[str, Param]) -> list:
        self.now, self.params = now, params
        out: list = []
        point = derive(sample, self.s, self.points)
        self.points.append(point)
        self._prune()
        self._external_changes(out)
        self.defrost.check_external(out)
        out += self.defrost.observe(point)
        if point.frost is None and not point.defrost and self.defrost.cycle.get("loss") is not None:
            # no frost sensor of its own: the defrost watch's estimate keeps frosted hours out of the
            # learned COP level and lets the diagnosis name frost
            point.frost = round(self.defrost.cycle["loss"] * 100, 1)
        self._monitor(point, out)
        if self.resume:
            self.resume = False
            self._resume(out)
        if point.fault and self.paused_until <= now:
            self._on_fault(out)
        writes = self._drain()
        if writes:
            self._update_status()
            return out + writes
        if self.action is not None:
            out += self._advance(point)
        elif self.mode != MODE_OFF and point.stable:
            out += self._consider()
        if not any(isinstance(c, Write | Force) for c in out):
            out += self.defrost.act(point)
        self._expire()
        self._update_status()
        return out

    def followups(self, now: float, params: dict[str, Param]) -> list:
        """Next write of a sequence, after the previous one was reported."""
        self.now, self.params = now, params
        out = self._drain()
        if not out and self.action is not None:
            out = self._phase_write()
        if not out:
            out = self.defrost.phase_write()
        self._update_status()
        return out

    def force_result(self, ok: bool, error: str | None = None, now: float | None = None) -> list:
        """Report whether the forced defrost script could be started."""
        if now is not None:
            self.now = now
        return self.defrost.force_result(ok, error)

    def set_defrost_mode(self, mode: str, now: float) -> list:
        self.now = now
        out: list = []
        self.defrost.set_mode(mode, out)
        self._update_status()
        return out

    def write_result(self, write: Write, ok: bool, error: str | None = None, now: float | None = None,
                     actual: int | None = None):
        """Report a write. actual = value read back after a failed write (None = unknown)."""
        if now is not None:
            self.now = now
        self.write_log.append(self.now)
        self.writes_total += 1
        out: list = []
        if write.kind.startswith("dfr_"):
            out = self.defrost.write_result(write, ok, error, actual)
            self._update_status()
            return out
        if write.kind == "restore":
            self._restore_result(write, ok, error, out)
            self._update_status()
            return out
        a = self.action
        if a is None or not a.get("inflight") or a.get("write_key") != write.key:
            return out
        a["inflight"] = False
        phase = a["phase"]
        if phase in ("probe_write", "apply_write"):
            if ok:
                self.originals.setdefault(write.key, a["before_values"][write.key])
                self._set_value(write.key, write.value)
                if phase == "apply_write":
                    self._start_verify(a, write.key)
                else:
                    a.update(phase="probe_wait", phase_t=self.now, probe_start=self.now)
            else:
                self.failed[write.key] = {"error": error or "kirjoitus epäonnistui", "t": self.now}
                out.append(Log(f"{short(write.key)}: laite ei hyväksynyt arvoa {write.value} ({error}). "
                               "Parametri jätetään viikoksi rauhaan."))
                before = a["before_values"].pop(write.key, None)
                if before is not None and actual != before:
                    self._queue_restore(write.key, before, "kirjoitus epäonnistui, varmistetaan alkuperäinen")
                self._next_candidate(a, out)
        elif phase in ("probe_revert", "revert"):
            if ok:
                self._set_value(write.key, write.value)
                if phase == "probe_revert":
                    self.misses.append({"table": a["table"], "key": write.key, **a["cond"], "ts": self.now})
                    self._next_candidate(a, out)
                else:
                    self._finish(a, out)
            else:
                self._queue_restore(write.key, write.value, "palautus epäonnistui, yritetään uudelleen")
                a["outcome"] = a.get("outcome") or ["keskeytetty", "palautus epäonnistui", 0]
                self._finish(a, out)
        self._update_status()
        return out

    def set_mode(self, mode: str, now: float) -> list:
        if mode not in MODES:
            return []
        self.now = now
        out: list = []
        old, self.mode = self.mode, mode
        if mode in (MODE_OFF, MODE_OBSERVE) and self.action is not None:
            self._abort(self.action, f"tila vaihdettiin: {mode}", out)
        eev_pending = self.pending and not self.pending.get("type", "").startswith("dfr_")
        if mode != MODE_PROPOSE and (eev_pending or self.approved):
            self.approved = None
            if eev_pending:
                self.pending = None
            out.append(Dismiss("ehdotus"))
        if old != mode:
            out.append(Log(f"Tila: {old} → {mode}"))
        self._update_status()
        return out

    def approve(self, now: float) -> list:
        self.now = now
        if not self.pending:
            return [Log("Ei hyväksyttävää ehdotusta")]
        if self.pending.get("type", "").startswith("dfr_"):
            # defrost proposals rest on past defrosts or on the frost right now: act at once
            p, self.pending = self.pending, None
            out = [Dismiss("ehdotus"), Log(f"Ehdotus hyväksytty: {p['title']}")]
            if p["type"] == "dfr_force":
                out += self.defrost.force_now(self.points[-1] if self.points else None)
            else:
                out += self.defrost.start(p)
            self._update_status()
            return out
        self.approved = {**self.pending, "approved_at": now}
        self.pending = None
        self._update_status()
        return [Dismiss("ehdotus"), Log(f"Ehdotus hyväksytty: {self.approved['title']}. "
                                        "Toteutetaan, kun tilanne on sama.")]

    def reject(self, now: float) -> list:
        self.now = now
        if not self.pending:
            return [Log("Ei hylättävää ehdotusta")]
        p, self.pending = self.pending, None
        if p["type"] == "dfr_force":
            self.backoff["type:dfr_force"] = now + 2 * HOUR
            self._update_status()
            return [Dismiss("ehdotus"), Log("Pakkosulatusehdotus hylätty. Ei ehdoteta uudelleen kahteen tuntiin.")]
        self.backoff[f"type:{p['type']}"] = now + DAY
        self._update_status()
        return [Dismiss("ehdotus"), Log(f"Ehdotus hylätty: {p['title']}. Ei ehdoteta uudelleen 24 tuntiin.")]

    def rollback_all(self, now: float, reason: str) -> list:
        """Restore every parameter the AI has changed to the value it had before the AI."""
        self.now = now
        out: list = []
        a = self.action
        if a is not None:
            if a.get("inflight") and a.get("write_key") in a["before_values"]:
                self._queue_restore(a["write_key"], a["before_values"][a["write_key"]], reason)
            self.action = None
            self.history.append(self._entry(a, "keskeytetty", reason))
        self.defrost.cancel(reason, out)
        for key, value in list(self.managed.items()):
            orig = self.originals.get(key)
            if orig is not None and orig != value:
                self._queue_restore(key, orig, reason)
        self.pending = self.approved = None
        if reason == "käyttäjä":
            self.mode = MODE_OBSERVE
            self.defrost.mode = MODE_OBSERVE
        if self.queue:
            out.append(Log(f"Palautetaan alkuperäiset arvot ({reason}): "
                           + ", ".join(f"{short(q['key'])} → {q['value']}" for q in self.queue)))
        else:
            out.append(Log("Ei palautettavaa: tekoäly ei ole muuttanut yhtään arvoa"))
        out.append(Dismiss("ehdotus"))
        self._update_status()
        return out

    # ------------------------------------------------------------------ helpers
    @property
    def eta_ref(self) -> float:
        """Apparent compressor efficiency with surely dry suction: learned, or the default."""
        if self.eta_dry.get("n", 0) >= 60:
            return self.eta_dry["value"]
        return self.s.eta_dry_default

    @property
    def wet_eta(self) -> float:
        return self.eta_ref + self.s.wet_margin

    def _learn_dry(self, p: Point):
        # superheat >= 3 K at the suction sensor: no liquid can be present, so this is the dry level
        if not p.stable or p.sh is None or p.sh < 3.0 or p.eta is None or not 0.5 <= p.eta <= 0.9:
            return
        n = min(self.eta_dry.get("n", 0) + 1, 10_000)
        v = self.eta_dry.get("value", p.eta)
        v += max(0.005, 1.0 / n) * (p.eta - v)
        self.eta_dry = {"n": n, "value": min(0.82, max(0.55, v))}

    def _window(self, minutes: float) -> list[Point]:
        start = self.now - minutes * MINUTE
        return [p for p in self.points if p.t > start]

    def _need(self) -> int:
        """Steady minutes needed before deciding anything."""
        return max(20, int(self.s.window_min * 0.6))

    def _decision_window(self) -> list[Point]:
        """The minutes a decision rests on: normally the last window_min minutes.

        A big unit in mild weather runs in short cycles: 15 settling minutes after every start leave
        too few steady minutes in any window_min. Then the steady minutes of the earlier runs in the
        same conditions (outdoor within 2 K, supply water within 6 K, at most LOOKBACK x window_min
        back) top the sample up, so that short runs are not a reason never to decide.
        """
        win = self._window(self.s.window_min)
        need = self._need()
        have = sum(1 for p in win if p.stable and p.eta is not None)
        cur = self.points[-1] if self.points else None
        if have >= need or cur is None or cur.t_out is None:
            return win
        start, cut = self.now - LOOKBACK * self.s.window_min * MINUTE, self.now - self.s.window_min * MINUTE
        older = [p for p in self.points if start < p.t <= cut and p.stable and p.eta is not None
                 and p.t_out is not None and abs(p.t_out - cur.t_out) <= 2.0
                 and (p.t_sup is None or cur.t_sup is None or abs(p.t_sup - cur.t_sup) <= 6.0)]
        return older[-(need - have):] + win

    def steady_minutes(self) -> tuple[int, int]:
        """(steady minutes available for a decision, minutes needed)."""
        n = sum(1 for p in self._decision_window() if p.stable and p.eta is not None)
        return n, self._need()

    def _steps_per_pct(self) -> float:
        if self.s.eev_full_steps:
            return self.s.eev_full_steps / 100
        p = self.params.get(EEV_MAX_STEP)
        if p and p.available and p.value and p.value >= 100:
            return p.value / 100
        return 5.0

    def _floors(self) -> dict[str, float]:
        k = self._steps_per_pct()
        return {key: p.value * k for key in MIN_OD
                if (p := self.params.get(key)) and p.available and p.value is not None}

    def _writes_24h(self) -> int:
        return sum(1 for t in self.write_log if t > self.now - DAY)

    def _budget_ok(self, needed: int = 2) -> bool:
        return self._writes_24h() + needed <= self.s.writes_per_day

    def _prune(self):
        self.write_log = [t for t in self.write_log if t > self.now - 2 * DAY]
        self.backoff = {k: v for k, v in self.backoff.items() if v > self.now}
        self.misses = [m for m in self.misses if m["ts"] > self.now - 7 * DAY]

    def _set_value(self, key: str, value: int):
        if self.originals.get(key) == value:
            self.managed.pop(key, None)
        else:
            self.managed[key] = value

    def _limits(self, key: str, direction: int):
        """Allowed range for a move, or None. Moves only ever go one way from the original."""
        p = self.params.get(key)
        if not p or not p.available or p.value is None or (
                key in self.failed and self.failed[key]["t"] > self.now - FAILED_RETRY):
            return None
        if max(self.backoff.get(key, 0), self.backoff.get(f"{key}|{direction}", 0)) > self.now:
            return None
        orig = self.originals.get(key, p.value)
        lo = p.lower if p.lower is not None else -10**6
        hi = p.upper if p.upper is not None else 10**6
        if key in MIN_OD:
            step = self.s.min_od_step
            if direction < 0:
                lo = max(lo, self.s.min_od_floor, orig - self.s.min_od_max_delta, self.limits.get(key, lo))
            else:
                hi = min(hi, orig)
        else:
            step = self.s.sv_step
            if direction > 0:
                hi = min(hi, self.s.sv_abs_max, orig + self.s.sv_max_delta)
            else:
                lo = max(lo, orig)
        new = p.value + direction * step
        new = min(max(new, lo), hi)
        return new if new != p.value and (new - p.value) * direction > 0 else None

    def _near(self, h: dict, cond: dict, dt: float = 2.0) -> bool:
        if h.get("t_out") is None or cond.get("t_out") is None:
            return False
        if abs(h["t_out"] - cond["t_out"]) > dt:
            return False
        if h.get("t_sup") is not None and cond.get("t_sup") is not None:
            return abs(h["t_sup"] - cond["t_sup"]) <= 6.0
        return True

    def _expected_index(self, table: str, cond: dict):
        """Most likely active zone: learned hits of this table, then of the other table, then a prior.

        The prior assumes the nine zones are outdoor-temperature bands from warm (1) to cold (9),
        about 4 K wide around 0 °C. It only sets the order of the first trial; a wrong guess costs
        one extra probe and is remembered as a miss.
        """
        own = [h for h in self.hits if h["table"] == table and h.get("t_out") is not None]
        src = own or [h for h in self.hits if h.get("t_out") is not None]
        t = cond.get("t_out")
        if t is None:
            return None
        if not src:
            return min(9.0, max(1.0, 1 + (16 - t) / 4))
        below = [h for h in src if h["t_out"] <= t]
        above = [h for h in src if h["t_out"] >= t]
        if below and above:
            b = max(below, key=lambda h: h["t_out"])
            a = min(above, key=lambda h: h["t_out"])
            if a["t_out"] > b["t_out"]:
                f = (t - b["t_out"]) / (a["t_out"] - b["t_out"])
                return index_of(b["key"]) + f * (index_of(a["key"]) - index_of(b["key"]))
        return index_of(min(src, key=lambda h: abs(h["t_out"] - t))["key"])

    def _order(self, table: str, cands: list[str], cond: dict, matching: list[str] | None = None) -> list[str]:
        """Candidates in trial order. If a zone is already known for these conditions, only that one."""
        known = self._known_zone(matching or TABLES[table], cond)
        if known is not None:
            return [known] if known in cands else []
        cands = [c for c in cands if not any(
            m["key"] == c and self._near(m, cond, 1.5) for m in self.misses)]
        exp = self._expected_index(table, cond)
        if exp is None:
            return sorted(cands, key=index_of)
        return sorted(cands, key=lambda c: (abs(index_of(c) - exp), index_of(c)))

    def _zone_likely(self, key: str, cond: dict) -> bool:
        return self._known_zone([key], cond) == key

    def _known_zone(self, keys, cond: dict):
        """The key whose learned hit is closest to these conditions (within 1.5 K), or None."""
        near = [h for h in self.hits if h["key"] in keys and self._near(h, cond, 1.5)
                and h["ts"] > self.now - 60 * DAY]
        if not near:
            return None
        return min(near, key=lambda h: (abs(h["t_out"] - cond["t_out"]), -h["ts"]))["key"]

    # ------------------------------------------------------------------ monitoring
    def _monitor(self, point: Point, out: list):
        self._learn_dry(point)
        w60 = self._window(60)
        h = hourly(w60)
        m60 = metrics(w60, self.wet_eta)
        basis = "lq_kp" if self.s.cop_basis == "kylmapiiri" else "lq_w"
        cop = dict(h)
        cop["basis"] = self.s.cop_basis
        devs: dict[str, float] = {}
        expected = None
        if m60.get("n", 0) >= 30 and m60.get("t_out") is not None and m60.get("rps") is not None:
            key = Baselines.key(m60["t_out"], m60["rps"])
            cop["bin"] = key
            for f in ("lq_kp", "lq_w"):
                base, cur = self.baselines.get(key, f), h.get(f)
                if base and cur:
                    devs[f] = (cur / base - 1) * 100
                    if f == basis and h.get("carnot"):
                        expected = base * h["carnot"]
        # an anomaly needs both COP bases to agree: wet suction inflates only the refrigerant-side
        # COP and a flow-sensor glitch moves only the water side
        dev = max(devs.values()) if devs else None
        cop.update(expected=expected, deviation_pct=devs.get(basis), deviation_kp=devs.get("lq_kp"),
                   deviation_w=devs.get("lq_w"))
        self.cop = cop

        if self.now - self.last_baseline >= 30 * MINUTE:
            w30 = self._window(30)
            m30 = metrics(w30, self.wet_eta)
            healthy = (
                m30.get("n", 0) >= 20 and m30["wet_frac"] < 0.2
                and (m30.get("frost") is None or m30["frost"] < 5)
                and self.action is None and not any(p.fault or p.heater for p in w30)
                and m30.get("t_out") is not None and m30.get("rps") is not None
            )
            if m30.get("n", 0) >= 20:
                self.last_baseline = self.now
            if healthy and (dev is None or dev > -self.s.anomaly_pct / 2):
                self.baselines.update(Baselines.key(m30["t_out"], m30["rps"]), m30)

        if dev is not None and dev <= -self.s.anomaly_pct:
            self.anomaly_since = self.anomaly_since or self.now
        elif dev is None or dev > -self.s.anomaly_pct / 2:
            self.anomaly_since = None
        self.diagnosis = self._diagnose(point, dev)
        if self.anomaly_since is not None and self.now - self.anomaly_since >= HOUR:
            code = self.diagnosis["code"]
            if self.now - self.anomaly_notified.get(code, 0) >= ANOMALY_REPEAT and self.mode != MODE_OFF:
                self.anomaly_notified[code] = self.now
                parts = [f"{fmt(abs(devs[f]), 0)} % ({name})" for f, name in
                         (("lq_kp", "kylmäpiiri"), ("lq_w", "vesipuoli")) if f in devs]
                out.append(Notify(
                    "COP normaalia matalampi",
                    f"COP-laatuaste on ollut tunnin alle opitun tason: {' ja '.join(parts)}. "
                    f"Odotettu COP {fmt(expected)}, nyt {fmt(h.get(basis.replace('lq', 'cop')))}.\n"
                    f"Todennäköinen syy: {self.diagnosis['text']}.", "poikkeama"))

    def _diagnose(self, point: Point, dev) -> dict:
        def d(code, text, **details):
            return {"code": code, "text": text, "details": details}

        missing = list(point.missing)
        if missing:
            return d("ei_dataa", "Anturitietoja puuttuu: " + ", ".join(missing))
        if point.fault:
            return d("vika", "Lämpöpumpussa on vika- tai hälytystila")
        if point.heater:
            return d("vastus", "Sähkövastus on päällä, sen COP on noin 1")
        if point.defrost:
            return d("sulatus", "Sulatus käynnissä")
        if not point.comp_on:
            return d("seis", "Kompressori seis")
        if (point.comp_on_min or 0) < 15 or (point.since_defrost_min is not None
                                              and point.since_defrost_min < 20):
            return d("tasaantuu", "Käynnistys tai sulatus juuri ohi, odotetaan tasaista käyntiä")
        m = metrics(self._window(20), self.wet_eta, self._floors())
        if m.get("n", 0) < 10:
            return d("tasaantuu", "Käynti ei ole vielä tasaista")
        info = {"näennäinen_hyötysuhde": round(m["eta_med"], 2), "märkänä": pct(m["wet_frac"]),
                "eev_askeleet": m.get("eev_med")}
        if m["wet_frac"] >= 0.5:
            if m["pinned_frac"] >= 0.6:
                return d("marka_imu_minimi",
                         f"Imukaasu märkää: EEV on minimiavauksessa ({m['pinned_steps']:.0f} askelta) "
                         "eikä pysty kuristamaan", **info)
            return d("marka_imu", "Imukaasu märkää, vaikka EEV säätää: tulistuksen tavoite on liian "
                     "pieni", **info)
        if m.get("frost") is not None and m["frost"] >= 8:
            return d("huurre", f"Kennossa huurretta, COP-häviö {fmt(m['frost'], 0)} %", **info)
        key = Baselines.key(m["t_out"], m["rps"]) if m.get("t_out") is not None else None
        base_e = self.baselines.get(key, "appr_e") if key else None
        base_c = self.baselines.get(key, "appr_c") if key else None
        if base_e is not None and m.get("appr_e") is not None and m["appr_e"] - base_e >= 3:
            return d("hoyrystin", f"Höyrystimen lähestymä {fmt(m['appr_e'] - base_e, 1)} K normaalia "
                     "suurempi: huurre, lika tai puhallin", **info)
        if base_c is not None and m.get("appr_c") is not None and m["appr_c"] - base_c >= 3:
            return d("lauhdutin", f"Lauhduttimen lähestymä {fmt(m['appr_c'] - base_c, 1)} K normaalia "
                     "suurempi: vesivirtaus tai lämmönsiirto heikentynyt", **info)
        sh = median(p.sh for p in self._window(20) if p.stable)
        if sh is not None and sh >= 10 and (m.get("eev_med") or 0) >= 0.9 * self._steps_per_pct() * 100:
            return d("tulistus_suuri", "EEV lähes täysin auki ja tulistus suuri: kylmäainevaje tai "
                     "EEV-vika?", **info)
        if dev is not None and dev <= -self.s.anomaly_pct:
            return d("cop_matala", f"COP {fmt(abs(dev), 0)} % normaalia alempi, syy ei selvä", **info)
        return d("ok", "Kaikki kunnossa", **info)

    # ------------------------------------------------------------------ proposals
    def _find_proposal(self) -> dict | None:
        floors = self._floors()
        win = self._decision_window()
        m = metrics(win, self.wet_eta, floors)
        if m.get("n", 0) < self._need() or not win or win[0].t > self.now - (self.s.window_min - 2) * MINUTE:
            return None
        cond = {"t_out": m["t_out"], "t_sup": m["t_sup"]}
        before = {k: m.get(k) for k in ("eta_med", "wet_frac", "pinned_frac", "pinned_steps", "eev_med",
                                        "eev_spread", "lq_kp", "lq_w", "appr_e", "t_dis_p95", "sh_tgt",
                                        "sh_act", "t_out", "t_sup", "rps")}
        # start drying at the wet level; after a drying step accepted today, continue to a lower margin
        drying = any(h["outcome"] == "hyväksytty" and TYPES.get(h["type"], {}).get("purpose") == "kuivatus"
                     and h["t"] > self.now - DAY for h in self.history)
        wet_start = m["wet_frac"] >= 0.6 and m["eta_med"] >= self.wet_eta
        wet = wet_start or (drying and m["eta_med"] >= self.eta_ref + self.s.continue_margin)
        state = "Imukaasu on märkää" if wet_start else "Imukaasu on yhä märän rajalla, kuivatusta jatketaan"
        evidence = (f"näennäinen hyötysuhde {fmt(m['eta_med'])}, kuivan imun taso {fmt(self.eta_ref)}, "
                    f"märkänä {pct(m['wet_frac'])} ajasta, "
                    f"ulkona {fmt(m['t_out'], 1)} °C, menovesi {fmt(m['t_sup'], 1)} °C")

        def proposal(kind, cands, title, reason):
            if self.backoff.get(f"type:{kind}", 0) > self.now:
                return None
            return {"type": kind, "table": TYPES[kind]["table"], "candidates": cands, "cond": cond,
                    "before": before, "title": title, "reason": reason, "created": self.now}

        def title(cands, direction, family):
            key = cands[0]
            val, new = self.params[key].value, self._limits(key, direction)
            if len(cands) == 1 and self._zone_likely(key, cond):
                return f"{short(key)} {val} → {new}"
            return f"{family} {val} → {new} (aktiivinen vyöhyke)"

        if wet and self.s.enable_min_od and m["pinned_frac"] >= 0.75:
            steps = m["pinned_steps"]
            matching = [k for k, v in floors.items() if abs(v - steps) < 0.01]
            cands = self._order("min_od", [k for k in matching if self._limits(k, -1) is not None], cond,
                                matching)
            if cands:
                p = proposal(
                    "min_od_down", cands, title(cands, -1, "Heat Min OD"),
                    f"{state} ({evidence}) ja EEV on minimiavauksessa {steps:.0f} askelta "
                    f"{pct(m['pinned_frac'])} ajasta. Lasken aktiivisen vyöhykkeen Heat Min OD -arvoa "
                    f"{self.s.min_od_step} %. Kokeilujärjestys: {', '.join(short(c) for c in cands)}.")
                if p:
                    return p
        if wet and self.s.enable_sv and m["pinned_frac"] <= 0.3 and (
                m.get("sh_tgt") is not None or m.get("sh_act") is not None):
            cands = self._order("sv", [k for k in SV if self._limits(k, 1) is not None], cond)
            if cands:
                p = proposal(
                    "sv_up", cands, title(cands, 1, "Heat Sv"),
                    f"{state} ({evidence}), vaikka EEV säätää vapaasti. Nostan aktiivisen "
                    f"vyöhykkeen tulistustavoitetta Heat Sv {fmt(self.s.sv_step / 10, 1)} K. "
                    f"Kokeilujärjestys: {', '.join(short(c) for c in cands)}.")
                if p:
                    return p
        if self.s.enable_min_od and m.get("eev_spread") is not None and m["eev_spread"] >= 20:
            for key in MIN_OD:
                if key in self.managed and self._zone_likely(key, cond) and key in floors:
                    eev = [p.eev for p in win if p.stable and p.eev is not None]
                    at_floor = sum(abs(e - floors[key]) <= EEV_TOLERANCE for e in eev) / max(len(eev), 1)
                    new = self._limits(key, 1)
                    if 0.1 <= at_floor <= 0.6 and new is not None:
                        p = proposal(
                            "min_od_up", [key], f"{short(key)} {self.params[key].value} → {new}",
                            f"EEV heiluu lasketun minimin ympärillä (vaihteluväli {m['eev_spread']:.0f} "
                            f"askelta, minimissä {pct(at_floor)} ajasta). Nostan {short(key)} takaisin "
                            f"{self.params[key].value} → {new}.")
                        if p:
                            return p
        if self.s.enable_sv:
            ml = metrics(self._window(180), self.wet_eta)
            if ml.get("n", 0) >= 90 and ml["eta_med"] <= self.eta_ref + self.s.dry_margin \
                    and ml["wet_frac"] <= 0.05:
                for key in SV:
                    if key in self.managed and self.managed[key] > self.originals.get(key, 10**6) \
                            and self._zone_likely(key, cond):
                        new = self._limits(key, -1)
                        if new is not None:
                            p = proposal(
                                "sv_down", [key], f"{short(key)} {self.params[key].value} → {new}",
                                f"Imu on pysynyt kuivana 3 tuntia (näennäinen hyötysuhde "
                                f"{fmt(ml['eta_med'])}). Lasken {short(key)} {self.params[key].value} → "
                                f"{new}, jotta höyrystymislämpö nousee.")
                            if p:
                                return p
        return None

    def _consider(self) -> list:
        out: list = []
        if self.now < self.paused_until or self.now < self.cooldown_until or self.queue:
            return out
        prop = self._find_proposal()
        self.suggestion = prop
        if prop is None:
            return out
        if self.mode == MODE_OBSERVE:
            if self.now - self.observed.get(prop["type"], 0) >= OBSERVE_LOG_EVERY:
                self.observed[prop["type"]] = self.now
                out.append(Log(f"Tarkkailu, ei muutettu mitään. Tekisin: {prop['title']}. {prop['reason']}"))
            return out
        if not self._budget_ok():
            if self.now - self.budget_logged >= 6 * HOUR:
                self.budget_logged = self.now
                out.append(Log(f"Kirjoitusraja {self.s.writes_per_day}/vrk täynnä, odotetaan"))
            return out
        if self.mode == MODE_PROPOSE:
            if self.approved and self.approved["type"] == prop["type"]:
                self.approved = None
                return out + self._start(prop)
            if not self.pending and not self.approved:
                self.pending = prop
                out.append(Notify(
                    "COP-tekoälyn ehdotus",
                    f"{prop['reason']}\n\nHyväksy painikkeella **Hyväksy ehdotus** tai hylkää painikkeella "
                    "**Hylkää ehdotus**. Muutos varmistetaan tunnin ajan ja perutaan, jos tulos on huonompi.",
                    "ehdotus"))
            return out
        if self.mode == MODE_AUTO:
            return out + self._start(prop)
        return out

    def _expire(self):
        if self.pending and self.now - self.pending["created"] > PROPOSAL_TTL:
            self.pending = None
        if self.approved and self.now - self.approved["approved_at"] > APPROVAL_TTL:
            self.approved = None

    # ------------------------------------------------------------------ action state machine
    def _start(self, prop: dict) -> list:
        kind = prop["type"]
        recent = [p for p in self._window(10) if p.stable]
        self.action = {
            "id": self.next_id, "type": kind, "table": prop["table"],
            "direction": TYPES[kind]["direction"], "probe": TYPES[kind]["probe"],
            "candidates": prop["candidates"], "ci": 0,
            "phase": "probe_write" if TYPES[kind]["probe"] else "apply_write",
            "before_values": {}, "started": self.now, "phase_t": self.now,
            "before": prop["before"], "cond": prop["cond"], "reason": prop["reason"],
            "title": prop["title"], "identified": None, "verify_n": 0, "settle_until": None,
            "deadline": None, "inflight": False, "write_key": None, "target": None,
            "revert_key": None, "outcome": None,
            "ref": {"eev": median(p.eev for p in recent), "sh_tgt": median(p.sh_tgt for p in recent),
                    "sh_act": median(p.sh_act for p in recent), "rps": median(p.rps for p in recent)},
            "probe_start": self.now,
        }
        self.next_id += 1
        self.pending = None
        out = [Log(f"Aloitetaan: {prop['title']}. {prop['reason']}")]
        return out + self._phase_write()

    def _phase_write(self) -> list:
        a = self.action
        if a is None or a.get("inflight"):
            return []
        if a["phase"] in ("probe_write", "apply_write"):
            while a["ci"] < len(a["candidates"]):
                key = a["candidates"][a["ci"]]
                new = self._limits(key, a["direction"])
                if new is None:
                    a["ci"] += 1
                    continue
                if not self._budget_ok():
                    out: list = [Log("Kirjoitusraja täynnä kesken testin, jatketaan myöhemmin")]
                    a["outcome"] = ["keskeytetty", "kirjoitusraja", 0]
                    self._finish(a, out)
                    oldest = min((t for t in self.write_log if t > self.now - DAY), default=self.now)
                    self.cooldown_until = max(self.cooldown_until, oldest + DAY)
                    return out
                a["before_values"].setdefault(key, self.params[key].value)
                a.update(inflight=True, inflight_t=self.now, write_key=key, target=new)
                kind = "probe" if a["phase"] == "probe_write" else "apply"
                return [Write(key, new, kind, a["title"])]
            out = []
            if a["probe"]:
                self._finish_not_found(a, out)
            else:
                a["outcome"] = ["keskeytetty", "arvoa ei voi enää muuttaa", 0]
                self._finish(a, out)
            return out
        if a["phase"] in ("probe_revert", "revert"):
            key = a["revert_key"]
            a.update(inflight=True, inflight_t=self.now, write_key=key)
            return [Write(key, a["before_values"][key], "revert", a["title"])]
        return []

    def _next_candidate(self, a: dict, out: list):
        a["ci"] += 1
        if a["ci"] < len(a["candidates"]):
            a["phase"], a["phase_t"] = "probe_write", self.now
            return
        self._finish_not_found(a, out)

    def _finish_not_found(self, a: dict, out: list):
        self.backoff[f"type:{a['type']}"] = self.now + BACKOFF_NOT_FOUND
        a["outcome"] = ["ei_tunnistettu", "yksikään ehdokas ei vaikuttanut säätimeen", 0]
        self._finish(a, out)

    def _start_verify(self, a: dict, key: str):
        a.update(phase="verify", identified=key, verify_n=0, settle_until=self.now + SETTLE,
                 deadline=self.now + DAY, phase_t=self.now)

    def _advance(self, point: Point) -> list:
        a = self.action
        out: list = []
        if a.get("inflight"):
            if self.now - a.get("inflight_t", self.now) > 10 * MINUTE:
                # the write never reported back: assume the worst and restore the previous value
                key = a["write_key"]
                a["inflight"] = False
                self._queue_restore(key, a["before_values"][key], "kirjoituksen tulos jäi saamatta")
                a["outcome"] = ["keskeytetty", "kirjoituksen tulos jäi saamatta", 0]
                self._finish(a, out)
            return out
        if a["phase"] in ("probe_write", "apply_write", "probe_revert", "revert"):
            return self._phase_write()
        if a["phase"] == "probe_wait":
            key = a["candidates"][a["ci"]]
            if point.defrost or not point.comp_on or self.mode in (MODE_OFF, MODE_OBSERVE):
                self._abort(a, "sulatus tai pysähdys kesken testin", out)
                return out + self._phase_write()
            elapsed = self.now - a["phase_t"]
            lo, hi = PROBE_WAIT[a["table"]]
            seen = self._observe(a)
            if seen == "wait" and self.now - a["probe_start"] < 3 * hi:
                # oil-return boost or speed change: not comparable, give the controller more time
                a["phase_t"] = min(a["phase_t"] + MINUTE, self.now)
                return out
            if seen == "yes" and elapsed >= lo:
                self.hits.append({"table": a["table"], "key": key, **a["cond"], "ts": self.now})
                self.hits = self.hits[-200:]
                self._start_verify(a, key)
                out.append(Log(f"Aktiivinen vyöhyke löytyi: {short(key)} (ulkona {fmt(a['cond']['t_out'], 1)} °C). "
                               f"Varmistetaan vaikutus {self.s.verify_min} min."))
            elif seen == "lost":
                self._abort(a, "säädin lähti pois minimistä kesken testin", out)
                return out + self._phase_write()
            elif elapsed >= hi:
                a.update(phase="probe_revert", revert_key=key, phase_t=self.now)
                return out + self._phase_write()
            return out
        if a["phase"] == "verify":
            return self._verify(point)
        return out

    def _observe(self, a: dict) -> str:
        """Did the controller react to the probe? yes / no / lost / wait (not comparable now)."""
        ref = a["ref"]
        recent = [p for p in self.points if p.t > self.now - 3 * MINUTE and p.comp_on and not p.defrost]
        if ref.get("rps") is not None:
            recent = [p for p in recent if p.rps is not None and abs(p.rps - ref["rps"]) <= 3.0]
        if not recent:
            return "wait"
        if a["table"] == "min_od":
            eev = median(p.eev for p in recent)
            if eev is None or ref.get("eev") is None:
                return "no"
            if eev <= ref["eev"] - 0.5 * self.s.min_od_step * self._steps_per_pct():
                return "yes"
            if eev >= ref["eev"] + 3 * EEV_TOLERANCE:
                return "lost"
            return "no"
        need = 0.6 * self.s.sv_step * 0.1
        tgt = median(p.sh_tgt for p in recent)
        if tgt is not None and ref.get("sh_tgt") is not None:
            return "yes" if a["direction"] * (tgt - ref["sh_tgt"]) >= need - 1e-9 else "no"
        eev, sh = median(p.eev for p in recent), median(p.sh_act for p in recent)
        if None in (eev, sh, ref.get("eev"), ref.get("sh_act")):
            return "no"
        d = a["direction"]
        if d * (ref["eev"] - eev) >= 4 and d * (sh - ref["sh_act"]) >= 0.3:
            return "yes"
        return "no"

    def _verify(self, point: Point) -> list:
        a = self.action
        out: list = []
        if self.now > a["deadline"]:
            self._revert(a, "ei_varmistettu", "tasaista käyntiä ei saatu vuorokaudessa", DAY, out)
            return out + self._phase_write()
        if point.stable and point.t >= a["settle_until"]:
            a["verify_n"] += 1
        after_pts = [p for p in self.points if p.t >= a["settle_until"] and p.stable]
        b = a["before"]
        recent = after_pts[-20:]
        if len(recent) >= 10:
            harm = None
            t_dis = median(p.t_dis for p in recent[-5:])
            appr = median(p.appr_e for p in recent)
            eev = [p.eev for p in recent if p.eev is not None]
            spread = (quantile(eev, 0.9) - quantile(eev, 0.1)) if eev else 0
            wet = sum((p.eta or 0) >= self.wet_eta for p in recent) / len(recent)
            if t_dis is not None and t_dis >= self.s.discharge_max:
                harm = f"kuumakaasu {fmt(t_dis, 1)} °C"
            elif appr is not None and b.get("appr_e") is not None and appr >= b["appr_e"] + self.s.approach_max_rise:
                harm = f"höyrystin nälkiintyy (lähestymä {fmt(appr, 1)} K, ennen {fmt(b['appr_e'], 1)} K)"
            elif spread >= max(25.0, 3 * (b.get("eev_spread") or 0) + 10):
                harm = f"EEV heiluu {spread:.0f} askelta"
            elif TYPES[a["type"]]["purpose"] == "tehostus" and wet >= 0.5:
                harm = "imukaasu märkää taas"
            if harm:
                self._revert(a, "haitta", harm, BACKOFF_HARM, out)
                return out + self._phase_write()
        if a["verify_n"] < self.s.verify_min:
            return out
        after = metrics(after_pts, self.wet_eta, self._floors())
        ok, why = self._judge(a, b, after)
        if ok:
            self._accept(a, after, why, out)
            return out
        self._revert(a, "ei_hyotya", why, BACKOFF_NO_GAIN, out)
        return out + self._phase_write()

    def _judge(self, a: dict, b: dict, x: dict):
        purpose = TYPES[a["type"]]["purpose"]
        change = (f"märkänä {pct(b.get('wet_frac'))} → {pct(x.get('wet_frac'))}, näennäinen hyötysuhde "
                  f"{fmt(b.get('eta_med'))} → {fmt(x.get('eta_med'))}")
        if x.get("n", 0) == 0:
            return False, "ei dataa"
        if purpose == "kuivatus":
            better = (x["wet_frac"] <= b["wet_frac"] - 0.3) or (x["eta_med"] <= b["eta_med"] - 0.02)
            if a["type"] == "min_od_down" and x.get("pinned_frac", 1) <= b.get("pinned_frac", 0) - 0.4 \
                    and x["wet_frac"] < b["wet_frac"]:
                better = True
            return better, change if better else f"ei vaikutusta: {change}"
        qual_ok, qual = True, ""
        for f, name in (("lq_w", "vesipuoli"), ("lq_kp", "kylmäpiiri")):
            if b.get(f) and x.get(f):
                ratio = x[f] / b[f]
                qual = f", COP-laatuaste ({name}) {fmt((ratio - 1) * 100, 1)} %"
                limit = 0.97 if purpose == "tehostus" else 1 - self.s.cop_drop_pct / 100
                qual_ok = ratio >= limit
                break
        if purpose == "vakautus":
            ok = bool(x.get("eev_spread") is not None and b.get("eev_spread")
                      and x["eev_spread"] <= 0.6 * b["eev_spread"])
            return ok, f"EEV-vaihtelu {fmt(b.get('eev_spread'), 0)} → {fmt(x.get('eev_spread'), 0)} askelta{qual}"
        dry = x["wet_frac"] <= 0.2 and x["eta_med"] <= self.wet_eta - 0.03
        return dry and qual_ok, change + qual

    def _accept(self, a: dict, after: dict, why: str, out: list):
        key = a["identified"]
        frm, to = a["before_values"].get(key), a["target"]
        a["outcome"] = ["hyväksytty", why, 0]
        self.history.append(self._entry(a, "hyväksytty", why))
        self.action = None
        self.cooldown_until = self.now + COOLDOWN
        if a["type"] == "min_od_up":
            # the valve hunted at the lower minimum: never go below this value again
            self.limits[key] = max(self.limits.get(key, to), to)
            why += f". {short(key)} ei laske enää alle {to}"
        b = a["before"]
        lq = f" Vesipuolen COP-laatuaste {fmt(b.get('lq_w'))} → {fmt(after.get('lq_w'))}."
        if TYPES[a["type"]]["purpose"] == "kuivatus":
            lq += (f" Kylmäpiirin COP-lukema {fmt(b.get('lq_kp'))} → {fmt(after.get('lq_kp'))} (laatuaste): "
                   "märkä imukaasu näyttää sen liian suurena, joten lasku on odotettu.")
        else:
            lq += f" Kylmäpiirin COP-laatuaste {fmt(b.get('lq_kp'))} → {fmt(after.get('lq_kp'))}."
        msg = f"{short(key)}: {frm} → {to} jää voimaan. {why[0].upper() + why[1:]}.{lq}"
        out.append(Log(f"Hyväksytty: {msg}"))
        out.append(Notify("COP-tekoäly: muutos jää voimaan", msg))

    def _revert(self, a: dict, outcome: str, why: str, backoff: float, out: list):
        key = a["identified"] or a["candidates"][a["ci"]]
        a.update(phase="revert", revert_key=key, outcome=[outcome, why, backoff], phase_t=self.now)
        out.append(Log(f"Perutaan {short(key)} → {a['before_values'].get(key)}: {why}"))

    def _abort(self, a: dict, why: str, out: list):
        key = a["identified"] or (a["candidates"][a["ci"]] if a["ci"] < len(a["candidates"]) else None)
        written = key is not None and key in a["before_values"] and self.managed.get(key) not in (
            None, a["before_values"][key])
        if a.get("inflight"):
            # the write result is still coming; restore afterwards through the safety queue
            self._queue_restore(a["write_key"], a["before_values"][a["write_key"]], why)
            a["outcome"] = ["keskeytetty", why, 0]
            self._finish(a, out)
        elif written:
            a.update(phase="revert", revert_key=key, outcome=["keskeytetty", why, 0])
            out.append(Log(f"Keskeytetään ja palautetaan {short(key)} → {a['before_values'][key]}: {why}"))
        else:
            a["outcome"] = ["keskeytetty", why, 0]
            self._finish(a, out)
        self.cooldown_until = max(self.cooldown_until, self.now + 30 * MINUTE)

    def _finish(self, a: dict, out: list):
        outcome, why, backoff = a.get("outcome") or ["valmis", "", 0]
        key = a.get("revert_key") or a.get("identified")
        if backoff and key:
            self.backoff[key] = self.now + backoff
        self.history.append(self._entry(a, outcome, why))
        self.history = self.history[-100:]
        if self.action is a:
            self.action = None
        self.cooldown_until = max(self.cooldown_until, self.now + COOLDOWN)
        if outcome in ("ei_hyotya", "haitta", "ei_varmistettu", "ei_tunnistettu"):
            msg = f"{a['title']}: {why}."
            if key and outcome != "ei_tunnistettu":
                msg += f" {short(key)} palautettiin arvoon {a['before_values'].get(key)}."
            out.append(Log(f"Ei jätetty voimaan: {msg}"))
            out.append(Notify("COP-tekoäly: muutos peruttiin", msg))

    def _entry(self, a: dict, outcome: str, why: str) -> dict:
        key = a.get("identified") or a.get("revert_key") or (
            a["candidates"][a["ci"]] if a["ci"] < len(a["candidates"]) else None)
        return {"id": a["id"], "t": self.now, "type": a["type"], "key": key,
                "from": a["before_values"].get(key) if key else None, "to": a.get("target"),
                "outcome": outcome, "why": why}

    # ------------------------------------------------------------------ safety
    def _queue_restore(self, key: str, value: int, reason: str):
        self.queue = [q for q in self.queue if q["key"] != key]
        self.queue.append({"key": key, "value": value, "reason": reason, "tries": 0, "next_t": 0.0,
                           "inflight": False})

    def _drain(self) -> list:
        for q in self.queue:
            if q["inflight"]:
                if self.now - q.get("inflight_t", self.now) <= 10 * MINUTE:
                    return []
                q["inflight"] = False  # result never came: try again
        for q in self.queue:
            p = self.params.get(q["key"])
            if q["next_t"] <= self.now and p is not None and p.available:
                q["inflight"], q["inflight_t"] = True, self.now
                return [Write(q["key"], q["value"], "restore", q["reason"])]
        return []

    def _restore_result(self, write: Write, ok: bool, error: str | None, out: list):
        for q in list(self.queue):
            if q["key"] != write.key or not q["inflight"]:
                continue
            q["inflight"] = False
            if ok:
                self.queue.remove(q)
                self._set_value(write.key, write.value)
                out.append(Log(f"Palautettu {short(write.key)} → {write.value}"))
            else:
                q["tries"] += 1
                q["next_t"] = self.now + 2 * MINUTE
                if q["tries"] >= 5:
                    self.queue.remove(q)
                    out.append(Notify(
                        "COP-tekoäly: palautus epäonnistui",
                        f"{short(write.key)} pitäisi palauttaa arvoon {write.value}, mutta laite ei "
                        f"vahvistanut kirjoitusta ({error}). Aseta arvo käsin Mango Control -näkymässä.",
                        "palautus"))
            return

    def _on_fault(self, out: list):
        self.paused_until = self.now + FAULT_PAUSE
        self.pause_reason = "vika- tai hälytystila"
        changed = bool(self.managed) or self.action is not None
        out.extend(self.rollback_all(self.now, "vika- tai hälytystila"))
        out.append(Notify(
            "COP-tekoäly: tauko",
            "Lämpöpumpussa on vika- tai hälytystila. Tekoäly pitää 24 tunnin tauon"
            + (" ja palauttaa muuttamansa arvot." if changed else "."), "tauko"))

    def _external_changes(self, out: list):
        a = self.action
        busy = {q["key"] for q in self.queue if q["inflight"]}
        if a is not None and a.get("inflight"):
            busy.add(a["write_key"])
        for key, value in list(self.managed.items()):
            p = self.params.get(key)
            if key in busy or not p or not p.available or p.value is None or p.value == value:
                continue
            self.managed.pop(key, None)
            self.originals[key] = p.value
            self.backoff[key] = self.now + 7 * DAY
            self.queue = [q for q in self.queue if q["key"] != key]
            out.append(Log(f"{short(key)} muutettiin muualta {value} → {p.value}. Tekoäly pitää arvon "
                           "uutena lähtökohtana eikä koske siihen viikkoon."))
            if a is not None and key in (a.get("identified"), a.get("write_key"), a.get("revert_key")):
                a["outcome"] = ["keskeytetty", "arvoa muutettiin muualta", 0]
                self.history.append(self._entry(a, "keskeytetty", "arvoa muutettiin muualta"))
                self.action = a = None

    def _resume(self, out: list):
        for q in self.queue:
            q["inflight"] = False
        a = self.action
        if a is None:
            return
        key = a.get("write_key") if a.get("inflight") else None
        a["inflight"] = False
        if a["phase"] in ("probe_write", "apply_write") and key:
            p = self.params.get(key)
            if p and p.value == a["target"]:
                self.originals.setdefault(key, a["before_values"][key])
                self._set_value(key, a["target"])
                a.update(phase="revert", revert_key=key, outcome=["keskeytetty", "uudelleenkäynnistys", 0])
            else:
                a["outcome"] = ["keskeytetty", "uudelleenkäynnistys", 0]
                self._finish(a, out)
        elif a["phase"] in ("probe_wait", "probe_revert"):
            k = a["candidates"][a["ci"]]
            a.update(phase="revert", revert_key=k, outcome=["keskeytetty", "uudelleenkäynnistys", 0])
        elif a["phase"] == "verify":
            a.update(verify_n=0, settle_until=self.now + SETTLE, deadline=self.now + DAY)
            out.append(Log(f"Jatketaan {short(a['identified'])} varmistusta uudelleenkäynnistyksen jälkeen"))

    # ------------------------------------------------------------------ status
    def _update_status(self):
        a = self.action
        if self.queue:
            text = "Palauttaa arvoja: " + ", ".join(f"{short(q['key'])} → {q['value']}" for q in self.queue)
        elif a is not None:
            key = a.get("identified") or a.get("write_key") or (
                a["candidates"][a["ci"]] if a["ci"] < len(a["candidates"]) else "")
            frm = a["before_values"].get(key)
            if a["phase"] == "verify":
                text = (f"Varmistaa {short(key)} {frm} → {a['target']} "
                        f"({a['verify_n']}/{self.s.verify_min} min)")
            elif a["phase"] in ("probe_revert", "revert"):
                text = f"Palauttaa {short(key)} → {frm}"
            else:
                text = f"Testaa {short(key)} {frm} → {a['target']} ({a['ci'] + 1}/{len(a['candidates'])})"
        elif self.mode == MODE_OFF:
            text = "Pois päältä"
        elif self.paused_until > self.now:
            text = f"Tauko: {self.pause_reason}"
        elif self.pending:
            text = "Ehdotus odottaa hyväksyntää"
        elif self.approved:
            text = "Ehdotus hyväksytty, odottaa samaa tilannetta"
        else:
            n, need = self.steady_minutes()
            if n < need:
                text = f"Odottaa tasaista käyntiä ({n}/{need} min)"
                why = self.waiting_reason()
                if why:
                    text += f": {why}"
            elif self.suggestion and self.mode == MODE_OBSERVE:
                text = f"Tarkkailee. Tekisin: {self.suggestion['title']}"
            else:
                text = "Tarkkailee" if self.mode == MODE_OBSERVE else "Valvoo"
        self.status_text = text[:250]

    def waiting_reason(self) -> str | None:
        """Why the latest minute does not count as steady running (None = it does)."""
        return why_text(self.points[-1]) if self.points else None

    def status(self) -> dict:
        a = self.action
        return {
            "mode": self.mode,
            "text": self.status_text,
            "action": None if a is None else {
                "tyyppi": a["type"], "vaihe": a["phase"], "parametri": a.get("identified") or a.get("write_key"),
                "ehdokkaat": [short(c) for c in a["candidates"]], "testattu": a["ci"],
                "varmistus_min": a["verify_n"], "syy": a["reason"]},
            "pending": None if not self.pending else {"otsikko": self.pending["title"],
                                                      "syy": self.pending["reason"]},
            "approved": None if not self.approved else self.approved["title"],
            "suggestion": None if not self.suggestion else self.suggestion["title"],
            "writes_24h": self._writes_24h(),
            "writes_total": self.writes_total,
            "changed": {short(k): {"alkuperäinen": self.originals.get(k), "nyt": v}
                        for k, v in self.managed.items()},
            "zones": [{"parametri": short(h["key"]), "ulko": round(h["t_out"], 1) if h.get("t_out") is not None
                       else None, "menovesi": round(h["t_sup"], 1) if h.get("t_sup") is not None else None}
                      for h in self.hits[-12:]],
            "history": [{**h, "key": short(h["key"]) if h.get("key") else None} for h in self.history[-8:]],
            "failed": {short(k): v["error"] for k, v in self.failed.items()},
            "paused_until": self.paused_until if self.paused_until > self.now else None,
            "cooldown_until": self.cooldown_until if self.cooldown_until > self.now else None,
            "budget_full": not self._budget_ok(),
        }
