"""Constants for the Weishaupt integration."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)

DOMAIN = "weishaupt"

CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60  # seconds
DEFAULT_PATH = "/parameter.json"
REQUEST_TIMEOUT = 10  # seconds
RETRIES = 1  # extra attempts per update after a bad answer
RETRY_DELAY = 2  # seconds
MAX_STALE_UPDATES = 3  # consecutive failed updates before entities go unavailable

# module types used in telegrams
MODULE_HEATER = 10  # WTC-G/-O heat generator
MODULE_ROOM_UNIT = 6  # room unit / remote control


@dataclass(frozen=True)
class WeishauptParameter:
    """One numeric value of the heating controller."""

    key: str  # also the translation key
    index: int
    module: int = MODULE_HEATER
    factor: float = 1.0
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    entity_category: EntityCategory | None = None
    precision: int | None = None


@dataclass(frozen=True)
class WeishauptBinaryParameter:
    """One on/off value of the heating controller (non-zero = on)."""

    key: str  # also the translation key
    index: int
    module: int = MODULE_HEATER
    device_class: BinarySensorDeviceClass | None = None
    entity_category: EntityCategory | None = None


_TEMP = {
    "unit": UnitOfTemperature.CELSIUS,
    "device_class": SensorDeviceClass.TEMPERATURE,
    "factor": 0.1,
    "precision": 1,
}

PARAMETERS: tuple[WeishauptParameter, ...] = (
    WeishauptParameter("outside_temperature", 12, **_TEMP),
    WeishauptParameter("damped_outside_temperature", 2572, **_TEMP),
    WeishauptParameter("flow_temperature", 3101, **_TEMP),
    WeishauptParameter("exhaust_temperature", 325, **_TEMP),
    WeishauptParameter("hot_water_temperature", 14, **_TEMP),
    WeishauptParameter("heat_demand", 2, **_TEMP),
    WeishauptParameter("room_temperature", 17, module=MODULE_ROOM_UNIT, **_TEMP),
    WeishauptParameter("room_setpoint", 4, module=MODULE_ROOM_UNIT, **_TEMP),
    WeishauptParameter("load_state", 138, unit=PERCENTAGE),
    # The device itself only shows raw numbers for these two (no text table).
    WeishauptParameter(
        "operating_phase", 373, entity_category=EntityCategory.DIAGNOSTIC
    ),
    # 0 = no error, anything else is an error code
    WeishauptParameter("error", 1, entity_category=EntityCategory.DIAGNOSTIC),
    WeishauptParameter(
        "burner_cycles",
        3158,
        factor=1000,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    WeishauptParameter(
        "burner_operating_hours",
        3159,
        factor=100,
        unit=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    WeishauptParameter(
        "time_since_service",
        700,
        factor=10,
        unit=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

# Info numbers derived from the device web UI (idT0051, idT05D9, idT05DA).
BINARY_PARAMETERS: tuple[WeishauptBinaryParameter, ...] = (
    WeishauptBinaryParameter(
        "flame", 81, device_class=BinarySensorDeviceClass.RUNNING
    ),
    WeishauptBinaryParameter(
        "gas_valve_1", 1497, device_class=BinarySensorDeviceClass.OPENING
    ),
    WeishauptBinaryParameter(
        "gas_valve_2", 1498, device_class=BinarySensorDeviceClass.OPENING
    ),
)
