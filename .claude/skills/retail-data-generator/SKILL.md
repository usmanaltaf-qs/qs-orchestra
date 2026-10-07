---
name: retail-data-generator
description: Build, run, and extend a deterministic Python + DuckDB generator for dummy retail data (customers, products, stores, orders, order items, inventory, returns), writing to a local .duckdb file or Parquet in GCS, and wire it into an Orchestra pipeline. Use this whenever the user wants synthetic/fake/dummy/sample retail or e-commerce data, seed data for dbt or a demo pipeline, DuckDB test tables, data landing in GCS, or an Orchestra Python task — even if they don't say "generator".
---

# Retail data generator

Goal: a plain Python CLI (no LLM at runtime) that generates realistic, deterministic retail
source data with DuckDB, lands it locally or in GCS, and runs as an Orchestra Python task.
Downstream (dbt, validation, dashboards) treats it like a real source system.

## Project layout to build

```
retail-data-generator/
├── pyproject.toml / requirements.txt   # duckdb, google-cloud-storage (optional)
├── generate_retail.py                  # main CLI
├── validate_retail.py                  # data quality checks, exits non-zero on failure
├── orchestra/pipeline.yml              # see references/orchestra.md
└── README.md
```

Keep it as two scripts, not a framework. One file each is fine until it passes ~400 lines.

## Core design rules (don't drift from these)

1. **Generate in DuckDB SQL, not Python loops.** Use `range()` / `unnest(range(n))` to make
   rows and SQL for every column. No Faker — it's slow and a dependency we don't need.
   Small lookup lists (names, cities, categories) live as Python constants injected as
   DuckDB list literals.
2. **Deterministic via hashing, not `random()`.** `random()` isn't reproducible across
   threads. Define a macro once per connection:
   ```sql
   CREATE MACRO u(k, salt) AS (hash(k, salt || '{seed}') % 1000000)::DOUBLE / 1000000.0;
   ```
   Every random-looking column is `u(<row key>, '<column name>')`. Same seed + same date
   ⇒ byte-identical output. This is what makes incremental runs and backfills possible.
3. **Date-keyed IDs.** `order_id = yyyymmdd * 1_000_000 + seq`, so IDs are unique across
   independent daily runs without reading previous state.
4. **One code path for full and incremental.** Both modes take a window `[start, end]`:
   - full: `end = --end-date (default today)`, `start = end - --days-back (default 365)`
   - incremental: `start = end = --run-date (default today UTC; empty string = today)`

   Internally always generate orders/items for `[start - 30d, end]`, compute returns, then
   filter orders/items to `[start, end]` and returns to `return_date in [start, end]`.
   Because generation is deterministic, the 30-day lookback is recomputed, never read.
5. **Idempotent writes.** Re-running the same date must not duplicate data
   (see Outputs below).
6. **Dimensions are rewritten every run** (cheap, deterministic, idempotent).

Full table specs, distributions and the "add a table" checklist: `references/schema.md`.

## CLI

```
python generate_retail.py \
  --mode full|incremental \
  --run-date YYYY-MM-DD        # incremental
  --end-date YYYY-MM-DD --days-back 365   # full
  --scale 1.0                  # multiplies base row counts
  --seed 42
  --target duckdb|parquet
  --output PATH_OR_URI         # ./retail.duckdb | ./data | gs://bucket/prefix
  --upload-duckdb gs://bucket/path/retail.duckdb   # optional, duckdb target only
  --set-orchestra-outputs      # optional
```

Every flag should also read from an env var (`RETAIL_MODE`, `RETAIL_RUN_DATE`, `RETAIL_OUTPUT`,
…) so Orchestra config can live in env vars rather than long commands. CLI wins over env.

Log a summary at the end: rows per table, window, target, elapsed seconds.

## Outputs

**DuckDB target** — tables in schema `raw`. Incremental: inside one transaction,
`DELETE` rows for the window from fact tables, then `INSERT`. Dims: `CREATE OR REPLACE`.

**Parquet target** (recommended for GCS + pipelines) — Hive-style layout:
```
{output}/customers/data_0.parquet          (same for products, stores)
{output}/orders/order_date=YYYY-MM-DD/data_0.parquet
{output}/order_items/order_date=YYYY-MM-DD/data_0.parquet
{output}/returns/return_date=YYYY-MM-DD/data_0.parquet
{output}/inventory_snapshots/snapshot_date=YYYY-MM-DD/data_0.parquet
```
Use `COPY ... (FORMAT parquet, PARTITION_BY (...), FILENAME_PATTERN 'data_{i}', OVERWRITE_OR_IGNORE true)`.
Fixed filenames ⇒ re-runs overwrite the same objects ⇒ idempotent.

GCS auth and the `.duckdb`-file-in-GCS option: `references/gcs.md`.

## validate_retail.py

Reads the same target (duckdb file or parquet URI via `read_parquet('{uri}/orders/*/*.parquet', hive_partitioning=true)`).
Checks, each printed PASS/FAIL, exit 1 if any fail:
- every table non-empty (for the window if `--run-date` given)
- primary keys unique
- FKs: order_items→orders, order_items→products, orders→customers (nullable), orders→stores (nullable), returns→order_items
- `line_total = round(quantity * unit_price * (1 - discount_pct), 2)`
- `refund_amount <= line_total`, `return_date >= order_date`
- cancelled orders have no returns

## Orchestra

Pipeline YAML, task params, secrets and gotchas: `references/orchestra.md`.
Key gotcha: Orchestra-managed compute is ephemeral — a local `.duckdb` file vanishes after the
task. Anything a downstream task needs must land in GCS.

## Working style

- Build the generator, run it locally at `--scale 0.1`, then run the validator, before
  touching GCS or Orchestra.
- Run the same date twice and confirm row counts don't change (idempotency test).
- Run full mode with the same seed twice and diff a hash of each table (determinism test).
- Add a `pytest` file covering those two tests plus the validator on a tiny scale.
