"""Exceptions for the SolarEdge Modbus client."""


class SolarEdgeError(Exception):
    """Generic SolarEdge exception."""


class SolarEdgeConnectionError(SolarEdgeError):
    """SolarEdge Modbus communication error.

    Raised when reading from or writing to the device over Modbus fails,
    wrapping the backend-neutral error from ``modbus-connection``.
    """
