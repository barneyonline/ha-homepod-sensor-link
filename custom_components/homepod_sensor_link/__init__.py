"""HomePod Sensor Link setup, actions, unload and explicit removal."""

from __future__ import annotations
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast
from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller.ip import IpController
from homeassistant.components import persistent_notification, zeroconf
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, Event, SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError, HomeAssistantError
from homeassistant.helpers import device_registry as dr, config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey
from .migration import migrate_entry
from .repairs import create_issue
from .models import Data
from .onboarding import discover_devices, refresh_names
from .credentials import DOMAIN, public_targets
from .engine import SafetyFailure
from .vault import async_drain_future, get_vault, Vault
from .coordinator import (
    Runtime as Runtime,
    OWNER as OWNER,
    HomePodConfigEntry,
    conflicting_recovery,
    validate_state_location as validate_state_location,
    remove_owned_registry,
    clear_device_issues,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.SENSOR]


@dataclass(frozen=True)
class _EntryCleanup:
    entry: HomePodConfigEntry
    cleanup: Callable[[ConfigEntryState], Awaitable[None]]


_PENDING_ENTRY_CLEANUP: HassKey[_EntryCleanup] = HassKey(DOMAIN + ".pending_entry_cleanup")


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


async def async_migrate_entry(hass: HomeAssistant, entry: HomePodConfigEntry) -> bool:
    return await migrate_entry(hass, entry)


async def _drain_cleanup(cleanup: Awaitable[None], *, propagate_cancellation: bool = True) -> BaseException | None:
    task = asyncio.gather(cleanup, return_exceptions=True)
    cancellation = await async_drain_future(task)
    if cancellation is not None and propagate_cancellation:
        raise cancellation
    return task.result()[0]


async def _finish_entry_cleanup(
    hass: HomeAssistant, pending: _EntryCleanup, state: ConfigEntryState
) -> BaseException | None:
    async def finish() -> None:
        await pending.cleanup(state)
        hass.data.pop(_PENDING_ENTRY_CLEANUP)
        hass.data.pop(OWNER, None)

    return await _drain_cleanup(finish())


async def async_finish_removed_entry_cleanup(hass: HomeAssistant) -> bool:
    while (pending := hass.data.get(_PENDING_ENTRY_CLEANUP)) is not None:
        if hass.config_entries.async_get_entry(pending.entry.entry_id) is not None:
            break
        # HA removes the entry from its index before this lock is released.
        # Its removal hook can replace the pending cleanup while we wait.
        async with pending.entry.setup_lock:
            if hass.data.get(_PENDING_ENTRY_CLEANUP) is pending:
                return await _finish_entry_cleanup(hass, pending, ConfigEntryState.NOT_LOADED) is None
    return True


async def _unload_startup_platforms(
    hass: HomeAssistant, entry: HomePodConfigEntry, *, entry_state: ConfigEntryState
) -> bool:
    if entry_state is ConfigEntryState.NOT_LOADED:
        # HA skips forwarding unload for NOT_LOADED entries.
        from homeassistant.components.sensor import async_unload_entry as async_unload_sensor_entry

        await async_unload_sensor_entry(hass, entry)
    elif not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    # HA drops the component's entry before destroying the entity platform.
    # If destruction failed, a retry can succeed while that platform still exists.
    for platform in tuple(async_get_platforms(hass, DOMAIN)):
        if platform.config_entry is entry:
            await platform.async_destroy()
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HomePodConfigEntry) -> bool:
    if entry.version != 3:
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="unsupported_configuration")
    previous = hass.data.get(_PENDING_ENTRY_CLEANUP)
    if previous is not None and previous.entry is entry:
        if await _finish_entry_cleanup(hass, previous, ConfigEntryState.SETUP_IN_PROGRESS) is not None:
            raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="setup_failed")
    elif not await async_finish_removed_entry_cleanup(hass):
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="setup_failed")
    if conflicting_recovery(hass) or OWNER in hass.data:
        create_issue(hass, "engine_conflict", "engine_conflict", entry.entry_id, ir.IssueSeverity.ERROR)
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="engine_conflict")
    ir.async_delete_issue(hass, DOMAIN, "engine_conflict")
    hass.data[OWNER] = entry.entry_id
    runtime, controller = None, None
    platform_setup: asyncio.Task[None] | None = None

    async def forward_platforms() -> None:
        nonlocal platform_setup
        # HA platform setup can have its own shielded work. Finish forwarding
        # before attempting to unload a partially registered platform.
        platform_setup = asyncio.create_task(hass.config_entries.async_forward_entry_setups(entry, PLATFORMS))
        await asyncio.shield(platform_setup)

    async def rollback_setup(active: Runtime) -> None:
        # A refresh can already be queued behind preflight's engine lock.
        active.stopped = True
        platforms_unloaded = platform_setup is None
        unsubscribe: Callable[[], None] | None = None

        async def rollback(state: ConfigEntryState) -> None:
            nonlocal platform_setup, platforms_unloaded
            try:
                # If teardown fails, surviving entities must stop reporting
                # readings from the failed setup.
                active.discard_startup_readings()
            finally:
                try:
                    if platform_setup is not None:
                        await asyncio.gather(platform_setup, return_exceptions=True)
                        platform_setup = None
                    if not platforms_unloaded:
                        platforms_unloaded = await _unload_startup_platforms(hass, entry, entry_state=state)
                finally:
                    await active.stop()
            if not platforms_unloaded:
                raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="setup_failed")
            if unsubscribe is not None:
                unsubscribe()

        async def cleanup_unloaded_entry() -> None:
            async with entry.setup_lock:
                remaining = hass.data.get(_PENDING_ENTRY_CLEANUP)
                if entry.state is ConfigEntryState.NOT_LOADED and remaining is not None and remaining.entry is entry:
                    await _finish_entry_cleanup(hass, remaining, ConfigEntryState.NOT_LOADED)

        @callback
        def on_state_change() -> None:
            if entry.state is ConfigEntryState.NOT_LOADED:
                hass.async_create_task(cleanup_unloaded_entry(), "HomePod Sensor Link setup rollback")

        pending = _EntryCleanup(entry, rollback)
        hass.data[_PENDING_ENTRY_CLEANUP] = pending
        try:
            await _finish_entry_cleanup(hass, pending, ConfigEntryState.SETUP_IN_PROGRESS)
        finally:
            if hass.data.get(_PENDING_ENTRY_CLEANUP) is pending:
                unsubscribe = entry.async_on_state_change(on_state_change)

    async def close_controller() -> None:
        if controller:
            await cast(Callable[[], Awaitable[None]], controller.async_stop)()

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
        entry.runtime_data = runtime
        await runtime.preflight(forward_platforms)
        if platform_setup is None:
            await forward_platforms()
    except BaseException as error:
        if runtime is not None:
            await rollback_setup(runtime)
        else:
            try:
                await _drain_cleanup(close_controller())
            finally:
                hass.data.pop(OWNER, None)
        if not isinstance(error, Exception):
            raise
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="setup_failed") from None

    async def update_names() -> None:
        try:
            discovered = await discover_devices(hass)
            # Only applying metadata needs the lock. Select current records
            # afterwards so discovery cannot resurrect removed devices.
            async with runtime.engine.lock:
                if runtime.stopped:
                    return
                await refresh_names(hass, runtime.records, vault, entry_id=entry.entry_id, discovered=discovered)
        except Exception:
            # Optional metadata must not interrupt sensor monitoring.
            return

    async def started(_hass: HomeAssistant) -> None:
        if runtime.stopped:
            return
        runtime.watchdog.start()
        await runtime.startup_refresh()
        if runtime.stopped:
            return
        runtime.name_refresh_task = entry.async_create_background_task(
            hass, update_names(), "HomePod Sensor Link name refresh"
        )

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
    # HA has already removed the entry from memory. Let it finish persisting
    # the deletion and clearing registry associations after the purge drains.
    error = await _drain_cleanup(_remove_entry(hass, entry), propagate_cancellation=False)
    if error is not None:
        raise error


async def _remove_entry(hass: HomeAssistant, entry: HomePodConfigEntry) -> None:
    runtime: Runtime | None = getattr(entry, "runtime_data", None)
    rollback = hass.data.get(_PENDING_ENTRY_CLEANUP)
    interrupted_setup = rollback is not None and rollback.entry is entry
    if not interrupted_setup and runtime:
        await runtime.stop()
    # Abort outstanding flows before purge; Vault drains cancelled atomic I/O.
    for flow in list(hass.config_entries.flow.async_progress_by_handler(DOMAIN)):
        hass.config_entries.flow.async_abort(flow["flow_id"])
    vault = hass.data.get(DOMAIN + ".vault") or Vault(hass)
    if interrupted_setup:
        await _remove_interrupted_setup(hass, entry, runtime, vault, cast(_EntryCleanup, rollback))
        return
    await vault.purge()
    await _clear_removed_entry_state(hass, entry, runtime)
    if hass.data.get(OWNER) == entry.entry_id:
        hass.data.pop(OWNER)


async def _remove_interrupted_setup(
    hass: HomeAssistant, entry: HomePodConfigEntry, runtime: Runtime | None, vault: Vault, rollback: _EntryCleanup
) -> None:
    teardown_pending = vault_pending = state_pending = True

    async def cleanup(state: ConfigEntryState) -> None:
        nonlocal teardown_pending, vault_pending, state_pending
        error = None
        if teardown_pending:
            error = await _drain_cleanup(rollback.cleanup(state))
            teardown_pending = error is not None
        if vault_pending:
            await vault.purge()
            vault_pending = False
        if state_pending:
            await _clear_removed_entry_state(hass, entry, runtime)
            state_pending = False
        if error is not None:
            raise error

    pending = _EntryCleanup(entry, cleanup)
    hass.data[_PENDING_ENTRY_CLEANUP] = pending
    error = await _finish_entry_cleanup(hass, pending, ConfigEntryState.NOT_LOADED)
    if state_pending:
        assert error is not None
        raise error


async def _clear_removed_entry_state(hass: HomeAssistant, entry: HomePodConfigEntry, runtime: Runtime | None) -> None:
    await Store(hass, 1, DOMAIN + ".state", private=True, atomic_writes=True).async_remove()
    ir.async_delete_issue(hass, DOMAIN, "checks_unavailable")
    ir.async_delete_issue(hass, DOMAIN, "engine_conflict")
    ir.async_delete_issue(hass, DOMAIN, "monitoring_stalled")
    ir.async_delete_issue(hass, DOMAIN, "controller_transition")
    persistent_notification.async_dismiss(hass, DOMAIN + "_watchdog_" + entry.entry_id)
    for key in entry.data.get("device_keys", []):
        clear_device_issues(hass, key)
    if runtime:
        runtime.records.clear()
        runtime.targets.clear()
        runtime.readings.clear()
        runtime.engine.targets.clear()
        runtime.engine.states.clear()


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
