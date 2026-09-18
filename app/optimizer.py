"""Exact 24-hour cost minimisation (Problem Statement sections 05 and 09).

The scheduling problem is a linear program: a linear objective
(sum of grid_kwh * tariff) over linear energy-balance, battery-state and
directive constraints. We solve it exactly with CBC via PuLP, so a valid
scenario yields the organizer-optimal cost rather than a heuristic near-miss.
"""

from __future__ import annotations

from dataclasses import dataclass

import pulp

from .guardrails import Directive
from .schemas import Battery, HourEntry, HourPlan

ROUND = 6
_EPS = 1e-9


def _solver():
    """CBC, tolerant of the PuLP 3.x -> 4.x solver-class rename."""
    for name in ("PULP_CBC_CMD", "COIN_CMD"):
        factory = getattr(pulp, name, None)
        if factory is None:
            continue
        try:
            solver = factory(msg=0)
        except Exception:  # solver binary not present under this name
            continue
        if solver.available():
            return solver
    return None


@dataclass
class EffectiveScenario:
    """The scenario after every validated directive has been folded into the math."""

    effective_solar: list[float]
    reserve: list[float]
    no_charge: set[int]
    no_discharge: set[int]
    grid_cap: dict[int, float]


def build_effective_scenario(
    hours: list[HourEntry],
    battery: Battery,
    directives: list[Directive],
) -> EffectiveScenario:
    """Apply directives deterministically (Problem Statement section 5.3)."""
    effective_solar = [h.solar_kwh for h in hours]
    reserve = [battery.minimum_energy_kwh] * 24
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    grid_cap: dict[int, float] = {}

    for directive in directives:
        if directive.directive_type == "solar_reduction":
            for hour in directive.hours:
                effective_solar[hour] *= directive.factor or 0.0
        elif directive.directive_type == "minimum_battery_reserve":
            for hour in directive.hours:
                reserve[hour] = max(reserve[hour], directive.minimum_energy_kwh or 0.0)
        elif directive.directive_type == "no_charge_window":
            no_charge.update(directive.hours)
        elif directive.directive_type == "no_discharge_window":
            no_discharge.update(directive.hours)
        elif directive.directive_type == "max_grid_window":
            cap = directive.max_grid_kwh or 0.0
            for hour in directive.hours:
                grid_cap[hour] = min(grid_cap.get(hour, cap), cap)

    # A reserve above capacity is already clamped by the guardrails, but keep the
    # model self-consistent in case capacity itself is the tighter bound.
    reserve = [min(r, battery.capacity_kwh) for r in reserve]
    return EffectiveScenario(effective_solar, reserve, no_charge, no_discharge, grid_cap)


def _solve(
    hours: list[HourEntry],
    battery: Battery,
    scenario: EffectiveScenario,
    *,
    relaxed: bool,
) -> tuple[list[float], list[float], list[float]] | None:
    """Solve the LP. Returns (charge, discharge, solar_used) or None if infeasible.

    `relaxed` adds heavily penalised slack so that a pathological scenario still
    produces a response instead of a 500 (Problem Statement: safe failure).
    """
    problem = pulp.LpProblem("gridwise", pulp.LpMinimize)
    rng = range(24)

    grid = [pulp.LpVariable(f"g{h}", lowBound=0) for h in rng]
    solar = [pulp.LpVariable(f"s{h}", lowBound=0, upBound=scenario.effective_solar[h]) for h in rng]
    charge = [
        pulp.LpVariable(
            f"c{h}",
            lowBound=0,
            upBound=0.0 if h in scenario.no_charge else battery.max_charge_kwh_per_hour,
        )
        for h in rng
    ]
    discharge = [
        pulp.LpVariable(
            f"d{h}",
            lowBound=0,
            upBound=0.0 if h in scenario.no_discharge else battery.max_discharge_kwh_per_hour,
        )
        for h in rng
    ]
    energy = [pulp.LpVariable(f"e{h}", lowBound=0, upBound=battery.capacity_kwh) for h in rng]

    penalties: list[pulp.LpVariable] = []

    def slack(name: str):
        if not relaxed:
            return 0.0
        var = pulp.LpVariable(name, lowBound=0)
        penalties.append(var)
        return var

    for h in rng:
        # Energy balance: grid + solar + discharge = demand + charge
        problem += (
            grid[h] + solar[h] + discharge[h] + slack(f"unmet{h}")
            == hours[h].demand_kwh + charge[h]
        ), f"balance{h}"

        previous = battery.initial_energy_kwh if h == 0 else energy[h - 1]
        problem += energy[h] == previous + charge[h] - discharge[h], f"state{h}"
        problem += energy[h] + slack(f"res{h}") >= scenario.reserve[h], f"reserve{h}"

        if h in scenario.grid_cap:
            problem += grid[h] <= scenario.grid_cap[h] + slack(f"cap{h}"), f"gridcap{h}"

    # End-of-day neutrality.
    neutrality_slack = slack("neutral")
    problem += energy[23] <= battery.initial_energy_kwh + neutrality_slack, "neutral_upper"
    problem += energy[23] >= battery.initial_energy_kwh - neutrality_slack, "neutral_lower"

    cost = pulp.lpSum(grid[h] * hours[h].tariff_bdt_per_kwh for h in rng)
    problem += cost + 1e6 * pulp.lpSum(penalties) if penalties else cost

    solver = _solver()
    problem.solve(solver) if solver is not None else problem.solve()
    if pulp.LpStatus[problem.status] != "Optimal":
        return None

    value = pulp.value
    return (
        [float(value(charge[h]) or 0.0) for h in rng],
        [float(value(discharge[h]) or 0.0) for h in rng],
        [float(value(solar[h]) or 0.0) for h in rng],
    )


def _net_flows(
    raw_charge: list[float],
    raw_discharge: list[float],
    battery: Battery,
    scenario: EffectiveScenario,
) -> list[float]:
    """Collapse charge/discharge into one signed action per hour.

    The response schema allows exactly one battery action per hour. Netting is
    energy-neutral: the balance equation only ever sees `charge - discharge`.
    """
    nets: list[float] = []
    for h in range(24):
        net = round(raw_charge[h] - raw_discharge[h], ROUND)
        if net > 0:
            net = 0.0 if h in scenario.no_charge else min(net, battery.max_charge_kwh_per_hour)
        elif net < 0:
            net = 0.0 if h in scenario.no_discharge else max(net, -battery.max_discharge_kwh_per_hour)
        nets.append(net)

    # Squeeze out floating-point drift so end-of-day energy lands exactly on the
    # initial level rather than a fraction of a kWh away from it.
    drift = round(sum(nets), ROUND)
    if abs(drift) > _EPS:
        for h in sorted(range(24), key=lambda i: -abs(nets[i])):
            candidate = round(nets[h] - drift, ROUND)
            if candidate > 0 and (h in scenario.no_charge or candidate > battery.max_charge_kwh_per_hour):
                continue
            if candidate < 0 and (
                h in scenario.no_discharge or candidate < -battery.max_discharge_kwh_per_hour
            ):
                continue
            nets[h] = candidate
            break
    return nets


def build_plan(
    hours: list[HourEntry],
    battery: Battery,
    directives: list[Directive],
) -> tuple[list[HourPlan], EffectiveScenario, bool]:
    """Return (hourly_plan, effective scenario, solved_strictly)."""
    scenario = build_effective_scenario(hours, battery, directives)

    solution = _solve(hours, battery, scenario, relaxed=False)
    strict = solution is not None
    if solution is None:
        solution = _solve(hours, battery, scenario, relaxed=True)
    if solution is None:
        raise RuntimeError("no feasible schedule could be produced")

    raw_charge, raw_discharge, raw_solar = solution
    nets = _net_flows(raw_charge, raw_discharge, battery, scenario)

    plan: list[HourPlan] = []
    energy = battery.initial_energy_kwh
    for h in range(24):
        net = nets[h]
        solar_used = min(max(raw_solar[h], 0.0), scenario.effective_solar[h])

        # Recompute grid from the balance equation so reported numbers are exact.
        needed = hours[h].demand_kwh + net
        if solar_used > needed:
            solar_used = max(needed, 0.0)  # surplus solar is curtailed
        solar_used = round(solar_used, ROUND)
        grid_kwh = round(needed - solar_used, ROUND)
        if grid_kwh < 0:
            solar_used = round(solar_used + grid_kwh, ROUND)
            grid_kwh = 0.0

        energy = round(energy + net, ROUND)
        if net > 0:
            action, magnitude = "charge", net
        elif net < 0:
            action, magnitude = "discharge", -net
        else:
            action, magnitude = "idle", 0.0

        plan.append(
            HourPlan(
                hour=h,
                grid_kwh=grid_kwh,
                solar_used_kwh=solar_used,
                battery_action=action,
                battery_kwh=round(magnitude, ROUND),
                battery_energy_after_kwh=energy,
            )
        )

    return plan, scenario, strict


def summarize(plan: list[HourPlan], hours: list[HourEntry]) -> tuple[float, float, float]:
    """Totals recomputed from the plan itself, so they can never disagree with it."""
    total_grid = round(sum(p.grid_kwh for p in plan), ROUND)
    total_cost = round(sum(p.grid_kwh * hours[p.hour].tariff_bdt_per_kwh for p in plan), ROUND)
    peak_grid = round(max((p.grid_kwh for p in plan), default=0.0), ROUND)
    return total_grid, total_cost, peak_grid
