"""Read-only candidate discovery and verification of adopted native sensors."""

from __future__ import annotations
from typing import cast
from .models import Data
from homeassistant.core import HomeAssistant, State
from collections.abc import AsyncIterator, Callable, Awaitable
from datetime import datetime
from zeroconf.asyncio import AsyncZeroconf


from contextlib import asynccontextmanager
import math
import sys

from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller.ip import IpController
from homeassistant.components import zeroconf
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .backend import HomeKitBackend
from .engine import INTERVAL, SafetyFailure


def sensor_ids(hass: HomeAssistant, entry_id: str) -> list[str]:
    registry = er.async_get(hass)
    by_class = {}
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.domain == "sensor" and not entity.disabled_by:
            kind = entity.device_class or entity.original_device_class
            if kind in ("temperature", "humidity"):
                if kind in by_class:
                    raise SafetyFailure("ambiguous_native_sensors")
                by_class[kind] = entity.entity_id
    if set(by_class) != {"temperature", "humidity"}:
        raise SafetyFailure("native_sensor_inventory_changed")
    return list(by_class.values())


def verify_readings(hass: HomeAssistant, entry_id: str, ids: list[str], since: datetime | None = None) -> None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.disabled_by is not None or entry.state.value != "loaded":
        raise OSError("native_entry_not_loaded")
    now = dt_util.utcnow()
    states = [hass.states.get(entity_id) for entity_id in ids]
    try:
        valid = all(
            s is not None
            and math.isfinite(float(s.state))
            and 0 <= (now - s.last_reported).total_seconds() <= 2 * INTERVAL
            and (since is None or s.last_reported >= since)
            for s in states
        )
    except (ValueError, TypeError):
        valid = False
    if not valid or {cast(State, s).attributes.get("device_class") for s in states} != {"temperature", "humidity"}:
        raise OSError("native_readings_not_fresh")


@asynccontextmanager
async def controller_session(hass: HomeAssistant) -> AsyncIterator[tuple[IpController, AsyncZeroconf]]:
    zc = await zeroconf.async_get_async_instance(hass)
    controller = IpController(CharacteristicCacheMemory(), zc)
    try:
        await cast(Callable[[], Awaitable[None]], controller.async_start)()
        yield controller, zc
    finally:
        # No pairing is stored on this read-only discovery controller.
        primary = sys.exception()
        try:
            await cast(Callable[[], Awaitable[None]], controller.async_stop)()
        except Exception:
            if primary is not None:
                raise primary from None
            raise OSError("discovery_cleanup_failed") from None


async def discover_devices(hass: HomeAssistant) -> dict[str, Data]:
    """Only public candidate metadata; pins always come from trusted records."""
    found = {}
    async with controller_session(hass) as (controller, _):
        async for device in controller.async_discover():
            description = device.description
            if str(description.model).lower().startswith("homepod"):
                found[description.id.lower()] = {
                    "name": description.name,
                    "endpoint": HomeKitBackend._endpoint(description.addresses, description.port),
                }
    registry = dr.async_get(hass)
    for entry in hass.config_entries.async_entries("homekit_controller"):
        devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
        if entry.disabled_by is None and (
            any(str(d.model).lower().startswith("homepod") for d in devices)
            or str(entry.data.get("AccessoryPairingID", "")).lower() in found
        ):
            identifier = str(entry.data["AccessoryPairingID"]).lower()
            found[identifier] = {"name": entry.title, "native_entry_id": entry.entry_id}
    return found
