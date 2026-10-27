-- Data model for the Banxico-Fed vs USD/MXN pipeline.
-- Safe to run repeatedly (IF NOT EXISTS / upserts).
--
-- bronze  as received from the APIs, text only. The only layer that keeps state between runs.
-- silver  typed, validated, conformed. Rebuilt from bronze on every run.
-- gold    analysis tables read by the dashboard. Rebuilt from silver on every run.

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;

-- bronze

CREATE TABLE IF NOT EXISTS bronze.raw_observation (
    source        TEXT        NOT NULL,
    series_id     TEXT        NOT NULL,
    obs_date_raw  TEXT        NOT NULL,   -- dd/mm/yyyy (Banxico) or yyyy-mm-dd (FRED)
    value_raw     TEXT,                   -- '18.6670', 'N/E', '.', untouched
    run_id        TEXT        NOT NULL,   -- run that last changed value_raw
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, series_id, obs_date_raw)
);
COMMENT ON TABLE bronze.raw_observation IS
    'One row per source, series and observation date, exactly as received. Upserted on the primary key, so re-running a load or re-reading a lookback window never duplicates rows.';

-- silver

CREATE TABLE IF NOT EXISTS silver.dim_series (
    series_id   TEXT PRIMARY KEY,
    source      TEXT NOT NULL CHECK (source IN ('banxico', 'fred')),
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    unit        TEXT NOT NULL,
    frequency   TEXT NOT NULL,
    calendar    TEXT NOT NULL,
    source_url  TEXT NOT NULL
);
COMMENT ON TABLE silver.dim_series IS
    'One row per series. Describes what each series is; see docs/DATA_SOURCES.md for the justification.';

INSERT INTO silver.dim_series (series_id, source, name, description, unit, frequency, calendar, source_url)
VALUES
    ('SF43718', 'banxico', 'usd_mxn_fix',
     'USD/MXN exchange rate for settling USD obligations payable in Mexico (FIX), determination date',
     'MXN per USD', 'daily', 'banking business days',
     'https://www.banxico.org.mx/SieAPIRest/service/v1/series/SF43718'),
    ('SF61745', 'banxico', 'banxico_target_rate',
     'Target set by Banco de Mexico for the overnight interbank funding rate',
     'percent', 'daily', 'all days (observed)',
     'https://www.banxico.org.mx/SieAPIRest/service/v1/series/SF61745'),
    ('DFF', 'fred', 'fed_funds_effective',
     'Effective federal funds rate: weighted average overnight rate between depository institutions',
     'percent', 'daily', 'all days',
     'https://fred.stlouisfed.org/series/DFF')
ON CONFLICT (series_id) DO UPDATE SET
    source = EXCLUDED.source, name = EXCLUDED.name, description = EXCLUDED.description,
    unit = EXCLUDED.unit, frequency = EXCLUDED.frequency, calendar = EXCLUDED.calendar,
    source_url = EXCLUDED.source_url;

CREATE TABLE IF NOT EXISTS silver.observation (
    series_id  TEXT          NOT NULL REFERENCES silver.dim_series (series_id),
    obs_date   DATE          NOT NULL,   -- the common key across both providers
    value      NUMERIC(12,4) NOT NULL,
    loaded_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (series_id, obs_date)
);
COMMENT ON TABLE silver.observation IS
    'One row per series and calendar date. Typed and validated; no filling of missing dates.';

CREATE TABLE IF NOT EXISTS silver.quarantine (
    source        TEXT        NOT NULL,
    series_id     TEXT        NOT NULL,
    obs_date_raw  TEXT        NOT NULL,
    value_raw     TEXT,
    reason        TEXT        NOT NULL,   -- first failed rule: null_value, bad_date, out_of_range, future_date
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, series_id, obs_date_raw)
);
COMMENT ON TABLE silver.quarantine IS
    'Bronze rows that failed a validation rule. They never reach silver, and they do not stop the pipeline.';

CREATE TABLE IF NOT EXISTS silver.dq_result (
    run_id      TEXT        NOT NULL,
    series_id   TEXT        NOT NULL,
    check_name  TEXT        NOT NULL,
    metric      NUMERIC,
    passed      BOOLEAN     NOT NULL,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, series_id, check_name)
);
COMMENT ON TABLE silver.dq_result IS
    'One row per run, series and check: the audit trail of the data-quality validations.';

-- gold

CREATE TABLE IF NOT EXISTS gold.rates_daily (
    obs_date        DATE PRIMARY KEY,
    usd_mxn_fix     NUMERIC(12,4) NOT NULL,
    banxico_target  NUMERIC(12,4),        -- NULL when the source has no value for that date
    fed_funds       NUMERIC(12,4),        -- NULL on the latest dates (publication lag)
    rate_diff_pp    NUMERIC(12,4)        -- banxico_target - fed_funds, percentage points
);
COMMENT ON TABLE gold.rates_daily IS
    'One row per FIX business day. Exact-date join of the three series; no forward fill.';

CREATE TABLE IF NOT EXISTS gold.rates_monthly (
    month_start       DATE PRIMARY KEY,
    usd_mxn_fix_avg   NUMERIC(12,4),
    banxico_target_avg NUMERIC(12,4),
    fed_funds_avg     NUMERIC(12,4),
    rate_diff_avg_pp  NUMERIC(12,4),
    fix_pct_change    NUMERIC(10,6),      -- change of the monthly average vs the previous month
    fix_days          INTEGER NOT NULL,   -- FIX observations used
    is_complete       BOOLEAN NOT NULL    -- FALSE for the current, still-open month
);
COMMENT ON TABLE gold.rates_monthly IS
    'One row per calendar month. Lag analysis uses complete months only.';

CREATE TABLE IF NOT EXISTS gold.lag_correlation (
    lag_months   INTEGER PRIMARY KEY CHECK (lag_months BETWEEN 0 AND 12),
    correlation  NUMERIC(8,6),
    n_months     INTEGER NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE gold.lag_correlation IS
    'Correlation between rate_diff_avg_pp lagged k months and fix_pct_change, over complete months.';
