"""Constants for the Mango COP AI."""

DOMAIN = "mango_cop_ai"
NAME = "Mango COP-tekoäly"
VERSION = "1.2.0"
MANGO_DOMAIN = "mango_tuya_local"

MODE_OFF = "Pois"
MODE_OBSERVE = "Tarkkailu"
MODE_PROPOSE = "Ehdota"
MODE_AUTO = "Automaattinen"
MODES = [MODE_OFF, MODE_OBSERVE, MODE_PROPOSE, MODE_AUTO]


def initial_mode(stored: dict, configured: str | None) -> tuple[str, str | None]:
    """(mode to start in, configuration.yaml mode to remember).

    The mode chosen in the UI survives restarts. A mode written in configuration.yaml wins when it
    has been changed there since the previous start; on the first start it is the starting mode.
    """
    remembered = stored.get("mode") if stored.get("mode") in MODES else None
    if configured is not None and "yaml_mode" in stored and configured != stored["yaml_mode"]:
        return configured, configured
    return remembered or configured or MODE_OBSERVE, configured


# Parameter keys are "<section>|<name>" exactly as in the Mango profile.
MIN_OD = [f"EEV Set|Heat Min OD{i}" for i in range(1, 10)]
SV = [f"EEV Set|Heat Sv{i}" for i in range(1, 10)]
EEV_MAX_STEP = "MORTOR Set|M.EEV Max step"
TABLES = {"min_od": MIN_OD, "sv": SV}

# Defrost section. Only the interval parameters are ever written; the rest is read to explain
# what started each defrost. Meanings are inferred from the names and verified from behaviour.
DFR = "Defro.Set."
DFR_INTERVAL = f"{DFR}|Dfr.Interval"  # minimum run time between defrosts, min
DFR_TIME = f"{DFR}|Dfr.Time"  # maximum defrost time, s
DFR_ENV = f"{DFR}|Dfr. Env.T"  # defrost allowed below this ambient, °C
DFR_COIL_START = f"{DFR}|Dfr. Coil T"  # coil temperature that allows a defrost, °C
DFR_COIL_END = f"{DFR}|Dfr.Coil T"  # coil temperature that ends a defrost, °C
DFR_SMART = f"{DFR}|EWD.SmartDfr"
DFR_FROST_D1 = f"{DFR}|Frost D1"
DFR_FROST_D2 = f"{DFR}|Frost D2"
DFR_SURE = f"{DFR}|T. Sure Time"  # condition confirmation time, min
DFR_BANDS = [f"{DFR}|Dfr.Env.T.{i}" for i in range(1, 5)]  # ambient band edges, warm -> cold
DFR_INTE = [f"{DFR}|Dfr.Inte. {i}" for i in range(1, 6)]  # interval per band 1 (warm) .. 5 (cold)
DFR_DIFF = [f"{DFR}|Dfr.T.Diff {i}" for i in range(1, 6)]  # ambient - coil difference per band
DFR_TIMING = f"{DFR}|TimingDefrost"
DFR_INTERVALS = [DFR_INTERVAL, *DFR_INTE]
DFR_KEYS = [DFR_INTERVAL, DFR_TIME, DFR_ENV, DFR_COIL_START, DFR_COIL_END, DFR_SMART, DFR_FROST_D1,
            DFR_FROST_D2, DFR_SURE, *DFR_BANDS, *DFR_INTE, *DFR_DIFF, DFR_TIMING]

# Parameter IDs from the Mango profile; the number entity unique_id ends with "_<id>".
PARAMETER_IDS = {
    **{key: f"0x{0x047A + i:04X}" for i, key in enumerate(MIN_OD)},
    **{key: f"0x{0x0468 + i:04X}" for i, key in enumerate(SV)},
    EEV_MAX_STEP: "0x0429",
    DFR_INTERVAL: "0x04B6", DFR_TIME: "0x04B7", DFR_ENV: "0x04B9", DFR_COIL_START: "0x04BA",
    DFR_COIL_END: "0x04BB", DFR_SMART: "0x04BD", DFR_FROST_D1: "0x04BE", DFR_FROST_D2: "0x04BF",
    DFR_SURE: "0x04C0", DFR_TIMING: "0x04D3",
    **{key: f"0x{0x04C1 + i:04X}" for i, key in enumerate(DFR_BANDS)},
    **{key: f"0x{0x04C5 + i:04X}" for i, key in enumerate(DFR_INTE)},
    **{key: f"0x{0x04CA + i:04X}" for i, key in enumerate(DFR_DIFF)},
}
ALL_PARAMETERS = [*MIN_OD, *SV, EEV_MAX_STEP, *DFR_KEYS]

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
    "sulatus_varalla": "binary_sensor.hp_m0_defrost_venttiili",  # used when "sulatus" does not exist
    "vastus": "binary_sensor.hp_electric_heater",
    "lamelli": "sensor.kylmaaine_lamellilampo_fin",
    "ulkoyksikko": "sensor.hp_module_ambient_temperature",
    "huurteeton_ero": "sensor.hp_frost_free_coil_ambient_delta",
    "virtaus": "sensor.module_0_vf_water_pump_flow",
    "puhallin": "sensor.hp_fan_1_rpm_actual",
}
# the owner's forced defrost script; if missing, a script named like "Sulatus pakotettu" is used
DEFAULT_FORCE_SCRIPT = "script.sulatus_pakotettu_moduuli_0"
DEFAULT_FAULTS = [
    "binary_sensor.hp_fault_active",
    "binary_sensor.hp_serious_alarm",
    "binary_sensor.hp_fault_low_pressure",
    "binary_sensor.hp_fault_high_pressure",
    "binary_sensor.hp_fault_discharge_temperature_high",
]
