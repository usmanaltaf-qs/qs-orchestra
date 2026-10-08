"""Matching rules eval (dbt models/ap) against injected_problems.

Layer 1 (free, every PR): ground truth is written out as a perfect extraction, the AP models are
built on it in a scratch DuckDB, and failed checks are scored per problem type. Rules on perfect
input should be perfect: gate at precision 1.0 and recall 1.0.

End to end: pass a real extraction folder instead (e.g. an eval repeat's output) to see what
extraction errors do to statuses, including the value of problem invoices wrongly auto-approved.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
DBT_DIR = REPO / "retail-dbt"
sys.path.insert(0, str(REPO / "invoice-pipeline"))

import extract_invoices  # noqa: E402
from common import file_hash, write_parquet  # noqa: E402

# injected problem -> the check that should catch it; line_level ones need a valid PO
PROBLEM_CHECK = {
    "price_variance": ("price_variance", True),
    "qty_over_received": ("qty_over_received", True),
    "extra_charge": ("unmatched_line", True),
    "missing_po": ("missing_po", False),
    "wrong_po": ("po_supplier_mismatch", False),
    "duplicate": ("duplicate", False),
    "arithmetic_error": ("arithmetic", False),
    "unknown_supplier": ("unknown_supplier", False),
}
PO_PROBLEMS = {"missing_po", "wrong_po"}


def truth_as_extraction(eval_root: Path, out: Path) -> None:
    """Write _truth/*.json as if extraction had been perfect."""
    shutil.rmtree(out / "extracted", ignore_errors=True)
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    inv_rows, line_rows, inbox_rows, seen = [], [], [], set()
    for p in sorted((eval_root / "_truth").glob("*.json")):
        t = json.loads(p.read_text())
        data = {k: t[k] for k in extract_invoices.INVOICE_SCHEMA["properties"] if k in t}
        data["extraction_notes"] = None
        res = {"data": data, "meta": {"model": "ground_truth", "input_tokens": 0, "output_tokens": 0,
                                      "latency_s": 0, "attempts": 0, "status": "ok", "error": None}}
        fhash = file_hash(eval_root / t["file"])
        inbox_rows.append({"file_path": t["file"], "file_hash": fhash, "run_id": "truth", "first_seen_at": at})
        if fhash in seen:  # byte-identical re-send: one extraction, like the real extractor
            continue
        seen.add(fhash)
        inv, lines = extract_invoices.to_rows(t["file"], fhash, "truth", at, "truth", res)
        inv_rows.append(inv)
        line_rows.extend(lines)
    write_parquet(out / "extracted/invoices/run=truth.parquet", extract_invoices.INVOICE_COLS, inv_rows)
    write_parquet(out / "extracted/invoice_lines/run=truth.parquet", extract_invoices.LINE_COLS, line_rows)
    write_parquet(out / "extracted/inbox_files/run=truth.parquet", extract_invoices.INBOX_COLS, inbox_rows)


def dbt_build(eval_root: Path, extracted_root: Path, db: Path) -> None:
    dbt = DBT_DIR / ".venv/bin/dbt"
    env = {**os.environ, "INVOICES_SOURCE_URI": str(eval_root), "INVOICES_EXTRACTED_URI": str(extracted_root),
           "DBT_DUCKDB_PATH": str(db), "RETAIL_SOURCE_URI": os.environ.get("RETAIL_SOURCE_URI", str(REPO / "data")),
           "DBT_TARGET": "dev"}
    db.unlink(missing_ok=True)
    cmd = [str(dbt) if dbt.exists() else "dbt", "build", "--select", "+tag:ap", "--indirect-selection", "cautious",
           "--exclude", "assert_ap_matches_ground_truth", "--target", "dev", "--quiet"]
    r = subprocess.run(cmd, cwd=DBT_DIR, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"dbt build failed:\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}")


def score(eval_root: Path, db: Path) -> dict:
    truth = {}
    for p in (eval_root / "_truth").glob("*.json"):
        t = json.loads(p.read_text())
        truth[t["file"]] = t
    con = duckdb.connect(str(db), read_only=True)
    status = {r[0]: r[1:] for r in con.sql(
        "select file_path, status, total_gross, invoice_id from fct_invoice_status").fetchall()}
    failed = defaultdict(set)
    for fp, check in con.sql("""select s.file_path, c.check_name from int_ap__invoice_checks c
                                join fct_invoice_status s using (invoice_id) where c.passed = false""").fetchall():
        failed[fp].add(check)

    tp, fn, fp_ = defaultdict(int), defaultdict(int), defaultdict(int)
    misses, false_pos = [], []
    for f, t in truth.items():
        probs = set(t["injected_problems"]) - {"clean"}
        expected = {PROBLEM_CHECK[p][0] for p in probs
                    if not (PROBLEM_CHECK[p][1] and probs & PO_PROBLEMS)}
        got = failed.get(f, set())
        for c in expected:
            if c in got:
                tp[c] += 1
            else:
                fn[c] += 1
                misses.append(f"{t['file_stem']}: {c} not caught ({','.join(sorted(probs))})")
        for c in got - expected:
            fp_[c] += 1
            false_pos.append(f"{t['file_stem']}: {c} failed ({','.join(sorted(probs)) or 'clean'})")

    checks = sorted(set(tp) | set(fn) | set(fp_) | {c for c, _ in PROBLEM_CHECK.values()})
    per_check = {}
    for c in checks:
        p = tp[c] / (tp[c] + fp_[c]) if tp[c] + fp_[c] else None
        r = tp[c] / (tp[c] + fn[c]) if tp[c] + fn[c] else None
        per_check[c] = {"tp": tp[c], "fn": fn[c], "fp": fp_[c], "precision": p, "recall": r}

    clean = [f for f, t in truth.items() if t["injected_problems"] == ["clean"]]
    problem = [f for f in truth if f not in clean]
    wrongly_approved = [f for f in problem if status[f][0] == "auto_approved"]
    wrongly_held = [f for f in clean if status[f][0] != "auto_approved"]
    return {
        "invoices": len(truth),
        "per_check": per_check,
        "min_precision": min((v["precision"] for v in per_check.values() if v["precision"] is not None), default=1.0),
        "min_recall": min((v["recall"] for v in per_check.values() if v["recall"] is not None), default=1.0),
        "problem_invoices_auto_approved": len(wrongly_approved),
        "value_wrongly_auto_approved_gbp": float(sum(status[f][1] or 0 for f in wrongly_approved)),
        "clean_invoices_held": len(wrongly_held),
        "value_clean_held_gbp": float(sum(status[f][1] or 0 for f in wrongly_held)),
        "misses": misses,
        "false_positives": false_pos,
    }


def report(m: dict, title: str) -> list[str]:
    out = [f"## {title}", "", f"{m['invoices']} invoices. Problem invoices wrongly auto-approved: "
           f"**{m['problem_invoices_auto_approved']} (£{m['value_wrongly_auto_approved_gbp']:,.2f})**. "
           f"Clean invoices held: {m['clean_invoices_held']} (£{m['value_clean_held_gbp']:,.2f}).", "",
           "| check | caught | missed | false positives | precision | recall |", "|---|---|---|---|---|---|"]
    for c, v in m["per_check"].items():
        f = lambda x: "–" if x is None else f"{x:.0%}"  # noqa: E731
        out.append(f"| {c} | {v['tp']} | {v['fn']} | {v['fp']} | {f(v['precision'])} | {f(v['recall'])} |")
    out.append("")
    for label, items in (("Missed", m["misses"]), ("False positives", m["false_positives"])):
        if items:
            out += [f"**{label}:**", "", *[f"- {i}" for i in items[:15]],
                    *([f"- ... and {len(items) - 15} more"] if len(items) > 15 else []), ""]
    return out


def run(eval_root: Path, work: Path, extracted_root: Path | None = None) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    if extracted_root is None:
        truth_as_extraction(eval_root, work)
        extracted_root = work
    db = work / "ap.duckdb"
    dbt_build(eval_root, extracted_root, db)
    return score(eval_root, db)
