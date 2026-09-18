"""LLM operator-note interpretation (Problem Statement sections 04 and 08).

A language-capable generative model is the *required* interpretation path: it reads
the free-text operator notes and emits structured directive candidates. Those
candidates are untrusted until `app.guardrails` validates them, and only the
validated result reaches the optimizer.

Structured Outputs (`response_format={"type": "json_schema", ..., "strict": True}`)
constrain the model to the directive vocabulary at decode time, which removes a whole
class of malformed-output failures before the guardrails even run.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import AsyncOpenAI

from .schemas import Battery, HourEntry

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"


class InterpreterUnavailable(RuntimeError):
    """The model provider could not produce a usable interpretation."""


DIRECTIVE_SCHEMA: dict[str, Any] = {
    "name": "operator_note_interpretation",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["interpretations"],
        "properties": {
            "interpretations": {
                "type": "array",
                "description": "Exactly one entry per operator note, in note_index order.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "note_index",
                        "directive_type",
                        "hours",
                        "factor",
                        "minimum_energy_kwh",
                        "max_grid_kwh",
                        "explanation",
                    ],
                    "properties": {
                        "note_index": {
                            "type": "integer",
                            "description": "Zero-based index of the operator note.",
                        },
                        "directive_type": {
                            "type": "string",
                            "enum": [
                                "solar_reduction",
                                "minimum_battery_reserve",
                                "no_charge_window",
                                "no_discharge_window",
                                "max_grid_window",
                                "no_op",
                            ],
                        },
                        "hours": {
                            "type": "array",
                            "description": (
                                "Affected hours as unique integers 0-23 in ascending order. "
                                "Empty list for no_op."
                            ),
                            "items": {"type": "integer"},
                        },
                        "factor": {
                            "type": ["number", "null"],
                            "description": (
                                "solar_reduction only: usable fraction of solar REMAINING, 0-1."
                            ),
                        },
                        "minimum_energy_kwh": {
                            "type": ["number", "null"],
                            "description": "minimum_battery_reserve only: required floor in kWh.",
                        },
                        "max_grid_kwh": {
                            "type": ["number", "null"],
                            "description": "max_grid_window only: hourly grid import cap in kWh.",
                        },
                        "explanation": {
                            "type": "string",
                            "description": "One short sentence justifying the interpretation.",
                        },
                    },
                },
            }
        },
    },
}


SYSTEM_PROMPT = """You convert campus-operator notes into structured energy directives for a 24-hour scheduling optimizer. You are the interpretation stage of a pipeline; a deterministic validator checks your output afterwards.

Return exactly one entry per note, with note_index matching the note's position (0-based).

DIRECTIVE TYPES
- solar_reduction: usable solar is reduced during specific hours. Set `factor` = the fraction of solar that REMAINS.
- minimum_battery_reserve: battery energy must stay at or above a level during specific hours. Set `minimum_energy_kwh`.
- no_charge_window: the battery may not charge during specific hours.
- no_discharge_window: the battery may not discharge during specific hours.
- max_grid_window: grid import may not exceed a limit during specific hours. Set `max_grid_kwh`.
- no_op: the note does not affect today's 24-hour energy schedule.

RULES
1. Time windows are whole hours, start INCLUSIVE and end EXCLUSIVE. "1 PM to 3 PM" -> [13, 14]. "from 6 PM until 9 PM" -> [18, 19, 20]. "between 09:00 and 12:00" -> [9, 10, 11]. "during hour 14" -> [14].
2. `factor` is what REMAINS, not what is lost. "drops to 20%" -> 0.20. "an 80% reduction" -> 0.20. "roughly one-fifth of normal" -> 0.20. "cut by half" -> 0.50.
3. Reserves given as a percentage refer to battery CAPACITY. With capacity 500 kWh, "keep at least 30%" -> 150.
4. Notes about menus, schedules, registrations, staffing, next week, next month, or anything with no effect on today's electricity schedule are `no_op`: hours = [], all numeric fields null.
5. Never invent a directive type outside the list. Never alter demand, tariff, or battery limits. If a note is energy-related but does not map cleanly onto one of the five real types, use no_op.
6. Only ever emit hours in 0-23, unique, ascending.
7. A single note maps to exactly one directive. If a note mentions two rules, choose the one it states most directly.

Be literal and precise about numbers and hour boundaries; paraphrased wording is expected."""


def _build_user_prompt(notes: list[str], hours: list[HourEntry], battery: Battery) -> str:
    tariffs = ", ".join(f"{h.hour}:{h.tariff_bdt_per_kwh:g}" for h in hours)
    note_block = "\n".join(f"[{i}] {note.strip()}" for i, note in enumerate(notes))
    return (
        f"Battery: capacity {battery.capacity_kwh:g} kWh, "
        f"starting energy {battery.initial_energy_kwh:g} kWh, "
        f"base minimum reserve {battery.minimum_energy_kwh:g} kWh, "
        f"max charge {battery.max_charge_kwh_per_hour:g} kWh/h, "
        f"max discharge {battery.max_discharge_kwh_per_hour:g} kWh/h.\n"
        f"Hourly tariff (hour:BDT/kWh): {tariffs}\n\n"
        f"Operator notes ({len(notes)}):\n{note_block}\n\n"
        f"Return exactly {len(notes)} interpretations, note_index 0 to {len(notes) - 1}."
    )


class OperatorNoteInterpreter:
    """Thin async wrapper around the provider call."""

    def __init__(self) -> None:
        self.model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        self._api_key = os.getenv("OPENAI_API_KEY")
        timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "12"))
        retries = int(os.getenv("OPENAI_MAX_RETRIES", "2"))
        self._client: AsyncOpenAI | None = (
            AsyncOpenAI(api_key=self._api_key, timeout=timeout, max_retries=retries)
            if self._api_key
            else None
        )

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def interpret(
        self,
        notes: list[str],
        hours: list[HourEntry],
        battery: Battery,
    ) -> list[dict[str, Any]]:
        """Return raw (still untrusted) interpretation candidates from the model."""
        if self._client is None:
            raise InterpreterUnavailable("OPENAI_API_KEY is not configured")

        try:
            completion = await self._client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_prompt(notes, hours, battery)},
                ],
                response_format={"type": "json_schema", "json_schema": DIRECTIVE_SCHEMA},
            )
        except Exception as exc:  # provider error, timeout, quota, network
            raise InterpreterUnavailable(f"model provider call failed: {type(exc).__name__}") from exc

        content = completion.choices[0].message.content if completion.choices else None
        if not content:
            raise InterpreterUnavailable("model returned an empty response")

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise InterpreterUnavailable("model returned non-JSON content") from exc

        items = payload.get("interpretations")
        if not isinstance(items, list):
            raise InterpreterUnavailable("model response missing the interpretations array")
        return items
