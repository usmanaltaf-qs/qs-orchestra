#!/usr/bin/env python3
"""Deterministic dummy retail data generator (DuckDB SQL, no Faker, no LLM at runtime).

Writes raw, source-system-shaped tables to a local .duckdb file (schema `raw`) or to
Hive-partitioned Parquet. Spec: .claude/skills/retail-data-generator/references/schema.md

Every random-looking column is u(<row key>, '<table.column>'), a hash of the key, salt and
seed, so the same seed + window always produces identical data.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

log = logging.getLogger("generate_retail")

LOOKBACK_DAYS = 30
ROW_WARN_THRESHOLD = 50_000_000
# Signup dates are anchored to a fixed date rather than the window start so the customers
# dim is identical across daily incremental runs (dims are rewritten on every run).
SIGNUP_ANCHOR = date(2024, 1, 1)
STORE_OPEN_FROM, STORE_OPEN_TO = date(2005, 1, 1), date(2023, 12, 31)

BASE = {"stores": 25, "products": 500, "customers": 10_000, "orders_per_day": 150}

DIMS = ["stores", "products", "customers"]
FACTS = {
    "orders": "order_date",
    "order_items": "order_date",
    "returns": "return_date",
    "inventory_snapshots": "snapshot_date",
}
TABLES = DIMS + list(FACTS)
SORT_KEYS = {
    "stores": "store_id",
    "products": "product_id",
    "customers": "customer_id",
    "orders": "order_id",
    "order_items": "order_item_id",
    "returns": "return_id",
    "inventory_snapshots": "snapshot_date, store_id, product_id",
}

CITIES = [
    ("London", "London"), ("Manchester", "North West"), ("Liverpool", "North West"),
    ("Birmingham", "Midlands"), ("Nottingham", "Midlands"), ("Leicester", "Midlands"),
    ("Leeds", "Yorkshire"), ("Sheffield", "Yorkshire"), ("York", "Yorkshire"),
    ("Newcastle", "North East"), ("Glasgow", "Scotland"), ("Edinburgh", "Scotland"),
    ("Aberdeen", "Scotland"), ("Cardiff", "Wales"), ("Swansea", "Wales"),
    ("Bristol", "South West"), ("Exeter", "South West"), ("Brighton", "South East"),
    ("Southampton", "South East"), ("Oxford", "South East"), ("Cambridge", "East of England"),
    ("Norwich", "East of England"), ("Belfast", "Northern Ireland"),
]
STORE_SUFFIXES = ["High Street", "Retail Park", "Centre"]
# (category, subcategory, min list price, max list price) in GBP
SUBCATEGORIES = [
    ("Clothing", "Tops", 8, 40), ("Clothing", "Jeans", 25, 90), ("Clothing", "Jackets", 40, 200),
    ("Home", "Bedding", 15, 120), ("Home", "Kitchen", 5, 80), ("Home", "Decor", 5, 60),
    ("Electronics", "Audio", 20, 300), ("Electronics", "Accessories", 5, 50),
    ("Electronics", "Smart Home", 25, 250),
    ("Beauty", "Skincare", 5, 60), ("Beauty", "Fragrance", 20, 120),
    ("Food", "Snacks", 1, 6), ("Food", "Drinks", 1, 10),
]
BRANDS = [
    "Northbay", "Halden", "Copperleaf", "Brightwell", "Ashgrove", "Kestrel & Co", "Marlow Lane",
    "Thistle", "Fenwick Works", "Oakmere", "Larkspur", "Bramble", "Greystone", "Peregrine",
    "Saltmarsh",
]
ADJECTIVES = [
    "Classic", "Essential", "Premium", "Everyday", "Signature", "Urban", "Heritage", "Smart",
    "Organic", "Deluxe", "Compact", "Original", "Luxe", "Studio", "Coastal",
]
FIRST_NAMES = [
    "Oliver", "George", "Harry", "Jack", "Jacob", "Noah", "Charlie", "Muhammad", "Thomas",
    "Oscar", "William", "James", "Leo", "Alfie", "Henry", "Joshua", "Freddie", "Archie", "Ethan",
    "Isaac", "Alexander", "Joseph", "Edward", "Samuel", "Max", "Olivia", "Amelia", "Isla", "Ava",
    "Emily", "Sophia", "Grace", "Mia", "Poppy", "Ella", "Lily", "Evie", "Isabella", "Sophie",
    "Ivy", "Freya", "Harper", "Willow", "Charlotte", "Jessica", "Rosie", "Daisy", "Alice",
    "Florence", "Priya",
]
LAST_NAMES = [
    "Smith", "Jones", "Williams", "Taylor", "Brown", "Davies", "Evans", "Wilson", "Thomas",
    "Johnson", "Roberts", "Robinson", "Thompson", "Wright", "Walker", "White", "Edwards",
    "Hughes", "Green", "Hall", "Lewis", "Harris", "Clarke", "Patel", "Jackson", "Wood", "Turner",
    "Martin", "Cooper", "Hill", "Ward", "Morris", "Moore", "Clark", "Lee", "King", "Baker",
    "Harrison", "Morgan", "Allen", "James", "Scott", "Phillips", "Watson", "Davis", "Parker",
    "Price", "Bennett", "Young", "Khan",
]
RETURN_REASONS = ["wrong size", "damaged", "not as described", "changed mind"]
ONLINE_RETURN_REASONS = RETURN_REASONS + ["late delivery"]


def q(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def sql_list(items) -> str:
    return "[" + ", ".join(q(i) if isinstance(i, str) else str(i) for i in items) + "]"


def scaled(base: int, scale: float) -> int:
    return max(1, int(base * scale + 0.5))


# --------------------------------------------------------------------------- generation


def generate(con, start: date, end: date, scale: float, seed: int, clean: bool,
             inventory_frequency: str) -> None:
    """Create all tables in `con` for the window [start, end]."""
    gen_start = start - timedelta(days=LOOKBACK_DAYS)
    dirty = "false" if clean else "true"
    n_stores = scaled(BASE["stores"], scale)
    n_products = scaled(BASE["products"], scale)
    n_customers = scaled(BASE["customers"], scale)
    cities, regions = sql_list([c for c, _ in CITIES]), sql_list([r for _, r in CITIES])
    cats, subs = sql_list([s[0] for s in SUBCATEGORIES]), sql_list([s[1] for s in SUBCATEGORIES])
    lo, hi = sql_list([s[2] for s in SUBCATEGORIES]), sql_list([s[3] for s in SUBCATEGORIES])

    con.execute(
        f"CREATE OR REPLACE MACRO u(k, salt) AS "
        f"(hash(k, salt || '{int(seed)}') % 1000000)::DOUBLE / 1000000.0"
    )
    # pick(list, key, salt): deterministic uniform choice from a list literal
    con.execute(
        "CREATE OR REPLACE MACRO pick(lst, k, salt) AS "
        "lst[floor(u(k, salt) * len(lst))::INTEGER + 1]"
    )

    con.execute(f"""
        CREATE OR REPLACE TABLE stores AS
        WITH b AS (
            SELECT range + 1 AS k,
                   floor(u(range + 1, 'store.city') * {len(CITIES)})::INTEGER + 1 AS ci,
                   u(range + 1, 'store.type') AS ut
            FROM range({n_stores})
        )
        SELECT k::INTEGER AS store_id,
               {cities}[ci] || ' ' || pick({sql_list(STORE_SUFFIXES)}, k, 'store.suffix') AS store_name,
               {cities}[ci] AS city,
               {regions}[ci] AS region,
               CASE WHEN ut < 0.10 THEN 'flagship' WHEN ut < 0.80 THEN 'standard' ELSE 'outlet' END
                   AS store_type,
               (CASE WHEN ut < 0.10 THEN 20000 + floor(u(k, 'store.sq_ft') * 20001)
                     WHEN ut < 0.80 THEN 5000 + floor(u(k, 'store.sq_ft') * 10001)
                     ELSE 3000 + floor(u(k, 'store.sq_ft') * 5001) END)::INTEGER AS sq_ft,
               DATE '{STORE_OPEN_FROM}'
                   + floor(u(k, 'store.opened') * {(STORE_OPEN_TO - STORE_OPEN_FROM).days + 1})::INTEGER
                   AS opened_date
        FROM b ORDER BY store_id
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE products AS
        WITH b AS (
            SELECT range + 1 AS k,
                   floor(u(range + 1, 'product.subcategory') * {len(SUBCATEGORIES)})::INTEGER + 1 AS si,
                   pick({sql_list(BRANDS)}, range + 1, 'product.brand') AS brand
            FROM range({n_products})
        ), p AS (
            SELECT *,
                   floor({lo}[si] + u(k, 'product.price') * ({hi}[si] - {lo}[si]))::INTEGER
                       + CASE WHEN u(k, 'product.cents') < 0.5 THEN 0.99 ELSE 0.49 END AS price
            FROM b
        )
        SELECT k::INTEGER AS product_id,
               'SKU-' || lpad(k::VARCHAR, 6, '0') AS sku,
               brand || ' ' || pick({sql_list(ADJECTIVES)}, k, 'product.adjective') || ' ' || {subs}[si]
                   AS product_name,
               {cats}[si] AS category,
               {subs}[si] AS subcategory,
               brand,
               price::DECIMAL(10,2) AS list_price,
               round(price * (0.35 + 0.30 * u(k, 'product.cost')), 2)::DECIMAL(10,2) AS unit_cost,
               u(k, 'product.active') < 0.95 AS is_active
        FROM p ORDER BY product_id
    """)

    con.execute(f"""
        CREATE OR REPLACE TABLE customers AS
        WITH b AS (
            SELECT range + 1 AS k,
                   pick({sql_list(FIRST_NAMES)}, range + 1, 'customer.first_name') AS first_name,
                   pick({sql_list(LAST_NAMES)}, range + 1, 'customer.last_name') AS last_name,
                   floor(u(range + 1, 'customer.city') * {len(CITIES)})::INTEGER + 1 AS ci,
                   u(range + 1, 'customer.loyalty') AS ul,
                   u(range + 1, 'customer.city_mess') AS um
            FROM range({n_customers})
        )
        SELECT k::INTEGER AS customer_id,
               first_name,
               last_name,
               CASE WHEN {dirty} AND u(k, 'customer.email_null') < 0.02 THEN NULL
                    ELSE lower(first_name || '.' || last_name || k::VARCHAR || '@example.com') END
                   AS email,
               CASE WHEN {dirty} AND um < 0.005 THEN upper({cities}[ci])
                    WHEN {dirty} AND um < 0.01 THEN '  ' || lower({cities}[ci]) || ' '
                    ELSE {cities}[ci] END AS city,
               {regions}[ci] AS region,
               DATE '{SIGNUP_ANCHOR}' - (1 + floor(u(k, 'customer.signup') * 1095))::INTEGER
                   AS signup_date,
               CASE WHEN ul < 0.60 THEN 'none' WHEN ul < 0.85 THEN 'bronze'
                    WHEN ul < 0.95 THEN 'silver' ELSE 'gold' END AS loyalty_tier,
               u(k, 'customer.opt_in') < 0.40 AS marketing_opt_in
        FROM b ORDER BY customer_id
    """)

    # Daily order volume for the full generation window [start - 30d, end].
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE days AS
        WITH d AS (
            SELECT t.d::DATE AS day, strftime(t.d, '%Y%m%d')::BIGINT AS dkey
            FROM range(DATE '{gen_start}', DATE '{end}' + INTERVAL 1 DAY, INTERVAL 1 DAY) t(d)
        )
        SELECT day, dkey,
               greatest(1, round({BASE['orders_per_day']} * {float(scale)}
                   * CASE isodow(day) WHEN 5 THEN 1.1 WHEN 6 THEN 1.4 WHEN 7 THEN 1.2 ELSE 0.9 END
                   * CASE month(day) WHEN 11 THEN 1.3 WHEN 12 THEN 1.6 WHEN 1 THEN 0.8 ELSE 1.0 END
                   * (0.85 + 0.30 * u(dkey, 'orders.day_volume'))))::INTEGER AS n_orders
        FROM d
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE orders_all AS
        WITH s AS (SELECT day, dkey, unnest(range(n_orders)) AS seq FROM days),
        k AS (
            SELECT day AS order_date, dkey * 1000000 + seq AS order_id,
                   CASE WHEN u(dkey * 1000000 + seq, 'orders.channel') < 0.65
                        THEN 'store' ELSE 'online' END AS channel
            FROM s
        )
        SELECT order_id,
               order_date,
               order_date::TIMESTAMP + to_seconds((
                   CASE WHEN u(order_id, 'orders.ts_band') < 0.8
                        THEN 10 + floor(u(order_id, 'orders.hour') * 10)
                        ELSE 7 + floor(u(order_id, 'orders.hour') * 16) END * 3600
                   + floor(u(order_id, 'orders.second') * 3600))::BIGINT) AS order_ts,
               channel,
               CASE WHEN channel = 'store'
                    THEN floor(u(order_id, 'orders.store_id') * {n_stores})::INTEGER + 1 END AS store_id,
               CASE WHEN {dirty} AND u(order_id, 'orders.guest')
                         < CASE channel WHEN 'online' THEN 0.15 ELSE 0.40 END THEN NULL
                    ELSE floor({n_customers} * pow(u(order_id, 'orders.customer_id'), 2))::INTEGER + 1
               END AS customer_id,
               CASE WHEN u(order_id, 'orders.status') < 0.05 THEN 'cancelled'
                    WHEN {dirty} AND u(order_id, 'orders.status') >= 0.97
                         AND order_date >= DATE '{end}' - 1 THEN 'pending'
                    ELSE 'completed' END AS status
        FROM k
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE items_all AS
        WITH o AS (
            SELECT order_id, order_date,
                   CASE WHEN u(order_id, 'items.n_lines') < 0.40 THEN 1
                        WHEN u(order_id, 'items.n_lines') < 0.70 THEN 2
                        WHEN u(order_id, 'items.n_lines') < 0.85 THEN 3
                        WHEN u(order_id, 'items.n_lines') < 0.95 THEN 4 ELSE 5 END AS n_lines
            FROM orders_all
        ), l AS (
            SELECT order_id, order_date, unnest(range(1, n_lines + 1)) AS line_number FROM o
        ), k AS (
            SELECT order_id * 10 + line_number AS order_item_id, order_id, order_date,
                   line_number::INTEGER AS line_number,
                   floor({n_products} * pow(u(order_id * 10 + line_number, 'items.product_id'), 2))::INTEGER
                       + 1 AS product_id
            FROM l
        ), v AS (
            SELECT k.*,
                   CASE WHEN u(order_item_id, 'items.quantity') < 0.75 THEN 1
                        WHEN u(order_item_id, 'items.quantity') < 0.93 THEN 2
                        ELSE 3 + floor(u(order_item_id, 'items.quantity_hi') * 3)::INTEGER
                   END AS quantity,
                   p.list_price AS unit_price,
                   (CASE WHEN month(order_date) = 1 THEN
                        CASE WHEN u(order_item_id, 'items.discount') < 0.60 THEN 0
                             WHEN u(order_item_id, 'items.discount') < 0.75 THEN 0.10
                             WHEN u(order_item_id, 'items.discount') < 0.85 THEN 0.20 ELSE 0.50 END
                    ELSE
                        CASE WHEN u(order_item_id, 'items.discount') < 0.70 THEN 0
                             WHEN u(order_item_id, 'items.discount') < 0.85 THEN 0.10
                             WHEN u(order_item_id, 'items.discount') < 0.95 THEN 0.20 ELSE 0.50 END
                    END)::DECIMAL(4,2) AS discount_pct
            FROM k JOIN products p USING (product_id)
        )
        SELECT order_item_id, order_id, order_date, line_number, product_id, quantity,
               unit_price, discount_pct,
               round(quantity * unit_price * (1 - discount_pct), 2)::DECIMAL(12,2) AS line_total
        FROM v
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE returns_all AS
        SELECT i.order_item_id AS return_id,
               i.order_item_id,
               i.order_date + (1 + floor(u(i.order_item_id, 'returns.days') * 30))::INTEGER
                   AS return_date,
               CASE WHEN o.channel = 'online'
                    THEN pick({sql_list(ONLINE_RETURN_REASONS)}, i.order_item_id, 'returns.reason')
                    ELSE pick({sql_list(RETURN_REASONS)}, i.order_item_id, 'returns.reason') END
                   AS reason,
               CASE WHEN u(i.order_item_id, 'returns.partial') < 0.10
                    THEN round(i.line_total * 0.5, 2) ELSE i.line_total END::DECIMAL(12,2)
                   AS refund_amount
        FROM items_all i
        JOIN orders_all o USING (order_id)
        JOIN products p USING (product_id)
        WHERE o.status = 'completed'
          AND u(i.order_item_id, 'returns.is_returned') < CASE p.category
                WHEN 'Electronics' THEN 0.09 WHEN 'Clothing' THEN 0.09
                WHEN 'Food' THEN 0.003 ELSE 0.045 END
    """)

    # Trim facts to the requested window; the 30-day lookback only feeds returns.
    con.execute(f"""
        CREATE OR REPLACE TABLE orders AS SELECT * FROM orders_all
        WHERE order_date BETWEEN DATE '{start}' AND DATE '{end}' ORDER BY order_id
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE order_items AS SELECT * FROM items_all
        WHERE order_date BETWEEN DATE '{start}' AND DATE '{end}' ORDER BY order_item_id
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE returns AS SELECT * FROM returns_all
        WHERE return_date BETWEEN DATE '{start}' AND DATE '{end}' ORDER BY return_id
    """)

    snapshot_filter = "true" if inventory_frequency == "daily" else "isodow(day) = 7"
    con.execute(f"""
        CREATE OR REPLACE TABLE inventory_snapshots AS
        WITH k AS (
            SELECT d.day AS snapshot_date, s.store_id, p.product_id,
                   d.dkey * 10000000000 + s.store_id * 1000000 + p.product_id AS key
            FROM days d CROSS JOIN stores s CROSS JOIN products p
            WHERE d.day BETWEEN DATE '{start}' AND DATE '{end}' AND {snapshot_filter} AND p.is_active
        ), h AS (
            SELECT *, CASE WHEN u(key, 'inventory.stockout') < 0.05 THEN 0
                           ELSE 1 + floor(u(key, 'inventory.on_hand') * 200)::INTEGER END AS on_hand_qty
            FROM k
        )
        SELECT snapshot_date, store_id, product_id, on_hand_qty,
               CASE WHEN on_hand_qty < 20
                    THEN 20 + floor(u(key, 'inventory.on_order') * 81)::INTEGER ELSE 0 END AS on_order_qty
        FROM h ORDER BY snapshot_date, store_id, product_id
    """)


def estimate_rows(start: date, end: date, scale: float, inventory_frequency: str) -> int:
    days = (end - start).days + 1
    orders = days * BASE["orders_per_day"] * scale
    snapshot_days = days if inventory_frequency == "daily" else days / 7
    inventory = (snapshot_days * scaled(BASE["stores"], scale)
                 * scaled(BASE["products"], scale) * 0.95)
    return int(orders * (1 + 2.3 + 0.15) + inventory)


# --------------------------------------------------------------------------- writers


def is_gcs(path: str) -> bool:
    return path.startswith("gs://")


def upload_to_gcs(local_path: str, uri: str) -> None:
    """Upload a local file with google-cloud-storage (ADC, or GCP_SERVICE_ACCOUNT_JSON)."""
    try:
        from google.cloud import storage
    except ImportError:
        raise SystemExit("--upload-duckdb needs google-cloud-storage: "
                         "pip install -r requirements-gcs.txt") from None
    if sa_json := os.environ.get("GCP_SERVICE_ACCOUNT_JSON"):
        # Environments without ADC (Orchestra) pass the key JSON itself in an env var
        fd, key_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(sa_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_path
    bucket, _, blob = uri.removeprefix("gs://").partition("/")
    if not bucket or not blob:
        raise SystemExit(f"--upload-duckdb needs gs://bucket/path/file.duckdb, got {uri}")
    storage.Client().bucket(bucket).blob(blob).upload_from_filename(local_path)
    log.info("uploaded %s -> %s", local_path, uri)


def connect(output: str) -> duckdb.DuckDBPyConnection:
    """In-memory connection; for gs:// paths, with httpfs loaded and a GCS HMAC secret."""
    con = duckdb.connect()
    if is_gcs(output):
        key_id, secret = os.environ.get("GCS_HMAC_KEY_ID"), os.environ.get("GCS_HMAC_SECRET")
        if not key_id or not secret:
            raise SystemExit("gs:// output needs GCS_HMAC_KEY_ID and GCS_HMAC_SECRET set "
                             "(HMAC keys from Cloud Storage > Settings > Interoperability)")
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
        con.execute(f"CREATE OR REPLACE SECRET gcs (TYPE gcs, KEY_ID {q(key_id)}, SECRET {q(secret)})")
    return con


def write_duckdb(con, output: str, start: date, end: date) -> None:
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"ATTACH {q(output)} AS tgt")
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS tgt.raw")
        con.execute("BEGIN TRANSACTION")
        for t in DIMS:
            con.execute(f"CREATE OR REPLACE TABLE tgt.raw.{t} AS SELECT * FROM {t}")
        for t, col in FACTS.items():
            con.execute(f"CREATE TABLE IF NOT EXISTS tgt.raw.{t} AS SELECT * FROM {t} LIMIT 0")
            con.execute(f"DELETE FROM tgt.raw.{t} WHERE {col} BETWEEN DATE '{start}' AND DATE '{end}'")
            con.execute(f"INSERT INTO tgt.raw.{t} SELECT * FROM {t} ORDER BY {SORT_KEYS[t]}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("DETACH tgt")


def write_parquet(con, output: str, start: date, end: date) -> None:
    root = output.rstrip("/")
    remote = is_gcs(root)
    for t in DIMS:
        if not remote:
            Path(root, t).mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY {t} TO {q(f'{root}/{t}/data_0.parquet')} (FORMAT parquet)")
    for t, col in FACTS.items():
        if not remote:
            # Replace the window's partitions, mirroring the DuckDB target's DELETE + INSERT, so
            # a partition that would now be empty can't keep stale files from an earlier run.
            # DuckDB can't delete GCS objects, so there we rely on the fixed filenames below
            # overwriting the same objects (identical for a rerun with the same parameters).
            d = start
            while d <= end:
                shutil.rmtree(Path(root, t, f"{col}={d}"), ignore_errors=True)
                d += timedelta(days=1)
        if con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0:
            continue
        con.execute(f"""
            COPY (SELECT * FROM {t} ORDER BY {SORT_KEYS[t]}) TO {q(f'{root}/{t}')}
            (FORMAT parquet, PARTITION_BY ({col}), FILENAME_PATTERN 'data_{{i}}',
             OVERWRITE_OR_IGNORE true)
        """)


# --------------------------------------------------------------------------- CLI


def env(name: str, default=None):
    value = os.environ.get(f"RETAIL_{name}")
    return default if value is None else value


def env_bool(name: str) -> bool:
    return str(env(name, "")).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value.strip()) if value and value.strip() else None


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mode", choices=["full", "incremental"], default=env("MODE", "full"))
    p.add_argument("--run-date", default=env("RUN_DATE", ""),
                   help="incremental: YYYY-MM-DD (default/empty: today UTC)")
    p.add_argument("--end-date", default=env("END_DATE", ""),
                   help="full: last day of window (default: today UTC)")
    p.add_argument("--days-back", type=int, default=int(env("DAYS_BACK", 365)))
    p.add_argument("--scale", type=float, default=float(env("SCALE", 1.0)))
    p.add_argument("--seed", type=int, default=int(env("SEED", 42)))
    p.add_argument("--target", choices=["duckdb", "parquet"], default=env("TARGET", "duckdb"))
    p.add_argument("--output", default=env("OUTPUT"),
                   help="./retail.duckdb (duckdb target) or ./data (parquet target)")
    p.add_argument("--inventory-frequency", choices=["weekly", "daily"],
                   default=env("INVENTORY_FREQUENCY", "weekly"))
    p.add_argument("--clean", action="store_true", default=env_bool("CLEAN"),
                   help="turn off deliberate messiness (NULL emails, guests, pending, dirty cities)")
    p.add_argument("--upload-duckdb", default=env("UPLOAD_DUCKDB", ""),
                   help="duckdb target only: also upload the file to gs://bucket/path/retail.duckdb")
    args = p.parse_args(argv)
    if not args.output:
        args.output = "./retail.duckdb" if args.target == "duckdb" else "./data"
    if "://" in args.output and not is_gcs(args.output):
        p.error("--output must be a local path or a gs:// URI")
    if is_gcs(args.output) and args.target == "duckdb":
        p.error("DuckDB can't write a .duckdb file in GCS; use --target parquet for gs:// output")
    if args.upload_duckdb and (args.target != "duckdb" or not is_gcs(args.upload_duckdb)):
        p.error("--upload-duckdb needs --target duckdb and a gs:// URI")
    if args.scale <= 0 or args.days_back < 0:
        p.error("--scale must be > 0 and --days-back >= 0")
    return args


def resolve_window(args) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    if args.mode == "incremental":
        d = parse_date(args.run_date) or today
        return d, d
    end = parse_date(args.end_date) or today
    return end - timedelta(days=args.days_back), end


def main(argv=None) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    t0 = time.perf_counter()
    start, end = resolve_window(args)

    estimate = estimate_rows(start, end, args.scale, args.inventory_frequency)
    if estimate > ROW_WARN_THRESHOLD:
        log.warning("this run will generate roughly %s rows", f"{estimate:,}")

    con = connect(args.output)
    generate(con, start, end, args.scale, args.seed, args.clean, args.inventory_frequency)
    if args.target == "duckdb":
        write_duckdb(con, args.output, start, end)
        if args.upload_duckdb:
            upload_to_gcs(args.output, args.upload_duckdb)
    else:
        write_parquet(con, args.output, start, end)
    rows = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    elapsed = time.perf_counter() - t0

    lines = [
        "run summary",
        f"  mode      {args.mode}  (seed={args.seed} scale={args.scale}"
        f"{' clean' if args.clean else ''})",
        f"  window    {start} .. {end}  ({(end - start).days + 1} days)",
        f"  target    {args.target} -> {args.output}",
        *(f"  {t:<20}{n:>10,}" for t, n in rows.items()),
        f"  elapsed   {elapsed:.2f}s",
    ]
    log.info("\n".join(lines))
    return {"mode": args.mode, "start": start, "end": end, "rows": rows, "elapsed": elapsed}


if __name__ == "__main__":
    main()
