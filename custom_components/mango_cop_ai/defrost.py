"""Defrost watch and control. Pure Python: the brain calls it once a minute.

1. Frost between defrosts. Frost insulates the coil and blocks the air, so the evaporator approach
   (coil air - evaporating temperature) grows. The clean-coil approach is measured on steady
   minutes 12-60 compressor minutes after each defrost and scaled with compressor speed (and fan
   speed when known). The growth since then, plus the frost that grew before the reference at the
   same measured rate (at most 2.5 K), is the frost estimate in K. A lasting step that frost cannot
   make in a minute (silent mode, fan change) moves the reference instead.
   Defrost need 100 % = the AI's own threshold, frost_target_k (4 K) to start with.
2. Demand defrost. When the need reaches 100 % the AI starts a defrost with the owner's forced
   defrost script (e.g. script.sulatus_pakotettu_moduuli_0). Guards: 25 compressor minutes since
   the last defrost, air at most +8 °C, steady running, 24 per day. The defrost sensor must turn
   on within 6 minutes; two misses stop forced defrosts for a day. The threshold calibrates
   itself: +0.5 K when a forced defrost melts no longer than a clean coil, -0.5 K when the
   controller's own defrost turns out late before the AI thought it was needed (2.5-8 K).
3. Every defrost is evaluated when it ends:
     turha       almost clean coil (growth <= frost_clean_k and a short melt)
     sopiva      in between
     myöhäinen   heavy frost (growth >= 1.5 x threshold, or COP loss >= 15 %) or a long melt
     kesken      ran to the controller's maximum time without reaching the end temperature
     epäselvä    no clean reference in this cycle (e.g. right after a restart)
   together with what started it: the AI, the interval (compressor minutes since the previous
   defrost matched Dfr.Interval or the band's Dfr.Inte. n) or a temperature condition.
4. The controller's own interval is changed one step at a time, only the one that started the
   defrosts (bands 1 warm .. 5 cold from Dfr.Env.T.1-4):
     dfr_interval_up    +5 min after 3 of the last 4 defrosts of a band were turha and started by
                        the same interval, none late
     dfr_interval_down  -5 min after 2 of the last 3 were myöhäinen/kesken and waited for it (one is
                        enough if the AI itself had raised that interval)
   A change is verified by the next defrosts of the band: the interval has to move with the
   setting and no defrost may become late or incomplete. Otherwise the previous value comes back.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from .analysis import Point, median
from .const import (
    DFR_BANDS,
    DFR_COIL_END,
    DFR_FROST_D1,
    DFR_INTE,
    DFR_INTERVAL,
    DFR_INTERVALS,
    DFR_KEYS,
    DFR_SMART,
    DFR_SURE,
    DFR_TIME,
    MODE_AUTO,
    MODE_OBSERVE,
    MODE_OFF,
    MODE_PROPOSE,
    MODES,
    initial_mode,
)

MINUTE, HOUR, DAY = 60.0, 3600.0, 86400.0
LOSS_FACTOR = 1.5  # real cycles lose ~1.5x the Carnot share per K of lower evaporating temperature
FAN_EXP = 0.7  # coil conductance ~ airflow^0.7
VERIFY_DEFROSTS = 2
VERIFY_DAYS = 7 * DAY
BACKOFF_NO_GAIN = 7 * DAY
BACKOFF_HARM = 14 * DAY
NOTE_REPEAT = 6 * HOUR
OBSERVE_LOG_EVERY = 6 * HOUR
REF_POINTS, REF_FROM, REF_UNTIL = 8, 12.0, 60.0  # clean reference: steady points, compressor minutes
MAX_BACKFILL = 2.5  # K, most frost credited to the minutes before the reference was measured
FORCE_MIN_RUN = 25.0  # compressor minutes after a defrost before the AI may start another
FORCE_MAX_AIR = 8.0  # °C, no frost above this
FORCE_PER_DAY = 24
FORCE_CONFIRM = 6 * MINUTE  # the defrost sensor must turn on within this
FORCE_PROPOSAL_TTL = 45 * MINUTE
FORCE_RETRY = 10 * MINUTE
TARGET_MIN, TARGET_MAX, TARGET_STEP = 2.5, 8.0, 0.5
VERDICTS = ("turha", "sopiva", "myöhäinen", "kesken", "epäselvä")


def short(key: str) -> str:
    return key.split("|", 1)[-1]


def fmt(value, digits=1, unit=""):
    if value is None:
        return "–"
    text = f"{value:.{digits}f}".replace(".", ",")
    return f"{text} {unit}".rstrip()


def fmt_s(seconds) -> str:
    if seconds is None:
        return "–"
    seconds = int(round(seconds))
    return f"{seconds // 60} min {seconds % 60:02d} s"


def _r(v, digits=1):
    return None if v is None else round(v, digits)


class Defrost:
    def __init__(self, host, stored: dict | None = None, mode: str | None = None):
        """mode: the defrost mode written in configuration.yaml (None = not written)."""
        st = stored or {}
        self.host = host  # the Brain: settings, parameters, write budget, originals, restore queue
        self.mode, self.yaml_mode = initial_mode(st, mode)
        self.records: list[dict] = list(st.get("records", []))[-150:]
        self.action: dict | None = st.get("action")
        self.cycle: dict = st.get("cycle") or self._new_cycle(None)
        self.current: dict | None = st.get("current")
        self.misses: list[dict] = list(st.get("misses", []))[-50:]
        self.observed: dict[str, float] = dict(st.get("observed", {}))
        self.notified: dict[str, float] = dict(st.get("notified", {}))
        base = float(host.s.frost_target_k)
        self.target_base = float(st.get("target_base", base))
        self.target = float(st.get("target", base))
        if self.target_base != base:  # the owner changed the setting: start again from it
            self.target_base = self.target = base
        self.forced: dict | None = st.get("forced")
        self.force_log: list[float] = [float(t) for t in st.get("force_log", [])]
        self.force_fail = int(st.get("force_fail", 0))
        self.force_block = float(st.get("force_block", 0.0))
        self.force_retry = float(st.get("force_retry", 0.0))
        self.last_slope: float | None = st.get("last_slope")  # frost growth K/min of the previous cycle
        self.force_ok = False  # the forced defrost script exists (set every minute)
        self.proposal: dict | None = None
        self.suggestion: dict | None = None
        self.view: dict = {}
        self.resume = True

    def to_dict(self) -> dict:
        return {"mode": self.mode, "yaml_mode": self.yaml_mode, "records": self.records, "action": self.action,
                "cycle": self.cycle,
                "current": self.current, "misses": self.misses, "observed": self.observed,
                "notified": self.notified, "target": self.target, "target_base": self.target_base,
                "forced": self.forced, "force_log": self.force_log, "force_fail": self.force_fail,
                "force_block": self.force_block, "force_retry": self.force_retry, "last_slope": self.last_slope}

    # ------------------------------------------------------------------ helpers
    @property
    def s(self):
        return self.host.s

    @property
    def now(self) -> float:
        return self.host.now

    def _val(self, key: str) -> int | None:
        p = self.host.params.get(key)
        return p.value if p is not None and p.available and p.value is not None else None

    def band_of(self, t: float | None) -> int | None:
        """Ambient band 1 (warm) .. 5 (cold) of the controller's Dfr.Env.T.1-4 table."""
        edges = [self._val(k) for k in DFR_BANDS]
        if t is None or any(e is None for e in edges):
            return None
        for i, edge in enumerate(sorted(edges, reverse=True)):
            if t > edge:
                return i + 1
        return 5

    def _window_hi(self) -> float:
        sure = self._val(DFR_SURE)
        return min(max(sure if sure is not None else 3, 0), 10) + 3.0

    def _new_cycle(self, start: float | None) -> dict:
        return {"start": start, "run": 0.0, "last_t": start, "ref": None, "ref_pts": [], "recent": [],
                "recent_fin": [], "growth": None, "frost": None, "frost_fin": None, "backfill": 0.0,
                "trend": [], "e_frost": 0.0, "loss": None, "lift": None, "cop": [], "air": [], "jump": [],
                "other_speed": [], "last_valid": None, "rebased": 0, "said_force": False}

    def _note_due(self, code: str, every: float = NOTE_REPEAT) -> bool:
        if self.mode == MODE_OFF or self.now - self.notified.get(code, 0.0) < every:
            return False
        self.notified[code] = self.now
        return True

    def _need(self) -> float | None:
        f = self.cycle.get("frost")
        return None if f is None else max(f, 0.0) / self.target * 100

    def _forced_24h(self) -> int:
        return sum(1 for t in self.force_log if t > self.now - DAY)

    # ------------------------------------------------------------------ the minute
    def observe(self, p: Point) -> list:
        out: list = []
        c = self.cycle
        self.force_ok = bool(p.force_ok)
        self.force_log = [t for t in self.force_log if t > self.now - 2 * DAY]
        if self.resume:
            self.resume = False
            # a restart longer than a few minutes loses the run-time count of this cycle
            if c.get("last_t") is not None and self.now - c["last_t"] > 10 * MINUTE:
                c["start"] = None
            self.resume_action(out)
        h = self.host
        if h.pending and h.pending.get("type") == "dfr_force" and (
                p.defrost or self.now - h.pending["created"] > FORCE_PROPOSAL_TTL):
            h.pending = None
            out.append(_dismiss("ehdotus"))
        if p.defrost:
            if self.current is None:
                self._started(p, out)
            self._defrost_minute(p)
        else:
            if self.current is not None:
                self._ended(p, out)
            self._running_minute(p, out)
            f = self.forced
            if f and f["status"] == "pending" and self.now - f["t"] > FORCE_CONFIRM:
                self._force_failed("sulatus ei käynnistynyt 6 minuutissa", out)
        self.cycle["last_t"] = self.now
        self._update_view(p)
        return out

    def _running_minute(self, p: Point, out: list):
        c = self.cycle
        if not p.comp_on:
            return
        c["run"] += 1.0
        if p.t_air is not None:
            c["air"] = (c["air"] + [p.t_air])[-5:]
        if p.cop_kp is not None and p.stable:
            c["cop"] = (c["cop"] + [p.cop_kp])[-30:]
        ref = c["ref"]
        usable = self._steady(p)
        if ref is None and usable and c["start"] is not None and REF_FROM <= c["run"] <= REF_UNTIL:
            c["ref_pts"].append([p.t_air - p.te, p.rps, None if p.t_fin is None else p.t_air - p.t_fin,
                                 p.cop_kp, p.fan, c["run"]])
            mid = median(x[1] for x in c["ref_pts"])
            pts = [x for x in c["ref_pts"] if abs(x[1] - mid) <= 0.1 * mid]  # one speed only (no boosts)
            if len(pts) >= REF_POINTS:
                ref = c["ref"] = {
                    "appr": median(x[0] for x in pts), "rps": median(x[1] for x in pts),
                    "fin": median(x[2] for x in pts), "cop": median(x[3] for x in pts),
                    "fan": median(x[4] for x in pts), "run": c["run"], "run_mid": median(x[5] for x in pts),
                }
                c["ref_pts"] = []
                c["growth"] = c["frost"] = 0.0
                c["frost_fin"], c["last_valid"] = None, c["run"]
        measured = False
        if ref is not None and usable:
            scale = p.rps / ref["rps"]
            fan_ok = True
            if p.fan and ref.get("fan"):
                fr = ref["fan"] / p.fan
                fan_ok = 0.6 <= fr <= 1.6
                scale *= fr ** FAN_EXP
            if 0.7 <= p.rps / ref["rps"] <= 1.3 and fan_ok:
                self._frost_point(p, ref, scale)
                measured = True
                c["other_speed"] = []
            elif c["frost"] is not None:
                self._other_speed(p, ref)
        slope = c.get("slope") or self.last_slope
        if not measured and c["growth"] is not None and c["last_valid"] is not None and slope and slope > 0 \
                and c["run"] - c["last_valid"] <= 30:
            # oil-return boost or speed change: not comparable now, frost keeps growing at the rate seen
            c["frost"] = c["growth"] + slope * (c["run"] - c["last_valid"]) + c["backfill"]
        if p.tc is not None and p.te is not None:
            c["lift"] = p.tc - p.te
        if c["frost"] is not None and c["lift"]:
            loss = LOSS_FACTOR * max(c["frost"], 0.0) / max(c["lift"], 15.0)
            c["loss"] = loss
            if p.p_el:
                c["e_frost"] += p.p_el * loss / 60.0
        need = self._need()
        if need is not None and need >= 200 and self._note_due("huurre"):
            out.append(_notify(
                "Kenno huurteessa",
                f"Höyrystimen lähestymä on kasvanut {fmt(c['frost'])} K puhtaan kennon tasosta (sulatustarve "
                f"{need:.0f} %), eikä sulatusta ole tullut. Arvioitu COP-häviö {fmt((c['loss'] or 0) * 100, 0)} %."
                + (f" Tekoäly ei sulattanut: {why}." if (why := self._force_blocked(p) or (
                    None if self.mode == MODE_AUTO else f"sulatustila on {self.mode}")) else "")
                + " Tarkista kenno ja sulatuksen toiminta.", "huurre"))

    def _other_speed(self, p: Point, ref: dict):
        """A lasting new compressor or fan speed: measure from it, continuing the frost estimate."""
        c = self.cycle
        c.setdefault("other_speed", []).append([p.t_air - p.te, p.rps, p.fan])
        pts = c["other_speed"]
        if len(pts) < 10:
            return
        mid = median(x[1] for x in pts)
        if max(x[1] for x in pts) - min(x[1] for x in pts) > max(3.0, 0.1 * mid):
            c["other_speed"] = pts[-5:]  # still changing
            return
        growth = c["frost"] - c["backfill"]
        ref.update(appr=median(x[0] for x in pts) - growth, rps=mid,
                   fan=median(x[2] for x in pts) if all(x[2] for x in pts) else ref.get("fan"))
        if ref.get("fin") is not None and c["frost_fin"] is not None:
            ref["fin"] = None  # the fin reference cannot be moved reliably; the evaporator one carries on
            c["frost_fin"] = None
        c["growth"], c["recent"], c["recent_fin"], c["jump"] = growth, [growth], [], []
        c["last_valid"], c["other_speed"] = c["run"], []
        c["rebased"] += 1

    def _steady(self, p: Point) -> bool:
        """Comparable running minute: no defrost or fault, compressor settled and speed steady."""
        if not p.comp_on or p.defrost or p.fault or p.te is None or p.t_air is None or not p.rps or p.rps < 12:
            return False
        if (p.comp_on_min or 0) < 8:
            return False
        pts = [x.rps for x in list(self.host.points)[-5:-1] if x.comp_on and not x.defrost and x.rps]
        return len(pts) >= 3 and abs(p.rps - median(pts)) <= 2.0

    def _frost_point(self, p: Point, ref: dict, scale: float):
        c = self.cycle
        raw = (p.t_air - p.te) - ref["appr"] * scale
        prev, last = c["growth"], c["last_valid"]
        consecutive = last is not None and c["run"] - last <= 3
        if last is not None and not consecutive and prev is not None:
            # after a gap the frost has grown: start the smoothing again from the extrapolated level
            prev = c["growth"] = c["frost"] - c["backfill"]
            c["recent"], c["recent_fin"] = [], []
        if prev is not None and consecutive and abs(raw - prev) > 2.0:
            # frost cannot grow 2 K in a minute: a fan, silent-mode or flow change. Hold, and if the step
            # lasts, move the reference so that the frost estimate continues from where it was.
            c["jump"].append(raw)
            c["last_valid"] = c["run"]
            j = c["jump"]
            if len(j) >= 5 and all(abs(x - prev) > 1.5 for x in j) and max(j) - min(j) < 1.5:
                ref["appr"] += (median(j) - prev) / scale
                c["jump"], c["rebased"] = [], c["rebased"] + 1
            return
        c["jump"] = []
        c["last_valid"] = c["run"]
        c["recent"] = (c["recent"] + [raw])[-5:]
        c["growth"] = median(c["recent"])
        c["trend"] = (c["trend"] + [[c["run"], c["growth"]]])[-40:]
        # frost grew already before the reference was measured: credit it at the rate seen since
        # (the previous cycle's rate until this one has enough points)
        tr = c["trend"]
        slope = _slope(tr) if len(tr) >= 8 and tr[-1][0] - tr[0][0] >= 10 else self.last_slope
        if slope is not None and slope > 0:
            c["slope"] = slope
            c["backfill"] = min(MAX_BACKFILL, slope * (ref.get("run_mid") or ref["run"]))
        c["frost"] = c["growth"] + c["backfill"]
        if p.t_fin is not None and ref.get("fin") is not None:
            c["recent_fin"] = (c["recent_fin"] + [(p.t_air - p.t_fin) - ref["fin"] * scale])[-5:]
            c["frost_fin"] = median(c["recent_fin"]) + c["backfill"]

    def _started(self, p: Point, out: list):
        c = self.cycle
        t0 = p.defrost_changed if p.defrost_changed and p.defrost_changed > self.now - 3 * MINUTE else self.now
        known = c["start"] is not None
        t_air = median(c["air"]) if c["air"] else p.t_air
        band = self.band_of(t_air)
        run = c["run"] if known else None
        wall = (t0 - c["start"]) / 60 if known else None
        last = next((x for x in reversed(self.host.points) if not x.defrost and x.comp_on), None)
        excess = None
        if last is not None and None not in (last.t_air, last.t_fin, last.ffd):
            excess = (last.t_air - last.t_fin) - last.ffd
        f = self.forced
        if f and f["status"] == "pending" and t0 - f["t"] <= FORCE_CONFIRM:
            trigger = {"type": "tekoäly"}
            f["status"] = "started"
            self.force_fail = 0
        else:
            trigger = self._trigger(run, wall, band)
            prev = self.records[-1] if self.records else None
            if prev and prev["trigger"]["type"] == "tekoäly" and prev["verdict"] != "kesken" \
                    and t0 - prev["end"] < 10 * MINUTE:
                self.force_block = self.now + DAY
                out.append(_notify(
                    "Pakkosulatus pysäytetty",
                    "Uusi sulatus alkoi heti tekoälyn pakkosulatuksen jälkeen, vaikka edellinen ei jäänyt kesken. "
                    "Pakkosulatusskripti saattaa jättää pakotuksen päälle. Tekoäly ei käytä sitä vuorokauteen: "
                    "tarkista skripti.", "sulatus"))
        self.current = {
            "t": t0, "t_air": t_air, "t_out": p.t_out, "band": band, "run": run, "wall": wall,
            "frost": c["frost"], "frost_fin": c["frost_fin"], "loss": c["loss"], "need": self._need(),
            "target": self.target, "e_frost": c["e_frost"],
            "cop": median(c["cop"]) if c["cop"] else (c["ref"] or {}).get("cop"),
            "excess": excess, "trigger": trigger, "force_ok": self.force_ok,
            "params": {short(k): v for k in (DFR_INTERVAL, *DFR_INTE, DFR_TIME, DFR_COIL_END, DFR_SMART,
                                             DFR_FROST_D1) if (v := self._val(k)) is not None},
            "e_el": 0.0, "q": 0.0, "flow_ok": False, "fin_max": None, "minutes": 0,
        }

    def _trigger(self, run, wall, band) -> dict:
        """What started this defrost: the interval (which one) or a temperature condition."""
        if run is None:
            return {"type": "tuntematon"}
        hi = self._window_hi()
        if band is None:
            cands = list(DFR_INTE)
        else:
            cands = [DFR_INTE[b - 1] for b in (band, band - 1, band + 1) if 1 <= b <= 5]
        cands.append(DFR_INTERVAL)
        matches = []
        lowest = None
        for key in cands:
            v = self._val(key)
            if v is None or v < 10:
                continue
            lowest = v if lowest is None else min(lowest, v)
            for basis, x in (("käynti", run), ("aika", wall)):
                if x is not None and v - 2 <= x <= v + hi:
                    matches.append((key, v, basis))
                    break
        if matches:
            key, v, basis = matches[0]
            return {"type": "väli", "key": key, "value": v, "basis": basis,
                    "alt": [m[0] for m in matches[1:] if m[0] != key]}
        if lowest is not None and run < lowest - 2 and (wall is None or wall < lowest - 2):
            return {"type": "muu"}  # earlier than any interval: emergency or low-pressure defrost
        return {"type": "ehto"}

    def _defrost_minute(self, p: Point):
        d = self.current
        d["minutes"] += 1
        if p.p_el:
            d["e_el"] += p.p_el / 60.0
        if p.flow is not None and p.t_sup is not None and p.t_ret is not None and p.flow > 0:
            d["flow_ok"] = True
            # heat taken from the water: l/h -> kg/s x 4.186 kJ/kgK x (return - supply), kWh per minute
            d["q"] += p.flow / 3600 * 4.186 * max(p.t_ret - p.t_sup, 0.0) * self.s.water_factor / 60.0
        if p.t_fin is not None:
            d["fin_max"] = p.t_fin if d["fin_max"] is None else max(d["fin_max"], p.t_fin)

    def _typical(self, band) -> float | None:
        vals = [r["dur"] for r in self.records[-40:] if r["band"] == band
                and r["verdict"] in ("sopiva", "myöhäinen")][-10:]
        return median(vals) if len(vals) >= 3 else None

    def _clean_duration(self) -> float:
        """How long the controller takes to defrost a clean coil (its own turha defrosts)."""
        vals = [r["dur"] for r in self.records if r["verdict"] == "turha" and r["trigger"]["type"] != "tekoäly"
                and r["t"] > self.now - 30 * DAY][-10:]
        return median(vals) if len(vals) >= 3 else 150.0

    def _ended(self, p: Point, out: list):
        d, self.current = self.current, None
        t1 = p.defrost_changed if p.defrost_changed and p.defrost_changed > d["t"] else self.now
        dur = max(t1 - d["t"], 0.0)
        if dur < 60 and d["minutes"] <= 1 and d["trigger"]["type"] != "tekoäly":
            # a real defrost takes at least 90 s (Dfr.Time minimum): a flickering status bit, keep the cycle
            out.append(_log(f"Sulatussignaali {dur:.0f} s ohitettiin: liian lyhyt ollakseen sulatus."))
            return
        cop = d["cop"] if d["cop"] and d["cop"] > 1 else 3.0
        e_def = d["e_el"] + (d["q"] / cop if d["flow_ok"] else d["e_el"] * 2.0 / cop)
        max_s, end_t = self._val(DFR_TIME), self._val(DFR_COIL_END)
        frost, loss, trig = d["frost"], d["loss"], d["trigger"]
        target, clean = d["target"], self.s.frost_clean_k
        typ = self._typical(d["band"])
        at_max = max_s is not None and dur >= max_s - 20
        if at_max and d["fin_max"] is not None and end_t is not None:
            at_max = d["fin_max"] < end_t - 1  # the end temperature was not reached
        elif at_max:
            at_max = frost is not None and frost >= target  # no fin sensor: only with heavy frost
        long_melt = dur >= (max(540.0, 1.6 * typ) if typ else 600.0) and not (max_s and dur >= max_s - 20)
        short_melt = dur <= (max(150.0, 0.7 * typ) if typ else 150.0)
        forced = trig["type"] == "tekoäly"
        clean_dur = self._clean_duration()
        if at_max:
            verdict, why = "kesken", f"kesti maksimiajan {fmt_s(max_s)}, loppulämpöä ei saavutettu"
        elif forced and dur <= clean_dur * 1.2 + 15:
            verdict = "turha"
            why = (f"pakkosulatus, mutta sulatus kesti vain {fmt_s(dur)} kuten puhtaalla kennolla: huurretta oli "
                   f"vähemmän kuin arvio {fmt(frost)} K")
        elif frost is None:
            verdict, why = "epäselvä", "puhtaan kennon tasoa ei ehditty mitata tässä jaksossa"
        elif frost >= 1.5 * target or (loss or 0) >= 0.15 or long_melt:
            verdict = "myöhäinen"
            why = (f"huurretta {fmt(frost)} K, arvioitu COP-häviö {fmt((loss or 0) * 100, 0)} %"
                   + (f", sulatus kesti {fmt_s(dur)}" if long_melt else ""))
        elif frost <= clean and short_melt:
            verdict, why = "turha", f"kenno oli lähes puhdas (huurretta {fmt(frost)} K)"
        elif frost <= clean:
            verdict, why = "epäselvä", f"huurretta vain {fmt(frost)} K, mutta sulatus kesti {fmt_s(dur)}"
        else:
            verdict, why = "sopiva", f"huurretta {fmt(frost)} K"
        rec = {
            "t": d["t"], "end": t1, "dur": round(dur), "t_air": _r(d["t_air"]), "t_out": _r(d["t_out"]),
            "band": d["band"], "run": _r(d["run"], 0), "wall": _r(d["wall"], 0), "frost": _r(frost, 2),
            "frost_fin": _r(d["frost_fin"], 2), "loss": _r(loss, 3), "need": _r(d["need"], 0),
            "target": d["target"], "e_frost": _r(d["e_frost"], 3), "e_el": _r(d["e_el"], 3),
            "e_def": _r(e_def, 3), "excess": _r(d["excess"]), "fin_max": _r(d["fin_max"]), "trigger": trig,
            "verdict": verdict, "why": why, "params": d["params"], "force_ok": d["force_ok"],
        }
        self.records.append(rec)
        self.records = self.records[-150:]
        if self.cycle.get("slope") is not None and d["run"] is not None:
            self.last_slope = self.cycle["slope"]
        self.cycle = self._new_cycle(t1)
        if forced:
            self.forced = None
        out.append(_log(self.describe(rec)))
        self._calibrate(rec, out)
        if verdict == "kesken" and self._note_due("sulatus"):
            out.append(_notify("Sulatus jäi kesken", self.describe(rec) + " Jos tämä toistuu, kennoon voi "
                               "kertyä jäätä: tarkista kenno ja sulatusasetukset.", "sulatus"))
        if self.action is not None:
            self._verify(rec, out)
        else:
            self._consider(rec, out)

    def _calibrate(self, rec: dict, out: list):
        """Keep the AI's own defrost threshold honest against what the melts show."""
        if self.mode != MODE_AUTO or not self.s.enable_defrost:
            return
        old = self.target
        if rec["trigger"]["type"] == "tekoäly" and rec["verdict"] == "turha":
            self.target = min(TARGET_MAX, self.target + TARGET_STEP)
            why = "pakkosulatus tuli turhan aikaisin"
        elif rec["trigger"]["type"] != "tekoäly" and rec["verdict"] in ("myöhäinen", "kesken") \
                and rec["force_ok"] and (rec["need"] or 0) < 100:
            self.target = max(TARGET_MIN, self.target - TARGET_STEP)
            why = "säätimen sulatus oli myöhäinen ennen kuin tekoäly arvioi sen tarpeelliseksi"
        else:
            return
        if self.target != old:
            out.append(_log(f"Sulatus: {why}. Oma huurrekynnys {fmt(old)} → {fmt(self.target)} K."))

    def describe(self, r: dict) -> str:
        trig = r["trigger"]
        started = {
            "väli": lambda: f"Laukaisi {short(trig['key'])} ({trig['value']} min).",
            "ehto": lambda: "Laukaisi lämpötilaehto.",
            "muu": lambda: "Alkoi ennen sulatusväliä (hätä- tai painesulatus).",
            "tekoäly": lambda: "Tekoälyn pakkosulatus.",
        }.get(trig["type"], lambda: "")()
        band = f" (kaista {r['band']})" if r.get("band") else ""
        run = f", käyntiä edellisestä {r['run']:.0f} min" if r.get("run") is not None else ""
        return (f"Sulatus {fmt_s(r['dur'])}{run}, ulkona {fmt(r['t_air'])} °C{band}: {r['verdict']} – "
                f"{r['why']}. {started}").strip()

    # ------------------------------------------------------------------ demand defrost
    def _force_blocked(self, p: Point) -> str | None:
        """Why a forced defrost is not allowed now, or None."""
        h, c = self.host, self.cycle
        if not self.s.enable_defrost:
            return "sulatusohjaus pois asetuksista"
        if not self.force_ok:
            return "pakkosulatusskriptiä ei löydy"
        if self.force_block > self.now:
            return "pakkosulatus tauolla"
        if self.force_retry > self.now:
            return "edellinen pakkosulatus ei käynnistynyt, odotetaan"
        if h.paused_until > self.now or p.fault:
            return "vika- tai hälytystila"
        if p.defrost or self.current is not None or (self.forced and self.forced["status"] == "pending"):
            return "sulatus jo käynnissä"
        if c["start"] is None or c["run"] < FORCE_MIN_RUN:
            return f"alle {FORCE_MIN_RUN:.0f} min käyntiä edellisestä sulatuksesta"
        if not p.comp_on or (p.comp_on_min or 0) < 10:
            return "kompressori ei käy tasaisesti"
        if p.t_air is None or p.t_air > FORCE_MAX_AIR:
            return f"ulkoilma yli {FORCE_MAX_AIR:.0f} °C"
        if self._forced_24h() >= FORCE_PER_DAY:
            return f"{FORCE_PER_DAY} pakkosulatusta vuorokaudessa"
        a = h.action
        if a is not None and a.get("phase") in ("probe_write", "probe_wait", "probe_revert", "apply_write", "revert") \
                and (self._need() or 0) < 130:
            return "EEV-testi kesken, sulatetaan sen jälkeen"
        return None

    def demand(self, p: Point) -> list:
        """Start a defrost when the coil needs it (mode decides: log, ask or do)."""
        need = self._need()
        if need is None or need < 100 or self.mode == MODE_OFF:
            return []
        if self._force_blocked(p) or self.host.backoff.get("type:dfr_force", 0) > self.now:
            return []
        c = self.cycle
        reason = (f"Kennossa on huurretta {fmt(c['frost'])} K (sulatustarve {need:.0f} %, arvioitu COP-häviö "
                  f"{fmt((c['loss'] or 0) * 100, 0)} %), käyntiä edellisestä sulatuksesta {c['run']:.0f} min.")
        if self.mode == MODE_OBSERVE:
            if not c["said_force"]:
                c["said_force"] = True
                return [_log(f"Sulatus, tarkkailu: ei tehty mitään. Tekisin pakkosulatuksen nyt. {reason}")]
            return []
        h = self.host
        if self.mode == MODE_PROPOSE:
            if not h.pending and not h.approved and not c["said_force"]:
                c["said_force"] = True
                h.pending = {"type": "dfr_force", "title": "Pakkosulatus nyt", "reason": reason,
                             "created": self.now}
                return [_notify("COP-tekoälyn sulatusehdotus",
                                f"{reason}\n\nKäynnistä sulatus painikkeella **Hyväksy ehdotus**. Ehdotus vanhenee "
                                "45 minuutissa tai kun säädin sulattaa itse.", "ehdotus")]
            return []
        return self._force(reason)

    def force_now(self, p: Point | None) -> list:
        """Approved by the owner: start the defrost if it is still needed and allowed."""
        need = self._need()
        if p is None or need is None or need < 80:
            return [_log("Pakkosulatusta ei tarvita enää")]
        blocked = self._force_blocked(p)
        if blocked:
            return [_log(f"Pakkosulatusta ei tehty: {blocked}")]
        return self._force("hyväksytty ehdotus")

    def _force(self, reason: str) -> list:
        self.forced = {"t": self.now, "status": "pending", "need": _r(self._need(), 0),
                       "frost": _r(self.cycle["frost"], 2)}
        self.force_log.append(self.now)
        return [_log(f"Pakkosulatus: {reason}"), _force_cmd(reason)]

    def force_result(self, ok: bool, error: str | None) -> list:
        out: list = []
        if self.forced and self.forced["status"] == "pending" and not ok:
            self._force_failed(f"skripti ei käynnistynyt ({error})", out)
        return out

    def _force_failed(self, why: str, out: list):
        self.forced = None
        self.force_fail += 1
        self.force_retry = self.now + FORCE_RETRY
        if self.force_fail >= 2:
            self.force_block = self.now + DAY
            self.force_fail = 0
            out.append(_log(f"Pakkosulatus epäonnistui kahdesti ({why}). Tauko vuorokausi."))
            out.append(_notify("Pakkosulatus ei toimi",
                               f"Tekoäly yritti käynnistää sulatuksen kahdesti, mutta {why}. Pakkosulatus on "
                               "tauolla vuorokauden. Tarkista skripti ja säätimen sulatusehdot.", "sulatus"))
        else:
            out.append(_log(f"Pakkosulatus epäonnistui: {why}. Yritetään myöhemmin uudelleen."))

    # ------------------------------------------------------------------ interval tuning
    def _limits(self, key: str, direction: int) -> int | None:
        h = self.host
        p = h.params.get(key)
        if not p or not p.available or p.value is None or (
                key in h.failed and h.failed[key]["t"] > self.now - 7 * DAY):
            return None
        if max(h.backoff.get(key, 0), h.backoff.get(f"{key}|{direction}", 0)) > self.now:
            return None
        orig = h.originals.get(key, p.value)
        if direction > 0:
            hi = min(p.upper if p.upper is not None else 255, self.s.dfr_abs_max, orig + self.s.dfr_max_raise)
            new = min(p.value + self.s.dfr_step, hi)
        else:
            lo = max(p.lower if p.lower is not None else 0, self.s.dfr_abs_min, orig - self.s.dfr_max_lower)
            new = max(p.value - self.s.dfr_step, lo)
        return new if (new - p.value) * direction > 0 else None

    def _pick_key(self, recs: list[dict], band) -> str | None:
        missed = {m["key"] for m in self.misses if m.get("band") == band and m["t"] > self.now - 14 * DAY}
        counts = Counter()
        for r in recs:
            for k in [r["trigger"]["key"], *r["trigger"].get("alt", [])]:
                if k not in missed:
                    counts[k] += 1
                    break
        return counts.most_common(1)[0][0] if counts else None

    def _consider(self, rec: dict, out: list):
        self.proposal = None
        if not self.s.enable_defrost or self.mode == MODE_OFF:
            return
        band = rec["band"]
        same = [r for r in self.records if r["band"] == band and r["t"] > self.now - 3 * DAY]
        prop = None
        bad = ("myöhäinen", "kesken")
        if rec["verdict"] in bad and rec["trigger"]["type"] == "väli":
            waited = [r for r in same[-3:] if r["verdict"] in bad and r["trigger"]["type"] == "väli"]
            key = self._pick_key(waited, band)
            own = key is not None and self.host.managed.get(key, -1) > self.host.originals.get(key, 10**6)
            new = self._limits(key, -1) if key else None
            if new is not None and (len(waited) >= 2 or own):
                prop = self._proposal(
                    "dfr_interval_down", key, new, band,
                    f"{len(waited)} viimeisimmästä sulatuksesta kaistalla {band} odotti sulatusväliä, vaikka "
                    f"kennossa oli jo paljon huurretta ({rec['why']}). Lyhennän väliä {short(key)} "
                    f"{self._val(key)} → {new} min.")
        if prop is None and rec["verdict"] == "turha" and rec["trigger"]["type"] == "väli":
            last4 = same[-4:]
            idle = [r for r in last4 if r["verdict"] == "turha" and r["trigger"]["type"] == "väli"]
            key = self._pick_key(idle, band)
            if len(idle) >= 3 and not any(r["verdict"] in bad for r in last4) and key:
                new = self._limits(key, 1)
                if new is not None:
                    frost = median(r["frost"] for r in idle)
                    prop = self._proposal(
                        "dfr_interval_up", key, new, band,
                        f"{len(idle)}/{len(last4)} viimeisintä sulatusta kaistalla {band} (ulkona noin "
                        f"{fmt(rec['t_air'])} °C) tehtiin lähes puhtaaseen kennoon (huurretta {fmt(frost)} K), ja "
                        f"ne laukaisi sulatusväli {short(key)}. Pidennän väliä {self._val(key)} → {new} min"
                        + (", tekoäly sulattaa itse, kun huurretta kertyy." if self.force_ok else "."))
        self.suggestion = prop
        if prop is None:
            return
        h = self.host
        if h.backoff.get(f"type:{prop['type']}", 0) > self.now:
            return
        if self.mode == MODE_OBSERVE:
            code = f"{prop['type']}|{prop['key']}"
            if self.now - self.observed.get(code, 0) >= OBSERVE_LOG_EVERY:
                self.observed[code] = self.now
                out.append(_log(f"Sulatus, tarkkailu: ei muutettu mitään. Tekisin: {prop['title']}. "
                                f"{prop['reason']}"))
        elif self.mode == MODE_PROPOSE:
            if not h.pending and not h.approved:
                h.pending = prop
                out.append(_notify(
                    "COP-tekoälyn sulatusehdotus",
                    f"{prop['reason']}\n\nHyväksy painikkeella **Hyväksy ehdotus** tai hylkää painikkeella "
                    "**Hylkää ehdotus**. Muutos varmistetaan seuraavista sulatuksista ja perutaan, jos sulatus "
                    "jää myöhäiseksi tai väli ei muutu.", "ehdotus"))
        elif self.mode == MODE_AUTO:
            self.proposal = prop

    def _proposal(self, kind, key, new, band, reason) -> dict:
        return {"type": kind, "key": key, "to": new, "from": self._val(key), "band": band,
                "title": f"{short(key)} {self._val(key)} → {new} min", "reason": reason,
                "created": self.now, "candidates": [key]}

    def act(self, p: Point) -> list:
        """What the defrost control wants to do now (called when nothing else is writing)."""
        h = self.host
        a = self.action
        if a is not None:
            if a.get("inflight"):
                if self.now - a.get("inflight_t", self.now) > 10 * MINUTE:
                    a["inflight"] = False
                    h._queue_restore(a["key"], a["from"], "sulatusvälin kirjoituksen tulos jäi saamatta")
                    out: list = []
                    self._finish(a, "keskeytetty", "kirjoituksen tulos jäi saamatta", 0, out)
                    return out
                return []
            if a["phase"] == "verify" and self.now > a["deadline"]:
                out = []
                self._revert(a, "ei_varmistettu", "kaistalla ei tullut sulatuksia viikkoon", 0, out)
                return out + self.phase_write()
            if a["phase"] in ("write", "revert"):
                return self.phase_write()
        force = self.demand(p)
        if force:
            return force
        prop = self.proposal
        if (a is None and prop is not None and self.mode == MODE_AUTO and not h.queue
                and h.paused_until <= self.now and h._budget_ok(1)):
            self.proposal = None
            return self.start(prop)
        return []

    def start(self, prop: dict) -> list:
        h = self.host
        if self._val(prop["key"]) != prop["from"]:
            return [_log(f"Sulatusehdotus {prop['title']} vanhentui: arvo on muuttunut")]
        self.action = {
            "id": h.next_id, "type": prop["type"], "key": prop["key"], "from": prop["from"], "to": prop["to"],
            "band": prop["band"], "phase": "write", "inflight": False, "started": self.now,
            "applied": None, "seen": 0, "deadline": None, "title": prop["title"], "reason": prop["reason"],
            "outcome": None,
        }
        h.next_id += 1
        self.suggestion = None
        if h.pending and h.pending.get("type", "").startswith("dfr_"):
            h.pending = None
        return [_log(f"Aloitetaan sulatusvälin muutos: {prop['title']}. {prop['reason']}")] + self.phase_write()

    def phase_write(self) -> list:
        a = self.action
        if a is None or a.get("inflight"):
            return []
        if a["phase"] == "write":
            a.update(inflight=True, inflight_t=self.now)
            return [_write(a["key"], a["to"], "dfr_apply", a["title"])]
        if a["phase"] == "revert":
            a.update(inflight=True, inflight_t=self.now)
            return [_write(a["key"], a["from"], "dfr_revert", a["title"])]
        return []

    def write_result(self, write, ok: bool, error: str | None, actual: int | None) -> list:
        a = self.action
        out: list = []
        if a is None or not a.get("inflight") or write.key != a["key"]:
            return out
        a["inflight"] = False
        h = self.host
        if write.kind == "dfr_apply":
            if ok:
                h.originals.setdefault(a["key"], a["from"])
                h._set_value(a["key"], a["to"])
                toward_original = (a["to"] - a["from"]) * (h.originals[a["key"]] - a["from"]) > 0
                if a["type"] == "dfr_interval_down" and toward_original:
                    # stepping back an earlier raise of our own after a late defrost: no trial needed
                    self._accept(a, "palautettiin kohti alkuperäistä myöhäisen sulatuksen jälkeen", out)
                else:
                    a.update(phase="verify", applied=self.now, deadline=self.now + VERIFY_DAYS)
                    out.append(_log(f"{short(a['key'])} = {a['to']} min. Varmistetaan {VERIFY_DEFROSTS} "
                                    f"seuraavasta sulatuksesta kaistalla {a['band']}."))
            else:
                h.failed[a["key"]] = {"error": error or "kirjoitus epäonnistui", "t": self.now}
                if actual is not None and actual != a["from"]:
                    h._queue_restore(a["key"], a["from"], "kirjoitus epäonnistui, varmistetaan alkuperäinen")
                self._finish(a, "keskeytetty", f"laite ei hyväksynyt arvoa ({error})", 0, out)
        elif write.kind == "dfr_revert":
            if ok:
                h._set_value(a["key"], a["from"])
            else:
                h._queue_restore(a["key"], a["from"], "sulatusvälin palautus epäonnistui, yritetään uudelleen")
            outcome = a.get("outcome") or ["keskeytetty", "", 0]
            self._finish(a, *outcome, out)
        return out

    def _verify(self, rec: dict, out: list):
        a = self.action
        if a["phase"] != "verify" or rec["band"] != a["band"] or rec["t"] < (a["applied"] or 0):
            return
        up = a["type"] == "dfr_interval_up"
        if up and rec["verdict"] in ("myöhäinen", "kesken"):
            self._revert(a, "haitta", f"sulatus oli {rec['verdict']}: {rec['why']}", BACKOFF_HARM, out)
            return
        if not up and rec["verdict"] == "turha":
            self._revert(a, "ei_hyotya", "sulatus tuli nyt lähes puhtaaseen kennoon", BACKOFF_NO_GAIN, out)
            return
        run = rec.get("run")
        if run is None or rec["verdict"] == "epäselvä" or rec["trigger"]["type"] == "tekoäly":
            return
        hi = self._window_hi()
        if up:
            moved = run >= a["to"] - 2
            stuck = a["from"] - 2 <= run < a["to"] - 2
        else:
            moved = run <= a["to"] + hi
            stuck = a["to"] + hi < run <= a["from"] + hi and rec["trigger"]["type"] == "väli"
        if stuck:
            self.misses.append({"key": a["key"], "band": a["band"], "t": self.now})
            self._revert(a, "ei_hyotya", f"sulatusväli ei muuttunut (käyntiä {run:.0f} min): {short(a['key'])} "
                         "ei ohjaa tämän kaistan sulatuksia", BACKOFF_NO_GAIN, out)
            return
        if moved:
            a["seen"] += 1
            if a["seen"] >= VERIFY_DEFROSTS:
                self._accept(a, f"{a['seen']} sulatusta uudella välillä, viimeisin {rec['verdict']} "
                             f"(käyntiä {run:.0f} min)", out)

    def _accept(self, a: dict, why: str, out: list):
        self.host.history.append(self._entry(a, "hyväksytty", why))
        self.action = None
        msg = f"{short(a['key'])}: {a['from']} → {a['to']} min jää voimaan. {why[0].upper() + why[1:]}."
        out.append(_log(f"Hyväksytty: {msg}"))
        out.append(_notify("COP-tekoäly: sulatusväli muutettu", msg, "toimenpide"))

    def _revert(self, a: dict, outcome: str, why: str, backoff: float, out: list):
        a.update(phase="revert", outcome=[outcome, why, backoff])
        out.append(_log(f"Perutaan {short(a['key'])} → {a['from']} min: {why}"))

    def _finish(self, a: dict, outcome: str, why: str, backoff: float, out: list):
        if backoff:
            self.host.backoff[a["key"]] = self.now + backoff
        self.host.history.append(self._entry(a, outcome, why))
        self.host.history = self.host.history[-100:]
        if self.action is a:
            self.action = None
        if outcome in ("ei_hyotya", "haitta", "ei_varmistettu"):
            msg = f"{a['title']}: {why}. {short(a['key'])} palautettiin arvoon {a['from']} min."
            out.append(_log(f"Ei jätetty voimaan: {msg}"))
            out.append(_notify("COP-tekoäly: sulatusvälin muutos peruttiin", msg, "toimenpide"))

    def _entry(self, a: dict, outcome: str, why: str) -> dict:
        return {"id": a["id"], "t": self.now, "type": a["type"], "key": a["key"], "from": a["from"],
                "to": a["to"], "outcome": outcome, "why": why}

    # ------------------------------------------------------------------ host events
    def cancel(self, reason: str, out: list):
        """Rollback, fault or mode change: drop the action, restoring its value if it was written."""
        a = self.action
        self.proposal = None
        if a is None:
            return
        written = a.get("inflight") or a["phase"] in ("verify", "revert")
        if written and self.host.managed.get(a["key"]) not in (None, a["from"]):
            self.host._queue_restore(a["key"], a["from"], reason)
        elif written and a.get("inflight"):
            self.host._queue_restore(a["key"], a["from"], reason)
        self.host.history.append(self._entry(a, "keskeytetty", reason))
        self.action = None

    def set_mode(self, mode: str, out: list):
        if mode not in MODES or mode == self.mode:
            return
        old, self.mode = self.mode, mode
        if mode in (MODE_OFF, MODE_OBSERVE):
            self.cancel(f"sulatustila vaihdettiin: {mode}", out)
        h = self.host
        if mode != MODE_PROPOSE and h.pending and h.pending.get("type", "").startswith("dfr_"):
            h.pending = None
            out.append(_dismiss("ehdotus"))
        out.append(_log(f"Sulatustila: {old} → {mode}"))

    def check_external(self, out: list):
        """The owner changed the interval we are verifying: his value wins, the trial ends."""
        a = self.action
        if a is None or a.get("inflight") or a["phase"] != "verify":
            return
        v = self._val(a["key"])
        if v is not None and v != a["to"]:
            self.host.history.append(self._entry(a, "keskeytetty", "arvoa muutettiin muualta"))
            self.action = None

    def resume_action(self, out: list):
        a = self.action
        if a is None or not a.get("inflight"):
            return
        a["inflight"] = False
        v = self._val(a["key"])
        if a["phase"] == "write":
            if v == a["to"]:
                self.host.originals.setdefault(a["key"], a["from"])
                self.host._set_value(a["key"], a["to"])
                a.update(phase="verify", applied=self.now, deadline=self.now + VERIFY_DAYS)
            else:
                self._finish(a, "keskeytetty", "uudelleenkäynnistys", 0, out)
        elif a["phase"] == "revert" and v == a["from"]:
            self.host._set_value(a["key"], a["from"])
            outcome = a.get("outcome") or ["keskeytetty", "uudelleenkäynnistys", 0]
            self._finish(a, *outcome, out)

    # ------------------------------------------------------------------ status
    def _update_view(self, p: Point):
        c = self.cycle
        need = self._need()
        if p.defrost:
            state = "Sulatus käynnissä"
        elif self.forced and self.forced["status"] == "pending":
            state = "Pakkosulatus pyydetty"
        elif c["start"] is None:
            state = "Odottaa ensimmäistä sulatusta"
        elif c["ref"] is None:
            state = "Mittaa puhtaan kennon tasoa"
        elif not p.comp_on:
            state = "Kompressori seis"
        elif need is None or need < 25:
            state = "Puhdas"
        elif need < 75:
            state = "Huurtuu"
        elif need < 150:
            state = "Sulatus tarpeen"
        else:
            state = "Huurteessa, sulatus myöhässä"
        eta = None
        tr = c["trend"][-30:]
        if need is not None and need < 100 and len(tr) >= 10 and tr[-1][0] - tr[0][0] >= 10:
            slope = _slope(tr)
            if slope > 0.002:
                eta = (self.target - max(c["frost"], 0.0)) / slope
        last = next((x for x in reversed(self.host.points) if not x.defrost and x.comp_on), None)
        excess = None
        if last is not None and None not in (last.t_air, last.t_fin, last.ffd):
            excess = (last.t_air - last.t_fin) - last.ffd
        self.view = {
            "need": need, "state": state, "frost": c["frost"], "frost_fin": c["frost_fin"],
            "backfill": c["backfill"] if c["frost"] is not None else None,
            "loss": c["loss"], "run": c["run"] if c["start"] is not None else None, "eta": eta,
            "ref": c["ref"], "e_frost": c["e_frost"], "excess": excess, "target": self.target,
            "band": self.band_of(median(c["air"]) if c["air"] else p.t_air),
            "blocked": self._force_blocked(p) if need is not None and need >= 100 else None,
        }

    def stats(self, days: float = 7) -> dict:
        recs = [r for r in self.records if r["t"] > self.now - days * DAY]
        bands: dict = {}
        for r in recs:
            b = bands.setdefault(str(r["band"] or "–"), {"sulatuksia": 0, **{v: 0 for v in VERDICTS},
                                                       "tekoäly": 0, "_run": [], "_dur": []})
            b["sulatuksia"] += 1
            b[r["verdict"]] += 1
            b["tekoäly"] += r["trigger"]["type"] == "tekoäly"
            if r.get("run") is not None:
                b["_run"].append(r["run"])
            b["_dur"].append(r["dur"])
        for b in bands.values():
            b["väli_min"] = _r(median(b.pop("_run")), 0)
            b["kesto_s"] = _r(median(b.pop("_dur")), 0)
        return bands

    def binding(self) -> dict:
        """Interval parameter that started most interval-limited defrosts, per band (14 days)."""
        out = {}
        recs = [r for r in self.records if r["t"] > self.now - 14 * DAY and r["trigger"]["type"] == "väli"]
        for band in sorted({r["band"] for r in recs if r["band"] is not None}):
            key = self._pick_key([r for r in recs if r["band"] == band], band)
            if key:
                out[str(band)] = short(key)
        return out

    def recommendations(self) -> list[str]:
        tips = []
        recs = [r for r in self.records if r["t"] > self.now - 3 * DAY]
        for band in sorted({r["band"] for r in recs if r["band"] is not None}):
            same = [r for r in recs if r["band"] == band][-4:]
            cond_late = [r for r in same if r["verdict"] in ("myöhäinen", "kesken") and r["trigger"]["type"] == "ehto"]
            cond_idle = [r for r in same if r["verdict"] == "turha" and r["trigger"]["type"] == "ehto"]
            if len(cond_late) >= 2:
                tips.append(f"Kaista {band}: sulatukset alkavat lämpötilaehdosta vasta paljon huurretta keränneinä "
                            f"(mediaani {fmt(median(r['frost'] for r in cond_late))} K). Säätimen kynnys "
                            f"(Dfr.T.Diff {band} tai Frost D1) voi olla liian suuri.")
            if len(cond_idle) >= 3:
                tips.append(f"Kaista {band}: lämpötilaehto laukaisee sulatuksia puhtaaseen kennoon. Säätimen "
                            "kynnys voi olla liian herkkä.")
            warm = [r for r in same if r["verdict"] == "turha" and (r["t_air"] if r["t_air"] is not None else -99) >= 4]
            if len(warm) >= 3:
                tips.append("Sulatuksia tehdään yli +4 °C:ssa puhtaaseen kennoon: Dfr. Env.T (sulatus sallittu "
                            "tämän alapuolella) voisi olla pienempi.")
        return tips

    def status_text(self) -> str:
        a = self.action
        if self.mode == MODE_OFF:
            return "Pois päältä"
        if a is not None:
            if a["phase"] == "verify":
                return f"Varmistaa {a['title']} ({a['seen']}/{VERIFY_DEFROSTS} sulatusta)"
            if a["phase"] == "revert":
                return f"Palauttaa {short(a['key'])} → {a['from']} min"
            return f"Muuttaa {a['title']}"
        if self.forced and self.forced["status"] == "pending":
            return "Pakkosulatus pyydetty, odotetaan sulatuksen alkua"
        if not self.records:
            return "Ei sulatuksia vielä"
        r = self.records[-1]
        from homeassistant.util import dt as dt_util

        when = dt_util.as_local(datetime.fromtimestamp(r["t"], UTC)).strftime("%H:%M")
        frost = f"huurre {fmt(r['frost'])} K, " if r.get("frost") is not None else ""
        who = "tekoäly, " if r["trigger"]["type"] == "tekoäly" else ""
        return f"{when} {r['verdict']} ({who}{frost}{fmt_s(r['dur'])})"

    def status(self) -> dict:
        h = self.host
        a = self.action
        day = [r for r in self.records if r["t"] > self.now - DAY]
        return {
            "mode": self.mode,
            "action": None if a is None else {"tyyppi": a["type"], "vaihe": a["phase"], "parametri": short(a["key"]),
                                              "muutos": a["title"], "kaista": a["band"], "varmistettu": a["seen"],
                                              "syy": a["reason"]},
            "suggestion": None if not self.suggestion else self.suggestion["title"],
            "records": [{**r, "trigger": _trigger_text(r["trigger"])} for r in self.records[-10:]][::-1],
            "count_24h": len(day),
            "forced_24h": sum(1 for r in day if r["trigger"]["type"] == "tekoäly"),
            "verdicts_24h": {v: sum(1 for r in day if r["verdict"] == v) for v in VERDICTS},
            "energy_24h": _r(sum(r["e_def"] or 0 for r in day), 2),
            "stats": self.stats(),
            "binding": self.binding(),
            "params": {short(k): v for k in DFR_KEYS if (v := self._val(k)) is not None},
            "changed": {short(k): {"alkuperäinen": h.originals.get(k), "nyt": v}
                        for k, v in h.managed.items() if k in DFR_INTERVALS},
            "recommendations": self.recommendations(),
            "target": self.target,
            "force_ok": self.force_ok,
            "force_block": self.force_block if self.force_block > self.now else None,
        }


def _slope(points) -> float:
    """Least-squares slope of [x, y] pairs."""
    n = len(points)
    mx, my = sum(x for x, _ in points) / n, sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    return sum((x - mx) * (y - my) for x, y in points) / sxx if sxx else 0.0


def _trigger_text(t: dict) -> str:
    if t["type"] == "väli":
        return f"{short(t['key'])} {t['value']} min"
    return {"ehto": "lämpötilaehto", "muu": "hätä/paine", "tekoäly": "tekoäly", "tuntematon": "–"}.get(
        t["type"], t["type"])


# command objects live in brain.py; imported lazily to avoid a circular import
def _log(message):
    from .brain import Log

    return Log(message)


def _notify(title, message, tag):
    from .brain import Notify

    return Notify(title, message, tag)


def _dismiss(tag):
    from .brain import Dismiss

    return Dismiss(tag)


def _write(key, value, kind, reason):
    from .brain import Write

    return Write(key, value, kind, reason)


def _force_cmd(reason):
    from .brain import Force

    return Force(reason)
