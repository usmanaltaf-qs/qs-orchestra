# Evals: retail insights agent

The hard part of evaluating an "explain what happened" agent is knowing the true cause. We solve
that by **planting scenarios**: deliberate, known anomalies with a known cause, injected into an
eval copy of the data.

## Planted scenarios (`evals/insights/scenarios.yml`)
Add a `--scenario <file>` option to the retail generator (follow its "add a table" style:
deterministic, documented in its schema.md). Scenarios apply perturbations on top of normal
generation and write to an **eval prefix only** (`./data_eval/` or `gs://…/retail/eval/`), never dev/prod.
Then build dbt with `--target eval` against that prefix.

```yaml
- id: store_promo_spike
  date: 2026-03-12
  perturbation: {store_id: 7, category: Electronics, discount_pct: 0.5, volume_multiplier: 2.5}
  expected:
    must_flag: {dimension: store, value: "7", metric: net_sales, direction: up}
    cause_keywords_any: [discount, promotion, Electronics]
    must_not_claim: [weather, competitor]
- id: returns_surge_clothing
  date: 2026-04-02
  perturbation: {category: Clothing, return_rate_multiplier: 3, lookback_days: 7}
  expected:
    must_flag: {dimension: category, value: Clothing, metric: return_rate, direction: up}
    cause_keywords_any: [return]
- id: online_outage
  date: 2026-05-20
  perturbation: {channel: online, volume_multiplier: 0.2}
  expected:
    must_flag: {dimension: channel, value: online, metric: orders, direction: down}
- id: quiet_day            # control: nothing planted
  date: 2026-06-17
  expected: {max_findings_severity_act: 0, max_findings: 2}
```
Aim for ~10 scenarios covering: spikes, drops, mix shifts (sales flat but margin down), a
stockout, a seasonal false alarm (Black Friday with YoY context), and 2–3 quiet controls.

## Metrics
| metric | definition | gating |
|---|---|---|
| detection recall | planted anomalies that appear as a finding | yes, tolerance −0.05 |
| cause attribution | findings for planted anomalies whose text matches `cause_keywords_any` (or the judge says the cause is right) | yes, −0.10 |
| false alarm rate | `act`-severity findings on quiet controls | yes, +0.05 |
| unsupported claims | `must_not_claim` hits + judge "unsupported causal claim" fails | yes, must be 0 |
| grounding drop rate | findings dropped by grounding / findings proposed | no (watch) |
| tool calls, tokens, cost, seconds | per run | no |

Detection itself is SQL (dbt), so test it separately as layer 1: each scenario's anomaly must
appear in `rpt_daily_anomalies`. If it doesn't, that's a dbt bug, not an agent bug. Keep the two apart.

## Golden days (real generator patterns, no planting)
The existing golden days (Black Friday week, early January, a Saturday, a quiet October day) stay
as a smaller suite to catch regressions on "natural" data.

## Judge use
Judge the narrative (rubric in llm-judge.md): clear headline, no unsupported causal claims,
appropriate hedging, concise, British English. Exact metrics above stay primary.
