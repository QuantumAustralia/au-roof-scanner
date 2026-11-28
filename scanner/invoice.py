"""Buyer details and the QMS Roof Scan invoice.

Listed prices are exclusive of GST. The invoice adds GST at 10 percent and
shows the ex-GST total, the GST, and the amount payable. Street addresses
are included only on the invoice, which is emailed after payment.
"""

from __future__ import annotations

import base64
import html
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scanner.config import ROOT, env

LOGO_PATH = ROOT / "scanner" / "static" / "qms-roof-scan.jpg"

ATTRIBUTION = (
    "Aerial Imagery Attribution: Aerial roof imagery sourced from Spatial Services, "
    "NSW Department of Customer Service (© State of New South Wales), modified with "
    "automated condition overlays and licensed under Creative Commons Attribution 4.0 (CC BY 4.0)."
)

DISCLAIMER = (
    "There are no warranties or guarantees as to the accuracy of the images or the condition of the roofs. "
    "Images are third-party satellite imagery and we cannot guarantee the age, currency, or accuracy of any image. "
    "Leads are provided for information purposes only and buyers should conduct their own inspections."
)

_STATES = ("NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT")
GST_PERCENT = 10


def gst_breakdown(ex_gst_dollars: int) -> dict[str, int | str]:
    """Add 10 percent GST to a whole-dollar ex-GST amount.

    Cents are rounded half up. Whole-dollar prices land on exact cents.
    """
    ex_cents = int(ex_gst_dollars) * 100
    gst_cents = (ex_cents * GST_PERCENT + 50) // 100
    total_cents = ex_cents + gst_cents
    return {
        "ex_gst_cents": ex_cents,
        "gst_cents": gst_cents,
        "total_cents": total_cents,
        "ex_gst_label": _money(ex_cents),
        "gst_label": _money(gst_cents),
        "total_label": _money(total_cents),
    }


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def buyer_from_mapping(form: object) -> tuple[dict | None, str]:
    """Read the checkout form. The error string is empty when the details are usable."""
    getter = getattr(form, "get", None)
    if getter is None:
        return None, "Enter the buyer details."
    buyer = {
        "name": str(getter("buyer_name") or "").strip(),
        "business": str(getter("business_name") or "").strip(),
        "phone": str(getter("phone") or "").strip(),
        "email": str(getter("email") or "").strip(),
        "line1": str(getter("billing_line") or "").strip(),
        "suburb": str(getter("billing_suburb") or "").strip(),
        "state": str(getter("billing_state") or "").strip().upper(),
        "postcode": str(getter("billing_postcode") or "").strip(),
    }
    if len(buyer["name"]) < 2:
        return None, "Enter the buyer's full name."
    if len(buyer["business"]) < 2:
        return None, "Enter the business name."
    digits = "".join(character for character in buyer["phone"] if character.isdigit())
    if len(digits) < 8:
        return None, "Enter a phone number."
    email = buyer["email"]
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return None, "Enter an email address for the invoice and the lead addresses."
    if len(buyer["line1"]) < 4:
        return None, "Enter the billing street address."
    if len(buyer["suburb"]) < 2:
        return None, "Enter the billing suburb."
    if buyer["state"] not in _STATES:
        return None, "Choose a billing state."
    if not (buyer["postcode"].isdigit() and len(buyer["postcode"]) == 4):
        return None, "Enter a four-digit billing postcode."
    return buyer, ""


def render_invoice(
    order: dict,
    details: list[dict],
    *,
    logo_mode: str = "data",
    for_operator: bool = False,
) -> tuple[str, str]:
    """Plain-text invoice and an HTML copy. Both include the street addresses."""
    buyer = order.get("buyer") if isinstance(order.get("buyer"), dict) else {}
    when = _invoice_date(str(order.get("paid_at") or order.get("created_at") or ""))
    number = str(order.get("invoice_number") or "").strip() or str(order.get("reference") or order.get("order_id") or "")
    lines = [_address_line(item) for item in details]
    amounts = [
        f"{_lead_label(item):<48} {item.get('price_label') or ''}"
        for item in details
    ]
    bill = [
        str(buyer.get("business") or "").strip(),
        str(buyer.get("name") or "").strip(),
        str(buyer.get("line1") or "").strip(),
        " ".join(
            part
            for part in (
                str(buyer.get("suburb") or "").strip(),
                str(buyer.get("state") or "").strip(),
                str(buyer.get("postcode") or "").strip(),
            )
            if part
        ),
        str(buyer.get("email") or order.get("email") or "").strip(),
        str(buyer.get("phone") or "").strip(),
    ]
    bill = [line for line in bill if line]
    abn = env("QMS_ABN")
    seller = ["QMS Roof Scan"]
    if abn:
        seller.append(f"ABN {abn}")
    ex_gst = int(order.get("amount_ex_gst") or 0)
    if ex_gst <= 0:
        ex_gst = sum(int(item.get("price_ex_gst") or 0) for item in details)
    totals = gst_breakdown(ex_gst)
    text = "\n".join(
        [
            *seller,
            "Tax invoice",
            "",
            f"Invoice {number}",
            f"Date {when}",
            f"Order {order.get('reference') or order.get('order_id') or ''}",
            "",
            "Bill to",
            *bill,
            "",
            "Leads (exclusive of GST)",
            *amounts,
            "",
            f"Total ex GST {totals['ex_gst_label']}",
            f"GST ({GST_PERCENT}%) {totals['gst_label']}",
            f"Invoice total {totals['total_label']}",
            *_payment_text(order),
            "",
            "Lead addresses",
            *lines,
            "",
            DISCLAIMER,
            "",
            ATTRIBUTION,
            "",
            str(order.get("data_notice") or ""),
        ]
    )
    if order.get("invoice_url"):
        text += f"\n\nStripe invoice: {order['invoice_url']}\n"
    html_body = _html_invoice(
        seller=seller,
        number=number,
        when=when,
        reference=str(order.get("reference") or order.get("order_id") or ""),
        bill=bill,
        details=details,
        totals=totals,
        invoice_url=str(order.get("invoice_url") or ""),
        lines=lines,
        logo_mode=logo_mode,
        data_notice=str(order.get("data_notice") or ""),
        for_operator=for_operator,
        order_id=str(order.get("order_id") or ""),
        paid=order.get("status") == "paid",
    )
    return text, html_body


def render_invoice_pdf(order: dict, details: list[dict]) -> bytes:
    """A4 tax invoice as a PDF. The layout matches the on-screen invoice."""
    from fpdf import FPDF

    buyer = order.get("buyer") if isinstance(order.get("buyer"), dict) else {}
    number = str(order.get("invoice_number") or "").strip() or str(order.get("reference") or order.get("order_id") or "")
    when = _invoice_date(str(order.get("paid_at") or order.get("created_at") or ""))
    reference = str(order.get("reference") or order.get("order_id") or "")
    ex_gst = int(order.get("amount_ex_gst") or 0)
    if ex_gst <= 0:
        ex_gst = sum(int(item.get("price_ex_gst") or 0) for item in details)
    totals = gst_breakdown(ex_gst)
    bill = [
        str(buyer.get("business") or "").strip(),
        str(buyer.get("name") or "").strip(),
        str(buyer.get("line1") or "").strip(),
        " ".join(
            part
            for part in (
                str(buyer.get("suburb") or "").strip(),
                str(buyer.get("state") or "").strip(),
                str(buyer.get("postcode") or "").strip(),
            )
            if part
        ),
        str(buyer.get("email") or order.get("email") or "").strip(),
        str(buyer.get("phone") or "").strip(),
    ]
    bill = [line for line in bill if line]
    seller = ["QMS Roof Scan"]
    abn = env("QMS_ABN")
    if abn:
        seller.append(f"ABN {abn}")

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    pdf.set_fill_color(1, 15, 52)
    pdf.rect(0, 0, 210, 34, "F")
    if LOGO_PATH.is_file():
        pdf.image(str(LOGO_PATH), x=12, y=7, h=20)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_xy(38, 9)
    pdf.cell(100, 8, "QMS ROOF SCAN")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_xy(38, 18)
    pdf.cell(100, 6, "Market Intelligence for Contractors")
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_xy(120, 8)
    pdf.cell(76, 7, "TAX INVOICE", align="R")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_xy(120, 16)
    pdf.cell(76, 5, _pdf_text(number), align="R")
    pdf.set_xy(120, 21)
    pdf.cell(76, 5, _pdf_text(when), align="R")

    pdf.set_text_color(28, 36, 48)
    pdf.set_y(42)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(95, 6, "From")
    pdf.cell(95, 6, "Bill to")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 10)
    left = seller or ["QMS Roof Scan"]
    top = pdf.get_y()
    for line in left:
        pdf.set_x(10)
        pdf.cell(95, 5, _pdf_text(line))
        pdf.ln(5)
    after_left = pdf.get_y()
    pdf.set_y(top)
    for line in bill:
        pdf.set_x(105)
        pdf.cell(95, 5, _pdf_text(line))
        pdf.ln(5)
    pdf.set_y(max(after_left, pdf.get_y()) + 4)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(92, 107, 122)
    pdf.cell(0, 5, _pdf_text(f"Order {reference}"))
    pdf.ln(8)

    pdf.set_text_color(28, 36, 48)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(243, 245, 248)
    pdf.cell(140, 8, "Description", border=0, fill=True)
    pdf.cell(50, 8, "Amount ex GST", border=0, align="R", fill=True)
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 10)
    for item in details:
        label = _pdf_text(_lead_label(item))
        amount = _pdf_text(_line_amount(item))
        pdf.cell(140, 7, label)
        pdf.cell(50, 7, amount, align="R")
        pdf.ln(7)
    pdf.ln(2)
    pdf.set_x(110)
    pdf.cell(40, 6, "Total ex GST")
    pdf.cell(50, 6, _pdf_text(str(totals["ex_gst_label"])), align="R")
    pdf.ln(6)
    pdf.set_x(110)
    pdf.cell(40, 6, f"GST ({GST_PERCENT}%)")
    pdf.cell(50, 6, _pdf_text(str(totals["gst_label"])), align="R")
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_fill_color(1, 15, 52)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x(110)
    pdf.cell(40, 8, "Invoice total", fill=True)
    pdf.cell(50, 8, _pdf_text(str(totals["total_label"])), align="R", fill=True)
    pdf.ln(10)
    if order.get("status") == "paid":
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(92, 107, 122)
        pdf.set_x(110)
        pdf.cell(90, 4, "Payment received", align="R")
        pdf.ln(4)
        pdf.set_x(110)
        pdf.cell(90, 4, "Stripe", align="R")
        pdf.ln(8)
    else:
        pdf.ln(4)

    pdf.set_text_color(28, 36, 48)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Lead addresses")
    pdf.ln(7)
    pdf.set_font("Helvetica", "", 10)
    for line in [_address_line(item) for item in details]:
        pdf.multi_cell(0, 5, _pdf_text(line))
        pdf.ln(1)
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(92, 107, 122)
    for paragraph in (DISCLAIMER, ATTRIBUTION, str(order.get("data_notice") or "")):
        if paragraph.strip():
            pdf.multi_cell(0, 4, _pdf_text(paragraph))
            pdf.ln(2)
    return bytes(pdf.output())


def _html_invoice(
    *,
    seller: list[str],
    number: str,
    when: str,
    reference: str,
    bill: list[str],
    details: list[dict],
    totals: dict,
    invoice_url: str,
    lines: list[str],
    logo_mode: str = "data",
    data_notice: str = "",
    for_operator: bool = False,
    order_id: str = "",
    paid: bool = False,
) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(_lead_label(item))}</td>"
        f"<td class=\"money\">{html.escape(_line_amount(item))}</td>"
        "</tr>"
        for item in details
    )
    addresses = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
    bill_html = "<br>".join(html.escape(line) for line in bill)
    seller_html = "<br>".join(html.escape(line) for line in seller)
    link = ""
    if invoice_url:
        safe = html.escape(invoice_url, quote=True)
        link = f'<p class="fine"><a href="{safe}">View the Stripe invoice</a></p>'
    logo = _logo_tag(logo_mode)
    received = '<p class="received">Payment received<br>Stripe</p>' if paid else ""
    toolbar = ""
    if for_operator and order_id:
        pdf_href = html.escape(f"/admin/invoice/{order_id}/pdf", quote=True)
        toolbar = (
            '<div class="toolbar">'
            '<a href="/admin">Back to sales and invoices</a>'
            f'<a class="download" href="{pdf_href}">Download PDF</a>'
            "</div>"
        )
    return f"""<!DOCTYPE html>
<html lang="en-AU">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Invoice {html.escape(number)}</title>
  <style>
    body {{ margin: 0; background: #e7edf3; color: #1c2430; font-family: "Segoe UI", sans-serif; }}
    .toolbar {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; width: min(820px, calc(100% - 24px)); margin: 16px auto 0; }}
    .toolbar a {{ color: #1f4e79; }}
    .download {{ background: #010F34; color: #fff !important; text-decoration: none; border-radius: 6px; padding: 8px 14px; font-weight: 700; }}
    .sheet {{ width: min(820px, calc(100% - 24px)); margin: 16px auto 32px; background: #fff; box-shadow: 0 8px 28px rgba(1, 15, 52, 0.12); }}
    .banner {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; background: #010F34; color: #fff; padding: 18px 22px; }}
    .brand {{ display: flex; gap: 14px; align-items: center; }}
    .brand p {{ margin: 0; }}
    .banner h1 {{ margin: 0; font-size: 26px; letter-spacing: 0.04em; }}
    .banner p {{ margin: 4px 0 0; color: #c5d0dc; }}
    .banner img {{ height: 64px; width: auto; border-radius: 8px; }}
    .doc {{ text-align: right; white-space: nowrap; flex: 0 0 auto; }}
    .doc strong {{ display: block; font-size: 16px; letter-spacing: 0.08em; }}
    .body {{ padding: 22px; }}
    .parties {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }}
    h2 {{ margin: 0 0 6px; font-size: 13px; letter-spacing: 0.04em; text-transform: uppercase; color: #5c6b7a; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ text-align: left; padding: 8px 0; border-bottom: 2px solid #010F34; font-size: 13px; }}
    td {{ padding: 8px 0; border-bottom: 1px solid #e3e8ee; vertical-align: top; }}
    .money, th.money {{ text-align: right; white-space: nowrap; }}
    .totals {{ width: 280px; margin-left: auto; margin-top: 8px; }}
    .totals div {{ display: flex; justify-content: space-between; padding: 4px 0; }}
    .grand {{ background: #010F34; color: #fff; margin-top: 6px; padding: 8px 10px !important; font-weight: 700; font-size: 18px; }}
    .received {{ margin: 8px 0 0; text-align: right; color: #5c6b7a; font-size: 12px; line-height: 1.35; }}
    .addresses {{ margin-top: 22px; }}
    .fine {{ color: #5c6b7a; font-size: 12px; line-height: 1.45; }}
    @media print {{
      body {{ background: #fff; }}
      .toolbar {{ display: none; }}
      .sheet {{ width: auto; margin: 0; box-shadow: none; }}
    }}
  </style>
</head>
<body>
  {toolbar}
  <article class="sheet">
    <header class="banner">
      <div class="brand">
        {logo}
        <div>
          <h1>QMS ROOF SCAN</h1>
          <p>Market Intelligence for Contractors</p>
        </div>
      </div>
      <div class="doc">
        <strong>TAX INVOICE</strong>
        <div>{html.escape(number)}</div>
        <div>{html.escape(when)}</div>
      </div>
    </header>
    <div class="body">
      <div class="parties">
        <section>
          <h2>From</h2>
          <p>{seller_html}</p>
        </section>
        <section>
          <h2>Bill to</h2>
          <p>{bill_html}</p>
        </section>
      </div>
      <p class="fine">Order {html.escape(reference)}</p>
      <table>
        <thead>
          <tr><th>Description</th><th class="money">Amount ex GST</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <div class="totals">
        <div><span>Total ex GST</span><span>{html.escape(str(totals['ex_gst_label']))}</span></div>
        <div><span>GST ({GST_PERCENT}%)</span><span>{html.escape(str(totals['gst_label']))}</span></div>
        <div class="grand"><span>Invoice total</span><span>{html.escape(str(totals['total_label']))}</span></div>
        {received}
      </div>
      <section class="addresses">
        <h2>Lead addresses</h2>
        <ul>{addresses}</ul>
      </section>
      {link}
      <p class="fine">{html.escape(DISCLAIMER)}</p>
      <p class="fine">{html.escape(ATTRIBUTION)}</p>
      <p class="fine">{html.escape(data_notice)}</p>
    </div>
  </article>
</body>
</html>
"""


def logo_bytes() -> bytes:
    """Logo file for the invoice email. Empty when the file has not been copied in."""
    if not LOGO_PATH.is_file():
        return b""
    return LOGO_PATH.read_bytes()


def _logo_tag(mode: str = "data") -> str:
    data = logo_bytes()
    if not data:
        return ""
    if mode == "cid":
        return (
            '<p><img src="cid:qms-logo" alt="QMS Roof Scan" '
            'height="72" style="height:72px;width:72px;border-radius:8px"></p>'
        )
    encoded = base64.b64encode(data).decode("ascii")
    return (
        f'<p><img src="data:image/jpeg;base64,{encoded}" alt="QMS Roof Scan" '
        'height="72" style="height:72px;width:72px;border-radius:8px"></p>'
    )


def _payment_text(order: dict) -> list[str]:
    """Paid invoices note the method under the total. Unpaid invoices omit it."""
    if order.get("status") != "paid":
        return []
    return ["Payment received", "Stripe"]


def _line_amount(item: dict) -> str:
    """Ex-GST amount for one lead, shown in dollars and cents."""
    try:
        return _money(int(item.get("price_ex_gst") or 0) * 100)
    except (TypeError, ValueError):
        return str(item.get("price_label") or "")


def _pdf_text(value: str) -> str:
    """Helvetica only covers Latin-1, so replace the few symbols the invoice uses."""
    cleaned = (
        value.replace("\u00a9", "(c)")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    return cleaned.encode("latin-1", "replace").decode("latin-1")


def _lead_label(item: dict) -> str:
    category = str(item.get("category") or "repair").replace("_", " ")
    suburb = str(item.get("suburb") or "Unknown suburb")
    return f"{category.title()} lead in {suburb} (ex GST)"


def _address_line(item: dict) -> str:
    address = str(item.get("address") or "").strip()
    suburb = str(item.get("suburb") or "").strip()
    state = str(item.get("state") or "").strip()
    postcode = str(item.get("postcode") or "").strip()
    if suburb and suburb.casefold() in address.casefold():
        return address
    tail = " ".join(part for part in (suburb, state, postcode) if part)
    return ", ".join(part for part in (address, tail) if part)


def _invoice_date(created_at: str) -> str:
    from datetime import timezone

    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _in_sydney(parsed).strftime("%d %B %Y")


def _in_sydney(moment: datetime):
    """Sydney local time. Uses a fixed eastern offset only when the zone database is missing."""
    from datetime import timedelta, timezone

    try:
        return moment.astimezone(ZoneInfo("Australia/Sydney"))
    except (ZoneInfoNotFoundError, ModuleNotFoundError):
        return moment.astimezone(timezone(timedelta(hours=11)))
