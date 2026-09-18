#!/usr/bin/env python3

import argparse
import json
import sys
import time

import httpx


def load_cases(json_path: str):
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "cases" in data:
        return data["cases"]
    elif isinstance(data, list):
        return data
    else:
        raise ValueError(
            "Invalid JSON format for test cases. "
            "Must be a list or dict with 'cases' array."
        )


def evaluate_interpretation(actual, expected):
    if not actual or not expected:
        return actual == expected

    if len(actual) != len(expected):
        return False

    for act, exp in zip(actual, expected):
        if act.get("note_index") != exp.get("note_index"):
            return False

        if act.get("applies") != exp.get("applies"):
            return False

        if act.get("directive_type") != exp.get("directive_type"):
            return False

        act_adj = act.get("structured_adjustment")
        exp_adj = exp.get("structured_adjustment")

        if act_adj != exp_adj:
            return False

    return True


def main():
    parser = argparse.ArgumentParser(
        description="GridWise Smoke Test Runner"
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="http://127.0.0.1:8000",
        help="Target API server URL",
    )
    parser.add_argument(
        "--cases",
        default="tests/edge_cases.json",
        help="Path to JSON test cases file",
    )

    args = parser.parse_args()

    endpoint = f"{args.url.rstrip('/')}/optimize-energy"

    try:
        cases = load_cases(args.cases)
        print(f"Loaded {len(cases)} test cases from {args.cases}")
    except Exception as e:
        print(
            f"Failed to load test cases from {args.cases}: {e}"
        )
        sys.exit(1)

    passed = 0
    total = len(cases)
    latencies = []

    print("\n" + "=" * 60)
    print(f" Running GridWise Smoke Tests against {endpoint}")
    print("=" * 60 + "\n")

    for case in cases:
        cid = case.get("id", "UNKNOWN")
        label = case.get("label", "No Label")
        payload = case.get("input", {})
        expected_out = case.get("expected_output", {})

        print(f"[{cid}] {label} ... ", end="", flush=True)

        start_time = time.time()

        try:
            resp = httpx.post(
                endpoint,
                json=payload,
                timeout=10.0,
            )

            elapsed_ms = (time.time() - start_time) * 1000
            latencies.append(elapsed_ms)

            if resp.status_code != 200:
                print(
                    f"FAIL (HTTP {resp.status_code}) "
                    f"[{elapsed_ms:.1f}ms]"
                )
                print(f"  Response: {resp.text}")
                continue

            resp_data = resp.json()

            act_interp = resp_data.get(
                "directive_interpretation", []
            )
            exp_interp = expected_out.get(
                "directive_interpretation", []
            )

            is_match = evaluate_interpretation(
                act_interp,
                exp_interp,
            )

            if is_match:
                print(f"PASS [{elapsed_ms:.1f}ms]")
                passed += 1
            else:
                print(f"MISMATCH [{elapsed_ms:.1f}ms]")

                print("  Expected interpretation:")
                print(json.dumps(exp_interp, indent=4))

                print("  Actual interpretation:")
                print(json.dumps(act_interp, indent=4))

        except Exception as e:
            elapsed_ms = (time.time() - start_time) * 1000
            print(f"ERROR [{elapsed_ms:.1f}ms]: {e}")

    print("\n" + "=" * 60)
    print(f" Summary: {passed}/{total} Passed")

    if latencies:
        avg_lat = sum(latencies) / len(latencies)

        if len(latencies) > 1:
            p95_index = min(
                int(len(latencies) * 0.95),
                len(latencies) - 1,
            )
            p95_lat = sorted(latencies)[p95_index]
        else:
            p95_lat = latencies[0]

        print(
            f" Latency - Avg: {avg_lat:.1f}ms | "
            f"p95: {p95_lat:.1f}ms"
        )

    print("=" * 60 + "\n")

    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
