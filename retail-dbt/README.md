# retail-dbt

dbt-duckdb project that turns the retail generator's Parquet into tested marts for Lightdash.
Business definitions and model specs: `.claude/skills/retail-dbt-analytics/models.md`.

```
staging (views, 1:1 with source)  →  intermediate (views)  →  marts/core (tables)  →  marts/reporting (tables)
stg_retail__*                         int_order_lines_enriched   dim_date / customers /    rpt_daily_sales
                                      int_customer_orders        products / stores         rpt_customer_cohorts
                                                                 fct_orders                rpt_inventory_health
                                                                 fct_order_lines (incr.)   rpt_kpi_daily
                                                                 fct_returns
                                                                 fct_inventory_snapshots
```

Sources are read in place from `RETAIL_SOURCE_URI` (default `../data`); there is no load step.

## Local loop

```bash
# from the repo root: generate + validate local Parquet into ./data
python retail-data-generator/generate_retail.py --mode full --scale 0.1 --target parquet --output ./data
python retail-data-generator/validate_retail.py --target parquet --output ./data

cd retail-dbt
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/dbt deps
RETAIL_SOURCE_URI=../data .venv/bin/dbt build --target dev     # writes ./dev.duckdb
.venv/bin/dbt source freshness --target dev
.venv/bin/dbt docs generate --target dev
```

`profiles.yml` is committed and only reads secrets from env vars. Prod (`DBT_TARGET=prod`)
writes to MotherDuck `md:retail_analytics` and reads `gs://…` with `MOTHERDUCK_TOKEN`,
`GCS_HMAC_KEY_ID`, `GCS_HMAC_SECRET` and `RETAIL_SOURCE_URI` set.

## Things worth knowing

- **Sales measures count completed orders only** (0 on cancelled/pending lines); `line_total`
  and `fct_orders.order_value` keep the raw value for sizing cancellations.
- **Online sales use `store_id = 0`**, which is the Online row in `dim_stores`.
- **Refunds are reported by `return_date`.** Returns in the first 30 days of data can belong to
  orders placed before it starts. They're kept in `fct_returns` with `has_order_line = false`
  and NULL store/category/channel, and the FK tests allow for them.
- **`fct_order_lines` is incremental** (merge on `order_item_id`). Each run reprocesses the
  last `incremental_lookback_days` (3) of orders *and* any line returned in that period, so
  late returns update `is_returned`/`refund_amount`. Use `--full-refresh` after a backfill
  that rewrites older dates.
- `rpt_daily_sales` has no order counts because orders span categories; use `rpt_kpi_daily`
  or `fct_orders`.
