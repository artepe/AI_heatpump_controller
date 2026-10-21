# Mango COP AI – safe optimization / turvallinen optimointi

This integration supervises a real R290 refrigeration system. The heat pump's own
certified safety chain remains primary. Software estimates are **not** proof that
liquid refrigerant is entering a compressor.

## Before enabling automatic EEV writes

1. Fix **all** configured `vikatilat` entities. Every required fault input must
   return `on` or `off` and be fresh. Missing, unavailable and unknown values
   block unattended adjustments and forced defrosts.
2. Measure and confirm electrical power for **the whole heat pump**, not merely
   compressor inverter output, and verify that `vesilampo` includes actual
   delivered heat. Configure `anturit.sahko` for that power entity. Shelly
   readings must be time-aligned with water temperature and flow.
3. Check suction-temperature position and evaporating-saturation-pressure
   measurement. Compare with a calibrated field instrument. A negative SH is
   an inconclusive measurement for a dry-vapour enthalpy model.
4. Validate the physical meaning, units, active zones and readbacks of
   **each specific parameter** before putting its exact Mango key in the
   `vahvistetut_parametrit` list. Local Tuya readback alone cannot establish
   the scale or physical function. Heat Sv5 and Heat Min OD5 are not validated
   by the public captured profile as of this version.
5. Use `Tarkkailu` first and review several defrost/run cycles.

Sample YAML (replace both example entity IDs with real verified entities):

```yaml
mango_cop_ai:
  tila: Tarkkailu
  sulatus_tila: Tarkkailu
  # Complete vikatilat list is the default; repair missing entities.
  anturit:
    sahko: sensor.REPLACE_WITH_CONFIRMED_WHOLE_UNIT_KW
    vesilampo: sensor.hp_lampoteho_laskettu
  asetukset:
    cop_lahde: vesipuoli
    kokonaisottoteho_vahvistettu: false
    turvatulistus_k: 1.5
    vaadi_vesipuolen_cop: true
    cop_lasku_max: 3
  vahvistetut_parametrit: []
```

After physical commissioning, set
`kokonaisottoteho_vahvistettu: true` and add only the verified parameter
keys. This **does not** bypass missing fault/superheat/water-COP guards.

## Interpretations and limitations

- Calculated isentropic efficiency is **apparent**, not a wet-suction sensor.
- An R290 dry-vapour enthalpy fit is disabled when measured suction is clearly
  below saturation. Refrigerant-side COP is diagnostic, not the acceptance
  criterion for EEV writes.
- Water-side COP uses synchronized heat output and **total** electrical input.
  A change is rejected if the measured quality metric falls more than the
  configured tolerance or stable positive superheat is not demonstrated.
- COP baselines use outdoor temperature, compressor speed **and heating water
  temperature**, to reduce misleading comparisons between operating points.
- Defrost timing needs a measured clean-coil reference. A missing safety input
  blocks AI-initiated defrost and parameter changes; the factory control still
  governs the refrigeration plant.
- Compressor-speed, pump-speed, fan-speed and Ouman supply-target optimization
  remain **observation-only**. This integration does not have proven write
  maps, safety boundaries or energy-validated actuators for them. Never replace
  the factory safety controls with external AI.

## Operational recovery

Switch to `Tarkkailu` to stop new AI experiments. To undo existing AI changes,
inspect `muutetut_arvot` and invoke the dedicated restore button when safe.
A restore can fail when the local device is offline; read back and verify every
restored raw value. If any protection sensor disappears, investigate wiring and
Home Assistant entity mappings before returning to automatic control.
