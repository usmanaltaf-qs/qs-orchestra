# LLM-as-judge

Use judges only for qualities without an exact answer. Never as the only gate on something
measurable.

## Design
- **Binary criteria, not 1–10 scores.** Each criterion is pass/fail with a one-sentence reason. Scores
  drift and nobody agrees what a 7 means.
- **One criterion per call** (or a forced-tool JSON with one field per criterion). Ask for the reason
  before the verdict.
- **Give the judge the evidence:** the output and the inputs/query results it was based on, so
  "unsupported claim" can actually be checked.
- **Judge model via `JUDGE_MODEL`.** Prefer a model at least as capable as the one being judged,
  and keep it fixed across comparisons (changing the judge invalidates baselines).
- Rubrics live in `evals/judges/*.md`, versioned. A rubric change means a new baseline.

## Rubrics
**Insights narrative**
1. headline states the most important change with a number
2. no causal claim beyond what the evidence shows (correlation phrased as such)
3. hedges when evidence is thin ("unclear", "possibly") rather than overclaiming
4. no real-world events, weather, competitors (data is synthetic)
5. concise: no filler, no repetition across findings
6. British English, GBP formatting

**Invoice exception explanation**
1. names the specific discrepancy (which line, what field)
2. numbers match the evidence tables
3. suggested action is consistent with the discrepancy
4. neutral tone, no blame or speculation about intent

## Calibration (do this before trusting a judge)
1. Generate ~30 outputs per rubric, including deliberately bad variants (e.g. a prompt tweaked to
   overclaim) so failures exist.
2. The user labels them pass/fail per criterion. A tiny `label.py` CLI or a CSV is fine.
3. Measure judge agreement with the user's labels per criterion. Target ≥ 90%. Below that,
   rewrite the criterion or drop it.
4. Store the labels in `evals/judges/labels/`. Re-check agreement whenever the rubric or judge model changes.

## Cost
Judge on a sample in PR runs (e.g. 10 outputs) and on everything nightly. Log judge tokens
separately from agent tokens.
