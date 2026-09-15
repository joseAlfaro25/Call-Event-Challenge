from datetime import datetime

import pytest

from config import CampaignConfig
from domain.scheduling import resolve_callback

CONFIG = CampaignConfig.load("tests/extra_events/18-callback-outside-window.json")
ANCHOR = datetime.fromisoformat("2026-09-16T17:00:00+02:00")


def test_resolve_callback_uses_structured_day_and_time():
    result = resolve_callback(
        "mañana a las 9",
        ANCHOR,
        CONFIG,
        callback_day="jueves",
        callback_time="11:30",
    )

    assert result.requested == datetime.fromisoformat("2026-09-17T11:30:00+02:00")
    assert result.scheduled == result.requested
    assert not result.adjusted


def test_resolve_callback_accepts_accented_weekday():
    result = resolve_callback(
        None,
        ANCHOR,
        CONFIG,
        callback_day="miércoles",
        callback_time="10:00",
    )

    assert result.requested == datetime.fromisoformat("2026-09-16T10:00:00+02:00")


def test_resolve_callback_adjusts_structured_time_outside_window():
    result = resolve_callback(
        "hoy a las 22",
        ANCHOR,
        CONFIG,
        callback_day="hoy",
        callback_time="22:00",
    )

    assert result.requested == ANCHOR.replace(hour=22)
    assert result.scheduled == datetime.fromisoformat("2026-09-17T10:00:00+02:00")
    assert result.adjusted


def test_resolve_callback_falls_back_to_raw_expression():
    result = resolve_callback("mañana a las 11:15", ANCHOR, CONFIG)

    assert result.requested == datetime.fromisoformat("2026-09-17T11:15:00+02:00")
    assert result.scheduled == result.requested


def test_resolve_callback_fallback_accepts_accented_weekday():
    result = resolve_callback("miércoles a las 11:15", ANCHOR, CONFIG)

    assert result.requested == datetime.fromisoformat("2026-09-16T11:15:00+02:00")


def test_resolve_callback_uses_raw_component_when_structured_time_is_missing():
    result = resolve_callback(
        "mañana a las 11:15",
        ANCHOR,
        CONFIG,
        callback_day="jueves",
    )

    assert result.requested == datetime.fromisoformat("2026-09-17T11:15:00+02:00")


@pytest.mark.parametrize("value", ["9:00", "24:00", "12:60"])
def test_resolve_callback_rejects_invalid_structured_time(value):
    with pytest.raises(ValueError):
        resolve_callback("hoy a las 10", ANCHOR, CONFIG, callback_time=value)
