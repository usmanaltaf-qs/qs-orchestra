# Synthetic invoices

Built on the retail data so it joins up: products and stores come from the generator's dims
(read via DuckDB from the same `RETAIL_SOURCE_URI`). Same determinism rules as the retail
generator: hash-based randomness, seed + date ⇒ identical output, date-keyed IDs.

## Entities
**suppliers** (~12): `supplier_id`, `supplier_name` (invented), `vat_number` (fake GB format),
`payment_terms_days` (30/45/60), `template` (A–D), `bank_sort_code`, `bank_account` (obviously fake,
e.g. sort code `00-00-xx`). Each supplier supplies a set of brands, so products map to one supplier.

**purchase_orders** + **po_lines**: `po_number` (`PO-yyyymmdd-nnnn`), supplier, delivery `store_id`,
order date, lines of product, qty, agreed `unit_cost`. ~3–6 lines per PO.

**goods_receipts** (GRNs) + **grn_lines**: per PO, received date (PO date + 2–10 days),
`qty_received` (usually = ordered).

**invoices**: one per PO normally, issued on GRN date + 0–5 days.

Write POs/GRNs/suppliers as Parquet (they're "system data" in a real ERP). Write invoices **only**
as PDFs, plus ground-truth JSON under a separate prefix.

## UK details that make it realistic (and test the rules)
- VAT: 20% standard. **Food is zero-rated (0%)**, so mixed invoices have two VAT rates.
- Currency GBP, dates in UK format on the PDF (`06/10/2026`, or `6 Oct 2026` on some templates).
- Invoice number formats differ per supplier (`INV-00123`, `2026/0456`, `A12345`).

## Templates (reportlab), so extraction can't overfit to one layout
- **A:** classic. Header block, line table, totals bottom right.
- **B:** totals at the top, lines below, PO number in the footer.
- **C:** two-column, "Qty" after "Unit price", line descriptions only (no SKU column).
- **D:** dense, small font, multi-page when > 15 lines, continued table headers.

## "Scanned" variants (~15%)
Render to image (pypdfium2 → Pillow), then apply slight rotation (±2°), noise, blur,
greyscale, maybe a stamp ("RECEIVED") overlapping text. Save back as an image-only PDF.

## Injected problems (per invoice, deterministic, rates are config)
| problem | rate | how |
|---|---|---|
| clean | ~70% | matches PO and GRN exactly |
| price_variance | 8% | one line's unit price +3–15% vs PO |
| qty_over_received | 5% | invoiced qty > GRN qty on a line (short delivery) |
| missing_po | 4% | no PO number on the invoice |
| wrong_po | 2% | PO number of a different supplier's order |
| duplicate | 3% | same invoice re-sent (same number; sometimes "COPY" watermark, re-rendered) |
| arithmetic_error | 3% | line totals or VAT don't add up on the document itself |
| unknown_supplier | 1% | supplier not in master data |
| extra_charge | 4% | "Delivery charge" line not on PO |

Multiple problems on one invoice are allowed at a low rate.

## Ground truth (`invoices/<env>/_truth/<invoice_file>.json`)
The exact values printed on the PDF (what perfect extraction would return) **plus**
`injected_problems: [...]`. These are two different things: extraction is scored against the
printed values; matching is scored against injected_problems.

## Landing layout
```
{root}/invoices/<env>/inbox/YYYY/MM/DD/<supplier>_<invoice_no>_<hash8>.pdf
{root}/invoices/<env>/system/{suppliers,purchase_orders,po_lines,goods_receipts,grn_lines}/...parquet
{root}/invoices/<env>/_truth/...json
```

## CLI
```
python generate_invoices.py --mode full|incremental --run-date --days-back 30 \
  --invoices-per-day 20 --scanned-rate 0.15 --seed 42 --output ./data | gs://bucket
```
Incremental adds one day's POs, GRNs and invoices, which simulates an inbox filling up.
