"""Deterministic guardrails (Problem Statement section 08).

Language-model output is treated as UNTRUSTED structured data. Nothing reaches the
optimizer until it has passed through `normalize_interpretations`, which enforces:

  * exactly one entry per operator note, in note_index order 0..N-1
  * only the six supported directive types
  * hours are unique integers 0..23 in ascending order
  * numeric ranges (solar factor, battery reserve, grid cap)
  * `applies` semantics -- no_op is the only type allowed with applies = false

Anything the model returns that cannot be repaired deterministically is downgraded
to `no_op` rather than being invented into a constraint.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .schemas import Battery, DirectiveInterpretation

SUPPORTED_TYPES = frozenset(
    {
        "solar_reduction",
        "minimum_battery_reserve",
        "no_charge_window",
        "no_discharge_window",
        "max_grid_window",
    }
)

_DEFAULT_NO_OP_EXPLANATION = "This note does not affect today's 24-hour energy schedule."


@dataclass(frozen=True)
class Directive:
    """A validated directive, safe to hand to the optimizer."""

    note_index: int
    directive_type: str
    hours: tuple[int, ...]
    factor: float | None = None
    minimum_energy_kwh: float | None = None
    max_grid_kwh: float | None = None


def _finite(value: Any) -> float | None:
    """Coerce to a finite float, or None if that is not possible."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _clean_hours(raw: Any) -> tuple[int, ...]:
    """Unique integers 0..23 in ascending order. Junk is dropped, not guessed."""
    if not isinstance(raw, (list, tuple, set)):
        return ()
    out: set[int] = set()
    for item in raw:
        if isinstance(item, bool):
            continue
        try:
            hour = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= hour <= 23:
            out.add(hour)
    return tuple(sorted(out))


def _window_bound(value: Any, *, upper: int) -> int | None:
    """Coerce a clock hour to an int within 0..upper, or None if that is not possible."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= upper else None


def expand_window(start: Any, end: Any) -> tuple[int, ...]:
    """Expand a half-open clock range into whole hours.

    The start hour is included and the end hour is excluded (Problem Statement
    section 5.1), so 18..22 is [18, 19, 20, 21]. A range whose end falls at or
    before its start wraps through midnight: 22..6 is [22, 23, 0, 1, 2, 3, 4, 5].
    `end` may be 24, meaning midnight at the close of the day.

    This is the deterministic replacement for asking the model to expand windows
    itself; the model only reports the two clock times the note named.
    """
    first = _window_bound(start, upper=23)
    last = _window_bound(end, upper=24)
    if first is None or last is None:
        return ()
    if last > first:
        hours: object = range(first, last)
    elif last == first:
        hours = (first,)
    else:  # wraps past midnight
        hours = list(range(first, 24)) + list(range(0, last))
    return tuple(sorted({h % 24 for h in hours}))


def _resolve_hours(item: dict[str, Any]) -> tuple[int, ...]:
    """Hours for a directive: a named range wins, else an explicit list of hours."""
    window = expand_window(item.get("start_hour"), item.get("end_hour"))
    if window:
        return window
    return _clean_hours(item.get("hours"))



def _normalize_factor(raw: Any) -> float | None:
    """`factor` is the usable fraction REMAINING, in [0, 1].

    A model that answers in percent ("25" for 25%) is repaired deterministically;
    anything still outside the legal range is rejected.
    """
    value = _finite(raw)
    if value is None:
        return None
    if 1.0 < value <= 100.0:
        value = value / 100.0
    if 0.0 <= value <= 1.0:
        return round(value, 6)
    return None


def _structured_adjustment(directive: Directive) -> dict[str, Any]:
    hours = list(directive.hours)
    if directive.directive_type == "solar_reduction":
        return {"hours": hours, "factor": directive.factor}
    if directive.directive_type == "minimum_battery_reserve":
        return {"hours": hours, "minimum_energy_kwh": directive.minimum_energy_kwh}
    if directive.directive_type == "max_grid_window":
        return {"hours": hours, "max_grid_kwh": directive.max_grid_kwh}
    # no_charge_window / no_discharge_window
    return {"hours": hours}


def _build_directive(index: int, item: dict[str, Any], battery: Battery) -> Directive | None:
    """Return a validated Directive, or None to mean 'downgrade this note to no_op'."""
    directive_type = item.get("directive_type")
    if directive_type not in SUPPORTED_TYPES:
        return None

    hours = _resolve_hours(item)
    if not hours:
        # A window directive with no valid hours cannot be applied to anything.
        return None

    if directive_type == "solar_reduction":
        factor = _normalize_factor(item.get("factor"))
        if factor is None:
            return None
        return Directive(index, directive_type, hours, factor=factor)

    if directive_type == "minimum_battery_reserve":
        reserve = _finite(item.get("minimum_energy_kwh"))
        if reserve is None or reserve < 0:
            return None
        # Guardrail: a reserve above capacity is impossible; clamp instead of inventing.
        reserve = min(reserve, battery.capacity_kwh)
        return Directive(index, directive_type, hours, minimum_energy_kwh=round(reserve, 6))

    if directive_type == "max_grid_window":
        cap = _finite(item.get("max_grid_kwh"))
        if cap is None or cap < 0:
            return None
        return Directive(index, directive_type, hours, max_grid_kwh=round(cap, 6))

    return Directive(index, directive_type, hours)


def normalize_interpretations(
    raw_items: Any,
    note_count: int,
    battery: Battery,
) -> tuple[list[DirectiveInterpretation], list[Directive]]:
    """Turn raw model output into exactly `note_count` validated entries.

    Returns (response entries in note_index order, directives to apply).
    """
    by_index: dict[int, dict[str, Any]] = {}
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            index = item.get("note_index")
            if isinstance(index, bool) or not isinstance(index, int):
                continue
            # First mapping wins; duplicates are discarded rather than merged.
            if 0 <= index < note_count and index not in by_index:
                by_index[index] = item

    entries: list[DirectiveInterpretation] = []
    directives: list[Directive] = []

    for index in range(note_count):
        item = by_index.get(index, {})
        directive = _build_directive(index, item, battery) if item else None
        explanation = item.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            explanation = None

        if directive is None:
            entries.append(
                DirectiveInterpretation(
                    note_index=index,
                    applies=False,
                    directive_type="no_op",
                    structured_adjustment=None,
                    explanation=explanation or _DEFAULT_NO_OP_EXPLANATION,
                )
            )
            continue

        directives.append(directive)
        entries.append(
            DirectiveInterpretation(
                note_index=index,
                applies=True,
                directive_type=directive.directive_type,
                structured_adjustment=_structured_adjustment(directive),
                explanation=explanation or f"Applied {directive.directive_type} for hours {list(directive.hours)}.",
            )
        )

    return entries, directives
