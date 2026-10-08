"""Generator tests. Need the retail dims locally (run the retail local loop first)."""
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import generate_invoices  # noqa: E402

SOURCE = Path(__file__).resolve().parents[2] / "data"
pytestmark = pytest.mark.skipif(not (SOURCE / "products").exists(), reason="no retail data in ./data")

ARGS = ["--mode", "full", "--run-date", "2026-10-07", "--days-back", "3", "--invoices-per-day", "10",
        "--source", str(SOURCE)]


def run(tmp_path, name):
    out = tmp_path / name
    generate_invoices.main(ARGS + ["--output", str(out)])
    return out / "invoices" / "dev"


def digest(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    return run(tmp_path_factory.mktemp("gen"), "a")


def truths(root):
    return [json.loads(p.read_text()) for p in (root / "_truth").glob("*.json")]


def test_deterministic(root, tmp_path):
    assert digest(root) == digest(run(tmp_path, "b"))


def test_every_problem_type_present(root):
    seen = {p for t in truths(root) for p in t["injected_problems"]}
    assert set(generate_invoices.PROBLEM_RATES) <= seen


def test_every_pdf_has_truth(root):
    pdfs = {p.stem for p in (root / "inbox").rglob("*.pdf")}
    assert pdfs == {t["file_stem"] for t in truths(root)}


def test_arithmetic_only_broken_when_injected(root):
    for t in truths(root):
        d = lambda x: Decimal(str(x))  # noqa: E731
        ok = all(d(ln["quantity"]) * d(ln["unit_price"]) == d(ln["line_net"]) for ln in t["lines"])
        ok &= sum(d(ln["line_net"]) for ln in t["lines"]) == d(t["subtotal_net"])
        ok &= d(t["subtotal_net"]) + d(t["vat_total"]) == d(t["total_gross"])
        by_rate = {}
        for ln in t["lines"]:
            by_rate[ln["vat_rate"]] = by_rate.get(ln["vat_rate"], 0) + d(ln["line_net"])
        vat = sum((n * d(r)).quantize(Decimal("0.01")) for r, n in by_rate.items())
        ok &= vat == d(t["vat_total"])
        assert ok == ("arithmetic_error" not in t["injected_problems"]), t["file_stem"]


def test_printed_po_matches_system(root):
    pos = dict(duckdb.sql(
        f"SELECT po_number, supplier_id FROM read_parquet('{root}/system/purchase_orders/*/*.parquet')").fetchall())
    for t in truths(root):
        probs = set(t["injected_problems"])
        if "missing_po" in probs:
            assert t["po_number"] is None
        elif "wrong_po" in probs:
            assert t["po_number"] in pos and pos[t["po_number"]] != t["supplier_id"]
        else:
            assert t["po_number"] == t["po_number_actual"] and t["po_number"] in pos


def test_duplicates_repeat_an_existing_invoice(root):
    by_stem = {t["file_stem"]: t for t in truths(root)}
    for t in by_stem.values():
        if "duplicate" in t["injected_problems"]:
            orig = by_stem[t["duplicate_of_file_stem"]]
            assert orig["invoice_number"] == t["invoice_number"]
            assert orig["total_gross"] == t["total_gross"]
