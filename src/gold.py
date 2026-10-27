import logging
from datetime import date

import pandas as pd
import psycopg

from src.config import require_env

log = logging.getLogger(__name__)

FIX_ID = "SF43718"
TARGET_ID = "SF61745"
FED_ID = "DFF"
MAX_LAG = 6


def clean(value):
    return None if pd.isna(value) else float(value)


def load_wide(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT series_id, obs_date, value FROM silver.observation")
        df = pd.DataFrame(cur.fetchall(), columns=["series_id", "obs_date", "value"])
    df["value"] = df["value"].astype(float)
    df["obs_date"] = pd.to_datetime(df["obs_date"])
    return df.pivot(index="obs_date", columns="series_id", values="value")


def build_daily(wide):
    # Dates are the FIX business days. A missing rate stays NaN: filling it would invent a value.
    fix_dates = wide.index[wide[FIX_ID].notna()]
    rates = wide.reindex(fix_dates)
    daily = pd.DataFrame({
        "usd_mxn_fix": rates[FIX_ID],
        "banxico_target": rates[TARGET_ID],
        "fed_funds": rates[FED_ID],
    })
    daily["rate_diff_pp"] = daily["banxico_target"] - daily["fed_funds"]
    return daily


def build_monthly(daily, today):
    by_month = daily.groupby(daily.index.to_period("M"))
    monthly = pd.DataFrame({
        "usd_mxn_fix_avg": by_month["usd_mxn_fix"].mean(),
        "banxico_target_avg": by_month["banxico_target"].mean(),
        "fed_funds_avg": by_month["fed_funds"].mean(),
        "rate_diff_avg_pp": by_month["rate_diff_pp"].mean(),
        "fix_days": by_month.size(),
    })
    monthly["fix_pct_change"] = monthly["usd_mxn_fix_avg"].pct_change() * 100
    # The current month is still open, so its average is not final.
    monthly["is_complete"] = monthly.index < pd.Timestamp(today).to_period("M")
    monthly.index = monthly.index.to_timestamp()
    return monthly


def lag_correlations(monthly):
    # The rate difference at month t-k is paired with the FIX change at month t.
    complete = monthly[monthly["is_complete"]]
    rows = []
    for k in range(MAX_LAG + 1):
        pairs = pd.DataFrame({
            "rate": complete["rate_diff_avg_pp"].shift(k),
            "fix": complete["fix_pct_change"],
        }).dropna()
        r = pairs["rate"].corr(pairs["fix"])
        rows.append((k, None if pd.isna(r) else round(float(r), 6), len(pairs)))
    return rows


def write_gold(conn, daily, monthly, lags):
    daily_rows = [
        (d.date(), clean(r.usd_mxn_fix), clean(r.banxico_target), clean(r.fed_funds), clean(r.rate_diff_pp))
        for d, r in zip(daily.index, daily.itertuples(index=False))
    ]
    monthly_rows = [
        (m.date(), clean(r.usd_mxn_fix_avg), clean(r.banxico_target_avg), clean(r.fed_funds_avg),
         clean(r.rate_diff_avg_pp), clean(r.fix_pct_change), int(r.fix_days), bool(r.is_complete))
        for m, r in zip(monthly.index, monthly.itertuples(index=False))
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM gold.rates_daily")
        cur.executemany(
            "INSERT INTO gold.rates_daily (obs_date, usd_mxn_fix, banxico_target, fed_funds, rate_diff_pp) "
            "VALUES (%s, %s, %s, %s, %s)", daily_rows)
        cur.execute("DELETE FROM gold.rates_monthly")
        cur.executemany(
            "INSERT INTO gold.rates_monthly (month_start, usd_mxn_fix_avg, banxico_target_avg, fed_funds_avg, "
            "rate_diff_avg_pp, fix_pct_change, fix_days, is_complete) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            monthly_rows)
        cur.execute("DELETE FROM gold.lag_correlation")
        cur.executemany(
            "INSERT INTO gold.lag_correlation (lag_months, correlation, n_months) VALUES (%s, %s, %s)", lags)


def build(conn, today=None):
    today = today or date.today()
    daily = build_daily(load_wide(conn))
    monthly = build_monthly(daily, today)
    lags = lag_correlations(monthly)
    write_gold(conn, daily, monthly, lags)
    log.info("gold: %d daily rows, %d monthly rows", len(daily), len(monthly))
    for k, r, n in lags:
        log.info("lag %d months: r=%s over %d months", k, r, n)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with psycopg.connect(require_env("DATABASE_URL")) as conn:
        build(conn)
