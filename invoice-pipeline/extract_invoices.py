#!/usr/bin/env python3
"""Extract structured data from invoice PDFs with Claude. Idempotent by file hash.

One Messages API call per PDF: the PDF as a base64 document block, the versioned prompt from
prompts/extraction_<version>.md, and structured output (output_config.format) so the reply is
always schema-shaped JSON. (Current models reject forced tool_choice; structured output gives the
same guarantee.) Spec: .claude/skills/invoice-processing/references/extraction.md

Reads   {input}/inbox/**/*.pdf
Writes  {output}/extracted/invoices/run=<run_id>-<chunk>.parquet       (flushed every FLUSH_EVERY files)
        {output}/extracted/invoice_lines/run=<run_id>-<chunk>.parquet
        {output}/extracted/processed_files/manifest.parquet   one row per (file_hash, prompt_version)
        {output}/extracted/inbox_files/run=<run_id>.parquet   every PDF path the first time it's seen

Extraction is cached by file hash, but every inbox *file* is an invoice: a byte-identical re-send
gets an inbox_files row (and is caught as a duplicate downstream) without a second API call.

Never reads _truth/. Logs file-hash prefixes and counts only, never document content.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

import anthropic
import duckdb

import storage
from common import bytes_hash, load_env, money, setup_logging, write_parquet
from storage import join, relative

log = setup_logging("extract_invoices")

PROMPTS = Path(__file__).resolve().parent / "prompts"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FLUSH_EVERY = 20      # write results every N files
RATE_LIMIT_WAITS = 4  # extra waits of 15s, 30s, 60s, 120s after the SDK gives up on a 429
REQUIRED = ("supplier_name", "invoice_number", "invoice_date", "total_gross")

_nullable_str = {"type": ["string", "null"]}
LINE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "sku": _nullable_str,
        "quantity": {"type": "number"},
        "unit_price": {"type": "number"},
        "vat_rate": {"type": "number", "description": "fraction, e.g. 0.20 for 20%"},
        "line_net": {"type": "number", "description": "line amount before VAT, as printed"},
    },
    "required": ["description", "sku", "quantity", "unit_price", "vat_rate", "line_net"],
    "additionalProperties": False,
}
INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string"},
        "supplier_vat_number": _nullable_str,
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string", "description": "YYYY-MM-DD"},
        "po_number": _nullable_str,
        "currency": {"type": "string"},
        "lines": {"type": "array", "items": LINE_SCHEMA},
        "subtotal_net": {"type": "number"},
        "vat_total": {"type": "number"},
        "total_gross": {"type": "number"},
        "bank_sort_code": _nullable_str,
        "bank_account": _nullable_str,
        "is_copy_or_duplicate_marked": {"type": "boolean"},
        "extraction_notes": _nullable_str,
    },
    "required": ["supplier_name", "supplier_vat_number", "invoice_number", "invoice_date", "po_number",
                 "currency", "lines", "subtotal_net", "vat_total", "total_gross", "bank_sort_code",
                 "bank_account", "is_copy_or_duplicate_marked", "extraction_notes"],
    "additionalProperties": False,
}

META_COLS = ("file_path VARCHAR, file_hash VARCHAR, run_id VARCHAR, extracted_at TIMESTAMPTZ, model VARCHAR, "
             "prompt_version VARCHAR, input_tokens INTEGER, output_tokens INTEGER, latency_s DOUBLE, "
             "attempts INTEGER, status VARCHAR, error VARCHAR")
INVOICE_COLS = META_COLS + (
    ", supplier_name VARCHAR, supplier_vat_number VARCHAR, invoice_number VARCHAR, invoice_date DATE, "
    "po_number VARCHAR, currency VARCHAR, subtotal_net DECIMAL(12,2), vat_total DECIMAL(12,2), "
    "total_gross DECIMAL(12,2), bank_sort_code VARCHAR, bank_account VARCHAR, "
    "is_copy_or_duplicate_marked BOOLEAN, extraction_notes VARCHAR, line_count INTEGER")
LINE_COLS = ("file_hash VARCHAR, run_id VARCHAR, prompt_version VARCHAR, line_no INTEGER, description VARCHAR, "
             "sku VARCHAR, quantity DECIMAL(12,3), unit_price DECIMAL(12,2), vat_rate DECIMAL(5,4), "
             "line_net DECIMAL(12,2)")
INBOX_COLS = "file_path VARCHAR, file_hash VARCHAR, run_id VARCHAR, first_seen_at TIMESTAMPTZ"
MANIFEST_COLS = ("file_hash VARCHAR, prompt_version VARCHAR, file_path VARCHAR, status VARCHAR, model VARCHAR, "
                 "run_id VARCHAR, extracted_at TIMESTAMPTZ")


class ExtractionError(Exception):
    pass


def load_prompt(version: str) -> str:
    path = PROMPTS / f"extraction_{version}.md"
    if not path.exists():
        raise SystemExit(f"no prompt for version {version}: {path}")
    return path.read_text()


def call_claude(client, model: str, effort: str, system: str, pdf_b64: str):
    return client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": INVOICE_SCHEMA}},
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": "Extract this invoice."},
        ]}],
    )


def parse_response(resp) -> dict:
    if resp.stop_reason == "refusal":
        raise ExtractionError("refusal")
    if resp.stop_reason == "max_tokens":
        raise ExtractionError("max_tokens")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise ExtractionError("no text block")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ExtractionError(f"invalid json at char {e.pos}") from None
    missing = [k for k in INVOICE_SCHEMA["required"] if k not in data]
    if missing:
        raise ExtractionError(f"missing keys: {','.join(missing)}")
    return data


def quality_status(data: dict) -> tuple[str, str | None]:
    """Deterministic review flags. Arithmetic is not checked here: that's the matching rules' job."""
    problems = [k for k in REQUIRED if data.get(k) in (None, "")]
    try:
        if data.get("invoice_date"):
            date.fromisoformat(data["invoice_date"])
    except ValueError:
        problems.append("invoice_date_unparseable")
    if not data.get("lines"):
        problems.append("no_lines")
    return ("needs_review", "; ".join(problems)) if problems else ("ok", None)


def extract_one(client, model: str, effort: str, system: str, pdf: bytes) -> dict:
    """Returns {'meta': ..., 'data': dict|None}. Retries once on an unusable response. The SDK
    retries 429/5xx/connection errors briefly; on top of that, rate limits back off for longer
    (they're about throughput, not this document) without using up the response retry."""
    pdf_b64 = base64.standard_b64encode(pdf).decode()
    attempts, tokens_in, tokens_out, served_by = 0, 0, 0, model
    t0 = time.perf_counter()
    data, err = None, None
    response_tries, rate_limit_waits = 0, 0
    while response_tries < 2:
        attempts += 1
        try:
            resp = call_claude(client, model, effort, system, pdf_b64)
            tokens_in += resp.usage.input_tokens
            tokens_out += resp.usage.output_tokens
            served_by = resp.model
            data = parse_response(resp)
            err = None
            break
        except ExtractionError as e:
            err = str(e)
            response_tries += 1
        except anthropic.BadRequestError as e:  # not retryable: same request fails the same way
            err = f"bad_request: {e.status_code}"
            break
        except anthropic.RateLimitError as e:
            err = "api_error: 429"
            if rate_limit_waits >= RATE_LIMIT_WAITS:
                break
            retry_after = e.response.headers.get("retry-after")
            wait = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else 0
            time.sleep(max(wait, 15 * 2 ** rate_limit_waits))
            rate_limit_waits += 1
        except anthropic.APIStatusError as e:
            err = f"api_error: {e.status_code}"
            response_tries += 1
        except anthropic.APIConnectionError:
            err = "connection_error"
            response_tries += 1
    if data is None:
        status = "failed"
    else:
        status, err = quality_status(data)
    meta = {"model": served_by, "input_tokens": tokens_in, "output_tokens": tokens_out,
            "latency_s": round(time.perf_counter() - t0, 2), "attempts": attempts, "status": status, "error": err}
    return {"meta": meta, "data": data}


def safe_extract_one(client, model: str, effort: str, system: str, path: str, data: bytes | None) -> dict:
    """extract_one, but an unexpected per-file error (e.g. the file vanished) becomes a failed row
    instead of taking down the run. Logs the exception type only."""
    try:
        return extract_one(client, model, effort, system, data if data is not None else storage.read_bytes(path))
    except Exception as e:  # noqa: BLE001
        return {"data": None, "meta": {"model": model, "input_tokens": 0, "output_tokens": 0, "latency_s": 0,
                                       "attempts": 0, "status": "failed", "error": f"error: {type(e).__name__}"}}


def to_rows(rel: str, fhash: str, run_id: str, at: datetime, prompt_version: str, res: dict):
    d = res["data"] or {}
    num = lambda x: money(x) if isinstance(x, (int, float)) else None  # noqa: E731
    try:
        inv_date = date.fromisoformat(d["invoice_date"]) if d.get("invoice_date") else None
    except ValueError:
        inv_date = None
    inv = {"file_path": rel, "file_hash": fhash, "run_id": run_id, "extracted_at": at,
           "prompt_version": prompt_version, **res["meta"],
           **{k: d.get(k) for k in ("supplier_name", "supplier_vat_number", "invoice_number", "po_number",
                                    "currency", "bank_sort_code", "bank_account", "is_copy_or_duplicate_marked",
                                    "extraction_notes")},
           "invoice_date": inv_date, "subtotal_net": num(d.get("subtotal_net")),
           "vat_total": num(d.get("vat_total")), "total_gross": num(d.get("total_gross")),
           "line_count": len(d.get("lines") or [])}
    lines = [{"file_hash": fhash, "run_id": run_id, "prompt_version": prompt_version, "line_no": i,
              "description": ln.get("description"), "sku": ln.get("sku"), "quantity": ln.get("quantity"),
              "unit_price": num(ln.get("unit_price")), "vat_rate": ln.get("vat_rate"),
              "line_net": num(ln.get("line_net"))}
             for i, ln in enumerate(d.get("lines") or [], 1)]
    return inv, lines


def read_manifest(path: str) -> dict:
    if not storage.exists(path):
        return {}
    rows = storage.duck(path).sql(f"SELECT * FROM read_parquet('{path}')").fetchall()
    cols = [c.split()[0] for c in MANIFEST_COLS.split(", ")]
    return {(r[0], r[1]): dict(zip(cols, r)) for r in rows}


def known_inbox(out_root: str) -> dict[str, str]:
    """file_path -> file_hash for every inbox file recorded so far."""
    prefix = join(out_root, "extracted", "inbox_files")
    if not storage.list_files(prefix, ".parquet"):
        return {}
    return dict(storage.duck(prefix).sql(
        f"SELECT file_path, file_hash FROM read_parquet('{prefix}/*.parquet')").fetchall())


def seen_paths(out_root: str) -> set[str]:
    return set(known_inbox(out_root))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--input", default=os.environ.get("INVOICES_INPUT", "./data/invoices/dev"),
                   help="env root containing inbox/")
    p.add_argument("--output", default=os.environ.get("INVOICES_OUTPUT"),
                   help="env root for extracted/ (default: same as --input)")
    p.add_argument("--prompt-version", default=os.environ.get("INVOICES_PROMPT_VERSION", "v1"))
    p.add_argument("--reprocess", action="store_true",
                   help="re-extract files already done at an older prompt version (never at the same one)")
    p.add_argument("--retry-failed", action="store_true", help="also retry files whose last attempt failed")
    p.add_argument("--concurrency", type=int, default=int(os.environ.get("INVOICES_CONCURRENCY", 5)))
    p.add_argument("--limit", type=int, default=0, help="extract at most N new files (0 = all)")
    p.add_argument("--effort", default=os.environ.get("ANTHROPIC_EFFORT", "medium"),
                   choices=["low", "medium", "high", "xhigh", "max"])
    args = p.parse_args(argv)
    args.output = args.output or args.input
    return args


def main(argv=None) -> dict:
    args = parse_args(argv)
    load_env()
    model = os.environ.get("ANTHROPIC_MODEL", "").strip()
    if not model:
        raise SystemExit("ANTHROPIC_MODEL is not set")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    system = load_prompt(args.prompt_version)

    in_root, out_root = str(args.input), str(args.output)
    manifest_path = join(out_root, "extracted", "processed_files", "manifest.parquet")
    manifest = read_manifest(manifest_path)
    done_hashes = {h for (h, _pv), m in manifest.items() if m["status"] != "failed"}
    failed = {h for (h, pv), m in manifest.items() if pv == args.prompt_version and m["status"] == "failed"}

    run_at = datetime.now(timezone.utc)
    run_id = run_at.strftime("%Y%m%dT%H%M%SZ")
    known = known_inbox(out_root)
    new_inbox = []
    todo, skipped = [], 0
    for path in storage.list_files(join(in_root, "inbox"), ".pdf"):
        rel = relative(path, in_root)
        data = None
        if rel in known:              # hash already recorded: don't download it again
            fhash = known[rel]
        else:
            data = storage.read_bytes(path)
            fhash = bytes_hash(data)
            new_inbox.append({"file_path": rel, "file_hash": fhash, "run_id": run_id, "first_seen_at": run_at})
        if (fhash, args.prompt_version) in manifest and not (fhash in failed and args.retry_failed):
            skipped += 1                      # already done at this prompt version
        elif fhash in done_hashes and not args.reprocess:
            skipped += 1                      # done at another version; re-extract only on --reprocess
        elif fhash in {t[2] for t in todo}:
            skipped += 1                      # byte-identical file twice in the inbox
        else:
            todo.append((path, rel, fhash, data))
    if args.limit:
        todo = todo[:args.limit]
    log.info("files: %d new in inbox, %d to extract, %d skipped (prompt %s, model %s)", len(new_inbox),
             len(todo), skipped, args.prompt_version, model)
    if new_inbox:
        write_parquet(join(out_root, "extracted", "inbox_files", f"run={run_id}.parquet"), INBOX_COLS, new_inbox,
                      order_by="file_path")
    if not todo:
        return {"extracted": 0, "skipped": skipped, "new_inbox_files": len(new_inbox)}

    client = anthropic.Anthropic(max_retries=4)
    inv_rows, line_rows = [], []
    chunk = {"n": 0, "inv": 0, "lines": 0}

    def flush():
        """Persist what's done so far, so a crash never loses paid-for extractions."""
        if chunk["inv"] == len(inv_rows):
            return
        k = chunk["n"]
        write_parquet(join(out_root, "extracted", "invoices", f"run={run_id}-{k:03d}.parquet"), INVOICE_COLS,
                      inv_rows[chunk["inv"]:], order_by="file_path")
        write_parquet(join(out_root, "extracted", "invoice_lines", f"run={run_id}-{k:03d}.parquet"), LINE_COLS,
                      line_rows[chunk["lines"]:], order_by="file_hash, line_no")
        write_parquet(manifest_path, MANIFEST_COLS, list(manifest.values()), order_by="file_path, prompt_version")
        chunk.update(n=k + 1, inv=len(inv_rows), lines=len(line_rows))

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(safe_extract_one, client, model, args.effort, system, path, data): (rel, fhash)
                   for path, rel, fhash, data in todo}
        for fut in as_completed(futures):
            rel, fhash = futures[fut]
            res = fut.result()
            inv, lines = to_rows(rel, fhash, run_id, run_at, args.prompt_version, res)
            inv_rows.append(inv)
            line_rows.extend(lines)
            m = res["meta"]
            log.info("file %s status=%s tokens=%d/%d attempts=%d %.1fs", fhash[:12], m["status"],
                     m["input_tokens"], m["output_tokens"], m["attempts"], m["latency_s"])
            manifest[(fhash, args.prompt_version)] = {
                "file_hash": fhash, "prompt_version": args.prompt_version, "file_path": rel,
                "status": m["status"], "model": m["model"], "run_id": run_id, "extracted_at": run_at}
            if len(inv_rows) - chunk["inv"] >= FLUSH_EVERY:
                flush()
    flush()

    statuses = {s: sum(r["status"] == s for r in inv_rows) for s in ("ok", "needs_review", "failed")}
    summary = {"run_id": run_id, "extracted": len(inv_rows), "skipped": skipped, **statuses,
               "input_tokens": sum(r["input_tokens"] for r in inv_rows),
               "output_tokens": sum(r["output_tokens"] for r in inv_rows)}
    log.info("run summary %s", json.dumps(summary))
    if statuses["failed"] == len(inv_rows):
        sys.exit(1)  # nothing worked: fail the task so Orchestra alerts
    return summary


if __name__ == "__main__":
    main()
