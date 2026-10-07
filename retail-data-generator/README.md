# retail-data-generator

Deterministic dummy UK retail data (stores, products, customers, orders, order items,
returns, inventory snapshots) generated in DuckDB SQL. Same seed + same window ⇒ identical
data, so incremental runs and backfills are safe to repeat. Spec and table definitions:
`.claude/skills/retail-data-generator/references/schema.md`.

Targets: a local DuckDB file, or Hive-partitioned Parquet locally or in GCS. Orchestra comes next.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
```

## Generate

```bash
# full year ending today, into a DuckDB file (tables in schema `raw`)
.venv/bin/python generate_retail.py --mode full --scale 0.1 --target duckdb --output out/retail.duckdb

# same into Hive-partitioned Parquet
.venv/bin/python generate_retail.py --mode full --scale 0.1 --target parquet --output out/data

# one day (re-running the same day replaces it, never duplicates)
.venv/bin/python generate_retail.py --mode incremental --run-date 2026-10-05 --target parquet --output out/data
```

| flag | env var | default |
|---|---|---|
| `--mode full\|incremental` | `RETAIL_MODE` | `full` |
| `--run-date` (incremental) | `RETAIL_RUN_DATE` | today UTC (empty = today) |
| `--end-date` / `--days-back` (full) | `RETAIL_END_DATE` / `RETAIL_DAYS_BACK` | today / 365 |
| `--scale` | `RETAIL_SCALE` | 1.0 |
| `--seed` | `RETAIL_SEED` | 42 |
| `--target duckdb\|parquet` | `RETAIL_TARGET` | `duckdb` |
| `--output` | `RETAIL_OUTPUT` | `./retail.duckdb` or `./data` |
| `--inventory-frequency weekly\|daily` | `RETAIL_INVENTORY_FREQUENCY` | `weekly` |
| `--clean` (no deliberate messiness) | `RETAIL_CLEAN` | off |
| `--upload-duckdb gs://…/retail.duckdb` (duckdb target) | `RETAIL_UPLOAD_DUCKDB` | off |

CLI flags win over env vars.

## GCS

Parquet goes straight to GCS via DuckDB httpfs, authenticated with HMAC keys
(Cloud Storage > Settings > Interoperability) for a service account with
`roles/storage.objectAdmin` on the bucket:

```bash
export GCS_HMAC_KEY_ID=... GCS_HMAC_SECRET=...      # or: set -a; source .env; set +a
.venv/bin/python generate_retail.py --mode incremental --run-date 2026-10-05 --target parquet --output gs://qs_orchestra/dev
.venv/bin/python validate_retail.py --target parquet --output gs://qs_orchestra/dev --run-date 2026-10-05
```

Reruns overwrite the same fixed-name objects, so they're idempotent. DuckDB can't delete GCS
objects, so a rerun that would produce *fewer* partitions (e.g. a different seed) leaves the
old files in place; clear the prefix first in that case.

`--upload-duckdb` (needs `pip install -r requirements-gcs.txt`) uploads the finished
`.duckdb` file as a snapshot, using Application Default Credentials or the key JSON in
`GCP_SERVICE_ACCOUNT_JSON`.

## Validate

```bash
.venv/bin/python validate_retail.py --target duckdb --output out/retail.duckdb
.venv/bin/python validate_retail.py --target parquet --output out/data --run-date 2026-10-05
.venv/bin/python validate_retail.py --target parquet --output out/data --hashes   # row count + md5 per table
```

Exits 1 if any check fails. Returns dated in the first 30 days of the data refer to orders
from before the data starts; those are reported as INFO and excluded from the FK check.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

Covers: validator passes on both targets, re-running an incremental day is idempotent,
full runs are deterministic per seed, DuckDB and Parquet outputs hold identical data, and the
validator actually fails on a broken FK.
