"""Malformed model output must never reach the optimizer.

Structured Outputs constrain the shape at decode time, but the guardrails are what
the Problem Statement actually requires, and they have to hold when the provider
changes, when a schema is relaxed, or when the emergency parser is the one answering.
The rule throughout: repair what is unambiguously repairable, degrade everything else
to no_op, and never invent a constraint.
"""

from __future__ import annotations

import math

import pytest

from app.guardrails import normalize_interpretations
from app.schemas import Battery

BATTERY = Battery(
    capacity_kwh=200,
    initial_energy_kwh=100,
    minimum_energy_kwh=40,
    max_charge_kwh_per_hour=50,
    max_discharge_kwh_per_hour=50,
)


def _item(**overrides) -> dict:
    item = {
        "note_index": 0,
        "directive_type": "no_charge_window",
        "hours": [14, 15],
        "start_hour": None,
        "end_hour": None,
        "factor": None,
        "minimum_energy_kwh": None,
        "max_grid_kwh": None,
        "explanation": "test",
    }
    item.update(overrides)
    return item


def _only(items, note_count=1):
    return normalize_interpretations(items, note_count, BATTERY)


# --------------------------------------------------------------------------- #
# The payload itself                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("payload", [None, {}, "[]", 42, [], [None], ["a string"], [[1, 2]]])
def test_a_payload_that_is_not_a_list_of_objects_yields_no_ops(payload) -> None:
    entries, directives = _only(payload, note_count=2)
    assert directives == []
    assert [e.directive_type for e in entries] == ["no_op", "no_op"]
    assert all(e.applies is False and e.structured_adjustment is None for e in entries)


def test_every_note_gets_an_entry_even_when_the_model_skips_one() -> None:
    entries, _directives = _only([_item(note_index=2)], note_count=3)
    assert [e.note_index for e in entries] == [0, 1, 2]
    assert [e.directive_type for e in entries] == ["no_op", "no_op", "no_charge_window"]


def test_duplicate_note_index_keeps_the_first_and_drops_the_rest() -> None:
    entries, directives = _only(
        [
            _item(note_index=0, hours=[1, 2]),
            _item(note_index=0, hours=[20, 21]),
        ],
        note_count=1,
    )
    assert len(entries) == 1
    assert len(directives) == 1
    assert directives[0].hours == (1, 2)


@pytest.mark.parametrize("index", [True, False, "0", 0.0, -1, 5, None])
def test_an_unusable_note_index_is_ignored(index) -> None:
    """A mapping we cannot trust is discarded rather than guessed at."""
    entries, directives = _only([_item(note_index=index)], note_count=1)
    assert directives == []
    assert entries[0].directive_type == "no_op"


# --------------------------------------------------------------------------- #
# Directive type                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "directive_type",
    ["shutdown", "NO_CHARGE_WINDOW", "", None, 7, "no_op", ["no_charge_window"]],
)
def test_only_the_five_real_directive_types_survive(directive_type) -> None:
    _entries, directives = _only([_item(directive_type=directive_type)])
    assert directives == []


# --------------------------------------------------------------------------- #
# Hours                                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([15, 14, 14, 15], (14, 15)),  # unsorted with duplicates
        (["18", "19"], (18, 19)),  # digit strings
        ([18.0, 19.9], (18, 19)),  # floats truncate
        ([-1, 24, 99, 13], (13,)),  # out-of-range dropped, the rest kept
        ((14, 15), (14, 15)),  # tuple
        ({14, 15}, (14, 15)),  # set
    ],
)
def test_hours_are_coerced_to_unique_ascending_integers(raw, expected) -> None:
    _entries, directives = _only([_item(hours=raw)])
    assert directives[0].hours == expected


@pytest.mark.parametrize("raw", ["18-20", {"start": 18}, None, [], [-1, 24], [True, False]])
def test_a_window_with_no_usable_hours_becomes_no_op(raw) -> None:
    entries, directives = _only([_item(hours=raw)])
    assert directives == []
    assert entries[0].directive_type == "no_op"
    assert entries[0].structured_adjustment is None


def test_reported_hours_are_always_ascending() -> None:
    entries, _directives = _only([_item(hours=[23, 0, 22, 1])])
    hours = entries[0].structured_adjustment["hours"]
    assert hours == sorted(hours)


# --------------------------------------------------------------------------- #
# Numeric fields                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.2, 0.2),
        (0, 0.0),
        (1, 1.0),
        (25, 0.25),  # answered in percent, repaired
        (100, 1.0),
        ("0.5", 0.5),
    ],
)
def test_solar_factor_is_repaired_where_the_intent_is_unambiguous(raw, expected) -> None:
    _entries, directives = _only([_item(directive_type="solar_reduction", factor=raw)])
    assert directives[0].factor == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw", [-0.5, 101, 1.5, None, "half", float("nan"), float("inf"), True]
)
def test_an_unrepairable_solar_factor_becomes_no_op(raw) -> None:
    entries, directives = _only([_item(directive_type="solar_reduction", factor=raw)])
    assert directives == []
    assert entries[0].directive_type == "no_op"


def test_a_reserve_above_capacity_is_clamped_not_rejected() -> None:
    _entries, directives = _only(
        [_item(directive_type="minimum_battery_reserve", minimum_energy_kwh=99999)]
    )
    assert directives[0].minimum_energy_kwh == BATTERY.capacity_kwh


@pytest.mark.parametrize("raw", [-1, None, "lots", float("nan"), float("inf")])
def test_an_unusable_reserve_becomes_no_op(raw) -> None:
    _entries, directives = _only(
        [_item(directive_type="minimum_battery_reserve", minimum_energy_kwh=raw)]
    )
    assert directives == []


def test_a_zero_grid_cap_is_honoured_rather_than_discarded() -> None:
    """Zero is a real limit, not a missing value; feasibility is decided downstream."""
    _entries, directives = _only([_item(directive_type="max_grid_window", max_grid_kwh=0)])
    assert directives[0].max_grid_kwh == 0.0


@pytest.mark.parametrize("raw", [-1, None, "none", float("nan"), float("inf")])
def test_an_unusable_grid_cap_becomes_no_op(raw) -> None:
    _entries, directives = _only([_item(directive_type="max_grid_window", max_grid_kwh=raw)])
    assert directives == []


def test_numeric_fields_are_always_finite() -> None:
    for raw in (float("inf"), float("-inf"), float("nan")):
        _entries, directives = _only(
            [_item(directive_type="minimum_battery_reserve", minimum_energy_kwh=raw)]
        )
        assert directives == []
    _entries, directives = _only(
        [_item(directive_type="minimum_battery_reserve", minimum_energy_kwh=120)]
    )
    assert math.isfinite(directives[0].minimum_energy_kwh)


# --------------------------------------------------------------------------- #
# The reported entry                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", [None, "", "   ", 42, [], {"text": "hi"}])
def test_a_missing_explanation_is_replaced_not_left_empty(raw) -> None:
    entries, _directives = _only([_item(explanation=raw)])
    assert isinstance(entries[0].explanation, str)
    assert entries[0].explanation.strip()


def test_applies_is_true_for_every_real_directive_and_false_only_for_no_op() -> None:
    entries, _directives = _only(
        [_item(note_index=0), _item(note_index=1, directive_type="nonsense")], note_count=2
    )
    assert (entries[0].applies, entries[0].directive_type) == (True, "no_charge_window")
    assert (entries[1].applies, entries[1].directive_type) == (False, "no_op")
    assert entries[1].structured_adjustment is None


@pytest.mark.parametrize(
    ("directive_type", "extra", "keys"),
    [
        ("solar_reduction", {"factor": 0.2}, {"hours", "factor"}),
        (
            "minimum_battery_reserve",
            {"minimum_energy_kwh": 90},
            {"hours", "minimum_energy_kwh"},
        ),
        ("max_grid_window", {"max_grid_kwh": 150}, {"hours", "max_grid_kwh"}),
        ("no_charge_window", {}, {"hours"}),
        ("no_discharge_window", {}, {"hours"}),
    ],
)
def test_structured_adjustment_carries_exactly_the_required_shape(
    directive_type, extra, keys
) -> None:
    entries, _directives = _only([_item(directive_type=directive_type, **extra)])
    assert set(entries[0].structured_adjustment) == keys
