
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
