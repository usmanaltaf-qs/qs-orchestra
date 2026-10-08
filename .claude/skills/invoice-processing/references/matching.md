# Matching, review loop and reporting

## dbt models (`retail-dbt/models/ap/`, tag `ap`)
Sources: `system/*` Parquet, `extracted/*` Parquet, `explanations/*` Parquet, `review/decisions`.

| model | purpose |
|---|---|
| `stg_ap__*` | one per source, typed and cleaned (trim and upper-case invoice/PO numbers, normalise supplier names) |
| `int_ap__invoice_supplier` | match extracted supplier to master data: VAT number first, then normalised name |
| `int_ap__invoice_lines_matched` | invoice lines ⨝ PO lines ⨝ GRN lines, matched on SKU when present, else description similarity within the PO |
| `int_ap__invoice_checks` | one row per invoice × check, with `passed`, `expected`, `actual`, `detail` |
| `fct_invoice_status` | one row per invoice: system status, latest human decision, final status, amounts, days in queue |
| `rpt_ap_daily` | daily counts and values by status and exception type |

## Checks (each a row in `int_ap__invoice_checks`; thresholds as dbt vars)
| check | fails when |
|---|---|
| `duplicate` | same supplier + invoice number already seen (earlier extraction), or same file hash |
| `unknown_supplier` | no master-data match |
| `missing_po` | no PO number |
| `po_not_found` / `po_supplier_mismatch` | PO doesn't exist, or belongs to a different supplier |
| `price_variance` | any line unit price differs from PO by > 2% **and** > £0.05 |
| `qty_over_received` | invoiced qty > GRN qty received |
| `unmatched_line` | invoice line with no PO line (e.g. delivery charge) |
| `arithmetic` | Σ line_net ≠ subtotal, or VAT ≠ Σ(line_net × rate), or subtotal + VAT ≠ total (± £0.02) |
| `vat_rate` | rate isn't 0% for Food products or 20% for others |
| `bank_details_changed` | sort code/account differs from supplier master (classic fraud signal) |
| `extraction` | extraction status is `needs_review` or `failed` |

## Status logic (`fct_invoice_status`)
- `duplicate` if the duplicate check fails (takes precedence)
- `needs_review` if extraction failed
- `exception` if any other check fails
- `auto_approved` otherwise
- `final_status` = latest human decision (`approved` / `rejected`) if one exists, else the system status
- `exception_types` = list of failed checks; `value_on_hold` = total_gross for unresolved exceptions

Tests: one status per invoice; every `auto_approved` invoice has all checks passed; and a
**singular test against ground truth (dev only)** that every injected problem is caught and no clean
invoice is flagged. Report precision and recall per problem type rather than just pass/fail.

## Exception explanations (`explain_exceptions.py`)
For each unresolved exception without an explanation for its current check results: one Claude
call, no tools. Input is the failed check rows (expected vs actual), the relevant invoice lines,
PO lines and GRN lines as compact tables. Output via a forced tool:
`{summary: ≤ 2 sentences, suggested_action: "request credit note | approve with note | query supplier | reject duplicate | fix master data", details: [..]}`.
- Grounding: every number in the summary must appear in the input tables (reuse the
  `retail-insights-agent` grounding approach). Failures fall back to a templated sentence built
  from the check rows.
- `suggested_action` is advice shown to the reviewer. It never changes status.
- Output to `explanations/*.parquet` keyed by invoice + check-hash, so it isn't regenerated each run.

## Review loop
Decisions table (`review/decisions`): `invoice_id, decision (approved|rejected), reason,
decided_by, decided_at`. Append-only. The latest decision wins.

Ask the user how they want to record decisions, offering:
1. **`review.py` CLI (default, simplest):** lists open exceptions with explanations, prompts
   approve/reject + reason, appends a Parquet file to `review/decisions/`.
2. **Small Streamlit app:** the same thing with a UI, run locally.
3. **Google Sheet:** the exception digest links to a sheet, and a task syncs it to Parquet.
   Nicest for non-technical reviewers, but adds an integration.

The next pipeline run picks up decisions via dbt. No task waits on a human.

## Exception digest email (`notify.py`)
Reuse the SMTP setup and HTML conventions from `retail-insights-agent/references/email.md`.
Send only when there are **new** exceptions since the last digest (track in a small state file in
GCS). Contents: counts by type, value on hold, a table of new exceptions with the explanation
and suggested action, and links to the Lightdash AP dashboard and the review method.

## Lightdash AP dashboard
Metrics: invoices processed, auto-approval rate, exceptions by type, value on hold, median days
to resolve, duplicate value caught, extraction critical-field accuracy (from `evaluate.py`
output), tokens and cost per invoice. Charts: daily status stack, exceptions by type × supplier,
open exceptions table, accuracy by template over time.
