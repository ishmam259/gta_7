"""Validate the optimizer and guardrails against the 10 public sample cases.

For each case we take the organizer's ground-truth directives, build our own
schedule, replay it exactly the way the judge does, and check that our recalculated
cost is at least as good as the published reference schedule.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.guardrails import normalize_interpretations
from app.optimizer import build_plan, summarize
from app.schemas import Battery, HourEntry, HourPlan, OptimizeRequest
from app.validator import TOLERANCE, replay

SAMPLES = json.loads(
    (Path(__file__).resolve().parents[1] / "samples" / "public_cases.json").read_text(
        encoding="utf-8"
    )
)
CASES = SAMPLES["cases"]
CASE_IDS = [case["id"] for case in CASES]


def _ground_truth_raw(case: dict) -> list[dict]:
    """Flatten the reference structured_adjustment back into raw-candidate shape."""
    items = []
    for entry in case["expected_output"]["directive_interpretation"]:
        adjustment = entry.get("structured_adjustment") or {}
        items.append(
            {
                "note_index": entry["note_index"],
                "directive_type": entry["directive_type"],
                "hours": adjustment.get("hours", []),
                "factor": adjustment.get("factor"),
                "minimum_energy_kwh": adjustment.get("minimum_energy_kwh"),
                "max_grid_kwh": adjustment.get("max_grid_kwh"),
                "explanation": entry.get("explanation", ""),
            }
        )
    return items


def _parts(case: dict):
    request = OptimizeRequest.model_validate(case["input"])
    hours = request.hours_in_order()
    entries, directives = normalize_interpretations(
        _ground_truth_raw(case), len(request.operator_notes), request.battery
    )
    return request, hours, entries, directives


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_request_schema_accepts_case(case: dict) -> None:
    OptimizeRequest.model_validate(case["input"])


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_guardrails_round_trip_ground_truth(case: dict) -> None:
    """Our guardrails must preserve the organizer's own interpretation unchanged."""
    _, _, entries, _ = _parts(case)
    expected = case["expected_output"]["directive_interpretation"]

    assert [e.note_index for e in entries] == list(range(len(expected)))
    for produced, reference in zip(entries, expected):
        assert produced.directive_type == reference["directive_type"]
        assert produced.applies == reference["applies"]
        assert produced.structured_adjustment == reference["structured_adjustment"]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_reference_schedule_is_valid_under_our_replay(case: dict) -> None:
    """Sanity-check the replay engine itself against the organizer's own schedule."""
    request, hours, _, directives = _parts(case)
    reference_plan = [HourPlan.model_validate(p) for p in case["expected_output"]["hourly_plan"]]
    assert replay(hours, request.battery, directives, reference_plan) == []


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_our_plan_is_valid_and_at_least_as_cheap(case: dict) -> None:
    request, hours, _, directives = _parts(case)

    plan, _scenario, strict = build_plan(hours, request.battery, directives)
    assert strict, "the strict LP should be feasible for every public case"

    totals = summarize(plan, hours)
    assert replay(hours, request.battery, directives, plan, totals) == []

    reference_cost = case["expected_output"]["total_cost_bdt"]
    our_cost = totals[1]
    assert our_cost <= reference_cost + TOLERANCE, (
        f"{case['id']}: our cost {our_cost} is worse than the reference {reference_cost}"
    )


def test_totals_are_recomputed_from_the_plan() -> None:
    case = CASES[0]
    request, hours, _, directives = _parts(case)
    plan, _scenario, _strict = build_plan(hours, request.battery, directives)
    total_grid, total_cost, peak_grid = summarize(plan, hours)

    assert total_grid == pytest.approx(sum(p.grid_kwh for p in plan), abs=TOLERANCE)
    assert peak_grid == pytest.approx(max(p.grid_kwh for p in plan), abs=TOLERANCE)
    assert total_cost == pytest.approx(
        sum(p.grid_kwh * hours[p.hour].tariff_bdt_per_kwh for p in plan), abs=TOLERANCE
    )


def test_guardrails_reject_unsupported_and_malformed_model_output() -> None:
    """Untrusted model output must degrade to no_op, never invent a constraint."""
    battery = Battery(
        capacity_kwh=500,
        initial_energy_kwh=200,
        minimum_energy_kwh=50,
        max_charge_kwh_per_hour=100,
        max_discharge_kwh_per_hour=100,
    )
    raw = [
        {"note_index": 0, "directive_type": "shed_load", "hours": [1, 2]},
        {"note_index": 1, "directive_type": "solar_reduction", "hours": [30, 31], "factor": 0.5},
        # Outside the repairable 0-1 fraction and 1-100 percent bands -> not guessable.
        {"note_index": 2, "directive_type": "solar_reduction", "hours": [3], "factor": -0.5},
    ]
    entries, directives = normalize_interpretations(raw, 3, battery)

    assert directives == []
    assert [e.directive_type for e in entries] == ["no_op"] * 3
    assert all(e.applies is False and e.structured_adjustment is None for e in entries)


def test_guardrails_normalise_percent_factor_and_hour_order() -> None:
    battery = Battery(
        capacity_kwh=400,
        initial_energy_kwh=100,
        minimum_energy_kwh=40,
        max_charge_kwh_per_hour=80,
        max_discharge_kwh_per_hour=80,
    )
    raw = [
        # percent instead of fraction, unsorted duplicate hours
        {"note_index": 0, "directive_type": "solar_reduction", "hours": [14, 13, 13], "factor": 25},
        # reserve above capacity must clamp, not be invented away
        {"note_index": 1, "directive_type": "minimum_battery_reserve", "hours": [20], "minimum_energy_kwh": 9999},
    ]
    entries, directives = normalize_interpretations(raw, 2, battery)

    assert entries[0].structured_adjustment == {"hours": [13, 14], "factor": 0.25}
    assert entries[1].structured_adjustment == {"hours": [20], "minimum_energy_kwh": 400.0}
    assert len(directives) == 2


def test_fill_missing_entries_with_no_op() -> None:
    battery = Battery(
        capacity_kwh=100,
        initial_energy_kwh=50,
        minimum_energy_kwh=10,
        max_charge_kwh_per_hour=20,
        max_discharge_kwh_per_hour=20,
    )
    entries, directives = normalize_interpretations([], 3, battery)
    assert [e.note_index for e in entries] == [0, 1, 2]
    assert directives == []


def test_half_open_window_expansion() -> None:
    """The exact failure seen live: end-hour boundaries must not drift."""
    from app.guardrails import _expand_window

    assert _expand_window(13, 15) == (13, 14)          # 1 PM to 3 PM
    assert _expand_window(12, 14) == (12, 13)          # noon until 2 PM
    assert _expand_window(18, 21) == (18, 19, 20)      # 6 PM until 9 PM
    assert _expand_window(18, 22) == (18, 19, 20, 21)  # 6 PM until 10 PM
    assert _expand_window(9, 12) == (9, 10, 11)        # 09:00 to 12:00
    assert _expand_window(2, 5) == (2, 3, 4)           # 2 AM until 5 AM
    assert _expand_window(14, 15) == (14,)             # during hour 14
    assert _expand_window(22, 2) == (22, 23, 0, 1)     # wraps past midnight
    assert _expand_window(20, 24) == (20, 21, 22, 23)  # through end of day
    assert _expand_window(None, None) == ()
    assert _expand_window("x", 5) == ()
    assert _expand_window(25, 30) == ()


def test_window_takes_priority_over_enumerated_hours() -> None:
    """If the model both enumerates and gives a window, trust the window."""
    battery = Battery(
        capacity_kwh=500,
        initial_energy_kwh=200,
        minimum_energy_kwh=50,
        max_charge_kwh_per_hour=100,
        max_discharge_kwh_per_hour=100,
    )
    raw = [
        {
            "note_index": 0,
            "directive_type": "minimum_battery_reserve",
            "start_hour": 18,
            "end_hour": 22,
            "hours": [18, 19, 20],  # the enumeration the model used to get wrong
            "minimum_energy_kwh": 90,
        }
    ]
    entries, _ = normalize_interpretations(raw, 1, battery)
    assert entries[0].structured_adjustment == {
        "hours": [18, 19, 20, 21],
        "minimum_energy_kwh": 90.0,
    }
