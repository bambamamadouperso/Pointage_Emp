from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import BigInteger, DateTime, Double, Integer, Numeric, SmallInteger, String, Text, Time
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.postgresql import JSONB

from app import watermark
from app.crypto import decrypt, encrypt
from app.sync import _value_converter, map_type


@pytest.mark.parametrize(
    "value",
    [None, 42, Decimal("12.50"), 1.5, "abc", date(2024, 1, 2), datetime(2024, 1, 2, 3, 4, 5, 6), time(8, 30)],
)
def test_watermark_roundtrip(value):
    assert watermark.decode(watermark.encode(value)) == value


def test_encrypt_roundtrip():
    token = encrypt("mot de passe")
    assert token != "mot de passe"
    assert decrypt(token) == "mot de passe"
    assert encrypt("") == "" and decrypt("") == ""


@pytest.mark.parametrize(
    "src, expected",
    [
        (mysql.INTEGER(unsigned=True), BigInteger),
        (mysql.INTEGER(), Integer),
        (mysql.BIGINT(unsigned=True), Numeric),
        (mysql.TINYINT(1), SmallInteger),
        (mysql.YEAR(), SmallInteger),
        (mysql.BIT(1), BigInteger),
        (mysql.SET("a", "b"), Text),
        (mysql.ENUM("court", "tres-long"), String),
        (mysql.LONGTEXT(collation="utf8mb4_general_ci"), Text),
        (mysql.DOUBLE(), Double),
        (mysql.DATETIME(fsp=6), DateTime),
        (mysql.TIME(), Time),
        (mysql.JSON(), JSONB),
    ],
)
def test_map_type(src, expected):
    assert isinstance(map_type(src), expected)


def test_map_type_drops_collation():
    mapped = map_type(mysql.VARCHAR(50, collation="utf8mb4_general_ci"))
    assert isinstance(mapped, String) and mapped.length == 50 and mapped.collation is None
    assert map_type(mysql.ENUM("court", "tres-long")).length == len("tres-long")


def test_value_converter():
    conv = _value_converter(mysql.DATETIME(), DateTime())
    assert conv("0000-00-00 00:00:00") is None
    assert conv(None) is None
    text_conv = _value_converter(mysql.VARCHAR(10), String(10))
    assert text_conv("a\x00b") == "ab"
    assert text_conv({"b", "a"}) == "a,b"
    time_conv = _value_converter(mysql.TIME(), Time())
    assert time_conv(timedelta(hours=8, minutes=5)) == time(8, 5)
    with pytest.raises(ValueError):
        time_conv(timedelta(hours=30))
