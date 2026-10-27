"""The COP-tekoäly dashboard card, served by the integration and loaded on every dashboard.

The card is plain JavaScript in frontend/cop-tekoaly-card.js. Home Assistant loads it as an extra
frontend module, so `type: custom:cop-tekoaly-card` works without adding a dashboard resource.
The websocket command mango_cop_ai/entities tells the card the entity ids, also after renaming.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, VERSION

_LOGGER = logging.getLogger(__name__)
CARD_DIR = Path(__file__).with_name("frontend")
CARD_PATH = f"/{DOMAIN}/frontend"
CARD_URL = f"{CARD_PATH}/cop-tekoaly-card.js?v={VERSION}"
# card key -> (platform, unique_id suffix)
CARD_ENTITIES = {
    **{f"sensor.{k}": ("sensor", k) for k in (
        "tilanne", "mittausten_tila", "diagnoosi", "cop", "cop_poikkeama", "cop_laatuaste",
        "naennainen_hyotysuhde", "kirjoitukset_24h", "sulatustarve", "sulatus")},
    "select.tila": ("select", "tila"),
    "select.sulatustila": ("select", "sulatustila"),
    "button.hyvaksy": ("button", "hyvaksy"),
    "button.hylkaa": ("button", "hylkaa"),
    "button.palauta": ("button", "palauta"),
}


async def async_setup_card(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_entities)
    try:
        await hass.http.async_register_static_paths([StaticPathConfig(CARD_PATH, str(CARD_DIR), False)])
        frontend.add_extra_js_url(hass, CARD_URL)
    except (AttributeError, KeyError, RuntimeError) as err:  # no frontend: the sensors still work
        _LOGGER.warning("Mango COP-tekoäly: kojelautakorttia ei voitu rekisteröidä: %s", err)


@callback
def card_entities(hass: HomeAssistant, permissions=None) -> dict[str, str]:
    registry = er.async_get(hass)
    out = {}
    for key, (platform, suffix) in CARD_ENTITIES.items():
        eid = registry.async_get_entity_id(platform, DOMAIN, f"{DOMAIN}_{suffix}")
        if eid and (permissions is None or permissions.check_entity(eid, POLICY_READ)):
            out[key] = eid
    return out


@websocket_api.websocket_command({"type": f"{DOMAIN}/entities"})
@callback
def websocket_entities(hass, connection, msg):
    connection.send_result(msg["id"], {"version": VERSION,
                                       "entities": card_entities(hass, connection.user.permissions)})
