"""Config flow for Aer Smart Matic integration."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import voluptuous as vol

from collections.abc import Awaitable

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.helpers.device_registry import format_mac
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)

from .const import CONF_COMMAND_CHAR, CONF_HAS_CLOCK, CONF_REPLY_CHAR, DOMAIN
from .godrej import SmartMatic
from .godrej.exception import InvalidDeviceError, ConnectionError, NotIdle

_LOGGER = logging.getLogger(__name__)

MANUAL_MAC = "manual_mac"
# The name devices advertise; the default device name.
DEFAULT_NAME = "Smart Matic"
_MAC_RE = re.compile(r"([0-9a-f]{2}:){5}[0-9a-f]{2}")
# The Select device form's tick box: flash the chosen device's lights only.
CONF_FLASH = "flash"
# Shown on the Setup confirm form when its lights couldn't flash.
_FLASH_NOTES = {
    "flash_not_idle": "Its lights can't flash while auto-spray or a schedule is running.",
    "flash_no_reply": "The device didn't respond.",
}
# A menu can't show errors: the Setup confirm form shows a failed setup's
# reason as a note instead (the "error" texts in strings.json).
_ERROR_NOTES = {
    "cannot_connect": "Failed to connect.",
    "invalid_device": "This is not a supported Smart Matic device.",
    "no_reply": (
        "The device didn't respond. Move it closer to a Bluetooth adapter or proxy, "
        "disconnect any phone connected to it, and try again."
    ),
    "unknown": "Unexpected error.",
}


def _label(name: str, mac: str) -> str:
    """The name with the end of the address, to tell devices apart:
    "Smart Matic (78-61)"."""
    return f"{name} ({mac[-5:].replace(':', '-').upper()})"


class AerSmartMaticConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self.name = DEFAULT_NAME
        self._listed_names: dict[str, str] = {}
        self.mac: str = ""
        self.command_char: str | None = None
        self.reply_char: str | None = None
        self.has_clock = True
        self._manual_entry = False
        self._discovered = False
        self._discovered_name = DEFAULT_NAME
        self._smartmatic: SmartMatic | None = None
        self._detect_task: asyncio.Task[None] | None = None
        self._detect_error: str | None = None
        self._stage: tuple[str, dict[str, str]] = ("connecting", {})
        self._stage_changed = asyncio.Event()
        self._waiter: asyncio.Task[None] | None = None
        self._flash_task: asyncio.Task[str | None] | None = None
        self._flash_ticked = False
        self._note = ""

    def _is_device_supported(self, device_info: BluetoothServiceInfoBleak) -> bool:
        name = device_info.name or ""
        return "smart matic" in name.lower()

    async def _detect(self, smartmatic: SmartMatic) -> str | None:
        """Detect the channels; return an error key, or None on success."""
        try:
            channels = await smartmatic.detect_channels(progress=self._on_progress)
        except InvalidDeviceError:
            return "invalid_device"
        except ConnectionError:
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error while validating %s", self.mac)
            return "unknown"
        if channels is None:
            return "no_reply"
        self.command_char, self.reply_char = channels
        self.has_clock = smartmatic.detected_clock is not False
        return None

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            if user_input[CONF_MAC] == MANUAL_MAC:
                self._manual_entry = True
                return await self.async_step_manual_mac()

            self._manual_entry = False
            self.mac = user_input[CONF_MAC]
            self.name = self._listed_names.get(self.mac) or DEFAULT_NAME
            self._flash_ticked = bool(user_input.get(CONF_FLASH))
            if self._flash_ticked:
                return await self.async_step_flash()
            await self.async_set_unique_id(format_mac(self.mac), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()

        configured_ids = self._async_current_ids()
        discovered_devices = []
        discovered_infos = async_discovered_service_info(
            self.hass,
            connectable=True
        )
        for device_info in discovered_infos:
            if not self._is_device_supported(device_info):
                continue
            if format_mac(device_info.address) in configured_ids:
                continue
            discovered_devices.append(device_info)

        self._listed_names = {dev.address: dev.name for dev in discovered_devices if dev.name}
        device_options = {dev.address: f"{dev.name} ({dev.address})" for dev in discovered_devices}
        device_options[MANUAL_MAC] = "Enter MAC address manually"

        # After a flash, the same device and the tick box stay as they were.
        selected = {"default": self.mac} if self.mac in device_options else {}
        return self.async_show_form(
            step_id='user',
            data_schema=vol.Schema({
                vol.Required(CONF_MAC, **selected): vol.In(device_options),
                vol.Optional(CONF_FLASH, default=self._flash_ticked): bool,
            }),
            description_placeholders={
                "description": "Please select a device to configure"
            },
            errors=errors
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Offer a device Home Assistant heard advertising."""
        _LOGGER.debug(
            "Discovered %s (%s) via %s",
            discovery_info.name, discovery_info.address, getattr(discovery_info, "source", "?"),
        )
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        if not self._is_device_supported(discovery_info):
            return self.async_abort(reason="not_supported")
        self.mac = discovery_info.address
        self._discovered = True
        self.name = discovery_info.name or DEFAULT_NAME
        # In the Discovered list and the form's title, with the end of the
        # address to tell devices apart; it is added under its own name.
        self._discovered_name = _label(self.name, self.mac)
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """The Setup confirm form: flash the device's lights, or set it up."""
        if errors:
            self._note = _ERROR_NOTES.get(errors.get("base", ""), _ERROR_NOTES["unknown"])
        self._set_confirm_only()
        note, self._note = self._note, ""
        return self.async_show_menu(
            step_id="bluetooth_confirm",
            menu_options=["flash", "setup"],
            description_placeholders={
                "name": self._discovered_name,
                "address": self.mac,
                "note": f"\n\n**{note}**" if note else "",
            },
        )

    async def async_step_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set it up: run the checks."""
        return await self.async_step_validate()

    async def async_step_flash(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Flash the device's lights, to find which one it is."""
        if self._flash_task is None:
            self._flash_task = self.hass.async_create_task(self._flash())
        if self._flash_task.done():
            return self.async_show_progress_done(next_step_id="flash_done")
        return self.async_show_progress(
            step_id="flash",
            progress_action="flashing",
            progress_task=self._flash_task,
        )

    async def _flash(self) -> str | None:
        """Identify, as the button does; an error key if it couldn't."""
        smartmatic = SmartMatic(self.hass, self.mac)
        try:
            await smartmatic.identify()
        except NotIdle:
            return "flash_not_idle"
        except Exception as e:  # unreachable, unanswered or refused
            _LOGGER.debug("Couldn't flash %s: %s", self.mac, e)
            return "flash_no_reply"
        finally:
            await smartmatic.disconnect()
        return None

    async def async_step_flash_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Back to the form the flash was asked from."""
        assert self._flash_task is not None
        error = self._flash_task.result()
        self._flash_task = None
        if self._discovered:
            self._note = _FLASH_NOTES.get(error or "", "")
            return await self.async_step_bluetooth_confirm()
        return await self.async_step_user(errors={"base": error} if error else None)

    async def async_step_manual_mac(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Handle manual mac step."""
        if user_input is not None:
            self.mac = user_input[CONF_MAC]
            # format_mac returns anything it can't parse unchanged.
            normalized_mac = format_mac(self.mac)
            if not _MAC_RE.fullmatch(normalized_mac):
                return self.async_show_form(
                    step_id="manual_mac",
                    data_schema=vol.Schema({vol.Required(CONF_MAC): str}),
                    errors={"base": "invalid_mac"},
                )

            await self.async_set_unique_id(normalized_mac, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_validate()

        return self.async_show_form(
            step_id="manual_mac",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC): str
                }
            ),
            errors=errors or {},
        )

    async def async_step_validate(
        self, user_input: "dict[str, Any] | None" = None
    ) -> ConfigFlowResult:
        """Start detecting the device's channels."""
        self._smartmatic = SmartMatic(self.hass, self.mac)
        return await self.async_step_detect()

    def _on_progress(self, stage: str, details: dict[str, str], fraction: float) -> None:
        self._stage = (stage, details)
        self.async_update_progress(fraction)
        self._stage_changed.set()

    async def _run_detection(self) -> None:
        smartmatic = self._smartmatic
        assert smartmatic is not None  # set before detection starts
        try:
            self._detect_error = await self._detect(smartmatic)
        finally:
            if not self._reconfiguring:
                await smartmatic.disconnect()

    @property
    def _reconfiguring(self) -> bool:
        return self.source == SOURCE_RECONFIGURE

    async def async_step_detect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show what detection is doing, step by step."""
        # The frontend re-runs this step whenever it refreshes the dialog,
        # so it must be safe to enter repeatedly: one detection, and one
        # waiter per stage. Home Assistant continues the flow when the
        # waiter finishes (the stage changed or detection ended).
        if self._detect_task is None:
            self._detect_task = self.hass.async_create_task(self._run_detection())
        if self._detect_task.done():
            return self.async_show_progress_done(next_step_id="detect_done")

        if self._waiter is None or self._waiter.done():
            self._stage_changed.clear()
            self._waiter = self.hass.async_create_task(
                _first_of(self._detect_task, self._stage_changed.wait())
            )
        stage, details = self._stage
        return self.async_show_progress(
            step_id="detect",
            progress_action=stage,
            description_placeholders=details,
            progress_task=self._waiter,
        )

    async def async_step_detect_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        error = self._detect_error
        # Allow another attempt (e.g. after an error, from the form).
        self._detect_task = self._waiter = None
        if self._reconfiguring:
            if error:
                return await self.async_step_reconfigure(errors={"base": error})
            entry = self._get_reconfigure_entry()
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_COMMAND_CHAR: self.command_char,
                    CONF_REPLY_CHAR: self.reply_char,
                    CONF_HAS_CLOCK: self.has_clock,
                },
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="channels_detected")

        if error:
            if self._discovered:
                return await self.async_step_bluetooth_confirm(errors={"base": error})
            if self._manual_entry:
                return await self.async_step_manual_mac(errors={"base": error})
            return await self.async_step_user(errors={"base": error})
        return self._create_entry()

    def async_remove(self) -> None:
        """Stop detection or a flash if the dialog is closed while it runs."""
        if self._detect_task is not None:
            self._detect_task.cancel()
        if self._flash_task is not None:
            self._flash_task.cancel()

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Detect the command and reply channels again."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            self.mac = entry.options.get(CONF_MAC) or entry.data[CONF_MAC]
            # The device takes one connection: use the loaded instance.
            instance = getattr(getattr(entry, "runtime_data", None), "instance", None)
            if not isinstance(instance, SmartMatic):
                instance = SmartMatic(self.hass, self.mac)
            self._smartmatic = instance
            return await self.async_step_detect()

        return self.async_show_form(step_id="reconfigure", errors=errors or {})

    def _create_entry(self) -> ConfigFlowResult:
        """Add the device under its advertised name.

        Home Assistant's own dialog follows, to rename it and pick an area."""
        return self.async_create_entry(
            title=self.name,
            data={
                CONF_MAC: self.mac,
                CONF_NAME: self.name,
                CONF_COMMAND_CHAR: self.command_char,
                CONF_REPLY_CHAR: self.reply_char,
                CONF_HAS_CLOCK: self.has_clock,
            },
        )


async def _first_of(*aws: Awaitable[object]) -> None:
    """Wait until any of the given awaitables finishes."""
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        if task is not tasks[0]:  # never cancel the detection itself
            task.cancel()
