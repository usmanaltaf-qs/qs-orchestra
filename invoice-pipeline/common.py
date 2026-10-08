"""Shared helpers for the invoice pipeline: deterministic randomness, paths, env, money."""
from __future__ import annotations

import hashlib
import logging
import os
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CENT = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


class Rng:
    """Hash-based randomness: u(key, salt) depends only on seed, key and salt, never on call order."""

    def __init__(self, seed: int):
        self.seed = int(seed)

    def u(self, key, salt: str) -> float:
        h = hashlib.sha256(f"{self.seed}|{salt}|{key}".encode()).digest()
        return int.from_bytes(h[:8], "big") / 2**64

    def randint(self, key, salt: str, lo: int, hi: int) -> int:
        return lo + int(self.u(key, salt) * (hi - lo + 1))

    def choice(self, key, salt: str, seq):
        return seq[int(self.u(key, salt) * len(seq))]

    def weighted(self, key, salt: str, items):
        """items: [(value, weight), ...]"""
        total = sum(w for _, w in items)
        x = self.u(key, salt) * total
        for value, w in items:
            x -= w
            if x < 0:
                return value
        return items[-1][0]


def env_root(output: str, env: str) -> Path:
    if output.startswith("gs://"):
        raise SystemExit("GCS output is not wired up yet: use a local --output path")
    return Path(output) / "invoices" / env


def load_env() -> None:
    """Load .env from the repo root (local dev only; Orchestra sets real env vars).

    Accepts ANTHROPIC_KEY as an alias for ANTHROPIC_API_KEY. Never logs values.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env", override=False)
    if not os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("ANTHROPIC_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_KEY"]


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs at INFO; keep it quiet
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_parquet(path: Path, schema: str, rows: list[dict], order_by: str = "1") -> None:
    """Write rows (dicts keyed by column) to one Parquet file with an explicit DuckDB schema,
    e.g. schema="po_number VARCHAR, qty INTEGER"."""
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [c.strip().split()[0] for c in schema.split(", ")]
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({schema})")
    if rows:
        con.executemany(f"INSERT INTO t ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [[r.get(c) for c in cols] for r in rows])
    con.execute(f"COPY (SELECT * FROM t ORDER BY {order_by}) TO '{path}' (FORMAT parquet)")
