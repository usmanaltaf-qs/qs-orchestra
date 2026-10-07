# Running the dbt project in Orchestra

Two ways. Try A first and fall back to B.

## A. Native dbt Core task (preferred — gives Orchestra lineage, per-model metadata, state-aware runs)
Integration `DBT_CORE`, job `DBT_CORE_EXECUTE`. Orchestra clones the repo, installs the
dbt-core + adapter versions you specify, and runs the dbt command using a `profiles.yml`
stored on the Orchestra dbt Core connection (secrets live there, not in the repo).

- Upload the prod `profiles.yml` to the connection; set `MOTHERDUCK_TOKEN`,
  `GCS_HMAC_KEY_ID`, `GCS_HMAC_SECRET`, `RETAIL_SOURCE_URI` as connection secrets/env.
- Command: `dbt build --target prod`
- Orchestra's docs list BigQuery/Snowflake/Databricks/Fabric target examples. **Confirm
  `dbt-duckdb` is accepted** as the adapter (check the dbt Core integration page or ask the
  Orchestra docs MCP). If it isn't, use B.
- Look at Orchestra's dbt Core state management once things work — it skips unchanged models.

Get the exact parameter names from the `DBT_CORE_EXECUTE` schema in the pipeline YAML
reference before writing the task. Don't guess them.

## B. Python task running dbt (fallback)
`PYTHON_EXECUTE_SCRIPT` with `source: GIT`, `project_dir: retail-dbt`,
`build_command: pip install -r requirements.txt && dbt deps`. The `command` must
start with `python`/`uv`/`poetry`, so either:
- `uv run dbt build --target prod` (package manager UV), or
- a tiny `run_dbt.py` that calls `dbtRunner().invoke(["build", "--target", "prod"])` and
  exits non-zero on failure.

Secrets go on the Python connection as env vars. You lose Orchestra's native dbt
lineage, so upload `target/run_results.json` and `manifest.json` somewhere if needed.

## Where it sits in the pipeline
generate → validate → **dbt build** → refresh dashboards.
The full pipeline YAML lives in the `lightdash-dashboards` skill
(`references/pipeline.md`). Extend the existing `orchestra/pipeline.yml`, don't create a second pipeline.

## Environments
Use Orchestra environments for dev/prod. Swap `RETAIL_SOURCE_URI` (`…/retail/dev` vs
`…/retail/prod`) and the MotherDuck database name (`retail_analytics_dev` vs
`retail_analytics`). The model code stays identical.
