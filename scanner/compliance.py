"""Privacy, attribution, and checkout-consent records.

Street addresses stay off the public marketplace. A suppressed property is
removed from the public list. Consent is stored on the order and sent to
Stripe. Supabase is updated when it is configured.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from scanner.config import DATA_DIR, env

TERMS_VERSION = "v1.0"
SUPPRESSED_PATH = DATA_DIR / "suppressed_properties.json"
logger = logging.getLogger(__name__)

DATA_NOTICE = (
    "Data Use & Privacy Notice: Property records include NSW spatial data "
    "(© State of New South Wales, DCS Spatial Services, CC BY 4.0) and contain no homeowner contact details. "
    'Supplied strictly on an "as is" basis for single-business use only (no resale). '
    "Purchaser warrants compliance with the Privacy Act 1988 (APPs, including APP 7 opt-outs), "
    "Spam Act 2003, Do Not Call Register Act 2006, and NSW Fair Trading licensing rules. "
    "Full terms: {url}"
)

AUTOMATED_DECISION = (
    "Automated Processing & Aerial Roof Assessments: We use automated computer-vision models to analyse "
    "publicly accessible and licensed aerial imagery and classify property roof conditions (such as visible wear, "
    "moss, or structural damage) into service categories for roofing contractors. These automated assessments are "
    "indicative only, do not determine credit, insurance eligibility, or legal rights, and are subject to human "
    "review prior to publication on our marketplace."
)


def data_notice(base_url: str) -> str:
    """Short notice with the full data-terms address."""
    root = base_url.rstrip("/")
    return DATA_NOTICE.format(url=f"{root}/data-terms")


def load_suppressed() -> set[tuple[str, str, str]]:
    """Street, suburb, and postcode keys that must not appear in public listings."""
    found = _read_suppressed_file()
    found.update(_read_suppressed_supabase())
    return found


def is_suppressed(lead: dict, blocked: set[tuple[str, str, str]]) -> bool:
    """True when this lead's property is on the suppression list."""
    if not blocked:
        return False
    street = _street(str(lead.get("address") or ""))
    suburb = _key(lead.get("suburb"))
    postcode = str(lead.get("postcode") or "").strip()
    if not street or not suburb or not postcode:
        return False
    return (street, suburb, postcode) in blocked


def consent_record(accepted_at: datetime, ip_address: str, user_agent: str) -> dict:
    """Fields stored on the order and copied into the Stripe session."""
    return {
        "terms_accepted_at": accepted_at.astimezone(timezone.utc).isoformat(),
        "terms_version": TERMS_VERSION,
        "buyer_ip": (ip_address or "")[:200],
        "buyer_user_agent": (user_agent or "")[:500],
    }


def push_order(order: dict) -> None:
    """Write the paid order, including consent, to Supabase when credentials exist."""
    url = env("SUPABASE_URL")
    key = env("SUPABASE_SERVICE_ROLE_KEY") or env("SUPABASE_KEY")
    if not url or not key:
        return
    try:
        from supabase import create_client

        payload = {
            "order_id": order.get("order_id"),
            "email": order.get("email") or "",
            "amount_ex_gst": int(order.get("amount_ex_gst") or 0),
            "status": order.get("status") or "",
            "terms_accepted_at": order.get("terms_accepted_at") or None,
            "terms_version": order.get("terms_version") or "",
            "buyer_ip": order.get("buyer_ip") or "",
            "buyer_user_agent": order.get("buyer_user_agent") or "",
            "created_at": order.get("created_at") or None,
        }
        create_client(url, key).table("orders").upsert(payload, on_conflict="order_id").execute()
    except Exception as exc:
        logger.warning("Could not store the order in Supabase: %s", exc)


def _read_suppressed_file() -> set[tuple[str, str, str]]:
    if not SUPPRESSED_PATH.is_file():
        return set()
    try:
        data = json.loads(SUPPRESSED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    rows = data if isinstance(data, list) else data.get("rows", []) if isinstance(data, dict) else []
    found: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        street = _key(row.get("street_address"))
        suburb = _key(row.get("suburb"))
        postcode = str(row.get("postcode") or "").strip()
        if street and suburb and postcode:
            found.add((street, suburb, postcode))
    return found


def _read_suppressed_supabase() -> set[tuple[str, str, str]]:
    url = env("SUPABASE_URL")
    key = env("SUPABASE_SERVICE_ROLE_KEY") or env("SUPABASE_KEY")
    if not url or not key:
        return set()
    try:
        from supabase import create_client

        response = create_client(url, key).table("suppressed_properties").select(
            "street_address,suburb,postcode"
        ).execute()
    except Exception as exc:
        logger.warning("Could not read suppressed properties: %s", exc)
        return set()
    found: set[tuple[str, str, str]] = set()
    for row in response.data or []:
        street = _key(row.get("street_address"))
        suburb = _key(row.get("suburb"))
        postcode = str(row.get("postcode") or "").strip()
        if street and suburb and postcode:
            found.add((street, suburb, postcode))
    return found


def _street(address: str) -> str:
    return _key(address.split(",", 1)[0])


def _key(value: object) -> str:
    return " ".join(str(value or "").casefold().split())
