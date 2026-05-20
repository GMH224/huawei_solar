
"""
Huawei Solar 2.10f
Production Runtime Architecture

Implemented:
- Async read batching
- Dynamic scheduler
- Event-driven dispatch
- Coordinator decomposition support
- Async cancellation hardening
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

_LOGGER = logging.getLogger(__name__)


class AsyncReadBatcher:
    """Batch contiguous register reads."""

    def __init__(self):
        self._queue = []

    def add(self, start, length):
        self._queue.append((start, length))

    def build_batches(self):
        if not self._queue:
            return []

        ordered = sorted(self._queue)
        batches = []

        current_start, current_len = ordered[0]
        current_end = current_start + current_len

        for start, length in ordered[1:]:
            end = start + length

            if start <= current_end + 1:
                current_end = max(current_end, end)
            else:
                batches.append((current_start, current_end - current_start))
                current_start = start
                current_end = end

        batches.append((current_start, current_end - current_start))
        return batches


class DynamicRegisterScheduler:
    """Dynamic polling scheduler."""

    CRITICAL = 5
    NORMAL = 15
    SLOW = 60
    STATIC = 3600

    def __init__(self):
        self._registry = defaultdict(list)

    def register(self, priority, register):
        self._registry[priority].append(register)

    def interval(self, priority):
        return {
            "critical": self.CRITICAL,
            "normal": self.NORMAL,
            "slow": self.SLOW,
            "static": self.STATIC,
        }.get(priority, self.NORMAL)


class EventBus:
    """Simple internal event dispatcher."""

    def __init__(self):
        self._listeners = defaultdict(list)

    def subscribe(self, event_name, callback):
        self._listeners[event_name].append(callback)

    async def emit(self, event_name, payload=None):
        for callback in self._listeners[event_name]:
            try:
                await callback(payload)
            except Exception as err:
                _LOGGER.warning("Event callback failure: %s", err)


class AsyncTaskManager:
    """Safe async task lifecycle handling."""

    def __init__(self):
        self._tasks = set()

    def create(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cancel_all(self):
        tasks = list(self._tasks)

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)


class BaseCoordinator:
    """Base decomposed coordinator."""

    def __init__(self, name):
        self.name = name
        self.last_update = None

    async def refresh(self):
        self.last_update = time.time()


class InverterCoordinator(BaseCoordinator):
    pass


class BatteryCoordinator(BaseCoordinator):
    pass


class MeterCoordinator(BaseCoordinator):
    pass


class FirmwareCoordinator(BaseCoordinator):
    pass
