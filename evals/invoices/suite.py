"""Invoice extraction suite: regenerate the eval set from eval_set.yml, extract it with the
production extractor (once per repeat, into a fresh folder so idempotency doesn't skip it), and
score it with invoice-pipeline/evaluate.py against the eval set's ground truth.

Matching (dbt rules on ground-truth input) and explanation stages get added here as they're built.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PIPELINE = REPO / "invoice-pipeline"
sys.path.insert(0, str(PIPELINE))

import evaluate  # noqa: E402
import extract_invoices  # noqa: E402
import generate_invoices  # noqa: E402

EVAL_SET = Path(__file__).resolve().parent / "eval_set.yml"
HEADLINE = ["critical_field_acc_digital", "critical_field_acc_scanned", "critical_field_acc_scanned_standard",
            "critical_field_acc_scanned_hard", "all_critical_rate", "line_f1", "field_acc_all",
            "needs_review", "failed", "cost_usd", "cost_per_invoice_usd", "mean_latency_s"]


def load_config() -> dict:
    return yaml.safe_load(EVAL_SET.read_text())


def generate(cfg: dict, subset: str) -> Path:
    sub, comp = cfg["subsets"][subset], cfg["composition"]
    out = REPO / sub["output"]
    argv = ["--mode", "full", "--env", "eval", "--seed", str(cfg["seed"]), "--run-date", cfg["run_date"],
            "--days-back", str(sub["days_back"]), "--invoices-per-day", str(sub["invoices_per_day"]),
            "--scanned-rate", str(comp["scanned_rate"]), "--hard-scan-rate", str(comp["hard_scan_rate"]),
            "--min-per-problem", str(comp["min_per_problem"]),
            "--problem-rates", json.dumps(comp["problems"]),
            "--source", os.environ.get("RETAIL_SOURCE_URI", str(REPO / "data")), "--output", str(out)]
    if comp.get("balance_templates"):
        argv.append("--balance-templates")
    root = out / "invoices" / "eval"
    # Regenerating rewrites the inbox, so skip it when an identical set is already there
    # (another suite may be reading it). The generator is deterministic, so this is safe.
    code = b"".join(p.read_bytes() for p in sorted([PIPELINE / "generate_invoices.py",
                                                    *(PIPELINE / "templates").glob("*.py")]))
    fingerprint = hashlib.sha256(json.dumps(argv).encode() + code).hexdigest()
    marker = root / ".eval_set_fingerprint"
    if not (marker.exists() and marker.read_text() == fingerprint and (root / "inbox").exists()):
        generate_invoices.main(argv)
        marker.write_text(fingerprint)
    return root


def run_extraction(eval_root: Path, out: Path, model: str, concurrency: int) -> None:
    os.environ["ANTHROPIC_MODEL"] = model
    extract_invoices.main(["--input", str(eval_root), "--output", str(out), "--concurrency", str(concurrency),
                           "--retry-failed"])
    # one pass of retries for anything rate-limited
    extract_invoices.main(["--input", str(eval_root), "--output", str(out), "--concurrency", "1",
                           "--retry-failed"])


def acc(rows, fields=evaluate.CRITICAL):
    return sum(r["fields"][f] for r in rows for f in fields) / (len(rows) * len(fields)) if rows else None


def score(eval_root: Path, extracted: Path) -> tuple[dict, list[dict]]:
    rows, n_truth = evaluate.evaluate(eval_root, extracted_root=extracted)
    if len(rows) < n_truth:
        raise SystemExit(f"only {len(rows)}/{n_truth} eval invoices were extracted")
    s = evaluate.summarise(rows)
    cost = sum((evaluate.PRICES.get(r["model"], (0, 0))[0] * r["input_tokens"]
                + evaluate.PRICES.get(r["model"], (0, 0))[1] * r["output_tokens"]) / 1e6 for r in rows)
    status = Counter(r["status"] for r in rows)
    m = {
        "invoices": len(rows),
        "critical_field_acc_digital": acc([r for r in rows if not r["scanned"]]),
        "critical_field_acc_scanned": acc([r for r in rows if r["scanned"]]),
        "critical_field_acc_scanned_standard": acc([r for r in rows if r["scan_profile"] == "standard"]),
        "critical_field_acc_scanned_hard": acc([r for r in rows if r["scan_profile"] == "hard"]),
        "all_critical_rate": s["all_critical_rate"],
        "line_f1": s["line_f1"],
        "field_acc_all": acc(rows, evaluate.FIELDS),
        "needs_review": status["needs_review"],
        "failed": status["failed"],
        "cost_usd": cost,
        "cost_per_invoice_usd": cost / len(rows),
        "mean_latency_s": sum(r["ext"]["latency_s"] or 0 for r in rows) / len(rows),
    }
    for t in "ABCD":
        m[f"critical_field_acc_template_{t}"] = acc([r for r in rows if r["template"] == t])
    return m, rows


def worst_examples(rows: list[dict], n: int = 10) -> list[str]:
    """Field names only, never values: results are shared (PR comments, GCS)."""
    def badness(r):
        return (sum(not r["fields"][f] for f in evaluate.CRITICAL),
                r["lines_truth"] - r["lines_matched"] + r["lines_extracted"] - r["lines_matched"],
                sum(not v for v in r["fields"].values()))
    out = []
    for r in sorted((r for r in rows if any(badness(r))), key=badness, reverse=True)[:n]:
        kind = "digital" if not r["scanned"] else (f"hard scan, stamp over {r['stamp_over']}"
                                                   if r["scan_profile"] == "hard" else "scanned")
        wrong = [f for f, ok in r["fields"].items() if not ok]
        out.append(f"- {r['file_stem']} (template {r['template']}, {kind}, status {r['status']}): "
                   f"wrong {', '.join(wrong) or 'none'}; lines {r['lines_matched']}/{r['lines_truth']} "
                   f"(extracted {r['lines_extracted']})")
    return out
