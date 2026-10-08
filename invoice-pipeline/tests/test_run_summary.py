"""Run summary: renders, escapes, never raises when email fails or isn't configured."""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_summary as rs  # noqa: E402

DATA = {
    "new": 1, "by_status": {"auto_approved": 0, "exception": 1, "duplicate": 0, "needs_review": 0},
    "extraction": {"ok": 1, "needs_review": 0, "failed": 0}, "new_value": 100.0, "duplicate_value": 0.0,
    "held": [(date(2026, 10, 8), "Bramley & Hart <Ltd>", "INV-1", 100.0, "exception", "price_variance",
              "request credit note", "Line 1 is 5.72 vs 5.38.", "llm", True)],
    "value_on_hold": 100.0, "totals": {"exception": 1},
    "explanations": {"written": 1, "grounded": 1, "cost": 0.01}, "extraction_cost": 0.03,
}
EVALS = [{"suite": "invoices", "run_id": "r1", "passed": False,
          "headline": {"full/claude-opus-5-5: critical_field_acc_scanned": 0.97}}]


def test_render():
    subject, body = rs.render(DATA, EVALS, rs.EPOCH, datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert subject == "Invoices: 1 new, 1 held, £100.00 on hold"
    assert "Bramley &amp; Hart &lt;Ltd&gt;" in body          # invoice text is escaped
    assert "1 new invoice</b>" in body and "1 exception," in body
    assert "critical field acc scanned 97.0%" in body and "failed" in body


def test_not_configured_is_not_an_error(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    assert rs.send("s", "<p>x</p>") is False


def test_send_failure_is_swallowed(monkeypatch):
    for k, v in {"SMTP_HOST": "h", "SMTP_PORT": "587", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
                 "EMAIL_FROM": "f@x", "EMAIL_TO": "a@x, b@x"}.items():
        monkeypatch.setenv(k, v)

    class Boom:
        def __init__(self, *a, **k):
            raise OSError("no network")
    monkeypatch.setattr(rs.smtplib, "SMTP", Boom)
    assert rs.send("s", "<p>x</p>") is False


def test_main_never_raises_and_keeps_state_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(rs, "gather", lambda db, since: DATA)
    monkeypatch.setattr(rs, "send", lambda s, b: False)
    out = rs.main(["--db", "x", "--root", str(tmp_path)])
    assert out["sent"] is False and not (tmp_path / "notify" / "state.json").exists()
    monkeypatch.setattr(rs, "send", lambda s, b: True)
    assert rs.main(["--db", "x", "--root", str(tmp_path)])["sent"] is True
    assert (tmp_path / "notify" / "state.json").exists()        # next run reports only newer invoices
    monkeypatch.setattr(rs, "gather", lambda db, since: 1 / 0)
    assert rs.main(["--db", "x", "--root", str(tmp_path)])["sent"] is False
