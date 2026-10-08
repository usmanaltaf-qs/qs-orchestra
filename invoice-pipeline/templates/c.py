"""Template C: two-column header, "Qty" after "Unit price", descriptions only (no SKU column)."""
from .base import (H, MARGIN, W, copy_watermark, fmt_date, fmt_money, fmt_rate, hline, lines_block,
                   table, text)


def render(c, inv):
    s = inv["supplier"]
    if inv["is_copy"]:
        copy_watermark(c)
    mid = W / 2 + 10
    y = H - MARGIN - 10
    # left column: supplier
    text(c, MARGIN, y, s["name"], 14, bold=True)
    yy = lines_block(c, MARGIN, y - 18, s["address"], 9)
    yy = lines_block(c, MARGIN, yy - 4, [f"VAT: {s['vat']}", s["phone"]], 9)
    text(c, MARGIN, yy - 10, "Remit to", 8, bold=True)
    lines_block(c, MARGIN, yy - 22, [f"Sort code: {s['sort_code']}", f"Account: {s['account']}"], 8)

    # right column: invoice meta + customer
    text(c, mid, y, "Sales Invoice", 14, bold=True)
    meta = [("Number", inv["invoice_number"]), ("Issued", fmt_date(inv["invoice_date"], "short"))]
    if inv["po_number"]:
        meta.append(("Purchase order", inv["po_number"]))
    meta.append(("Payment due", fmt_date(inv["due_date"], "short")))
    for i, (k, v) in enumerate(meta):
        text(c, mid, y - 20 - i * 13, k, 9)
        text(c, mid + 95, y - 20 - i * 13, v, 9, bold=True)
    yy = y - 20 - len(meta) * 13 - 10
    text(c, mid, yy, "Customer", 8, bold=True)
    yy = lines_block(c, mid, yy - 12, inv["bill_to"], 8, leading=10)
    text(c, mid, yy - 6, "Ship to", 8, bold=True)
    lines_block(c, mid, yy - 18, inv["deliver_to"], 8, leading=10)

    cols = [(MARGIN, "left"), (360, "right"), (410, "right"), (460, "right"), (W - MARGIN, "right")]
    rows = [[ln["description"], fmt_money(ln["unit_price"]), str(ln["quantity"]), fmt_rate(ln["vat_rate"]),
             fmt_money(ln["line_net"])] for ln in inv["lines"]]
    y = table(c, cols, H - 330, ["Item", "Unit price", "Qty", "VAT", "Total"], rows)
    hline(c, MARGIN, W - MARGIN, y + 6)

    totals = [("Goods total", inv["subtotal_net"])]
    totals += [(f"VAT {fmt_rate(rate)}", vat) for rate, _net, vat in inv["vat_summary"]]
    for i, (k, v) in enumerate(totals):
        text(c, 460, y - 10 - i * 13, k, 9, align="right")
        text(c, W - MARGIN, y - 10 - i * 13, fmt_money(v), 9, align="right")
    yy = y - 10 - len(totals) * 13 - 6
    text(c, 460, yy, "Invoice total (GBP)", 10, bold=True, align="right")
    text(c, W - MARGIN, yy, fmt_money(inv["total_gross"]), 10, bold=True, align="right")

    text(c, W / 2, 50, f"Payment terms: {s['terms_days']} days from invoice date. Thank you for your business.",
         8, align="center")
    c.showPage()
