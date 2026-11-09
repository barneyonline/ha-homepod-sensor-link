"""Owned temperature/humidity entities and per-device diagnostic health."""

from __future__ import annotations
from typing import cast
from .models import Data, Target
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .entity import DeviceSensor
from .coordinator import HomePodConfigEntry, Runtime


from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTemperature, PERCENTAGE
from homeassistant.util import dt as dt_util

from .engine import INTERVAL

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: HomePodConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def add_devices() -> None:
        entities: list[DeviceSensor] = []
        keys = {target["entry_id"] for target in runtime.targets}
        known.intersection_update((key, kind) for key, kind in known if key in keys)
        for target in runtime.targets:
            key = target["entry_id"]
            if (key, "status") not in known:
                known.add((key, "status"))
                entities.append(RecoveryStatus(runtime, target))
            if "pairing" in target:
                for kind in ("temperature", "humidity"):
                    if (key, kind) not in known:
                        known.add((key, kind))
                        entities.append(Measurement(runtime, target, kind))
        if entities:
            async_add_entities(entities)

    add_devices()
    entry.async_on_unload(runtime.coordinator.async_add_listener(add_devices))


class Measurement(DeviceSensor):
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, runtime: Runtime, target: Target, kind: str) -> None:
        super().__init__(runtime, target)
        self.kind = kind
        self._attr_unique_id = self.key + "_" + kind
        self._attr_translation_key = kind
        self._attr_device_class = SensorDeviceClass.TEMPERATURE if kind == "temperature" else SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS if kind == "temperature" else PERCENTAGE

    @property
    def available(self) -> bool:
        record = self.runtime.records.get(self.key)
        sample = self.runtime.readings.get(self.key)
        state = self.runtime.engine.states.get(self.key)
        return bool(
            record
            and record["phase"] in ("verified", "active")
            and not record.get("cancelled")
            and state
            and not state.blocked
            and sample
            and 0 <= (dt_util.utcnow() - sample[0]).total_seconds() <= 2 * INTERVAL
        )

    @property
    def native_value(self) -> float | None:
        sample = self.runtime.readings.get(self.key)
        return sample[1][self.kind] if sample else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.runtime.entities.setdefault(self.key, {})[self.kind] = self.entity_id
        self.async_on_remove(self.runtime.listen_measurements(self.key, self.async_write_ha_state))

    async def async_will_remove_from_hass(self) -> None:
        self.runtime.entities.get(self.key, {}).pop(self.kind, None)
        await super().async_will_remove_from_hass()


class RecoveryStatus(DeviceSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "starting",
        "healthy",
        "authentication_failed",
        "unreachable",
        "blocked",
        "cooldown",
        "repairing",
        "repair_failed",
        "publication_pending",
        "publication_unavailable",
        "paused",
        "prepared",
        "needs_credentials",
        "transition_required",
        "write_uncertain",
        "accepted",
        "verified",
        "active",
        "setup_cancelled",
        "removed",
    ]

    def __init__(self, runtime: Runtime, target: Target) -> None:
        super().__init__(runtime, target)
        self._attr_unique_id = self.key + "_sensor_access"
        self._attr_translation_key = "sensor_access"

    @property
    def native_value(self) -> str:
        record = self.runtime.records.get(self.key)
        if not record:
            return "removed"
        if record.get("cancelled"):
            return "setup_cancelled"
        if record["phase"] not in ("active", "verified"):
            return cast(str, record["phase"])
        return cast(str, self.coordinator.data[self.key]["status"])

    @property
    def extra_state_attributes(self) -> Data:
        data = self.coordinator.data.get(self.key, {})
        record = self.runtime.records.get(self.key, {})
        return {
            "automatic_recovery": record.get("target", {}).get("recovery", True),
            "onboarding": record.get("phase"),
            "onboarding_error": record.get("error"),
            "last_checked": _timestamp(data.get("last_checked")),
            "last_completed": _timestamp(data.get("last_completed")),
            "last_verified": _timestamp(data.get("last_verified")),
            "last_repair": _timestamp(data.get("last_repair")),
            "successful_repairs": data.get("repairs", 0),
            "blocked": data.get("blocked", False),
            "reason": data.get("reason"),
            "last_error_type": data.get("last_error_type"),
            "last_error_location": data.get("last_error_location"),
            "repair_attempts_24h": len(data.get("attempts", [])),
            "publication_pending": data.get("publication_pending", False),
            "publication_attempts_1h": len(data.get("publication_attempts", [])),
            "recovery_access_status": data.get("recovery_status", "not_checked"),
        }


def _timestamp(value: float | None) -> str | None:
    return dt_util.utc_from_timestamp(value).isoformat() if value is not None else None
