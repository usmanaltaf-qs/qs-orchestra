"""Drawing helpers shared by the invoice templates.

A template is a function render(c, inv) that draws the invoice `inv` (see generate_invoices.py,
build_invoice) onto a reportlab canvas, calling c.showPage() after each page. Templates print the
values they are given: injected errors are already in `inv`.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from reportlab.lib.colors import Color, black
from reportlab.lib.pagesizes import A4

W, H = A4
MARGIN = 40


def fmt_date(d: date, style: str) -> str:
    if style == "num":
        return d.strftime("%d/%m/%Y")
    return f"{d.day} {d.strftime('%b %Y')}"


def fmt_money(x: Decimal, symbol: bool = False) -> str:
    s = f"{x:,.2f}"
    return f"£{s}" if symbol else s


def fmt_rate(r: Decimal) -> str:
    return f"{int(r * 100)}%"


def text(c, x, y, s, size=9, bold=False, align="left", font=None):
    c.setFont(font or ("Helvetica-Bold" if bold else "Helvetica"), size)
    if align == "right":
        c.drawRightString(x, y, s)
    elif align == "center":
        c.drawCentredString(x, y, s)
    else:
        c.drawString(x, y, s)


def lines_block(c, x, y, rows, size=9, leading=None, bold_first=False):
    leading = leading or size + 3
    for i, row in enumerate(rows):
        text(c, x, y - i * leading, row, size, bold=bold_first and i == 0)
    return y - len(rows) * leading


def hline(c, x1, x2, y, width=0.5, grey=0.0):
    c.setStrokeColor(Color(grey, grey, grey))
    c.setLineWidth(width)
    c.line(x1, y, x2, y)
    c.setStrokeColor(black)


def copy_watermark(c):
    c.saveState()
    c.setFillColor(Color(0.6, 0.6, 0.6, alpha=0.35))
    c.setFont("Helvetica-Bold", 110)
    c.translate(W / 2, H / 2)
    c.rotate(35)
    c.drawCentredString(0, -30, "COPY")
    c.restoreState()


def vat_summary_rows(inv):
    """[(label, amount)] for the VAT lines in a totals block."""
    return [(f"VAT @ {fmt_rate(rate)} on {fmt_money(net)}", vat) for rate, net, vat in inv["vat_summary"]]


def table(c, x_cols, y, header, rows, size=9, row_h=None, header_bold=True, rule=True):
    """x_cols: [(x, align)] per column. Returns y after the last row."""
    row_h = row_h or size + 7
    for (x, align), h in zip(x_cols, header):
        text(c, x, y, h, size, bold=header_bold, align=align)
    if rule:
        hline(c, x_cols[0][0] - 2, W - MARGIN, y - 4)
    y -= row_h
    for row in rows:
        for (x, align), v in zip(x_cols, row):
            text(c, x, y, v, size, align=align)
        y -= row_h
    return y
