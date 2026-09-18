# GridWise — LLM-Assisted Campus Energy Optimizer

BUP CSE Fest 2026 Hackathon · Online Preliminary · Smart Campus Energy Optimization Challenge

A single HTTP service that reads free-text campus-operator notes with a language model,
validates the extracted directives deterministically, applies them to a 24-hour energy
scheduling problem, and returns a provably valid minimum-cost plan.

```
operator notes ──▶ LLM interpretation ──▶ deterministic guardrails ──▶ LP optimizer ──▶ replay self-check ──▶ response
   (free text)      OpenAI, structured      repair / reject / no_op     exact CBC solve    judge-equivalent,
                    JSON schema output                                                     and it can veto
```

**Deployed endpoint** — `https://gridwise-api-gta-7.onrender.com`

```bash
curl -s https://gridwise-api-gta-7.onrender.com/health
# {"status":"ok"}
```

No login, VPN or manual approval is needed to reach it.

**Current measured state**

| | |
|---|---|
| Public pack, live | 18/18 interpretation · 10/10 valid · cost ratio **1.0000** |
| Paraphrase corpus, live | **80/81** across 18 phrasing families |
| Offline test suite | **233 passing** |
| Latency | p95 ≈ 3 s, ≈ 2 s under 10-way concurrency |

---

## Contents

- [Quickstart](#quickstart) · [Running the checks](#running-the-checks)
- [The problem in one page](#the-problem-in-one-page)
- [Architecture](#architecture) — the five stages and what each guarantees
- [Design decisions worth knowing](#design-decisions-worth-knowing) — and the bugs behind them
- [Testing](#testing) · [Configuration](#configuration) · [Deployment](#deployment)
- [API contract](#api-contract) · [Reliability](#reliability-and-safe-failure)
- [Repository layout](#repository-layout) · [Working on this](#working-on-this)
- [Known limitations](#known-limitations) · [Security](#security)

---

## Quickstart

Requires Python 3.11+ and network access to the model provider.

```bash
git clone https://github.com/ishmam259/gta_7.git && cd gta_7
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then edit .env and set OPENAI_API_KEY
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The service is ready in about two seconds; `GET /health` answers well inside the 60-second
readiness requirement.

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}

curl -s -X POST http://127.0.0.1:8000/optimize-energy \
  -H 'Content-Type: application/json' \
  -d @samples/example_request.json | head -40
```

## Running the checks

**Offline tests** — no API key, no network; the provider is stubbed.

```bash
pytest -q        # 233 passed
```

**The public pack, against a running service** — posts all ten published cases, compares
each interpretation against the organizer's ground truth, replays every returned schedule
hour by hour, and reports the cost-quality ratio and p95 latency.

```bash
python scripts/smoke_test.py                  # against http://127.0.0.1:8000
python scripts/smoke_test.py https://your-deployed-host
```

```
Interpretation exact-match : 18/18
Valid schedules            : 10/10
Mean cost-quality ratio    : 1.0000  -> 10.00/10
All public cases passed.
```

**Interpretation on wording the public pack never shows** — needs an API key.

```bash
python scripts/paraphrase_bench.py                 # every family
python scripts/paraphrase_bench.py factor_lost     # one family
```

Ten published cases are not a test set: hidden notes restate the same six directives in
different words. `tests/paraphrase_cases.json` holds 81 notes across 18 phrasing families,
scored **per family** — because "90%" is not actionable, while "every note whose window
ends in *until* is off by one hour" tells you exactly what to fix.

---

## The problem in one page

Each request is a synthetic 24-hour campus energy scenario — hourly demand, solar and
tariff, plus a battery — and **1–3 free-text operator notes**. The service must do two
separable things, scored separately:

1. **Interpret the notes with an LLM** into exactly one structured entry per note, in
   `note_index` order, drawn from a closed vocabulary of six directive types.
2. **Apply those directives and minimise grid cost**, returning a 24-hour plan the judge
   independently replays.

The six directive types, and what each does to the maths:

| Directive | Effect |
|---|---|
| `solar_reduction` | `effective_solar[h] = solar[h] × factor` for the listed hours |
| `minimum_battery_reserve` | raises the floor on `battery_energy_after_kwh` for those hours |
| `no_charge_window` | charge = 0 in those hours |
| `no_discharge_window` | discharge = 0 in those hours |
| `max_grid_window` | `grid_kwh ≤ max_grid_kwh` in those hours |
| `no_op` | the note changes nothing |

**Two conventions decide most of the score:**

- Windows are whole hours, **start inclusive, end exclusive**. "1 PM to 3 PM" → `[13, 14]`.
- `factor` is the fraction of solar that **remains**. "An 80% reduction" → `0.2`.

And the rules every returned plan must satisfy, every hour:

```
grid_kwh + solar_used_kwh + discharge  =  demand_kwh + charge
0 ≤ solar_used_kwh ≤ effective_solar
reserve ≤ battery_energy_after_kwh ≤ capacity
charge ≤ max_charge_per_hour,  discharge ≤ max_discharge_per_hour
battery_energy_after_kwh[23] = initial_energy_kwh        (end-of-day neutrality)
```

Correct extraction with wrong application scores nothing, and a cheap plan that breaks a
directive scores nothing. Validity first, then cost.

---

## Architecture

Five stages. Each one has a single job and a guarantee it upholds, so a failure in one is
contained by the next.

### 1. LLM interpretation — `app/interpreter.py`

The language model is the **required** interpretation path (hard-coded phrase matching as
the sole interpreter is explicitly non-compliant). It receives the operator notes, the
battery parameters, and the hourly **tariff and solar** profiles, and returns one
structured candidate per note.

Structured Outputs (`response_format` → `json_schema`, `strict: true`) constrain the model
to the six-directive vocabulary *at decode time*, which removes a whole class of malformed
output before validation runs.

**The model never expands a time window itself.** It reports the two clock times the note
*names* — `start_hour` and `end_hour` on a 24-hour clock — and deterministic code applies
the half-open rule. Notes that name individual hours, or cover the whole day, use the
`hours` array instead. See [Design decisions](#1-the-model-reads-clocks-code-does-arithmetic)
for why.

The solar profile is sent for a specific reason: it lets the model resolve bare clock
numbers. "Panel washing from one until three" is `[13, 14]`, because there is no sun at
01:00.

### 2. Deterministic guardrails — `app/guardrails.py`

Model output is **untrusted data**. Nothing reaches the optimizer until it has passed:

* exactly one entry per note, in `note_index` order `0..N-1`; missing → `no_op`, duplicates dropped (first wins)
* only the six supported directive types survive; a non-string or unknown type → `no_op`
* `start_hour`/`end_hour` expanded by `expand_window()` — start inclusive, end exclusive,
  `end_hour = 24` meaning the close of the day, and a range ending at or before its start
  wrapping through midnight: `22 → 6` is `[0, 1, 2, 3, 4, 5, 22, 23]`, ascending as the
  specification requires
* the expanded window is **combined** with any explicitly listed `hours`, never chosen between
* hours coerced to unique integers `0..23`, ascending; a window directive with no valid hours → `no_op`
* `factor` must land in `[0, 1]`; a percentage-shaped answer (`25`) is repaired to `0.25`,
  anything else → `no_op`
* reserve must be finite and non-negative, and is clamped to battery capacity
* `max_grid_kwh` must be finite and non-negative (zero is a real limit, not a missing value)
* `applies = false` is emitted **only** for `no_op`; every real directive uses `applies = true`

The guardrails never invent a constraint. Anything unrepairable degrades to `no_op`.

### 3. Optimizer — `app/optimizer.py`

Cost minimisation is a **linear program**, solved exactly with CBC via PuLP rather than
greedily. Five variables per hour: `grid`, `solar_used`, `charge`, `discharge`, `energy_after`,
subject to the rules listed above plus the directive constraints, minimising
`Σ grid[h] × tariff[h]`.

Charge and discharge are then netted into the single `battery_action` the response schema
allows — energy-neutral, since the balance equation only ever sees `charge − discharge`.
`grid_kwh` is recomputed from the balance equation after rounding, and the three totals are
derived from `hourly_plan` itself, so they cannot disagree with it.

**On the public pack this reaches the organizer's optimal cost on all ten cases.**

### 4. Replay self-check — `app/validator.py`

Before answering, the service replays its own schedule the way the judge does — every
directive, the energy balance, battery bounds and rate limits, effective-solar ceilings,
end-of-day neutrality, and the three totals. **A plan that breaks any of them is never
returned.**

### 5. Recovery — `_solve_best_effort()` in `app/main.py`

If the interpreted directives cannot all be satisfied at once, the service does not ship
the schedule anyway. It searches for the largest subset of directives that yields a
genuinely valid plan, prefers the cheapest, and names what it dropped in `plan_summary`.

The reasoning: a real scenario is *guaranteed* feasible under its ground truth, so an
infeasible set means we misread a note. Dropping a directive we invented is recoverable;
returning a schedule that visibly breaks one never is. The reported
`directive_interpretation` is untouched either way, so a directive we could not apply is
still reported exactly as it was read — interpretation credit survives even when
application fails.

Normal requests cost one solve. The subset search only runs on failure, and with at most
three directives that is at most eight solves at ~30 ms each.

---

## Design decisions worth knowing

These are the choices a teammate is most likely to want to change, and the evidence for
why they are the way they are.

### 1. The model reads clocks, code does arithmetic

Originally the model produced the `hours` array itself. Live testing found a systematic
boundary bug in **3 of 18** notes: "noon until 2 PM" came back as `[12, 13, 14]` (end
included), and "6 PM until 10 PM" as `[18, 19, 20]` — the latter induced by the prompt's
own worked example `"6 PM until 9 PM" -> [18, 19, 20]`, which invited pattern-matching on
the `"6 PM until ..."` prefix.

Two of those three misses were worse than a lost mark: the plan looked *cheaper* than the
reference because it optimised against a window one hour too short, then failed the judge's
replay on the battery reserve at hour 21. **A cheaper-looking answer that is actually
illegal.**

Moving the expansion into `expand_window()` eliminated the entire error class. Models read
two clock times reliably and enumerate ranges unreliably.

### 2. An LP, not a greedy heuristic

The obvious approach is "charge when cheap, discharge when expensive". It cannot prove
optimality and it quietly breaks under constraints. The LP makes validity a property of the
model rather than something you hope your loop preserved, and it finds moves a heuristic
never would — on SAMPLE-01 it discharges exactly 40 kWh (not the permitted 50) at hour 1 so
that three consecutive cheap hours refill the battery to exactly capacity, and deliberately
buys expensive energy at hour 13 because the rate limit means there is no other way to be
full for the evening peak.

### 3. The self-check can veto, not just log

It used to log violations and return the plan anyway. A probe with an impossible directive
produced a plan with **24 violations returned as HTTP 200**. Now it drives the recovery in
stage 5.

### 4. The fallback parser is for liveness, not accuracy

`app/fallback_parser.py` runs **only** when the provider call raises, and every activation
is logged. It is not the interpretation path.

It used to score 18/18 on the public pack, which was misleading — it had been shaped around
that exact wording. Independent testing found it would emit *inverted* directives:
"keep grid import at or below 150 kWh" became a battery **reserve** of 150 (wrong type,
opposite direction), and "charge the battery during the scheduled maintenance" became a
**prohibition** on charging. Both now behave. The guiding rule is that its mistakes must be
in the safe direction — failing to recognise a note is survivable, inverting one is not.

### 5. The interpretation step has a hard deadline

Per-call timeout × retries could reach ~36 s on retryable failures, past the judge's 30 s
limit where a timed-out request counts as a failure. `INTERPRETER_DEADLINE_SECONDS` wraps
the whole step in `asyncio.wait_for`, so a hanging provider hands over to the emergency
parser with time to spare.

---

## Testing

**233 offline tests**, plus two live scripts. What each file is for:

| File | Covers |
|---|---|
| `test_api.py` | Endpoints, response contract, malformed requests, provider failure |
| `test_public_cases.py` | The ten published scenarios end to end |
| `test_window_expansion.py` | Half-open windows, midnight wrap, `end_hour = 24`, field combination |
| `test_fallback_parser.py` | The emergency parser, including every inversion it once made |
| `test_recovery.py` | What happens when directives cannot all be honoured |
| `test_guardrail_fuzz.py` | Malformed model output of every shape — wrong types, duplicates, junk hours, out-of-range figures |
| `test_edge_cases.py` | Degenerate batteries, flat/zero/negative tariffs, the request surface |

The suite is regression-tested, not just green: removing the fallback's unit-masking breaks
3 tests, and flipping the window rule from exclusive to inclusive breaks 8.

**What the tests deliberately do not cover:** interpretation *quality*. The provider is
stubbed everywhere in `pytest`, so the suite verifies plumbing, not comprehension. That is
what `scripts/paraphrase_bench.py` is for, and it needs a real key.

---

## Configuration

All configuration is by environment variable. **No secret values are committed to this
repository, and none are baked into the Docker image.**

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENAI_API_KEY` | **yes** | — | Credential for the operator-note interpreter |
| `OPENAI_MODEL` | no | `gpt-5.1` | Interpretation model |
| `OPENAI_TIMEOUT_SECONDS` | no | `8` | Per-call provider timeout |
| `OPENAI_MAX_RETRIES` | no | `1` | Provider retry budget |
| `INTERPRETER_DEADLINE_SECONDS` | no | `15` | Hard ceiling on the whole interpretation step |
| `PORT` | no | `8000` | Listen port |
| `LOG_LEVEL` | no | `INFO` | Log verbosity |

**Provider / model:** OpenAI, `gpt-5.1` by default, called through the official
`openai` Python SDK with Structured Outputs. Deterministic decoding (`temperature=0`)
is requested where the model allows it; reasoning-class models reject any non-default
temperature, so the call retries once without it rather than losing the model — see
`_create_with_temperature_fallback`.

`gpt-5.1` scores **81/81** on the paraphrase corpus. `gpt-4.1` also scores 81/81 and is
faster (p95 2.2s against 3.5s end to end), so either is a defensible choice and the switch
is one environment variable. We kept `gpt-5.1` because a saturated corpus cannot rank the
two, and the stronger model is the better bet against unseen phrasings.

---

## Deployment

```bash
docker pull ishmam259/gridwise-api:v1
docker run --rm -p 8000:8000 \
  -e OPENAI_API_KEY=sk-... \
  -e OPENAI_MODEL=gpt-5.1 \
  ishmam259/gridwise-api:v1
curl -s http://127.0.0.1:8000/health
# {"status":"ok"}
```

Build locally:

```bash
docker build -t gridwise-api:local .
docker run --rm -p 8000:8000 \
  -e OPENAI_API_KEY=sk-... \
  -e OPENAI_MODEL=gpt-5.1 \
  gridwise-api:local
```

The image exposes port 8000, binds `0.0.0.0`, contains no credentials, and fails its own
build if the CBC solver binary is not present. It is 330 MB and was verified end to end:
`/health` ready, all ten public cases valid at cost ratio 1.0000, `PORT` override honoured,
and a deliberately invalid key confirmed to degrade to `200` rather than erroring.

The judged deployment is `https://gridwise-api-gta-7.onrender.com`, built from this
repository via `render.yaml` with `autoDeploy: false`, so the evaluated build stays fixed
during the window. `OPENAI_API_KEY` is set in the Render dashboard and never committed.

`render.yaml` is a free-tier blueprint. Because that tier sleeps after 15 minutes and needs
30–60 s to wake — a cold start inside a 30 s judging limit is a failed request —
`.github/workflows/keep-warm.yml` pings it every 5 minutes, with `scripts/keep_warm.py` as
a local equivalent. Scheduled GitHub runs can be delayed under load, so treat the workflow
as a backup to a real uptime monitor rather than the only defence.

---

## API contract

### `GET /health`

```json
{"status": "ok"}
```

### `POST /optimize-energy`

Request and response follow the Problem Statement exactly. Abbreviated response:

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
      "explanation": "Solar availability is reduced to 25% during the panel-cleaning window."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note does not affect today's 24-hour energy schedule."
    }
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 90.0, "solar_used_kwh": 0.0,
     "battery_action": "idle", "battery_kwh": 0.0, "battery_energy_after_kwh": 110.0}
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "..."
}
```

Status codes: `200` success · `400` malformed or structurally invalid · `500` controlled internal error.

---

## Reliability and safe failure

| Situation | Behaviour |
|---|---|
| Malformed JSON / structurally invalid request | `400` with a trimmed, stringified detail — never the raw body |
| Model returns an unsupported or unparseable directive | Guardrails downgrade that note to `no_op`; no invented constraint |
| Provider outage, timeout, or exhausted quota | Logged, then a deterministic emergency parser keeps the service answering `200` |
| Provider hangs or rate-limits | `INTERPRETER_DEADLINE_SECONDS` caps the whole interpretation step, so per-call timeout × retries can never approach the 30 s request limit |
| Directive set cannot all be satisfied | The smallest number of directives is dropped until the schedule is genuinely valid; the omission is logged and stated in `plan_summary` |
| Nothing is schedulable at all | Penalised-slack LP returns a best-effort plan instead of a `500` |
| Provider unusually slow on one request | Measured p95 is around 3 s, but individual calls have been seen to take 13 s under provider load. That is inside the interpretation deadline, so the answer is still the model's rather than the emergency parser's; it costs latency score, not correctness |
| Battery starting below its own minimum reserve | Self-contradictory once end-of-day neutrality applies, so no schedule can satisfy both. A best-effort plan is returned with the relaxation stated in `plan_summary`, rather than a `500` or a rejection that would forfeit the case |
| Degenerate scenario — zero capacity, zero charge rate, flat, zero or negative tariffs, solar far above demand | Solved normally; surplus solar is curtailed, never exported |
| A note that tries to instruct the interpreter | Treated as data, not instruction; the note becomes `no_op` |
| Any unhandled error | Controlled `500`; no stack traces, prompts, or configuration in the response |

The LP solve runs in a worker thread, so concurrent hidden cases do not block the event
loop. Twenty concurrent requests were measured at p95 ≈ 3 s with every plan still optimal
and no fallback activations.

---

## Repository layout

```
app/
  main.py             FastAPI app, endpoints, error handling, recovery search
  interpreter.py      LLM operator-note interpretation (OpenAI Structured Outputs)
  guardrails.py       Deterministic validation of untrusted model output
  optimizer.py        Exact LP schedule (PuLP + CBC)
  validator.py        Judge-equivalent replay of a finished schedule
  fallback_parser.py  Emergency-only parser for provider outages
  schemas.py          Request/response models
scripts/
  smoke_test.py       Public-pack runner and scorer
  paraphrase_bench.py Interpretation scored per phrasing family (needs a key)
  keep_warm.py        Keeps a free-tier host from sleeping between judge calls
samples/              Organizer public sample cases
tests/                233 offline tests + the paraphrase corpus
render.yaml           Free-tier deployment blueprint
Dockerfile            Fallback image; fails its build without a working CBC solver
```

## Working on this

**If you change the prompt or the schema**, run `scripts/paraphrase_bench.py` before and
after. It is the only check that measures comprehension, and prompt edits regress in
surprising ways — one rule added here ("a note must state a figure") silently broke every
`no_charge_window`, because those carry no figure at all. Per-family scores make that
visible in one line.

**If you change `guardrails.py` or `optimizer.py`**, `pytest -q` is the gate. The optimizer
is the one component verified optimal against the organizer's own numbers on all ten public
cases — treat changes there as high-risk and re-run `scripts/smoke_test.py` against a live
service afterwards.

**If you add a phrasing family**, put it in `tests/paraphrase_cases.json` with an expected
directive and a family name. The benchmark picks it up automatically.

**Batching note:** `paraphrase_bench.py` interleaves families deliberately. Sending three
paraphrases of the same directive in one request makes the model mark the repeats as
`no_op` — reasonably, since they add nothing — which is an artefact of batching, not a
fault in interpretation.

## Dependencies

| Package | Role |
|---|---|
| [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/) | HTTP service |
| [Pydantic](https://docs.pydantic.dev/) | Request/response schema enforcement |
| [PuLP](https://coin-or.github.io/pulp/) + CBC | Exact linear-programming solve |
| [openai](https://github.com/openai/openai-python) | Model provider SDK |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | Local `.env` loading (optional) |
| [pytest](https://docs.pytest.org/), [httpx](https://www.python-httpx.org/) | Tests and smoke runner |

Sample scenario data in `samples/public_cases.json` is the organizer-published public pack.
Development used an AI coding assistant; the architecture, guardrail design, LP formulation
and validation strategy are the team's own.

## Known limitations

* A note combining two distinct rules yields one directive, per the one-directive-per-note contract.
* A note naming several non-contiguous hours ("charging is blocked in hours 9, 13 and 17")
  is the weakest case in the corpus — the model tends to read the first hours as a range.
  Contiguous windows, which is how every published case is worded, are unaffected.
* The word "through" ("6 PM through 8 PM") reads as inclusive in ordinary English, while
  every range in the Problem Statement is half-open. The prompt asks for the half-open
  reading and the model returns the inclusive one; the statement never uses the word, so
  neither reading is asserted anywhere.
* A `solar_reduction` note that states a window but no percentage yields `factor = 1.0`,
  which is mathematically inert but still reported as an applied directive.
* Interpretation quality is bounded by the provider model; `OPENAI_MODEL` can be raised if
  hidden paraphrases prove harder than the corpus.
* The emergency parser exists for liveness, not accuracy. It handles the phrasings in the
  public pack, whole-day wording, windows that wrap past midnight, and figures stated either
  way round ("a 20% drop" and "drops to 20%"), and it will not read a quantity such as
  `20 kWh` as a clock hour. It still has no grasp of durations ("for the next three hours"),
  named windows ("the evening peak"), spelled-out numerals ("six PM"), or fractions outside
  its small table ("three quarters"), and it maps a note to at most one directive. It prefers
  `no_op` to a guessed constraint, which is the direction that fails safely.
* Battery round-trip efficiency is not modelled, matching the Problem Statement's rules.

## Security

No API keys, tokens, `.env` files or secrets are committed; `.gitignore` excludes them.
Secrets are never logged or returned in responses, and the Docker image contains none.
Operator-note text is treated as data throughout: a note that tries to issue instructions
to the interpreter is interpreted as a note, never obeyed. Only the synthetic scenario data
supplied by the harness is used.
