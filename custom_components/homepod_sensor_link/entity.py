"""Common translated coordinator entity and its device association."""

from __future__ import annotations
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from .coordinator import Runtime
from .credentials import DOMAIN
from .models import Target


class DeviceSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, runtime: Runtime, target: Target) -> None:
        super().__init__(runtime.coordinator)
        self.runtime = runtime
        self.key = target["entry_id"]
        name = runtime.records[self.key].get("name", "HomePod")
        self._attr_device_info = DeviceInfo(
            **{
                "identifiers": {(DOMAIN, self.key)},
                "name": name,
                "manufacturer": "Apple",
                "model": "HomePod Sensor Link",
            }
        )
