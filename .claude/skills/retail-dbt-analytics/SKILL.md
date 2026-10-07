---
name: retail-dbt-analytics
description: Build and extend the dbt-duckdb project that turns the dummy retail Parquet data (from the retail-data-generator skill) into reporting marts and metrics, running locally on DuckDB and in prod on MotherDuck, orchestrated by Orchestra. Use this whenever the user mentions dbt models, staging/marts, retail reporting, KPIs, revenue/returns/inventory analytics, MotherDuck, or running dbt in Orchestra for this project — even if they just say "the analytics layer" or "the models".
---

# Retail dbt analytics

Turns the generator's raw Parquet (local dir or `gs://…/retail/<env>/`) into tested,
documented marts that Lightdash (see the `lightdash-dashboards` skill) reads directly.

## Decisions (keep to these unless the user says otherwise)

- **dbt, not Python tasks, for reporting.** Transformations are SQL, we want tests, docs and
  lineage, and Lightdash's semantic layer lives in the dbt YAML. Python stays for generation only.
- **Adapter: `dbt-duckdb`** (pin `dbt-core>=1.8`, Lightdash's MotherDuck support needs ≥1.8).
- **Targets:**
  - `dev` → local file `./dev.duckdb`, sources read from local Parquet (`./data`).
  - `prod` → MotherDuck `md:retail_analytics`, sources read from `gs://<bucket>/retail/prod`.
  The same code runs on both; only `profiles.yml` + env vars differ.
- **Sources are external Parquet**, not loaded tables. dbt-duckdb reads them in place via
  `external_location` on the source. No separate load step.
- If the user later wants BigQuery instead of MotherDuck (e.g. a client demo on GCP), swap
  the adapter and turn sources into BigQuery external tables over the same GCS prefix — the
  model SQL should need only minor dialect changes. Keep SQL portable: avoid DuckDB-only
  functions in marts where a standard equivalent exists.

## Project layout

```
retail-dbt/
├── dbt_project.yml
├── profiles.yml            # committed; secrets only via env_var()
├── packages.yml            # dbt_utils, dbt_expectations (optional)
├── requirements.txt        # dbt-core, dbt-duckdb (pinned)
├── models/
│   ├── staging/            # stg_retail__*.sql + _retail__sources.yml + _stg_retail__models.yml
│   ├── intermediate/       # int_*.sql
│   └── marts/
│       ├── core/           # dims + facts
│       └── reporting/      # aggregates for dashboards
├── macros/
├── tests/                  # singular tests
└── analyses/
```

## Sources

```yaml
# models/staging/_retail__sources.yml
sources:
  - name: retail
    meta:
      external_location: "read_parquet('{{ env_var('RETAIL_SOURCE_URI') }}/{name}/**/*.parquet', hive_partitioning = true, union_by_name = true)"
    tables:
      - name: customers
      - name: products
      - name: stores
      - name: orders
        loaded_at_field: order_ts
        freshness: {warn_after: {count: 26, period: hour}, error_after: {count: 50, period: hour}}
      - name: order_items
      - name: returns
      - name: inventory_snapshots
```
Check the current dbt-duckdb README for the exact `external_location` templating
(`{name}` substitution) and fix the glob if dims are unpartitioned single files.

## profiles.yml

```yaml
retail:
  target: "{{ env_var('DBT_TARGET', 'dev') }}"
  outputs:
    dev:
      type: duckdb
      path: dev.duckdb
      threads: 4
    prod:
      type: duckdb
      path: "md:retail_analytics?motherduck_token={{ env_var('MOTHERDUCK_TOKEN') }}"
      threads: 4
      extensions: [httpfs]
      secrets:
        - type: gcs
          key_id: "{{ env_var('GCS_HMAC_KEY_ID') }}"
          secret: "{{ env_var('GCS_HMAC_SECRET') }}"
```
MotherDuck may execute the Parquet scan server-side, which needs a secret stored in
MotherDuck too. One-off setup the user runs in MotherDuck:
`CREATE SECRET IN MOTHERDUCK (TYPE gcs, KEY_ID '…', SECRET '…');`
Verify this against the current MotherDuck docs.

## Modelling conventions

- staging: one model per source table, views, rename/cast/clean only (trim + initcap
  `city`, lowercase emails, cast decimals). No joins.
- intermediate: joins and business logic, ephemeral or views.
- marts/core: tables. `fct_order_lines` is **incremental** (`unique_key='order_item_id'`,
  `merge` strategy, 3-day lookback on `order_date`) so daily Orchestra runs stay cheap.
- marts/reporting: small aggregate tables shaped for dashboards.
- Every model has a YAML entry with description; every PK has `unique` + `not_null`;
  every FK has `relationships`.
- Money: `DECIMAL(12,2)`, never float.
- Model and metric specs: `references/models.md`. Keep that file in sync when adding models.

## Tests worth having beyond generic ones
- `net_sales` in `fct_orders` reconciles to the sum of `fct_order_lines` (singular test)
- no returns on cancelled orders
- `refund_amount <= line_total`
- `rpt_daily_sales` total for a sample day equals `fct_order_lines` for that day
- source freshness on `orders`

## Running it

Local loop:
```
cd retail-dbt
dbt deps && dbt build --target dev       # build = run + test, in DAG order
dbt docs generate
```
Run the generator at `--scale 0.1` into `./data` first, with `RETAIL_SOURCE_URI=../data`.

In Orchestra: `references/orchestra-dbt.md`.

## Working style
- Build staging → intermediate → core → reporting, running `dbt build` after each layer.
- After any change to a model in `marts/`, check whether the Lightdash metrics on it
  (in its YAML `meta`) still make sense.
- Don't add metrics/dimension `meta` for Lightdash here — that's the `lightdash-dashboards`
  skill's job, though it edits the same YAML files.