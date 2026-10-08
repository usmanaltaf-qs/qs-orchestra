"""Scorer tests: it must catch wrong values, not just agree with itself. No API calls."""
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import evaluate as ev  # noqa: E402
import extract_invoices as ex  # noqa: E402
from common import file_hash  # noqa: E402

TRUTH = {
    "supplier_name": "Calder Valley Trading", "supplier_vat_number": "GB 743 7372 62",
    "invoice_number": "INV-100807", "invoice_date": "2026-10-05", "po_number": "PO-20260924-1107",
    "currency": "GBP",
    "lines": [
        {"description": "Kestrel & Co Luxe Audio", "sku": "SKU-000019", "quantity": 40, "unit_price": 38.37,
         "vat_rate": 0.2, "line_net": 1534.8},
        {"description": "Delivery charge", "sku": None, "quantity": 1, "unit_price": 38.5, "vat_rate": 0.2,
         "line_net": 38.5},
    ],
    "subtotal_net": 1573.3, "vat_total": 314.66, "total_gross": 1887.96,
    "bank_sort_code": "00-00-55", "bank_account": "00006910", "is_copy_or_duplicate_marked": False,
    "template": "A", "scanned": False, "file_stem": "calder_INV-100807_abc",
}


def ext_from(truth, **overrides):
    d = {k: copy.deepcopy(v) for k, v in truth.items() if k in ex.INVOICE_SCHEMA["properties"]}
    d["extraction_notes"] = None
    d.update(overrides)
    return d


def test_normalisation_is_lenient_on_format_only():
    assert ev.field_correct("supplier_vat_number", "GB 743 7372 62", "GB743737262")
    assert ev.field_correct("bank_sort_code", "00-00-55", "000055")
    assert ev.field_correct("supplier_name", "Calder Valley Trading", " calder valley  trading")
    assert ev.field_correct("total_gross", 1887.96, 1887.955)
    assert ev.field_correct("po_number", None, None)
    assert not ev.field_correct("po_number", None, "PO-20260924-1107")      # invented PO
    assert not ev.field_correct("po_number", "PO-20260924-1107", None)      # missed PO
    assert not ev.field_correct("total_gross", 1887.96, 1887.94)
    assert not ev.field_correct("invoice_date", "2026-10-05", "2026-05-10")  # US date order
    assert not ev.field_correct("invoice_number", "INV-100807", "INV-100801")


def test_line_matching_requires_amounts_and_sku():
    t = TRUTH["lines"]
    assert ev.match_lines(t, copy.deepcopy(t)) == 2
    wrong_net = copy.deepcopy(t)
    wrong_net[0]["line_net"] = 1543.80
    assert ev.match_lines(t, wrong_net) == 1
    sku_on_charge = copy.deepcopy(t)
    sku_on_charge[1]["sku"] = "Delivery charge"
    assert ev.match_lines(t, sku_on_charge) == 1
    assert ev.match_lines(t, t[:1]) == 1  # a dropped line is a miss
    reworded = copy.deepcopy(t)
    reworded[0]["description"] = "Kestrel & Co. Luxe Audio"
    assert ev.match_lines(t, reworded) == 2


def write_extraction(root: Path, truth: dict, data: dict | None, status="ok"):
    run_at = datetime(2026, 10, 8, tzinfo=timezone.utc)
    res = {"data": data, "meta": {"model": "m", "input_tokens": 1, "output_tokens": 1, "latency_s": 0.1,
                                  "attempts": 1, "status": status, "error": None}}
    rel = f"inbox/2026/10/05/{truth['file_stem']}.pdf"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(b"%PDF fake " + truth["file_stem"].encode())
    truth = {**truth, "file": rel}
    inv, lines = ex.to_rows(rel, file_hash(root / rel), "r1", run_at, "v1", res)
    ex.write_parquet(root / "extracted/invoices/run=r1.parquet", ex.INVOICE_COLS, [inv])
    ex.write_parquet(root / "extracted/invoice_lines/run=r1.parquet", ex.LINE_COLS, lines)
    (root / "_truth").mkdir(parents=True, exist_ok=True)
    (root / "_truth" / f"{truth['file_stem']}.json").write_text(json.dumps(truth))


def test_end_to_end_perfect(tmp_path):
    write_extraction(tmp_path, TRUTH, ext_from(TRUTH))
    rows, _ = ev.evaluate(tmp_path)
    assert rows[0]["critical_all"] and all(rows[0]["fields"].values())
    assert rows[0]["lines_matched"] == 2


def test_end_to_end_catches_errors(tmp_path):
    bad = ext_from(TRUTH, invoice_number="INV-1OO807", total_gross=1878.96, is_copy_or_duplicate_marked=True)
    bad["lines"][0]["quantity"] = 4
    write_extraction(tmp_path, TRUTH, bad)
    r = ev.evaluate(tmp_path)[0][0]
    wrong = {f for f, ok in r["fields"].items() if not ok}
    assert wrong == {"invoice_number", "total_gross", "is_copy_or_duplicate_marked"}
    assert not r["critical_all"]
    assert r["lines_matched"] == 1


def test_failed_extraction_scores_zero(tmp_path):
    write_extraction(tmp_path, TRUTH, None, status="failed")
    r = ev.evaluate(tmp_path)[0][0]
    assert not any(r["fields"].values()) and r["lines_matched"] == 0
