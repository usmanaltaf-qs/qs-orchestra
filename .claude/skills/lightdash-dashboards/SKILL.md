---
name: lightdash-dashboards
description: Define Lightdash metrics and dimensions in the retail dbt project's YAML, build the retail dashboards (exec, product, stores, customers, inventory), deploy them with the Lightdash CLI, and refresh them from Orchestra at the end of the pipeline. Use this whenever the user mentions dashboards, BI, Lightdash, metrics or semantic layer, charts, refreshing reports, or "hooking the analytics up to a BI tool" for the retail project — even if they don't name Lightdash.
---

# Lightdash dashboards for the retail project

Sits on top of the `retail-dbt-analytics` skill. Lightdash reads the dbt project directly:
metrics and dimensions are YAML `meta` on the dbt models, so the semantic layer is
version-controlled with the models.

## Why Lightdash
- dbt-native: no second modelling layer to maintain.
- Supports DuckDB via **MotherDuck** (needs dbt ≥1.8), our prod warehouse.
- Orchestra has a native `LIGHTDASH_REFRESH_DASHBOARD` task.
- Charts and dashboards can live as code (`lightdash download` / `upload`).
Alternatives if the user asks: Power BI or Tableau (both have Orchestra refresh tasks;
better for client-facing demos in Microsoft shops), or Hex (notebook-style, has an Orchestra
run-project task). The dbt marts stay the same whichever is chosen.

## Setup (user does the account bits, Claude does the rest)
1. User creates a Lightdash Cloud project (or self-hosts with Docker) and connects the
   warehouse: type **DuckDB → MotherDuck**, database `retail_analytics`, a MotherDuck token
   made just for Lightdash.
2. Install the CLI: `npm install -g @lightdash/cli` (or `brew install lightdash`), then
   `lightdash login <host>`.
3. Point the project at the dbt repo (`retail-dbt/`) so it reads the dbt project.
4. `lightdash deploy --target prod` from `retail-dbt/` publishes models + metrics.
   Use `lightdash preview` for throwaway preview projects while iterating.
5. Optional: `lightdash install-skills --agent claude` if it's available for Claude Code,
   and Lightdash's MCP server, to let Claude query/explore the deployed project.

## Defining metrics and dimensions
Metrics go in the existing `_*_models.yml` files next to the dbt model docs. Check the
dbt version first: dbt ≥1.10 wants `config: meta:` instead of top-level `meta:`, and Lightdash
supports both. Use whichever matches the project and be consistent.

```yaml
models:
  - name: fct_order_lines
    meta:
      joins:
        - join: dim_products
          sql_on: ${fct_order_lines.product_id} = ${dim_products.product_id}
        - join: dim_stores
          sql_on: ${fct_order_lines.store_id} = ${dim_stores.store_id}
    columns:
      - name: order_date
        meta:
          dimension:
            type: date
            time_intervals: [DAY, WEEK, MONTH, QUARTER, YEAR]
      - name: line_total
        meta:
          dimension: {hidden: true}
          metrics:
            net_sales:
              type: sum
              format: '[$£]#,##0'
              label: Net sales
      - name: order_id
        meta:
          metrics:
            orders:
              type: count_distinct
    meta:   # model-level custom SQL metrics
      metrics:
        aov:
          type: number
          sql: ${net_sales} / NULLIF(${orders}, 0)
          format: '[$£]#,##0.00'
```
- Implement every metric in `references/dashboards.md` exactly per the definitions in the
  dbt skill's `references/models.md`. One definition per metric, no duplicates across models.
- Format money as GBP. Percentages use `format: '0.0%'` with the value as a ratio.
- Hide raw IDs and helper columns as dimensions.
- Run `lightdash validate` (or `lightdash lint`) after changes and fix all errors.
- Check the Lightdash docs for current syntax before writing YAML (the format is
  evolving). If it differs from the example above, follow the docs.

## Dashboards
Specs are in `references/dashboards.md`. Build them in the UI or as code. Once they exist,
`lightdash download` them into `retail-dbt/lightdash/` and commit, so they're reviewable
and can be restored. `lightdash upload` pushes changes back.

## Orchestra
The repo is on **GitHub**. Deploying (`lightdash deploy`) needs Node, so run it from a
**GitHub Actions workflow on merge to main**, not in the Orchestra pipeline. Orchestra only **refreshes** dashboards after
`dbt build` succeeds. Full pipeline YAML: `references/pipeline.md`.

## Working style
- Start with the Exec dashboard end-to-end (metrics → chart → dashboard → Orchestra refresh)
  before building the other four. Prove the loop first.
- After building metrics, sanity-check one number against a direct SQL query on the mart.
