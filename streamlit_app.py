import altair as alt
import pandas as pd
import psycopg
import streamlit as st

from src.config import require_env

LOCAL_TZ = "America/Mexico_City"
FIX_COLOR = "#1f77b4"
DIFF_COLOR = "#ff7f0e"
FIX_LABEL = "USD/MXN FIX (left axis)"
DIFF_LABEL = "Banxico − Fed differential (right axis)"

QUERIES = {
    "latest": """
        SELECT obs_date, usd_mxn_fix::float8 AS fix, rate_diff_pp::float8 AS diff
        FROM gold.rates_daily ORDER BY obs_date DESC
    """,
    "monthly": """
        SELECT month_start, usd_mxn_fix_avg::float8 AS usd_mxn_fix_avg,
               rate_diff_avg_pp::float8 AS rate_diff_avg_pp, is_complete
        FROM gold.rates_monthly ORDER BY month_start
    """,
    "lags": """
        SELECT lag_months, correlation::float8 AS r, n_months
        FROM gold.lag_correlation ORDER BY lag_months
    """,
    "runs": f"""
        SELECT run_id,
               to_char(max(checked_at) AT TIME ZONE '{LOCAL_TZ}', 'YYYY-MM-DD HH24:MI') AS local_time,
               count(*) AS checks,
               count(*) FILTER (WHERE NOT passed) AS failed
        FROM silver.dq_result GROUP BY run_id
        ORDER BY max(checked_at) DESC LIMIT 5
    """,
    "checks": """
        SELECT series_id, check_name, metric::float8 AS metric, passed
        FROM silver.dq_result
        WHERE run_id = (SELECT run_id FROM silver.dq_result ORDER BY checked_at DESC LIMIT 1)
        ORDER BY series_id, check_name
    """,
    "quarantine": """
        SELECT source, series_id, reason, count(*) AS rows
        FROM silver.quarantine GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
    """,
    "coverage": """
        SELECT series_id, count(*) AS observations, min(obs_date) AS first_date, max(obs_date) AS last_date
        FROM silver.observation GROUP BY 1 ORDER BY 1
    """,
}

st.set_page_config(page_title="Banxico-Fed rates and USD/MXN", layout="wide")


def read(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=columns)


@st.cache_data(show_spinner="Loading data from the database...")
def load(stamp):
    # One connection for all queries: each new connection to a remote database costs a round trip of setup.
    # The stamp changes when a pipeline run finishes, so the cache lasts until new data arrives.
    with psycopg.connect(require_env("DATABASE_URL")) as conn:
        return {name: read(conn, sql) for name, sql in QUERIES.items()}


# Read on every rerun. It is a single cheap row and changes only when dq_result gets new rows.
with psycopg.connect(require_env("DATABASE_URL")) as conn:
    stamp = read(conn, "SELECT max(checked_at)::text AS stamp FROM silver.dq_result")["stamp"].iloc[0]

data = load(stamp)


def dual_axis_chart(months):
    fix = pd.DataFrame({"month": months.index, "value": months["usd_mxn_fix_avg"], "series": FIX_LABEL})
    diff = pd.DataFrame({"month": months.index, "value": months["rate_diff_avg_pp"], "series": DIFF_LABEL})
    color = alt.Color("series:N", legend=alt.Legend(title=None, orient="top"),
                      scale=alt.Scale(domain=[FIX_LABEL, DIFF_LABEL], range=[FIX_COLOR, DIFF_COLOR]))
    x = alt.X("month:T", title=None)
    fix_line = alt.Chart(fix).mark_line().encode(
        x=x, color=color,
        y=alt.Y("value:Q", title="USD/MXN FIX (MXN per USD)",
                scale=alt.Scale(domain=[float(min(15, fix["value"].min())), float(fix["value"].max() * 1.05)]),
                axis=alt.Axis(titleColor=FIX_COLOR)))
    diff_line = alt.Chart(diff).mark_line().encode(
        x=x, color=color,
        y=alt.Y("value:Q", title="Differential (pp)",
                scale=alt.Scale(domain=[float(min(2, diff["value"].min())), float(diff["value"].max() * 1.05)]),
                axis=alt.Axis(orient="right", titleColor=DIFF_COLOR)))
    return alt.layer(fix_line, diff_line).resolve_scale(y="independent").properties(height=380)


def lag_chart(lags):
    band = pd.DataFrame({"lag_months": lags["lag_months"],
                         "lo": -lags["bound_95"], "hi": lags["bound_95"]})
    background = alt.Chart(band).mark_bar(color="gray", opacity=0.2).encode(
        x=alt.X("lag_months:O", title="Lag (months)"), y="lo:Q", y2="hi:Q")
    bars = alt.Chart(lags).mark_bar(color=FIX_COLOR).encode(
        x="lag_months:O", y=alt.Y("r:Q", title="Correlation r"),
        tooltip=["lag_months", "r", "n_months", "bound_95"])
    return alt.layer(background, bars).properties(height=300)


st.title("Banxico − Fed rate differential and USD/MXN")
analysis, quality = st.tabs(["Analysis", "Data quality"])

with analysis:
    latest = data["latest"]
    last = latest.iloc[0]
    with_diff = latest.dropna(subset=["diff"]).iloc[0]
    a, b, c = st.columns(3)
    a.metric("USD/MXN FIX (latest)", f"{last['fix']:.4f}", help=f"Date {last['obs_date']}")
    b.metric("Banxico − Fed differential (latest)", f"{with_diff['diff']:.2f} pp",
             help=f"Date {with_diff['obs_date']}")
    c.metric("Latest FIX date", str(last["obs_date"]))

    monthly = data["monthly"]
    monthly["month_start"] = pd.to_datetime(monthly["month_start"])
    complete = monthly[monthly["is_complete"]].set_index("month_start")

    st.subheader("USD/MXN and differential, monthly average")
    st.caption("Complete months only. The current month is excluded because its average is not final.")
    st.altair_chart(dual_axis_chart(complete), width="stretch")

    lags = data["lags"]
    lags["bound_95"] = 1.96 / lags["n_months"] ** 0.5
    st.subheader("Correlation of the differential with the monthly FIX change, by lag")
    st.altair_chart(lag_chart(lags), width="stretch")
    st.caption("The grey band is the approximate 95% bound, ±1.96/√n. A bar inside the band is not "
               "distinguishable from zero with this sample.")
    st.dataframe(lags, hide_index=True)

with quality:
    runs = data["runs"]
    if runs.empty:
        st.info("No data-quality results yet. Run the transform step.")
    else:
        st.subheader("Last 5 runs")
        st.caption("Times in Mexico City.")
        st.dataframe(runs.drop(columns="run_id"), hide_index=True)

        checks = data["checks"]
        st.subheader(f"Checks for the run at {runs['local_time'].iloc[0]}")
        total, failed = st.columns(2)
        total.metric("Checks", len(checks))
        failed.metric("Failed", int((~checks["passed"]).sum()))
        st.dataframe(checks, hide_index=True)

    st.subheader("Quarantine")
    quarantine = data["quarantine"]
    if quarantine.empty:
        st.write("No rows in quarantine.")
    else:
        st.dataframe(quarantine, hide_index=True)

    st.subheader("Observations in silver")
    st.dataframe(data["coverage"], hide_index=True)
