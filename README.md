# AI Heat Pump Controller

[![Tests](https://github.com/artepe/AI_heatpump_controller/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/artepe/AI_heatpump_controller/actions/workflows/tests.yml)

A Home Assistant integration (`mango_cop_ai`, "Mango COP-tekoäly") that watches the COP of an
R290 air-to-water heat pump every minute, tunes its electronic expansion valve when the data
show a fault it can correct, and decides when the outdoor coil needs a defrost. It runs inside Home Assistant, for example on a Raspberry Pi, and
writes parameters only through the [Mango Tuya Local](https://github.com/artepe/chinaheatpump-tuya-local-home-assistant)
integration (Mango Energy monoblocks, 770-parameter Tuya profile).

[Suomenkielinen ohje](README_FI.md)

**Version 1.1.0 beta.** Tested against a real Home Assistant core and the real Mango Tuya Local
integration over its simulated Tuya transport, with a simulated heat pump. The EEV writes have
been confirmed on a physical unit (Heat Sv5 changed the controller's superheat target as
expected); the defrost control has not yet run on a physical unit.

## What it does

- **Watches COP.** Computes COP from the refrigerant cycle (R290 enthalpies from pressures and
  temperatures) and from the water side, and normalises both to the Carnot COP. A normal level
  is learned for each outdoor-temperature and compressor-speed bin. When both COP bases stay
  more than 10 % below normal for an hour, it notifies you with the most likely cause: wet
  suction, frost, evaporator, condenser, refrigerant shortage, electric heater or fault.
- **Corrects wet suction.** Wet suction is recognised from the compressor's apparent isentropic
  efficiency. Dry suction reads about 0.6–0.75; liquid in the suction gas looks like saturated
  vapour, so the reading climbs towards 1. The dry level is learned from periods with more than
  3 K suction superheat.
  - EEV stuck at its minimum opening and wet: *Heat Min OD* of the active zone −2 %.
  - EEV regulating freely but wet: *Heat Sv* (superheat target) of the active zone +0.5 K.
  - EEV hunting on a lowered minimum: one step back, and that minimum is never lowered again.
  - Suction dry for 3 h after an Sv raise: its own raise is undone step by step to recover
    evaporating temperature.
- **Finds the active zone by itself.** The controller's 9-zone tables have unknown boundaries.
  The AI changes one candidate and watches the controller react: the EEV floor moves, or the
  superheat target moves. A miss is restored and remembered. The first guess assumes zones run
  from warm (1) to cold (9) by outdoor temperature. Found zones are stored, so the next time it
  goes straight to the right one.
- **Verifies every change.** It judges each change over 60 steady minutes, excluding defrosts,
  oil-return boosts and starts. A change stays if suction dried or the EEV settled. It is
  undone, and the parameter is left alone for a week or two, if there was no benefit or any
  harm appeared: discharge gas above 100 °C, a starving evaporator, a hunting EEV, or suction
  turning wet again.

- **Defrosts on demand.** Frost is measured continuously as the growth of the evaporator
  approach (coil air − evaporating temperature) from the clean-coil level after each defrost,
  scaled with compressor and fan speed. When it reaches the AI's threshold (4 K to start with,
  self-calibrating 2.5–8 K from how long the melts take), the AI starts a defrost with the
  owner's forced-defrost script. Every defrost is judged (turha = needless, sopiva = right,
  myöhäinen = late, kesken = incomplete) together with what started it, and the controller's
  own interval (`Dfr.Interval` / `Dfr.Inte. 1–5`, the one that actually started the defrosts) is
  lengthened after repeated defrosts on a clean coil or shortened after late ones, each step
  verified by the following defrosts.

It never touches tank temperature, compressor speeds, fan, pump, protections or the defrost
temperature thresholds.

## Safety

- **Only these parameters are writable:** `Heat Min OD1–9`, `Heat Sv1–9`, `Dfr.Interval` and
  `Dfr.Inte. 1–5`. The forced-defrost script is the only other action.
  - Defrost interval: 5 min steps, at most +30 / −15 min from the original, always 30–120 min.
  - Forced defrosts: at least 25 compressor minutes apart, air at most +8 °C, at most 24 a day;
    two that do not start pause them for a day.
  - Heat Min OD: 2 % steps, at most 8 % below the original value, never below 8 %.
  - Heat Sv: 0.5 K steps, at most 3 K above the original value, never above 6 K.
  - A value only ever moves away from its original and back, never past it.
- **Write limit:** at most 12 parameter writes per 24 h, to protect the controller's memory.
  Restores are always allowed.
- **Faults pause it:** a fault or alarm entity turning on restores every changed value and
  pauses adjustments for 24 h.
- **Manual edits win:** a value you change yourself becomes the new baseline and is left
  alone for a week.
- **Writes are confirmed:** every write goes through Mango Tuya Local, which checks fresh
  device bounds, reads the value back and restores the original on failure. A parameter the
  device refuses is left alone for a week.
- **Restarts are safe:** a Home Assistant restart in the middle of a trial restores the
  trial value.
- **Missing or frozen data stops decisions:** a core reading that is unavailable or has not
  refreshed for 15 min stops all decisions.
- **Everything is logged:** every decision is written to the logbook with its reasons, and
  the important ones also raise a notification.

## Modes

The mode is set with `select.cop_tekoaly_tila`. It starts in observe mode, and the last
selected mode survives restarts.

| Mode | Behaviour |
|---|---|
| Tarkkailu (observe) | Learns and diagnoses; writes nothing. The logbook shows "Tekisin: …" (I would …) with reasons. |
| Ehdota (propose) | Notifies its proposal and acts only after **Hyväksy ehdotus** (approve) is pressed. |
| Automaattinen (automatic) | Acts, verifies and undoes changes by itself. |
| Pois (off) | Does nothing; a trial in progress is restored. |

The defrost control has its own mode, `select.cop_tekoaly_sulatustila`, with the same options;
it also starts in observe mode.

**Palauta alkuperäiset** restores every value the AI changed, defrost intervals included, and
returns both modes to observe.

## Installation

Requires Home Assistant 2026.10 or later and Mango Tuya Local with its parameter entities
enabled.

**HACS:** add `https://github.com/artepe/AI_heatpump_controller` as a custom repository of type
Integration, then download **AI Heat Pump Controller**.

**Manual:** unpack `mango_cop_ai_<version>.zip` from
[Releases](https://github.com/artepe/AI_heatpump_controller/releases) into the Home Assistant
`config` folder, so that `config/custom_components/mango_cop_ai/` exists.

Then add one line to `configuration.yaml` and restart Home Assistant:

```yaml
mango_cop_ai:
```

The Mango number entities are found by their unique IDs, so no entity IDs need to be
configured. The default source sensors are the author's entity names (see
[examples/configuration.yaml](examples/configuration.yaml)); map your own under `anturit:`. A
dashboard card is in [examples/kortti.yaml](examples/kortti.yaml).

## Entities

| Entity | Content |
|---|---|
| `select.cop_tekoaly_tila` | Mode |
| `sensor.cop_tekoaly_tilanne` | What it is doing now. Attributes: current action, changed values, learned zones, history. |
| `sensor.cop_tekoaly_diagnoosi` | Likely state or fault |
| `sensor.cop_tekoaly_cop` | Hourly COP. Attributes: water side, Carnot, expected COP. |
| `sensor.cop_tekoaly_cop_poikkeama` | % deviation from the learned level |
| `sensor.cop_tekoaly_cop_laatuaste` | COP / Carnot COP, % |
| `sensor.cop_tekoaly_naennainen_hyotysuhde` | Apparent compressor efficiency, % (wet suction indicator) |
| `sensor.cop_tekoaly_kirjoitukset_24_h` | Parameter writes in the last 24 h |
| `select.cop_tekoaly_sulatustila` | Defrost control mode |
| `sensor.cop_tekoaly_sulatustarve` | Frost as % of the AI's defrost threshold (100 % = defrost now) |
| `sensor.cop_tekoaly_sulatus` | Verdict of the last defrost. Attributes: last 10 defrosts, 24 h counts, 7-day band statistics, controller defrost parameters, changed intervals, recommendations. |
| `button.cop_tekoaly_hyvaksy_ehdotus`, `_hylkaa_ehdotus`, `_palauta_alkuperaiset` | Approve, reject, restore |

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python tools/fetch_mango.py      # Mango Tuya Local for the end-to-end tests (git-ignored)
.venv/bin/pytest
.venv/bin/ruff check custom_components/mango_cop_ai tests tools
```

The tests cover:

- the R290 property fits against CoolProp;
- the decision core, minute by minute, against a simulated heat pump with hidden zone tables,
  an EEV superheat controller, wet suction, defrosts and oil-return boosts;
- the defrost control against a simulated controller that defrosts from the Mango defrost
  parameters, with weather-dependent frost, fan and speed changes and a forced-defrost script;
- end-to-end runs in a real Home Assistant core: observe, propose, automatic, rollback, fault,
  refused writes, restarts, frozen data, YAML settings, forced defrost through a script and a
  defrost interval written through Mango.

## License

MIT. Community project, not a manufacturer release. It changes heat pump controller
parameters; use it at your own risk and start in observe mode.
