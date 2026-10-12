"""Sensors for the Weishaupt integration."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WeishauptConfigEntry
from .const import DOMAIN, PARAMETERS, WeishauptParameter
from .coordinator import WeishauptCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WeishauptConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        WeishauptSensor(coordinator, entry, param) for param in PARAMETERS
    )


class WeishauptSensor(CoordinatorEntity[WeishauptCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WeishauptCoordinator,
        entry: WeishauptConfigEntry,
        param: WeishauptParameter,
    ) -> None:
        super().__init__(coordinator)
        self._key = param.key
        self._attr_translation_key = param.key
        self._attr_unique_id = f"{entry.entry_id}_{param.key}"
        self._attr_native_unit_of_measurement = param.unit
        self._attr_device_class = param.device_class
        self._attr_state_class = param.state_class
        self._attr_entity_category = param.entity_category
        self._attr_suggested_display_precision = param.precision
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Weishaupt Heizung",
            manufacturer="Weishaupt",
            configuration_url=f"http://{entry.data[CONF_HOST]}/",
        )

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get(self._key)
