"""Score the interpreter against the paraphrase corpus, one line per phrasing family.

    python scripts/paraphrase_bench.py                # every family
    python scripts/paraphrase_bench.py factor_lost    # only the families named

Needs OPENAI_API_KEY. Notes are sent in batches of three, matching the 1-3 notes a
real request carries, so batching effects are part of what is measured.

A single overall percentage hides what matters. Per-family scoring says *which*
wording broke, which is the difference between "83% correct" and "every note whose
window ends in 'until' is off by one hour".
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:  # local convenience; the key may also come from the environment
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover
    pass

from app.guardrails import normalize_interpretations  # noqa: E402
from app.interpreter import InterpreterUnavailable, OperatorNoteInterpreter  # noqa: E402
from app.schemas import Battery, HourEntry  # noqa: E402

CORPUS = json.loads((ROOT / "tests" / "paraphrase_cases.json").read_text(encoding="utf-8"))
BATTERY = Battery(**CORPUS["_meta"]["battery"])
BATCH = 3

# A realistic solar/tariff day, so the model can resolve bare clock numbers the way it
# would in a live request ("panel washing from one until three" is not 1 AM).
PUBLIC = json.loads((ROOT / "samples" / "public_cases.json").read_text(encoding="utf-8"))
HOURS = [HourEntry(**h) for h in PUBLIC["cases"][0]["input"]["hours"]]


def _actual(entry) -> dict:
    adjustment = entry.structured_adjustment or {}
    return {
        "directive_type": entry.directive_type,
        "hours": list(adjustment.get("hours", [])),
        "factor": adjustment.get("factor"),
        "minimum_energy_kwh": adjustment.get("minimum_energy_kwh"),
        "max_grid_kwh": adjustment.get("max_grid_kwh"),
    }


def _matches(expected: dict, actual: dict) -> tuple[bool, str]:
    if actual["directive_type"] != expected["directive_type"]:
        return False, f"type {actual['directive_type']} != {expected['directive_type']}"
    if expected["directive_type"] == "no_op":
        return True, ""
    if actual["hours"] != list(expected.get("hours", [])):
        return False, f"hours {actual['hours']} != {expected.get('hours')}"
    for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
        want = expected.get(key)
        if want is None:
            continue
        got = actual.get(key)
        if got is None or abs(got - want) > 0.01:
            return False, f"{key} {got} != {want}"
    return True, ""


async def main() -> int:
    wanted = set(sys.argv[1:])
    cases = [c for c in CORPUS["cases"] if not wanted or c["family"] in wanted]
    if not cases:
        print(f"no cases match {sorted(wanted)}")
        return 2

    # Interleave families so a batch never carries two restatements of the same
    # directive. Consecutive corpus rows are paraphrases of each other, and a model
    # shown three versions of one instruction reasonably marks the repeats as adding
    # nothing -- an artefact of how we batch, not a fault in the interpretation. A
    # real request carries 1-3 unrelated notes.
    by_family: dict[str, list[dict]] = defaultdict(list)
    for case in cases:
        by_family[case["family"]].append(case)
    interleaved: list[dict] = []
    while any(by_family.values()):
        for family in list(by_family):
            if by_family[family]:
                interleaved.append(by_family[family].pop(0))
    cases = interleaved

    interpreter = OperatorNoteInterpreter()
    if not interpreter.configured:
        print("OPENAI_API_KEY is not set: this benchmark measures the model, not the fallback.")
        return 2
    print(f"model {interpreter.model} · {len(cases)} notes · batches of {BATCH}\n")

    passed: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    failures: list[str] = []

    for start in range(0, len(cases), BATCH):
        batch = cases[start : start + BATCH]
        notes = [c["note"] for c in batch]
        try:
            raw = await interpreter.interpret(notes, HOURS, BATTERY)
        except InterpreterUnavailable as exc:
            print(f"  provider unavailable: {exc}")
            return 1
        entries, _directives = normalize_interpretations(raw, len(notes), BATTERY)

        for case, entry in zip(batch, entries):
            family = case["family"]
            total[family] += 1
            ok, why = _matches(case["expect"], _actual(entry))
            passed[family] += ok
            if not ok:
                failures.append(f"  {case['id']:10} [{family}] {why}\n             {case['note'][:88]}")

    print(f"{'family':22} {'score':>8}   {'':4}")
    weakest = []
    for family in CORPUS["_meta"]["families"]:
        if family not in total:
            continue
        got, want = passed[family], total[family]
        pct = 100 * got / want
        bar = "#" * round(pct / 10) + "." * (10 - round(pct / 10))
        flag = "" if got == want else "   <-- "
        print(f"{family:22} {got:>3}/{want:<4} {bar}{flag}")
        if got != want:
            weakest.append((pct, family))

    if failures:
        print("\nfailures")
        print("\n".join(failures))

    grand_ok, grand_n = sum(passed.values()), sum(total.values())
    print(f"\noverall {grand_ok}/{grand_n}  ({100 * grand_ok / grand_n:.1f}%)")
    if weakest:
        worst = ", ".join(f for _pct, f in sorted(weakest)[:3])
        print(f"weakest families: {worst}")
    return 0 if grand_ok == grand_n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
