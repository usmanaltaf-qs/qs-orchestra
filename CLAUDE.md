# Retail analytics demo pipeline

End-to-end demo: dummy retail data → GCS → dbt on MotherDuck → Lightdash, orchestrated by
Orchestra, plus an agentic supplier-invoice workflow on the same stack. The repo is hosted on GitHub.

```
generator (Python + DuckDB) ──► Parquet in GCS ──► dbt-duckdb on MotherDuck ──► Lightdash
                                                                   └──► insights agent ──► email
Orchestra: generate → validate → dbt_build ─┬─► refresh_dashboards
                                            └─► insights

invoice PDFs ──► Claude extraction ──► dbt three-way match ──► exception explanations ──► review
```

## Repo layout
```
.claude/skills/
  retail-data-generator/    # how to build/extend the generator + validator
  retail-dbt-analytics/     # how to build/extend the dbt project
  lightdash-dashboards/     # metrics, dashboards, CI, end-to-end pipeline
  retail-insights-agent/    # daily anomaly briefing: dbt anomalies + Claude agent + email
  invoice-processing/       # synthetic invoices, extraction, matching, review loop
  agent-evals/              # evals for all LLM components: golden sets, judges, baselines, CI
retail-data-generator/      # generator code (Orchestra project_dir)
retail-dbt/                 # dbt project (+ lightdash/ for charts-as-code)
insights-agent/             # insights agent code (Orchestra project_dir)
invoice-pipeline/           # invoice workflow code (Orchestra project_dir)
evals/                      # eval harness, suites, committed baselines
orchestra/pipeline.yml      # daily retail pipeline
orchestra/invoice_pipeline.yml  # invoice pipeline (separate cadence)
.github/workflows/          # CI: dbt + pipeline YAML checks on PRs, lightdash deploy on main
```

## Which skill to use
- Changing the data being generated, GCS landing, validator → `retail-data-generator`
- Models, tests, business definitions → `retail-dbt-analytics`
  (`references/models.md` is the single source of truth for metric definitions)
- Lightdash metrics/dashboards, CI, refresh tasks → `lightdash-dashboards`
- Anomaly model, the insights agent, the briefing email → `retail-insights-agent`
- Invoices, AP models (`retail-dbt/models/ap/`), review loop, invoice pipeline → `invoice-processing`
- Any change to a prompt, model, agent tool or agent code → also `agent-evals`: run the
  relevant suite and compare to the baseline before calling the change done
- `orchestra/pipeline.yml` is shared by the retail skills. Each owns its own task groups, so edit
  only yours and leave the rest intact. `orchestra/invoice_pipeline.yml` belongs to
  `invoice-processing` alone.
- LLMs never make decisions with consequences (approvals, statuses, what counts as an anomaly).
  Rules decide; agents extract, investigate and explain.

## Cross-cutting rules
- A change that crosses a boundary (e.g. a new generator column you want on a dashboard)
  goes through every layer in one change: generator → sources/staging → mart → Lightdash
  metric. Then run the local loop below.
- Secrets only come from env vars. Never commit tokens or keys. Prod secrets live on Orchestra
  connections and GitHub Actions secrets.
- Orchestra runners have no $HOME: anything using DuckDB there needs `HOME=/tmp`.
  Orchestra logs aren't redacted, so never print secrets or connection strings.
- Prefer checking current docs (Orchestra, dbt-duckdb, MotherDuck, Lightdash) over memory
  for config syntax.

## Local loop
```
python retail-data-generator/generate_retail.py --mode full --scale 0.1 --target parquet --output ./data
python retail-data-generator/validate_retail.py --target parquet --output ./data
cd retail-dbt && RETAIL_SOURCE_URI=../data dbt build --target dev
```
