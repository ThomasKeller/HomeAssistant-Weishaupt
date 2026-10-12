"""Binary sensors for the Weishaupt integration."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WeishauptConfigEntry
from .const import BINARY_PARAMETERS, DOMAIN, WeishauptBinaryParameter
from .coordinator import WeishauptCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WeishauptConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        WeishauptBinarySensor(coordinator, entry, param) for param in BINARY_PARAMETERS
    ]
    entities.append(WeishauptProblemSensor(coordinator, entry))
    async_add_entities(entities)


class _WeishauptBase(CoordinatorEntity[WeishauptCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: WeishauptCoordinator, entry: WeishauptConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Weishaupt Heizung",
            manufacturer="Weishaupt",
            configuration_url=f"http://{entry.data[CONF_HOST]}/",
        )


class WeishauptBinarySensor(_WeishauptBase):
    """On/off value of the controller (flame, gas valves); non-zero = on."""

    def __init__(
        self,
        coordinator: WeishauptCoordinator,
        entry: WeishauptConfigEntry,
        param: WeishauptBinaryParameter,
    ) -> None:
        super().__init__(coordinator, entry, param.key)
        self._attr_device_class = param.device_class
        self._attr_entity_category = param.entity_category

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self._key)
        return None if value is None else value != 0


class WeishauptProblemSensor(_WeishauptBase):
    """On when the controller reports an error code other than 0."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: WeishauptCoordinator, entry: WeishauptConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "problem")

    @property
    def is_on(self) -> bool | None:
        code = self.coordinator.data.get("error")
        return None if code is None else code != 0
