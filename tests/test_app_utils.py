from app_utils import (
    format_date_pl,
    format_pln,
    format_pull_time,
    parse_bool_value,
    parse_datetime_value,
    parse_int_value,
)


def test_parse_int_value_respects_bounds():
    assert parse_int_value("12", 5, min_value=1, max_value=20) == 12
    assert parse_int_value("0", 5, min_value=1, max_value=20) == 5
    assert parse_int_value("abc", 5, min_value=1, max_value=20) == 5


def test_parse_bool_value_accepts_common_truthy_inputs():
    assert parse_bool_value("1") is True
    assert parse_bool_value("yes") is True
    assert parse_bool_value("off") is False
    assert parse_bool_value(None, default=True) is True


def test_datetime_and_format_helpers_return_expected_polish_output():
    parsed = parse_datetime_value("2026-03-11T12:30:00Z")

    assert parsed is not None
    assert format_pull_time("2026-03-11T12:30:00Z")
    assert format_date_pl("2026-03-11") == "11.03.2026"
    assert format_pln(1234.5) == "1 234,50 zł"
