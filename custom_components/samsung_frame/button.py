"""Button entity – triggers an immediate artwork update on the Frame TV."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_TV_IP
from .coordinator import SamsungFrameCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SamsungFrameCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SamsungFrameButton(coordinator, entry)])


class SamsungFrameButton(CoordinatorEntity[SamsungFrameCoordinator], ButtonEntity):
    """Button that pushes a new artwork to the Frame TV right now."""

    _attr_has_entity_name = True
    _attr_name = "Update Artwork Now"
    _attr_icon = "mdi:image-refresh"

    def __init__(
        self,
        coordinator: SamsungFrameCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_update_artwork"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"Samsung Frame ({self._entry.data.get(CONF_TV_IP, 'TV')})",
            manufacturer="Samsung",
            model="The Frame",
        )

    async def async_press(self) -> None:
        """Handle the button press: immediately push a new artwork."""
        await self.coordinator.async_force_update()
