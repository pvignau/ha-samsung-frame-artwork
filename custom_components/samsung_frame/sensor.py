"""Sensor entity – shows the currently displayed artwork on the Frame TV."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
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
    async_add_entities([SamsungFrameSensor(coordinator, entry)])


class SamsungFrameSensor(CoordinatorEntity[SamsungFrameCoordinator], SensorEntity):
    """Sensor that tracks the artwork currently shown on the Frame TV."""

    _attr_has_entity_name = True
    _attr_name = "Current Artwork"
    _attr_icon = "mdi:television-play"

    def __init__(
        self,
        coordinator: SamsungFrameCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_current_artwork"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"Samsung Frame ({self._entry.data.get(CONF_TV_IP, 'TV')})",
            manufacturer="Samsung",
            model="The Frame",
        )

    @property
    def native_value(self) -> str | None:
        """Return the artwork name as the sensor state."""
        data = self.coordinator.data or {}
        return data.get("name")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "source": data.get("source"),
            "last_update": data.get("last_update"),
            "image_path": data.get("path"),
        }
