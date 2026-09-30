"""MAC address parsing and formatting.

Accepted input: ``B0-0A-D5-9A-7B-B4``, ``b0:0a:d5:9a:7b:b4`` or ``b00ad59a7bb4``.
Display format: uppercase with dashes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEX = "[0-9A-Fa-f]{2}"
_SEPARATED = re.compile(rf"^{_HEX}([-:])(?:{_HEX}\1){{4}}{_HEX}$")
_PLAIN = re.compile(r"^[0-9A-Fa-f]{12}$")


class InvalidMacError(ValueError):
    """The text is not a MAC address in one of the accepted formats."""


@dataclass(frozen=True, slots=True, order=True)
class MacAddress:
    octets: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.octets, bytes) or len(self.octets) != 6:
            raise InvalidMacError("a MAC address has exactly 6 bytes")

    @classmethod
    def parse(cls, text: str) -> MacAddress:
        cleaned = text.strip()
        if _SEPARATED.match(cleaned):
            hex_digits = cleaned.replace("-", "").replace(":", "")
        elif _PLAIN.match(cleaned):
            hex_digits = cleaned
        else:
            raise InvalidMacError(
                f"invalid MAC address {text!r}: use B0-0A-D5-9A-7B-B4, "
                "b0:0a:d5:9a:7b:b4 or b00ad59a7bb4"
            )
        return cls(bytes.fromhex(hex_digits))

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview) -> MacAddress:
        return cls(bytes(data))

    @property
    def is_zero(self) -> bool:
        return not any(self.octets)

    def same_device(self, other: MacAddress) -> bool:
        """True if only the last byte differs.

        A router's BSSIDs (one per band or SSID) and its LAN MAC usually come
        from one block of addresses that differ only in the last byte.
        """
        return self.octets[:5] == other.octets[:5]

    def __str__(self) -> str:
        return "-".join(f"{b:02X}" for b in self.octets)


def try_parse_mac(text: str) -> MacAddress | None:
    try:
        return MacAddress.parse(text)
    except InvalidMacError:
        return None
