"""GridWise preliminary service.

Pipeline:  operator notes -> LLM interpretation -> deterministic guardrails
           -> LP optimizer -> replayed 24-hour schedule.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

try:  # optional convenience for local runs; never required in production
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

from . import fallback_parser
from .guardrails import normalize_interpretations
from .interpreter import InterpreterUnavailable, OperatorNoteInterpreter
from .optimizer import build_plan, summarize
from .schemas import OptimizeRequest, OptimizeResponse
from .validator import replay

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("gridwise")

interpreter = OperatorNoteInterpreter()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not interpreter.configured:
        logger.warning(
            "OPENAI_API_KEY is not set: operator-note interpretation will use the "
            "emergency deterministic fallback only."
        )
    else:
        logger.info("Operator-note interpreter ready (model=%s)", interpreter.model)
    yield


app = FastAPI(
    title="GridWise LLM-Assisted Energy Optimizer",
    version="1.0.0",
    description="BUP CSE Fest 2026 preliminary - operator-note interpretation and 24-hour scheduling.",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Structurally invalid requests answer 400, per the Problem Statement.

    Pydantic error objects can carry the raw request body (bytes) in `input`, which
    is neither JSON-serialisable nor safe to echo back, so we emit a trimmed,
    stringified view instead.
    """
    detail = [
        {
            "loc": ".".join(str(part) for part in error.get("loc", ())),
            "msg": str(error.get("msg", "invalid value")),
            "type": str(error.get("type", "value_error")),
        }
        for error in exc.errors()[:10]
    ]
    return JSONResponse(status_code=400, content={"error": "invalid_request", "detail": detail})


@app.exception_handler(Exception)
async def _unhandled_error(_: Request, exc: Exception) -> JSONResponse:
    """Controlled 500: never leak stack traces, prompts or configuration."""
    logger.exception("unhandled error: %s", type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": "The request could not be completed."},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _plan_summary(entries, strict: bool, used_fallback: bool) -> str:
    applied = [e.directive_type for e in entries if e.applies]
    if applied:
        detail = "applied " + ", ".join(sorted(set(applied)))
    else:
        detail = "no operator directive affected today's schedule"
    source = "deterministic fallback interpretation" if used_fallback else "LLM interpretation"
    feasibility = "" if strict else " Constraints were relaxed to return a best-effort plan."
    return (
        f"Charged the battery in cheap hours and discharged it into the expensive evening peak; "
        f"{detail} from the {source}, and solar was used before grid import.{feasibility}"
    )


@app.post("/optimize-energy", response_model=OptimizeResponse)
async def optimize_energy(payload: OptimizeRequest) -> OptimizeResponse:
    hours = payload.hours_in_order()
    notes = payload.operator_notes

    used_fallback = False
    try:
        raw_items = await interpreter.interpret(notes, hours, payload.battery)
    except InterpreterUnavailable as exc:
        # Safe failure: degrade rather than 500. Logged so it is visible in operations.
        logger.warning("interpreter unavailable (%s); using deterministic fallback", exc)
        used_fallback = True
        raw_items = fallback_parser.parse_notes(notes, payload.battery.capacity_kwh)

    entries, directives = normalize_interpretations(raw_items, len(notes), payload.battery)

    plan, _scenario, strict = await run_in_threadpool(
        build_plan, hours, payload.battery, directives
    )
    total_grid, total_cost, peak_grid = summarize(plan, hours)

    violations = replay(hours, payload.battery, directives, plan, (total_grid, total_cost, peak_grid))
    if violations:
        logger.error("self-check found %d violation(s): %s", len(violations), violations[:5])

    return OptimizeResponse(
        scenario_id=payload.scenario_id,
        directive_interpretation=entries,
        hourly_plan=plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
        plan_summary=_plan_summary(entries, strict, used_fallback),
    )
