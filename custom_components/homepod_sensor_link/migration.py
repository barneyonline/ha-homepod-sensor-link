"""Pause adopted devices using only this integration's private inventory."""

from __future__ import annotations

from copy import deepcopy

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .vault import get_vault


async def migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Never inspect a native entry or convert its controller into an owned one."""
    if entry.version != 2:
        return False
    vault = await get_vault(hass)
    async with vault.lock:
        data = deepcopy(vault.data)
        pending = set()
        for key, record in data["records"].items():
            if record["target"].get("owned") is True and record["phase"] != "transition_required":
                continue
            pending.add(key)
            target = record["target"]
            state = data["states"].get(key, {})
            attempts = sorted(set(record.get("onboarding_attempts", []) + state.get("attempts", [])))
            # Discard native controller pins, cached pairing data, imported
            # administrator keys and any outstanding pairing checkpoints.
            # Retain only candidate metadata and conservative attempt history.
            data["records"][key] = {
                "target": {
                    "entry_id": key,
                    "accessory_id": target["accessory_id"],
                    "owned": True,
                    "recovery": False,
                },
                "name": record.get("name", "HomePod"),
                "phase": "transition_required",
                "onboarding_attempts": attempts,
                "last_onboarding_attempt": record.get("last_onboarding_attempt"),
            }
            # Public counters remain, but cannot schedule a native recovery.
            data["states"][key] = {
                "attempts": state.get("attempts", []),
                "repairs": state.get("repairs", 0),
            }
        # Persist before updating the HA config version. Repeating after an
        # interrupted update retains the same paused devices and owned keys.
        await vault._commit(data)
    hass.config_entries.async_update_entry(
        entry,
        version=3,
        minor_version=1,
        data={
            **entry.data,
            "pins": {key: pin for key, pin in entry.data.get("pins", {}).items() if key not in pending},
            "admin_pins": {key: pin for key, pin in entry.data.get("admin_pins", {}).items() if key not in pending},
        },
    )
    return True
