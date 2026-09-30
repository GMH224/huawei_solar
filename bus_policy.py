"""Bus-side policy switches and transport configuration (v2.3.1.0).

Pure module: no Home Assistant imports, so it can be executed directly by the
test suite against the REAL installed ``huawei-solar`` / ``tmodbus`` /
``tenacity`` libraries -- the transport policy below reaches into library
objects, and only a test against the real objects proves it does what it
says.

See AUDIT_2.3.1.0.md for the field evidence behind every switch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import Any

from .const import (
    CONF_DAWN_PEER_WAKE,
    CONF_PROTECT_POWER_READS,
    CONF_SINGLE_RETRY_LAYER,
    CONF_SLOW_PATH_ISOLATION,
    CONF_SLOW_REGISTER_CADENCE,
    DEFAULT_DAWN_PEER_WAKE,
    DEFAULT_PROTECT_POWER_READS,
    DEFAULT_SINGLE_RETRY_LAYER,
    DEFAULT_SLOW_PATH_ISOLATION,
    DEFAULT_SLOW_REGISTER_CADENCE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BusPolicy:
    """The five v2.3.1.0 bus-side switches for one config entry."""

    slow_path_isolation: bool = DEFAULT_SLOW_PATH_ISOLATION
    slow_register_cadence: bool = DEFAULT_SLOW_REGISTER_CADENCE
    single_retry_layer: bool = DEFAULT_SINGLE_RETRY_LAYER
    protect_power_reads: bool = DEFAULT_PROTECT_POWER_READS
    dawn_peer_wake: bool = DEFAULT_DAWN_PEER_WAKE

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> "BusPolicy":
        """Build from a config entry's options; missing keys use the defaults.

        Only a real ``bool`` is accepted as an explicit value -- anything else
        (a stray string from a hand-edited storage file, None) falls back to
        the default rather than being coerced by truthiness.
        """
        opts = options or {}

        def pick(key: str, default: bool) -> bool:
            value = opts.get(key, default)
            return value if isinstance(value, bool) else default

        return cls(
            slow_path_isolation=pick(CONF_SLOW_PATH_ISOLATION, DEFAULT_SLOW_PATH_ISOLATION),
            slow_register_cadence=pick(CONF_SLOW_REGISTER_CADENCE, DEFAULT_SLOW_REGISTER_CADENCE),
            single_retry_layer=pick(CONF_SINGLE_RETRY_LAYER, DEFAULT_SINGLE_RETRY_LAYER),
            protect_power_reads=pick(CONF_PROTECT_POWER_READS, DEFAULT_PROTECT_POWER_READS),
            dawn_peer_wake=pick(CONF_DAWN_PEER_WAKE, DEFAULT_DAWN_PEER_WAKE),
        )


def apply_single_retry_layer(client: Any, response_timeout_s: float) -> bool:
    """HS-2310-002: make the vendor client send every request exactly once.

    ``huawei_solar.modbus_client.create_client()`` wraps the transport in a
    ``TimeoutAwareSmartTransport`` whose retry strategy re-sends a request
    after a response timeout (3 attempts, 1-10 s back-off) and after a busy
    or device-failure reply. That runs INSIDE one guarded request of this
    integration, on top of the integration's own busy retry, and re-sends
    writes whose answer was merely late.

    After this call the transport retries only when the connection itself
    was lost (tmodbus' own ``_retry_with_new_connection_if_needed``: the
    request did not reach the device, and the transport reconnects), at most
    once; timeouts, busy and device-failure replies surface immediately to
    the integration, which owns the retry policy. One attempt may take up to
    ``response_timeout_s`` (applied to the base transport before it
    connects, so it holds for the first connection and every reconnect).

    Fail-safe: if the objects do not look exactly as expected (a future
    library version), nothing is changed, a warning is logged and False is
    returned -- the library's own defaults then stay in force, which is the
    pre-2.3.1.0 behaviour.
    """
    transport = getattr(client, "transport", None)
    base = getattr(transport, "base_transport", None)
    reconnect_predicate = getattr(transport, "_retry_with_new_connection_if_needed", None)
    if (
        transport is None
        or base is None
        or not hasattr(transport, "response_retry_strategy")
        or not callable(reconnect_predicate)
        or not hasattr(base, "timeout")
    ):
        _LOGGER.warning(
            "Single retry layer not applied: the Modbus client does not have the "
            "expected structure (library version change?). Keeping the "
            "library's own retry behaviour."
        )
        return False
    try:
        from tenacity import AsyncRetrying, stop_after_attempt, wait_fixed

        strategy = AsyncRetrying(
            stop=stop_after_attempt(2),
            wait=wait_fixed(1),
            retry=reconnect_predicate,
            reraise=True,
        )
        timeout = float(response_timeout_s)
        if timeout <= 0:
            raise ValueError("response timeout must be positive")
    except Exception as err:  # noqa: BLE001 -- fail-safe, see docstring
        _LOGGER.warning("Single retry layer not applied (%s); keeping library defaults", err)
        return False
    transport.response_retry_strategy = strategy
    base.timeout = timeout
    _LOGGER.debug(
        "Single retry layer applied: reconnect-only retry, %.0f s per attempt", timeout
    )
    return True
