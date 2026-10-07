# End-to-end Orchestra pipeline

Extend the existing `orchestra/pipeline.yml` from the retail-data-generator skill. The full
DAG is:

```
generate ──► validate ──► dbt_build ──► refresh_dashboards
```

- `refresh_dashboards` is one task group with one `LIGHTDASH_REFRESH_DASHBOARD` task per
  dashboard. They run in parallel.
- Lightdash auth (host, project ID, personal access token) is set up once as an
  Orchestra **Lightdash connection** in the UI, not in YAML.
- Dashboard UUIDs come from the URL: `<host>/projects/<project_id>/dashboards/<dashboard_id>`.
  Put them in the YAML; they aren't secret.
- `invalidate_cache: true`, because the data really did change, so cached results are stale.

## YAML to add (after the existing generate + validate groups)

```yaml
  dbt_build:
    name: dbt build
    depends_on: [validate]
    tasks:
      dbt_build_prod:
        name: dbt build prod
        integration: DBT_CORE            # or PYTHON fallback, see retail-dbt-analytics/references/orchestra-dbt.md
        integration_job: DBT_CORE_EXECUTE
        parameters:
          # fill from the DBT_CORE_EXECUTE schema in Orchestra's pipeline YAML reference
          # command: dbt build --target prod
          # project_dir: retail-dbt
  refresh_dashboards:
    name: Refresh Lightdash dashboards
    depends_on: [dbt_build]
    tasks:
      refresh_exec:
        name: Refresh exec overview
        integration: LIGHTDASH
        integration_job: LIGHTDASH_REFRESH_DASHBOARD
        parameters:
          dashboard_id: "<exec dashboard uuid>"
          invalidate_cache: true
      refresh_product:
        name: Refresh product & category
        integration: LIGHTDASH
        integration_job: LIGHTDASH_REFRESH_DASHBOARD
        parameters:
          dashboard_id: "<uuid>"
          invalidate_cache: true
      # …stores, customers, inventory
```

Tasks may also need a `connection:` field pointing at the named Orchestra connection.
Check the TaskModel definition in the schema reference and add it if required.

## Alerts
Add a pipeline-level alert on `FAILED` (Slack or email). A failure in validate or dbt tests
should stop the dashboards refreshing, which happens by default because downstream groups
don't run when an upstream fails. Dashboards then keep showing yesterday's good data
instead of today's bad data. That's the whole point of orchestrating the refresh.

## Git hosting: GitHub
The repo lives on GitHub, and the pipeline is **Git-backed** (GitBridge: two-way sync between
`orchestra/pipeline.yml` and Orchestra).
- Pipeline sync: Orchestra workspace settings → GitHub → install the Orchestra GitHub app on
  the **organisation** account (not a personal one), limited to this repo. Each user who edits
  pipelines also connects their own GitHub user (Orchestra prompts for this).
- Task code: the Python and dbt Core tasks clone the repo through a GitHub connection. Use a
  fine-grained PAT scoped to just this repo (read access to contents), or the native user-level
  connection if offered.
- Register the pipeline once, either in the UI (New Pipeline → import existing → repo, path
  `orchestra/pipeline.yml`, default branch `main`) or with
  `orchestra pipeline import -a retail-dummy-data -p orchestra/pipeline.yml` from the repo.
  Run import **once only**. Re-running it creates duplicate pipelines.
- Test a branch before merging: `orchestra pipeline run -a retail-dummy-data -b <branch>`.

## CI (separate from Orchestra) — GitHub Actions
Store `MOTHERDUCK_TOKEN`, `LIGHTDASH_API_KEY`, `LIGHTDASH_URL`, `GCS_HMAC_KEY_ID` and
`GCS_HMAC_SECRET` as **repository secrets** (prod ones on a `production` environment with
required reviewers if wanted).

`.github/workflows/ci.yml`, on `pull_request`:
- job `orchestra`: if `orchestra/**` changed, `pip install orchestra-cli` then
  `orchestra pipeline validate orchestra/pipeline.yml`.
- job `dbt`: if `retail-dbt/**` or `retail-data-generator/**` changed, set up Python 3.12, run the
  generator at `--scale 0.05` into `./data`, then `dbt deps && dbt build --target dev`
  with `RETAIL_SOURCE_URI=../data`. Fully local, no cloud credentials needed.
- optional job `lightdash-preview`: set up Node 20 + Python, `lightdash preview`, and post the
  URL as a PR comment.

`.github/workflows/deploy.yml`, on `push` to `main` with paths `retail-dbt/**`, environment
`production`: install dbt + `@lightdash/cli`, then `cd retail-dbt && lightdash deploy --target prod`.

Use `dorny/paths-filter` or `on.pull_request.paths` to skip jobs whose files didn't change.
Pin action versions.

## Before importing
Validate the YAML with the Orchestra CLI or the validation endpoint. Do a manual run in the
dev environment and confirm all four groups go green, then enable the schedule.
