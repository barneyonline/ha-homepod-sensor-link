"""Bounded recovery policy. No Home Assistant imports or credential logging."""

from __future__ import annotations

from typing import Any, cast
from .models import Target, Snapshot, PairingRow, Clock, SaveState, Backend
from collections.abc import Awaitable, Callable


import asyncio
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePath
import traceback

INTERVAL = 300
GRACE = 600
COOLDOWN = 3600
WINDOW = 86400
MAX_ATTEMPTS = 3
NATIVE_COOLDOWN = 300
NATIVE_WINDOW = 3600
NATIVE_MAX_ATTEMPTS = 3


class AuthenticationFailure(Exception):
    """A fresh session explicitly rejected the controller identity."""


class SafetyFailure(Exception):
    """An identity, permission, or preservation check failed."""


class RecoveryPaused(OSError):
    """Another recovery owner appeared; no pairing write is permitted."""


@dataclass
class DeviceState:
    status: str = "starting"
    last_checked: float | None = None
    last_completed: float | None = None
    last_verified: float | None = None
    auth_since: float | None = None
    auth_failures: int = 0
    attempts: list[float] = field(default_factory=list)
    last_repair: float | None = None
    repairs: int = 0
    blocked: bool = False
    reason: str | None = None
    last_error_type: str | None = None
    last_error_location: str | None = None
    native_pending: bool = False
    pending_pairing_added: bool = False
    native_attempts: list[float] = field(default_factory=list)
    recovery_last_checked: float | None = None
    recovery_last_verified: float | None = None
    recovery_status: str = "not_checked"
    recovery_error: str | None = None

    def public(self) -> dict[str, Any]:
        return dict(vars(self), attempts=list(self.attempts), native_attempts=list(self.native_attempts))

    @classmethod
    def restore(cls, value: dict[str, Any]) -> DeviceState:
        if not isinstance(value, dict):
            raise SafetyFailure("invalid_persisted_state")
        names = cls.__dataclass_fields__
        state = cls(**{k: v for k, v in value.items() if k in names})
        # Never let malformed storage erase cooldowns through NaN comparisons,
        # reinterpret a blocked flag, or enable recovery after a corrupt restart.
        import math

        timestamps = (
            "last_checked",
            "last_completed",
            "last_verified",
            "auth_since",
            "last_repair",
            "recovery_last_checked",
            "recovery_last_verified",
        )

        def finite(number: object) -> bool:
            return type(number) in (int, float) and math.isfinite(cast(float, number))

        if (
            any(getattr(state, key) is not None and not finite(getattr(state, key)) for key in timestamps)
            or any(
                type(getattr(state, key)) is not int or getattr(state, key) < 0 for key in ("auth_failures", "repairs")
            )
            or any(
                type(getattr(state, key)) is not bool for key in ("blocked", "native_pending", "pending_pairing_added")
            )
            or any(
                not isinstance(getattr(state, key), list) or not all(finite(t) for t in getattr(state, key))
                for key in ("attempts", "native_attempts")
            )
        ):
            raise SafetyFailure("invalid_persisted_state")
        # A failed repair may already have reached the accessory; retain pending
        # publication conservatively without ever re-adding an accepted pairing.
        if state.status == "repair_failed":
            state.native_pending = True
        return state


def canonical(rows: list[PairingRow]) -> Counter[tuple[str, str, int]]:
    """Include identifier, public key and permission, preserving multiplicity."""
    try:
        if not isinstance(rows, list) or not rows:
            raise ValueError
        ids = set()
        for row in rows:
            identifier, key, permission = row["pairingId"], row["publicKey"], row["permissions"]
            if (
                not isinstance(identifier, str)
                or not identifier
                or identifier in ids
                or len(bytes.fromhex(key)) != 32
                or type(permission) is not int
                or permission not in (0, 1)
            ):
                raise ValueError
            ids.add(identifier)
        return Counter((r["pairingId"], r["publicKey"].lower(), r["permissions"]) for r in rows)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise SafetyFailure("invalid_pairing_list") from None


class RecoveryEngine:
    """Check fresh HA sessions and restore only an absent, pinned controller."""

    def __init__(
        self,
        targets: list[Target],
        backend: Backend,
        save: SaveState,
        reload_entry: Callable[[Target], Awaitable[None]],
        notify: Callable[[Target, DeviceState, bool], Awaitable[None]],
        clock: Clock,
        states: Snapshot | None = None,
        verify_native: Callable[[Target], Awaitable[None]] | None = None,
        automatic_recovery: bool = True,
        is_active: Callable[[], bool] | None = None,
        check_completed: Callable[[str], None] | None = None,
    ) -> None:
        self.targets = targets
        self.backend = backend
        self.save = save
        self.reload_entry = reload_entry
        self.notify = notify
        self.clock = clock
        self.verify_native = verify_native
        self.automatic_recovery = automatic_recovery
        self.is_active = is_active or (lambda: True)
        self.check_completed = check_completed or (lambda _key: None)
        if states is not None and not isinstance(states, dict):
            raise SafetyFailure("invalid_persisted_state")
        self.started = clock()
        self.states = {t["entry_id"]: DeviceState.restore((states or {}).get(t["entry_id"], {})) for t in targets}
        self.lock = asyncio.Lock()

    def snapshot(self) -> Snapshot:
        return {key: value.public() for key, value in self.states.items()}

    async def check_all(self) -> None:
        if self.lock.locked():
            return
        async with self.lock:
            if not self.is_active():
                return

            async def check(target: Target) -> None:
                if self.is_active():
                    await self._check(target)
                    key = target["entry_id"]
                    self.states[key].last_completed = self.clock()
                    self.check_completed(key)

            async with asyncio.TaskGroup() as group:
                for target in self.targets:
                    group.create_task(check(target))
            await self.save(self.snapshot())

    async def _check(self, target: Target) -> None:
        state = self.states[target["entry_id"]]
        now = self.clock()
        state.last_checked = now
        # Retain future timestamps on a backwards clock jump: fail closed.
        state.attempts = [t for t in state.attempts if now - t < WINDOW]
        state.native_attempts = [t for t in state.native_attempts if now - t < NATIVE_WINDOW]
        if state.blocked:
            return
        try:
            await self.backend.read_ha(target)
        except AuthenticationFailure:
            state.last_error_type, state.last_error_location = "AuthenticationFailure", None
            state.auth_failures += 1
            if state.auth_since is None:
                state.auth_since = now
            state.status = "authentication_failed"
        except SafetyFailure:
            state.blocked, state.status, state.reason = True, "blocked", "identity_validation_failed"
            await self.notify(target, state, False)
            return
        except Exception as error:
            # Retain only code location and exception class, never exception
            # messages, locals, key material, or remote response contents.
            frames = traceback.extract_tb(error.__traceback__)
            frame = frames[-1] if frames else None
            state.last_error_type = type(error).__name__
            state.last_error_location = (
                f"{PurePath(frame.filename).name}:{frame.lineno}:{frame.name}" if frame else None
            )
            state.status, state.reason = "unreachable", "discovery_or_read_failed"
            state.auth_since, state.auth_failures = None, 0
            if state.last_verified is None or now - state.last_verified >= GRACE:
                await self.notify(target, state, False)
            return
        else:
            state.auth_since, state.auth_failures = None, 0
            if (
                not state.native_pending or (not self.automatic_recovery or not target.get("recovery", True))
            ) and self.verify_native is not None:
                try:
                    await self.verify_native(target)
                except SafetyFailure:
                    state.blocked, state.status, state.reason = True, "blocked", "native_inventory_changed"
                    await self.notify(target, state, False)
                    return
                except Exception:
                    if not self.automatic_recovery or not target.get("recovery", True):
                        state.status, state.reason = "native_unavailable", "native_readings_not_fresh"
                        return
                    state.native_pending = True
                    await self.save(self.snapshot())
            if state.native_pending and (not self.automatic_recovery or not target.get("recovery", True)):
                state.status, state.reason = "native_reload_pending", "automatic_recovery_disabled"
            elif state.native_pending:
                await self._finish_native(target, state)
            else:
                await self._healthy(target, state)
            return

        if not self.is_active():
            return
        if state.pending_pairing_added:
            state.reason = "accepted_pairing_authentication_failed"
            return
        if not self.automatic_recovery or not target.get("recovery", True):
            state.reason = "automatic_recovery_disabled"
            return
        if state.auth_failures < 3 or now - state.auth_since < GRACE or now - self.started < GRACE:
            return
        if len(state.attempts) >= MAX_ATTEMPTS or (state.attempts and now - state.attempts[-1] < COOLDOWN):
            state.status, state.reason = "cooldown", "repair_rate_limit"
            await self.notify(target, state, False)
            return

        # Persist the attempt BEFORE any pairing write, surviving restart or failure.
        state.attempts.append(now)
        state.status, state.reason = "repairing", None
        state.native_pending = True
        await self.save(self.snapshot())
        try:
            added = await self.backend.restore_missing(target)
            state.pending_pairing_added = state.pending_pairing_added or added
            await self.save(self.snapshot())
            # Independently verify with HA's dedicated private key before reloading HA.
            await self.backend.read_ha(target)
        except SafetyFailure:
            state.blocked, state.status, state.reason = True, "blocked", "repair_safety_check_failed"
            await self.notify(target, state, False)
            return
        except Exception:
            state.status, state.reason = "repair_failed", "recovery_or_reload_failed"
            await self.notify(target, state, False)
            return
        await self._finish_native(target, state)

    async def _finish_native(self, target: Target, state: DeviceState) -> None:
        """Retry a pending native reload without re-adding an accepted pairing."""
        now = self.clock()
        if len(state.native_attempts) >= NATIVE_MAX_ATTEMPTS or (
            state.native_attempts and now - state.native_attempts[-1] < NATIVE_COOLDOWN
        ):
            state.status, state.reason = "native_reload_pending", "native_reload_rate_limit"
            await self.notify(target, state, False)
            return
        state.native_attempts.append(now)
        state.status, state.reason = "native_reload_pending", None
        await self.save(self.snapshot())
        try:
            await self.reload_entry(target)
            if self.verify_native is not None:
                await self.verify_native(target)
        except SafetyFailure:
            state.blocked, state.status, state.reason = True, "blocked", "native_inventory_changed"
            await self.notify(target, state, False)
            return
        except Exception:
            state.status, state.reason = "native_reload_pending", "native_reload_or_verification_failed"
            await self.notify(target, state, False)
            return
        state.native_pending = False
        added = state.pending_pairing_added
        state.pending_pairing_added = False
        state.auth_since, state.auth_failures = None, 0
        if added:
            state.repairs += 1
        await self._healthy(target, state, repaired=added)
        await self.save(self.snapshot())

    async def _healthy(self, target: Target, state: DeviceState, repaired: bool = False) -> None:
        recovered = state.status not in ("healthy", "starting")
        state.status, state.reason = "healthy", None
        state.last_error_type, state.last_error_location = None, None
        state.last_verified = self.clock()
        if repaired:
            state.last_repair = state.last_verified
        if recovered:
            await self.notify(target, state, True)
