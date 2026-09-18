"""Run the public sample pack against a running service and score it like the judge.

    python scripts/smoke_test.py                       # local service
    python scripts/smoke_test.py https://your-host

Checks, per case: interpretation vs the organizer's published ground truth, a full
hour-by-hour replay of the returned schedule, reported-totals consistency, and the
cost ratio used by the Optimization Quality rubric. Also reports p95 latency.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.guardrails import normalize_interpretations  # noqa: E402
from app.schemas import HourPlan, OptimizeRequest  # noqa: E402
from app.validator import TOLERANCE, replay  # noqa: E402

CASES = json.loads((ROOT / "samples" / "public_cases.json").read_text(encoding="utf-8"))["cases"]


def ground_truth_items(case: dict) -> list[dict]:
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
                "explanation": "",
            }
        )
    return items


def main(base_url: str) -> int:
    base_url = base_url.rstrip("/")
    client = httpx.Client(timeout=40.0)

    health = client.get(f"{base_url}/health")
    print(f"GET /health -> {health.status_code} {health.text.strip()}")
    if health.status_code != 200 or health.json().get("status") != "ok":
        print("FAIL: health endpoint is not ready")
        return 1

    latencies: list[float] = []
    interp_hits = interp_total = 0
    valid_cases = 0
    ratios: list[float] = []
    failures: list[str] = []

    for case in CASES:
        request = OptimizeRequest.model_validate(case["input"])
        hours = request.hours_in_order()
        expected = case["expected_output"]["directive_interpretation"]

        started = time.perf_counter()
        response = client.post(f"{base_url}/optimize-energy", json=case["input"])
        elapsed = time.perf_counter() - started
        latencies.append(elapsed)

        if response.status_code != 200:
            failures.append(f"{case['id']}: HTTP {response.status_code}")
            print(f"{case['id']:10} FAIL  HTTP {response.status_code}  ({elapsed:.2f}s)")
            continue

        body = response.json()
        produced = body.get("directive_interpretation", [])

        # --- interpretation vs organizer ground truth -------------------------
        matched = 0
        for reference in expected:
            entry = next(
                (p for p in produced if p.get("note_index") == reference["note_index"]), None
            )
            if (
                entry
                and entry.get("directive_type") == reference["directive_type"]
                and entry.get("applies") == reference["applies"]
                and entry.get("structured_adjustment") == reference["structured_adjustment"]
            ):
                matched += 1
            else:
                got = (
                    (entry or {}).get("directive_type"),
                    (entry or {}).get("structured_adjustment"),
                )
                failures.append(
                    f"{case['id']} note {reference['note_index']}: "
                    f"want {(reference['directive_type'], reference['structured_adjustment'])} got {got}"
                )
        interp_hits += matched
        interp_total += len(expected)

        # --- replay the returned schedule against the TRUE directives ---------
        _, true_directives = normalize_interpretations(
            ground_truth_items(case), len(request.operator_notes), request.battery
        )
        plan = [HourPlan.model_validate(p) for p in body["hourly_plan"]]
        totals = (body["total_grid_kwh"], body["total_cost_bdt"], body["peak_grid_kwh"])
        violations = replay(hours, request.battery, true_directives, plan, totals)

        reference_cost = case["expected_output"]["total_cost_bdt"]
        if violations:
            failures.extend(f"{case['id']}: {v}" for v in violations[:3])
            ratios.append(0.0)
            status = f"INVALID ({len(violations)} violation(s))"
        else:
            valid_cases += 1
            our_cost = body["total_cost_bdt"]
            ratio = 1.0 if our_cost <= TOLERANCE else min(1.0, reference_cost / our_cost)
            ratios.append(ratio)
            status = f"valid  cost {our_cost:>9.2f} vs ref {reference_cost:>9.2f}  ratio {ratio:.4f}"

        print(f"{case['id']:10} {matched}/{len(expected)} notes  {status}  ({elapsed:.2f}s)")

    print("\n" + "=" * 72)
    print(f"Interpretation exact-match : {interp_hits}/{interp_total}")
    print(f"Valid schedules            : {valid_cases}/{len(CASES)}")
    print(f"Mean cost-quality ratio    : {statistics.mean(ratios):.4f}  -> {10 * statistics.mean(ratios):.2f}/10")
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
    print(f"Latency mean/p95           : {statistics.mean(latencies):.2f}s / {p95:.2f}s", end="")
    print("  (<=5s scores 3/3)" if p95 <= 5 else "  (>5s loses latency points)")

    if failures:
        print(f"\n{len(failures)} issue(s):")
        for failure in failures[:20]:
            print(f"  - {failure}")
        return 1

    print("\nAll public cases passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"))
