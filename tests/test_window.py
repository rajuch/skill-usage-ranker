from datetime import UTC, date, datetime, timedelta

import pytest

from skill_ranker.window import Window, is_day_complete, parse_window

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)


def test_relative_window_ends_today_inclusive():
    w = parse_window("7d", now=NOW)
    assert w == Window(date(2026, 11, 11), date(2026, 11, 17))
    assert len(w.days) == 7


def test_explicit_dates():
    w = parse_window(start="2026-11-01", end="2026-11-03", now=NOW)
    assert w.days == [date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]


@pytest.mark.parametrize("spec", ["0d", "abc", "7w", "-3d"])
def test_invalid_specs(spec):
    with pytest.raises(ValueError):
        parse_window(spec, now=NOW)


def test_start_after_end_rejected():
    with pytest.raises(ValueError):
        parse_window(start="2026-11-05", end="2026-11-01", now=NOW)


def test_day_completeness_respects_settle_period():
    settle = timedelta(hours=2)
    yesterday = date(2026, 11, 16)
    assert not is_day_complete(yesterday, datetime(2026, 11, 17, 1, 0, tzinfo=UTC), settle)
    assert is_day_complete(yesterday, datetime(2026, 11, 17, 2, 0, tzinfo=UTC), settle)
    assert not is_day_complete(date(2026, 11, 17), NOW, settle)
