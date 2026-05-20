
"""
Huawei Solar 2.10.9 firmware update binary sensors.
"""

FIRMWARE_BINARY_SENSORS = [
    {
        "key": "firmware_update_available",
        "name": "Firmware Update Available",
    }
]


# Huawei Solar 2.10.10 communication health sensors
MODBUS_BINARY_SENSORS = [
    "modbus_communication_healthy",
    "modbus_communication_degraded",
]


from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity import EntityCategory

try:
    from .diagnostics_runtime import MODBUS_STATS
except Exception:
    MODBUS_STATS = None


class HuaweiModbusHealthBinarySensor(BinarySensorEntity):
    """Huawei Modbus communication health sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, key, name):
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"huawei_solar_{key}"

    @property
    def is_on(self):
        if MODBUS_STATS is None:
            return False

        availability = MODBUS_STATS.availability_percent

        if self._key == "modbus_communication_healthy":
            return availability >= 95

        if self._key == "modbus_communication_degraded":
            return availability < 95

        return False


# ============================================================
# Huawei Solar 2.10.12 binary sensor registration
# ============================================================

async def async_setup_binary_modbus(async_add_entities):
    entities = [
        HuaweiModbusHealthBinarySensor(
            "modbus_communication_healthy",
            "Modbus Communication Healthy",
        ),
        HuaweiModbusHealthBinarySensor(
            "modbus_communication_degraded",
            "Modbus Communication Degraded",
        ),
    ]

    async_add_entities(entities)


try:
    ORIGINAL_BINARY_ASYNC_SETUP_ENTRY = async_setup_entry
except Exception:
    ORIGINAL_BINARY_ASYNC_SETUP_ENTRY = None


async def async_setup_entry(hass, entry, async_add_entities):
    if ORIGINAL_BINARY_ASYNC_SETUP_ENTRY:
        try:
            await ORIGINAL_BINARY_ASYNC_SETUP_ENTRY(
                hass,
                entry,
                async_add_entities,
            )
        except TypeError:
            pass

    await async_setup_binary_modbus(async_add_entities)
