import enum


class PortProtocol(enum.StrEnum):
    """Transport protocol for a locally bound port."""

    TCP = "tcp"
    UDP = "udp"
