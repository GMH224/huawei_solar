from .diagnostics_runtime import MODBUS_STATS

"""
Huawei Solar 2.10.8 compatibility-safe runtime layer.
"""

from __future__ import annotations

import asyncio
import logging
import random

_LOGGER = logging.getLogger(__name__)

_MODBUS_LOCK = asyncio.Lock()


async def safe_modbus_call(factory, retries: int = 3):
    """
    Serialized retry-safe wrapper for Modbus operations.
    """

    last_error = None

    for attempt in range(retries):
        try:
            async with _MODBUS_LOCK:
                return await factory()

        except Exception as err:
            last_error = err

            _LOGGER.warning(
                "Huawei Solar retry %s/%s failed: %s",
                attempt + 1,
                retries,
                err,
            )

            await asyncio.sleep(
                (attempt + 1) + random.uniform(0, 0.25)
            )

    raise last_error

# Huawei Solar 2.10.10 diagnostics enabled


from time import monotonic

try:
    from .diagnostics_runtime import (
        record_success,
        record_failure,
    )
except Exception:
    record_success = None
    record_failure = None

# Huawei Solar 2.10.11 diagnostics instrumentation active
