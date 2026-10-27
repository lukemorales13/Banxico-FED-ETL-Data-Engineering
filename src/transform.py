import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import psycopg

from src.config import FRESHNESS_DAYS, JOIN_MIN_SHARE, SERIES, require_env

log = logging.getLogger(__name__)

DATE_FORMATS = {"banxico": "%d/%m/%Y", "fred": "%Y-%m-%d"}
CHECKS = ["bad_date", "null_value", "out_of_range", "future_date"]
SPEC = {s["id"]: s for s in SERIES}
FIX_ID = "SF43718"


def parse_date(source, raw):
    try:
        return datetime.strptime(raw, DATE_FORMATS[source]).date()
    except ValueError:
        return None


def parse_value(raw):
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError):
        return None
    return value if value.is_finite() else None


def classify(row, today):
    source, series_id, obs_date_raw, value_raw = row
    obs_date = parse_date(source, obs_date_raw)
    if obs_date is None:
        return "bad_date", None, None
    value = parse_value(value_raw)
    if value is None:
        return "null_value", obs_date, None
    spec = SPEC[series_id]
    if not spec["lo"] <= value <= spec["hi"]:
        return "out_of_range", obs_date, value
    if obs_date > today:
        return "future_date", obs_date, value
    return None, obs_date, value


def quality_results(observations, quarantined, today):
    results = []
    for s in SERIES:
        sid = s["id"]
        for check in CHECKS:
            n = sum(1 for q in quarantined if q[1] == sid and q[4] == check)
            results.append((sid, check, n, n == 0))

        dates = [o[1] for o in observations if o[0] == sid]
        if dates:
            age = (today - max(dates)).days
            results.append((sid, "freshness", age, age <= FRESHNESS_DAYS))
        else:
            results.append((sid, "freshness", None, False))

    fix_dates = {o[1] for o in observations if o[0] == FIX_ID}
    for s in SERIES:
        if s["id"] == FIX_ID:
            continue
        rate_dates = {o[1] for o in observations if o[0] == s["id"]}
        share = len(fix_dates & rate_dates) / len(fix_dates) if fix_dates else 0
        results.append((s["id"], "join_coverage", round(share, 6), share >= JOIN_MIN_SHARE))
    return results


def transform(conn, run_id, today=None):
    today = today or date.today()
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT source, series_id, obs_date_raw, value_raw FROM bronze.raw_observation")
        observations, quarantined = [], []
        for row in cur.fetchall():
            reason, obs_date, value = classify(row, today)
            if reason is None:
                observations.append((row[1], obs_date, value))
            else:
                quarantined.append((row[0], row[1], row[2], row[3], reason))

        cur.execute("DELETE FROM silver.observation")
        cur.executemany(
            "INSERT INTO silver.observation (series_id, obs_date, value) VALUES (%s, %s, %s)", observations)
        cur.execute("DELETE FROM silver.quarantine")
        cur.executemany(
            "INSERT INTO silver.quarantine (source, series_id, obs_date_raw, value_raw, reason) "
            "VALUES (%s, %s, %s, %s, %s)", quarantined)

        results = quality_results(observations, quarantined, today)
        cur.executemany(
            "INSERT INTO silver.dq_result (run_id, series_id, check_name, metric, passed) "
            "VALUES (%s, %s, %s, %s, %s)", [(run_id, *r) for r in results])

    log.info("silver: %d observations, %d quarantined", len(observations), len(quarantined))
    for r in results:
        if not r[3]:
            log.warning("check failed: %s %s metric=%s", r[0], r[1], r[2])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with psycopg.connect(require_env("DATABASE_URL")) as conn:
        transform(conn, run_id)
