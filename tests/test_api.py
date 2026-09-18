"""End-to-end API tests.

The model provider is stubbed so the suite runs offline and deterministically; the
real interpreter is exercised by `scripts/smoke_test.py` against a live service.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.interpreter import InterpreterUnavailable
from app.validator import TOLERANCE

CASES = json.loads(
    (Path(__file__).resolve().parents[1] / "samples" / "public_cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]

client = TestClient(main.app, raise_server_exceptions=False)


def _ground_truth_items(case: dict) -> list[dict]:
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
                "explanation": "stubbed",
            }
        )
    return items


@pytest.fixture
def stub_interpreter(monkeypatch):
    """Replace the provider call with the organizer's ground-truth interpretation."""

    def _install(case: dict):
        async def fake(notes, hours, battery):
            return _ground_truth_items(case)

        monkeypatch.setattr(main.interpreter, "interpret", fake)

    return _install


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_optimize_matches_contract(case: dict, stub_interpreter) -> None:
    stub_interpreter(case)
    response = client.post("/optimize-energy", json=case["input"])
    assert response.status_code == 200
    body = response.json()

    assert body["scenario_id"] == case["input"]["scenario_id"]
    assert [e["note_index"] for e in body["directive_interpretation"]] == list(
        range(len(case["input"]["operator_notes"]))
    )
    for entry in body["directive_interpretation"]:
        if entry["directive_type"] == "no_op":
            assert entry["applies"] is False
            assert entry["structured_adjustment"] is None
        else:
            assert entry["applies"] is True
            assert isinstance(entry["structured_adjustment"], dict)
            hours = entry["structured_adjustment"]["hours"]
            assert hours == sorted(set(hours))
            assert all(0 <= h <= 23 for h in hours)

    assert [p["hour"] for p in body["hourly_plan"]] == list(range(24))
    for plan in body["hourly_plan"]:
        assert plan["battery_action"] in {"charge", "discharge", "idle"}
        assert plan["grid_kwh"] >= -TOLERANCE
        assert plan["solar_used_kwh"] >= -TOLERANCE
        assert plan["battery_kwh"] >= -TOLERANCE
        if plan["battery_action"] == "idle":
            assert abs(plan["battery_kwh"]) <= TOLERANCE

    assert body["total_cost_bdt"] <= case["expected_output"]["total_cost_bdt"] + TOLERANCE
    assert isinstance(body["plan_summary"], str) and body["plan_summary"]


def test_provider_failure_degrades_instead_of_crashing(monkeypatch) -> None:
    """A dead model provider must not take the service down (safe failure)."""

    async def boom(notes, hours, battery):
        raise InterpreterUnavailable("simulated provider outage")

    monkeypatch.setattr(main.interpreter, "interpret", boom)
    response = client.post("/optimize-energy", json=CASES[0]["input"])
    assert response.status_code == 200
    assert len(response.json()["hourly_plan"]) == 24


def test_malformed_requests_return_400() -> None:
    assert client.post("/optimize-energy", content=b"{not json").status_code == 400
    assert client.post("/optimize-energy", json={"scenario_id": "X"}).status_code == 400

    short_hours = json.loads(json.dumps(CASES[0]["input"]))
    short_hours["hours"] = short_hours["hours"][:12]
    assert client.post("/optimize-energy", json=short_hours).status_code == 400

    no_notes = json.loads(json.dumps(CASES[0]["input"]))
    no_notes["operator_notes"] = []
    assert client.post("/optimize-energy", json=no_notes).status_code == 400

    blank_note = json.loads(json.dumps(CASES[0]["input"]))
    blank_note["operator_notes"] = ["   "]
    assert client.post("/optimize-energy", json=blank_note).status_code == 400


def test_repeated_requests_are_stable(stub_interpreter) -> None:
    case = CASES[5]
    stub_interpreter(case)
    costs = set()
    for _ in range(3):
        response = client.post("/optimize-energy", json=case["input"])
        assert response.status_code == 200
        costs.add(round(response.json()["total_cost_bdt"], 2))
    assert len(costs) == 1
