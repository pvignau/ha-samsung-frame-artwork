"""Config flow for Samsung Frame Artwork integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_TV_IP, CONF_TV_PORT,
    CONF_THEFRAMETV_ENABLED, CONF_THEFRAMETV_WEIGHT,
    CONF_ICLOUD_ENABLED, CONF_ICLOUD_WEIGHT, CONF_ICLOUD_URL,
    CONF_INTERVAL_HOURS, CONF_HISTORY_SIZE,
    CONF_IMAGE_MODE, CONF_IMAGE_WIDTH, CONF_IMAGE_HEIGHT,
    DEFAULT_TV_PORT, DEFAULT_THEFRAMETV_WEIGHT, DEFAULT_ICLOUD_WEIGHT,
    DEFAULT_INTERVAL_HOURS, DEFAULT_HISTORY_SIZE,
    DEFAULT_IMAGE_MODE, DEFAULT_IMAGE_WIDTH, DEFAULT_IMAGE_HEIGHT,
)

_LOGGER = logging.getLogger(__name__)


async def _async_check_host_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    """TCP-level check: returns True if the host accepts connections on the given port."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


STEP_TV_SCHEMA = vol.Schema({
    vol.Required(CONF_TV_IP): str,
    vol.Optional(CONF_TV_PORT, default=DEFAULT_TV_PORT): int,
})

STEP_OPTIONS_SCHEMA = vol.Schema({
    vol.Optional(CONF_THEFRAMETV_ENABLED, default=True): bool,
    vol.Optional(CONF_THEFRAMETV_WEIGHT, default=DEFAULT_THEFRAMETV_WEIGHT): vol.All(
        int, vol.Range(min=1, max=100)
    ),
    vol.Optional(CONF_ICLOUD_ENABLED, default=False): bool,
    vol.Optional(CONF_ICLOUD_WEIGHT, default=DEFAULT_ICLOUD_WEIGHT): vol.All(
        int, vol.Range(min=1, max=100)
    ),
    vol.Optional(CONF_ICLOUD_URL, default=""): str,
    vol.Optional(CONF_INTERVAL_HOURS, default=DEFAULT_INTERVAL_HOURS): vol.All(
        int, vol.Range(min=1, max=168)
    ),
    vol.Optional(CONF_HISTORY_SIZE, default=DEFAULT_HISTORY_SIZE): vol.All(
        int, vol.Range(min=5, max=200)
    ),
    vol.Optional(CONF_IMAGE_MODE, default=DEFAULT_IMAGE_MODE): vol.In(["fill", "fit"]),
    vol.Optional(CONF_IMAGE_WIDTH, default=DEFAULT_IMAGE_WIDTH): int,
    vol.Optional(CONF_IMAGE_HEIGHT, default=DEFAULT_IMAGE_HEIGHT): int,
})


def _options_schema_with_defaults(cfg: dict) -> vol.Schema:
    return vol.Schema({
        vol.Optional(
            CONF_THEFRAMETV_ENABLED,
            default=cfg.get(CONF_THEFRAMETV_ENABLED, True),
        ): bool,
        vol.Optional(
            CONF_THEFRAMETV_WEIGHT,
            default=cfg.get(CONF_THEFRAMETV_WEIGHT, DEFAULT_THEFRAMETV_WEIGHT),
        ): vol.All(int, vol.Range(min=1, max=100)),
        vol.Optional(
            CONF_ICLOUD_ENABLED,
            default=cfg.get(CONF_ICLOUD_ENABLED, False),
        ): bool,
        vol.Optional(
            CONF_ICLOUD_WEIGHT,
            default=cfg.get(CONF_ICLOUD_WEIGHT, DEFAULT_ICLOUD_WEIGHT),
        ): vol.All(int, vol.Range(min=1, max=100)),
        vol.Optional(
            CONF_ICLOUD_URL,
            default=cfg.get(CONF_ICLOUD_URL, ""),
        ): str,
        vol.Optional(
            CONF_INTERVAL_HOURS,
            default=cfg.get(CONF_INTERVAL_HOURS, DEFAULT_INTERVAL_HOURS),
        ): vol.All(int, vol.Range(min=1, max=168)),
        vol.Optional(
            CONF_HISTORY_SIZE,
            default=cfg.get(CONF_HISTORY_SIZE, DEFAULT_HISTORY_SIZE),
        ): vol.All(int, vol.Range(min=5, max=200)),
        vol.Optional(
            CONF_IMAGE_MODE,
            default=cfg.get(CONF_IMAGE_MODE, DEFAULT_IMAGE_MODE),
        ): vol.In(["fill", "fit"]),
        vol.Optional(
            CONF_IMAGE_WIDTH,
            default=cfg.get(CONF_IMAGE_WIDTH, DEFAULT_IMAGE_WIDTH),
        ): int,
        vol.Optional(
            CONF_IMAGE_HEIGHT,
            default=cfg.get(CONF_IMAGE_HEIGHT, DEFAULT_IMAGE_HEIGHT),
        ): int,
    })


def _validate_sources(user_input: dict) -> dict:
    """Return errors dict (empty = valid)."""
    errors: dict[str, str] = {}
    if user_input.get(CONF_ICLOUD_ENABLED) and not user_input.get(CONF_ICLOUD_URL, "").strip():
        errors[CONF_ICLOUD_URL] = "icloud_url_required"
    elif not user_input.get(CONF_THEFRAMETV_ENABLED) and not user_input.get(CONF_ICLOUD_ENABLED):
        errors["base"] = "no_source_enabled"
    return errors


class SamsungFrameConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle the initial setup wizard."""

    VERSION = 1

    def __init__(self) -> None:
        self._tv_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: TV connection details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            ok = await _async_check_host_reachable(
                user_input[CONF_TV_IP],
                user_input.get(CONF_TV_PORT, DEFAULT_TV_PORT),
            )
            if ok:
                self._tv_data = user_input
                return await self.async_step_sources()
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_TV_SCHEMA,
            errors=errors,
        )

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Sources, rotation and image settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate_sources(user_input)
            if not errors:
                data = {**self._tv_data, **user_input}
                return self.async_create_entry(
                    title=f"Samsung Frame ({self._tv_data[CONF_TV_IP]})",
                    data=data,
                )

        return self.async_show_form(
            step_id="sources",
            data_schema=STEP_OPTIONS_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "SamsungFrameOptionsFlow":
        return SamsungFrameOptionsFlow(config_entry)


class SamsungFrameOptionsFlow(config_entries.OptionsFlow):
    """Handle reconfiguration of sources / rotation / image settings."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        merged = {**self._config_entry.data, **self._config_entry.options}

        if user_input is not None:
            errors = _validate_sources(user_input)
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema_with_defaults(merged),
            errors=errors,
        )
