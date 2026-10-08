#!/usr/bin/env python3
"""Extraction accuracy vs ground truth (the printed values in _truth/).

Quick local check during development. The regression suite in evals/ (agent-evals skill) reuses
the functions here. Only this module and the evals read _truth/.

Reads   {input}/_truth/*.json, {input}/extracted/{invoices,invoice_lines}/*.parquet
Writes  {input}/eval/extraction_accuracy/run=<ts>.parquet   one row per invoice
        {input}/eval/extraction_report_<ts>.md              also printed
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path

import duckdb

from common import write_parquet

FIELDS = ["supplier_name", "supplier_vat_number", "invoice_number", "invoice_date", "po_number", "currency",
          "subtotal_net", "vat_total", "total_gross", "bank_sort_code", "bank_account",
          "is_copy_or_duplicate_marked"]
CRITICAL = ["supplier_name", "invoice_number", "invoice_date", "po_number", "subtotal_net", "vat_total",
            "total_gross"]
MONEY = {"subtotal_net", "vat_total", "total_gross"}
DIGITS_ONLY = {"bank_sort_code", "bank_account"}
NO_SPACE_IDS = {"supplier_vat_number", "invoice_number", "po_number"}
TARGETS = {"digital": 0.98, "scanned": 0.90}
DESC_SIMILARITY = 0.85
# USD per million tokens (input, output). Estimates only; check the current pricing page.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0)}


# ---------------------------------------------------------------------- normalisation + comparison


def norm(field: str, v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if field in MONEY:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    if field == "is_copy_or_duplicate_marked":
        return bool(v)
    s = str(v).strip()
    if field in DIGITS_ONLY:
        return re.sub(r"\D", "", s)
    if field in NO_SPACE_IDS:
        return re.sub(r"\s+", "", s).upper()
    if field == "invoice_date":
        return s[:10]
    return re.sub(r"\s+", " ", s).casefold()


def field_correct(field: str, truth, extracted) -> bool:
    t, e = norm(field, truth), norm(field, extracted)
    if field in MONEY and t is not None and e is not None:
        return abs(t - e) <= Decimal("0.01")
    return t == e


def _num_eq(a, b, tol: str) -> bool:
    if a is None or b is None:
        return a is b
    return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal(tol)


def line_matches(t: dict, e: dict) -> bool:
    return (_num_eq(t["quantity"], e["quantity"], "0.001") and _num_eq(t["unit_price"], e["unit_price"], "0.01")
            and _num_eq(t["line_net"], e["line_net"], "0.01") and _num_eq(t["vat_rate"], e["vat_rate"], "0.001")
            and norm("invoice_number", t["sku"]) == norm("invoice_number", e["sku"]))


def match_lines(truth_lines: list[dict], ext_lines: list[dict]) -> int:
    """Greedy one-to-one match: same amounts, VAT rate and SKU, description similarity >= threshold."""
    unused = list(range(len(ext_lines)))
    matched = 0
    for t in truth_lines:
        best, best_sim = None, DESC_SIMILARITY
        for j in unused:
            e = ext_lines[j]
            if not line_matches(t, e):
                continue
            sim = SequenceMatcher(None, str(t["description"]).casefold(),
                                  str(e["description"] or "").casefold()).ratio()
            if sim >= best_sim:
                best, best_sim = j, sim
        if best is not None:
            unused.remove(best)
            matched += 1
    return matched


def score_invoice(truth: dict, ext: dict | None, ext_lines: list[dict]) -> dict:
    ok = ext is not None and ext["status"] != "failed"
    fields = {f: ok and field_correct(f, truth[f], ext.get(f)) for f in FIELDS}
    matched = match_lines(truth["lines"], ext_lines) if ok else 0
    return {"fields": fields, "critical_all": all(fields[f] for f in CRITICAL),
            "lines_truth": len(truth["lines"]), "lines_extracted": len(ext_lines), "lines_matched": matched}


# ---------------------------------------------------------------------- loading


def load_truth(root: Path) -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text()) for p in (root / "_truth").glob("*.json")}


def load_extracted(root: Path, prompt_version: str | None = None):
    """Latest extraction per file (optionally for one prompt version), with its lines."""
    inv_glob, line_glob = root / "extracted/invoices/*.parquet", root / "extracted/invoice_lines/*.parquet"
    where = f"WHERE prompt_version = '{prompt_version}'" if prompt_version else ""
    con = duckdb.connect()
    rel = con.sql(f"""
        SELECT * FROM read_parquet('{inv_glob}') {where}
        QUALIFY row_number() OVER (PARTITION BY file_path ORDER BY extracted_at DESC) = 1""")
    cols = rel.columns
    invoices = {Path(r[cols.index("file_path")]).stem: dict(zip(cols, r)) for r in rel.fetchall()}
    lines = defaultdict(list)
    if list(root.glob("extracted/invoice_lines/*.parquet")):
        lrel = con.sql(f"SELECT * FROM read_parquet('{line_glob}') ORDER BY line_no")
        lcols = lrel.columns
        for r in lrel.fetchall():
            d = dict(zip(lcols, r))
            lines[(d["file_hash"], d["run_id"])].append(d)
    return invoices, lines


# ---------------------------------------------------------------------- report


def pct(n, d):
    return f"{n / d:.1%}" if d else "n/a"


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    crit = sum(r["fields"][f] for r in rows for f in CRITICAL)
    lt, le, lm = (sum(r[k] for r in rows) for k in ("lines_truth", "lines_extracted", "lines_matched"))
    p, rc = (lm / le if le else 0), (lm / lt if lt else 0)
    return {"n": n, "critical_field_acc": crit / (n * len(CRITICAL)) if n else 0,
            "all_critical_rate": sum(r["critical_all"] for r in rows) / n if n else 0,
            "line_f1": 2 * p * rc / (p + rc) if p + rc else 0, "lines": f"{lm}/{lt}"}


def report(rows: list[dict], meta: dict, worst: int, show_values: bool) -> str:
    seg = lambda pred: [r for r in rows if pred(r)]  # noqa: E731
    out = [f"# Extraction accuracy: {meta['run']}", "",
           f"Model: {', '.join(meta['models'])} · prompt: {', '.join(meta['prompts'])} · invoices: {len(rows)} · "
           f"status: {meta['statuses']} · tokens in/out: {meta['tokens_in']:,}/{meta['tokens_out']:,} · "
           f"est. cost ${meta['cost']:.2f} (${meta['cost'] / max(len(rows), 1):.3f}/invoice)", ""]

    def table(title, groups):
        out.extend([f"## {title}", "",
                    "| segment | n | critical-field acc | all-critical-correct | line F1 (matched/truth) | target |",
                    "|---|---|---|---|---|---|"])
        for name, rs, target in groups:
            s = summarise(rs)
            flag = "" if target is None else ("✅" if s["critical_field_acc"] >= target else "❌") + f" ≥{target:.0%}"
            out.append(f"| {name} | {s['n']} | {s['critical_field_acc']:.1%} | {s['all_critical_rate']:.1%} | "
                       f"{s['line_f1']:.3f} ({s['lines']}) | {flag} |")
        out.append("")

    groups = [("digital", seg(lambda r: not r["scanned"]), TARGETS["digital"]),
              ("scanned", seg(lambda r: r["scanned"]), TARGETS["scanned"])]
    if any(r["scan_profile"] == "hard" for r in rows):
        groups += [("scanned, standard", seg(lambda r: r["scan_profile"] == "standard"), None),
                   ("scanned, hard (stamp over a figure)", seg(lambda r: r["scan_profile"] == "hard"), None)]
    table("Scanned vs digital", groups + [("all", rows, None)])
    table("By template", [(f"template {t}", seg(lambda r, t=t: r["template"] == t), None) for t in "ABCD"]
          + [(f"template {t} scanned", seg(lambda r, t=t: r["template"] == t and r["scanned"]), None)
             for t in "ABCD" if seg(lambda r, t=t: r["template"] == t and r["scanned"])])

    cols = [("digital", lambda r: not r["scanned"]), ("scanned", lambda r: r["scanned"])] + \
           [(t, lambda r, t=t: r["template"] == t) for t in "ABCD"]
    out.extend(["## Per-field accuracy", "", "| field | " + " | ".join(c for c, _ in cols) + " |",
                "|---|" + "---|" * len(cols)])
    for f in FIELDS:
        cells = []
        for _, pred in cols:
            rs = seg(pred)
            cells.append(pct(sum(r["fields"][f] for r in rs), len(rs)))
        out.append(f"| {f}{' *' if f in CRITICAL else ''} | " + " | ".join(cells) + " |")
    out.extend(["", "\\* critical field", ""])

    def badness(r):
        wrong_crit = sum(not r["fields"][f] for f in CRITICAL)
        wrong_other = sum(not r["fields"][f] for f in FIELDS if f not in CRITICAL)
        line_miss = r["lines_truth"] - r["lines_matched"] + (r["lines_extracted"] - r["lines_matched"])
        return (wrong_crit, line_miss, wrong_other)

    bad = sorted((r for r in rows if any(badness(r))), key=badness, reverse=True)[:worst]
    out.extend([f"## Worst {worst} invoices", ""])
    if not bad:
        out.append("No errors.")
    for r in bad:
        wrong = [f for f in FIELDS if not r["fields"][f]]
        kind = "digital" if not r["scanned"] else (
            f"hard scan, stamp over {r['stamp_over']}" if r["scan_profile"] == "hard" else "scanned")
        out.append(f"- **{r['file_stem']}** (template {r['template']}, {kind}, "
                   f"status {r['status']}): wrong fields: {', '.join(wrong) or 'none'}; "
                   f"lines matched {r['lines_matched']}/{r['lines_truth']} (extracted {r['lines_extracted']})")
        if show_values:
            for f in wrong:
                out.append(f"    - {f}: truth `{r['truth'][f]}` · extracted `{r['ext'].get(f) if r['ext'] else None}`")
            for i, d in enumerate(r["line_diffs"]):
                out.append(f"    - line diff {i + 1}: {d}")
    return "\n".join(out) + "\n"


def line_diffs(truth_lines, ext_lines):
    """Per-position differences, for --show-values debugging only."""
    diffs = []
    for i in range(max(len(truth_lines), len(ext_lines))):
        t = truth_lines[i] if i < len(truth_lines) else None
        e = ext_lines[i] if i < len(ext_lines) else None
        if t is None or e is None:
            diffs.append(f"#{i + 1} truth={t and t['description']} extracted={e and e['description']}")
            continue
        bad = [k for k in ("description", "sku", "quantity", "unit_price", "vat_rate", "line_net")
               if (k == "description" and SequenceMatcher(None, str(t[k]).casefold(),
                                                          str(e[k] or "").casefold()).ratio() < DESC_SIMILARITY)
               or (k == "sku" and norm("invoice_number", t[k]) != norm("invoice_number", e[k]))
               or (k not in ("description", "sku") and not _num_eq(t[k], e[k], "0.01"))]
        if bad:
            diffs.append(f"#{i + 1} " + ", ".join(f"{k}: {t[k]} vs {e[k]}" for k in bad))
    return diffs


def evaluate(root: Path, prompt_version: str | None = None, extracted_root: Path | None = None):
    """Score the latest extraction of every file in root/_truth. extracted_root defaults to root
    (evals point it at a per-repeat output folder)."""
    truth = load_truth(root)
    invoices, lines = load_extracted(extracted_root or root, prompt_version)
    rows = []
    for stem, t in sorted(truth.items()):
        ext = invoices.get(stem)
        if ext is None:
            continue  # not extracted yet
        ext_lines = lines.get((ext["file_hash"], ext["run_id"]), [])
        s = score_invoice(t, ext, ext_lines)
        rows.append({**s, "file_stem": stem, "file_hash": ext["file_hash"], "template": t["template"],
                     "scanned": t["scanned"], "scan_profile": t.get("scan_profile"), "stamp_over": t.get("stamp_over"), "status": ext["status"], "model": ext["model"],
                     "prompt_version": ext["prompt_version"], "input_tokens": ext["input_tokens"] or 0,
                     "output_tokens": ext["output_tokens"] or 0, "truth": t, "ext": ext,
                     "line_diffs": line_diffs(t["lines"], ext_lines)})
    return rows, len(truth)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", default="./data/invoices/dev", help="env root with _truth/ and extracted/")
    p.add_argument("--extracted", default=None, help="root holding extracted/ (default: --input)")
    p.add_argument("--prompt-version", default=None, help="only score this prompt version (default: latest)")
    p.add_argument("--worst", type=int, default=5)
    p.add_argument("--show-values", action="store_true",
                   help="print truth vs extracted values for the worst invoices (local debugging only)")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)
    root = Path(args.input)
    rows, n_truth = evaluate(root, args.prompt_version, Path(args.extracted) if args.extracted else None)
    if not rows:
        raise SystemExit("nothing extracted yet")

    run_at = datetime.now(timezone.utc)
    tin, tout = sum(r["input_tokens"] for r in rows), sum(r["output_tokens"] for r in rows)
    cost = sum((PRICES.get(r["model"], (0, 0))[0] * r["input_tokens"] + PRICES.get(r["model"], (0, 0))[1]
                * r["output_tokens"]) / 1e6 for r in rows)
    statuses = {s: sum(r["status"] == s for r in rows) for s in ("ok", "needs_review", "failed")}
    meta = {"run": run_at.strftime("%Y-%m-%d %H:%M UTC") + f" ({len(rows)}/{n_truth} extracted)",
            "models": sorted({r["model"] for r in rows}), "prompts": sorted({r["prompt_version"] for r in rows}),
            "statuses": ", ".join(f"{k} {v}" for k, v in statuses.items()), "tokens_in": tin, "tokens_out": tout,
            "cost": cost}
    md = report(rows, meta, args.worst, args.show_values)
    print(md)
    if args.no_write:
        return rows
    ts = run_at.strftime("%Y%m%dT%H%M%SZ")
    (root / "eval").mkdir(parents=True, exist_ok=True)
    (root / "eval" / f"extraction_report_{ts}.md").write_text(
        report(rows, meta, args.worst, show_values=False))  # never persist values
    schema = ("eval_run_at TIMESTAMPTZ, file_hash VARCHAR, file_stem VARCHAR, template VARCHAR, scanned BOOLEAN, "
              "model VARCHAR, prompt_version VARCHAR, status VARCHAR, critical_all BOOLEAN, lines_truth INTEGER, "
              "lines_extracted INTEGER, lines_matched INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
              + ", ".join(f"{f}_correct BOOLEAN" for f in FIELDS))
    out_rows = [{**{k: r[k] for k in ("file_hash", "file_stem", "template", "scanned", "model", "prompt_version",
                                      "status", "critical_all", "lines_truth", "lines_extracted", "lines_matched",
                                      "input_tokens", "output_tokens")},
                 "eval_run_at": run_at, **{f"{f}_correct": r["fields"][f] for f in FIELDS}} for r in rows]
    write_parquet(root / "eval" / "extraction_accuracy" / f"run={ts}.parquet", schema, out_rows,
                  order_by="file_stem")
    return rows


if __name__ == "__main__":
    main()
