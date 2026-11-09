"""Resolve discovery inventories with bounded tasks and request concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable

RESOLVE_WORKERS = 32


async def resolve_all[T, R](values: Iterable[T], resolve: Callable[[T], Awaitable[R]]) -> list[R]:
    """Retain all results in input order, draining workers on failure or cancel."""
    # Snapshot the browser's mutable inventory before workers yield. Keep all
    # services, but avoid a task and request for every untrusted advertisement.
    # Above 32 cold services, per-service timeouts can span multiple batches;
    # this deliberately trades extreme-LAN latency for bounded concurrency.
    items = tuple(values)
    pending = iter(enumerate(items))
    results: dict[int, R] = {}

    async def worker() -> None:
        for index, value in pending:
            results[index] = await resolve(value)

    workers = [asyncio.create_task(worker()) for _ in range(min(RESOLVE_WORKERS, len(items)))]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        # A resolver can itself be cancelled. Propagate that cancellation just
        # like the original gather, while also cancelling and draining peers.
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return [results[index] for index in range(len(items))]
