# Data sources

Last updated: 2026-10-27.

## Question

Does the gap between Mexico's and the US short-term policy rates help explain monthly moves in USD/MXN, and at what lag?

## Series

| ID | Source | Description | Unit | Calendar | Role |
|---|---|---|---|---|---|
| `SF43718` | Banxico SIE | USD/MXN FIX, the official daily reference rate | MXN per USD | Banking business days | Dependent variable |
| `SF61745` | Banxico SIE | Banxico target rate | Percent | All days | Mexico side of the differential |
| `DFF` | FRED (Federal Reserve) | Effective federal funds rate | Percent | All days | US side of the differential |

The differential is `SF61745 − DFF`, in percentage points.

## Why these series

- **FIX:** Banxico's official reference rate, free through the SIE API, with one definition over the whole period.
- **Target rate:** the policy rate itself, set by the Banxico board. Market rates such as TIIE or CETES also include term premia and liquidity.
- **DFF:** a single daily series. The Fed's target range would add two series and a modelling choice.
- **Two providers:** the case asks for two APIs. FRED alone would not give a Mexican policy rate.

## Calendars and joins

- FIX has business days only. The target rate has rows on weekends. DFF has rows on weekends too, but the Fed's H.15 release is published on weekdays only, so the weekend DFF rows are probably carried-forward values, not published observations. The join uses FIX dates, so they do not enter the analysis.
- The join spine is the FIX date. Each FIX date gets the rates for that same date. Nothing is filled forward.
- The latest rows can be missing one side. DFF lags the FIX by one or more days because of publication. The missing value stays null.

## Access

**Banxico SIE** (`https://www.banxico.org.mx/SieAPIRest/service/v1`)
- Requires a 64-character token, sent in the `Bmx-Token` header.
- Dates are `dd/mm/yyyy`. Missing values are `N/E`.

**FRED** (`https://api.stlouisfed.org/fred/series/observations`)
- Requires a 32-character `api_key`. `file_type=json` returns JSON.
- Dates are `yyyy-mm-dd`. Missing values are marked `.` in third-party notes; none appeared in our data.
- The key travels in the URL, so the pipeline never logs URLs or raw exception text.

Limits quoted by third parties (not checked against the official pages in this document): Banxico 80 requests per minute, FRED 120 per minute. The pipeline makes one request per series per run.

## Incremental load

- The first run fetches from 2020-01-01.
- Later runs start 7 days before the last date stored in silver for each series and fetch up to today. Anything after that point is fetched, so a skipped run is caught up on the next one. The extra days are a safeguard for values that arrive late for a date inside that week. No such gap has been observed so far, and the re-reads in our runs changed no values.
- Bronze upserts on `(source, series_id, obs_date_raw)`. A value is rewritten only when it changes.

## Data-quality checks

Checked in `silver.dq_result` on every run:

| Check | Rule |
|---|---|
| `bad_date` | Date does not parse for its source format |
| `null_value` | Value is `N/E`, `.`, empty or not numeric |
| `out_of_range` | FIX outside 5–40, rates outside 0–25 |
| `future_date` | Date after today |
| `freshness` | Days since the latest observation, threshold 7 |
| `join_coverage` | Share of FIX dates with each rate, threshold 99% |

Failing rows go to `silver.quarantine` with the first rule they failed.

Not implemented yet: day-over-day jump checks (FIX above 3%, rates above 1 pp). The first probe found 12 FIX days above 3%. Those need review before they are flagged as errors.

## First run results

Data as loaded on 2026-10-27:

| Series | Bronze rows | Silver observations | Latest date |
|---|---|---|---|
| `SF43718` | 1,716 | 1,716 | 2026-10-26 |
| `SF61745` | 2,481 | 2,481 | 2026-10-27 |
| `DFF` | 2,488 | 2,488 | 2026-10-23 |

Quarantine was empty. Every check passed. The probe found no nulls, non-numeric values, duplicates or future dates.

`DFF` covers 99.9% of FIX dates. The one missing date is the latest FIX date (2026-10-26).

Weekend share in the 2026-10-26 probe: 28.3% for `SF61745`, 28.5% for `DFF`, 0.0% for the FIX.

Lag correlations, monthly, complete months only (80 to 75 months depending on the lag):

| Lag (months) | Correlation |
|---|---|
| 0 | 0.160 |
| 1 | 0.086 |
| 2 | 0.059 |
| 3 | 0.024 |
| 4 | −0.025 |
| 5 | −0.034 |
| 6 | −0.028 |

The approximate 95% bound for about 80 months is ±0.22. No lag falls outside it.

## Limitations

- Correlation is not causation. The model leaves out inflation, risk appetite, oil prices and capital flows.
- The target rate changes a few times a year, so daily lag analysis has little variation. The analysis uses monthly averages.
- One series is a policy target and the other is an effective market rate.
- Policy regimes change, so one relationship may not hold over the whole period.

## References

- Banxico SIE API: https://www.banxico.org.mx/SieAPIRest/service/v1
- Banxico SIE API examples: https://www.banxico.org.mx/SieAPIRest/service/v1/doc/ejemplos
- Banxico FIX: https://www.banxico.org.mx/tipcamb/llenarTiposCambioAction.do?idioma=en
- Banxico table CF101, "Tasas de Interés en el Mercado de Dinero": https://www.banxico.org.mx/SieInternet/consultarDirectorioInternetAction.do?accion=consultarCuadro&idCuadro=CF101&sector=18&locale=es
- FRED, Federal Funds Effective Rate (DFF): https://fred.stlouisfed.org/series/DFF
- FRED API documentation: https://fred.stlouisfed.org/docs/api/fred
- Series screenshots from the SIE catalogue: `docs/evidence/`
