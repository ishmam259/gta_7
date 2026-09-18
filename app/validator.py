"""Independent replay of a finished schedule (Problem Statement section 11).

This mirrors what the judge does: take the scenario plus the ground-truth
directives, walk the returned `hourly_plan` hour by hour, and report every rule it
breaks. Used by the test suite against the public sample pack, and as an optional
self-check before the service answers.
"""

from __future__ import annotations

from .guardrails import Directive
from .optimizer import build_effective_scenario
from .schemas import Battery, HourEntry, HourPlan

TOLERANCE = 0.01


def replay(
    hours: list[HourEntry],
    battery: Battery,
    directives: list[Directive],
    plan: list[HourPlan],
    totals: tuple[float, float, float] | None = None,
) -> list[str]:
    """Return a list of violation messages; empty means the plan is valid."""
    problems: list[str] = []

    if len(plan) != 24 or sorted(p.hour for p in plan) != list(range(24)):
        return ["hourly_plan must contain exactly one entry for each hour 0..23"]

    scenario = build_effective_scenario(hours, battery, directives)
    ordered = sorted(plan, key=lambda p: p.hour)
    energy = battery.initial_energy_kwh

    for entry in ordered:
        h = entry.hour
        demand = hours[h].demand_kwh

        for name, value in (
            ("grid_kwh", entry.grid_kwh),
            ("solar_used_kwh", entry.solar_used_kwh),
            ("battery_kwh", entry.battery_kwh),
            ("battery_energy_after_kwh", entry.battery_energy_after_kwh),
        ):
            if value != value or value in (float("inf"), float("-inf")):
                problems.append(f"hour {h}: {name} is not a finite number")
            elif value < -TOLERANCE:
                problems.append(f"hour {h}: {name} is negative ({value})")

        if entry.solar_used_kwh > scenario.effective_solar[h] + TOLERANCE:
            problems.append(
                f"hour {h}: solar_used_kwh {entry.solar_used_kwh} exceeds effective solar "
                f"{scenario.effective_solar[h]}"
            )

        if entry.battery_action == "idle" and abs(entry.battery_kwh) > TOLERANCE:
            problems.append(f"hour {h}: idle action must have battery_kwh = 0")

        charge = entry.battery_kwh if entry.battery_action == "charge" else 0.0
        discharge = entry.battery_kwh if entry.battery_action == "discharge" else 0.0

        if charge > battery.max_charge_kwh_per_hour + TOLERANCE:
            problems.append(f"hour {h}: charge {charge} exceeds max_charge_kwh_per_hour")
        if discharge > battery.max_discharge_kwh_per_hour + TOLERANCE:
            problems.append(f"hour {h}: discharge {discharge} exceeds max_discharge_kwh_per_hour")
        if charge > TOLERANCE and h in scenario.no_charge:
            problems.append(f"hour {h}: charging during a no_charge_window")
        if discharge > TOLERANCE and h in scenario.no_discharge:
            problems.append(f"hour {h}: discharging during a no_discharge_window")
        if h in scenario.grid_cap and entry.grid_kwh > scenario.grid_cap[h] + TOLERANCE:
            problems.append(
                f"hour {h}: grid_kwh {entry.grid_kwh} exceeds max_grid_kwh {scenario.grid_cap[h]}"
            )

        balance = entry.grid_kwh + entry.solar_used_kwh + discharge - (demand + charge)
        if abs(balance) > TOLERANCE:
            problems.append(f"hour {h}: energy balance off by {balance:.4f} kWh")

        energy = energy + charge - discharge
        if abs(entry.battery_energy_after_kwh - energy) > TOLERANCE:
            problems.append(
                f"hour {h}: battery_energy_after_kwh {entry.battery_energy_after_kwh} does not match "
                f"the replayed state {energy:.4f}"
            )
        energy = entry.battery_energy_after_kwh

        if energy > battery.capacity_kwh + TOLERANCE:
            problems.append(f"hour {h}: battery energy {energy} exceeds capacity")
        if energy < scenario.reserve[h] - TOLERANCE:
            problems.append(
                f"hour {h}: battery energy {energy} below required reserve {scenario.reserve[h]}"
            )

    if abs(ordered[-1].battery_energy_after_kwh - battery.initial_energy_kwh) > TOLERANCE:
        problems.append(
            f"end-of-day battery energy {ordered[-1].battery_energy_after_kwh} does not return to "
            f"the initial {battery.initial_energy_kwh}"
        )

    if totals is not None:
        total_grid, total_cost, peak_grid = totals
        expected_grid = sum(p.grid_kwh for p in ordered)
        expected_cost = sum(p.grid_kwh * hours[p.hour].tariff_bdt_per_kwh for p in ordered)
        expected_peak = max(p.grid_kwh for p in ordered)
        if abs(total_grid - expected_grid) > TOLERANCE:
            problems.append(f"total_grid_kwh {total_grid} disagrees with hourly_plan {expected_grid}")
        if abs(total_cost - expected_cost) > TOLERANCE:
            problems.append(f"total_cost_bdt {total_cost} disagrees with hourly_plan {expected_cost}")
        if abs(peak_grid - expected_peak) > TOLERANCE:
            problems.append(f"peak_grid_kwh {peak_grid} disagrees with hourly_plan {expected_peak}")

    return problems
