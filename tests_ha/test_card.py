"""The dashboard card is served and registered with the frontend, and skipped without it."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.aer_smartmatic.card import CARD_PATH, CARD_URL, async_register_card

CARD = "custom_components.aer_smartmatic.card"


async def test_card_registered_with_the_frontend(hass):
    hass.config.components.update({"frontend", "http"})
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await async_register_card(hass)
    (paths,), _ = hass.http.async_register_static_paths.call_args
    assert [(p.url_path, p.path) for p in paths] == [(CARD_URL, str(CARD_PATH))]
    assert CARD_PATH.is_file()
    add_js.assert_called_once()
    url = add_js.call_args.args[1]
    # The version in the URL busts the browser's cache and tells the card its version.
    assert url.startswith(f"{CARD_URL}?v=") and url != f"{CARD_URL}?v="


@pytest.mark.parametrize(
    ("major", "minor", "waits"),
    [(2026, 10, True), (2025, 12, True), (2026, 11, False), (2027, 1, False)],
)
async def test_card_waits_for_the_frontend_before_2026_11(hass, major, minor, waits):
    """Before 2026.11 the card is asked to wait for the frontend's registry."""
    hass.config.components.update({"frontend", "http"})
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js, \
         patch(f"{CARD}.MAJOR_VERSION", major), patch(f"{CARD}.MINOR_VERSION", minor):
        await async_register_card(hass)
    assert add_js.call_args.args[1].endswith("&wait=1") is waits


async def test_card_skipped_without_the_frontend(hass):
    assert "frontend" not in hass.config.components
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await async_register_card(hass)
    hass.http.async_register_static_paths.assert_not_called()
    add_js.assert_not_called()
