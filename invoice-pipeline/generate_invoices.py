#!/usr/bin/env python3
"""Deterministic synthetic supplier invoices: suppliers, POs, GRNs (Parquet) and invoice PDFs.

Built on the retail generator's products and stores so everything joins up. Spec:
.claude/skills/invoice-processing/references/synthetic-invoices.md

Every invoice belongs to a slot (invoice_date, index). All of its randomness is a hash of the
slot key, a salt and the seed, so the same seed + window always produces the same output, and a
slot can be rebuilt on its own (that's how a duplicate re-sends an earlier invoice).

Layout under {output}/invoices/{env}/:
  inbox/YYYY/MM/DD/<supplier>_<invoice_no>_<hash8>.pdf     what the AP mailbox receives
  system/<table>/load_date=YYYY-MM-DD/data.parquet          ERP data (suppliers is unpartitioned)
  _truth/<pdf stem>.json                                    printed values + injected problems
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from reportlab.pdfgen import canvas

from common import Rng, env_root, money, setup_logging, write_parquet
from templates import TEMPLATES

log = setup_logging("generate_invoices")

PROBLEM_RATES = {
    "price_variance": 0.08,
    "qty_over_received": 0.05,
    "missing_po": 0.04,
    "wrong_po": 0.02,
    "duplicate": 0.03,
    "arithmetic_error": 0.03,
    "unknown_supplier": 0.01,
    "extra_charge": 0.04,
}
SECOND_PROBLEM_RATE = 0.05
# Problems that can stack on one invoice. duplicate and unknown_supplier stand alone.
COMBINABLE = ["price_variance", "qty_over_received", "missing_po", "wrong_po", "arithmetic_error",
              "extra_charge"]
EXCLUSIVE_PAIRS = {frozenset({"missing_po", "wrong_po"})}
DUPLICATE_LOOKBACK_DAYS = 5
SCAN_DPI = 150
HARD_SCAN_DPI = 110
# Hard scans put the stamp over one of these figures (a line amount, or a total).
STAMP_TARGETS = ["total_gross", "vat_total", "line_net"]

BILL_TO = ["Harbourline Retail Ltd", "Accounts Payable", "PO Box 4410", "Leeds LS1 9XX"]

# brands -> supplier. The wholesaler carries several brands, so its POs can run past one page.
SUPPLIERS = [
    # id, name, template, terms, invoice number format, brands, weight
    ("SUP-001", "Pennine Wholesale Supplies Ltd", "D", 60, "INV", ["Bramble", "Greystone", "Larkspur",
                                                                    "Marlow Lane"], 3),
    ("SUP-002", "Bramley & Hart Trading Ltd", "A", 30, "SLASH", ["Brightwell"], 1),
    ("SUP-003", "Coldharbour Goods Ltd", "B", 45, "ALPHA", ["Ashgrove"], 1),
    ("SUP-004", "Westcombe Distribution", "C", 30, "INV", ["Saltmarsh"], 1),
    ("SUP-005", "Ironbridge Supply Co", "A", 60, "ALPHA", ["Halden"], 1),
    ("SUP-006", "Marsh Lane Imports Ltd", "B", 30, "SLASH", ["Fenwick Works"], 1),
    ("SUP-007", "Fernhill Brands Ltd", "C", 45, "ALPHA", ["Thistle"], 1),
    ("SUP-008", "Calder Valley Trading", "A", 30, "INV", ["Kestrel & Co"], 1),
    ("SUP-009", "Seacroft Merchants Ltd", "D", 45, "SLASH", ["Northbay"], 1),
    ("SUP-010", "Hollins & Webb Ltd", "B", 60, "INV", ["Peregrine"], 1),
    ("SUP-011", "Dunmore Supply Partners", "C", 30, "SLASH", ["Copperleaf", "Oakmere"], 1),
]
# Not in master data: used for unknown_supplier.
UNKNOWN_SUPPLIERS = [("Redgate Trading Ltd", "A"), ("Tamworth Sourcing Co", "B"),
                     ("Kingsmead Products Ltd", "C")]

STREETS = ["Mill Road", "Canal Street", "Station Approach", "Victoria Way", "Brunswick Park",
           "Foundry Lane", "Riverside Court", "Wharf Road", "Albion Works", "Chapel Yard"]
TOWNS = [("Huddersfield", "HD1"), ("Wakefield", "WF1"), ("Stockport", "SK4"), ("Derby", "DE1"),
         ("Bolton", "BL1"), ("Preston", "PR1"), ("Doncaster", "DN1"), ("Telford", "TF3"),
         ("Halifax", "HX1"), ("Burnley", "BB11")]
CITY_POSTCODES = {"London": "EC1", "Manchester": "M1", "Liverpool": "L1", "Birmingham": "B1",
                  "Nottingham": "NG1", "Leicester": "LE1", "Leeds": "LS1", "Sheffield": "S1",
                  "York": "YO1", "Newcastle": "NE1", "Glasgow": "G1", "Edinburgh": "EH1",
                  "Aberdeen": "AB10", "Cardiff": "CF10", "Swansea": "SA1", "Bristol": "BS1",
                  "Exeter": "EX1", "Brighton": "BN1", "Southampton": "SO14", "Oxford": "OX1",
                  "Cambridge": "CB1", "Norwich": "NR1", "Belfast": "BT1"}


@dataclass
class Slot:
    day: date
    idx: int
    supplier: dict
    problems: list = field(default_factory=list)
    scanned: bool = False
    hard_scan: bool = False             # degraded scan with the stamp over a figure
    template: str | None = None         # set when templates are balanced
    duplicate_of: tuple | None = None   # (day, idx) of the re-sent invoice
    wrong_po_from: tuple | None = None  # (day, idx) whose PO number gets printed

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()}|{self.idx}"


class Generator:
    def __init__(self, seed: int, source: str, invoices_per_day: int, scanned_rate: float,
                 hard_scan_rate: float = 0.0, problem_rates: dict | None = None, balance_templates: bool = False):
        self.rng = Rng(seed)
        self.seed = seed
        self.per_day = invoices_per_day
        self.scanned_rate = scanned_rate
        self.hard_scan_rate = hard_scan_rate
        self.problem_rates = problem_rates or PROBLEM_RATES
        self.balance_templates = balance_templates
        self.products, self.stores = load_dims(source)
        self.suppliers = self._build_suppliers()
        self.plans: dict[tuple, Slot] = {}

    # ------------------------------------------------------------------ master data

    def _party(self, key: str, name: str) -> dict:
        r = self.rng
        town, pc = r.choice(key, "town", TOWNS)
        slug = re.sub(r"[^a-z]", "", name.lower().split()[0])
        return {
            "name": name,
            "address": [f"{r.randint(key, 'num', 1, 220)} {r.choice(key, 'street', STREETS)}",
                        f"{town} {pc} {r.randint(key, 'pc1', 1, 9)}{chr(65 + r.randint(key, 'pc2', 0, 25))}"
                        f"{chr(65 + r.randint(key, 'pc3', 0, 25))}"],
            "vat": f"GB {r.randint(key, 'vat1', 100, 999)} {r.randint(key, 'vat2', 1000, 9999)} "
                   f"{r.randint(key, 'vat3', 10, 99)}",
            "sort_code": f"00-00-{r.randint(key, 'sort', 10, 99)}",
            "account": f"0000{r.randint(key, 'acct', 1000, 9999)}",
            "email": f"accounts@{slug}.example",
            "phone": f"01632 960{r.randint(key, 'tel', 100, 999)}",
        }

    def _build_suppliers(self) -> list[dict]:
        out = []
        for sid, name, template, terms, fmt, brands, weight in SUPPLIERS:
            prods = [p for p in self.products if p["brand"] in brands]
            if not prods:
                log.warning("supplier %s has no active products in the source data; skipped", sid)
                continue
            out.append({"supplier_id": sid, "template": template, "terms_days": terms, "fmt": fmt,
                        "weight": weight, "products": prods, "wholesaler": len(brands) > 2,
                        **self._party(sid, name)})
        return out

    def unknown_party(self, slot: Slot) -> tuple[dict, str]:
        name, template = self.rng.choice(slot.key, "unknown", UNKNOWN_SUPPLIERS)
        party = self._party(f"unknown|{name}", name)
        return {**party, "supplier_id": None, "terms_days": 30, "fmt": "INV"}, slot.template or template

    # ------------------------------------------------------------------ planning

    def base_slot(self, d: date, i: int) -> Slot:
        key = f"{d.isoformat()}|{i}"
        r = self.rng
        template = None
        pool = self.suppliers
        if self.balance_templates:  # rotate A-D through the slots so every template gets the same count
            template = "ABCD"[(d.toordinal() * self.per_day + i) % 4]
            pool = [s for s in self.suppliers if s["template"] == template] or self.suppliers
        sup = r.weighted(key, "supplier", [(s, s["weight"]) for s in pool])
        slot = Slot(d, i, sup, scanned=r.u(key, "scanned") < self.scanned_rate, template=template)
        slot.hard_scan = slot.scanned and r.u(key, "hard_scan") < self.hard_scan_rate
        x = r.u(key, "problem")
        for p, rate in self.problem_rates.items():
            if x < rate:
                slot.problems = [p]
                break
            x -= rate
        if slot.problems and slot.problems[0] in COMBINABLE and r.u(key, "problem2") < SECOND_PROBLEM_RATE:
            other = [p for p in COMBINABLE if p != slot.problems[0]
                     and frozenset({p, slot.problems[0]}) not in EXCLUSIVE_PAIRS]
            slot.problems.append(r.choice(key, "problem2.pick", other))
        return slot

    def plan(self, d: date, i: int) -> Slot:
        """Slot plan, resolving duplicate/wrong_po targets. Cached; full mode may pre-seed it."""
        if (d, i) not in self.plans:
            slot = self.base_slot(d, i)
            self.plans[(d, i)] = slot
            self._resolve(slot)
        return self.plans[(d, i)]

    def _resolve(self, slot: Slot, window_start: date | None = None) -> bool:
        """Pick the targets that duplicate / wrong_po need. Drops the problem if none exist."""
        ok = True
        if "duplicate" in slot.problems:
            target = self._dup_target(slot, window_start)
            if target:
                slot.duplicate_of = target
            else:
                slot.problems.remove("duplicate")
                ok = False
        if "wrong_po" in slot.problems:
            def other(j):
                return self.plans.get((slot.day, j)) or self.base_slot(slot.day, j)
            others = [j for j in range(self.per_day) if j != slot.idx
                      and other(j).supplier["supplier_id"] != slot.supplier["supplier_id"]
                      and "duplicate" not in other(j).problems]
            if others:
                slot.wrong_po_from = (slot.day, self.rng.choice(slot.key, "wrong_po", others))
            else:
                slot.problems.remove("wrong_po")
                ok = False
        return ok

    def _dup_target(self, slot: Slot, window_start: date | None):
        cands = []
        # a re-send arrives on a later day than the original, so arrival order is unambiguous
        for back in range(1, DUPLICATE_LOOKBACK_DAYS + 1):
            d = slot.day - timedelta(days=back)
            if window_start and d < window_start:
                break
            for j in range(self.per_day):
                other = self.plans.get((d, j)) or self.base_slot(d, j)
                if not other.problems:  # re-send a clean invoice, so the duplicate is the only problem
                    cands.append((d, j))
        return self.rng.choice(slot.key, "dup_target", cands) if cands else None

    def plan_window(self, start: date, end: date, min_per_problem: int) -> list[Slot]:
        days = [start + timedelta(days=k) for k in range((end - start).days + 1)]
        slots = []
        for d in days:
            for i in range(self.per_day):
                s = self.base_slot(d, i)
                self.plans[(d, i)] = s
                slots.append(s)
        for s in slots:
            self._resolve(s, window_start=start)
        if min_per_problem:
            counts = Counter(p for s in slots for p in s.problems)
            for p in self.problem_rates:
                need = min_per_problem - counts[p]
                targets = {t for s in slots for t in (s.duplicate_of, s.wrong_po_from) if t}
                clean = sorted((s for s in slots if not s.problems and (s.day, s.idx) not in targets),
                               key=lambda s: self.rng.u(s.key, f"topup|{p}"))
                for s in clean:
                    if need <= 0:
                        break
                    s.problems = [p]
                    if self._resolve(s, window_start=start):
                        need -= 1
        return slots

    # ------------------------------------------------------------------ building

    def doc_ids(self, slot: Slot):
        """PO and GRN numbers/dates. Unique because the lag and slot index are encoded in the number."""
        grn_lag = self.rng.randint(slot.key, "grn_lag", 0, 5)
        po_lag = self.rng.randint(slot.key, "po_lag", 2, 10)
        grn_date = slot.day - timedelta(days=grn_lag)
        po_date = grn_date - timedelta(days=po_lag)
        return (f"PO-{po_date:%Y%m%d}-{(grn_lag + po_lag) * 100 + slot.idx:04d}", po_date,
                f"GRN-{grn_date:%Y%m%d}-{grn_lag * 100 + slot.idx:04d}", grn_date)

    def build_core(self, slot: Slot) -> dict:
        """PO, GRN and the printed invoice for a non-duplicate slot."""
        r, key, sup, d = self.rng, slot.key, slot.supplier, slot.day
        probs = set(slot.problems)
        store = r.choice(key, "store", self.stores)
        po_number, po_date, grn_number, grn_date = self.doc_ids(slot)

        lo, hi = (4, 20) if sup["wholesaler"] else (3, 6)
        n = min(r.randint(key, "n_lines", lo, hi), len(sup["products"]))
        prods = sorted(sup["products"], key=lambda p: r.u(f"{key}|{p['product_id']}", "pick"))[:n]
        prods.sort(key=lambda p: p["sku"])

        po_lines, grn_lines = [], []
        for ln, p in enumerate(prods, 1):
            qty = r.randint(f"{key}|{ln}", "qty", 5, 60)
            po_lines.append({"po_number": po_number, "line_no": ln, "product_id": p["product_id"],
                             "sku": p["sku"], "description": p["product_name"], "qty_ordered": qty,
                             "unit_cost": money(p["unit_cost"]),
                             "vat_rate": Decimal("0.00") if p["category"] == "Food" else Decimal("0.20")})
            grn_lines.append({"grn_number": grn_number, "po_number": po_number, "line_no": ln,
                              "product_id": p["product_id"], "qty_received": qty})
        if "qty_over_received" in probs:
            gl = grn_lines[r.randint(key, "short.line", 0, n - 1)]
            gl["qty_received"] -= r.randint(key, "short.qty", 1, max(1, gl["qty_received"] // 5))

        lines = [{"description": pl["description"], "sku": pl["sku"], "quantity": pl["qty_ordered"],
                  "unit_price": pl["unit_cost"], "vat_rate": pl["vat_rate"]} for pl in po_lines]
        if "price_variance" in probs:
            ln = lines[r.randint(key, "pv.line", 0, n - 1)]
            ln["unit_price"] = money(ln["unit_price"] * Decimal(str(1.03 + 0.12 * r.u(key, "pv.pct"))))
        if "extra_charge" in probs:
            lines.append({"description": "Delivery charge", "sku": None, "quantity": 1,
                          "unit_price": money(r.randint(key, "delivery", 30, 90) / 2), "vat_rate": Decimal("0.20")})
        for ln in lines:
            ln["line_net"] = money(ln["unit_price"] * ln["quantity"])

        if "unknown_supplier" in probs:
            party, template = self.unknown_party(slot)
        else:
            party, template = sup, sup["template"]
        if template == "C":  # no SKU column on the document
            for ln in lines:
                ln["sku"] = None

        printed_po = po_number
        if "missing_po" in probs:
            printed_po = None
        elif "wrong_po" in probs:
            printed_po = self.doc_ids(self.plan(*slot.wrong_po_from))[0]

        inv = {
            "supplier": party, "template": template,
            "invoice_number": invoice_number(party, d, slot.idx),
            "invoice_date": d, "due_date": d + timedelta(days=party["terms_days"]),
            "po_number": printed_po, "lines": lines, "is_copy": False,
            "bill_to": BILL_TO, "deliver_to": deliver_to(store, self.rng),
        }
        totals(inv)
        if "arithmetic_error" in probs:
            inject_arithmetic_error(inv, r, key)
        return {
            "invoice": inv,
            "po": {"po_number": po_number, "supplier_id": sup["supplier_id"], "store_id": store["store_id"],
                   "order_date": po_date, "currency": "GBP"},
            "po_lines": po_lines,
            "grn": {"grn_number": grn_number, "po_number": po_number, "received_date": grn_date},
            "grn_lines": grn_lines,
        }

    def build(self, slot: Slot) -> tuple[dict, dict]:
        """(printed invoice, core). A duplicate re-sends the original's invoice; its core is the
        original's and its PO/GRN must not be emitted again."""
        if slot.duplicate_of:
            core = self.build_core(self.plan(*slot.duplicate_of))
            inv = {**core["invoice"], "is_copy": self.rng.u(slot.key, "copy_mark") < 0.5}
            return inv, core
        core = self.build_core(slot)
        return core["invoice"], core

    def file_stem(self, slot: Slot, inv: dict) -> str:
        h8 = Rng(self.seed).u(slot.key, "filehash")
        name = re.sub(r"[^a-z0-9]+", "-", inv["supplier"]["name"].lower()).strip("-")
        inv_no = re.sub(r"[^A-Za-z0-9-]+", "-", inv["invoice_number"])
        return f"{name}_{inv_no}_{int(h8 * 2**32):08x}"


# ---------------------------------------------------------------------- invoice maths


def invoice_number(party: dict, d: date, i: int) -> str:
    n = (d - date(2024, 1, 1)).days * 100 + i
    if party["fmt"] == "SLASH":
        return f"{d.year}/{d.timetuple().tm_yday * 100 + i:05d}"
    if party["fmt"] == "ALPHA":
        return f"A{n:06d}"
    return f"INV-{n:06d}"


def totals(inv: dict) -> None:
    by_rate: dict[Decimal, Decimal] = {}
    for ln in inv["lines"]:
        by_rate[ln["vat_rate"]] = by_rate.get(ln["vat_rate"], Decimal("0")) + ln["line_net"]
    inv["vat_summary"] = [(rate, net, money(net * rate)) for rate, net in sorted(by_rate.items(), reverse=True)]
    inv["subtotal_net"] = money(sum(ln["line_net"] for ln in inv["lines"]))
    inv["vat_total"] = money(sum(v for _, _, v in inv["vat_summary"]))
    inv["total_gross"] = inv["subtotal_net"] + inv["vat_total"]


def inject_arithmetic_error(inv: dict, r: Rng, key: str) -> None:
    """Make the document disagree with itself. Later totals are recomputed from the printed values
    so exactly one relationship is broken."""
    kind = r.choice(key, "arith.kind", ["line", "vat", "gross"])
    delta = money(r.randint(key, "arith.delta", 100, 4000) / 100)
    if kind == "line":
        ln = inv["lines"][r.randint(key, "arith.line", 0, len(inv["lines"]) - 1)]
        ln["line_net"] += delta  # qty x unit price no longer equals the line total
        totals(inv)            # subtotal/VAT follow the printed lines, so only that line is off
    elif kind == "vat":
        rate, net, vat = inv["vat_summary"][0]
        inv["vat_summary"][0] = (rate, net, vat + delta)
        inv["vat_total"] = money(sum(v for _, _, v in inv["vat_summary"]))
        inv["total_gross"] = inv["subtotal_net"] + inv["vat_total"]
    else:
        inv["total_gross"] += delta
    inv["arithmetic_error_kind"] = kind


def deliver_to(store: dict, r: Rng) -> list[str]:
    key = f"store|{store['store_id']}"
    pc = CITY_POSTCODES.get(store["city"], store["city"][:2].upper() + "1")
    return [f"{store['store_name']} (store {store['store_id']})",
            f"Unit {r.randint(key, 'unit', 1, 40)}, {r.choice(key, 'street', STREETS)}",
            f"{store['city']} {pc} {r.randint(key, 'pc', 1, 9)}{chr(65 + r.randint(key, 'pca', 0, 25))}"
            f"{chr(65 + r.randint(key, 'pcb', 0, 25))}"]


# ---------------------------------------------------------------------- rendering


def render_pdf(inv: dict) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, invariant=1)  # invariant: no timestamps, so bytes are reproducible
    c.setTitle(f"Invoice {inv['invoice_number']}")
    TEMPLATES[inv["template"]](c, inv)
    c.save()
    return buf.getvalue()


def find_text(doc, needle: str, last: bool):
    """(page index, (left, top, right, bottom) in PDF points from the top-left) of `needle`."""
    hits = []
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        searcher = tp.search(needle, match_case=True)
        while (occ := searcher.get_next()) is not None:
            start, count = occ
            boxes = [tp.get_charbox(start + k) for k in range(count)]
            h = page.get_height()
            hits.append((i, (min(b[0] for b in boxes), h - max(b[3] for b in boxes),
                             max(b[2] for b in boxes), h - min(b[1] for b in boxes))))
    return (hits[-1] if last else hits[0]) if hits else None


def scan(pdf_bytes: bytes, seed_key: str, rng: Rng, received: date, hard: bool = False,
         stamp_over: tuple[str, str] | None = None) -> bytes:
    """Make a digital PDF look scanned: greyscale image, skew, blur, specks, maybe a stamp.

    hard: lower resolution, heavier blur/noise/compression, and the stamp placed over the printed
    figure stamp_over = (field, text) instead of in a blank-ish header area."""
    if hard:
        return _hard_scan(pdf_bytes, seed_key, rng, received, stamp_over)
    rnd = random.Random(int(rng.u(seed_key, "scan") * 2**53))
    doc = pdfium.PdfDocument(pdf_bytes)
    pages = []
    for i in range(len(doc)):
        img = doc[i].render(scale=SCAN_DPI / 72).to_pil().convert("L")
        w, h = img.size
        if i == 0 and rnd.random() < 0.6:
            img = stamp(img, received, rnd)
        img = img.rotate(rnd.uniform(-2, 2), resample=Image.BICUBIC, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(rnd.uniform(0.5, 1.0)))
        noise = Image.frombytes("L", (w, h), rnd.randbytes(w * h))
        specks = noise.point(lambda v: 60 if v < 2 else 255)
        img = ImageChops.darker(img, specks)
        img = Image.blend(img, Image.new("L", (w, h), 235), 0.08)  # paper tone, slightly washed out
        pages.append(img)
    doc.close()
    out = io.BytesIO()
    stamp_time = datetime(received.year, received.month, received.day).timetuple()
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=SCAN_DPI, quality=70,
                  creationDate=stamp_time, modDate=stamp_time)  # fixed dates keep bytes reproducible
    return out.getvalue()


def _hard_scan(pdf_bytes: bytes, seed_key: str, rng: Rng, received: date, stamp_over) -> bytes:
    rnd = random.Random(int(rng.u(seed_key, "scan.hard") * 2**53))
    scale = HARD_SCAN_DPI / 72
    doc = pdfium.PdfDocument(pdf_bytes)
    hit = None
    if stamp_over:
        field, needle = stamp_over
        hit = find_text(doc, needle, last=field != "line_net")
    pages = []
    for i in range(len(doc)):
        img = doc[i].render(scale=scale).to_pil().convert("L")
        w, h = img.size
        if hit and hit[0] == i:
            left, top, right, bottom = hit[1]
            img = stamp(img, received, rnd, centre=(int((left + right) / 2 * scale), int((top + bottom) / 2 * scale)),
                        size=HARD_SCAN_DPI / SCAN_DPI)
        img = img.rotate(rnd.uniform(-2, 2), resample=Image.BICUBIC, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(rnd.uniform(1.0, 1.4)))
        noise = Image.frombytes("L", (w, h), rnd.randbytes(w * h))
        img = ImageChops.darker(img, noise.point(lambda v: 70 if v < 6 else 255))
        img = Image.blend(img, Image.new("L", (w, h), 225), 0.2)  # faded, low contrast
        pages.append(img)
    doc.close()
    out = io.BytesIO()
    stamp_time = datetime(received.year, received.month, received.day).timetuple()
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=HARD_SCAN_DPI, quality=35,
                  creationDate=stamp_time, modDate=stamp_time)
    return out.getvalue()


def stamp(img: Image.Image, received: date, rnd: random.Random, centre: tuple[int, int] | None = None,
          size: float = 1.0) -> Image.Image:
    """Overlay a RECEIVED stamp. Without `centre` it lands somewhere in the upper part of the page."""
    w, h = img.size
    layer = Image.new("L", (520, 170), 0)
    dr = ImageDraw.Draw(layer)
    try:
        font = ImageFont.load_default(size=56)
        small = ImageFont.load_default(size=34)
    except TypeError:  # Pillow < 10.1
        font = small = ImageFont.load_default()
    dr.rectangle([4, 4, 515, 165], outline=255, width=6)
    dr.text((30, 18), "RECEIVED", fill=255, font=font)
    dr.text((30, 100), received.strftime("%d %b %Y").upper(), fill=255, font=small)
    if size != 1.0:
        layer = layer.resize((int(layer.width * size), int(layer.height * size)), Image.BICUBIC)
    layer = layer.rotate(rnd.uniform(-18, 18), expand=True, resample=Image.BICUBIC)
    if centre:
        # put a stamp stroke (the date text sits ~70% down the stamp) across the figure
        x = centre[0] - int(layer.width * rnd.uniform(0.3, 0.6))
        y = centre[1] - int(layer.height * rnd.uniform(0.55, 0.75))
    else:
        x = int(rnd.uniform(0.35, 0.65) * (w - layer.width))
        y = int(rnd.uniform(0.05, 0.35) * (h - layer.height))
    mask = Image.new("L", img.size, 0)
    mask.paste(layer.point(lambda v: int(v * 0.7)), (x, y))
    return Image.composite(Image.new("L", img.size, 90), img, mask)


# ---------------------------------------------------------------------- IO


def load_dims(source: str):
    con = duckdb.connect()
    src = source.rstrip("/")
    products = con.execute(
        f"SELECT product_id, sku, product_name, category, brand, unit_cost FROM read_parquet('{src}/products/*.parquet') "
        "WHERE is_active ORDER BY product_id").fetchall()
    stores = con.execute(
        f"SELECT store_id, store_name, city FROM read_parquet('{src}/stores/*.parquet') ORDER BY store_id").fetchall()
    pcols = ["product_id", "sku", "product_name", "category", "brand", "unit_cost"]
    return ([dict(zip(pcols, p)) for p in products],
            [dict(zip(["store_id", "store_name", "city"], s)) for s in stores])


def to_json(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, date):
        return v.isoformat()
    raise TypeError(type(v))


def truth_record(inv: dict, slot: Slot, stem: str, rel_path: str, pages: int, core: dict, orig_stem: str | None,
                 stamp_over: str | None = None):
    s = inv["supplier"]
    return {
        # what perfect extraction returns (the printed values)
        "supplier_name": s["name"],
        "supplier_vat_number": s["vat"],
        "invoice_number": inv["invoice_number"],
        "invoice_date": inv["invoice_date"].isoformat(),
        "po_number": inv["po_number"],
        "currency": "GBP",
        "lines": [{k: ln[k] for k in ("description", "sku", "quantity", "unit_price", "vat_rate", "line_net")}
                  for ln in inv["lines"]],
        "subtotal_net": inv["subtotal_net"],
        "vat_total": inv["vat_total"],
        "total_gross": inv["total_gross"],
        "bank_sort_code": s["sort_code"],
        "bank_account": s["account"],
        "is_copy_or_duplicate_marked": inv["is_copy"],
        # what matching should find
        "injected_problems": sorted(slot.problems) or ["clean"],
        "arithmetic_error_kind": inv.get("arithmetic_error_kind"),
        # context for evaluation
        "file": rel_path,
        "file_stem": stem,
        "template": inv["template"],
        "scanned": slot.scanned,
        "scan_profile": ("hard" if slot.hard_scan else "standard") if slot.scanned else None,
        "stamp_over": stamp_over,
        "pages": pages,
        "slot": slot.key,
        "supplier_id": s.get("supplier_id"),
        "po_number_actual": core["po"]["po_number"],
        "duplicate_of_file_stem": orig_stem,
    }


SYSTEM_SCHEMAS = {
    "purchase_orders": "po_number VARCHAR, supplier_id VARCHAR, store_id INTEGER, order_date DATE, currency VARCHAR",
    "po_lines": "po_number VARCHAR, line_no INTEGER, product_id INTEGER, sku VARCHAR, description VARCHAR, "
                "qty_ordered INTEGER, unit_cost DECIMAL(10,2), vat_rate DECIMAL(4,2)",
    "goods_receipts": "grn_number VARCHAR, po_number VARCHAR, received_date DATE",
    "grn_lines": "grn_number VARCHAR, po_number VARCHAR, line_no INTEGER, product_id INTEGER, qty_received INTEGER",
    "suppliers": "supplier_id VARCHAR, supplier_name VARCHAR, vat_number VARCHAR, payment_terms_days INTEGER, "
                 "template VARCHAR, bank_sort_code VARCHAR, bank_account VARCHAR, address_line1 VARCHAR, "
                 "address_line2 VARCHAR",
}


def write_table(path: Path, table: str, rows: list[dict]) -> None:
    write_parquet(path, SYSTEM_SCHEMAS[table], rows)


def env(name: str, default=None):
    value = os.environ.get(f"INVOICES_{name}")
    return default if value is None else value


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mode", choices=["full", "incremental"], default=env("MODE", "full"))
    p.add_argument("--run-date", default=env("RUN_DATE", ""),
                   help="last invoice day (full) or the one day to add (incremental). Default: today UTC")
    p.add_argument("--days-back", type=int, default=int(env("DAYS_BACK", 30)),
                   help="full mode: number of invoice days ending on --run-date")
    p.add_argument("--invoices-per-day", type=int, default=int(env("INVOICES_PER_DAY", 20)))
    p.add_argument("--scanned-rate", type=float, default=float(env("SCANNED_RATE", 0.15)))
    p.add_argument("--hard-scan-rate", type=float, default=float(env("HARD_SCAN_RATE", 0.0)),
                   help="share of scanned invoices that are hard: degraded, stamp over a figure")
    p.add_argument("--problem-rates", default=env("PROBLEM_RATES", ""),
                   help='JSON overriding PROBLEM_RATES, e.g. \'{"duplicate": 0.06}\'')
    p.add_argument("--balance-templates", action="store_true",
                   help="rotate templates A-D through the slots (eval sets) instead of by supplier weight")
    p.add_argument("--min-per-problem", type=int, default=int(env("MIN_PER_PROBLEM", 1)),
                   help="full mode: top up so each problem type appears at least this often")
    p.add_argument("--seed", type=int, default=int(env("SEED", 42)))
    p.add_argument("--source", default=os.environ.get("RETAIL_SOURCE_URI", "./data"),
                   help="retail Parquet root with products/ and stores/")
    p.add_argument("--output", default=env("OUTPUT", "./data"))
    p.add_argument("--env", default=env("ENV", "dev"), choices=["dev", "eval", "prod"])
    args = p.parse_args(argv)
    if not 1 <= args.invoices_per_day < 100:
        p.error("--invoices-per-day must be 1..99 (PO numbers encode the slot index in two digits)")
    rates = dict(PROBLEM_RATES)
    if args.problem_rates:
        override = json.loads(args.problem_rates)
        unknown = set(override) - set(PROBLEM_RATES)
        if unknown:
            p.error(f"unknown problem types: {', '.join(sorted(unknown))}")
        rates.update(override)
    if sum(rates.values()) > 1:
        p.error("problem rates sum to more than 1")
    args.problem_rates = rates
    return args


def main(argv=None) -> dict:
    args = parse_args(argv)
    run_date = date.fromisoformat(args.run_date) if args.run_date else datetime.now(timezone.utc).date()
    root = env_root(args.output, args.env)
    gen = Generator(args.seed, args.source, args.invoices_per_day, args.scanned_rate, args.hard_scan_rate,
                    args.problem_rates, args.balance_templates)

    if args.mode == "full":
        start = run_date - timedelta(days=args.days_back - 1)
        slots = gen.plan_window(start, run_date, args.min_per_problem)
        for sub in ("inbox", "system", "_truth"):
            shutil.rmtree(root / sub, ignore_errors=True)
    else:
        start = run_date
        slots = [gen.plan(run_date, i) for i in range(args.invoices_per_day)]

    stems: dict[tuple, str] = {}
    system: dict[date, dict[str, list]] = {}
    counts, templates, scanned, hard = Counter(), Counter(), 0, 0
    for slot in sorted(slots, key=lambda s: (s.day, s.idx)):
        inv, core = gen.build(slot)
        stem = gen.file_stem(slot, inv)
        stems[(slot.day, slot.idx)] = stem
        rel = f"inbox/{slot.day:%Y/%m/%d}/{stem}.pdf"
        pdf = render_pdf(inv)
        pages = len(pdfium.PdfDocument(pdf))
        stamp_over = None
        if slot.scanned:
            target = None
            if slot.hard_scan:
                stamp_over = gen.rng.choice(slot.key, "stamp_over", STAMP_TARGETS)
                if stamp_over == "line_net":
                    ln = gen.rng.choice(slot.key, "stamp_over.line", inv["lines"])
                    target = (stamp_over, f"{ln['line_net']:,.2f}")
                else:
                    target = (stamp_over, f"{inv[stamp_over]:,.2f}")
                hard += 1
            pdf = scan(pdf, slot.key, gen.rng, slot.day, hard=slot.hard_scan, stamp_over=target)
            scanned += 1
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(pdf)

        orig_stem = None
        if slot.duplicate_of:
            o = gen.plan(*slot.duplicate_of)
            orig_stem = stems.get(slot.duplicate_of) or gen.file_stem(o, core["invoice"])
        rec = truth_record(inv, slot, stem, rel, pages, core, orig_stem, stamp_over)
        (root / "_truth").mkdir(parents=True, exist_ok=True)
        (root / "_truth" / f"{stem}.json").write_text(json.dumps(rec, indent=2, default=to_json))

        if not slot.duplicate_of:
            day = system.setdefault(slot.day, {t: [] for t in SYSTEM_SCHEMAS if t != "suppliers"})
            day["purchase_orders"].append(core["po"])
            day["po_lines"].extend(core["po_lines"])
            day["goods_receipts"].append(core["grn"])
            day["grn_lines"].extend(core["grn_lines"])
        counts.update(rec["injected_problems"])
        templates[inv["template"]] += 1

    for d, tables in system.items():
        for t, rows in tables.items():
            write_table(root / "system" / t / f"load_date={d.isoformat()}" / "data.parquet", t, rows)
    write_table(root / "system" / "suppliers" / "data.parquet", "suppliers", [
        {"supplier_id": s["supplier_id"], "supplier_name": s["name"], "vat_number": s["vat"],
         "payment_terms_days": s["terms_days"], "template": s["template"], "bank_sort_code": s["sort_code"],
         "bank_account": s["account"], "address_line1": s["address"][0], "address_line2": s["address"][1]}
        for s in gen.suppliers])

    summary = {"mode": args.mode, "window": f"{start}..{run_date}", "env": args.env,
               "invoices": len(slots), "scanned": scanned, "hard_scans": hard, "templates": dict(sorted(templates.items())),
               "problems": dict(sorted(counts.items()))}
    log.info("run summary %s", json.dumps(summary))
    return summary


if __name__ == "__main__":
    main()
