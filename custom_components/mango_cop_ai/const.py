"""Constants for the Mango COP AI."""

DOMAIN = "mango_cop_ai"
NAME = "Mango COP-tekoäly"
VERSION = "1.0.0"
MANGO_DOMAIN = "mango_tuya_local"

MODE_OFF = "Pois"
MODE_OBSERVE = "Tarkkailu"
MODE_PROPOSE = "Ehdota"
MODE_AUTO = "Automaattinen"
MODES = [MODE_OFF, MODE_OBSERVE, MODE_PROPOSE, MODE_AUTO]

# Parameter keys are "<section>|<name>" exactly as in the Mango profile.
MIN_OD = [f"EEV Set|Heat Min OD{i}" for i in range(1, 10)]
SV = [f"EEV Set|Heat Sv{i}" for i in range(1, 10)]
EEV_MAX_STEP = "MORTOR Set|M.EEV Max step"
TABLES = {"min_od": MIN_OD, "sv": SV}

# Parameter IDs from the Mango profile; the number entity unique_id ends with "_<id>".
PARAMETER_IDS = {
    **{key: f"0x{0x047A + i:04X}" for i, key in enumerate(MIN_OD)},
    **{key: f"0x{0x0468 + i:04X}" for i, key in enumerate(SV)},
    EEV_MAX_STEP: "0x0429",
}

SIGNAL_UPDATE = f"{DOMAIN}_update"
STORAGE_KEY = DOMAIN
STORAGE_VERSION = 1

# Default source entities (the owner's Home Assistant names).
DEFAULT_SENSORS = {
    "sahko": "sensor.invt_pout_power",
    "nopeus": "sensor.hp_running_speed",
    "hoyrystyminen": "sensor.kylmaaine_haihdutuksen_kyllastyslampo_evap_sat",
    "lauhtuminen": "sensor.kylmaaine_lauhdutuksen_kyllastyslampo_cond_sat",
    "imukaasu": "sensor.kylmaaine_imukaasu_suction",
    "kuumakaasu": "sensor.kylmaaine_puristuskaasu_discharge",
    "eev": "sensor.eev_main_opening",
    "tulistus": "sensor.hp_main_eev_superheat_actual",
    "tulistus_tavoite": "sensor.hp_subcool_superheat_controller",
    "ulko": "sensor.ouman_outdoor_temperature",
    "ulko_varalla": "sensor.ulkolampotila",
    "menovesi": "sensor.lampopumppu_menovesi",
    "paluuvesi": "sensor.lampopumppu_paluuvesi",
    "vesilampo": "sensor.hp_lampoteho_laskettu",
    "huurre": "sensor.vilp_huurre_cop_havio",
    "kompressori": "binary_sensor.hp_m0_kompressori_rele",
    "sulatus": "binary_sensor.hp_defrost_active",
    "vastus": "binary_sensor.hp_electric_heater",
}
DEFAULT_FAULTS = [
    "binary_sensor.hp_fault_active",
    "binary_sensor.hp_serious_alarm",
    "binary_sensor.hp_fault_low_pressure",
    "binary_sensor.hp_fault_high_pressure",
    "binary_sensor.hp_fault_discharge_temperature_high",
]
