"""Anonymous health only: omit config-entry, controller and accessory identities."""

from __future__ import annotations
from .models import Data
from homeassistant.core import HomeAssistant
from .coordinator import HomePodConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: HomePodConfigEntry) -> Data:
    runtime = getattr(entry, "runtime_data", None)
    return {"version": "2.0.0", "recovery_default": True, "devices": runtime.public_snapshot() if runtime else []}
