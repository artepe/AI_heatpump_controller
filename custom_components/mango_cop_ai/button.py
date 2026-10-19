"""Approve or reject a proposal, or restore every value the AI has changed."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity

from .const import DOMAIN


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    runner = hass.data[DOMAIN]
    async_add_entities([
        AiButton(runner, "hyvaksy", "COP-tekoäly hyväksy ehdotus", "mdi:check-circle-outline", runner.async_approve),
        AiButton(runner, "hylkaa", "COP-tekoäly hylkää ehdotus", "mdi:close-circle-outline", runner.async_reject),
        AiButton(runner, "palauta", "COP-tekoäly palauta alkuperäiset", "mdi:restore", runner.async_rollback),
    ])


class AiButton(ButtonEntity):
    _attr_should_poll = False

    def __init__(self, runner, key, name, icon, action):
        self.runner = runner
        self._action = action
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_icon = icon

    async def async_press(self) -> None:
        await self._action()
