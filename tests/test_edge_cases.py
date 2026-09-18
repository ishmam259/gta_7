"""Inputs the public pack never shows, and the request surface around them.

The organizer promises hidden scenarios are feasible and well formed. These tests are
about what happens when that promise does not hold: degenerate batteries, prices that
behave oddly, request shapes just outside the schema. None of it should produce a 500,
an invalid schedule, or a plan that quietly breaks the energy rules.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.optimizer import build_plan, summarize
from app.schemas import Battery, HourEntry, OptimizeRequest
from app.validator import replay

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "samples" / "public_cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]

client = TestClient(main.app, raise_server_exceptions=False)


def _scenario(**battery_overrides):
    payload = copy.deepcopy(CASES[0]["input"])
    payload["battery"].update(battery_overrides)
    request = OptimizeRequest(**payload)
    return request.hours_in_order(), request.battery


def _flat_hours(demand=100.0, solar=0.0, tariff=10.0):
    return [
        HourEntry(hour=h, demand_kwh=demand, solar_kwh=solar, tariff_bdt_per_kwh=tariff)
        for h in range(24)
    ]


def _solve_and_check(hours, battery, directives=()):
    plan, _scenario_, _strict = build_plan(hours, battery, list(directives))
    totals = summarize(plan, hours)
    return plan, totals, replay(hours, battery, list(directives), plan, totals)


# --------------------------------------------------------------------------- #
# Degenerate batteries                                                         #
# --------------------------------------------------------------------------- #


def test_a_battery_with_no_capacity_still_schedules() -> None:
    """Nothing can be stored, so every hour is met from solar and the grid."""
    hours, battery = _scenario(
        capacity_kwh=0, initial_energy_kwh=0, minimum_energy_kwh=0
    )
    plan, _totals, violations = _solve_and_check(hours, battery)
    assert violations == []
    assert all(entry.battery_action == "idle" for entry in plan)


def test_a_battery_that_cannot_move_energy_still_schedules() -> None:
    hours, battery = _scenario(max_charge_kwh_per_hour=0, max_discharge_kwh_per_hour=0)
    plan, _totals, violations = _solve_and_check(hours, battery)
    assert violations == []
    assert all(entry.battery_kwh == 0 for entry in plan)


def test_starting_below_the_minimum_reserve_degrades_honestly() -> None:
    """A battery starting under its own floor makes the scenario self-contradictory.

    End-of-day neutrality requires hour 23 to finish at the starting energy, while the
    floor requires it to be at or above the minimum. With initial < minimum no schedule
    satisfies both, so no answer is correct. The schema permits this combination, so we
    still respond -- with a best-effort plan, the full 24 hours, and `valid` reported as
    False rather than a 500 or a silent claim of success. Rejecting the request instead
    would forfeit the case outright, which is worse for a scenario nobody can win.
    """
    hours, battery = _scenario(initial_energy_kwh=10, minimum_energy_kwh=40)
    plan, _totals, dropped, valid = main._solve_best_effort(hours, battery, [])
    assert valid is False
    assert dropped == []
    assert len(plan) == 24
    assert plan[-1].battery_energy_after_kwh == pytest.approx(10, abs=0.01)


def test_a_relaxed_plan_says_so_in_the_summary(monkeypatch) -> None:
    async def fake(notes, hours, battery):
        return [{"note_index": i, "directive_type": "no_op"} for i in range(len(notes))]

    monkeypatch.setattr(main.interpreter, "interpret", fake)
    payload = copy.deepcopy(CASES[0]["input"])
    payload["battery"].update(initial_energy_kwh=10, minimum_energy_kwh=40)
    response = client.post("/optimize-energy", json=payload)
    assert response.status_code == 200
    assert "relaxed" in response.json()["plan_summary"].lower()


def test_a_full_battery_at_its_own_ceiling_is_fine() -> None:
    hours, battery = _scenario(initial_energy_kwh=220, minimum_energy_kwh=220)
    _plan, _totals, violations = _solve_and_check(hours, battery)
    assert violations == []


# --------------------------------------------------------------------------- #
# Prices and quantities that behave oddly                                      #
# --------------------------------------------------------------------------- #


def test_a_flat_tariff_gives_a_valid_schedule() -> None:
    """With no price spread there is nothing to arbitrage, but the plan must still hold."""
    _hours, battery = _scenario()
    hours = _flat_hours(tariff=10.0)
    _plan, totals, violations = _solve_and_check(hours, battery)
    assert violations == []
    assert totals[1] == pytest.approx(24 * 100 * 10, abs=0.01)


def test_a_zero_tariff_day_gives_a_valid_schedule() -> None:
    _hours, battery = _scenario()
    _plan, totals, violations = _solve_and_check(_flat_hours(tariff=0.0), battery)
    assert violations == []
    assert totals[1] == pytest.approx(0.0, abs=0.01)


def test_a_negative_tariff_does_not_break_the_solve() -> None:
    """`tariff_bdt_per_kwh` has no lower bound in the schema, so a negative price is
    accepted. The battery bounds keep the problem finite; the schedule must stay legal."""
    _hours, battery = _scenario()
    hours = _flat_hours(tariff=-5.0)
    _plan, _totals, violations = _solve_and_check(hours, battery)
    assert violations == []


def test_surplus_solar_is_curtailed_not_exported() -> None:
    """Solar far above demand cannot be sold, banked beyond capacity, or thrown at the grid."""
    _hours, battery = _scenario()
    hours = _flat_hours(demand=10.0, solar=500.0, tariff=10.0)
    plan, _totals, violations = _solve_and_check(hours, battery)
    assert violations == []
    assert all(entry.grid_kwh >= 0 for entry in plan)
    assert all(entry.solar_used_kwh <= 500.0 + 0.01 for entry in plan)


def test_zero_demand_all_day_is_valid() -> None:
    _hours, battery = _scenario()
    _plan, totals, violations = _solve_and_check(_flat_hours(demand=0.0), battery)
    assert violations == []
    assert totals[0] == pytest.approx(0.0, abs=0.01)


def test_very_large_quantities_stay_consistent() -> None:
    battery = Battery(
        capacity_kwh=1e7,
        initial_energy_kwh=5e6,
        minimum_energy_kwh=0,
        max_charge_kwh_per_hour=1e6,
        max_discharge_kwh_per_hour=1e6,
    )
    hours = _flat_hours(demand=1e6, solar=0.0, tariff=3.0)
    _plan, _totals, violations = _solve_and_check(hours, battery)
    assert violations == []


# --------------------------------------------------------------------------- #
# The request surface                                                          #
# --------------------------------------------------------------------------- #


def _bad(payload) -> int:
    return client.post("/optimize-energy", json=payload).status_code


def test_more_than_three_notes_is_rejected() -> None:
    payload = copy.deepcopy(CASES[0]["input"])
    payload["operator_notes"] = ["a", "b", "c", "d"]
    assert _bad(payload) == 400


@pytest.mark.parametrize("notes", [[], [""], ["   "], [123], [None], ["ok", None]])
def test_unusable_notes_are_rejected(notes) -> None:
    payload = copy.deepcopy(CASES[0]["input"])
    payload["operator_notes"] = notes
    assert _bad(payload) == 400


def test_too_many_hours_is_rejected() -> None:
    payload = copy.deepcopy(CASES[0]["input"])
    payload["hours"] = payload["hours"] + [copy.deepcopy(payload["hours"][0])]
    assert _bad(payload) == 400


def test_duplicate_hours_are_rejected() -> None:
    payload = copy.deepcopy(CASES[0]["input"])
    payload["hours"][5] = copy.deepcopy(payload["hours"][4])
    assert _bad(payload) == 400


def test_an_hour_outside_the_day_is_rejected() -> None:
    payload = copy.deepcopy(CASES[0]["input"])
    payload["hours"][5]["hour"] = 24
    assert _bad(payload) == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"minimum_energy_kwh": 9999},  # floor above capacity
        {"initial_energy_kwh": 9999},  # starts above capacity
        {"capacity_kwh": -1},
        {"max_charge_kwh_per_hour": -5},
    ],
)
def test_impossible_battery_parameters_are_rejected(overrides) -> None:
    payload = copy.deepcopy(CASES[0]["input"])
    payload["battery"].update(overrides)
    assert _bad(payload) == 400


def test_negative_demand_is_rejected() -> None:
    payload = copy.deepcopy(CASES[0]["input"])
    payload["hours"][3]["demand_kwh"] = -10
    assert _bad(payload) == 400


def test_a_rejection_names_the_field_without_echoing_the_body() -> None:
    payload = copy.deepcopy(CASES[0]["input"])
    del payload["battery"]
    response = client.post("/optimize-energy", json=payload)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_request"
    assert any("battery" in item["loc"] for item in body["detail"])
    # The raw request must not come back out in the error.
    assert "operator_notes" not in json.dumps(body["detail"])


def test_unknown_routes_and_methods_do_not_500() -> None:
    assert client.get("/optimize-energy").status_code == 405
    assert client.get("/not-a-route").status_code == 404


def test_extra_unknown_fields_are_ignored(monkeypatch) -> None:
    async def fake(notes, hours, battery):
        return [{"note_index": i, "directive_type": "no_op"} for i in range(len(notes))]

    monkeypatch.setattr(main.interpreter, "interpret", fake)
    payload = copy.deepcopy(CASES[0]["input"])
    payload["unexpected"] = {"anything": True}
    payload["battery"]["chemistry"] = "LFP"
    assert client.post("/optimize-energy", json=payload).status_code == 200


# --------------------------------------------------------------------------- #
# Controlled failure                                                           #
# --------------------------------------------------------------------------- #


def test_an_unexpected_interpreter_error_is_a_controlled_500(monkeypatch) -> None:
    """Anything other than InterpreterUnavailable must not leak, and must not hang."""

    async def boom(notes, hours, battery):
        raise RuntimeError("provider exploded with secret=sk-abc123")

    monkeypatch.setattr(main.interpreter, "interpret", boom)
    response = client.post("/optimize-energy", json=CASES[0]["input"])
    assert response.status_code == 500
    body = response.json()
    assert body == {
        "error": "internal_error",
        "detail": "The request could not be completed.",
    }
    assert "sk-abc123" not in json.dumps(body)


def test_health_never_depends_on_the_provider(monkeypatch) -> None:
    async def boom(notes, hours, battery):
        raise RuntimeError("down")

    monkeypatch.setattr(main.interpreter, "interpret", boom)
    assert client.get("/health").json() == {"status": "ok"}
