"""Deterministic expansion of operator-note time windows.

The model reports the two clock times a note names; this module owns the conversion
into whole hours. Hour arithmetic was the single largest source of interpretation
error while the model did it itself, so the convention is pinned here rather than
in a prompt.
"""

from __future__ import annotations

import pytest

from app.guardrails import expand_window, normalize_interpretations
from app.schemas import Battery

BATTERY = Battery(
    capacity_kwh=220,
    initial_energy_kwh=110,
    minimum_energy_kwh=40,
    max_charge_kwh_per_hour=50,
    max_discharge_kwh_per_hour=50,
)


def _item(**overrides) -> dict:
    item = {
        "note_index": 0,
        "directive_type": "no_charge_window",
        "hours": [],
        "start_hour": None,
        "end_hour": None,
        "factor": None,
        "minimum_energy_kwh": None,
        "max_grid_kwh": None,
        "explanation": "test",
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # The end hour is excluded: this is the rule the whole challenge turns on.
        (13, 15, (13, 14)),  # "1 PM to 3 PM", the Problem Statement's own example
        (12, 14, (12, 13)),  # "from noon until 2 PM"
        (18, 22, (18, 19, 20, 21)),  # "from 6 PM until 10 PM"
        (9, 12, (9, 10, 11)),  # "between 09:00 and 12:00"
        (23, 24, (23,)),  # end_hour 24 is midnight at the close of the day
        (0, 24, tuple(range(24))),  # "all day"
    ],
)
def test_end_hour_is_excluded(start, end, expected) -> None:
    assert expand_window(start, end) == expected


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (22, 6, (0, 1, 2, 3, 4, 5, 22, 23)),  # "from 10 PM until 6 AM"
        (21, 1, (0, 21, 22, 23)),  # "9 PM to 1 AM"
    ],
)
def test_windows_wrap_past_midnight(start, end, expected) -> None:
    """A window ending before it starts runs overnight, and stays ascending."""
    hours = expand_window(start, end)
    assert hours == expected
    assert list(hours) == sorted(hours), "hours must be returned in ascending order"


def test_degenerate_window_is_a_single_hour() -> None:
    assert expand_window(14, 14) == (14,)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, 5),  # no start reported
        (18, None),  # no end reported
        (25, 30),  # outside the clock
        (-1, 4),
        (True, 5),  # a bool is not an hour
        ("evening", "night"),
        (18, 25),  # end beyond midnight-of-day
    ],
)
def test_unusable_bounds_yield_no_window(start, end) -> None:
    """Nothing is guessed: an unusable range produces no hours at all."""
    assert expand_window(start, end) == ()


def test_string_bounds_are_coerced() -> None:
    assert expand_window("18", "22") == (18, 19, 20, 21)


def test_directive_uses_the_named_range() -> None:
    entries, directives = normalize_interpretations(
        [_item(start_hour=18, end_hour=22)], 1, BATTERY
    )
    assert directives[0].hours == (18, 19, 20, 21)
    assert entries[0].structured_adjustment == {"hours": [18, 19, 20, 21]}
    assert entries[0].applies is True


def test_explicit_hours_are_used_when_no_range_is_given() -> None:
    """Notes naming individual hours, or a whole day, still come through `hours`."""
    _entries, directives = normalize_interpretations(
        [_item(hours=[9, 13, 17])], 1, BATTERY
    )
    assert directives[0].hours == (9, 13, 17)


def test_a_named_range_wins_over_an_hours_list() -> None:
    _entries, directives = normalize_interpretations(
        [_item(hours=[3], start_hour=18, end_hour=20)], 1, BATTERY
    )
    assert directives[0].hours == (18, 19)


def test_hours_list_is_the_fallback_when_the_range_is_unusable() -> None:
    _entries, directives = normalize_interpretations(
        [_item(hours=[3, 4], start_hour=99, end_hour=None)], 1, BATTERY
    )
    assert directives[0].hours == (3, 4)


def test_a_window_directive_with_no_hours_becomes_no_op() -> None:
    entries, directives = normalize_interpretations([_item()], 1, BATTERY)
    assert directives == []
    assert entries[0].directive_type == "no_op"
    assert entries[0].applies is False
    assert entries[0].structured_adjustment is None


def test_overnight_directive_survives_the_full_guardrail_pass() -> None:
    entries, directives = normalize_interpretations(
        [_item(directive_type="no_charge_window", start_hour=22, end_hour=6)], 1, BATTERY
    )
    assert directives[0].hours == (0, 1, 2, 3, 4, 5, 22, 23)
    assert entries[0].structured_adjustment == {"hours": [0, 1, 2, 3, 4, 5, 22, 23]}
