"""Typed internal seams around validated dynamic HAP/HA/storage dictionaries.

Private wire/storage maps remain dynamic because their schemas differ for native,
owned and incomplete devices. Validation at each boundary checks these values;
callbacks, lifecycle objects, readings and collections are typed independently.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

type Data = dict[str, Any]
type Target = Data
type Record = Data
type Snapshot = dict[str, Data]
type Readings = dict[str, float]
type SaveState = Callable[[Snapshot], Awaitable[None]]
type Clock = Callable[[], float]
type PairingRow = dict[str, Any]


@dataclass(frozen=True)
class Backend:
    """Explicit callable adapter; no executable placeholder protocol bodies."""

    read_ha: Callable[..., Awaitable[Readings]]
    restore_missing: Callable[[Target], Awaitable[bool]]


@dataclass(frozen=True)
class ProvisioningBackend(Backend):
    inspect_recovery: Callable[[Target], Awaitable[Data]]
    pairing_present: Callable[[Target], Awaitable[bool]]
