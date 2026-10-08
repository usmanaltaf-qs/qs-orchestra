"""Template B: totals at the top, lines below, PO number in the footer."""
from reportlab.lib.colors import Color, black

from .base import (H, MARGIN, W, copy_watermark, fmt_date, fmt_money, fmt_rate, hline, lines_block,
                   table, text)


def render(c, inv):
    s = inv["supplier"]
    if inv["is_copy"]:
        copy_watermark(c)
    y = H - MARGIN - 10
    text(c, MARGIN, y, s["name"].upper(), 13, bold=True)
    lines_block(c, MARGIN, y - 16, [", ".join(s["address"]), f"Tel {s['phone']}  ·  {s['email']}"], 8)

    # totals box, top right
    bx, by, bw, bh = W - MARGIN - 200, y - 95, 200, 100
    c.setFillColor(Color(0.92, 0.92, 0.92))
    c.rect(bx, by, bw, bh, stroke=0, fill=1)
    c.setFillColor(black)
    text(c, bx + 10, by + bh - 18, "AMOUNT DUE", 9, bold=True)
    text(c, bx + bw - 10, by + bh - 40, fmt_money(inv["total_gross"], True), 18, bold=True, align="right")
    rows = [("Net", inv["subtotal_net"]), ("VAT", inv["vat_total"])]
    for i, (k, v) in enumerate(rows):
        text(c, bx + 10, by + 32 - i * 13, k, 8)
        text(c, bx + bw - 10, by + 32 - i * 13, fmt_money(v, True), 8, align="right")

    y = H - 170
    text(c, MARGIN, y, "Tax Invoice", 18, bold=True)
    meta = [("Invoice number", inv["invoice_number"]), ("Date", fmt_date(inv["invoice_date"], "short")),
            ("Due", fmt_date(inv["due_date"], "short")), ("VAT no.", s["vat"])]
    for i, (k, v) in enumerate(meta):
        text(c, MARGIN + i * 130, y - 22, k, 7)
        text(c, MARGIN + i * 130, y - 34, v, 9, bold=True)

    text(c, MARGIN, y - 62, "Billed to", 8, bold=True)
    lines_block(c, MARGIN, y - 74, inv["bill_to"], 8, leading=10)
    text(c, 310, y - 62, "Delivered to", 8, bold=True)
    lines_block(c, 310, y - 74, inv["deliver_to"], 8, leading=10)

    cols = [(MARGIN, "left"), (300, "left"), (390, "right"), (450, "right"), (495, "right"),
            (W - MARGIN, "right")]
    rows = [[ln["description"], ln["sku"] or "", str(ln["quantity"]), fmt_money(ln["unit_price"]),
             fmt_rate(ln["vat_rate"]), fmt_money(ln["line_net"])] for ln in inv["lines"]]
    y = table(c, cols, H - 330, ["Description", "Product code", "Qty", "Price", "VAT rate", "Amount"], rows,
              size=9)
    hline(c, MARGIN, W - MARGIN, y + 6, grey=0.6)
    for i, (rate, net, vat) in enumerate(inv["vat_summary"]):
        text(c, W - MARGIN, y - 8 - i * 12, f"VAT {fmt_rate(rate)}: {fmt_money(vat)} on {fmt_money(net)}", 8,
             align="right")

    hline(c, MARGIN, W - MARGIN, 100, grey=0.5)
    if inv["po_number"]:
        text(c, MARGIN, 86, f"Order reference: {inv['po_number']}", 9, bold=True)
    text(c, MARGIN, 72, f"Terms: {s['terms_days']} days net. Bank: sort code {s['sort_code']}, "
                        f"account no. {s['account']}", 8)
    text(c, MARGIN, 60, f"{s['name']} · Registered in England & Wales", 7)
    c.showPage()
