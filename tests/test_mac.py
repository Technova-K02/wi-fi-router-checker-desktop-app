import pytest

from router_checker.core.mac import InvalidMacError, MacAddress, try_parse_mac

EXPECTED = "B0-0A-D5-9A-7B-B4"


@pytest.mark.parametrize(
    "text",
    [
        "B0-0A-D5-9A-7B-B4",
        "b0:0a:d5:9a:7b:b4",
        "b00ad59a7bb4",
        "  B00AD59A7BB4 ",
        "b0-0a-d5-9a-7b-b4",
    ],
)
def test_accepted_formats_display_uppercase_with_dashes(text: str) -> None:
    assert str(MacAddress.parse(text)) == EXPECTED


@pytest.mark.parametrize(
    "text",
    [
        "",
        "B0-0A-D5-9A-7B",
        "B0-0A-D5-9A-7B-B4-00",
        "B0:0A-D5:9A-7B:B4",  # mixed separators
        "G0-0A-D5-9A-7B-B4",
        "b00ad59a7bb",
        "B0.0A.D5.9A.7B.B4",
        "B00A-D59A-7BB4",
    ],
)
def test_rejects_invalid(text: str) -> None:
    with pytest.raises(InvalidMacError):
        MacAddress.parse(text)
    assert try_parse_mac(text) is None


def test_equality_and_hashing_ignore_input_format() -> None:
    a = MacAddress.parse("b0:0a:d5:9a:7b:b4")
    b = MacAddress.parse("B00AD59A7BB4")
    assert a == b
    assert len({a, b}) == 1


def test_from_bytes_and_zero() -> None:
    assert MacAddress.from_bytes(bytes(6)).is_zero
    assert str(MacAddress.from_bytes(bytes.fromhex("b00ad59a7bb4"))) == EXPECTED
    with pytest.raises(InvalidMacError):
        MacAddress.from_bytes(b"\x00" * 5)


def test_same_device_only_last_byte_differs() -> None:
    lan = MacAddress.parse("58-07-F8-09-A7-82")
    assert lan.same_device(MacAddress.parse("58-07-F8-09-A7-8C"))
    assert not lan.same_device(MacAddress.parse("58-07-F8-09-A8-82"))
