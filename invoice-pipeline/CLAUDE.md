# invoice-pipeline/

Agentic supplier-invoice workflow: **Claude reads, rules decide, humans resolve exceptions.**

```
generate_invoices.py ──► PDFs + POs/GRNs (GCS or ./data/invoices)
extract_invoices.py  ──► Claude PDF → JSON (forced tool) ──► extracted Parquet
dbt (retail-dbt/models/ap/, tag ap) ──► three-way match ──► fct_invoice_status
explain_exceptions.py ──► grounded explanation + suggested action per exception
review.py            ──► human approve/reject → picked up by the next dbt run
```

## Which skill to use
- Anything in this folder, the AP dbt models, `orchestra/invoice_pipeline.yml` → `invoice-processing`
- Any change to a prompt, model, extraction or explanation code → also `agent-evals`
  (run the invoices suite and compare to the baseline before calling it done)
- dbt conventions for `models/ap/` → `retail-dbt-analytics`

## Rules
- The LLM never approves or changes a status. Status comes from dbt checks plus human decisions only.
- Idempotent by file hash. Re-running never re-extracts or double-loads a PDF. Re-extraction is
  deliberate: `--reprocess --prompt-version vN`.
- Ground truth (`_truth/`) is only ever read by `evaluate.py` and the evals, never by extraction.
- Every extraction row records model, prompt version, file hash and tokens.
- Never log invoice content, extracted JSON, bank details or secrets. Log IDs and counts only.
  Orchestra logs aren't redacted.
- `explain_exceptions.py` is non-blocking: on LLM failure, use the templated fallback and exit 0.
- Model from `ANTHROPIC_MODEL`. Check current Anthropic docs, don't hardcode from memory.
- No exception email for now: the only notification is Orchestra's FAILED alert. Don't add
  `notify.py` or SMTP secrets unless asked.
- Eval and dev data go to their own prefixes. Never write eval or synthetic test data to prod.

## Orchestra
Separate pipeline from the daily retail one: `orchestra/invoice_pipeline.yml`
(generate_invoices → extract → dbt_ap → explain), hourly in working hours.
Python tasks use the `python-invoices` connection (Anthropic key, GCS keys). dbt reuses
the existing dbt Core connection with `--select tag:ap+`. Set `HOME=/tmp` (runners have no home dir).

## Local loop
Own venv: `python3 -m venv invoice-pipeline/.venv && invoice-pipeline/.venv/bin/pip install -r invoice-pipeline/requirements.txt pytest`.
`--run-date` defaults to today UTC; pin it to the last day of the retail data.
```
python invoice-pipeline/generate_invoices.py --mode full --run-date 2026-10-07 --days-back 5 --invoices-per-day 10 --output ./data
python -m pytest invoice-pipeline/tests -q
python invoice-pipeline/extract_invoices.py --input ./data/invoices/dev --output ./data/invoices/dev
python invoice-pipeline/evaluate.py --input ./data/invoices/dev
cd retail-dbt && RETAIL_SOURCE_URI=../data dbt build --select tag:ap+ --target dev && cd ..
python invoice-pipeline/explain_exceptions.py --dry-run
```
Adjust flags to match the CLI as built. Keep this section in sync if they change.
