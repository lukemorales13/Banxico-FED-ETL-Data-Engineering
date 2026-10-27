import logging
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from src import extract, gold, transform
from src.config import require_env

SCHEMA_PATH = Path(__file__).parent / "sql" / "01_schema.sql"


def apply_schema(conn):
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def run(conn, run_id, today=None):
    apply_schema(conn)
    extract.extract(conn, run_id, today)
    transform.transform(conn, run_id, today)
    gold.build(conn, today)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with psycopg.connect(require_env("DATABASE_URL")) as conn:
        run(conn, run_id)
