"""Extraction tests with the API stubbed out: response handling, review flags, idempotency."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import extract_invoices as ex  # noqa: E402

GOOD = {"supplier_name": "S", "supplier_vat_number": None, "invoice_number": "1", "invoice_date": "2026-10-05",
        "po_number": None, "currency": "GBP",
        "lines": [{"description": "x", "sku": None, "quantity": 1, "unit_price": 1, "vat_rate": 0.2, "line_net": 1}],
        "subtotal_net": 1, "vat_total": 0.2, "total_gross": 1.2, "bank_sort_code": None, "bank_account": None,
        "is_copy_or_duplicate_marked": False, "extraction_notes": None}


def resp(text=None, stop="end_turn"):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return SimpleNamespace(stop_reason=stop, content=content, model="m",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def test_parse_response():
    assert ex.parse_response(resp(json.dumps(GOOD)))["invoice_number"] == "1"
    for r, msg in [(resp(stop="refusal"), "refusal"), (resp("{}", stop="max_tokens"), "max_tokens"),
                   (resp("{not json"), "invalid json"), (resp("{}"), "missing keys"), (resp(), "no text")]:
        with pytest.raises(ex.ExtractionError, match=msg):
            ex.parse_response(r)


def test_quality_status():
    assert ex.quality_status(GOOD) == ("ok", None)
    assert ex.quality_status({**GOOD, "total_gross": None})[0] == "needs_review"
    assert "invoice_date_unparseable" in ex.quality_status({**GOOD, "invoice_date": "05/10/2026"})[1]
    assert "no_lines" in ex.quality_status({**GOOD, "lines": []})[1]
    # arithmetic is the rules' job, not a review flag
    assert ex.quality_status({**GOOD, "total_gross": 999})[0] == "ok"


def test_retries_once_on_bad_response(monkeypatch, tmp_path):
    calls = iter([resp("{oops"), resp(json.dumps(GOOD))])
    monkeypatch.setattr(ex, "call_claude", lambda *a: next(calls))
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF")
    out = ex.extract_one(None, "m", "low", "sys", pdf)
    assert out["meta"]["status"] == "ok" and out["meta"]["attempts"] == 2
    assert out["meta"]["input_tokens"] == 20  # both attempts are billed and recorded


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    monkeypatch.setattr(ex, "load_prompt", lambda v: f"prompt {v}")
    calls = []

    def fake(client, model, effort, system, path):
        calls.append(path.name)
        return {"data": GOOD, "meta": {"model": model, "input_tokens": 1, "output_tokens": 1, "latency_s": 0,
                                       "attempts": 1, "status": "ok", "error": None}}
    monkeypatch.setattr(ex, "extract_one", fake)
    d = tmp_path / "inbox" / "2026" / "10" / "05"
    d.mkdir(parents=True)
    for i in range(3):
        (d / f"inv{i}.pdf").write_bytes(f"%PDF {i}".encode())
    (d / "copy_of_inv0.pdf").write_bytes(b"%PDF 0")  # byte-identical to inv0
    return tmp_path, calls


def test_idempotent_by_hash(inbox):
    root, calls = inbox
    ex.main(["--input", str(root)])
    assert len(calls) == 3  # identical bytes extracted once
    ex.main(["--input", str(root)])
    assert len(calls) == 3  # nothing new
    ex.main(["--input", str(root), "--prompt-version", "v2"])
    assert len(calls) == 3  # new prompt version is not an accident...
    ex.main(["--input", str(root), "--prompt-version", "v2", "--reprocess"])
    assert len(calls) == 6  # ...only a deliberate re-extract
    ex.main(["--input", str(root), "--prompt-version", "v2", "--reprocess"])
    assert len(calls) == 6
