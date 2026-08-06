"""Diagnostics support for Huawei Solar."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .bus_diagnostics import pseudonym
from .const import DATA_DEVICE_DATAS
from .types import (
    HuaweiSolarConfigEntry,
    HuaweiSolarDeviceData,
    HuaweiSolarInverterData,
)

# v1.3.20 FIX (Defect X4, independent ICS audit): only CONF_PASSWORD was
# redacted here, despite bus_diagnostics.py's own explicit design constraint
# for this project ("No identifying data. Serial numbers and endpoints are
# replaced by a stable salted pseudonym") -- a discipline established there,
# after a documented past incident, but never carried over to this file,
# which is Home Assistant's OWN built-in "download diagnostics" feature and
# routinely gets attached to public GitHub issues. CONF_HOST (the device's
# IP/hostname) and CONF_USERNAME (if parameter-configuration login is set
# up) are both identifying/sensitive and were previously exposed raw.
TO_REDACT = {CONF_PASSWORD, CONF_HOST, CONF_USERNAME}

#: Register names carrying a serial number (see huawei_solar.register_names):
#: the primary inverter, plus per-storage-unit and per-battery-pack serials.
#: Matched by substring so this stays correct even if the vendor library
#: adds more (e.g. a third storage unit) without this list being updated.
_SERIAL_REGISTER_SUBSTRING = "serial_number"


def _redact_serial_number(value: str | None) -> str | None:
    """Replace a raw serial number with the same stable pseudonym scheme
    bus_diagnostics.py already uses, so a shared diagnostics file still
    lets a maintainer compare two captures without exposing the real
    number."""
    if not value:
        return value
    return f"**REDACTED-{pseudonym(str(value))}**"


def _redact_coordinator_data(
    data: dict[Any, Any] | None,
) -> dict[str, Any] | None:
    """Redact any register whose name indicates it carries a serial number.

    v1.3.20 FIX (Defect X4): raw coordinator .data dicts were dumped
    completely unredacted. SUN2000/LUNA2000 installations can expose
    several serial-number-bearing registers this way (the inverter's own,
    plus per-storage-unit and per-battery-pack serials) even where the
    top-level per-device summary below correctly omits it -- the register
    data was the actual leak, not just the explicit field.
    """
    if not data:
        return data
    redacted: dict[str, Any] = {}
    for name, result in data.items():
        if _SERIAL_REGISTER_SUBSTRING in str(name).lower():
            try:
                raw_value = result.value
            except Exception:  # noqa: BLE001 — best-effort extraction only
                raw_value = result
            redacted[str(name)] = _redact_serial_number(
                raw_value if raw_value is None else str(raw_value)
            )
        else:
            redacted[str(name)] = result
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HuaweiSolarConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]

    diagnostics_data = {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entities": {
            entity_entry.entity_id: entity_entry.extended_dict
            for entity_entry in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        },
    }
    for dd in device_datas:
        if isinstance(dd, HuaweiSolarInverterData):
            diagnostics_data[f"device_{dd.device.client.unit_id}"] = {
                "_type": "SUN2000",
                "model_name": dd.device.model_name,
                "firmware_version": dd.device.firmware_version,
                "software_version": dd.device.software_version,
                "pv_string_count": dd.device.pv_string_count,
                "has_optimizers": dd.device.has_optimizers,
                "battery_type": dd.device.battery_type,
                "battery_1_type": dd.device.battery_1_type,
                "battery_2_type": dd.device.battery_2_type,
                "power_meter_type": dd.device.power_meter_type,
                "supports_capacity_control": dd.device.supports_capacity_control,
            }

            if dd.power_meter_update_coordinator:
                diagnostics_data[
                    f"device_{dd.device.client.unit_id}_power_meter_data"
                ] = _redact_coordinator_data(dd.power_meter_update_coordinator.data)

            if dd.energy_storage_update_coordinator:
                diagnostics_data[f"device_{dd.device.client.unit_id}_battery_data"] = (
                    _redact_coordinator_data(dd.energy_storage_update_coordinator.data)
                )

            if dd.optimizer_update_coordinator:
                diagnostics_data[
                    f"device_{dd.device.client.unit_id}_optimizer_data"
                ] = dd.optimizer_update_coordinator.data  # v1.3.20: optimizer
                # data is keyed by numeric optimizer ID -> OptimizerRealTimeData,
                # not RegisterName -> Result like the other coordinators, so
                # _redact_coordinator_data's register-name matching doesn't
                # apply to this shape. Checked directly: OptimizerRealTimeData
                # carries no serial-number-like field, so nothing to redact here.
        else:
            diagnostics_data[f"device_{dd.device.client.unit_id}"] = {
                "_type": type(dd.device).__name__,
                "model_name": dd.device.model_name,
                # v1.3.20 FIX (Defect X4): this used to be the raw
                # dd.device.serial_number -- exactly the kind of
                # identifying data bus_diagnostics.py already goes out of
                # its way to avoid, just not applied here.
                "serial_number": _redact_serial_number(dd.device.serial_number),
            }

        diagnostics_data[f"device_{dd.device.client.unit_id}_data"] = (
            _redact_coordinator_data(dd.update_coordinator.data)
        )

        if dd.configuration_update_coordinator:
            diagnostics_data[f"device_{dd.device.client.unit_id}_config_data"] = (
                _redact_coordinator_data(dd.configuration_update_coordinator.data)
            )

    return diagnostics_data
