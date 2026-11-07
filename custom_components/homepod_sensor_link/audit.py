"""Read-only per-device administrator audits without blocking other peers."""

from __future__ import annotations
from .models import Data, Target, Clock, SaveState, ProvisioningBackend
from collections.abc import Awaitable, Callable
from .engine import RecoveryEngine, DeviceState


import asyncio

from .engine import SafetyFailure

AUDIT_INTERVAL = 7 * 86400
AUDIT_RETRY = 3600


class RecoveryAudit:
    def __init__(
        self,
        engine: RecoveryEngine,
        backend: ProvisioningBackend,
        save: SaveState,
        notify: Callable[[Target, DeviceState, bool], Awaitable[None]],
        clock: Clock,
    ) -> None:
        self.engine, self.backend, self.save = engine, backend, save
        self.notify, self.clock = notify, clock

    async def _device(self, target: Target, force: bool) -> Data | None:
        if not self.engine.is_active() or not target.get("recovery", True):
            return None
        state = self.engine.states[target["entry_id"]]
        now = self.clock()
        interval = AUDIT_INTERVAL if state.recovery_status == "verified" else AUDIT_RETRY
        if not force and state.recovery_last_checked is not None and now - state.recovery_last_checked < interval:
            return None
        state.recovery_last_checked = now
        try:
            result = await self.backend.inspect_recovery(target)
        except Exception as error:
            state.recovery_status = "rejected" if isinstance(error, SafetyFailure) else "unreachable"
            state.recovery_error = type(error).__name__
            result = {
                "entry_id": target["entry_id"],
                "recovery_admin_verified": False,
                "error_type": state.recovery_error,
            }
            await self.notify(target, state, False)
        else:
            state.recovery_status, state.recovery_error = "verified", None
            state.recovery_last_verified = self.clock()
            await self.notify(target, state, True)
        return result

    async def check(self, force: bool = False) -> list[Data]:
        async with self.engine.lock:
            if not self.engine.is_active():
                return []
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._device(target, force)) for target in self.engine.targets]
            await self.save(self.engine.snapshot())
            return [result for task in tasks if (result := task.result()) is not None]
