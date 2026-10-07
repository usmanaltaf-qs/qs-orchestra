#!/usr/bin/env python3
"""Data quality checks for generate_retail.py output. Exits 1 if any check fails.

  python validate_retail.py --target duckdb --output ./retail.duckdb
  python validate_retail.py --target parquet --output ./data --run-date 2026-10-05
  python validate_retail.py --target parquet --output ./data --hashes   # print table hashes
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

import duckdb

from generate_retail import DIMS, FACTS, TABLES, connect, env, q

PRIMARY_KEYS = {
    "stores": ["store_id"],
    "products": ["product_id"],
    "customers": ["customer_id"],
    "orders": ["order_id"],
    "order_items": ["order_item_id"],
    "returns": ["return_id"],
    "inventory_snapshots": ["snapshot_date", "store_id", "product_id"],
}
# (child, fk column, parent, pk column); NULL FKs are allowed
FOREIGN_KEYS = [
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("orders", "customer_id", "customers", "customer_id"),
    ("orders", "store_id", "stores", "store_id"),
    ("returns", "order_item_id", "order_items", "order_item_id"),
    ("inventory_snapshots", "store_id", "stores", "store_id"),
    ("inventory_snapshots", "product_id", "products", "product_id"),
]
# Facts that can legitimately be empty for a single day (weekly snapshots, low-volume returns)
SPARSE_FACTS = {"returns", "inventory_snapshots"}

# order_item_id = (yyyymmdd * 1_000_000 + seq) * 10 + line, so the order date is recoverable
ITEM_ID_TO_DKEY = "order_item_id // 10000000"


def open_source(target: str, output: str) -> tuple[duckdb.DuckDBPyConnection, set[str]]:
    """Return a connection with one view per table, plus the set of tables that exist."""
    con = connect(output)
    present = set()
    if target == "duckdb":
        con.execute(f"ATTACH {q(output)} AS src (READ_ONLY)")
        existing = {r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE database_name = 'src' AND schema_name = 'raw'"
        ).fetchall()}
        for t in TABLES:
            if t in existing:
                con.execute(f"CREATE VIEW {t} AS SELECT * FROM src.raw.{t}")
                present.add(t)
    else:
        root = output.rstrip("/")
        for t in TABLES:
            pattern = f"{root}/{t}/*.parquet" if t in DIMS else f"{root}/{t}/*/*.parquet"
            if con.execute("SELECT count(*) FROM glob(?)", [pattern]).fetchone()[0]:
                con.execute(f"CREATE VIEW {t} AS SELECT * FROM "
                            f"read_parquet({q(pattern)}, hive_partitioning = true)")
                present.add(t)
    return con, present


def table_hashes(con, tables=TABLES) -> dict[str, tuple[int, str]]:
    """Order-independent (row count, md5) per table, with columns in name order so the same
    data hashes identically whether it was read from DuckDB or Parquet."""
    out = {}
    for t in tables:
        cols = sorted(r[0] for r in con.execute(f"DESCRIBE {t}").fetchall())
        row = ", ".join(f"coalesce({c}::VARCHAR, '<null>')" for c in cols)
        out[t] = con.execute(f"""
            SELECT count(*), coalesce(md5(string_agg(r, chr(10) ORDER BY r)), '')
            FROM (SELECT concat_ws(chr(31), {row}) AS r FROM {t})
        """).fetchone()
    return out


def run_checks(con, present: set[str], run_date: date | None) -> list[tuple[str, str, str]]:
    """Return (status, check, detail) tuples; status is PASS, FAIL, WARN or SKIP."""
    results = []

    def check(name: str, tables: list[str], sql: str, detail: str = "bad rows") -> None:
        if missing := [t for t in tables if t not in present]:
            results.append(("SKIP", name, f"missing {', '.join(missing)}"))
            return
        bad = con.execute(sql).fetchone()[0]
        results.append(("PASS" if bad == 0 else "FAIL", name, f"{bad:,} {detail}"))

    for t in TABLES:
        where = ""
        if run_date and t in FACTS:
            where = f" WHERE {FACTS[t]} = DATE '{run_date}'"
        n = con.execute(f"SELECT count(*) FROM {t}{where}").fetchone()[0] if t in present else 0
        status = "PASS" if n else ("WARN" if run_date and t in SPARSE_FACTS else "FAIL")
        scope = f" on {run_date}" if where else ""
        results.append((status, f"non_empty {t}", f"{n:,} rows{scope}"))

    for t, cols in PRIMARY_KEYS.items():
        key = ", ".join(cols)
        nulls = " OR ".join(f"{c} IS NULL" for c in cols)
        check(f"pk_unique {t}({key})", [t],
              f"SELECT count(*) - count(DISTINCT ({key})) + count(*) FILTER (WHERE {nulls}) FROM {t}",
              "duplicate/null keys")

    for child, fk, parent, pk in FOREIGN_KEYS:
        extra = ""
        if child == "returns":
            # Returns near the start of the data reference orders from the 30-day lookback,
            # which were never written; only enforce the FK where the parent order could exist.
            extra = (f" AND c.{ITEM_ID_TO_DKEY} >= "
                     f"(SELECT strftime(min(order_date), '%Y%m%d')::BIGINT FROM order_items)")
        check(f"fk {child}.{fk} -> {parent}.{pk}", [child, parent], f"""
            SELECT count(*) FROM {child} c ANTI JOIN {parent} p ON c.{fk} = p.{pk}
            WHERE c.{fk} IS NOT NULL{extra}""", "orphans")

    if {"returns", "order_items"} <= present:
        n = con.execute(f"""
            SELECT count(*) FROM returns WHERE {ITEM_ID_TO_DKEY}
                < (SELECT strftime(min(order_date), '%Y%m%d')::BIGINT FROM order_items)
        """).fetchone()[0]
        results.append(("INFO", "returns for orders before the data start", f"{n:,} (FK not checked)"))

    check("line_total = round(quantity * unit_price * (1 - discount_pct), 2)", ["order_items"],
          "SELECT count(*) FROM order_items "
          "WHERE line_total IS DISTINCT FROM round(quantity * unit_price * (1 - discount_pct), 2)")
    check("refund_amount <= line_total", ["returns", "order_items"], """
        SELECT count(*) FROM returns r JOIN order_items i USING (order_item_id)
        WHERE r.refund_amount > i.line_total OR r.refund_amount IS NULL""")
    check("return_date >= order_date", ["returns"], f"""
        SELECT count(*) FROM returns
        WHERE return_date < strptime(({ITEM_ID_TO_DKEY})::VARCHAR, '%Y%m%d')::DATE""")
    check("cancelled orders have no returns", ["returns", "order_items", "orders"], """
        SELECT count(*) FROM returns r JOIN order_items i USING (order_item_id)
        JOIN orders o USING (order_id) WHERE o.status = 'cancelled'""")
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--target", choices=["duckdb", "parquet"], default=env("TARGET", "duckdb"))
    p.add_argument("--output", default=env("OUTPUT"), help="duckdb file or parquet root to check")
    p.add_argument("--run-date", default=env("VALIDATE_RUN_DATE", ""),
                   help="only require non-empty facts for this day")
    p.add_argument("--hashes", action="store_true", help="print row count + md5 per table and exit")
    args = p.parse_args(argv)
    if not args.output:
        args.output = "./retail.duckdb" if args.target == "duckdb" else "./data"
    run_date = date.fromisoformat(args.run_date) if args.run_date.strip() else None

    con, present = open_source(args.target, args.output)
    if args.hashes:
        for t, (n, h) in table_hashes(con, [t for t in TABLES if t in present]).items():
            print(f"{t:<20}{n:>10,}  {h}")
        return 0

    results = run_checks(con, present, run_date)
    for status, name, detail in results:
        print(f"{status:<5} {name}  ({detail})")
    failed = sum(s == "FAIL" for s, _, _ in results)
    passed = sum(s == "PASS" for s, _, _ in results)
    print(f"\n{passed} passed, {failed} failed, "
          f"{sum(s in ('WARN', 'SKIP') for s, _, _ in results)} warnings/skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
