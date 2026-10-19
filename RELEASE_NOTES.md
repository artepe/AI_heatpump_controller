First public beta of the AI Heat Pump Controller (`mango_cop_ai`, Mango COP-tekoäly).

- **COP watch:** every minute, the refrigerant-side and water-side COP are normalised to the Carnot COP and
  compared with a level learned for each outdoor-temperature and speed bin. A drop of more than 10 % on both
  bases for an hour raises a notification with the likely cause.
- **Wet-suction correction:** wet suction is detected from the compressor's apparent isentropic efficiency and
  corrected through Mango Tuya Local by tuning `Heat Min OD1–9` and `Heat Sv1–9`. The active zone is found by
  trial, every change is verified over 60 steady minutes, and changes are undone on no benefit or harm.
- **Safe defaults:** starts in observe mode, writes at most 12 parameters per day, restores everything on a
  fault, and adopts manual edits as the new baseline.

Tested against Home Assistant 2026.10.1 and Mango Tuya Local 0.2.1 over its simulated Tuya transport, with a
simulated heat pump. Not yet validated on a physical unit.

Asennus ja käyttö suomeksi: README_FI.md.
