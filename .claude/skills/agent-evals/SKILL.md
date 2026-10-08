---
name: agent-evals
description: Build and run evaluations for the project's LLM components (the retail insights agent and the invoice extraction/explanation steps): golden datasets with known answers, planted-scenario tests, LLM-as-judge rubrics, a regression harness that compares against a committed baseline, CI gating, and production quality monitoring. Use this whenever the user mentions evals, evaluation, accuracy, regression testing, prompt or model changes, comparing models, "is the agent getting better", LLM-as-judge, or agent quality monitoring.
---

# Agent evals

Purpose: any change to a prompt, model, tool or agent code gets a **number that says better or
worse**, before it ships. Production metrics then tell us if quality drifts.

Our big advantage: **all data is synthetic and deterministic, so we have ground truth.** Lean on
that. Exact-match metrics against known answers beat LLM judges wherever possible.

## Eval layers (cheapest first)

| layer | what | cost | when |
|---|---|---|---|
| 1. unit | grounding check, SQL guard, matching rules, number parsing | free | every PR (pytest) |
| 2. golden sets | fixed inputs with known answers; exact metrics | API tokens | PRs touching agents/prompts + nightly |
| 3. LLM-as-judge | rubric pass/fail for qualities with no exact answer (clarity, unsupported claims) | tokens ×2 | same as 2, on a sample |
| 4. production | grounding drop rate, needs_review rate, human override rate, cost | free (from logs/tables) | every run, Lightdash |

Per-agent specifics: `references/insights.md`, `references/invoices.md`.
Judge design: `references/llm-judge.md`.

## Layout
```
evals/
├── run_evals.py            # entrypoint: --suite insights|invoices|all --repeats 3 --compare baseline
├── harness/                # shared: runner, metrics, report, baseline compare, cost tracking
├── insights/               # scenarios.yml, golden_days.yml, suite.py
├── invoices/               # eval_set.yml (seed + composition), suite.py
├── judges/                 # rubric prompts, versioned
├── baselines/              # <suite>.json, committed: the numbers to beat
└── results/                # gitignored; also written to GCS for the dashboard
```
Agents are imported and called as functions (same code path as production), with a flag to
point at eval data, never prod.

## Regression harness rules
- **Eval sets are regenerated from seeds, not stored.** The generators are deterministic, so
  `eval_set.yml` (seed, dates, composition) is the dataset. Commit that, not PDFs or Parquet.
- **Repeat LLM runs** (`--repeats 3` default) and report mean and worst. LLMs are nondeterministic
  and a single run lies.
- **Compare to the baseline.** Fail if any **gating metric** drops by more than its tolerance (defined per
  metric in the suite). Non-gating metrics are reported but don't fail.
- **Update the baseline deliberately**: `--update-baseline`, committed in the same PR as the change
  that earned it, with the results summary in the PR description.
- **Record cost and latency** per suite. A change that's 1% better and 3× pricier should be visible.
- **Report, don't just assert**: write a markdown summary (metrics vs baseline, with deltas, and the
  worst failing examples) to `results/` and print it. Failing examples matter more than averages.
- Results also go to Parquet in GCS → dbt → Lightdash, so quality is tracked over time across
  prompt and model versions.

## CI (GitHub Actions)
- Layer 1 on every PR (already free and fast).
- Layers 2–3 on PRs that change `insights-agent/**`, `invoice-pipeline/**`, `evals/**` or prompts,
  plus nightly on main. Use a dedicated `ANTHROPIC_API_KEY_EVALS` secret with its own spend limit.
- Keep PR runs small (subset, `--repeats 1`) and nightly runs full (`--repeats 3`). Post the summary as a
  PR comment.

## Model comparisons
`run_evals.py --model <a> --model <b>` runs the same suite per model and outputs a side-by-side table
(quality, cost, latency). Use this when picking or upgrading `ANTHROPIC_MODEL`. Check the
current Anthropic models list. Don't rely on memory for model names.

## Build order
1. Harness skeleton + layer-1 tests (grounding, SQL guard, matching rules).
2. Invoices golden set (easiest: pure exact-match metrics). First baseline.
3. Insights planted scenarios + golden days. First baseline.
4. LLM judges, calibrated against the user's labels (see llm-judge.md) before they're trusted.
5. CI workflows.
6. Production metrics + Lightdash "Agent quality" dashboard.

Show the user the first results summary for each suite before setting a baseline.
