"""EMERGENCY-ONLY deterministic parser.

This is NOT the interpretation path. `app.interpreter` (a language model) is the
required and normal interpreter for operator notes; see the LLM requirement in the
Problem Statement. This module exists purely so that a provider outage, quota
exhaustion or timeout degrades the service instead of taking it down, which the
rubric scores under Performance & Reliability / safe failure.

It runs only when `InterpreterUnavailable` is raised, and every activation is logged.
"""

from __future__ import annotations

import re
from typing import Any

from .guardrails import _expand_window as expand_window

_NEGATION = re.compile(
    r"\b(no|not|do not|don't|cannot|can't|avoid|without|unavailable|suspend(?:ed)?|"
    r"disabled?|paused?|halt(?:ed)?|prohibit(?:ed)?|forbidden|offline|out of service|"
    r"must not|may not|refrain|isolat\w*|de-?energi[sz]\w*|locked out|lockout|"
    r"block(?:ed)?|barred|restrict(?:ed)?|curtail(?:ed)?|off[- ]limits|embargo\w*|"
    r"prevent(?:ed)?|deni(?:ed|es)|shut ?down|taken out|inoperable|unable)\b",
    re.I,
)
_TIME_TOKEN = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b",
    re.I,
)
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|per\s*cent|percent)", re.I)
# A number bound to a unit is a quantity, never a clock time. These spans are blanked
# before the time scan, so "keep at least 20 kWh" cannot be mistaken for 8 PM.
_UNIT_NUMBER = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|per\s*cent|percent|kwh|kw\b|bdt|taka|"
    r"kilowatt[-\s]?hours?|kilowatts?)",
    re.I,
)
_ALL_DAY = re.compile(
    r"\b(all day|whole day|entire day|full day|throughout the day|throughout today|"
    r"all of today|at any time today|at all times|round the clock|24 hours)\b",
    re.I,
)
_WORD_FRACTIONS = {
    "half": 50.0,
    "a half": 50.0,
    "one half": 50.0,
    "a third": 33.0,
    "one third": 33.0,
    "a quarter": 25.0,
    "one quarter": 25.0,
    "a fifth": 20.0,
    "one fifth": 20.0,
    "a tenth": 10.0,
    "one tenth": 10.0,
}


def _hour_tokens(text: str) -> list[int]:
    """Best-effort extraction of whole-hour clock references, in order of appearance."""
    lowered = text.lower()
    found: list[tuple[int, int, str | None]] = []  # (position, hour, meridiem)

    for match in re.finditer(r"\bnoon\b|\bmidday\b", lowered):
        found.append((match.start(), 12, "pm"))
    for match in re.finditer(r"\bmidnight\b", lowered):
        found.append((match.start(), 0, "am"))

    for match in _TIME_TOKEN.finditer(lowered):
        raw_hour = int(match.group(1))
        if raw_hour > 24:
            continue
        meridiem = match.group(3)
        meridiem = meridiem.replace(".", "")[0:2] if meridiem else None
        # A ":mm" form is an explicit 24-hour clock reference.
        if match.group(2) is not None and meridiem is None:
            meridiem = "24h"
        found.append((match.start(), raw_hour, meridiem))

    found.sort()
    if not found:
        return []

    # Let an explicit meridiem later in the phrase disambiguate earlier bare numbers.
    trailing = next((m for _, _, m in reversed(found) if m in ("am", "pm")), None)
    hours: list[int] = []
    for _, hour, meridiem in found:
        effective = meridiem if meridiem in ("am", "pm") else trailing
        if meridiem == "24h":
            effective = None
        if effective == "pm" and hour < 12:
            hour += 12
        elif effective == "am" and hour == 12:
            hour = 0
        hours.append(hour % 24 if hour != 24 else 0)
    return hours


def _window(text: str) -> list[int]:
    """Map a phrase to a half-open whole-hour window: start inclusive, end exclusive.

    Quantities carrying a unit are blanked first, so "keep at least 20 kWh from 6 PM
    until 10 PM" reads the window as 18..22 instead of seizing on the 20. Midnight
    wrapping and the exclusive end are delegated to `guardrails.expand_window`, which
    is the single definition of that convention.
    """
    if _ALL_DAY.search(text):
        return list(range(24))
    masked = _UNIT_NUMBER.sub(lambda m: " " * len(m.group(0)), text)
    hours = _hour_tokens(masked)
    if len(hours) < 2:
        return [hours[0]] if hours else []
    return list(expand_window(hours[0], hours[1]))


# Wording immediately in front of the figure that marks it as what REMAINS
# ("drops to 20%", "leaves roughly one fifth") rather than what is LOST.
_STATES_REMAINING = re.compile(
    r"\b(to|at|leaves?|leaving|remain\w*|left|available|usable|treated as|only)\s*"
    r"(about|roughly|around|approximately|just|nearly)?\s*$",
    re.I,
)
_REDUCTION_WORD = re.compile(r"\b(reduc\w*|drop\w*|down|lower\w*|cut|less|loss|decreas\w*)\b", re.I)


def _remaining_factor(text: str) -> float | None:
    """The fraction of solar still usable, or None when the note gives no figure.

    The figure a note states may be what remains ("drops to 20%") or what is lost
    ("a 20% drop", "an 80% reduction"). Which one it is depends on the few words
    immediately before the number, not on whether the note mentions a reduction
    somewhere -- an earlier version tested the whole sentence and so read the "2"
    of "to 2 PM" as evidence, inverting every note whose window ended in "to".
    """
    lowered = text.lower().replace("-", " ")
    percent = _PERCENT.search(lowered)
    if percent is not None:
        value: float | None = float(percent.group(1))
        at = percent.start()
    else:
        value, at = None, -1
        for phrase, pct in _WORD_FRACTIONS.items():
            found = re.search(r"\b" + re.escape(phrase) + r"\b", lowered)
            if found is not None:
                value, at = pct, found.start()
                break
    if value is None:
        return None

    states_remaining = bool(_STATES_REMAINING.search(lowered[max(0, at - 30):at]))
    if _REDUCTION_WORD.search(lowered) and not states_remaining:
        value = 100.0 - value
    return round(max(0.0, min(100.0, value)) / 100.0, 6)


def _number_before(text: str, unit_pattern: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*" + unit_pattern, text, re.I)
    return float(match.group(1)) if match else None


def parse_note(index: int, note: str, capacity_kwh: float) -> dict[str, Any]:
    """Return a raw candidate in the same shape the model produces."""
    text = note.strip()
    lowered = text.lower()
    negated = bool(_NEGATION.search(lowered))
    base: dict[str, Any] = {
        "note_index": index,
        "directive_type": "no_op",
        "hours": [],
        "factor": None,
        "minimum_energy_kwh": None,
        "max_grid_kwh": None,
        "explanation": "Deterministic fallback interpretation (model provider unavailable).",
    }

    hours = _window(text)

    if re.search(r"\b(solar|pv|photovoltaic|panel|rooftop)\b", lowered):
        factor = _remaining_factor(text)
        if factor is not None and hours:
            return {**base, "directive_type": "solar_reduction", "hours": hours, "factor": factor}

    if re.search(r"\bcharg\w*", lowered) and negated and hours:
        return {**base, "directive_type": "no_charge_window", "hours": hours}

    if re.search(r"\bdischarg\w*", lowered) and negated and hours:
        return {**base, "directive_type": "no_discharge_window", "hours": hours}

    if re.search(r"\b(grid|import|feeder|transformer|supply|draw)\b", lowered) and hours:
        cap = _number_before(text, r"kwh")
        if cap is not None and re.search(
            r"(\b(exceed|cap|capped|limit\w*|no more than|at most|maximum|max|"
            r"at or below|below|under|no higher than|not go above|within)\b|<=|≤)",
            lowered,
        ):
            return {**base, "directive_type": "max_grid_window", "hours": hours, "max_grid_kwh": cap}

    if re.search(r"\b(reserve|at least|minimum|no lower than|not fall below|keep)\b", lowered) and hours:
        percent = _PERCENT.search(lowered)
        reserve = _number_before(text, r"kwh")
        if reserve is None and percent:
            reserve = capacity_kwh * float(percent.group(1)) / 100.0
        if reserve is not None:
            return {
                **base,
                "directive_type": "minimum_battery_reserve",
                "hours": hours,
                "minimum_energy_kwh": round(reserve, 6),
            }

    return base


def parse_notes(notes: list[str], capacity_kwh: float) -> list[dict[str, Any]]:
    return [parse_note(i, note, capacity_kwh) for i, note in enumerate(notes)]
