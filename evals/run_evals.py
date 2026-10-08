#!/usr/bin/env python3
"""Run eval suites and compare to the committed baseline.

  python evals/run_evals.py --suite invoices --subset pr --repeats 1
  python evals/run_evals.py --suite invoices --subset full --repeats 3 --model claude-opus-5-5 --model claude-sonnet-5-5
  python evals/run_evals.py --suite invoices --subset full --update-baseline   # deliberate, commit with the change
  python evals/run_evals.py --suite invoices-matching --subset full             # rules only, free
  python evals/run_evals.py --suite invoices-matching --subset full --extracted evals/results/<run>/<model>/rep1
  python evals/run_evals.py --suite invoices-explain --subset full --repeats 1  # 5 explanations, ~5p

Exit 1 if a gating metric fails. Writes evals/results/<run_id>/summary.md (+ metrics.json) and prints it.
Uses API tokens: every repeat re-extracts the whole subset.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESULTS, aggregate, check_gates, load_baseline, metrics_table, save_baseline  # noqa: E402


def run_invoices(args, run_dir: Path) -> tuple[str, bool]:
    from invoices import suite

    cfg = suite.load_config()
    eval_root = suite.generate(cfg, args.subset)
    gates = cfg["gates"]
    baseline_all = load_baseline("invoices")
    md = [f"# Eval: invoices ({args.subset}, {args.repeats} repeat{'s' if args.repeats > 1 else ''})", "",
          f"Run {run_dir.name} · eval set seed {cfg['seed']} · {eval_root}", ""]
    ok, new_baseline, all_metrics = True, dict(baseline_all), {}
    models = args.model or [os.environ.get("ANTHROPIC_MODEL") or sys.exit("pass --model or set ANTHROPIC_MODEL")]
    for model in models:
        reps, last_rows = [], None
        for k in range(1, args.repeats + 1):
            out = run_dir / model / f"rep{k}"
            suite.run_extraction(eval_root, out, model, args.concurrency)
            m, last_rows = suite.score(eval_root, out)
            reps.append(m)
        agg = aggregate(reps)
        key = f"{args.subset}/{model}"
        baseline = baseline_all.get(key)
        fails = check_gates(agg, baseline, gates)
        ok &= not fails
        all_metrics[key] = {"repeats": reps, "aggregate": agg}
        md += [f"## {model}", "", *metrics_table(agg, baseline, gates, suite.HEADLINE), "",
               "Critical-field accuracy by template: " + ", ".join(
                   f"{t} {agg[f'critical_field_acc_template_{t}']['mean']:.1%}" for t in "ABCD"), "",
               "**Gates:** " + ("all passed" if not fails else "; ".join(fails))
               + ("" if baseline else " (no baseline yet: floors only)"), "",
               f"### Worst invoices (repeat {args.repeats})", "", *(suite.worst_examples(last_rows) or ["None."]), ""]
        new_baseline[key] = {m: v["mean"] for m, v in agg.items()}
    if args.update_baseline:
        p = save_baseline("invoices", new_baseline)
        md.append(f"Baseline updated: {p}")
    (run_dir / "metrics.json").write_text(json.dumps(all_metrics, indent=2, default=str))
    headline = {f"{k}: {m}": v["aggregate"][m]["mean"] for k, v in all_metrics.items()
                for m in ("critical_field_acc_digital", "critical_field_acc_scanned", "line_f1")
                if m in v["aggregate"]}
    return "\n".join(md) + "\n", ok, headline


def run_matching(args, run_dir: Path) -> tuple[str, bool]:
    """Rules on ground truth (gated at precision = recall = 1.0), or end to end on a real
    extraction folder (gated on £0 of problem invoices auto-approved)."""
    from invoices import matching, suite

    cfg = suite.load_config()
    eval_root = suite.generate(cfg, args.subset)
    extracted = Path(args.extracted).resolve() if args.extracted else None
    m = matching.run(eval_root, run_dir / "matching", extracted)
    if extracted:
        title = f"End to end: real extraction ({extracted}) -> rules"
        fails = ([f"£{m['value_wrongly_auto_approved_gbp']:,.2f} of problem invoices auto-approved"]
                 if m["problem_invoices_auto_approved"] else [])
    else:
        title = "Matching rules on ground-truth input"
        fails = [f"{c}: precision {v['precision']:.2f}, recall {v['recall']:.2f}"
                 for c, v in m["per_check"].items()
                 if (v["precision"] is not None and v["precision"] < 1) or (v["recall"] is not None and v["recall"] < 1)]
    md = [f"# Eval: invoices-matching ({args.subset})", "", *matching.report(m, title),
          "**Gates:** " + ("all passed" if not fails else "; ".join(fails)), ""]
    (run_dir / "metrics.json").write_text(json.dumps(m, indent=2, default=str))
    headline = {"min precision": m["min_precision"], "min recall": m["min_recall"],
                "problem invoices auto-approved": m["problem_invoices_auto_approved"],
                "clean invoices held": m["clean_invoices_held"]}
    return "\n".join(md) + "\n", not fails, headline


def run_explain(args, run_dir: Path) -> tuple[str, bool]:
    from invoices import explain, suite

    cfg = suite.load_config()
    ecfg = cfg["explanations"]
    eval_root = suite.generate(cfg, args.subset)
    baseline_all = load_baseline("invoices-explain")
    models = args.model or [os.environ.get("ANTHROPIC_MODEL") or sys.exit("pass --model or set ANTHROPIC_MODEL")]
    md, ok, new_baseline, all_metrics = [f"# Eval: invoices-explain ({args.subset}, {args.repeats} repeat(s))", ""], \
        True, dict(baseline_all), {}
    for model in models:
        reps, rows = [], None
        for k in range(1, args.repeats + 1):
            m, rows = explain.run(eval_root, run_dir / model / f"rep{k}", model, ecfg,
                                  args.limit or ecfg.get("sample", 5))
            reps.append(m)
        agg = aggregate(reps)
        key = f"{args.subset}/{model}"
        fails = check_gates(agg, baseline_all.get(key), ecfg["gates"])
        ok &= not fails
        all_metrics[key] = {"repeats": reps, "aggregate": agg}
        md += [f"## {model}", "", *metrics_table(agg, baseline_all.get(key), ecfg["gates"], list(reps[0])), "",
               "**Gates:** " + ("all passed" if not fails else "; ".join(fails)), "",
               f"### Repeat {args.repeats}", "", *explain.report(reps[-1], rows), ""]
        new_baseline[key] = {m: v["mean"] for m, v in agg.items()}
    if args.update_baseline:
        md.append(f"Baseline updated: {save_baseline('invoices-explain', new_baseline)}")
    (run_dir / "metrics.json").write_text(json.dumps(all_metrics, indent=2, default=str))
    headline = {f"{k}: {m}": v["aggregate"][m]["mean"] for k, v in all_metrics.items()
                for m in ("suggested_action_acc", "grounding_rate") if m in v["aggregate"]}
    return "\n".join(md) + "\n", ok, headline


def publish(uri: str, suite: str, run_dir: Path, ok: bool, headline: dict) -> None:
    """Copy the run's summary and metrics to {uri}/runs/<run_id>/ and point {uri}/latest/<suite>.json
    at it. run_summary.py shows latest/ in the pipeline email."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "invoice-pipeline"))
    import storage
    from common import load_env

    load_env()
    for name in ("summary.md", "metrics.json"):
        storage.write_bytes(storage.join(uri, "runs", run_dir.name, suite, name), (run_dir / name).read_bytes())
    latest = {"suite": suite, "run_id": run_dir.name, "passed": ok, "headline": headline,
              "published_at": datetime.now(timezone.utc).isoformat()}
    storage.write_bytes(storage.join(uri, "latest", f"{suite}.json"), json.dumps(latest, default=str).encode(),
                        "application/json")
    print(f"published to {uri}/runs/{run_dir.name}/{suite}/")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--suite", choices=["invoices", "invoices-matching", "invoices-explain"], required=True)
    p.add_argument("--extracted", help="invoices-matching: score a real extraction folder end to end")
    p.add_argument("--subset", choices=["pr", "full"], default="pr")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--model", action="append", help="repeatable; default ANTHROPIC_MODEL")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--limit", type=int, default=0, help="invoices-explain: explanations per run (default from eval_set)")
    p.add_argument("--update-baseline", action="store_true")
    p.add_argument("--publish", nargs="?", const=os.environ.get("EVALS_URI", "gs://qs_orchestra/dev/evals"),
                   help="also copy results to this URI (default $EVALS_URI or gs://qs_orchestra/dev/evals)")
    args = p.parse_args(argv)
    run_dir = RESULTS / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)
    runner = {"invoices": run_invoices, "invoices-matching": run_matching, "invoices-explain": run_explain}
    md, ok, headline = runner[args.suite](args, run_dir)
    (run_dir / "summary.md").write_text(md)
    print(md)
    if args.publish:
        publish(args.publish, args.suite, run_dir, ok, headline)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
