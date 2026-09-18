"""Request/response models for the GridWise preliminary API.

Field names and shapes follow the canonical Problem Statement (sections 07 and 10).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

BatteryAction = Literal["charge", "discharge", "idle"]


# --------------------------------------------------------------------------- #
# Request                                                                      #
# --------------------------------------------------------------------------- #


class HourEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: Annotated[int, Field(ge=0, le=23)]
    demand_kwh: Annotated[float, Field(ge=0)]
    solar_kwh: Annotated[float, Field(ge=0)]
    tariff_bdt_per_kwh: float


class Battery(BaseModel):
    model_config = ConfigDict(extra="ignore")

    capacity_kwh: Annotated[float, Field(ge=0)]
    initial_energy_kwh: Annotated[float, Field(ge=0)]
    minimum_energy_kwh: Annotated[float, Field(ge=0)]
    max_charge_kwh_per_hour: Annotated[float, Field(ge=0)]
    max_discharge_kwh_per_hour: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def _check_consistency(self) -> "Battery":
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        return self


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenario_id: str
    operator_notes: Annotated[list[str], Field(min_length=1, max_length=3)]
    hours: Annotated[list[HourEntry], Field(min_length=24, max_length=24)]
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, notes: list[str]) -> list[str]:
        if any(not note or not note.strip() for note in notes):
            raise ValueError("operator_notes entries must be non-empty strings")
        return notes

    @field_validator("hours")
    @classmethod
    def _hours_cover_full_day(cls, hours: list[HourEntry]) -> list[HourEntry]:
        seen = sorted(h.hour for h in hours)
        if seen != list(range(24)):
            raise ValueError("hours must contain exactly one entry for each hour 0..23")
        return hours

    def hours_in_order(self) -> list[HourEntry]:
        return sorted(self.hours, key=lambda h: h.hour)


# --------------------------------------------------------------------------- #
# Response                                                                     #
# --------------------------------------------------------------------------- #


class DirectiveInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: dict[str, Any] | None
    explanation: str


class HourPlan(BaseModel):
    hour: int
    grid_kwh: float
    solar_used_kwh: float
    battery_action: BatteryAction
    battery_kwh: float
    battery_energy_after_kwh: float


class OptimizeResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourPlan]
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
