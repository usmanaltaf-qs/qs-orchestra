"""Template A: classic. Header block, line table, totals bottom right."""
from .base import (H, MARGIN, W, copy_watermark, fmt_date, fmt_money, fmt_rate, hline, lines_block,
                   table, text, vat_summary_rows)


def render(c, inv):
    s = inv["supplier"]
    if inv["is_copy"]:
        copy_watermark(c)
    y = H - MARGIN - 10
    text(c, MARGIN, y, s["name"], 16, bold=True)
    lines_block(c, MARGIN, y - 18, s["address"] + [f"VAT Reg No: {s['vat']}", s["email"]], 9)

    text(c, W - MARGIN, y, "INVOICE", 20, bold=True, align="right")
    meta = [("Invoice No:", inv["invoice_number"]),
            ("Invoice Date:", fmt_date(inv["invoice_date"], "num"))]
    if inv["po_number"]:
        meta.append(("Your PO:", inv["po_number"]))
    meta += [("Terms:", f"{s['terms_days']} days"), ("Due Date:", fmt_date(inv["due_date"], "num"))]
    for i, (k, v) in enumerate(meta):
        yy = y - 24 - i * 13
        text(c, W - MARGIN - 110, yy, k, 9, bold=True, align="right")
        text(c, W - MARGIN, yy, v, 9, align="right")

    y = H - 190
    text(c, MARGIN, y, "Invoice to", 9, bold=True)
    lines_block(c, MARGIN, y - 13, inv["bill_to"], 9)
    text(c, 310, y, "Deliver to", 9, bold=True)
    lines_block(c, 310, y - 13, inv["deliver_to"], 9)

    cols = [(MARGIN, "left"), (115, "left"), (380, "right"), (450, "right"), (495, "right"),
            (W - MARGIN, "right")]
    rows = [[ln["sku"] or "", ln["description"], str(ln["quantity"]), fmt_money(ln["unit_price"]),
             fmt_rate(ln["vat_rate"]), fmt_money(ln["line_net"])] for ln in inv["lines"]]
    y = table(c, cols, H - 290, ["SKU", "Description", "Qty", "Unit price", "VAT", "Net"], rows)

    hline(c, 330, W - MARGIN, y + 4)
    totals = [("Subtotal", inv["subtotal_net"])] + vat_summary_rows(inv)
    for i, (k, v) in enumerate(totals):
        text(c, 450, y - 10 - i * 14, k, 9, align="right")
        text(c, W - MARGIN, y - 10 - i * 14, fmt_money(v, True), 9, align="right")
    yy = y - 10 - len(totals) * 14 - 4
    text(c, 450, yy, "Total due", 11, bold=True, align="right")
    text(c, W - MARGIN, yy, fmt_money(inv["total_gross"], True), 11, bold=True, align="right")

    hline(c, MARGIN, W - MARGIN, 90, grey=0.5)
    text(c, MARGIN, 76, "Payment by BACS to:", 8, bold=True)
    text(c, MARGIN, 64, f"{s['name']}   Sort code {s['sort_code']}   Account {s['account']}", 8)
    text(c, MARGIN, 52, "Please quote the invoice number as your payment reference.", 8)
    c.showPage()
