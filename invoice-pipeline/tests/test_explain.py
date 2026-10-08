"""Explanation tests with the API stubbed: grounding, fallback, non-blocking, caching."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import explain_exceptions as ee  # noqa: E402
from grounding import ungrounded  # noqa: E402

CASE = {"invoice_id": "abc", "check_hash": "h1", "status": "exception", "failed": ["price_variance"],
        "checks": [("price_variance", "line 8: 30.24", "line 8: 31.38", "tolerance 2.00% and £0.05")],
        "text": "FAILED CHECKS\nprice_variance | line 8: 30.24 | line 8: 31.38\n"
                "INVOICE LINES\n8 | Bramble Coastal Bedding | 45 | 31.38 | 30.24 | 1.14 | 3.8% | 51.30\n"
                "INVOICE\nINV-100804 | 2026-10-05 | PO-20260920-1504 | 16409.47 | 3262.90 | 19672.37"}


def test_grounding():
    src = CASE["text"]
    assert ungrounded("Line 8 is billed at £31.38 against £30.24 on the PO, 3.8% over (£51.30).", src) == []
    assert ungrounded("Invoice INV-100804 dated 5 Oct 2026 totals £19,672.37.", src) == []
    assert ungrounded("Line 8 is £1.14 over, about 4% on 45 units, so £51.3 in total.", src) == ["4"]
    assert ungrounded("Overcharged by £52.00.", src) == ["52.00"]


def resp(payload, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, model="m", usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                           content=[SimpleNamespace(type="text", text=json.dumps(payload))])


@pytest.mark.parametrize("payload,source,reason", [
    ({"summary": "Line 8 is £31.38 vs £30.24 on the PO (3.8% over).", "suggested_action": "request credit note",
      "details": ["line 8: 31.38 vs 30.24"]}, "llm", None),
    ({"summary": "Overcharged by £99.", "suggested_action": "request credit note", "details": []},
     "fallback", "ungrounded"),
])
def test_explain_grounded_or_fallback(monkeypatch, payload, source, reason):
    monkeypatch.setattr(ee, "call_claude", lambda *a: resp(payload))
    out = ee.explain(None, "m", "low", "sys", CASE)
    assert out["source"] == source and out["fallback_reason"] == reason
    if source == "fallback":
        assert out["suggested_action"] == "request credit note"  # from the rules, not the model
        assert ungrounded(out["summary"], CASE["text"]) == []    # the template is grounded too


def test_api_failure_falls_back(monkeypatch):
    def boom(*a):
        raise anthropic.APIConnectionError(request=None)
    monkeypatch.setattr(ee, "call_claude", boom)
    out = ee.explain(None, "m", "low", "sys", CASE)
    assert out["source"] == "fallback" and out["fallback_reason"].startswith("error")


def test_rule_action_priority():
    assert ee.rule_action(["price_variance", "duplicate"]) == "reject duplicate"
    assert ee.rule_action(["unknown_supplier", "bank_details_changed"]) == "fix master data"
    assert ee.rule_action(["qty_over_received"]) == "request credit note"
    assert ee.rule_action(["unmatched_line", "price_variance"]) == "query supplier"


def test_main_caches_by_check_hash_and_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(ee, "read_cases", lambda db: [CASE])
    monkeypatch.setenv("ANTHROPIC_MODEL", "m")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    calls = []
    good = {"summary": "Line 8 is £31.38 vs £30.24 on the PO.", "suggested_action": "request credit note",
            "details": []}

    def flaky(*a):
        calls.append(1)
        if len(calls) == 1:
            raise anthropic.APIConnectionError(request=None)
        return resp(good)
    monkeypatch.setattr(ee, "call_claude", flaky)
    args = ["--db", "x", "--output", str(tmp_path)]
    assert ee.main(args)["fallback"] == 1          # API error: templated note, no exception
    assert ee.main(args)["llm"] == 1               # error fallbacks are retried next run
    assert ee.main(args)["to_explain"] == 0        # a real explanation is cached by check_hash
    assert len(calls) == 2


def test_sample_covers_types_first():
    cases = [{"failed": f, "id": i} for i, f in enumerate(
        [["price_variance"], ["price_variance"], ["missing_po"], ["duplicate"], ["missing_po"]])]
    assert [c["id"] for c in ee.sample(cases, 3)] == [0, 2, 3]
    assert [c["id"] for c in ee.sample(cases, 4)] == [0, 2, 3, 1]
