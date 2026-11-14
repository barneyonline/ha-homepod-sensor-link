"""Typed coordinator runtime, per-device work and owned registry lifecycle."""

from __future__ import annotations

from typing import Any, cast
from .repairs import create_issue
from .models import Data, Target, Record, Snapshot, Readings, Backend, ProvisioningBackend
from collections.abc import Callable, Awaitable
from functools import partial
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from datetime import datetime
from zeroconf.asyncio import AsyncZeroconf


import asyncio
from datetime import timedelta
import logging
from copy import deepcopy
import os
from pathlib import Path
import stat

from aiohomekit.controller.ip import IpController
from homeassistant.components import persistent_notification
from homeassistant.core import CoreState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.util import dt as dt_util

from .backend import HomeKitBackend
from .audit import RecoveryAudit
from .credentials import DOMAIN, validate_target
from .engine import INTERVAL, RecoveryEngine, SafetyFailure, AuthenticationFailure, DeviceState
from .provisioning import Provisioner, boot_id
from .vault import Vault, VaultStates
from .watchdog import HeartbeatWatchdog

_LOGGER = logging.getLogger(__name__)
OWNER = DOMAIN + ".owner"
STARTUP_SAMPLE_MAX_AGE = 30


type HomePodConfigEntry = ConfigEntry[Runtime]


def conflicting_recovery(hass: HomeAssistant) -> bool:
    # Conflict detection only: no legacy import, compatibility engine or migration.
    return any(
        e.disabled_by is None or e.state.value == "loaded"
        for e in hass.config_entries.async_entries("homepod_recovery")
    )


def validate_state_location(config_dir: str) -> None:
    storage = Path(config_dir) / ".storage"
    if storage.is_symlink():
        raise SafetyFailure("unsafe_state_storage")
    location = storage / (DOMAIN + ".state")
    try:
        info = location.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise SafetyFailure("unsafe_state_storage")


def remove_owned_registry(hass: HomeAssistant, entry_id: str, key: str) -> None:
    devices, entities = dr.async_get(hass), er.async_get(hass)
    for device in list(dr.async_entries_for_config_entry(devices, entry_id)):
        if (DOMAIN, key) not in device.identifiers:
            continue
        associated = list(er.async_entries_for_device(entities, device.id))
        foreign = any(entity.platform != DOMAIN or entity.config_entry_id != entry_id for entity in associated)
        for entity in associated:
            if entity.platform == DOMAIN and entity.config_entry_id == entry_id:
                entities.async_remove(entity.entity_id)
        if not foreign:
            devices.async_remove_device(device.id)


def clear_device_issues(hass: HomeAssistant, key: str) -> None:
    for prefix in ("access_", "administrator_"):
        ir.async_delete_issue(hass, DOMAIN, prefix + key)


class Runtime:
    def __init__(
        self,
        hass: HomeAssistant,
        entry: HomePodConfigEntry,
        targets: list[Target],
        controller: IpController,
        zeroconf_instance: AsyncZeroconf,
        vault: Vault | None = None,
    ) -> None:
        self.hass, self.entry, self.targets = hass, entry, targets
        self.controller, self.vault = controller, vault
        self.store: VaultStates | Store[Snapshot] = (
            VaultStates(vault) if vault else Store(hass, 1, DOMAIN + ".state", private=True, atomic_writes=True)
        )
        self.notifications: dict[str, tuple[Any, ...]] = {}
        self.readings: dict[str, tuple[datetime, Readings]] = {}
        self.entities: dict[str, dict[str, str]] = {}
        self.measurement_listeners: dict[str, set[Callable[[], None]]] = {}
        self.pending_tasks: dict[str, asyncio.Task[tuple[Record, Readings | None]]] = {}
        self.name_refresh_task: asyncio.Task[None] | None = None
        self._startup_samples: dict[str, tuple[Target, datetime, float]] = {}
        self._use_startup_samples = False
        self.records: dict[str, Record] = {}
        self.stopped = self.closed = False
        self._runtime_listener: Callable[[], None] | None = None
        self.watchdog = HeartbeatWatchdog(
            hass,
            entry.entry_id,
            self.monitored_devices,
            lambda: not self.stopped and hass.state == CoreState.running and not conflicting_recovery(hass),
        )
        self.backend = HomeKitBackend(controller, zeroconf_instance)
        self.coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            config_entry=entry,
            name="HomePod Sensor Link",
            update_interval=timedelta(seconds=INTERVAL),
            update_method=self.update,
        )

    async def initialize(self) -> None:
        if not self.vault:
            await self.hass.async_add_executor_job(validate_state_location, self.hass.config.config_dir)
        saved = await self.store.async_load()
        self.saved = {} if saved is None else saved
        if not isinstance(self.saved, dict):
            raise SafetyFailure("invalid_persisted_state")
        for state in self.saved.values():
            DeviceState.restore(state)
        self.records = (
            {key: r for key, r in self.vault.records().items() if key in self.entry.data["device_keys"]}
            if self.vault
            else {t["entry_id"]: {"target": t, "phase": "active", "name": "HomePod"} for t in self.targets}
        )
        for record in self.records.values():
            DeviceState.restore(
                {
                    "attempts": record.get("onboarding_attempts", []),
                    "last_checked": record.get("last_onboarding_attempt"),
                }
            )
            if (
                type(record["target"].get("recovery", True)) is not bool
                or type(record.get("cancelled", False)) is not bool
            ):
                raise SafetyFailure("invalid_persisted_policy")
            target = record["target"]
            if record["phase"] in ("needs_credentials", "transition_required"):
                continue
            try:
                validate_target(target, record.get("source_pin"))
                if (
                    target.get("recovery", True)
                    and record["phase"] in ("active", "verified")
                    and "source" not in target
                ):
                    raise ValueError
            except Exception:
                # Invalid devices are isolated; no mutation can run for them.
                record.update(phase="blocked", error="identity_validation_failed")
        self.engine = RecoveryEngine(
            self.targets,
            Backend(self.backend.read_ha, self.backend.restore_missing),
            self.save_state,
            self.refresh_publication,
            self.notify,
            lambda: dt_util.utcnow().timestamp(),
            self.saved,
            verify_publication=self.verify_publication,
            automatic_recovery=True,
            is_active=lambda: not self.stopped and not conflicting_recovery(self.hass),
            check_completed=self.watchdog.completed,
        )
        self.backend.write_allowed = self.engine.is_active
        if self.vault:
            self.backend.before_write = self.persist_pairing_checkpoint
            self.backend.after_write = self.persist_pairing_checkpoint
        # Keep the real backend for audit/provisioning and wrap successful owned
        # samples to publish through actual HA entities before health succeeds.
        self.engine.backend = Backend(self.read_target, self.backend.restore_missing)
        operations = ProvisioningBackend(
            self.backend.read_ha,
            self.backend.restore_missing,
            self.backend.inspect_recovery,
            self.backend.pairing_present,
        )
        self.audit = RecoveryAudit(
            self.engine, operations, self.save_state, self.notify_access, lambda: dt_util.utcnow().timestamp()
        )
        self.provisioner = Provisioner(
            cast(Vault, self.vault), operations, lambda: dt_util.utcnow().timestamp(), boot_id(self.hass)
        )
        self.sync_targets()
        self.coordinator.async_set_updated_data(self.engine.snapshot())
        # Disabling every diagnostic/measurement entity must not silently
        # disable the configured five-minute recovery watchdog.
        if self._runtime_listener is None:
            self._runtime_listener = self.coordinator.async_add_listener(lambda: None)

    def monitored_devices(self) -> set[str]:
        return {
            key
            for key, record in self.records.items()
            if record["phase"] not in ("needs_credentials", "blocked", "transition_required")
            and not record.get("cancelled")
            and not self.engine.states[key].blocked
        }

    def sync_targets(self) -> None:
        if any(record["phase"] == "transition_required" for record in self.records.values()):
            create_issue(
                self.hass,
                "controller_transition",
                "controller_transition",
                self.entry.entry_id,
                ir.IssueSeverity.WARNING,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "controller_transition")
        self.targets = [r["target"] for r in self.records.values()]
        self.engine.targets = [
            r["target"]
            for r in self.records.values()
            if r["phase"] in ("active", "verified") and not r.get("cancelled")
        ]
        for key, record in self.records.items():
            state = self.engine.states.setdefault(key, DeviceState())
            state.attempts = sorted(set(state.attempts + record.get("onboarding_attempts", [])))
        self.watchdog.check()

    async def accept_inventory(self, data: Data) -> None:
        """Accept guided per-device results without unloading healthy peers."""
        async with self.engine.lock:
            records = cast(Vault, self.vault).records()
            selected = {key: records[key] for key in data["device_keys"]}
            for key, record in selected.items():
                if record["phase"] not in ("needs_credentials", "transition_required"):
                    try:
                        validate_target(record["target"], record.get("source_pin"))
                    except SafetyFailure:
                        record.update(phase="blocked", error="identity_validation_failed")
            self.records = selected
            self.hass.config_entries.async_update_entry(self.entry, data=data)
            self.sync_targets()
        self.coordinator.async_set_updated_data(self.engine.snapshot())

    def publish(self, target: Target, values: Readings) -> None:
        key = target["entry_id"]
        self.readings[key] = (dt_util.utcnow(), values)
        # Report this device synchronously before verify_publication checks HA's
        # last_reported timestamps. The final coordinator update publishes
        # health/inventory once, without an inventory-wide broadcast per read.
        for listener in tuple(self.measurement_listeners.get(key, ())):
            listener()

    def listen_measurements(self, key: str, listener: Callable[[], None]) -> Callable[[], None]:
        listeners = self.measurement_listeners.setdefault(key, set())
        listeners.add(listener)

        def unsubscribe() -> None:
            listeners.discard(listener)
            if not listeners and self.measurement_listeners.get(key) is listeners:
                self.measurement_listeners.pop(key, None)

        return unsubscribe

    async def persist_pairing_checkpoint(self, target: Target) -> None:
        key = target["entry_id"]
        record = cast(Vault, self.vault).record(key)
        record["target"] = deepcopy(target)
        await cast(Vault, self.vault).put(key, record)
        self.records[key] = record

    async def read_target(self, target: Target) -> Readings:
        key = target["entry_id"]
        previous = self._startup_samples.pop(key, None) if self._use_startup_samples else None
        if previous is not None:
            sample = self.readings.get(key)
            if (
                sample is not None
                and (target, sample[0]) == previous[:2]
                and not self.engine.states[key].publication_pending
                and 0 <= self.hass.loop.time() - previous[2] < STARTUP_SAMPLE_MAX_AGE
                and 0 <= (dt_util.utcnow() - sample[0]).total_seconds() < STARTUP_SAMPLE_MAX_AGE
            ):
                # Entities added after preflight report this original sample.
                # Do not change its timestamp or bypass publication verification.
                return sample[1]
        values = await self.backend.read_ha(target)
        self.publish(target, values)
        return values

    async def preflight(self) -> None:
        """Fresh read-only startup probes; no repair counters or pairing writes."""
        self._startup_samples.clear()

        async def probe(target: Target) -> str:
            state = self.engine.states[target["entry_id"]]
            if state.blocked:
                return "blocked"
            try:
                await self.read_target(target)
            except AuthenticationFailure:
                state.status = "authentication_failed"
            except SafetyFailure:
                state.status, state.blocked, state.reason = "blocked", True, "identity_validation_failed"
                await self.notify(target, state, False)
            except Exception:
                state.status, state.auth_failures, state.auth_since = "unreachable", 0, None
            else:
                self._startup_samples[target["entry_id"]] = (
                    deepcopy(target),
                    self.readings[target["entry_id"]][0],
                    self.hass.loop.time(),
                )
                return "verified"
            return state.status

        async with self.engine.lock:
            results = await asyncio.gather(*(probe(target) for target in self.engine.targets))
            await self.save_state(self.engine.snapshot())
        if results and all(result == "unreachable" for result in results):
            raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="unreachable")

    async def startup_refresh(self) -> None:
        """Use a recent preflight read once, keeping subsequent checks fresh."""
        self._use_startup_samples = True
        try:
            await self.coordinator.async_refresh()
        finally:
            self._use_startup_samples = False
            self._startup_samples.clear()

    async def finish_publication(self, key: str) -> None:
        record = self.records[key]
        try:
            await self.verify_publication(record["target"])
        except (SafetyFailure, OSError):
            return
        record["phase"] = "active"
        record["commissioned"] = True
        record["error"] = None
        if not record["target"].get("recovery", True):
            record["target"].pop("source", None)
        await cast(Vault, self.vault).put(key, record)

    async def update(self) -> Snapshot:
        try:
            result = await self._update()
        except Exception:
            create_issue(
                self.hass, "checks_unavailable", "checks_unavailable", self.entry.entry_id, ir.IssueSeverity.ERROR
            )
            raise UpdateFailed("HomePod sensor checks could not complete") from None
        ir.async_delete_issue(self.hass, DOMAIN, "checks_unavailable")
        return result

    async def _update(self) -> Snapshot:
        if conflicting_recovery(self.hass):
            self.watchdog.check()
            for state in self.engine.states.values():
                state.status, state.reason = "paused", "competing_recovery"
                state.auth_failures, state.auth_since = 0, None
            return self.engine.snapshot()
        if not self.stopped and self.hass.state == CoreState.running:
            await self.engine.check_all(self._check_devices if self.vault else None)
            self.watchdog.check()
            await self.audit.check()
            if self.vault:
                for key, record in list(self.records.items()):
                    if record["phase"] == "verified" and self.engine.states[key].status == "healthy":
                        await self.finish_publication(key)
        return self.engine.snapshot()

    async def _check_devices(self) -> None:
        """Check active devices alongside provisioning under the engine lock."""
        errors: list[Exception] = []

        async def check(work: Callable[[], Awaitable[None]]) -> None:
            try:
                await work()
            except Exception as error:
                errors.append(error)

        active = [target for target in self.engine.targets if self.records[target["entry_id"]]["phase"] == "active"]
        interrupted = set()

        async def inspect(key: str) -> None:
            if not await self.inspect_pending(key):
                interrupted.add(key)

        async with asyncio.TaskGroup() as group:
            for target in active:
                group.create_task(check(partial(self.engine.check_target, target)))
            for key, record in self.records.items():
                if record["phase"] not in ("active", "transition_required") and record["target"].get("owned"):
                    group.create_task(check(partial(inspect, key)))
        self.sync_targets()
        if errors:
            await self.save_state(self.engine.snapshot())
            raise ExceptionGroup("HomePod checks failed", errors)
        # A device that was pending at the start of the poll can only enter
        # the engine after provisioning finishes and synchronises its target.
        checked = interrupted | {target["entry_id"] for target in active}
        await self.engine.check_targets([target for target in self.engine.targets if target["entry_id"] not in checked])

    async def inspect_pending(self, key: str) -> bool:
        task = asyncio.create_task(self.provisioner.inspect(key))
        self.pending_tasks[key] = task
        try:
            record, values = await task
        except asyncio.CancelledError:
            if cast(asyncio.Task[None], asyncio.current_task()).cancelling():
                raise
            return False
        finally:
            self.pending_tasks.pop(key, None)
        self.records[key] = record
        if values:
            self.publish(record["target"], values)
            await self.finish_publication(key)
        self.engine.states[key].last_completed = dt_util.utcnow().timestamp()
        self.watchdog.completed(key)
        return True

    async def save_state(self, current: Snapshot) -> None:
        self.saved.update(current)
        await self.store.async_save(deepcopy(self.saved))

    def public_snapshot(self) -> list[Data]:
        return [
            {
                "device": index,
                **state.public(),
                "onboarding": self.records[key]["phase"],
                "onboarding_error": self.records[key].get("error"),
                "automatic_recovery": self.records[key]["target"].get("recovery", True),
            }
            for index, (key, state) in enumerate(self.engine.states.items(), 1)
            if key in self.records
        ]

    async def verify_publication(self, target: Target) -> None:
        ids = self.entities.get(target["entry_id"], {})
        if set(ids) != {"temperature", "humidity"}:
            raise OSError("sensor_entities_not_ready")
        states = [self.hass.states.get(entity_id) for entity_id in ids.values()]
        sample = self.readings.get(target["entry_id"])
        if not sample or not all(
            s and s.state not in ("unknown", "unavailable") and s.last_reported >= sample[0] for s in states
        ):
            raise OSError("sensor_reports_not_fresh")

    async def refresh_publication(self, target: Target) -> None:
        await self.read_target(target)
        await self.verify_publication(target)

    async def resume_device(self, key: str) -> None:
        if conflicting_recovery(self.hass):
            raise SafetyFailure("competing_recovery")
        async with self.engine.lock:
            if self.records[key]["phase"] == "transition_required":
                raise SafetyFailure("explicit_transition_required")
            if self.records[key]["phase"] == "active" or self.records[key].get("commissioned"):
                record = self.records[key]
                record["cancelled"] = False
                await cast(Vault, self.vault).put(key, record)
                self.sync_targets()
                self.coordinator.async_set_updated_data(self.engine.snapshot())
                return
            record, values = await self.provisioner.inspect(key, resume=True)
            self.records[key] = record
            if values:
                self.publish(record["target"], values)
                await self.finish_publication(key)
            self.sync_targets()
        self.coordinator.async_set_updated_data(self.engine.snapshot())

    async def cancel_device(self, key: str) -> None:
        task = self.pending_tasks.get(key)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.engine.lock:
            await self.provisioner.cancel(key)
            self.records[key] = cast(Vault, self.vault).record(key)
            self.sync_targets()
        self.coordinator.async_set_updated_data(self.engine.snapshot())

    async def set_recovery(self, key: str, enabled: bool) -> bool:
        async with self.engine.lock:
            record = self.records[key]
            if record["phase"] == "transition_required":
                raise SafetyFailure("explicit_transition_required")
            if enabled and "source" not in record["target"]:
                return False
            if enabled:
                await self.backend.inspect_recovery(record["target"])
            record["target"]["recovery"] = enabled
            if not enabled and record["phase"] == "active":
                record["target"].pop("source", None)
            await cast(Vault, self.vault).put(key, record)
            self.sync_targets()
        return True

    async def remove_device(self, key: str) -> None:
        task = self.pending_tasks.get(key)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.engine.lock:
            if key not in self.records:
                return
            # Keep the configured device retryable if protected-file removal
            # fails. Only forget live state after its credentials are deleted.
            await cast(Vault, self.vault).remove(key)
            self.records.pop(key)
            self.engine.states.pop(key, None)
            self.readings.pop(key, None)
            self.entities.pop(key, None)
            self.measurement_listeners.pop(key, None)
            self.notifications.pop(key, None)
            self.notifications.pop("access_" + key, None)
            for suffix in (key, key + "_recovered", "access_" + key):
                persistent_notification.async_dismiss(self.hass, "homepod_sensor_link_" + suffix)
            self.sync_targets()
            self.saved.pop(key, None)
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={
                    "device_keys": list(self.records),
                    "pins": {k: v for k, v in self.entry.data.get("pins", {}).items() if k != key},
                    "admin_pins": {k: v for k, v in self.entry.data.get("admin_pins", {}).items() if k != key},
                },
            )
            remove_owned_registry(self.hass, self.entry.entry_id, key)
            self.clear_issues(key)
        self.coordinator.async_set_updated_data(self.engine.snapshot())

    async def notify(self, target: Target, state: DeviceState, recovered: bool) -> None:
        key = target["entry_id"]
        signature = (state.status, state.reason, state.repairs)
        if self.notifications.get(key) == signature:
            return
        self.notifications[key] = signature
        name = "HomePod"
        notification_id = f"homepod_sensor_link_{key}"
        if recovered:
            ir.async_delete_issue(self.hass, DOMAIN, "access_" + key)
            persistent_notification.async_dismiss(self.hass, notification_id)
            _LOGGER.info("%s: connection verified; total repairs %s", name, state.repairs)
        else:
            create_issue(self.hass, "access_" + key, "sensor_access", self.entry.entry_id, ir.IssueSeverity.ERROR)
            persistent_notification.async_create(
                self.hass,
                f"{name}: {state.status.replace('_', ' ')}. "
                f"Reason: {(state.reason or 'authentication_failed').replace('_', ' ')}. "
                "Automatic repair is limited to one attempt per hour and three attempts per day. "
                "Pending owned sensor publication retries at most three times per hour. "
                "Identity or key mismatches require investigation.",
                title="HomePod recovery needs attention",
                notification_id=notification_id,
            )
            _LOGGER.warning("%s: recovery status %s (%s)", name, state.status, state.reason)

    async def notify_access(self, target: Target, state: DeviceState, verified: bool) -> None:
        key = "access_" + target["entry_id"]
        signature = (state.recovery_status, state.recovery_error)
        if self.notifications.get(key) == signature:
            return
        self.notifications[key] = signature
        name = "HomePod"
        notification_id = "homepod_sensor_link_" + key
        if verified:
            ir.async_delete_issue(self.hass, DOMAIN, "administrator_" + target["entry_id"])
            persistent_notification.async_dismiss(self.hass, notification_id)
        else:
            create_issue(
                self.hass,
                "administrator_" + target["entry_id"],
                "administrator_access",
                self.entry.entry_id,
                ir.IssueSeverity.WARNING,
            )
            if state.recovery_status == "rejected":
                self.entry.async_start_reauth(self.hass)
            persistent_notification.async_create(
                self.hass,
                f"{name}: recovery administrator access could not be verified "
                f"({state.recovery_status}). Existing sensor access may still work. "
                "A read-only check will retry in one hour. No pairing was changed by this check.",
                title="HomePod recovery credentials need attention",
                notification_id=notification_id,
            )

    def clear_issues(self, key: str) -> None:
        clear_device_issues(self.hass, key)

    async def stop(self) -> None:
        if self.closed:
            return
        self.stopped = True
        self._startup_samples.clear()
        self.watchdog.stop()
        if self.name_refresh_task is not None:
            self.name_refresh_task.cancel()
            await asyncio.gather(self.name_refresh_task, return_exceptions=True)
        for task in self.pending_tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.pending_tasks.values()), return_exceptions=True)
        if self._runtime_listener is not None:
            self._runtime_listener()
            self._runtime_listener = None
        await self.coordinator.async_shutdown()
        self.measurement_listeners.clear()
        if hasattr(self, "engine"):
            async with self.engine.lock:
                await cast(Callable[[], Awaitable[None]], self.controller.async_stop)()
        else:
            await cast(Callable[[], Awaitable[None]], self.controller.async_stop)()
        self.closed = True
