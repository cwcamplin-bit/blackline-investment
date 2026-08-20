# Blackline Backend — Property Analysis API

A working backend for Blackline's core "paste a Rightmove URL, get an
investment analysis" flow (Phase 1 of the product roadmap in the Business
Plan). It powers `analyse.html` from the front-end prototype directly —
no changes to that file's data contract were needed, only its data
*source* (a live API call instead of the hardcoded `PROPERTIES` mock).

## What it does

`POST /api/analyze { "url": "<rightmove listing url>" }` runs a five-stage
pipeline and returns one JSON object matching the front end's report shape
exactly:

1. **Extract** — fetches the Rightmove page and parses its embedded
   listing data (price, address, beds, EPC, tenure, description, ...).
2. **Estimate rent & comparables** — best-effort live lookups, with a
   documented fallback model so the pipeline never hard-fails (see
   "Data sourcing" below — this is the part most likely to need attention
   in production).
3. **Financial engine** — Stamp Duty Land Tax, deposit, interest-only BTL
   mortgage payment, net yield, cash-on-cash ROI. Pure arithmetic, no
   external dependency, unit-tested against known-correct figures.
4. **Score** — four axis scores (growth / value-add / security / cashflow,
   0-100), an overall verdict, a confidence rating, and BTL/BRRR/Flip
   strategy scores. Rules-based and fully traceable to specific inputs —
   not a black box.
5. **Narrative** — turns the scored data into the per-dimension clauses,
   strengths/risks, and executive summary the UI displays. Template-based
   by default; optionally polished by OpenAI if `OPENAI_API_KEY` is set
   (the model is only allowed to rephrase, never introduce new facts, and
   any failure/timeout falls back to the deterministic text untouched).

## Quick start

```bash
cd blackline-backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then either `curl` it:

```bash
curl -X POST http://localhost:8000/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"url": "rightmove.co.uk/properties/154829201"}'
```

...or open `analyse.html` from the front-end prototype in a browser — it
already points at `http://localhost:8000` by default (override with
`window.BLACKLINE_API_BASE` before the page's script runs, e.g. from a
build-time config, if you deploy the API elsewhere).

Run the test suite (stdlib `unittest`, no extra install needed beyond
`requirements.txt`):

```bash
python -m unittest discover -s app/tests -v
```

21 tests cover the SDLT/mortgage math, the scoring engine's edge cases
(missing comparables, negative cashflow, all-scores-in-bounds), and the
full HTTP endpoint (happy path + three error paths), using a saved
Rightmove HTML fixture so they run offline and deterministically.

## How this was verified

This was built and tested in a network-restricted environment that
cannot reach rightmove.co.uk (or any external site) directly. Everything
except the actual outbound HTTP call to Rightmove was verified for real:
the parser, financial engine, scoring, narrative, the Starlette app, and
a full browser run of the *actual* `analyse.html` file calling the
*actual* running server (Chromium via Playwright, screenshot available on
request) — all with a saved Rightmove HTML fixture standing in for the
live fetch. The one thing that could not be verified end-to-end here is
Rightmove's actual current page structure and anti-bot behaviour — see
"Data sourcing" below. Before relying on this in production, run it
against a handful of real Rightmove URLs from a normal network and check
`extraction_method` in the response (`"page_model"` = rich data extracted
cleanly; `"jsonld"` = fell back to the thinner SEO-tag path — see
`data_quality.fieldsMissing` for what was missing).

## Data sourcing — what's solid vs. best-effort

| Component | Approach | Reliability |
|---|---|---|
| Listing data (price, beds, address, EPC...) | Parses Rightmove's embedded `window.PAGE_MODEL` JSON, falling back to JSON-LD/OpenGraph tags | Fairly solid — this JSON blob is how most Rightmove tooling extracts data, but it's Rightmove's internal structure, not a public contract, and can change without notice |
| Financial calculations (SDLT, mortgage, yield, ROI) | Pure UK tax/finance formulas | Solid — deterministic, unit-tested. SDLT bands do change at Budgets though (see `app/engine/financial.py`); the current bands are dated as being valid from April 2025 and should be checked against gov.uk periodically |
| Estimated rent | Best-effort live search of Rightmove's to-let listings; falls back to a regional-yield model, clearly flagged in the response | Weakest link. Rightmove actively rate-limits/blocks non-browser traffic — expect this to fall back to the modelled estimate often until you either add proxy/browser-automation infrastructure or switch to a licensed rental AVM |
| Comparable sales | Best-effort scrape of Rightmove's sold-price pages | Same caveat as above. **Deliberately returns an empty list rather than inventing comparables** if the live lookup fails — showing fabricated sold prices as evidence would be actively misleading for an investment decision |

**Recommended next step for production reliability:** replace the
best-effort Rightmove scraping in `app/engine/comparables.py` with either
(a) a licensed property-data API, or (b) HM Land Registry's free, public
**Price Paid Data** for sold-price comparables (genuinely reliable, no
scraping/blocking risk, updated monthly) paired with a proper rental AVM
for the rent estimate. The module already has a clean
`fetch_rent_estimate()` / `fetch_comparable_sales()` interface designed
to be swapped out without touching the rest of the pipeline.

**Rightmove's Terms of Service** restrict automated scraping. This is
fine for a Phase 0/1 prototype but worth resolving properly (a data
partnership, or licensing sold/rental data from a provider) before
scaling usage — flagging this now rather than after it becomes a legal
problem.

## Deploying permanently (no local server needed)

Running `uvicorn` on your own machine every time is fine for development,
but for actual day-to-day use you want this hosted somewhere with a
permanent URL, so `analyse.html` always has something to talk to. This
repo is ready to deploy as-is (it includes a `Dockerfile` and a
`render.yaml` blueprint for [Render](https://render.com), a host with a
genuinely free web-service tier — see their [free tier docs](https://render.com/docs/free)
for current limits; Render may ask for card verification at signup, and
some community reports suggest it isn't always charged, but check current
terms yourself before entering card details). Any other Docker-friendly
host (Railway, Fly.io, a plain VPS) works too, using the same Dockerfile.

One-time setup (roughly 10 minutes):

1. **Get the code onto GitHub.** If you don't already have a GitHub
   account, create one at github.com (free). Create a new repository
   (e.g. `blackline-backend`), then use *Add file → Upload files* in the
   repo's web UI to drag in everything from the unzipped
   `blackline-backend` folder — no command line needed.
2. **Sign up at Render.com** and choose *New → Blueprint*, then connect
   the GitHub repo you just created. Render will read `render.yaml` and
   `Dockerfile` automatically and offer to deploy the free plan.
3. **Deploy.** Once it finishes building (a few minutes), Render gives you
   a permanent URL like `https://blackline-backend-xxxx.onrender.com`.
4. **Point the front end at it** — update the `API_BASE` constant near
   the top of `analyse.html`'s `<script>` block to that URL. From then on,
   opening `analyse.html` just works, with nothing to start manually.

**One caveat with Render's free tier specifically:** it spins the service
down after 15 minutes with no traffic, and the next request wakes it back
up — which takes 30-60 seconds. The first analysis after a quiet period
will just look slow rather than broken; every request after that is fast
until it goes idle again. If that's a problem, a paid "always on" instance
(or a different host) removes the cold start entirely.

## Configuration

Everything has a working default (see `app/config.py` /
`.env.example`) — nothing is required to run. Overridable via environment
variables or a `.env` file: mortgage rate/LTV/term assumptions, opex
percentages, `OPENAI_API_KEY` for narrative polishing, request timeouts.

## Disclaimer

Stamp Duty and financial figures here are decision-support estimates, not
tax or financial advice — the SDLT bands and BTL mortgage assumptions
should be confirmed with a solicitor/broker before a customer acts on
them. Worth surfacing this in the product UI, not just here.

## Project layout

```
app/
  main.py              # HTTP layer (Starlette routes, error handling, CORS)
  pipeline.py          # orchestrates the 5 stages end-to-end
  schemas.py            # Pydantic models — the exact analyse.html contract
  config.py              # settings, all overridable via env vars
  extractors/
    rightmove.py          # URL validation + page parsing
  engine/
    financial.py            # SDLT, mortgage, yield, ROI
    comparables.py            # rent estimate + comparable sales
    scoring.py                  # 4-axis scores, verdict, confidence, strategy scores
    narrative.py                  # clauses, strengths/risks, executive summary
  tests/                           # unittest suite + saved HTML fixture
```

## What's deliberately out of scope for this pass

Per the Business Plan's roadmap, this covers Phase 1's "Analyse" flow
only: auth, billing, the saved-properties/portfolio pages, and the
Discover/Optimise pillars are follow-on work. `analyse.html`'s Save
button now stores full real results to `localStorage` under
`blackline_saved_properties`, ready for `saved.html`/`portfolio.html` to
read from — those pages themselves haven't been wired up yet.
