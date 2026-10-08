#!/usr/bin/env python3
"""Explain held invoices for the AP reviewer: one Claude call per invoice, no tools.

Reads the dbt AP models (fct_invoice_status, int_ap__invoice_checks, matched lines, PO and GRN
lines) from the warehouse, and for every unresolved invoice (exception, duplicate, needs_review)
whose current check results (check_hash) have no explanation yet, writes:
  summary (<= 2 sentences), suggested_action, details
to {output}/explanations/run=<run_id>.parquet. dbt picks them up on its next run.

- suggested_action is advice for the reviewer. It never changes a status.
- Grounding: every number in the summary must appear in the input tables. If not, or if the call
  fails, a templated note built from the check rows is used instead (source = 'fallback').
- Non-blocking: LLM failures never fail the task (exit 0). Only unreadable inputs exit non-zero.
- Logs invoice-id prefixes and counts only, never invoice content.
Spec: .claude/skills/invoice-processing/references/matching.md
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import anthropic
import duckdb

import storage
from common import REPO_ROOT, load_env, setup_logging, write_parquet
from grounding import ungrounded
from storage import join

log = setup_logging("explain_exceptions")

PROMPTS = Path(__file__).resolve().parent / "prompts"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
ACTIONS = ["request credit note", "approve with note", "query supplier", "reject duplicate", "fix master data"]
UNRESOLVED = ("exception", "duplicate", "needs_review")

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "at most two sentences"},
        "suggested_action": {"type": "string", "enum": ACTIONS},
        "details": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "suggested_action", "details"],
    "additionalProperties": False,
}

# Templated fallback: label per check, and the action the rules imply (first match wins).
CHECK_LABEL = {
    "duplicate": "Duplicate invoice", "unknown_supplier": "Supplier not in master data",
    "missing_po": "No PO number on the invoice", "po_not_found": "PO number not found",
    "po_supplier_mismatch": "PO belongs to a different supplier", "price_variance": "Unit price above PO",
    "qty_over_received": "Invoiced quantity above quantity received", "unmatched_line": "Line not on the PO",
    "arithmetic": "Invoice totals don't add up", "vat_rate": "Unexpected VAT rate",
    "bank_details_changed": "Bank details differ from master data", "extraction": "Invoice couldn't be read reliably",
}
ACTION_PRIORITY = [
    ("duplicate", "reject duplicate"), ("unknown_supplier", "fix master data"),
    ("bank_details_changed", "query supplier"), ("missing_po", "query supplier"), ("po_not_found", "query supplier"),
    ("po_supplier_mismatch", "query supplier"), ("unmatched_line", "query supplier"), ("arithmetic", "query supplier"),
    ("extraction", "query supplier"), ("vat_rate", "query supplier"),
    ("price_variance", "request credit note"), ("qty_over_received", "request credit note"),
]

OUT_COLS = ("invoice_id VARCHAR, check_hash VARCHAR, run_id VARCHAR, created_at TIMESTAMPTZ, status VARCHAR, "
            "failed_checks VARCHAR, summary VARCHAR, suggested_action VARCHAR, details VARCHAR, source VARCHAR, "
            "fallback_reason VARCHAR, ungrounded_numbers VARCHAR, model VARCHAR, prompt_version VARCHAR, "
            "input_tokens INTEGER, output_tokens INTEGER, latency_s DOUBLE")


# ---------------------------------------------------------------------- inputs


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral() else str(v)
    return str(v)


def table(cols: list[str], rows: list[tuple]) -> str:
    return "\n".join([" | ".join(cols)] + [" | ".join(fmt(v) for v in r) for r in rows]) if rows else "(none)"


def rule_action(failed: list[str]) -> str:
    for check, action in ACTION_PRIORITY:
        if check in failed:
            return action
    return "query supplier"


def load_cases(con, statuses=UNRESOLVED) -> list[dict]:
    """One dict per unresolved invoice with everything the prompt needs, as text tables."""
    placeholders = ", ".join(f"'{s}'" for s in statuses)
    invoices = con.sql(f"""
        select s.invoice_id, s.check_hash, s.final_status, i.supplier_name, i.supplier_vat_number,
               i.invoice_number, i.invoice_date, i.po_number, i.subtotal_net, i.vat_total, i.total_gross
        from fct_invoice_status s join int_ap__invoices i using (invoice_id)
        where s.final_status in ({placeholders})
        order by s.received_date, s.invoice_id""").fetchall()
    checks = {}
    for inv_id, name, exp, act, det in con.sql("""
            select invoice_id, check_name, expected, actual, detail from int_ap__invoice_checks
            where passed = false order by invoice_id, check_name""").fetchall():
        checks.setdefault(inv_id, []).append((name, exp, act, det))
    lines = {}
    for r in con.sql("""
            select invoice_id, invoice_line_no, description, sku, quantity, unit_price,
                   cast(round(vat_rate * 100) as integer) || '%' as vat, line_net, po_line_no, po_unit_cost,
                   unit_price - po_unit_cost as price_diff,
                   case when po_unit_cost > 0 then round((unit_price - po_unit_cost) / po_unit_cost * 100, 1) end
                       || '%' as price_diff_pct,
                   round((unit_price - po_unit_cost) * quantity, 2) as price_difference_value,
                   qty_ordered, qty_received, quantity - qty_received as qty_over_received,
                   round((quantity - qty_received) * unit_price, 2) as over_received_value,
                   round(quantity * unit_price, 2) as qty_x_price
            from int_ap__invoice_lines_matched order by invoice_id, invoice_line_no""").fetchall():
        lines.setdefault(r[0], []).append(r[1:])
    po_lines = {}
    for r in con.sql("select po_number, po_line_no, sku, description, qty_ordered, unit_cost, "
                     "cast(round(vat_rate * 100) as integer) || '%' from stg_ap__po_lines "
                     "order by po_number, po_line_no").fetchall():
        po_lines.setdefault(r[0], []).append(r[1:])
    grn = {}
    for r in con.sql("select g.po_number, g.grn_number, g.received_date, l.po_line_no, l.qty_received "
                     "from stg_ap__goods_receipts g join stg_ap__grn_lines l using (grn_number) "
                     "order by g.po_number, l.po_line_no").fetchall():
        grn.setdefault(r[0], []).append(r[1:])

    cases = []
    for inv_id, chash, status, sup, vat, num, inv_date, po, net, vat_total, gross in invoices:
        failed = checks.get(inv_id, [])
        text = "\n\n".join([
            "FAILED CHECKS\n" + table(["check", "expected", "actual", "detail"], failed),
            "INVOICE\n" + table(["supplier", "VAT number", "invoice number", "invoice date", "PO number",
                                 "subtotal", "VAT", "total"],
                                [(sup, vat, num, inv_date, po, net, vat_total, gross)]),
            "INVOICE LINES (matched to the PO)\n" + table(
                ["line", "description", "sku", "qty", "unit price", "VAT", "line net", "PO line", "PO unit price",
                 "price difference", "price difference %", "price difference x qty", "PO qty", "qty received",
                 "qty over received", "value over received", "qty x unit price"], lines.get(inv_id, [])),
            f"PO LINES ({po or 'no PO'})\n" + table(["line", "sku", "description", "qty", "unit price", "VAT"],
                                                     po_lines.get(po, [])),
            "GOODS RECEIVED\n" + table(["GRN", "received", "PO line", "qty received"], grn.get(po, [])),
        ])
        cases.append({"invoice_id": inv_id, "check_hash": chash, "status": status,
                      "failed": [c[0] for c in failed], "checks": failed, "text": text})
    return cases


# ---------------------------------------------------------------------- explaining


def fallback(case: dict, reason: str) -> dict:
    parts = [f"{CHECK_LABEL.get(n, n)}: expected {e or '-'}, actual {a or '-'}." for n, e, a, _ in case["checks"]]
    summary = " ".join(parts[:2]) or "Held for review."
    return {"summary": summary, "suggested_action": rule_action(case["failed"]), "details": parts,
            "source": "fallback", "fallback_reason": reason}


def call_claude(client, model: str, effort: str, system: str, text: str):
    return client.beta.messages.create(
        model=model, max_tokens=4000, betas=[FALLBACK_BETA], fallbacks="default", system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": text}],
    )


def explain(client, model: str, effort: str, system: str, case: dict) -> dict:
    t0 = time.perf_counter()
    meta = {"model": model, "input_tokens": 0, "output_tokens": 0}
    if case["status"] == "needs_review":  # extraction failed: nothing reliable to explain from
        out = fallback(case, "needs_review")
    else:
        try:
            resp = call_claude(client, model, effort, system, case["text"])
            meta = {"model": resp.model, "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens}
            if resp.stop_reason != "end_turn":
                raise ValueError(f"stop_reason {resp.stop_reason}")
            data = json.loads(next(b.text for b in resp.content if b.type == "text"))
            bad = ungrounded(data["summary"] + " " + " ".join(data["details"]), case["text"])
            if bad:
                out = {**fallback(case, "ungrounded"), "ungrounded_numbers": ",".join(bad)}
            else:
                out = {**data, "source": "llm", "fallback_reason": None}
        except (anthropic.APIError, ValueError, KeyError, StopIteration, json.JSONDecodeError) as e:
            out = fallback(case, f"error: {type(e).__name__}")
    return {**out, **meta, "latency_s": round(time.perf_counter() - t0, 2)}


def read_cases(db: str) -> list[dict]:
    # MotherDuck (md:<database>, token from MOTHERDUCK_TOKEN) or a local DuckDB file
    con = duckdb.connect(db) if db.startswith("md:") else duckdb.connect(db, read_only=True)
    try:
        return load_cases(con)
    finally:
        con.close()


def sample(cases: list[dict], n: int) -> list[dict]:
    """Up to n cases, one per distinct set of failed checks first, so a small sample still covers
    the exception types. Deterministic (input order)."""
    picked, seen = [], set()
    for c in cases:
        key = ",".join(c["failed"])
        if key not in seen:
            seen.add(key)
            picked.append(c)
    picked += [c for c in cases if c not in picked]
    return picked[:n]


def existing(out_root: str) -> set[tuple[str, str]]:
    """(invoice_id, check_hash) already explained. Fallbacks caused by API errors (e.g. rate
    limits) don't count, so the next run retries them."""
    prefix = join(out_root, "explanations")
    if not storage.list_files(prefix, ".parquet"):
        return set()
    return set(storage.duck(prefix).sql(f"""select invoice_id, check_hash from read_parquet('{prefix}/*.parquet')
                                           where coalesce(fallback_reason, '') not like 'error%'""").fetchall())


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=os.environ.get("AP_WAREHOUSE", str(REPO_ROOT / "retail-dbt" / "dev.duckdb")),
                   help="DuckDB file or md:<database> holding the AP models")
    p.add_argument("--output", default=os.environ.get("INVOICES_OUTPUT", "./data/invoices/dev"),
                   help="env root; explanations go to {output}/explanations/")
    p.add_argument("--prompt-version", default=os.environ.get("EXPLAIN_PROMPT_VERSION", "v1"))
    p.add_argument("--effort", default=os.environ.get("EXPLAIN_EFFORT", "medium"),
                   choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--reprocess", action="store_true", help="explain again even if (invoice, check_hash) is done")
    p.add_argument("--limit", type=int, default=0,
                   help="explain at most N (one per exception type first); 0 = all. For cheap checks and evals")
    p.add_argument("--dry-run", action="store_true", help="count what would be explained; no API calls, no writes")
    p.add_argument("--print", action="store_true", dest="show",
                   help="print explanations to the terminal (local debugging only: contains invoice content)")
    p.add_argument("--concurrency", type=int, default=2)
    return p.parse_args(argv)


def main(argv=None) -> dict:
    args = parse_args(argv)
    load_env()
    out_root = str(args.output)
    cases = read_cases(args.db)
    done = set() if args.reprocess else existing(out_root)
    todo = [c for c in cases if (c["invoice_id"], c["check_hash"]) not in done]
    if args.limit:
        todo = sample(todo, args.limit)
    log.info("unresolved invoices: %d, to explain: %d", len(cases), len(todo))
    if args.dry_run or not todo:
        return {"unresolved": len(cases), "to_explain": len(todo)}

    model = os.environ.get("ANTHROPIC_MODEL", "").strip()
    system = (PROMPTS / f"explain_{args.prompt_version}.md").read_text()
    client = anthropic.Anthropic(max_retries=8) if model and os.environ.get("ANTHROPIC_API_KEY") else None
    if client is None:
        log.warning("no ANTHROPIC_MODEL/ANTHROPIC_API_KEY: using templated explanations only")

    def run(case):
        if client is None:
            return {**fallback(case, "no_api_key"), "model": None, "input_tokens": 0, "output_tokens": 0,
                    "latency_s": 0}
        return explain(client, model, args.effort, system, case)

    run_at = datetime.now(timezone.utc)
    run_id = run_at.strftime("%Y%m%dT%H%M%SZ")
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(run, todo))
    rows = []
    for case, r in zip(todo, results):
        log.info("invoice %s source=%s action=%s reason=%s tokens=%d/%d", case["invoice_id"][:12], r["source"],
                 r["suggested_action"], r.get("fallback_reason"), r["input_tokens"], r["output_tokens"])
        rows.append({"invoice_id": case["invoice_id"], "check_hash": case["check_hash"], "run_id": run_id,
                     "created_at": run_at, "status": case["status"], "failed_checks": ",".join(case["failed"]),
                     "summary": r["summary"], "suggested_action": r["suggested_action"],
                     "details": json.dumps(r["details"]), "source": r["source"],
                     "fallback_reason": r.get("fallback_reason"), "ungrounded_numbers": r.get("ungrounded_numbers"),
                     "model": r["model"], "prompt_version": args.prompt_version, "input_tokens": r["input_tokens"],
                     "output_tokens": r["output_tokens"], "latency_s": r["latency_s"]})
        if args.show:
            print(f"\n[{case['status']}: {','.join(case['failed'])}] -> {r['suggested_action']} ({r['source']})\n"
                  f"  {r['summary']}")
    write_parquet(join(out_root, "explanations", f"run={run_id}.parquet"), OUT_COLS, rows, order_by="invoice_id")
    summary = {"run_id": run_id, "explained": len(rows), "llm": sum(r["source"] == "llm" for r in rows),
               "fallback": sum(r["source"] == "fallback" for r in rows),
               "input_tokens": sum(r["input_tokens"] for r in rows),
               "output_tokens": sum(r["output_tokens"] for r in rows)}
    log.info("run summary %s", json.dumps(summary))
    return summary


if __name__ == "__main__":
    main()
