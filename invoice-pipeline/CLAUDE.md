# invoice-pipeline/

Agentic supplier-invoice workflow: **Claude reads, rules decide, humans resolve exceptions.**

```
generate_invoices.py ──► PDFs + POs/GRNs (GCS or ./data/invoices)
extract_invoices.py  ──► Claude PDF → JSON (structured output) ──► extracted Parquet
dbt (retail-dbt/models/ap/, tag ap) ──► three-way match ──► fct_invoice_status
explain_exceptions.py ──► grounded explanation + suggested action per exception
```
No human review loop (decided 2026-10-08): status is the rules' output. `final_status` exists in
`fct_invoice_status` so one could be added later without changing downstream models.

## Which skill to use
- Anything in this folder, the AP dbt models, `orchestra/invoice_pipeline.yml` → `invoice-processing`
- Any change to a prompt, model, extraction or explanation code → also `agent-evals`
  (run the invoices suite and compare to the baseline before calling it done)
- dbt conventions for `models/ap/` → `retail-dbt-analytics`

## Rules
- The LLM never approves or changes a status. Status comes from the dbt checks only.
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
(generate → extract → dbt_ap → explain → dbt_ap_refresh), hourly 08:00-18:00 on weekdays
(Europe/London). Orchestra has no GCS sensor, so it polls; runs with nothing new are near-free.
- Data: `gs://qs_orchestra/dev/invoices/dev/` (`--output gs://qs_orchestra/dev`, env `dev`).
  Warehouse: MotherDuck `md:retail_analytics` (AP tables next to the retail ones).
- Python tasks use the `python-invoices` connection: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
  `GCS_HMAC_KEY_ID`, `GCS_HMAC_SECRET`, `MOTHERDUCK_TOKEN`. One credential type for GCS (HMAC):
  boto3 for PDFs/JSON, DuckDB httpfs for Parquet (`storage.py`).
- dbt reuses the retail dbt Core connection with `--select tag:ap` (needs `stg_retail__products`
  from the retail build). A second, narrower dbt step after `explain` picks up new explanations.
- Pipeline inputs: `generate` (false = real inbox, generator exits 0) and `invoices_per_day`
  (5 by default, about 15p/day of extraction).
- `HOME=/tmp` on every task (runners have no home dir). Task outputs need an Orchestra API key:
  not set up yet.

## Local loop
Own venv: `python3 -m venv invoice-pipeline/.venv && invoice-pipeline/.venv/bin/pip install -r invoice-pipeline/requirements.txt pytest`.
`--run-date` defaults to today UTC; pin it to the last day of the retail data.
The API key comes from `ANTHROPIC_API_KEY` (or `ANTHROPIC_KEY`) in `.env`. Current models reject forced
`tool_choice`, so extraction uses structured output (`output_config.format`) instead of a forced tool.
`--retry-failed` retries files that failed (e.g. rate limits); `--show-values` prints truth vs extracted
for the worst invoices to the terminal only, never to the saved report.
```
python invoice-pipeline/generate_invoices.py --mode full --run-date 2026-10-07 --days-back 5 --invoices-per-day 10 --output ./data
python -m pytest invoice-pipeline/tests -q
ANTHROPIC_MODEL=claude-opus-5-5 python invoice-pipeline/extract_invoices.py --input ./data/invoices/dev
python invoice-pipeline/evaluate.py --input ./data/invoices/dev [--show-values]
cd retail-dbt && RETAIL_SOURCE_URI=../data dbt build --select +tag:ap --indirect-selection cautious --target dev && cd ..
python invoice-pipeline/explain_exceptions.py --dry-run          # what would be explained, no API calls
ANTHROPIC_MODEL=claude-opus-5-5 python invoice-pipeline/explain_exceptions.py [--limit 5] [--print]
cd retail-dbt && RETAIL_SOURCE_URI=../data dbt build --select +tag:ap --indirect-selection cautious --target dev && cd ..
```
explain_exceptions.py reads the AP tables from `retail-dbt/dev.duckdb` (`--db`), writes
`explanations/`, and the next dbt build joins them into `fct_invoice_status`. It explains each
(invoice, check_hash) once; API-error fallbacks are retried next run. `--limit N` (one per
exception type first) keeps local checks cheap; the pipeline runs without it.
Evals (eval sets regenerate from `evals/invoices/eval_set.yml` into `./data/evals/`, never dev/prod):
```
python evals/run_evals.py --suite invoices-matching --subset full          # rules on ground truth, free
python evals/run_evals.py --suite invoices --subset pr --repeats 1         # extraction, ~$1 (40 invoices)
python evals/run_evals.py --suite invoices --subset full --repeats 3       # nightly-sized, ~$18
python evals/run_evals.py --suite invoices-matching --subset full --extracted evals/results/<run>/<model>/rep1
python evals/run_evals.py --suite invoices-explain --subset full --repeats 1   # 5 explanations, ~5p
```
`dev`'s ground-truth dbt test (`assert_ap_matches_ground_truth`) assumes extraction was right; a
failure after a real extraction may be an extraction error, so check the extraction eval first.
Adjust flags to match the CLI as built. Keep this section in sync if they change.
