"""HomePod Sensor Link setup, actions, unload and explicit removal."""

from __future__ import annotations
from collections.abc import Awaitable, Callable
from typing import cast
from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller.ip import IpController
from homeassistant.components import persistent_notification, zeroconf
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, Event, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError, HomeAssistantError
from homeassistant.helpers import device_registry as dr, config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store
from .repairs import create_issue
from .models import Data
from .onboarding import refresh_names
from .credentials import DOMAIN, public_targets
from .engine import SafetyFailure
from .vault import get_vault, Vault
from .coordinator import (
    Runtime as Runtime,
    OWNER as OWNER,
    HomePodConfigEntry,
    conflicting_recovery,
    validate_state_location as validate_state_location,
    remove_owned_registry,
    clear_device_issues,
)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: Data) -> bool:
    def runtime() -> Runtime:
        entry = hass.config_entries.async_get_entry(hass.data.get(OWNER, ""))
        active = getattr(entry, "runtime_data", None)
        if active is None or active.stopped:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
        return cast(Runtime, active)

    async def check_now(_call: ServiceCall) -> Data:
        active = runtime()
        await active.coordinator.async_refresh()
        if not active.coordinator.last_update_success:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="check_failed")
        return {"devices": active.public_snapshot()}

    async def check_access(_call: ServiceCall) -> Data:
        active = runtime()
        try:
            await active.audit.check(force=True)
        except Exception:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="check_failed") from None
        return {"devices": active.public_snapshot()}

    for name, handler in (("check_now", check_now), ("check_recovery_access", check_access)):
        async_register_admin_service(hass, DOMAIN, name, handler, supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HomePodConfigEntry) -> bool:
    if entry.version != 2:
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="unsupported_configuration")
    if conflicting_recovery(hass) or OWNER in hass.data:
        create_issue(hass, "engine_conflict", "engine_conflict", entry.entry_id, ir.IssueSeverity.ERROR)
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="engine_conflict")
    ir.async_delete_issue(hass, DOMAIN, "engine_conflict")
    hass.data[OWNER] = entry.entry_id
    runtime, controller = None, None
    try:
        vault = await get_vault(hass)
        records = vault.records()
        keys = entry.data["device_keys"]
        if len(keys) != len(set(keys)) or any(key not in records for key in keys):
            raise SafetyFailure("configured_inventory_changed")
        targets = [records[key]["target"] for key in keys]
        for key, pin in entry.data.get("pins", {}).items():
            if key not in records or public_targets([records[key]["target"]])[0] != pin:
                raise SafetyFailure("stored_identity_changed")
        for key, pin in entry.data.get("admin_pins", {}).items():
            if key not in records or records[key].get("source_pin") != pin:
                raise SafetyFailure("stored_administrator_changed")
        if len({t["accessory_id"].lower() for t in targets}) != len(targets):
            raise SafetyFailure("duplicate_accessory")
        zc = await zeroconf.async_get_async_instance(hass)
        controller = IpController(CharacteristicCacheMemory(), zc)
        await cast(Callable[[], Awaitable[None]], controller.async_start)()
        runtime = Runtime(hass, entry, targets, controller, zc, vault)
        await runtime.initialize()
        await refresh_names(hass, runtime.records, vault)
        entry.runtime_data = runtime
        await runtime.preflight()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException as error:
        try:
            if runtime:
                await runtime.stop()
            elif controller:
                await cast(Callable[[], Awaitable[None]], controller.async_stop)()
        except BaseException:
            pass
        if runtime is None or runtime.closed:
            hass.data.pop(OWNER, None)
        if not isinstance(error, Exception):
            raise
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="setup_failed") from None

    async def started(_hass: HomeAssistant) -> None:
        if runtime.stopped:
            return
        runtime.watchdog.start()
        await runtime.coordinator.async_refresh()

    async def stop(_event: Event) -> None:
        await runtime.stop()

    entry.async_on_unload(async_at_started(hass, started))
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HomePodConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.stop()
    if hass.data.get(OWNER) == entry.entry_id:
        hass.data.pop(OWNER)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: HomePodConfigEntry) -> None:
    runtime = getattr(entry, "runtime_data", None)
    if runtime:
        await runtime.stop()
    # Abort outstanding flows before purge; Vault drains cancelled atomic I/O.
    for flow in list(hass.config_entries.flow.async_progress_by_handler(DOMAIN)):
        hass.config_entries.flow.async_abort(flow["flow_id"])
    vault = hass.data.get(DOMAIN + ".vault") or Vault(hass)
    await vault.purge()
    await Store(hass, 1, DOMAIN + ".state", private=True, atomic_writes=True).async_remove()
    ir.async_delete_issue(hass, DOMAIN, "checks_unavailable")
    ir.async_delete_issue(hass, DOMAIN, "engine_conflict")
    ir.async_delete_issue(hass, DOMAIN, "monitoring_stalled")
    persistent_notification.async_dismiss(hass, DOMAIN + "_watchdog_" + entry.entry_id)
    for key in entry.data.get("device_keys", []):
        clear_device_issues(hass, key)
    if runtime:
        runtime.records.clear()
        runtime.targets.clear()
        runtime.readings.clear()
        runtime.engine.targets.clear()
        runtime.engine.states.clear()
    if hass.data.get(OWNER) == entry.entry_id:
        hass.data.pop(OWNER)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: HomePodConfigEntry, device: dr.DeviceEntry
) -> bool:
    keys = [key for domain, key in device.identifiers if domain == DOMAIN]
    if len(keys) != 1 or keys[0] not in entry.data.get("device_keys", []):
        return False
    runtime = getattr(entry, "runtime_data", None)
    if runtime:
        await runtime.remove_device(keys[0])
    else:
        if OWNER in hass.data:
            return False
        vault = await get_vault(hass)
        await vault.remove(keys[0])
        hass.config_entries.async_update_entry(
            entry,
            data={
                "device_keys": [k for k in entry.data["device_keys"] if k != keys[0]],
                "pins": {k: v for k, v in entry.data.get("pins", {}).items() if k != keys[0]},
                "admin_pins": {k: v for k, v in entry.data.get("admin_pins", {}).items() if k != keys[0]},
            },
        )
        remove_owned_registry(hass, entry.entry_id, keys[0])
        clear_device_issues(hass, keys[0])
    return True
