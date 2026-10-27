import logging
import random
import time
from datetime import date, datetime, timedelta, timezone

import requests
import psycopg

from src.config import LOOKBACK_DAYS, SERIES, START_DATE, require_env

log = logging.getLogger(__name__)

BANXICO_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/{ids}/datos/{start}/{end}"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
RETRYABLE = {429, 500, 502, 503, 504}

UPSERT_BRONZE = """
    INSERT INTO bronze.raw_observation (source, series_id, obs_date_raw, value_raw, run_id)
    SELECT %s, %s, t.obs_date_raw, t.value_raw, %s
    FROM unnest(%s::text[], %s::text[]) AS t(obs_date_raw, value_raw)
    ON CONFLICT (source, series_id, obs_date_raw) DO UPDATE
    SET value_raw = EXCLUDED.value_raw, run_id = EXCLUDED.run_id, ingested_at = now()
    WHERE bronze.raw_observation.value_raw IS DISTINCT FROM EXCLUDED.value_raw
    RETURNING 1
"""


def get_json(session, url, *, params=None, headers=None, max_retries=5):
    # Never log the URL or the exception text: FRED sends api_key in the query string.
    last = "no attempt made"
    for attempt in range(max_retries):
        wait = 2 ** attempt + random.random()
        try:
            r = session.get(url, params=params, headers=headers, timeout=(5, 30))
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = type(exc).__name__
        else:
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    raise RuntimeError("HTTP 200 but the body is not JSON") from None
            last = f"HTTP {r.status_code}"
            if r.status_code not in RETRYABLE:
                raise RuntimeError(f"Non-retryable error: {last}")
            try:
                wait = float(r.headers["Retry-After"])
            except (KeyError, ValueError):
                pass
        log.warning("attempt %d/%d failed (%s), waiting %.1fs", attempt + 1, max_retries, last, wait)
        time.sleep(wait)
    raise RuntimeError(f"Failed after {max_retries} attempts: {last}")


def fetch_banxico(session, token, series_id, start, end):
    url = BANXICO_URL.format(ids=series_id, start=start, end=end)
    payload = get_json(session, url, headers={"Bmx-Token": token, "Accept": "application/json"})
    return [(d["fecha"], d["dato"]) for s in payload["bmx"]["series"] for d in s.get("datos", [])]


def fetch_fred(session, api_key, series_id, start, end):
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json",
              "observation_start": start, "observation_end": end}
    payload = get_json(session, FRED_URL, params=params)
    return [(o["date"], o["value"]) for o in payload["observations"]]


def start_date(conn, series_id):
    with conn.cursor() as cur:
        cur.execute("SELECT max(obs_date) FROM silver.observation WHERE series_id = %s", (series_id,))
        last = cur.fetchone()[0]
    if last is None:
        return START_DATE
    return (last - timedelta(days=LOOKBACK_DAYS)).isoformat()


def upsert_bronze(conn, run_id, source, series_id, rows):
    # One statement per series: a row-by-row loop costs a round trip per row, which is slow on a remote database.
    # RETURNING gives a row only for inserts and changed values, so counting them gives the changes.
    if not rows:
        return 0
    dates = [d for d, _ in rows]
    values = [v for _, v in rows]
    with conn.cursor() as cur:
        changed = cur.execute(UPSERT_BRONZE, (source, series_id, run_id, dates, values)).fetchall()
    return len(changed)


def extract(conn, run_id, today=None):
    token = require_env("BANXICO_TOKEN")
    api_key = require_env("FRED_API_KEY")
    end = (today or date.today()).isoformat()
    with requests.Session() as session:
        for s in SERIES:
            start = start_date(conn, s["id"])
            if s["source"] == "banxico":
                rows = fetch_banxico(session, token, s["id"], start, end)
            else:
                rows = fetch_fred(session, api_key, s["id"], start, end)
            changed = upsert_bronze(conn, run_id, s["source"], s["id"], rows)
            conn.commit()
            log.info("%s: %d fetched, %d changed (window %s to %s)", s["id"], len(rows), changed, start, end)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with psycopg.connect(require_env("DATABASE_URL")) as conn:
        extract(conn, run_id)
