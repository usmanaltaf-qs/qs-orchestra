# Evals: invoice processing

Three things to evaluate separately, then end to end. Separating them tells you *where* a
regression came from.

## Eval set (`evals/invoices/eval_set.yml`)
Regenerated from a fixed seed by `generate_invoices.py`, written to an eval prefix only.
```yaml
seed: 4242
invoices: 200
composition:            # stratified so small slices still have enough examples
  templates: {A: 50, B: 50, C: 50, D: 50}
  scanned_rate: 0.25    # higher than prod on purpose. Scanned is where accuracy breaks
  problems: {clean: 0.5, price_variance: 0.08, qty_over_received: 0.06, missing_po: 0.06,
             wrong_po: 0.05, duplicate: 0.06, arithmetic_error: 0.05, unknown_supplier: 0.03,
             extra_charge: 0.06, bank_details_changed: 0.05}
hard_cases: 20          # multi-page template D, stamps over totals, mixed VAT, multiple problems
```
PR runs use a fixed 40-invoice stratified subset. Nightly runs use all 200.

## 1. Extraction (vs printed values in ground truth)
| metric | gating |
|---|---|
| critical-field accuracy (supplier, invoice no, date, PO, subtotal, VAT, total), digital | yes, ≥ 0.98 and −0.01 vs baseline |
| critical-field accuracy, scanned | yes, ≥ 0.90 and −0.02 |
| line-level F1 (matched lines on description + amounts) | yes, −0.02 |
| per-field accuracy, per template | no, but shown in the report (find the weak spot) |
| schema-valid first try | no |
| tokens/cost per invoice | no, but flag +20% |

Normalise before comparing: trim/case for IDs, money to 2 dp, dates ISO. Report the **specific
wrong fields** for the worst 10 invoices in the summary.

## 2. Matching rules (deterministic, no LLM)
Feed **ground-truth extracted values** (not model output) into the dbt AP models, then score status
and exception types against `injected_problems`: precision and recall per problem type, gating at
**recall 1.0 and precision 1.0**. Rules on perfect input should be perfect. Free to run, so it's layer 1, every PR.

## 3. Exception explanations
| metric | how | gating |
|---|---|---|
| suggested_action correct | expected action per problem type (map in eval_set.yml, e.g. price_variance → request credit note, duplicate → reject duplicate) | yes, −0.05 |
| grounding pass rate | from the grounding check | yes, −0.05 |
| explanation quality | judge rubric: names the actual discrepancy with correct numbers, no blame/speculation, ≤ 2 sentences | yes, −0.10 |

## 4. End to end
Real extraction → rules → status, scored against `injected_problems`. Report precision/recall per
problem type and **money metrics**: value of problem invoices wrongly auto-approved (the number a
finance director cares about, gate at £0) and clean invoices wrongly held (friction).
A drop here with flat stage-2 numbers means extraction caused it.

## Production signals (layer 4)
needs_review rate, schema-retry rate, explanation fallback rate, and **human override rate**: review
decisions that contradict `suggested_action`. Overrides are free labels, so periodically add
overridden cases (with synthetic data, the same pattern) into hard_cases.
