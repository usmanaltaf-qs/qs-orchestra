# Orchestra: invoice pipeline

This is a **second pipeline**, `orchestra/invoice_pipeline.yml`, separate from the daily retail
pipeline, because it has a different cadence (frequent, small batches) and a different failure
profile. Register it once with `orchestra pipeline import -a invoice-processing -p orchestra/invoice_pipeline.yml`.

## DAG
```
generate_invoices (demo only) ─► extract ─► dbt_ap ─► explain ─► notify
```
| group | task | notes |
|---|---|---|
| `generate_invoices` | Python `generate_invoices.py --mode incremental` | demo only. Disable via a pipeline input/env var (`INVOICES_GENERATE=false`) when pointing at a real inbox |
| `extract` | Python `extract_invoices.py` | only new files (manifest). Exit 0 with "0 new files" when the inbox is empty |
| `dbt_ap` | dbt Core `dbt build --select tag:ap+ --target prod` | AP models only; reuse the existing dbt Core connection |
| `explain` | Python `explain_exceptions.py` | non-blocking: exits 0 on LLM failure, templated fallback |
| `notify` | Python `notify.py` | only emails when there are new exceptions |

## Schedule / trigger
Check the current Orchestra docs for **event-based triggers** (e.g. on file arrival in GCS or a
webhook). If available, trigger on new objects under `inbox/`. Otherwise schedule hourly during
working hours, e.g. `0 8-18 ? * MON-FRI *` (EventBridge 6-field cron), timezone Europe/London.
The manifest makes frequent runs cheap and safe.

## Connections
Reuse where possible, but keep the LLM key away from tasks that don't need it:
- **`python-invoices`** (new Python connection): git, `ANTHROPIC_API_KEY`, GCS HMAC keys for the
  invoice prefixes, SMTP creds, `HOME=/tmp`. A separate Anthropic key with its own spend limit
  makes cost per workflow visible.
- **dbt Core**: the existing connection. Add the `INVOICES_SOURCE_URI` secret if the AP sources
  read from a different prefix than `RETAIL_SOURCE_URI`.

## Outputs worth setting (`--set-orchestra-outputs`)
`files_new`, `extracted_ok`, `needs_review`, `tokens_total`, `exceptions_new`, `value_on_hold`.
These make each run legible in the Orchestra UI without opening logs.

## Alerts
FAILED alert on the pipeline (email). A red `extract` or `dbt_ap` means invoices aren't being
processed. A red `explain` shouldn't happen, since it's non-blocking by design. Investigate if it does.
