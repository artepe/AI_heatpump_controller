"""Mango COP AI: watches the heat pump's COP, tunes the EEV tables through Mango Tuya Local and
decides when the outdoor coil needs a defrost.

Configured in configuration.yaml. The minimum is one line:

    mango_cop_ai:

which starts both the EEV and the defrost control in observe mode (Tarkkailu) with the owner's
entity names. The dashboard card custom:cop-tekoaly-card is served by the integration itself.
See README_FI.md.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .analysis import Settings
from .card import async_setup_card
from .const import DEFAULT_FAULTS, DEFAULT_SENSORS, DOMAIN, MODES, STORAGE_KEY, STORAGE_VERSION
from .runner import Runner

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.SELECT, Platform.BUTTON]

# Finnish configuration keys -> Settings fields
SETTING_KEYS = {
    "cop_lahde": ("cop_basis", vol.In(["kylmapiiri", "vesipuoli"])),
    "vesipuolen_kerroin": ("water_factor", vol.All(vol.Coerce(float), vol.Range(min=0.2, max=3))),
    "nesteen_lahestyminen": ("liquid_approach", vol.All(vol.Coerce(float), vol.Range(min=0, max=10))),
    "kuivan_imun_hyotysuhde": ("eta_dry_default", vol.All(vol.Coerce(float), vol.Range(min=0.5, max=0.85))),
    "markyysraja": ("wet_margin", vol.All(vol.Coerce(float), vol.Range(min=0.05, max=0.4))),
    "kuivatus_jatkuu": ("continue_margin", vol.All(vol.Coerce(float), vol.Range(min=0.02, max=0.3))),
    "kuivaraja": ("dry_margin", vol.All(vol.Coerce(float), vol.Range(min=0.0, max=0.2))),
    "ikkuna_min": ("window_min", vol.All(vol.Coerce(int), vol.Range(min=20, max=180))),
    "varmistus_min": ("verify_min", vol.All(vol.Coerce(int), vol.Range(min=20, max=360))),
    "kirjoituksia_vrk": ("writes_per_day", vol.All(vol.Coerce(int), vol.Range(min=2, max=48))),
    "kuumakaasu_max": ("discharge_max", vol.All(vol.Coerce(float), vol.Range(min=60, max=120))),
    "lahestyma_nousu_max": ("approach_max_rise", vol.All(vol.Coerce(float), vol.Range(min=2, max=15))),
    "cop_lasku_max": ("cop_drop_pct", vol.All(vol.Coerce(float), vol.Range(min=2, max=30))),
    "poikkeama_raja": ("anomaly_pct", vol.All(vol.Coerce(float), vol.Range(min=3, max=50))),
    "min_od_askel": ("min_od_step", vol.All(vol.Coerce(int), vol.Range(min=1, max=5))),
    "min_od_alaraja": ("min_od_floor", vol.All(vol.Coerce(int), vol.Range(min=5, max=40))),
    "min_od_max_lasku": ("min_od_max_delta", vol.All(vol.Coerce(int), vol.Range(min=1, max=15))),
    "sv_askel": ("sv_step", vol.All(vol.Coerce(int), vol.Range(min=1, max=20))),
    "sv_max_nosto": ("sv_max_delta", vol.All(vol.Coerce(int), vol.Range(min=1, max=80))),
    "sv_ylaraja": ("sv_abs_max", vol.All(vol.Coerce(int), vol.Range(min=0, max=150))),
    "eev_askeleet": ("eev_full_steps", vol.All(vol.Coerce(int), vol.Range(min=100, max=9000))),
    "saada_min_od": ("enable_min_od", cv.boolean),
    "saada_sv": ("enable_sv", cv.boolean),
    "huurrekynnys": ("frost_target_k", vol.All(vol.Coerce(float), vol.Range(min=1.5, max=10))),
    "puhdas_kenno": ("frost_clean_k", vol.All(vol.Coerce(float), vol.Range(min=0.3, max=3))),
    "sulatusvali_askel": ("dfr_step", vol.All(vol.Coerce(int), vol.Range(min=1, max=15))),
    "sulatusvali_max_nosto": ("dfr_max_raise", vol.All(vol.Coerce(int), vol.Range(min=0, max=90))),
    "sulatusvali_max_lasku": ("dfr_max_lower", vol.All(vol.Coerce(int), vol.Range(min=0, max=60))),
    "sulatusvali_ylaraja": ("dfr_abs_max", vol.All(vol.Coerce(int), vol.Range(min=20, max=255))),
    "sulatusvali_alaraja": ("dfr_abs_min", vol.All(vol.Coerce(int), vol.Range(min=10, max=120))),
    "saada_sulatus": ("enable_defrost", cv.boolean),
}

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Any(
            None,
            vol.Schema(
                {
                    # no default: an unset mode keeps the one chosen in the UI (Tarkkailu at first)
                    vol.Optional("tila"): vol.In(MODES),
                    vol.Optional("sulatus_tila"): vol.In(MODES),
                    vol.Optional("pakkosulatus"): cv.entity_id,
                    vol.Optional("anturit", default={}): vol.Schema(
                        {vol.Optional(k): cv.entity_id for k in DEFAULT_SENSORS}
                    ),
                    vol.Optional("vikatilat", default=DEFAULT_FAULTS): cv.entity_ids,
                    vol.Optional("parametrit", default={}): vol.Schema({cv.string: cv.entity_id}),
                    vol.Optional("asetukset", default={}): vol.Schema(
                        {vol.Optional(k): v[1] for k, v in SETTING_KEYS.items()}
                    ),
                }
            ),
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    if DOMAIN not in config:
        return True
    conf = config[DOMAIN] or CONFIG_SCHEMA({DOMAIN: {}})[DOMAIN]
    settings = Settings(**{SETTING_KEYS[k][0]: v for k, v in conf["asetukset"].items()})
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored = await store.async_load()
    runner = Runner(hass, conf, settings, store, stored)
    hass.data[DOMAIN] = runner
    runner.async_start()
    for platform in PLATFORMS:
        hass.async_create_task(async_load_platform(hass, platform, DOMAIN, {}, config))
    await async_setup_card(hass)
    _LOGGER.info("Mango COP-tekoäly käynnistyi: EEV %s, sulatus %s", runner.brain.mode, runner.brain.defrost.mode)
    return True
