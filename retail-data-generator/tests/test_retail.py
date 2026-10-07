"""End-to-end tests: validator, idempotency and determinism at a tiny scale.

Each test drives the real CLIs in a subprocess, exactly as Orchestra would.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from validate_retail import open_source, table_hashes  # noqa: E402

SCALE = "0.05"
END_DATE = "2026-01-31"  # window crosses the Nov/Dec/Jan seasonal factors
DAYS_BACK = "120"
RUN_DATE = "2026-01-18"  # a Sunday, so inventory is written too


def generate(target: str, output: Path, *args: str) -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "generate_retail.py"), "--target", target,
         "--output", str(output), "--scale", SCALE, *args],
        check=True, capture_output=True, text=True,
    )


def full(target: str, output: Path, *args: str) -> None:
    generate(target, output, "--mode", "full", "--end-date", END_DATE, "--days-back", DAYS_BACK, *args)


def validate(target: str, output: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "validate_retail.py"), "--target", target,
         "--output", str(output), *args],
        capture_output=True, text=True,
    )


def hashes(target: str, output: Path) -> dict:
    con, _ = open_source(target, str(output))
    return table_hashes(con)


def out_path(tmp_path: Path, target: str, name: str = "retail") -> Path:
    return tmp_path / (f"{name}.duckdb" if target == "duckdb" else name)


@pytest.mark.parametrize("target", ["duckdb", "parquet"])
def test_full_run_passes_validator(tmp_path, target):
    out = out_path(tmp_path, target)
    full(target, out)
    result = validate(target, out)
    assert result.returncode == 0, result.stdout
    assert "FAIL" not in result.stdout


@pytest.mark.parametrize("target", ["duckdb", "parquet"])
def test_incremental_is_idempotent(tmp_path, target):
    out = out_path(tmp_path, target)
    generate(target, out, "--mode", "incremental", "--run-date", RUN_DATE)
    first = hashes(target, out)
    files = sorted(p.relative_to(out) for p in out.rglob("*.parquet")) if target == "parquet" else None

    generate(target, out, "--mode", "incremental", "--run-date", RUN_DATE)
    second = hashes(target, out)

    assert {t: n for t, (n, _) in first.items()} == {t: n for t, (n, _) in second.items()}
    assert first == second
    if target == "parquet":
        assert sorted(p.relative_to(out) for p in out.rglob("*.parquet")) == files
    assert validate(target, out, "--run-date", RUN_DATE).returncode == 0


def test_full_run_is_deterministic(tmp_path):
    a, b, c = (tmp_path / f"{n}.duckdb" for n in "abc")
    full("duckdb", a, "--seed", "7")
    full("duckdb", b, "--seed", "7")
    full("duckdb", c, "--seed", "8")
    assert hashes("duckdb", a) == hashes("duckdb", b)
    assert hashes("duckdb", a)["orders"] != hashes("duckdb", c)["orders"]


def test_duckdb_and_parquet_targets_hold_identical_data(tmp_path):
    full("duckdb", tmp_path / "retail.duckdb")
    full("parquet", tmp_path / "data")
    assert hashes("duckdb", tmp_path / "retail.duckdb") == hashes("parquet", tmp_path / "data")


def test_incremental_rerun_inside_full_window_changes_nothing(tmp_path):
    # With --clean there is no window-dependent "pending" status, so re-generating one day
    # of a full load must reproduce it exactly: full and incremental share one code path.
    out = tmp_path / "retail.duckdb"
    full("duckdb", out, "--clean")
    before = hashes("duckdb", out)
    generate("duckdb", out, "--mode", "incremental", "--run-date", RUN_DATE, "--clean")
    assert hashes("duckdb", out) == before


def test_validator_catches_broken_foreign_key(tmp_path):
    out = tmp_path / "retail.duckdb"
    full("duckdb", out)
    with duckdb.connect(str(out)) as con:
        con.execute("DELETE FROM raw.orders WHERE order_id = (SELECT min(order_id) FROM raw.orders)")
    result = validate("duckdb", out)
    assert result.returncode == 1
    assert "FAIL  fk order_items.order_id -> orders.order_id" in result.stdout


def test_gcs_output_fails_fast_without_hmac_keys(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GCS_HMAC_")}
    result = subprocess.run(
        [sys.executable, str(ROOT / "generate_retail.py"), "--target", "parquet",
         "--output", "gs://example-bucket/retail/dev", "--mode", "incremental"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode != 0
    assert "GCS_HMAC_KEY_ID and GCS_HMAC_SECRET" in result.stderr


def test_duckdb_target_rejects_gcs_output():
    result = subprocess.run(
        [sys.executable, str(ROOT / "generate_retail.py"), "--target", "duckdb",
         "--output", "gs://example-bucket/retail.duckdb"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "use --target parquet" in result.stderr


def test_upload_duckdb_requires_duckdb_target_and_gcs_uri():
    for args in (["--target", "parquet", "--upload-duckdb", "gs://example-bucket/retail.duckdb"],
                 ["--target", "duckdb", "--upload-duckdb", "./retail.duckdb"]):
        result = subprocess.run(
            [sys.executable, str(ROOT / "generate_retail.py"), *args],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "--upload-duckdb needs --target duckdb" in result.stderr
