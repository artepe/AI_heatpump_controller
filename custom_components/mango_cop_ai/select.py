"""Operating modes, Pois / Tarkkailu / Ehdota / Automaattinen: EEV tuning and defrost control."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, MODES, SIGNAL_UPDATE


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    runner = hass.data[DOMAIN]
    async_add_entities([ModeSelect(runner), DefrostModeSelect(runner)])


class ModeSelect(SelectEntity):
    _attr_should_poll = False
    _attr_name = "COP-tekoäly tila"
    _attr_unique_id = f"{DOMAIN}_tila"
    _attr_icon = "mdi:robot-outline"
    _attr_options = MODES

    def __init__(self, runner):
        self.runner = runner

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def current_option(self) -> str:
        return self.runner.brain.mode

    async def async_select_option(self, option: str) -> None:
        await self.runner.async_set_mode(option)


class DefrostModeSelect(ModeSelect):
    _attr_name = "COP-tekoäly sulatustila"
    _attr_unique_id = f"{DOMAIN}_sulatustila"
    _attr_icon = "mdi:snowflake-melt"

    @property
    def current_option(self) -> str:
        return self.runner.brain.defrost.mode

    async def async_select_option(self, option: str) -> None:
        await self.runner.async_set_defrost_mode(option)
