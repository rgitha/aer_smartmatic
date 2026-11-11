"""The companion dashboard card, served and registered by the integration."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILE = "aer-smartmatic-card.js"
CARD_PATH = Path(__file__).parent / "www" / CARD_FILE
CARD_URL = f"/{DOMAIN}/{CARD_FILE}"
# From this version the frontend loads the card only once its own custom
# element registry is in place (home-assistant/frontend#54444).
CARD_SAFE_FROM = (2026, 11)


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card and load it on every dashboard, once.

    Needs Home Assistant's frontend and http; without them (a headless
    setup) the card is skipped and the integration works as usual.
    """
    if not {"frontend", "http"} <= hass.config.components:
        _LOGGER.debug("No frontend: the dashboard card isn't registered")
        return
    # Imported here: only needed, and only loaded, with the frontend.
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_PATH), True)]
    )
    # The version in the URL makes browsers fetch the card again after an
    # update, and tells the card its version. On older versions the card
    # could register before the frontend's registry and be lost
    # (home-assistant/frontend#53890), so it's asked to wait for it.
    url = f"{CARD_URL}?v={version}"
    if (MAJOR_VERSION, MINOR_VERSION) < CARD_SAFE_FROM:
        url += "&wait=1"
    add_extra_js_url(hass, url)
