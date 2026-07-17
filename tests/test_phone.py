import pytest

from app.services.phone import full_phone, normalize_phone


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("+7 999 123-45-67", "9991234567"),
        ("8 999 123-45-67", "9991234567"),
        ("79991234567", "9991234567"),
        ("9991234567", "9991234567"),
        (9991234567.0, "9991234567"),
        ("123", None),
        (None, None),
    ],
)
def test_normalize_phone(value, expected):
    assert normalize_phone(value) == expected


def test_full_phone():
    assert full_phone("9991234567") == "+79991234567"

