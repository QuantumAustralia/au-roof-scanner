"""Marketplace listings, prices, and orders.

Prices are whole Australian dollars exclusive of GST. A listing is live until
its expiry, then it moves to the archive. Only live listings can be sold.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    return _rows(leads, live_only=True)


def archive_rows(leads: list[dict]) -> list[dict]:
    """Leads whose listing has expired."""
    return _rows(leads, live_only=False)


def public_cards(leads: list[dict]) -> list[dict]:
    """Live listings safe to show before payment. Street addresses are omitted."""
    cards = []
    for row in live_rows(leads):
        cards.append(_public_card(row))
    return cards


def cards_for_checkout(leads: list[dict], lead_ids: list[str]) -> list[dict]:
    """Live cards for the ids in the cart, in the order the buyer added them."""
    live = {row["lead_id"]: row for row in live_rows(leads)}
    chosen = []
    seen: set[str] = set()
    for lead_id in lead_ids:
        if lead_id in seen or lead_id not in live:
            continue
        seen.add(lead_id)
        chosen.append(_public_card(live[lead_id]))
    return chosen


def order_shortfall(count: int) -> int:
    """How many more leads are required. Zero when the minimum is met."""
    return max(0, load_config()["minimum_order"] - count)


def create_order(email: str, cards: list[dict]) -> dict:
    """Store a pending purchase. Payment is confirmed later."""
    orders = _load_orders()
    order_id = uuid.uuid4().hex[:16]
    total = sum(int(card["price_ex_gst"]) for card in cards)
    order = {
        "order_id": order_id,
        "email": email.strip(),
        "lead_ids": [card["lead_id"] for card in cards],
        "amount_ex_gst": total,
        "status": "pending",
        "emailed": False,
        "stripe_session_id": "",
        "created_at": _now().isoformat(),
    }
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
    orders[order_id] = order
    _save_orders(orders)
    return order


def mark_emailed(order_id: str) -> None:
    orders = _load_orders()
    order = orders.get(order_id)
    if not order:
        return
    order["emailed"] = True
    orders[order_id] = order
    _save_orders(orders)


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
                "address": lead.get("address") or "",
                "suburb": lead.get("suburb") or "",
                "postcode": lead.get("postcode") or "",
                "category": listing.get("category") or "",
                "wear_score": lead.get("wear_score"),
                "price_ex_gst": int(listing.get("price_ex_gst") or 0),
                "price_label": format_price(int(listing.get("price_ex_gst") or 0)),
            }
        )
    return details


def _rows(leads: list[dict], *, live_only: bool) -> list[dict]:
    listings = expire_due()
    by_id = {str(lead["lead_id"]): lead for lead in leads}
    rows = []
    for lead_id, listing in listings.items():
        is_live = bool(listing.get("published")) and not listing.get("archived")
        if is_live != live_only:
            continue
        lead = by_id.get(lead_id, {})
        rows.append(
            {
                "lead_id": lead_id,
                "suburb": lead.get("suburb") or "Unknown suburb",
                "postcode": str(lead.get("postcode") or ""),
                "lat": lead.get("lat") if isinstance(lead.get("lat"), float) else None,
                "lon": lead.get("lon") if isinstance(lead.get("lon"), float) else None,
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
