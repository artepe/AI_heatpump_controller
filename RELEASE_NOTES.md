Defrost control for the AI Heat Pump Controller (`mango_cop_ai` 1.1.0, beta).

- **Frost is measured:** the growth of the evaporator approach from the clean-coil level after each defrost,
  scaled with compressor and fan speed, with the frost that grew before the reference credited at the measured
  rate. Silent-mode steps and lasting speed changes do not look like frost.
- **Defrost on demand:** at 100 % defrost need the AI starts a defrost with the owner's forced-defrost script
  (`script.sulatus_pakotettu_moduuli_0`, or a script named "Sulatus pakotettu"). Guards: 25 compressor minutes
  since the last defrost, air at most +8 °C, 24 per day; the defrost must start within 6 min, two misses pause
  it for a day. The threshold starts at 4 K and calibrates itself from the melt times (2.5–8 K).
- **Every defrost is judged** (turha, sopiva, myöhäinen, kesken, epäselvä) with what started it and logged.
- **Controller interval tuning:** only the interval that started the defrosts (`Dfr.Interval` or the band's
  `Dfr.Inte. n`) is changed, 5 min at a time, verified by the following defrosts and undone on a late or
  incomplete defrost or when the interval does not move with the setting.
- **Own mode:** `select.cop_tekoaly_sulatustila` (starts in observe mode). New sensors
  `sensor.cop_tekoaly_sulatustarve` and `sensor.cop_tekoaly_sulatus`, and a defrost section on the technical card.

Tested against Home Assistant 2026.10.1 and Mango Tuya Local 0.2.1 over its simulated Tuya transport, with a
simulated heat pump and defrost controller. The defrost control has not yet run on a physical unit.

Asennus ja käyttö suomeksi: README_FI.md.
