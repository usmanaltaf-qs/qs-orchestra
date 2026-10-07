# Running it in Orchestra

Verified against docs.getorchestra.io (Oct 2026). If anything below fails validation, check
the Python Execute Script page and the pipeline YAML schema reference there — or install
Orchestra's own `orchestra-skills` Claude Code plugin / docs MCP server, which know the
current schema.

## How Orchestra runs Python
Integration `PYTHON`, job `PYTHON_EXECUTE_SCRIPT`. With `source: GIT` Orchestra:
1. clones the repo (optionally only `shallow_clone_dirs`), 2. `cd`s to `project_dir`,
3. runs `build_command`, 4. runs `command`.

Constraints that shape the code:
- `command` must start with `python`, `poetry` or `uv`, and is **one command** — no `&&`.
  So generate and validate are two tasks, not one.
- Compute is ephemeral. Nothing on local disk survives to the next task → write to GCS.
- `orchestra-sdk` is pre-installed. Injected env vars include `ORCHESTRA_API_KEY`,
  `ORCHESTRA_PIPELINE_RUN_ID`, `ORCHESTRA_TASK_RUN_ID`, `ORCHESTRA_TASK_ATTEMPT_NUMBER`.
- **Secrets** (HMAC keys, SA JSON) go on the Python *connection* in the Orchestra UI — they're
  exported as env vars and redacted in logs. Non-secret config goes in the task's
  `environment_variables`.
- Python 3.11 or 3.12.

## Script requirements for Orchestra
- Log to stdout (streams to the Orchestra UI). Exit non-zero on failure.
- Include `ORCHESTRA_PIPELINE_RUN_ID` in the log summary when present.
- `--set-orchestra-outputs`: if `ORCHESTRA_API_KEY` is set, set outputs like
  `rows_orders`, `rows_order_items`, `window_start`, `window_end`, `output_uri`:
  ```python
  from orchestra_sdk.orchestra import OrchestraSDK
  client = OrchestraSDK(api_key=os.environ["ORCHESTRA_API_KEY"])
  client.set_output("rows_orders", n_orders)
  ```
  Wrap in try/except — outputs must never fail the run. Task needs `set_outputs: true`.

## Pipeline YAML — `orchestra/pipeline.yml`
Task groups are the DAG unit; dependencies are between groups. Cron is AWS EventBridge
6-field format.

```yaml
version: v1
name: retail_dummy_data
schedule:
  - name: daily_6am
    cron: "0 6 * * ? *"
    timezone: Europe/London
configuration:
  retries: 1
  retry_delay: 300
pipeline:
  generate:
    name: Generate retail data
    tasks:
      generate_retail:
        name: Generate retail data
        integration: PYTHON
        integration_job: PYTHON_EXECUTE_SCRIPT
        parameters:
          source: GIT
          command: python generate_retail.py --mode incremental --target parquet --set-orchestra-outputs
          package_manager: PIP
          python_version: "3.12"
          build_command: pip install -r requirements.txt
          project_dir: retail-data-generator
          environment_variables:
            RETAIL_OUTPUT: gs://<bucket>/retail/prod
            RETAIL_SEED: "42"
          set_outputs: true
  validate:
    name: Validate retail data
    depends_on: [generate]
    tasks:
      validate_retail:
        name: Validate retail data
        integration: PYTHON
        integration_job: PYTHON_EXECUTE_SCRIPT
        parameters:
          source: GIT
          command: python validate_retail.py --target parquet
          package_manager: PIP
          python_version: "3.12"
          build_command: pip install -r requirements.txt
          project_dir: retail-data-generator
          environment_variables:
            RETAIL_OUTPUT: gs://<bucket>/retail/prod
```

Notes:
- `environment_variables` is documented as a JSON string in the task table but an object in
  the example YAML — if validation rejects the object, switch to a JSON string.
- Validate the file with the Orchestra CLI or the REST validation endpoint before importing.
- Backfill: trigger a manual run with `RETAIL_RUN_DATE` overridden, or add a pipeline
  input and pass `--run-date ${{ inputs.run_date }}`. Script must treat an empty
  `--run-date` as today.
- Use Orchestra environments (dev/prod) to swap `RETAIL_OUTPUT` prefixes rather than
  duplicating the pipeline.

## Downstream of this pipeline
This skill owns only the `generate` and `validate` task groups in `orchestra/pipeline.yml`.
The same file is extended by:
- `retail-dbt-analytics` → `dbt_build` group (depends on `validate`)
- `lightdash-dashboards` → `refresh_dashboards` group, alerts, and the GitHub Actions CI
  (see its `references/pipeline.md`)

There is one pipeline file. Never create a second one. When editing, keep the other skills'
groups intact.

The repo is on GitHub. The pipeline is Git-backed (GitBridge) via the Orchestra GitHub app,
and tasks clone the repo through a GitHub connection. See
`lightdash-dashboards/references/pipeline.md` for setup.
