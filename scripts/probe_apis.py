"""Probe Banxico SIE and FRED: fetch the series, inspect them, print basic data-quality indicators.

Run from the repo root:
    python scripts/probe_apis.py                          # 2020-01-01 -> today
    python scripts/probe_apis.py --start 2015-01-01 --save

Secrets come from the environment or a .env file:
    BANXICO_TOKEN=<64-character token>
    FRED_API_KEY=<32-character key>
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

BANXICO_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/{ids}/datos/{start}/{end}"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
OUT_DIR = Path("data/probe")
RETRYABLE = {429, 500, 502, 503, 504}

# lo/hi: wide sanity bounds, not statistical limits.
# jump: day-over-day move worth a look, ("pct", share) or ("abs", percentage points).
SERIES = [
    {"id": "SF43718", "name": "usd_mxn_fix", "lo": 5.0, "hi": 40.0, "jump": ("pct", 0.03)},
    {"id": "SF61745", "name": "banxico_target_rate", "lo": 0.0, "hi": 25.0, "jump": ("abs", 1.0)},
    {"id": "DFF", "name": "fed_funds_effective", "lo": 0.0, "hi": 25.0, "jump": ("abs", 1.0)},
]
# Checks where any non-zero count is a problem
PROBLEM_COUNTS = ["null_values", "bad_dates", "duplicate_dates", "future_dates", "out_of_range"]


def get_json(session, url, *, params=None, headers=None, max_retries=5):
    """GET with timeouts; retries 429/5xx/timeouts with exponential backoff and jitter.

    Other 4xx errors fail fast. Error messages never include the URL, because
    FRED's api_key travels in the query string.
    """
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
            last = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code not in RETRYABLE:
                raise RuntimeError(f"Non-retryable error. {last}")
            try:
                wait = float(r.headers["Retry-After"])
            except (KeyError, ValueError):
                pass
        print(f"  attempt {attempt + 1}/{max_retries} failed ({last[:60]}); waiting {wait:.1f}s")
        time.sleep(wait)
    raise RuntimeError(f"Failed after {max_retries} attempts. Last error: {last}")


def fetch_banxico(session, token, ids, start, end):
    url = BANXICO_URL.format(ids=",".join(ids), start=start, end=end)
    return get_json(session, url, headers={"Bmx-Token": token, "Accept": "application/json"})


def fetch_fred(session, api_key, series_id, start, end):
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json",
              "observation_start": start, "observation_end": end}
    return get_json(session, FRED_URL, params=params)


def to_frame(rows, date_format):
    """rows: iterable of (series_id, raw_date, raw_value) -> typed DataFrame.

    Anything that fails to parse becomes NaT/NaN and is counted in the quality checks.
    """
    df = pd.DataFrame(rows, columns=["series_id", "obs_date", "value"])
    df["obs_date"] = pd.to_datetime(df["obs_date"], format=date_format, errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df


def parse_banxico(payload):
    rows = [(s["idSerie"], d["fecha"], d["dato"])
            for s in payload["bmx"]["series"] for d in s.get("datos", [])]
    return to_frame(rows, "%d/%m/%Y")


def parse_fred(payload, series_id):
    rows = [(series_id, o["date"], o["value"]) for o in payload["observations"]]
    return to_frame(rows, "%Y-%m-%d")


def quality(df, spec, today):
    d = df[df["series_id"] == spec["id"]]
    if d.empty:
        raise RuntimeError(f"No rows returned for {spec['id']}")
    dates, vals = d["obs_date"].dropna(), d["value"].dropna()
    s = d.dropna().drop_duplicates("obs_date").sort_values("obs_date").set_index("obs_date")["value"]
    mode, threshold = spec["jump"]
    move = s.pct_change().abs() if mode == "pct" else s.diff().abs()
    return {
        "rows": len(d),
        "date_min": str(dates.min().date()),
        "date_max": str(dates.max().date()),
        "days_since_last_obs": (pd.Timestamp(today) - dates.max()).days,
        "null_values": int(d["value"].isna().sum()),
        "bad_dates": int(d["obs_date"].isna().sum()),
        "duplicate_dates": int(d.duplicated("obs_date").sum()),
        "future_dates": int((dates > pd.Timestamp(today)).sum()),
        "value_min": float(vals.min()),
        "value_max": float(vals.max()),
        "value_mean": round(float(vals.mean()), 4),
        "out_of_range": int(((vals < spec["lo"]) | (vals > spec["hi"])).sum()),
        "max_gap_days": int(s.index.to_series().diff().dt.days.max()),
        "weekend_obs_pct": round(100 * float((s.index.day_of_week >= 5).mean()), 1),
        "value_changes": int((s.diff().abs() > 1e-9).sum()),
        "big_jumps": int((move > threshold).sum()),
    }


def flags(rep):
    found = [f"{k}={rep[k]}" for k in PROBLEM_COUNTS if rep[k]]
    if rep["days_since_last_obs"] > 7:
        found.append(f"stale ({rep['days_since_last_obs']}d since last obs)")
    return found


def join_report(df):
    wide = df.pivot_table(index="obs_date", columns="series_id", values="value", aggfunc="last").sort_index()
    ids = [s["id"] for s in SERIES]
    fix_dates = wide["SF43718"].dropna().index
    print("\n== Join coverage on the calendar date ==")
    print(f"FIX dates: {len(fix_dates)}")
    for c in ids[1:]:
        missing = fix_dates[wide.loc[fix_dates, c].isna()]
        print(f"  {c}: present on {1 - len(missing) / len(fix_dates):.1%} of FIX dates; "
              f"missing: {[str(m.date()) for m in missing[:5]] or 'none'}")
    all_present = wide.dropna(subset=ids)
    print(f"Dates with all three series: {len(all_present)} (first: {all_present.index.min().date()})")
    return wide


def read_secret(name, expected_len):
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing {name}. Put it in .env (see .env.example) or export it.")
    note = "" if len(value) == expected_len else f" (warning: length {len(value)}, expected {expected_len})"
    print(f"{name} loaded{note}")
    return value


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2020-01-01", help="YYYY-MM-DD")
    ap.add_argument("--end", default=date.today().isoformat(), help="YYYY-MM-DD")
    ap.add_argument("--save", action="store_true", help=f"write raw JSON and CSVs to {OUT_DIR}/")
    args = ap.parse_args()

    load_dotenv()
    token = read_secret("BANXICO_TOKEN", 64)
    fred_key = read_secret("FRED_API_KEY", 32)
    today = date.fromisoformat(args.end)

    session = requests.Session()
    print(f"\nFetching {args.start} -> {args.end} ...")
    banxico_payload = fetch_banxico(session, token, ["SF43718", "SF61745"], args.start, args.end)
    fred_payload = fetch_fred(session, fred_key, "DFF", args.start, args.end)
    df = pd.concat([parse_banxico(banxico_payload), parse_fred(fred_payload, "DFF")], ignore_index=True)

    pd.set_option("display.width", 160)
    print("\n== Shape and dtypes ==")
    print(df.shape)
    print(df.dtypes.to_string())

    print("\n== Head / tail per series ==")
    for spec in SERIES:
        d = df[df["series_id"] == spec["id"]].sort_values("obs_date")
        print(f"\n{spec['id']} ({spec['name']}): {len(d)} rows")
        print(pd.concat([d.head(3), d.tail(3)]).to_string(index=False))

    print("\n== Data-quality indicators ==")
    reports = {spec["id"]: quality(df, spec, today) for spec in SERIES}
    print(pd.DataFrame(reports).to_string())
    print("\n== Flags ==")
    for sid, rep in reports.items():
        print(f"  {sid}: {'; '.join(flags(rep)) or 'OK'}")

    wide = join_report(df)

    if args.save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "raw_banxico.json").write_text(json.dumps(banxico_payload, ensure_ascii=False))
        (OUT_DIR / "raw_fred_DFF.json").write_text(json.dumps(fred_payload, ensure_ascii=False))
        df.to_csv(OUT_DIR / "probe_long.csv", index=False)
        wide.to_csv(OUT_DIR / "probe_wide.csv")
        pd.DataFrame(reports).to_csv(OUT_DIR / "quality_report.csv")
        print(f"\nSaved raw JSON and CSVs to {OUT_DIR}/ (keep data/ in .gitignore)")


if __name__ == "__main__":
    main()
