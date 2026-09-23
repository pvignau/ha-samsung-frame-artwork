"""Samsung Frame Artwork integration setup."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PLATFORMS
from .coordinator import SamsungFrameCoordinator

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Samsung Frame from a config entry."""
    coordinator = SamsungFrameCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register domain-level services once (on first entry setup)
    if not hass.services.has_service(DOMAIN, "update_artwork"):
        _register_services(hass)

    # Reload when options change
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

        # Remove services only once the last entry has been unloaded
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, "update_artwork")
            hass.services.async_remove(DOMAIN, "refresh_theframetv_index")
    else:
        _LOGGER.warning(
            "Failed to unload Samsung Frame entry %s; services left registered",
            entry.entry_id,
        )

    return unload_ok


def _register_services(hass: HomeAssistant) -> None:
    """Register domain-level services (called once, routes to all coordinators)."""

    async def handle_update_artwork(call: ServiceCall) -> None:
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await coordinator.async_force_update()

    async def handle_refresh_index(call: ServiceCall) -> None:
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await coordinator.async_refresh_theframetv_index()

    hass.services.async_register(DOMAIN, "update_artwork", handle_update_artwork)
    hass.services.async_register(DOMAIN, "refresh_theframetv_index", handle_refresh_index)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)
