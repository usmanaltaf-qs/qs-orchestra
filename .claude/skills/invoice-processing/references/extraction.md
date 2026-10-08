# Extraction

## Approach
One Messages API call per invoice: the PDF as a base64 `document` content block, the system
prompt from `prompts/extraction.md`, and a single tool `record_invoice` with
`tool_choice` forced to it, so the response is always schema-shaped JSON. Check the current
Anthropic docs for PDF support, size/page limits, and structured output options (if native
structured outputs are available, they're fine too).

Image-only "scanned" PDFs work the same way. Claude reads the page images. No separate OCR step.

## Schema (`record_invoice`)
```json
{
  "supplier_name": "string",
  "supplier_vat_number": "string|null",
  "invoice_number": "string",
  "invoice_date": "YYYY-MM-DD",
  "po_number": "string|null",
  "currency": "GBP",
  "lines": [
    {"description": "string", "sku": "string|null", "quantity": 0,
     "unit_price": 0.00, "vat_rate": 0.20, "line_net": 0.00}
  ],
  "subtotal_net": 0.00,
  "vat_total": 0.00,
  "total_gross": 0.00,
  "bank_sort_code": "string|null",
  "bank_account": "string|null",
  "is_copy_or_duplicate_marked": false,
  "extraction_notes": "string|null"
}
```
Prompt rules: copy values **as printed**, don't correct arithmetic, don't invent a missing PO,
use null when absent, convert dates to ISO, and use `extraction_notes` for anything ambiguous.
Fixing errors is the rules' job, not extraction's.

## Quality signals (deterministic, computed after extraction)
Don't ask the model for confidence scores. They're not reliable. Instead flag `needs_review`
when: required fields are missing (supplier, invoice number, date, total), the JSON failed schema
validation after one retry, or the line count is 0. Arithmetic problems are *not* extraction
failures. They're caught by matching as `arithmetic_error`.

## Output
`extracted/invoices/*.parquet` and `extracted/invoice_lines/*.parquet` with:
`file_path, file_hash, extracted_at, model, prompt_version, input_tokens, output_tokens,
status (ok|needs_review|failed), error`, plus the fields. Also keep a `processed_files`
manifest keyed by file hash for idempotency.

## Batching and cost
- Interactive runs (small, frequent): concurrent requests with a semaphore (e.g. 5), retries with
  backoff on 429/5xx.
- Backfills or overnight runs: `--batch` uses the **Message Batches API** (roughly half price,
  asynchronous). Submit, store the batch ID, and poll in the same task up to a timeout. If not
  done, a later run collects the results (store pending batch IDs in the manifest).
- Log tokens per invoice. Set a workspace spend limit on the API key.

## evaluate.py
Quick local accuracy check during development. The full regression suite (stratified eval set,
baselines, CI, judges) lives in the `agent-evals` skill and reuses this module's metrics.

Join extracted output to `_truth` on file. Report:
- field accuracy per field (exact after normalisation; money within £0.01; dates exact)
- line-level: matched lines / truth lines (match on description similarity + amounts)
- invoice-level: "all critical fields correct" rate (supplier, invoice number, date, PO, totals)
- broken down by template and scanned vs digital

Write the results to Parquet too so the AP dashboard can show extraction accuracy over time.
Target before moving on: ≥ 98% critical-field accuracy on digital, ≥ 90% on scanned. If below,
iterate on the prompt (bump `prompt_version`) before touching anything else.
