"""Exception explanation eval: explain every held invoice in the matching eval's ground-truth
warehouse, then score suggested_action against eval_set.yml, grounding, fallbacks and length.
Explanation quality (clarity, no speculation) needs an LLM judge calibrated on human labels:
not built yet (agent-evals references/llm-judge.md)."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "invoice-pipeline"))

import explain_exceptions  # noqa: E402

from invoices import matching  # noqa: E402

SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def expected_action(problems: list[str], cfg: dict) -> str | None:
    for p in cfg["action_priority"]:
        if p in problems:
            return cfg["expected_actions"][p]
    return None


def run(eval_root: Path, work: Path, model: str, cfg: dict, limit: int) -> tuple[dict, list[dict]]:
    m = matching.run(eval_root, work)               # ground truth -> AP models in work/ap.duckdb
    assert m["min_precision"] == 1 and m["min_recall"] == 1, "matching must be perfect before scoring explanations"
    shutil.rmtree(work / "explanations", ignore_errors=True)
    os.environ["ANTHROPIC_MODEL"] = model
    base = ["--db", str(work / "ap.duckdb"), "--output", str(work), "--limit", str(limit)]
    explain_exceptions.main(base + ["--concurrency", "1"])  # one at a time: avoids rate limits, still ~5 calls

    truth = {}
    for p in (eval_root / "_truth").glob("*.json"):
        t = json.loads(p.read_text())
        truth[t["file"]] = t
    con = duckdb.connect(str(work / "ap.duckdb"), read_only=True)
    ids = dict(con.sql("select invoice_id, file_path from fct_invoice_status").fetchall())
    rows = duckdb.sql(f"""select * from read_parquet('{work}/explanations/*.parquet')
                          qualify row_number() over (partition by invoice_id order by created_at desc) = 1""")
    cols = rows.columns
    out = []
    for r in rows.fetchall():
        d = dict(zip(cols, r))
        t = truth[ids[d["invoice_id"]]]
        d["problems"] = [p for p in t["injected_problems"] if p != "clean"]
        d["expected_action"] = expected_action(d["problems"], cfg)
        d["file_stem"] = t["file_stem"]
        out.append(d)

    llm = [d for d in out if d["source"] == "llm"]
    non_error = [d for d in out if not (d["fallback_reason"] or "").startswith("error")]
    cost = sum((4.0 * (d["input_tokens"] or 0) + 20.0 * (d["output_tokens"] or 0)) / 1e6 for d in out)
    metrics = {
        "explained": len(out),
        "suggested_action_acc": sum(d["suggested_action"] == d["expected_action"] for d in llm) / len(llm) if llm else None,
        "grounding_rate": len(llm) / len(non_error) if non_error else None,
        "error_fallbacks": len(out) - len(non_error),
        "two_sentences_max_rate": sum(len(SENTENCE_END.findall(d["summary"])) <= 2 for d in llm) / len(llm) if llm else None,
        "cost_usd": cost,
        "cost_per_explanation_usd": cost / len(out) if out else None,
    }
    return metrics, out


def report(metrics: dict, rows: list[dict]) -> list[str]:
    out = ["| metric | value |", "|---|---|"]
    for k, v in metrics.items():
        out.append(f"| {k} | {v:.1%} |" if isinstance(v, float) and v <= 1 and "usd" not in k
                   else f"| {k} | {v if not isinstance(v, float) else f'${v:.3f}'} |")
    wrong = [d for d in rows if d["source"] == "llm" and d["suggested_action"] != d["expected_action"]]
    ungrounded = [d for d in rows if d["fallback_reason"] == "ungrounded"]
    out += ["", f"**Action disagreements ({len(wrong)}):**", ""]
    out += [f"- {d['file_stem']} ({','.join(d['problems'])}): suggested '{d['suggested_action']}', "
            f"expected '{d['expected_action']}'" for d in wrong] or ["None."]
    out += ["", f"**Ungrounded ({len(ungrounded)}):**", ""]
    out += [f"- {d['file_stem']} ({','.join(d['problems'])}): {d['ungrounded_numbers']}" for d in ungrounded] or ["None."]
    by_type = Counter(d["suggested_action"] for d in rows)
    out += ["", "Actions suggested: " + ", ".join(f"{k} {v}" for k, v in by_type.most_common())]
    return out
