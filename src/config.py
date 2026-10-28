import os

from dotenv import load_dotenv

load_dotenv()

START_DATE = "2020-01-01"
LOOKBACK_DAYS = 7

FRESHNESS_DAYS = 7
JOIN_MIN_SHARE = 0.99

SERIES = [
    {"id": "SF43718", "source": "banxico", "name": "usd_mxn_fix", "lo": 5, "hi": 40},
    {"id": "SF61745", "source": "banxico", "name": "banxico_target_rate", "lo": 0, "hi": 25},
    {"id": "DFF", "source": "fred", "name": "fed_funds_effective", "lo": 0, "hi": 25},
]


def require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing {name}. Set it in .env (see .env.example) or in the environment.")
    return value
