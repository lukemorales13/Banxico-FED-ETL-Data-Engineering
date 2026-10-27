# Banxico-FED-ETL-Data-Engineering

An automated ETL pipeline that loads the Banxico and Federal Reserve policy rates into Postgres, computes their
differential, and shows how it relates to the USD/MXN exchange rate at different lags in a Streamlit dashboard.

**Question:** does the gap between Mexico's target rate and the US effective federal funds rate help explain
monthly moves in the peso–dollar FIX, and at what lag?

The methodology and the reasons for each source are in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

## Architecture

```mermaid
flowchart LR
    B[Banxico SIE API] --> E[src/extract.py]
    F[FRED API] --> E
    E -- upsert on natural key --> BR[(bronze.raw_observation)]
    BR --> T[src/transform.py]
    T --> S[(silver.observation)]
    T --> Q[(silver.quarantine)]
    T --> DQ[(silver.dq_result)]
    S --> G[src/gold.py]
    G --> GO[(gold.rates_daily, rates_monthly, lag_correlation)]
    GO --> APP[streamlit_app.py]
    DQ --> APP
    Q --> APP
```

| Layer | Contents | How it is rebuilt |
|---|---|---|
| bronze | Values exactly as received (text) | Incremental: each run fetches a window and upserts |
| silver | Typed, validated observations, plus quarantine and data-quality results | Rebuilt from bronze in one transaction |
| gold | Daily join on FIX dates, monthly aggregates, lag correlations | Rebuilt from silver in one transaction |

Bronze is the only layer with state. Its `run_id` and `ingested_at` record when a value last changed, not when
it was last seen. The extract reads the last date in silver and fetches from 30 days earlier, so a late
publication is picked up on the next run.

## Series

| ID | Source | What it is | Role |
|---|---|---|---|
| `SF43718` | Banxico SIE | USD/MXN FIX, banking business days | Dependent variable |
| `SF61745` | Banxico SIE | Banxico target rate, percent | Mexican side of the differential |
| `DFF` | FRED (Federal Reserve) | Effective federal funds rate, percent | US side of the differential |

## Requirements

- Python 3.13 (the version used in CI). Local development was done on 3.12.
- Docker Desktop, for the local Postgres.
- A Banxico SIE token and a FRED API key, both free.
- Optional: a cloud Postgres (for example Neon), if you want the daily workflow to run.

Python dependencies are pinned in [requirements.txt](requirements.txt).

## Environment variables

Copy [.env.example](.env.example) to `.env` and fill it in. Secrets are read from the environment only, and `.env`
is not committed.

| Variable | Used by | Example |
|---|---|---|
| `BANXICO_TOKEN` | extract | 64-character token from Banxico |
| `FRED_API_KEY` | extract | 32-character key from FRED |
| `DATABASE_URL` | pipeline, dashboard | `postgresql://postgres:postgres@localhost:5432/banxico_fred_db` |

## Reproduce locally

From the repository root:

```bash
# 1. Python dependencies
python -m venv .venv
.venv\Scripts\activate            # Windows (on macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

# 2. Secrets
copy .env.example .env            # then edit .env with your values

# 3. Postgres 16 in Docker (database banxico_fred_db, data kept in a named volume)
docker compose up -d
docker compose ps                 # wait until the db service is running

# 4. Pipeline: applies sql/01_schema.sql, then extract, transform and gold
python run_pipeline.py

# 5. Dashboard
python -m streamlit run streamlit_app.py
```

Running step 4 again is safe. The schema uses `IF NOT EXISTS`, the bronze load is an upsert, and silver and
gold are rebuilt. On a run with no new data, bronze keeps the same rows and the same `run_id`.

The steps can also be run one at a time: `python -m src.extract`, `python -m src.transform`, `python -m src.gold`.

## Daily automation

[.github/workflows/daily.yml](.github/workflows/daily.yml) runs `run_pipeline.py` every day at 12:00 UTC and
can be started by hand from the Actions tab. It needs three repository secrets: `DATABASE_URL` (a cloud Postgres,
since GitHub cannot reach `localhost`), `BANXICO_TOKEN` and `FRED_API_KEY`.

## Data quality

Each transform run writes a row to `silver.dq_result` for every check and series:

- **Counts:** `bad_date`, `null_value`, `out_of_range`, `future_date`. Failing rows go to `silver.quarantine` with
  the first rule they failed. They do not stop the run.
- **Freshness:** days since the last observation of each series. Threshold: 7 days.
- **Join coverage:** share of FIX dates that have a value for each rate. Threshold: 99%.

The dashboard's *Data quality* tab shows the last five runs, the checks of the latest run, the quarantine and
the coverage of silver.

## Results

Monthly, complete months only (2020-01 to 2026-09, 81 months). `fix_pct_change` is the percentage change of the
monthly FIX average. Correlation between the rate differential lagged *k* months and that change:

| Lag (months) | Correlation | Months |
|---|---|---|
| 0 | 0.160 | 80 |
| 1 | 0.086 | 80 |
| 2 | 0.059 | 79 |
| 3 | 0.024 | 78 |
| 4 | −0.025 | 77 |
| 5 | −0.034 | 76 |
| 6 | −0.028 | 75 |

With about 80 months, the approximate 95% bound is ±0.22. No lag is outside it, so this sample does not show a
relationship between the differential and monthly FIX changes at any lag from 0 to 6. The sample is short, the
differential is a target against an effective rate, and monthly FIX changes depend on many factors that this
model does not include. See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) section 8.

## Repository layout

```
run_pipeline.py          Runs the schema, then extract, transform and gold
src/config.py            Series, dates, thresholds and the require_env helper
src/extract.py           Banxico and FRED clients (retries, backoff) and the bronze upsert
src/transform.py         Parsing, validation, quarantine and dq_result
src/gold.py              Daily join, monthly aggregates and lag correlations
sql/01_schema.sql        DDL for the bronze, silver and gold schemas
streamlit_app.py         Dashboard: analysis and data quality
docker-compose.yml       Local Postgres 16
.github/workflows/       Daily run on GitHub Actions
docs/DATA_SOURCES.md     Sources, design decisions and probe results
```
