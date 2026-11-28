"""Marketplace listings, prices, and orders.

Prices are whole Australian dollars exclusive of GST. A listing is live until
its expiry, then it moves to the archive. Only live listings can be sold.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scanner.config import DATA_DIR, ROOT

CONFIG_PATH = ROOT / "marketplace_config.json"
LISTINGS_PATH = DATA_DIR / "marketplace_listings.json"
ORDERS_PATH = DATA_DIR / "marketplace_orders.json"

CATEGORIES = ("repair", "cleaning")
SCORES = (2, 3, 4, 5)

DEFAULT_CONFIG = {
    "listing_days": 30,
    "minimum_order": 10,
    "repair_prices_ex_gst": {"2": 90, "3": 160, "4": 280, "5": 420},
    "cleaning_prices_ex_gst": {"2": 60, "3": 95, "4": 140, "5": 190},
}


def format_price(amount: int) -> str:
    """Dollar amount with the exclusive-of-GST note used on every price."""
    return f"${int(amount):,} ex GST"


def load_config() -> dict:
    """Defaults, overlaid with marketplace_config.json when that file exists."""
    config = {
        "listing_days": DEFAULT_CONFIG["listing_days"],
        "minimum_order": DEFAULT_CONFIG["minimum_order"],
        "repair_prices_ex_gst": dict(DEFAULT_CONFIG["repair_prices_ex_gst"]),
        "cleaning_prices_ex_gst": dict(DEFAULT_CONFIG["cleaning_prices_ex_gst"]),
    }
    if not CONFIG_PATH.is_file():
        return config
    try:
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return config
    if not isinstance(stored, dict):
        return config
    config["listing_days"] = _positive(stored.get("listing_days"), config["listing_days"])
    config["minimum_order"] = _positive(stored.get("minimum_order"), config["minimum_order"])
    for key in ("repair_prices_ex_gst", "cleaning_prices_ex_gst"):
        incoming = stored.get(key)
        if isinstance(incoming, dict):
            for score in SCORES:
                config[key][str(score)] = _non_negative(
                    incoming.get(str(score), incoming.get(score)),
                    config[key][str(score)],
                )
    return config


def save_config(updates: dict) -> dict:
    """Save the price table, listing length, and minimum order."""
    current = load_config()
    current["listing_days"] = _positive(updates.get("listing_days"), current["listing_days"])
    current["minimum_order"] = _positive(updates.get("minimum_order"), current["minimum_order"])
    for key in ("repair_prices_ex_gst", "cleaning_prices_ex_gst"):
        incoming = updates.get(key) or {}
        for score in SCORES:
            current[key][str(score)] = _non_negative(
                incoming.get(str(score)),
                current[key][str(score)],
            )
    CONFIG_PATH.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return current


def default_price(category: str, score: int | None) -> int:
    """Price ex GST for a wear score or a cleaning score. Scores sit on 2 to 5."""
    config = load_config()
    table = config["cleaning_prices_ex_gst"] if category == "cleaning" else config["repair_prices_ex_gst"]
    band = score if score in SCORES else 2
    if score is not None and score > 5:
        band = 5
    return int(table[str(band)])


def publish(leads: list[dict], lead_ids: list[str], category: str, price_override: int | None) -> int:
    """Put selected leads on the Marketplace and restart the listing timer."""
    if category not in CATEGORIES:
        raise ValueError("Choose repair/wear or cleaning.")
    listings = _load_listings()
    by_id = {str(lead["lead_id"]): lead for lead in leads}
    config = load_config()
    published = 0
    for lead_id in lead_ids:
        lead = by_id.get(lead_id)
        if lead is None:
            continue
        score = _score_for(lead, category)
        price = price_override if price_override is not None else default_price(category, score)
        listings[lead_id] = _live_record(category, price, config["listing_days"])
        published += 1
    _save_listings(listings)
    return published


def drop(lead_ids: list[str]) -> int:
    """Remove listings for leads that have been binned. Past orders are left as they are."""
    listings = _load_listings()
    removed = 0
    for lead_id in lead_ids:
        if lead_id in listings:
            del listings[lead_id]
            removed += 1
    if removed:
        _save_listings(listings)
    return removed


def unpublish(lead_ids: list[str]) -> int:
    """Take selected live leads off the marketplace. They can be re-listed later."""
    listings = _load_listings()
    count = 0
    for lead_id in lead_ids:
        current = listings.get(lead_id)
        if not current or not current.get("published"):
            continue
        current["published"] = False
        current["archived"] = False
        current["unpublished"] = True
        listings[lead_id] = current
        count += 1
    _save_listings(listings)
    return count


def relist(lead_ids: list[str], price_override: int | None = None) -> int:
    """Return archived leads to the Marketplace and restart the  timer."""
    listings = _load_listings()
    days = load_config()["listing_days"]
    count = 0
    for lead_id in lead_ids:
        current = listings.get(lead_id)
        if not current:
            continue
        price = price_override if price_override is not None else int(current["price_ex_gst"])
        listings[lead_id] = _live_record(str(current["category"]), price, days)
        listings[lead_id]["unpublished"] = False
        count += 1
    _save_listings(listings)
    return count


def set_price(lead_id: str, price_ex_gst: int) -> None:
    """Manual price override for one listing. The amount is exclusive of GST."""
    if price_ex_gst < 0:
        raise ValueError("Price cannot be negative.")
    listings = _load_listings()
    current = listings.get(lead_id)
    if not current:
        raise ValueError("That lead is not listed.")
    current["price_ex_gst"] = int(price_ex_gst)
    listings[lead_id] = current
    _save_listings(listings)


def live_rows(leads: list[dict]) -> list[dict]:
    """Leads that are still inside their listing window."""
    return _rows(leads, bucket="live")


def archive_rows(leads: list[dict]) -> list[dict]:
    """Leads whose listing has expired."""
    return _rows(leads, bucket="expired")


def unpublished_rows(leads: list[dict]) -> list[dict]:
    """Leads taken off the marketplace before they expired."""
    return _rows(leads, bucket="unpublished")


def public_cards(leads: list[dict]) -> list[dict]:
    """Live listings safe to show before payment. Street addresses are omitted."""
    from scanner.compliance import is_suppressed, load_suppressed

    blocked = load_suppressed()
    by_id = {str(lead.get("lead_id")): lead for lead in leads}
    cards = []
    for row in live_rows(leads):
        if is_suppressed(by_id.get(row["lead_id"], {}), blocked):
            continue
        cards.append(_public_card(row))
    return cards


def cards_for_checkout(leads: list[dict], lead_ids: list[str]) -> list[dict]:
    """Live cards for the ids in the cart, in the order the buyer added them."""
    from scanner.compliance import is_suppressed, load_suppressed

    blocked = load_suppressed()
    by_id = {str(lead.get("lead_id")): lead for lead in leads}
    live = {row["lead_id"]: row for row in live_rows(leads)}
    chosen = []
    seen: set[str] = set()
    for lead_id in lead_ids:
        if lead_id in seen or lead_id not in live:
            continue
        if is_suppressed(by_id.get(lead_id, {}), blocked):
            continue
        seen.add(lead_id)
        chosen.append(_public_card(live[lead_id]))
    return chosen


def order_shortfall(count: int) -> int:
    """How many more leads are required. Zero when the minimum is met."""
    return max(0, load_config()["minimum_order"] - count)


def create_order(buyer: dict, cards: list[dict], consent: dict | None = None) -> dict:
    """Store a pending purchase. Payment is confirmed later."""
    orders = _load_orders()
    order_id = uuid.uuid4().hex[:16]
    total = sum(int(card["price_ex_gst"]) for card in cards)
    moment = _now()
    reference = "QMS-" + _sydney(moment).strftime("%Y%m%d") + "-" + order_id[:8].upper()
    order = {
        "order_id": order_id,
        "reference": reference,
        "email": str(buyer.get("email") or "").strip(),
        "buyer": {
            "name": str(buyer.get("name") or "").strip(),
            "business": str(buyer.get("business") or "").strip(),
            "phone": str(buyer.get("phone") or "").strip(),
            "email": str(buyer.get("email") or "").strip(),
            "line1": str(buyer.get("line1") or "").strip(),
            "suburb": str(buyer.get("suburb") or "").strip(),
            "state": str(buyer.get("state") or "").strip().upper(),
            "postcode": str(buyer.get("postcode") or "").strip(),
        },
        "lead_ids": [card["lead_id"] for card in cards],
        "amount_ex_gst": total,
        "status": "pending",
        "emailed": False,
        "stripe_session_id": "",
        "payment_method": "",
        "invoice_number": reference,
        "invoice_url": "",
        "terms_accepted_at": str((consent or {}).get("terms_accepted_at") or ""),
        "terms_version": str((consent or {}).get("terms_version") or ""),
        "buyer_ip": str((consent or {}).get("buyer_ip") or ""),
        "buyer_user_agent": str((consent or {}).get("buyer_user_agent") or ""),
        "data_notice": str((consent or {}).get("data_notice") or ""),
        "created_at": moment.isoformat(),
    }
    orders[order_id] = order
    _save_orders(orders)
    return order


def apply_consent(order_id: str, fields: dict) -> dict | None:
    """Store the consent fields Stripe returns when checkout completes."""
    orders = _load_orders()
    order = orders.get(order_id)
    if not order:
        return None
    for key in ("terms_accepted_at", "terms_version", "buyer_ip", "buyer_user_agent"):
        value = str(fields.get(key) or "").strip()
        if value:
            order[key] = value[:500]
    orders[order_id] = order
    _save_orders(orders)
    return order


def attach_session(order_id: str, session_id: str) -> None:
    orders = _load_orders()
    order = orders.get(order_id)
    if not order:
        return
    order["stripe_session_id"] = session_id
    orders[order_id] = order
    _save_orders(orders)


def get_order(order_id: str) -> dict | None:
    return _load_orders().get(order_id)


def order_by_session(session_id: str) -> dict | None:
    for order in _load_orders().values():
        if order.get("stripe_session_id") == session_id:
            return order
    return None


def mark_paid(order_id: str) -> dict | None:
    orders = _load_orders()
    order = orders.get(order_id)
    if not order:
        return None
    order["status"] = "paid"
    if not order.get("paid_at"):
        order["paid_at"] = _now().isoformat()
    if not order.get("invoice_number"):
        order["invoice_number"] = str(order.get("reference") or order_id)
    orders[order_id] = order
    _save_orders(orders)
    _retire_sold(order.get("lead_ids") or [])
    from scanner.compliance import push_order

    push_order(order)
    return order


def attach_payment_method(order_id: str, method: str) -> dict | None:
    """Remember the card or other method Stripe used. No payment reference is stored."""
    method = " ".join(str(method or "").split())
    if not method:
        return get_order(order_id)
    orders = _load_orders()
    order = orders.get(order_id)
    if not order:
        return None
    order["payment_method"] = method[:80]
    orders[order_id] = order
    _save_orders(orders)
    return order


def attach_invoice(order_id: str, number: str, url: str) -> dict | None:
    """Store the Stripe invoice once payment has produced one."""
    orders = _load_orders()
    order = orders.get(order_id)
    if not order:
        return None
    if number:
        order["stripe_invoice_number"] = number
        if not order.get("invoice_number"):
            order["invoice_number"] = number
    if url:
        order["invoice_url"] = url
    orders[order_id] = order
    _save_orders(orders)
    return order


def _retire_sold(lead_ids: list[str]) -> None:
    """A paid lead leaves the public marketplace and cannot be bought again."""
    listings = _load_listings()
    changed = False
    for lead_id in lead_ids:
        current = listings.get(str(lead_id))
        if not current or current.get("sold"):
            continue
        current["published"] = False
        current["sold"] = True
        current["sold_at"] = _now().isoformat()
        listings[str(lead_id)] = current
        changed = True
    if changed:
        _save_listings(listings)


def record_email(order_id: str, recipient: str, sent: bool) -> None:
    """Remember each attempt to send an invoice."""
    orders = _load_orders()
    order = orders.get(order_id)
    if not order:
        return
    if sent:
        order["emailed"] = True
    order["last_emailed_to"] = recipient
    order["last_emailed_at"] = _now().isoformat()
    history = order.get("email_log")
    if not isinstance(history, list):
        history = []
    history.append({"at": order["last_emailed_at"], "to": recipient, "sent": sent})
    order["email_log"] = history[-20:]
    orders[order_id] = order
    _save_orders(orders)


def mark_emailed(order_id: str) -> None:
    orders = _load_orders()
    order = orders.get(order_id)
    if not order:
        return
    order["emailed"] = True
    orders[order_id] = order
    _save_orders(orders)


def invoice_number_of(order: dict) -> str:
    """The QMS invoice number. Older orders used the same value as the reference."""
    return str(order.get("invoice_number") or order.get("reference") or order.get("order_id") or "")


def sales_report(
    leads: list[dict],
    *,
    start: str = "",
    end: str = "",
    category: str = "",
    purchaser: str = "",
) -> dict:
    """Paid invoices and lead totals for the operator sales page.

    Street addresses are not included. Amounts are exclusive of GST.
    """
    _backfill_paid_orders()
    by_id = {str(lead.get("lead_id")): lead for lead in leads}
    listings = _load_listings()
    needle = " ".join(purchaser.casefold().split())
    chosen_category = category if category in CATEGORIES else ""
    invoices = []
    lead_count = 0
    revenue = 0
    repair_count = 0
    cleaning_count = 0
    unsent = 0
    for order in _paid_orders():
        issued = _issued_date(order)
        if start and issued < start:
            continue
        if end and issued > end:
            continue
        buyer = order.get("buyer") if isinstance(order.get("buyer"), dict) else {}
        who = " ".join(
            str(buyer.get(field) or "")
            for field in ("business", "name", "email")
        )
        if needle and needle not in " ".join(who.casefold().split()):
            continue
        lines = _order_lines(order, by_id, listings)
        matched = [line for line in lines if not chosen_category or line["category"] == chosen_category]
        if chosen_category and not matched:
            continue
        matched_total = sum(line["price_ex_gst"] for line in matched)
        lead_count += len(matched)
        revenue += matched_total
        repair_count += sum(1 for line in matched if line["category"] == "repair")
        cleaning_count += sum(1 for line in matched if line["category"] == "cleaning")
        if not order.get("emailed"):
            unsent += 1
        invoices.append(
            {
                "order_id": order.get("order_id") or "",
                "invoice_number": invoice_number_of(order),
                "issued_label": _issued_label(order),
                "purchaser": str(buyer.get("business") or buyer.get("name") or "Purchaser"),
                "contact": str(buyer.get("name") or ""),
                "email": str(order.get("email") or buyer.get("email") or ""),
                "amount_ex_gst": int(order.get("amount_ex_gst") or 0),
                "amount_label": format_price(int(order.get("amount_ex_gst") or 0)),
                "matched_label": format_price(matched_total),
                "lead_count": len(matched),
                "repair_count": sum(1 for line in matched if line["category"] == "repair"),
                "cleaning_count": sum(1 for line in matched if line["category"] == "cleaning"),
                "emailed": bool(order.get("emailed")),
                "lines": matched,
            }
        )
    order_count = len(invoices)
    return {
        "invoices": invoices,
        "order_count": order_count,
        "lead_count": lead_count,
        "revenue_ex_gst": revenue,
        "revenue_label": format_price(revenue),
        "average_label": format_price(revenue // lead_count) if lead_count else format_price(0),
        "repair_count": repair_count,
        "cleaning_count": cleaning_count,
        "unsent": unsent,
        "pending_count": sum(1 for order in _load_orders().values() if order.get("status") != "paid"),
    }


def _paid_orders() -> list[dict]:
    rows = [order for order in _load_orders().values() if order.get("status") == "paid"]
    rows.sort(key=lambda order: str(order.get("paid_at") or order.get("created_at") or ""), reverse=True)
    return rows


def _backfill_paid_orders() -> None:
    """Give older paid orders an invoice number and an issue time."""
    orders = _load_orders()
    changed = False
    for order in orders.values():
        if order.get("status") != "paid":
            continue
        if not order.get("invoice_number"):
            order["invoice_number"] = str(order.get("reference") or order.get("order_id") or "")
            changed = True
        if not order.get("paid_at"):
            order["paid_at"] = str(order.get("created_at") or _now().isoformat())
            changed = True
    if changed:
        _save_orders(orders)


def _order_lines(order: dict, by_id: dict, listings: dict) -> list[dict]:
    lines = []
    for lead_id in order.get("lead_ids") or []:
        lead = by_id.get(str(lead_id), {})
        listing = listings.get(str(lead_id), {})
        category = str(listing.get("category") or "")
        if category not in CATEGORIES:
            category = "repair"
        price = int(listing.get("price_ex_gst") or 0)
        lines.append(
            {
                "category": category,
                "category_label": "Cleaning" if category == "cleaning" else "Repair",
                "suburb": str(lead.get("suburb") or ""),
                "postcode": str(lead.get("postcode") or ""),
                "price_ex_gst": price,
                "price_label": format_price(price),
            }
        )
    return lines


def _issued_date(order: dict) -> str:
    raw = str(order.get("paid_at") or order.get("created_at") or "")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    return _sydney(moment).strftime("%Y-%m-%d")


def _issued_label(order: dict) -> str:
    raw = str(order.get("paid_at") or order.get("created_at") or "")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    return _sydney(moment).strftime("%d %b %Y")


def purchased_details(leads: list[dict], order: dict) -> list[dict]:
    """Full address details. Only for a paid order."""
    by_id = {str(lead["lead_id"]): lead for lead in leads}
    details = []
    for lead_id in order.get("lead_ids") or []:
        lead = by_id.get(str(lead_id))
        if lead is None:
            continue
        listing = _load_listings().get(str(lead_id), {})
        details.append(
            {
                "lead_id": lead_id,
                "reference": lead.get("reference") or "",
                "address": lead.get("address") or "",
                "suburb": lead.get("suburb") or "",
                "state": lead.get("state") or "",
                "postcode": lead.get("postcode") or "",
                "category": listing.get("category") or "",
                "wear_score": lead.get("wear_score"),
                "price_ex_gst": int(listing.get("price_ex_gst") or 0),
                "price_label": format_price(int(listing.get("price_ex_gst") or 0)),
            }
        )
    return details


def _rows(leads: list[dict], *, bucket: str) -> list[dict]:
    listings = expire_due()
    by_id = {str(lead["lead_id"]): lead for lead in leads}
    rows = []
    for lead_id, listing in listings.items():
        if _listing_state(listing, bool(listing.get("published")) and not listing.get("archived")) != bucket:
            continue
        if listing.get("sold"):
            continue
        lead = by_id.get(lead_id, {})
        suburb = lead.get("suburb") or "Unknown suburb"
        postcode = str(lead.get("postcode") or "")
        lat = _number(lead.get("lat"))
        lon = _number(lead.get("lon"))
        if lat is None or lon is None:
            lat, lon = _suburb_centre(str(suburb), postcode)
        rows.append(
            {
                "lead_id": lead_id,
                "reference": str(lead.get("reference") or lead_id),
                "suburb": suburb,
                "postcode": postcode,
                "lat": lat,
                "lon": lon,
                "material": _label(str(lead.get("material") or "")),
                "roof_area_label": _area_label(lead.get("roof_area_m2")),
                "primary_defect": _label(str(lead.get("primary_defect") or "")),
                "cleaning_defect": _label(str(lead.get("primary_cleaning_defect") or "")),
                "wear_score": lead.get("wear_score", ""),
                "category": listing.get("category") or "repair",
                "category_label": _category_label(str(listing.get("category") or "repair")),
                "price_ex_gst": int(listing.get("price_ex_gst") or 0),
                "price_label": format_price(int(listing.get("price_ex_gst") or 0)),
                "published_at": str(listing.get("published_at") or ""),
                "expires_at": str(listing.get("expires_at") or ""),
                "expires_label": str(listing.get("expires_at") or "")[:10],
                "listing_state": bucket,
            }
        )
    rows.sort(key=lambda row: (str(row["suburb"]).lower(), row["lead_id"]))
    return rows


def _public_card(row: dict) -> dict:
    """Buyer-facing fields. The street address is never included."""
    category = row["category"] if row["category"] in CATEGORIES else "repair"
    defect = row["cleaning_defect"] if category == "cleaning" else row["primary_defect"]
    return {
        "lead_id": row["lead_id"],
        "suburb": row["suburb"],
        "postcode": row["postcode"],
        "lat": row["lat"],
        "lon": row["lon"],
        "material": row["material"],
        "roof_area_label": row["roof_area_label"],
        "defect": defect or "Not recorded",
        "wear_score": row["wear_score"],
        "category": category,
        "category_label": _category_label(category),
        "price_ex_gst": row["price_ex_gst"],
        "price_label": row["price_label"],
        "expires_label": row["expires_label"],
    }


def _suburb_centre(suburb: str, postcode: str) -> tuple[float | None, float | None]:
    """Suburb centre for a lead that has no property coordinate.

    The result is cached. The query is the suburb and postcode only, so the
    street address is never sent to the map service.
    """
    name = " ".join(suburb.casefold().split())
    code = postcode.strip()
    if not name or name == "unknown suburb" or len(code) != 4:
        return None, None
    key = f"{name}|{code}"
    cache = _load_suburb_points()
    saved = cache.get(key)
    if isinstance(saved, dict):
        return _number(saved.get("lat")), _number(saved.get("lon"))
    try:
        import httpx

        response = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{suburb} {code} Australia", "format": "jsonv2", "limit": "1", "countrycodes": "au"},
            headers={"User-Agent": "QMSRoofScan/1.0 (marketplace map)"},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None, None
    point = None
    if payload:
        point = (_number(payload[0].get("lat")), _number(payload[0].get("lon")))
    if point and point[0] is not None and point[1] is not None:
        cache[key] = {"lat": point[0], "lon": point[1]}
        _save_suburb_points(cache)
        return point
    return None, None


def _load_suburb_points() -> dict:
    path = DATA_DIR / "suburb_points.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_suburb_points(cache: dict) -> None:
    path = DATA_DIR / "suburb_points.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")


def _number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _label(value: str) -> str:
    text = value.replace("_", " ").strip()
    if not text or text in {"—", "-"}:
        return "Not recorded"
    return text.title()


def _area_label(value: object) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "Not recorded"
    if number <= 0:
        return "Not recorded"
    return f"{int(round(number))} m²"


def expire_due() -> dict[str, dict]:
    """Move listings past their end date into the archive."""
    listings = _load_listings()
    changed = False
    moment = _now()
    for lead_id, listing in listings.items():
        if not listing.get("published"):
            continue
        expires = _parse_time(str(listing.get("expires_at") or ""))
        if expires is not None and expires <= moment:
            listing["published"] = False
            listing["archived"] = True
            listings[lead_id] = listing
            changed = True
    if changed:
        _save_listings(listings)
    return listings


def _live_record(category: str, price: int, days: int) -> dict:
    start = _now()
    return {
        "published": True,
        "archived": False,
        "category": category,
        "price_ex_gst": int(price),
        "published_at": start.isoformat(),
        "expires_at": (start + timedelta(days=days)).isoformat(),
    }


def _score_for(lead: dict, category: str) -> int | None:
    raw = lead.get("cleaning_score") if category == "cleaning" else lead.get("wear_score")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _category_label(category: str) -> str:
    return "Cleaning" if category == "cleaning" else "Repair / wear"


def _listing_state(listing: dict, is_live: bool) -> str:
    if listing.get("sold"):
        return "sold"
    if is_live:
        return "live"
    if listing.get("unpublished"):
        return "unpublished"
    if listing.get("archived"):
        return "expired"
    return "unpublished"


def _load_listings() -> dict[str, dict]:
    return _read_dict(LISTINGS_PATH)


def _save_listings(listings: dict[str, dict]) -> None:
    _write_dict(LISTINGS_PATH, listings)


def _load_orders() -> dict[str, dict]:
    return _read_dict(ORDERS_PATH)


def _save_orders(orders: dict[str, dict]) -> None:
    _write_dict(ORDERS_PATH, orders)


def _read_dict(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_dict(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sydney(moment: datetime) -> datetime:
    """Sydney local time for invoice numbers. Fixed offset if the zone database is absent."""
    try:
        return moment.astimezone(ZoneInfo("Australia/Sydney"))
    except (ZoneInfoNotFoundError, ModuleNotFoundError):
        return moment.astimezone(timezone(timedelta(hours=11)))


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _positive(value: object, fallback: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if number >= 1 else fallback


def _non_negative(value: object, fallback: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback
