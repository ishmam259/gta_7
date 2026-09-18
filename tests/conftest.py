"""Shared fixtures for the adversarial vulnerability-discovery suite.

These fixtures are deliberately generic so both `test_adversarial.py` (new) and
the existing positive-path tests can share them without name collisions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List

import pytest
from fastapi.testclient import TestClient

from app import main


# --------------------------------------------------------------------------- #
# HTTP client                                                                  #
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_client() -> TestClient:
    """A `TestClient` with `raise_server_exceptions=False` so 5xx tests can
    inspect the body instead of the test process crashing."""
    return TestClient(main.app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# Interpreter stub                                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def stub_interpreter(monkeypatch: pytest.MonkeyPatch) -> Callable[[List[dict]], Any]:
    """Replace `app.main.interpreter.interpret` with an async stub.

    Returns a setter: call it with the list of raw model-output dicts (one per
    note) you want returned. The setter installs the patch via pytest's
    `monkeypatch`, so it auto-cleans after each test.

    Each dict MUST be in the shape the LLM emits:

        {
            "note_index": int,
            "directive_type": str,
            "hours": [int, ...],
            "factor": float | None,
            "minimum_energy_kwh": float | None,
            "max_grid_kwh": float | None,
            "explanation": str,
        }

    The fixture does NOT itself enforce guardrails -- tests that need that
    behaviour call `app.guardrails.normalize_interpretations` directly.
    """

    def _install(items: List[dict]) -> None:
        async def _fake(_notes, _hours, _battery):
            return items

        monkeypatch.setattr(main.interpreter, "interpret", _fake)

    return _install


@pytest.fixture
def stub_interpreter_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[BaseException], Any]:
    """Patch `interpret` to raise the given exception on every call.

    Used to trigger the deterministic fallback parser path (by raising
    `InterpreterUnavailable`) or to inject arbitrary errors for the 500-leak
    tests.
    """

    def _install(exc: BaseException) -> None:
        async def _fake(_notes, _hours, _battery):
            raise exc

        monkeypatch.setattr(main.interpreter, "interpret", _fake)

    return _install


# --------------------------------------------------------------------------- #
# Baseline scenario                                                            #
# --------------------------------------------------------------------------- #


def _make_baseline() -> dict:
    """Return a deepcopy-friendly dict matching `OptimizeRequest`.

    Demand pattern: 0.8 kWh baseline + 1.5 kWh evening bump hours 18-22.
    Tariff: 8.5 mid-day, 12.0 peak (18-21).
    Solar: 0.0-4.5 kWh with bell shape peaking at noon.
    Battery: 10 kWh capacity, 5 kWh initial, 1 kWh min reserve, +/-3 kWh rate.
    Two operator notes describing the canonical directives a healthy LLM
    would emit.
    """
    hours = []
    for h in range(24):
        evening = 1.5 if 18 <= h <= 22 else 0.0
        demand = 0.8 + evening
        tariff = 12.0 if 18 <= h <= 21 else 8.5
        # bell-shaped solar, max ~4.5 at hour 12
        solar = max(0.0, 4.5 * (1.0 - abs(h - 12) / 6.0)) if 6 <= h <= 18 else 0.0
        hours.append(
            {
                "hour": h,
                "demand_kwh": demand,
                "solar_kwh": solar,
                "tariff_bdt_per_kwh": tariff,
            }
        )
    return {
        "scenario_id": "sec-baseline-01",
        "operator_notes": [
            "Maintain 30% battery reserve across all hours.",
            "Do not charge from 18:00 to 21:00.",
        ],
        "hours": hours,
        "battery": {
            "capacity_kwh": 10.0,
            "initial_energy_kwh": 5.0,
            "minimum_energy_kwh": 1.0,
            "max_charge_kwh_per_hour": 3.0,
            "max_discharge_kwh_per_hour": 3.0,
        },
    }


@pytest.fixture
def valid_baseline_scenario() -> dict:
    """A fresh dict copy each call (tests should mutate freely)."""
    import copy

    return copy.deepcopy(_make_baseline())


@pytest.fixture
def valid_baseline_directives() -> List[dict]:
    """The two canonical directives the baseline interpreter would emit."""
    return [
        {
            "note_index": 0,
            "directive_type": "minimum_battery_reserve",
            "hours": list(range(24)),
            "factor": None,
            "minimum_energy_kwh": 3.0,
            "max_grid_kwh": None,
            "explanation": "30% of 10 kWh capacity = 3 kWh reserve",
        },
        {
            "note_index": 1,
            "directive_type": "no_charge_window",
            "hours": [18, 19, 20],
            "factor": None,
            "minimum_energy_kwh": None,
            "max_grid_kwh": None,
            "explanation": "Charging blocked during evening peak",
        },
    ]


# --------------------------------------------------------------------------- #
# Filesystem                                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def path_dotenv() -> Path:
    """Path to `gta_7/.env`. The file is gitignored; missing is OK."""
    return Path(__file__).resolve().parents[1] / ".env"


# --------------------------------------------------------------------------- #
# Terminal summary hook                                                        #
# --------------------------------------------------------------------------- #
#
# Lives in conftest.py because pytest auto-discovers conftest as a plugin,
# which is the only reliable place to install a `pytest_terminal_summary`
# hook in pytest >= 8. The class registry is imported lazily so this file
# doesn't depend on test_adversarial.py's imports during collection.


def pytest_terminal_summary(terminalreporter, exitstatus, config):  # noqa: ARG001
    """Print a one-glance PASS/FAIL table per vulnerability class.

    Grouped by `TestVuln<Name>` class. Each row shows the count of passing,
    failing, and skipped tests, plus the verdict. The class registry is
    imported from `test_adversarial` so the labels stay in sync.
    """
    import sys

    try:
        from tests.test_adversarial import VULN_CLASS_MAP
    except Exception as exc:  # pragma: no cover - if import fails, skip silently
        sys.stderr.write(f"[adversarial] could not import VULN_CLASS_MAP: {exc!r}\n")
        return

    try:
        passed = set(terminalreporter.stats.get("passed", []))
        failed = set(terminalreporter.stats.get("failed", []))
        skipped = set(terminalreporter.stats.get("skipped", []))

        def class_of(report):
            nodeid = getattr(report, "nodeid", "")
            parts = nodeid.split("::")
            if len(parts) >= 2:
                return parts[1]
            return None

        by_class: dict = {}
        for r in passed | failed | skipped:
            cls = class_of(r)
            if cls is None or cls not in VULN_CLASS_MAP:
                continue
            row = by_class.setdefault(cls, {"passed": 0, "failed": 0, "skipped": 0})
            if r in failed:
                row["failed"] += 1
            elif r in skipped:
                row["skipped"] += 1
            else:
                row["passed"] += 1

        terminalreporter.write_sep("=", "ADVERSARIAL SECURITY REPORT")
        terminalreporter.write_line(
            f"{'Vulnerability class':<40} {'Tests':>6} {'Pass':>6} {'Fail':>6} {'Skip':>6} {'Verdict':>8}"
        )
        terminalreporter.write_line("-" * 76)
        total_pass = total_fail = total_skip = total_tests = 0
        worst = "PASS"
        for cls_name, label in VULN_CLASS_MAP.items():
            row = by_class.get(cls_name, {"passed": 0, "failed": 0, "skipped": 0})
            n = row["passed"] + row["failed"] + row["skipped"]
            verdict = "PASS" if row["failed"] == 0 else "FAIL"
            if verdict == "FAIL":
                worst = "FAIL"
            terminalreporter.write_line(
                f"{label:<40} {n:>6} {row['passed']:>6} {row['failed']:>6} {row['skipped']:>6} {verdict:>8}"
            )
            total_pass += row["passed"]
            total_fail += row["failed"]
            total_skip += row["skipped"]
            total_tests += n
        terminalreporter.write_line("-" * 76)
        terminalreporter.write_line(
            f"{'TOTAL':<40} {total_tests:>6} {total_pass:>6} {total_fail:>6} {total_skip:>6} {worst:>8}"
        )
        if total_fail > 0:
            terminalreporter.write_line(
                f"Severity: investigate the {total_fail} failure(s) above."
            )
        else:
            terminalreporter.write_line(
                "Severity: clean -- all adversarial assertions passed."
            )
        terminalreporter.write_sep("=", "END ADVERSARIAL SECURITY REPORT")
    except Exception as exc:  # pragma: no cover - report must never crash pytest
        sys.stderr.write(f"[adversarial] report hook error: {exc!r}\n")
