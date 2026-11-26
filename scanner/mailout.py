"""Write the mail-out CSV and, when asked, upsert it to Supabase."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from scanner.config import env
from scanner.flag_highres import high_res_text, parse_high_res, read_high_res_index, resolve_high_res
from scanner.occupancy import resolve_occupancy, read_occupancy_index

logger = logging.getLogger(__name__)

MAILOUT_FIELDS = (
    "lead_id",
    "street_address",
    "suburb",
    "state",
    "postcode",
    "lat",
    "lon",
    "roof_area_m2",
    "material",
    "roof_material",
    "wear_score",
    "repair_likelihood",
    "cleaning_score",
    "primary_cleaning_defect",
    "needs_cleaning",
    "primary_defect",
    "verified_defect",
    "pass2_confidence",
    "summary",
    "defect_box",
    "price_low_aud",
    "price_high_aud",
    "pricing_basis",
    "tile_path",
    "annotated_path",
    "occupancy_type",
    "occupancy_source",
    "high_res_requested",
)


def write_mailout(path: Path, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Write the mail-out. Saved occupancy and high-res flags are kept."""
    previous_occupancy = read_occupancy_index(path)
    previous_high_res = read_high_res_index(path)
    stamped: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        lead_id = str(item.get("lead_id") or "")
        occupancy_type, occupancy_source = resolve_occupancy(item, previous_occupancy.get(lead_id))
        item["occupancy_type"] = occupancy_type
        item["occupancy_source"] = occupancy_source
        item["high_res_requested"] = high_res_text(
            resolve_high_res(item, previous_high_res.get(lead_id))
        )
        stamped.append(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAILOUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in stamped:
            writer.writerow({field: _csv_value(row.get(field, "")) for field in MAILOUT_FIELDS})
    return stamped


def push_supabase(rows: list[dict[str, object]]) -> int:
    """Upsert cleared leads. Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."""
    url = env("SUPABASE_URL")
    key = env("SUPABASE_SERVICE_ROLE_KEY") or env("SUPABASE_KEY")
    table = env("SUPABASE_TABLE") or "cleared_mailout"
    if not url or not key:
        raise RuntimeError(
            "Supabase push needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env."
        )
    if not rows:
        logger.info("No cleared leads to push to Supabase.")
        return 0

    from supabase import create_client

    client = create_client(url, key)
    payload = []
    for row in rows:
        item = {field: _json_value(row.get(field)) for field in MAILOUT_FIELDS}
        item["high_res_requested"] = parse_high_res(row.get("high_res_requested"))
        payload.append(item)
    client.table(table).upsert(payload, on_conflict="lead_id").execute()
    logger.info("Upserted %s row(s) into Supabase table %s", len(payload), table)
    return len(payload)


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _json_value(value: object) -> object:
    if value == "":
        return None
    return value
