
"""
Huawei Solar 2.10.8 compatibility-safe runtime helpers.
"""

from __future__ import annotations

import asyncio
import logging
import random

_LOGGER = logging.getLogger(__name__)

_MODBUS_LOCK = asyncio.Lock()


async def safe_modbus_call(factory, retries: int = 3):
    """
    Serialized retry-safe Modbus wrapper.
    """

    last_error = None

    for attempt in range(retries):
        try:
            async with _MODBUS_LOCK:
                return await factory()

        except Exception as err:
            last_error = err

            _LOGGER.warning(
                "Retrying Modbus operation (%s/%s): %s",
                attempt + 1,
                retries,
                err,
            )

            await asyncio.sleep(
                (attempt + 1) + random.uniform(0, 0.25)
            )

    raise last_error
