"""Shared eval harness pieces: repeat aggregation, baseline compare, gate checks, reporting."""
from __future__ import annotations

import json
import statistics
from pathlib import Path

EVALS_ROOT = Path(__file__).resolve().parent.parent
BASELINES = EVALS_ROOT / "baselines"
RESULTS = EVALS_ROOT / "results"

# metrics where lower is better; everything else is higher-is-better
LOWER_IS_BETTER = {"cost_usd", "cost_per_invoice_usd", "mean_latency_s", "failed", "needs_review"}


def aggregate(repeats: list[dict]) -> dict:
    """{metric: {"mean": x, "worst": y}} over repeats."""
    out = {}
    for k in repeats[0]:
        vals = [r[k] for r in repeats if r.get(k) is not None]
        if not vals:
            continue
        worst = max(vals) if k in LOWER_IS_BETTER else min(vals)
        out[k] = {"mean": statistics.fmean(vals), "worst": worst}
    return out


def load_baseline(suite: str) -> dict:
    p = BASELINES / f"{suite}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_baseline(suite: str, data: dict) -> Path:
    BASELINES.mkdir(parents=True, exist_ok=True)
    p = BASELINES / f"{suite}.json"
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return p


def check_gates(agg: dict, baseline: dict | None, gates: dict) -> list[str]:
    """Failure messages for gating metrics (compared on the mean)."""
    fails = []
    for metric, rule in gates.items():
        if metric not in agg:
            continue
        mean = agg[metric]["mean"]
        if "floor" in rule and mean < rule["floor"]:
            fails.append(f"{metric} {mean:.3f} below floor {rule['floor']}")
        base = (baseline or {}).get(metric)
        if base is not None and "tolerance" in rule and mean < base - rule["tolerance"]:
            fails.append(f"{metric} {mean:.3f} dropped more than {rule['tolerance']} vs baseline {base:.3f}")
    return fails


def fmt(metric: str, v) -> str:
    if v is None:
        return "–"
    if "usd" in metric:
        return f"${v:.3f}" if v < 1 else f"${v:.2f}"
    if metric.endswith("_s"):
        return f"{v:.1f}s"
    if isinstance(v, float) and v <= 1.0 and ("acc" in metric or "f1" in metric or "rate" in metric):
        return f"{v:.1%}"
    return f"{v:g}" if isinstance(v, float) else str(v)


def metrics_table(agg: dict, baseline: dict | None, gates: dict, metrics: list[str]) -> list[str]:
    lines = ["| metric | mean | worst | baseline | Δ vs baseline | gating |", "|---|---|---|---|---|---|"]
    for m in metrics:
        if m not in agg:
            continue
        a, base = agg[m], (baseline or {}).get(m)
        delta = "–" if base is None else f"{a['mean'] - base:+.3f}"
        lines.append(f"| {m} | {fmt(m, a['mean'])} | {fmt(m, a['worst'])} | {fmt(m, base)} | {delta} | "
                     f"{'yes' if m in gates else ''} |")
    return lines
