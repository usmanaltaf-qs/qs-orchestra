# Retail analytics demo pipeline

End-to-end demo: dummy retail data → GCS → dbt on MotherDuck → Lightdash, orchestrated by
Orchestra. The repo is hosted on GitHub.

```
generator (Python + DuckDB) ──► Parquet in GCS ──► dbt-duckdb on MotherDuck ──► Lightdash
         └──────────────── Orchestra: generate → validate → dbt_build → refresh_dashboards ──┘
```

## Repo layout
```
.claude/skills/
  retail-data-generator/    # how to build/extend the generator + validator
  retail-dbt-analytics/     # how to build/extend the dbt project
  lightdash-dashboards/     # metrics, dashboards, CI, end-to-end pipeline
retail-data-generator/      # generator code (Orchestra project_dir)
retail-dbt/                 # dbt project (+ lightdash/ for charts-as-code)
orchestra/pipeline.yml      # the ONE Orchestra pipeline
.github/workflows/          # CI: dbt + pipeline YAML checks on PRs, lightdash deploy on main
```

## Which skill to use
- Changing the data being generated, GCS landing, validator → `retail-data-generator`
- Models, tests, business definitions → `retail-dbt-analytics`
  (`references/models.md` is the single source of truth for metric definitions)
- Lightdash metrics/dashboards, CI, refresh tasks → `lightdash-dashboards`
- `orchestra/pipeline.yml` is shared. Each skill owns its own task groups, so edit only yours
  and leave the rest intact.

## Cross-cutting rules
- A change that crosses a boundary (e.g. a new generator column you want on a dashboard)
  goes through every layer in one change: generator → sources/staging → mart → Lightdash
  metric. Then run the local loop below.
- Secrets only come from env vars. Never commit tokens or keys. Prod secrets live on Orchestra
  connections and GitHub Actions secrets.
- Prefer checking current docs (Orchestra, dbt-duckdb, MotherDuck, Lightdash) over memory
  for config syntax.

## Local loop
```
python retail-data-generator/generate_retail.py --mode full --scale 0.1 --target parquet --output ./data
python retail-data-generator/validate_retail.py --target parquet --output ./data
cd retail-dbt && RETAIL_SOURCE_URI=../data dbt build --target dev
```
