#!/usr/bin/env python3
"""Email a short summary of the invoice pipeline: what's new since the last email, the queue of
held invoices with their explanations, quality signals, and the latest published eval results.

Runs last in orchestra/invoice_pipeline.yml. Non-blocking: if email isn't configured or sending
fails, it logs that (never the message or credentials) and exits 0.

Reads   the AP tables (fct_invoice_status, int_ap__invoices, stg_ap__explanations) from --db
        {evals}/latest/*.json                 written by evals/run_evals.py --publish
State   {root}/notify/state.json              last_sent_at, so a rerun only reports what's new
Email   SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO (comma-separated)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import duckdb

import storage
from common import REPO_ROOT, load_env, setup_logging
from evaluate import PRICES
from storage import join

log = setup_logging("run_summary")

HELD = ("exception", "needs_review", "duplicate")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def gbp(x) -> str:
    return f"£{float(x or 0):,.2f}"


def n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def label(key: str) -> str:
    """'full/claude-opus-5-5: critical_field_acc_digital' -> 'critical field acc digital'."""
    return key.split(": ")[-1].replace("_", " ")


def cost(model: str | None, tin, tout) -> float:
    pin, pout = PRICES.get(model or "", (0, 0))
    return (pin * float(tin or 0) + pout * float(tout or 0)) / 1e6


def read_state(root: str) -> datetime:
    path = join(root, "notify", "state.json")
    if not storage.exists(path):
        return EPOCH
    return datetime.fromisoformat(json.loads(storage.read_bytes(path))["last_sent_at"])


def write_state(root: str, at: datetime) -> None:
    storage.write_bytes(join(root, "notify", "state.json"),
                        json.dumps({"last_sent_at": at.isoformat()}).encode(), "application/json")


def gather(db: str, since: datetime) -> dict:
    con = duckdb.connect(db) if db.startswith("md:") else duckdb.connect(db, read_only=True)
    try:
        q = lambda sql, *p: con.execute(sql, list(p)).fetchall()  # noqa: E731
        new = q("""select s.status, s.extraction_status, s.total_gross, s.file_hash, s.model,
                          s.input_tokens, s.output_tokens
                   from fct_invoice_status s join int_ap__invoices i using (invoice_id)
                   where i.first_seen_at > ?""", since)
        held = q("""select s.received_date, s.supplier_name, s.invoice_number, s.total_gross, s.status,
                           s.exception_types, s.suggested_action, s.explanation, s.explanation_source,
                           i.first_seen_at > ? as is_new
                    from fct_invoice_status s join int_ap__invoices i using (invoice_id)
                    where s.final_status in ('exception', 'needs_review', 'duplicate')
                    order by s.received_date desc, s.total_gross desc""", since)
        expl = q("""select model, sum(input_tokens), sum(output_tokens), count(*) filter (where explanation_source = 'llm'),
                           count(*) from stg_ap__explanations where created_at > ? group by model""", since)
        totals = dict(q("select final_status, count(*) from fct_invoice_status group by 1"))
    finally:
        con.close()

    seen, extraction_cost = set(), 0.0
    for r in new:  # byte-identical re-sends share one extraction: count its cost once
        if r[3] not in seen:
            seen.add(r[3])
            extraction_cost += cost(r[4], r[5], r[6])
    by_status = {s: sum(1 for r in new if r[0] == s) for s in ("auto_approved", "exception", "duplicate", "needs_review")}
    return {
        "new": len(new), "by_status": by_status,
        "extraction": {s: sum(1 for r in new if r[1] == s) for s in ("ok", "needs_review", "failed")},
        "new_value": sum(float(r[2] or 0) for r in new),
        "duplicate_value": sum(float(r[2] or 0) for r in new if r[0] == "duplicate"),
        "held": held, "value_on_hold": sum(float(r[3] or 0) for r in held if r[4] != "duplicate"),
        "totals": totals,
        "explanations": {"written": sum(r[4] for r in expl), "grounded": sum(r[3] for r in expl),
                         "cost": sum(cost(r[0], r[1], r[2]) for r in expl)},
        "extraction_cost": extraction_cost,
    }


def latest_evals(evals_uri: str | None) -> list[dict]:
    if not evals_uri:
        return []
    try:
        return [json.loads(storage.read_bytes(f)) for f in storage.list_files(join(evals_uri, "latest"), ".json")]
    except Exception as e:  # noqa: BLE001  (evals are optional context, never block the email)
        log.warning("couldn't read eval results: %s", type(e).__name__)
        return []


def render(d: dict, evals: list[dict], since: datetime, run_at: datetime) -> tuple[str, str]:
    held_n = len(d["held"])
    subject = (f"Invoices: {d['new']} new, {held_n} held, {gbp(d['value_on_hold'])} on hold"
               if d["new"] else f"Invoices: nothing new, {held_n} held")
    e = html.escape
    td = 'style="padding:4px 8px;border-bottom:1px solid #ddd;vertical-align:top"'
    th = 'style="padding:4px 8px;border-bottom:2px solid #999;text-align:left"'
    since_txt = "the first run" if since == EPOCH else since.strftime("%d %b %Y %H:%M UTC")
    s = d["by_status"]
    parts = [f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;max-width:900px">',
             f"<h2 style='margin:0 0 4px'>Invoice pipeline</h2>",
             f"<p style='margin:0 0 16px;color:#666'>Run at {run_at:%d %b %Y %H:%M} UTC · new since {since_txt}</p>",
             "<h3>This run</h3>",
             f"<p><b>{n(d['new'], 'new invoice')}</b> ({gbp(d['new_value'])}): {s['auto_approved']} auto-approved, "
             f"{n(s['exception'], 'exception')}, {n(s['duplicate'], 'duplicate')} ({gbp(d['duplicate_value'])} "
             f"caught), {s['needs_review']} needing review.<br>"
             f"Extraction: {d['extraction']['ok']} ok, {d['extraction']['needs_review']} needs review, "
             f"{d['extraction']['failed']} failed. Explanations: {d['explanations']['written']} written, "
             f"{d['explanations']['grounded']} grounded.<br>"
             f"API cost: ${d['extraction_cost'] + d['explanations']['cost']:.2f} "
             f"(extraction ${d['extraction_cost']:.2f}, explanations ${d['explanations']['cost']:.2f}).</p>"]
    parts += [f"<h3>Held invoices ({held_n}, {gbp(d['value_on_hold'])} on hold excl. duplicates)</h3>"]
    if d["held"]:
        parts.append(f"<table style='border-collapse:collapse'><tr><th {th}>Received</th><th {th}>Supplier</th>"
                     f"<th {th}>Invoice</th><th {th}>Total</th><th {th}>Why</th><th {th}>Suggested</th>"
                     f"<th {th}>Note</th></tr>")
        for rd, sup, num, total, status, types, action, note, src, is_new in d["held"]:
            parts.append(f"<tr><td {td}>{rd:%d %b}{' <b>new</b>' if is_new else ''}</td><td {td}>{e(sup or '?')}</td>"
                         f"<td {td}>{e(num or '?')}</td><td {td}>{gbp(total)}</td>"
                         f"<td {td}>{e((types or status).replace(',', ', '))}</td><td {td}>{e(action or '–')}</td>"
                         f"<td {td}>{e(note or 'Not explained yet.')}"
                         f"{' <i>(template)</i>' if src == 'fallback' else ''}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p>Nothing held.</p>")
    t = d["totals"]
    parts.append(f"<p style='color:#666'>All invoices so far: " +
                 ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(t.items())) + ".</p>")
    parts.append("<h3>Latest eval results</h3>")
    if evals:
        parts.append(f"<table style='border-collapse:collapse'><tr><th {th}>Suite</th><th {th}>Run</th>"
                     f"<th {th}>Gates</th><th {th}>Headline</th></tr>")
        for ev in sorted(evals, key=lambda x: x["suite"]):
            gates = "passed" if ev.get("passed") else "<b style='color:#b00'>failed</b>"
            head = ", ".join(f"{e(label(k))} {v:.1%}" if isinstance(v, float) and v <= 1 else f"{e(label(k))} {v}"
                             for k, v in ev.get("headline", {}).items())
            parts.append(f"<tr><td {td}>{e(ev['suite'])}</td><td {td}>{e(ev.get('run_id', ''))}</td>"
                         f"<td {td}>{gates}</td><td {td}>{head}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p>No eval results published yet (evals/run_evals.py --publish).</p>")
    parts.append("<p style='color:#999;font-size:12px'>Statuses come from the matching rules; "
                 "suggested actions are advice only.</p></div>")
    return subject, "\n".join(parts)


def send(subject: str, body_html: str) -> bool:
    cfg = {k: os.environ.get(k, "").strip() for k in
           ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO")}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        log.warning("email not configured (missing %s): not sending", ", ".join(missing))
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, cfg["EMAIL_FROM"]
    msg["To"] = ", ".join(a.strip() for a in cfg["EMAIL_TO"].split(",") if a.strip())
    msg.set_content("This summary is HTML. Open it in an email client that shows HTML.")
    msg.add_alternative(body_html, subtype="html")
    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], int(cfg["SMTP_PORT"]), timeout=30) as smtp:
            smtp.starttls()
            smtp.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            smtp.send_message(msg)
    except Exception as e:  # noqa: BLE001  (non-blocking; log the type only, never credentials)
        log.warning("sending failed: %s", type(e).__name__)
        return False
    log.info("sent summary to %d recipient(s)", len(msg["To"].split(",")))
    return True


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--db", default=os.environ.get("AP_WAREHOUSE", str(REPO_ROOT / "retail-dbt" / "dev.duckdb")))
    p.add_argument("--root", default=os.environ.get("INVOICES_OUTPUT", "./data/invoices/dev"),
                   help="invoices env root (state file goes to {root}/notify/)")
    p.add_argument("--evals", default=os.environ.get("EVALS_URI"), help="where run_evals.py --publish writes")
    p.add_argument("--preview", help="write the HTML here instead of sending (state unchanged)")
    p.add_argument("--all", action="store_true", help="ignore the state file: report everything as new")
    return p.parse_args(argv)


def main(argv=None) -> dict:
    args = parse_args(argv)
    load_env()
    run_at = datetime.now(timezone.utc)
    try:
        since = EPOCH if args.all else read_state(args.root)
        data = gather(args.db, since)
        subject, body = render(data, latest_evals(args.evals), since, run_at)
    except Exception as e:  # noqa: BLE001  (a summary must never fail the pipeline)
        log.warning("couldn't build the summary: %s: %s", type(e).__name__, str(e)[:200])
        return {"sent": False}
    log.info("summary: %d new, %d held", data["new"], len(data["held"]))
    if args.preview:
        Path(args.preview).write_text(f"<!-- {subject} -->\n{body}")
        log.info("preview written to %s", args.preview)
        return {"sent": False, "subject": subject}
    sent = send(subject, body)
    if sent:
        write_state(args.root, run_at)
    return {"sent": sent, "subject": subject}


if __name__ == "__main__":
    main()
