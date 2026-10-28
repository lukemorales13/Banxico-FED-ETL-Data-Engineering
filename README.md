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
it was last seen. The extract starts 7 days before the last date in silver for each series and fetches up to today,
so a skipped run or a late publication is picked up on the next run.

## Data model

Three schemas: `bronze`, `silver` and `gold`. The DDL is in [sql/01_schema.sql](sql/01_schema.sql). Row counts are
from the run of 2026-10-27.

| Table | Layer | Primary key | Granularity | Purpose | Rows |
|---|---|---|---|---|---|
| `bronze.raw_observation` | bronze | `(source, series_id, obs_date_raw)` | One row per source, series and date, as received | Raw text from the APIs, upserted on each run | 6,685 |
| `silver.observation` | silver | `(series_id, obs_date)` | One row per series and calendar date | Typed, validated values. `obs_date` is the key shared by both sources | 6,685 |
| `silver.quarantine` | silver | `(source, series_id, obs_date_raw)` | One row per bronze row that failed a rule | Rejected rows, with the first rule they failed | 0 |
| `silver.dq_result` | silver | `(run_id, series_id, check_name)` | One row per run, series and check | Audit trail of the data-quality checks | 34 (17 per run, 2 runs) |
| `gold.rates_daily` | gold | `obs_date` | One row per FIX business day | FIX and both rates on the same date. Missing rates stay null | 1,716 |
| `gold.rates_monthly` | gold | `month_start` | One row per calendar month | Monthly averages and percentage change of the FIX. `is_complete` is false for the open month | 82 |
| `gold.lag_correlation` | gold | `lag_months` | One row per lag, 0 to 6 | Pearson correlation of the differential lagged *k* months with the FIX change | 7 |

## Architecture decisions

**Postgres.** I know Postgres better than any other database, and I use it locally more than the others. Databricks was
considered and set aside for delivery time, reproducibility and data volume: this data fits easily in Postgres. The
same database runs locally in Docker and in Neon for the scheduled job. Only `DATABASE_URL` changes. GitHub Actions
cannot reach a local database, so the daily run needs a cloud instance.

**Streamlit.** Power BI and AI/BI were considered. I chose Streamlit for reproducibility: a reviewer runs the app with
one Python command, without desktop software or an account. The dashboard reads the tables the pipeline writes, so the
numbers on screen are the ones in the database, with no CSV export to keep in sync.

**Python, SQL and Streamlit.** The case does not need big-data tools. Python, SQL and Streamlit keep the stack small and
easy to reproduce.

**Bronze, silver and gold.** Bronze keeps the values as the APIs send them and is the only layer that keeps state
between runs. Silver and gold are rebuilt from bronze in one transaction on every run. A change to a validation rule
applies to the full history, and a reprocess needs no new API calls.

**Monthly analysis.** The target rate changes rarely: the probe counted 38 changes between 2020 and 2026. Daily
variation is small, so a daily correlation would carry little information. Gold aggregates by month, and the lag
analysis uses complete months only.

**Quarantine instead of stopping.** A row that fails a rule goes to `silver.quarantine` with the first rule it failed,
and the run continues. The data has been clean so far, but the pipeline does not assume it will stay clean. A malformed
row should not block the daily update, and it stays visible for review.

**Raw text in bronze.** Values are stored as received. The probe found no missing markers between 2020 and 2026, but
the API notes describe `N/E` (Banxico) and `.` (FRED) as markers for missing values. If we converted at extract time,
we would have to decide what a marker means before we know how the transform should handle it. With the raw text, the
transform applies the rule, and quarantine records any row the rule rejects.

**Upsert on the natural key.** Bronze upserts on `(source, series_id, obs_date_raw)`. Re-running the pipeline does not
duplicate rows, and a row is rewritten only when its value changes. This matters more as the number of series and the
window grow. For this project, a full reload would also work.

**Incremental window.** Each run starts 7 days before the last date stored in silver for each series and fetches up to
today. Anything after that point is fetched, so a skipped run is caught up on the next one. The extra 7 days re-read
recent values in case a source revises them. The DFF lag observed in this project was up to four days.

**GitHub Actions for scheduling.** The job runs from the repository, with no server to maintain, and its runs are
listed and can be started by hand. Like the rest of the stack, it is simple and reproducible. Scheduled runs can start
late; the first one ran about six hours after its slot.

**Two providers.** Each rate comes from its own central bank: the FIX and the target rate from Banxico, the effective
federal funds rate from the Fed through FRED. I did not find a daily Mexican policy rate on FRED (the search was not
exhaustive), so FRED alone does not cover the question.

**SQL with psycopg, not an ORM.** I know Postgres and SQL best. The pipeline runs a few fixed queries, and writing them
directly keeps the upsert and the per-layer transactions visible. I did not benchmark against an ORM, so I do not claim
SQL is faster.

**One DDL file, not migrations.** The schema is a single SQL file that creates the tables if they do not exist, and it
runs on every pipeline run. A migration tool keeps a numbered history of schema changes and applies each one once. For
a small, new project one file is enough. The cost showed up when we removed `dim_series`: local and Neon both needed a
manual `DROP`, and a migration would have recorded that change.

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
model does not include. See the limitations in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

## Use of AI

The AI model Haiku 5.5, from Claude Code, was used for the implementation and documentation of this project. It
sped up development and implementation, which left more time for data analysis and for the architecture. Using AI
let me focus on the goals rather than the code. All of it was done under strict supervision, review and control of
the development.

## Dashboard

Analysis tab: monthly USD/MXN FIX and differential on separate axes, and the lag correlations against the 95% bound.

![Analysis](docs/evidence/dashboard_analysis1.png)
![Analysis, details](docs/evidence/dashboard_analysis2.png)

Data quality tab: the last five runs, the checks of the latest run, the quarantine and the coverage of silver.

![Data quality](docs/evidence/dashboard_quality1.png)
![Data quality, details](docs/evidence/dashboard_quality2.png)

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
