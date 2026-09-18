"""What happens when the interpreted directives cannot all be honoured.

A real scenario is guaranteed feasible under its ground truth, so an unschedulable
directive set means a note was misread. The service must not answer with a schedule
it already knows breaks a directive: the judge replays every plan, and a visibly
invalid one fails the case outright. Dropping a directive we invented is recoverable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.guardrails import normalize_interpretations
from app.schemas import OptimizeRequest
from app.validator import replay

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "samples" / "public_cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]

client = TestClient(main.app, raise_server_exceptions=False)


def _item(index: int, directive_type: str, **overrides) -> dict:
    item = {
        "note_index": index,
        "directive_type": directive_type,
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


# An hourly grid cap of zero against non-zero demand cannot be met by any schedule.
IMPOSSIBLE = _item(0, "max_grid_window", hours=list(range(24)), max_grid_kwh=0.0)
SATISFIABLE = _item(1, "no_charge_window", hours=[2, 3, 4])
NO_OP = _item(1, "no_op")


@pytest.fixture
def scenario():
    request = OptimizeRequest(**CASES[0]["input"])
    return request, request.hours_in_order()


def _directives(items, request):
    return normalize_interpretations(items, len(items), request.battery)[1]


def test_an_unschedulable_directive_is_dropped_rather_than_violated(scenario) -> None:
    request, hours = scenario
    directives = _directives([IMPOSSIBLE, NO_OP], request)

    plan, totals, dropped, valid = main._solve_best_effort(hours, request.battery, directives)

    assert valid is True
    assert [d.directive_type for d in dropped] == ["max_grid_window"]
    kept = [d for d in directives if d not in dropped]
    assert replay(hours, request.battery, kept, plan, totals) == []


def test_only_the_unschedulable_directive_is_dropped(scenario) -> None:
    """Directives are given up one at a time, never wholesale."""
    request, hours = scenario
    directives = _directives([IMPOSSIBLE, SATISFIABLE], request)

    _plan, _totals, dropped, valid = main._solve_best_effort(hours, request.battery, directives)

    assert valid is True
    assert [d.directive_type for d in dropped] == ["max_grid_window"]
    kept = [d.directive_type for d in directives if d not in dropped]
    assert kept == ["no_charge_window"]


def test_a_schedulable_set_keeps_every_directive(scenario) -> None:
    request, hours = scenario
    directives = _directives(
        [_item(0, "solar_reduction", hours=[12, 13], factor=0.25), NO_OP], request
    )

    _plan, totals, dropped, valid = main._solve_best_effort(hours, request.battery, directives)

    assert dropped == []
    assert valid is True
    assert totals[1] == pytest.approx(
        CASES[0]["expected_output"]["total_cost_bdt"], abs=0.01
    )


def test_no_directives_at_all_is_not_a_failure(scenario) -> None:
    request, hours = scenario
    plan, totals, dropped, valid = main._solve_best_effort(hours, request.battery, [])
    assert (dropped, valid) == ([], True)
    assert replay(hours, request.battery, [], plan, totals) == []


def test_the_response_stays_valid_and_says_what_was_dropped(monkeypatch) -> None:
    """End to end: a misread directive must not produce an invalid 200."""

    async def fake(notes, hours, battery):
        return [IMPOSSIBLE, NO_OP]

    monkeypatch.setattr(main.interpreter, "interpret", fake)

    response = client.post("/optimize-energy", json=CASES[0]["input"])
    assert response.status_code == 200
    body = response.json()

    request = OptimizeRequest(**CASES[0]["input"])
    hours = request.hours_in_order()
    # The plan is legal once the impossible directive is set aside.
    assert replay(hours, request.battery, [], main.OptimizeResponse(**body).hourly_plan) == []
    assert "max_grid_window" in body["plan_summary"]

    # The interpretation is still reported as it was read: a directive we could not
    # apply must not be downgraded, or interpretation credit is lost too.
    assert body["directive_interpretation"][0]["directive_type"] == "max_grid_window"
    assert body["directive_interpretation"][0]["applies"] is True
