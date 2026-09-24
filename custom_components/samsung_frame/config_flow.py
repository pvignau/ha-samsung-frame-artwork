"""Config flow for Samsung Frame Artwork integration."""
from __future__ import annotations

import asyncio
import logging
import re
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
    CONF_MAX_TV_IMAGES, CONF_CACHE_MAX_MB, CONF_INDEX_REFRESH_DAYS,
    DEFAULT_TV_PORT, DEFAULT_THEFRAMETV_WEIGHT, DEFAULT_ICLOUD_WEIGHT,
    DEFAULT_INTERVAL_HOURS, DEFAULT_HISTORY_SIZE,
    DEFAULT_IMAGE_MODE, DEFAULT_IMAGE_WIDTH, DEFAULT_IMAGE_HEIGHT,
    DEFAULT_MAX_TV_IMAGES, DEFAULT_CACHE_MAX_MB, DEFAULT_INDEX_REFRESH_DAYS,
    MIN_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION,
)

_LOGGER = logging.getLogger(__name__)

# Apple hands out two shapes for a public shared album, both with the token in
# the fragment: https://www.icloud.com/photos/#<token> (older) and
# https://www.icloud.com/sharedalbum/#<token> (current). The '#' and a trailing
# "#<album name>" are both optional. This must stay in sync with
# sources.icloud.extract_token(), which does the actual parsing.
ICLOUD_URL_RE = re.compile(
    r"^https://(?:[A-Za-z0-9-]+\.)*icloud\.com/(?:photos|sharedalbum)/#?[A-Za-z0-9_-]+",
    re.IGNORECASE,
)


async def _async_check_host_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    """TCP-level check: returns True if the host accepts connections on the given port."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Samsung Frame TV %s:%s unreachable: %s", host, port, err)
        return False


STEP_TV_SCHEMA = vol.Schema({
    vol.Required(CONF_TV_IP): str,
    vol.Optional(CONF_TV_PORT, default=DEFAULT_TV_PORT): int,
})


def build_options_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the sources/rotation/image schema, pre-filled with ``defaults``.

    Used both by the initial setup wizard (no defaults -> integration defaults)
    and by the options flow (defaults -> current entry data + options).
    """
    cfg = defaults or {}

    def _default(key: str, fallback: Any) -> Any:
        return cfg.get(key, fallback)

    return vol.Schema({
        vol.Optional(
            CONF_THEFRAMETV_ENABLED, default=_default(CONF_THEFRAMETV_ENABLED, True)
        ): bool,
        vol.Optional(
            CONF_THEFRAMETV_WEIGHT,
            default=_default(CONF_THEFRAMETV_WEIGHT, DEFAULT_THEFRAMETV_WEIGHT),
        ): vol.All(int, vol.Range(min=1, max=100)),
        vol.Optional(
            CONF_ICLOUD_ENABLED, default=_default(CONF_ICLOUD_ENABLED, False)
        ): bool,
        vol.Optional(
            CONF_ICLOUD_WEIGHT,
            default=_default(CONF_ICLOUD_WEIGHT, DEFAULT_ICLOUD_WEIGHT),
        ): vol.All(int, vol.Range(min=1, max=100)),
        vol.Optional(
            CONF_ICLOUD_URL, default=_default(CONF_ICLOUD_URL, "")
        ): str,
        vol.Optional(
            CONF_INTERVAL_HOURS,
            default=_default(CONF_INTERVAL_HOURS, DEFAULT_INTERVAL_HOURS),
        ): vol.All(int, vol.Range(min=1, max=168)),
        vol.Optional(
            CONF_SKIP_WHEN_WATCHING,
            default=_default(CONF_SKIP_WHEN_WATCHING, DEFAULT_SKIP_WHEN_WATCHING),
        ): bool,
        vol.Optional(
            CONF_HISTORY_SIZE,
            default=_default(CONF_HISTORY_SIZE, DEFAULT_HISTORY_SIZE),
        ): vol.All(int, vol.Range(min=5, max=200)),
        vol.Optional(
            CONF_MAX_TV_IMAGES,
            default=_default(CONF_MAX_TV_IMAGES, DEFAULT_MAX_TV_IMAGES),
        ): vol.All(int, vol.Range(min=1, max=100)),
        vol.Optional(
            CONF_CACHE_MAX_MB,
            default=_default(CONF_CACHE_MAX_MB, DEFAULT_CACHE_MAX_MB),
        ): vol.All(int, vol.Range(min=50, max=5000)),
        vol.Optional(
            CONF_INDEX_REFRESH_DAYS, CONF_SKIP_WHEN_WATCHING,
            default=_default(CONF_INDEX_REFRESH_DAYS, DEFAULT_INDEX_REFRESH_DAYS),
        ): vol.All(int, vol.Range(min=1, max=90)),
        vol.Optional(
            CONF_IMAGE_MODE, default=_default(CONF_IMAGE_MODE, DEFAULT_IMAGE_MODE)
        ): vol.In(["fill", "fit"]),
        vol.Optional(
            CONF_IMAGE_WIDTH, default=_default(CONF_IMAGE_WIDTH, DEFAULT_IMAGE_WIDTH)
        ): vol.All(int, vol.Range(min=MIN_IMAGE_DIMENSION, max=MAX_IMAGE_DIMENSION)),
        vol.Optional(
            CONF_IMAGE_HEIGHT, default=_default(CONF_IMAGE_HEIGHT, DEFAULT_IMAGE_HEIGHT)
        ): vol.All(int, vol.Range(min=MIN_IMAGE_DIMENSION, max=MAX_IMAGE_DIMENSION)),
    })


def _validate_sources(user_input: dict) -> dict:
    """Return errors dict (empty = valid).

    All independent problems are reported at once so the user does not have to
    submit the form repeatedly to discover them one by one.
    """
    errors: dict[str, str] = {}

    icloud_enabled = bool(user_input.get(CONF_ICLOUD_ENABLED))
    icloud_url = (user_input.get(CONF_ICLOUD_URL) or "").strip()

    if icloud_enabled:
        if not icloud_url:
            errors[CONF_ICLOUD_URL] = "icloud_url_required"
        elif not ICLOUD_URL_RE.match(icloud_url):
            errors[CONF_ICLOUD_URL] = "icloud_url_invalid"

    if not user_input.get(CONF_THEFRAMETV_ENABLED) and not icloud_enabled:
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
                await self.async_set_unique_id(user_input[CONF_TV_IP])
                self._abort_if_unique_id_configured()
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
            data_schema=build_options_schema(user_input),
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
        # Deliberately NOT assigning to self.config_entry: Home Assistant
        # deprecated that and provides the entry on the flow itself.
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
            merged = {**merged, **user_input}

        return self.async_show_form(
            step_id="init",
            data_schema=build_options_schema(merged),
            errors=errors,
        )
