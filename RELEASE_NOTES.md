Fixes for "it does nothing", and a dashboard card (AI Heat Pump Controller, `mango_cop_ai` 1.2.0, beta).

Fixed: three causes that could keep the controller waiting for steady running forever.

- Binary sensors that report only changes (template, ESPHome, MQTT) were treated as stale after
  15 minutes, so a defrost flag that stayed off, or a compressor relay that stayed on, rejected
  every minute (regression in 1.1.1). A reading now counts as frozen only when its source is known
  to re-report unchanged values, such as Modbus.
- A missing `binary_sensor.hp_defrost_active` blocked everything. The Modbus defrost valve
  `binary_sensor.hp_m0_defrost_venttiili` (new setting `sulatus_varalla`) now stands in for it.
- With short compressor runs, such as 40 min on and 20 min off in mild weather, no 45-minute
  window ever held the 27 steady minutes a decision needs. Steady minutes of earlier runs in the
  same conditions (outdoor within 2 K, supply water within 6 K, up to 3 hours back) now count.
- A `tila:` or `sulatus_tila:` changed in `configuration.yaml` was ignored after the first start.
  A changed value is now applied; the mode picked in the UI still survives restarts.

New:

- Dashboard card `custom:cop-tekoaly-card`, served and loaded by the integration itself: status,
  why it is not adjusting now and what to do about it, a sight glass for suction wetness, COP
  readouts, both mode switches, approve and reject, coil and last defrost, changed values and
  recent decisions.
- `sensor.cop_tekoaly_tilanne` attribute `esteet` lists every blocker with a fix, and the status
  text says why the current minute is not steady running.

Validation: 63 local tests passed, including the three failures reproduced against 1.1.1; Ruff
checks passed. The card was tested in a real Home Assistant 2026.10.1 dashboard. Tests use
Mango Tuya Local 0.2.1 with simulated hardware; the changes have not been run on a physical heat
pump yet.

Asennus ja käyttö suomeksi: README_FI.md.
