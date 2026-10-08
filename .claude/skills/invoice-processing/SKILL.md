---
name: invoice-processing
description: Build and run the supplier invoice processing workflow. Synthetic POs, goods receipts and invoice PDFs land in GCS; Claude extracts structured data from each PDF; deterministic dbt rules do three-way matching; an agent explains exceptions; a human review loop and emailed exception digest close it out, all orchestrated by Orchestra and reported in Lightdash. Use this whenever the user mentions invoices, accounts payable/AP, purchase orders, goods receipts, three-way matching, document/PDF extraction, an exception or review queue, or an agentic document workflow in Orchestra.
---

# Invoice processing

The agentic part is narrow on purpose: **Claude reads, rules decide, humans resolve
exceptions.** That's the version a finance team trusts, and the version worth demoing to clients.

```
generate_invoices.py ─► gs://…/invoices/<env>/inbox/*.pdf   (+ POs, GRNs as Parquet; ground truth kept separate)
        │
extract_invoices.py  ─► Claude (PDF → JSON via forced tool) ─► gs://…/invoices/<env>/extracted/*.parquet
        │
dbt (models/ap/)     ─► three-way match + rules ─► fct_invoice_status
        │                                   auto_approved | exception | duplicate | needs_review
explain_exceptions.py ─► one Claude call per exception → short grounded explanation
        │
notify.py            ─► email digest of exceptions (reuse retail-insights-agent email setup)
        │
review loop          ─► human decisions recorded → picked up by the next dbt run
        │
Lightdash            ─► AP dashboard
```

## Code layout
```
invoice-pipeline/
├── requirements.txt          # anthropic, duckdb, reportlab, pillow, pypdfium2, jinja2
├── generate_invoices.py      # synthetic suppliers, POs, GRNs, invoice PDFs + ground truth
├── templates/                # 3–4 supplier invoice layouts (reportlab)
├── extract_invoices.py       # new PDFs → extracted Parquet (idempotent by file hash)
├── explain_exceptions.py     # exception explanations → Parquet
├── notify.py                 # exception digest email
├── review.py                 # record human approve/reject decisions
├── evaluate.py               # extraction accuracy vs ground truth
├── prompts/                  # extraction.md, explain.md (versioned)
└── tests/
```
dbt models go in `retail-dbt/models/ap/` (tag `ap`), following `retail-dbt-analytics` conventions.

## Non-negotiables
- **The LLM never approves anything.** Status comes only from the dbt rules plus human decisions.
- **Idempotent by file hash.** Re-running never re-extracts or double-loads a PDF. A changed
  prompt or model version is a deliberate re-extract (`--reprocess --prompt-version vN`), not an accident.
- **Ground truth is never visible to extraction.** It's stored under a separate prefix and only
  `evaluate.py` reads it.
- **Every extraction row records** model, prompt version, file hash, tokens and timestamp.
- **Treat invoice content as sensitive** even though it's synthetic (bank details, addresses):
  never log document content or full extracted JSON. Log IDs and counts only.
- **No secrets in logs.** Orchestra logs aren't redacted. `HOME=/tmp` on Orchestra runners.
- Model name from `ANTHROPIC_MODEL` (check current Anthropic docs for one that supports PDF
  input). Don't hardcode from memory.

## References
- Synthetic data (suppliers, POs, GRNs, PDFs, injected problems): `references/synthetic-invoices.md`
- Extraction (schema, PDF input, batching, eval): `references/extraction.md`
- Matching rules, dbt models, review loop, explanations, AP dashboard: `references/matching.md`
- Orchestra pipeline, connections, trigger: `references/orchestra.md`

## Build order (stop for user review after steps 2 and 4)
1. `generate_invoices.py` locally: 50 invoices into `./data/invoices/`. Show the user 3–4 PDFs
   (one per template, one "scanned").
2. `extract_invoices.py` + `evaluate.py` on those 50. Report field-level accuracy by template and
   scanned vs digital. **Stop and review with the user.**
3. dbt `models/ap/`: matching + `fct_invoice_status`. Check every injected problem type is caught
   and nothing clean is flagged (ground truth tells you which is which).
4. `explain_exceptions.py` + `notify.py` (dry run HTML). **Stop and review with the user.**
5. `review.py` loop end to end.
6. GCS, then Orchestra, then the Lightdash AP dashboard.
