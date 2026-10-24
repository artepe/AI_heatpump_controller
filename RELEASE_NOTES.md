Measurement health for AI Heat Pump Controller (`mango_cop_ai` 1.1.1, beta).

- Reject electrical power readings older than 15 minutes when evaluating stable operation.
- Block stable operation when compressor or defrost states are missing, unknown or stale.
- Add `sensor.cop_tekoaly_mittausten_tila` with measurement health, required readings blocking control,
  missing entities and the last processing time.
- Update both dashboard examples and the Finnish and English guides.
- Test measurement outages and recovery, and make the observe-mode test wait for a proposal rather
  than assume a fixed simulation minute.

Validation: 51 local tests passed; Ruff checks passed. Tests use Home Assistant 2026.10.1 and
Mango Tuya Local 0.2.1 with simulated hardware. These changes have not been tested on a physical heat pump.

Asennus ja käyttö suomeksi: README_FI.md.
