"""Template D: dense, small font, VAT codes (S/Z) with a legend, multi-page past 15 lines with
continued table headers. Totals and VAT analysis on the last page."""
import math

from .base import H, MARGIN, W, copy_watermark, fmt_date, fmt_money, hline, lines_block, table, text

ROWS_PER_PAGE = 15
VAT_CODE = {"0.20": "S", "0.00": "Z"}


def _code(rate):
    return VAT_CODE[f"{rate:.2f}"]


def render(c, inv):
    s = inv["supplier"]
    lines = inv["lines"]
    pages = max(1, math.ceil(len(lines) / ROWS_PER_PAGE))
    cols = [(MARGIN, "left"), (60, "left"), (120, "left"), (400, "right"), (455, "right"), (480, "left"),
            (W - MARGIN, "right")]
    header = ["#", "Code", "Description", "Qty", "Price", "VC", "Net"]

    for p in range(pages):
        if inv["is_copy"]:
            copy_watermark(c)
        y = H - MARGIN
        text(c, MARGIN, y, s["name"], 11, bold=True)
        text(c, MARGIN, y - 11, " | ".join(s["address"]) + f" | VAT {s['vat']}", 6.5)
        text(c, W - MARGIN, y, "INVOICE", 11, bold=True, align="right")
        text(c, W - MARGIN, y - 11, f"Page {p + 1} of {pages}", 6.5, align="right")
        hline(c, MARGIN, W - MARGIN, y - 16)

        if p == 0:
            meta = [("Inv no", inv["invoice_number"]), ("Tax point", fmt_date(inv["invoice_date"], "num")),
                    ("Cust order", inv["po_number"] or ""), ("Terms", f"{s['terms_days']} days")]
            for i, (k, v) in enumerate(meta):
                text(c, MARGIN + i * 130, y - 28, k, 6.5)
                text(c, MARGIN + i * 130, y - 37, v, 7.5, bold=True)
            text(c, MARGIN, y - 54, "INVOICE ADDRESS", 6.5, bold=True)
            lines_block(c, MARGIN, y - 63, inv["bill_to"], 6.5, leading=8)
            text(c, 300, y - 54, "DELIVERY ADDRESS", 6.5, bold=True)
            lines_block(c, 300, y - 63, inv["deliver_to"], 6.5, leading=8)
            ty = y - 110
        else:
            text(c, MARGIN, y - 28, f"Invoice {inv['invoice_number']} (continued)", 7.5, bold=True)
            ty = y - 46

        chunk = lines[p * ROWS_PER_PAGE:(p + 1) * ROWS_PER_PAGE]
        rows = [[str(p * ROWS_PER_PAGE + i + 1), ln["sku"] or "", ln["description"], str(ln["quantity"]),
                 fmt_money(ln["unit_price"]), _code(ln["vat_rate"]), fmt_money(ln["line_net"])]
                for i, ln in enumerate(chunk)]
        y = table(c, cols, ty, header, rows, size=7, row_h=11)

        if p < pages - 1:
            text(c, W - MARGIN, y - 6, "Continued overleaf...", 7, align="right")
        else:
            hline(c, MARGIN, W - MARGIN, y + 4)
            text(c, MARGIN, y - 8, "VAT ANALYSIS", 6.5, bold=True)
            for i, (rate, net, vat) in enumerate(inv["vat_summary"]):
                text(c, MARGIN, y - 18 - i * 9,
                     f"{_code(rate)} = {int(rate * 100)}%   Net {fmt_money(net)}   VAT {fmt_money(vat)}", 6.5)
            tot = [("Net total", inv["subtotal_net"]), ("VAT total", inv["vat_total"]),
                   ("TOTAL GBP", inv["total_gross"])]
            for i, (k, v) in enumerate(tot):
                bold = i == 2
                text(c, 470, y - 10 - i * 10, k, 7, bold=bold, align="right")
                text(c, W - MARGIN, y - 10 - i * 10, fmt_money(v), 7, bold=bold, align="right")
            text(c, MARGIN, y - 52, "VAT codes: S standard rate, Z zero rated.", 6)

        hline(c, MARGIN, W - MARGIN, 44, grey=0.5)
        text(c, MARGIN, 34, f"Bank sort code {s['sort_code']} a/c {s['account']}  ·  {s['email']}  ·  "
                            f"E&OE", 6)
        c.showPage()
