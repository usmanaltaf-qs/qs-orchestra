# qs-orchestra

Two data pipelines on the same stack, orchestrated by [Orchestra](https://www.getorchestra.io/):

| | Retail analytics | Invoice processing |
|---|---|---|
| Orchestra pipeline | `retail_dummy_data` ([`orchestra/pipeline.yml`](orchestra/pipeline.yml)) | `invoice_processing` ([`orchestra/invoice_pipeline.yml`](orchestra/invoice_pipeline.yml)) |
| What it does | Generates a UK retailer's daily trading data and turns it into tested reporting tables and a Lightdash dashboard | Reads supplier invoice PDFs with Claude, checks them against purchase orders and goods received, and explains every invoice it holds |
| Storage | GCS `gs://qs_orchestra/dev` | GCS `gs://qs_orchestra/dev/invoices/dev` |
| Warehouse | MotherDuck `retail_analytics` | MotherDuck `retail_analytics` (AP tables alongside the retail ones) |
| Runs | Manually, from Orchestra | Manually, from Orchestra |

All data is synthetic and deterministic: the same seed and dates always produce the same output, which
makes reruns safe and gives the invoice pipeline a known right answer to test against.

## Retail analytics pipeline

```
generate ─► validate ─► dbt build ─► refresh Lightdash dashboard
```

1. **Generate** ([`retail-data-generator/`](retail-data-generator/)): stores, products, customers,
   orders, order lines, returns and weekly stock snapshots, written as partitioned Parquet to GCS.
   Built in DuckDB SQL, with deliberate mess (missing emails, guest checkouts, messy city names) so
   the models have real cleaning to do.
2. **Validate**: primary keys and business rules (line totals, refunds no larger than the line,
   returns after orders, no returns on cancelled orders) before anything uses the data.
3. **dbt build** ([`retail-dbt/`](retail-dbt/)): staging → intermediate → core facts and dimensions →
   reporting tables (daily sales, KPIs with week-on-week and year-on-year changes, customer cohorts,
   inventory health), all tested, on MotherDuck.
4. **Refresh dashboard**: the Lightdash *Exec overview* (net sales, orders, AOV, gross margin, return
   rate, sales by category and channel, top stores), defined as code in `retail-dbt/lightdash/`.

## Invoice processing pipeline

```
generate (demo) ─► extract ─► dbt matching ─► explain ─► dbt refresh ─► summary email
```

The principle: **Claude reads, rules decide.** The LLM never approves an invoice or sets a status.

1. **Generate** ([`invoice-pipeline/generate_invoices.py`](invoice-pipeline/generate_invoices.py), demo
   only): suppliers, purchase orders, goods received notes and invoice PDFs built from the retail
   products and stores. Four invoice layouts (one goes multi-page), about 15% "scanned" (skewed, noisy,
   stamped image-only PDFs), and injected problems such as price variances, short deliveries, missing
   or wrong PO numbers, duplicates, arithmetic errors, unknown suppliers and extra charges. Ground truth
   is kept separately for testing. Turn it off with the pipeline input `generate=false` for a real inbox.
2. **Extract** ([`extract_invoices.py`](invoice-pipeline/extract_invoices.py)): one Claude call per new
   PDF returns structured data (supplier, invoice number, date, PO, lines, totals, bank details) exactly
   as printed. Each file is processed once, keyed by its content hash, so reruns cost nothing.
3. **dbt matching** ([`retail-dbt/models/ap/`](retail-dbt/models/ap/)): three-way match of invoice
   vs purchase order vs goods received, plus checks for duplicates, unknown suppliers, arithmetic, VAT
   rates and changed bank details. Each invoice ends up `auto_approved`, `exception`, `duplicate` or
   `needs_review` in `fct_invoice_status`.
4. **Explain** ([`explain_exceptions.py`](invoice-pipeline/explain_exceptions.py)): a two-sentence
   note and a suggested action (request credit note, query supplier, reject duplicate, fix master
   data) for every held invoice. Every number in a note must appear in the source data, otherwise a
   templated note is used. Suggested actions are advice only.
5. **Summary email** ([`run_summary.py`](invoice-pipeline/run_summary.py)): new invoices, the held
   queue with explanations, quality signals, API cost and the latest eval results. Orchestra also
   emails on every run's success or failure.

### How well it works

Measured by the eval suites in [`evals/`](evals/) against the generator's ground truth:

| Stage | Result |
|---|---|
| Extraction, 200 invoices | 100% of critical fields right on digital and standard scans; 97% on scans overall, where the harder scans (stamp over a figure) are the weak spot. Line-level F1 98% |
| Matching rules, 200 invoices | 100% precision and recall for every problem type; no problem invoice auto-approved |
| End to end (real extraction, then rules) | £0 of problem invoices auto-approved; 8 clean invoices held because of misread hard scans |
| Explanations | Correct suggested action and fully grounded on the sampled invoices |

API cost is about US$0.03 per invoice for extraction and US$0.013 per explanation (Claude Opus 5.5).

## Running things

- **In Orchestra:** start either pipeline from the UI, or `orchestra pipeline run -a invoice_processing`
  (and `-a retail_dummy_data`). Secrets live on the Orchestra connections, never in the repo.
- **Locally:** each part has a local loop that needs no cloud access: see [`CLAUDE.md`](CLAUDE.md) for the
  retail side and [`invoice-pipeline/CLAUDE.md`](invoice-pipeline/CLAUDE.md) for invoices, including the eval
  commands.
- **CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): on every pull request, validates both
  Orchestra pipeline files and runs a full local dbt build on freshly generated data.
