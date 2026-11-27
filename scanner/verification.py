"""Address check written after grading.

Each processed lead is added to data/verification_report.csv and
data/verification_report.html. A new lead is pending_review. A lead that
already has a status keeps it, so approved and rejected are not reset.
"""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from scanner.addresses import Address, load_addresses
from scanner.config import DATA_DIR, GRADES_DIR

REPORT_CSV = DATA_DIR / "verification_report.csv"
REPORT_HTML = DATA_DIR / "verification_report.html"
PENDING = "pending_review"

REPORT_FIELDS = (
    "lead_id",
    "street_address",
    "suburb",
    "postcode",
    "lat",
    "lon",
    "roof_material",
    "wear_score",
    "primary_defect",
    "confidence",
    "google_maps_link",
    "status",
)


def maps_link(lat: float, lon: float) -> str:
    """Ready-to-open Google Maps search for the graded coordinate."""
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def write_verification_report(addresses: list[Address]) -> Path:
    """Merge graded leads into the CSV and rewrite the HTML table."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stored = _read_report()
    for address in addresses:
        grade = _read_grade(address.lead_id)
        if not grade:
            continue
        row = _row_from_grade(address, grade)
        previous = stored.get(address.lead_id)
        if previous and (previous.get("status") or "").strip():
            row["status"] = previous["status"].strip()
        stored[address.lead_id] = row
    ordered = sorted(stored.values(), key=_sort_key)
    _write_csv(ordered)
    _write_html(ordered)
    return REPORT_CSV


def report_saved_grades() -> Path:
    """Build the report from grades already on disk. Does not call the grader."""
    return write_verification_report(_graded_addresses())


def _row_from_grade(address: Address, grade: dict) -> dict[str, str]:
    first = grade.get("pass1") if isinstance(grade.get("pass1"), dict) else {}
    second = grade.get("pass2") if isinstance(grade.get("pass2"), dict) else {}
    wear = first.get("wear_score", first.get("visual_wear", ""))
    confidence = second.get("pass2_confidence", first.get("confidence", ""))
    return {
        "lead_id": address.lead_id,
        "street_address": address.street_address,
        "suburb": address.suburb,
        "postcode": address.postcode,
        "lat": f"{address.lat:.6f}",
        "lon": f"{address.lon:.6f}",
        "roof_material": str(first.get("material") or ""),
        "wear_score": "" if wear in (None, "") else str(wear),
        "primary_defect": str(first.get("primary_defect") or ""),
        "confidence": _confidence(confidence),
        "google_maps_link": maps_link(address.lat, address.lon),
        "status": PENDING,
    }


def _confidence(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _read_report() -> dict[str, dict[str, str]]:
    if not REPORT_CSV.is_file():
        return {}
    found: dict[str, dict[str, str]] = {}
    with REPORT_CSV.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            lead_id = (row.get("lead_id") or "").strip()
            if lead_id:
                found[lead_id] = {name: (row.get(name) or "").strip() for name in REPORT_FIELDS}
    return found


def _write_csv(rows: list[dict[str, str]]) -> None:
    with REPORT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _write_html(rows: list[dict[str, str]]) -> None:
    body = "\n".join(_html_row(row) for row in rows)
    page = f"""<!DOCTYPE html>
<html lang="en-AU">
<head>
  <meta charset="utf-8">
  <title>Address verification</title>
  <style>
    body {{ font-family: "Segoe UI", sans-serif; margin: 24px; color: #1c2430; }}
    h1 {{ font-size: 24px; margin-bottom: 8px; }}
    p {{ color: #5c6b7a; max-width: 46rem; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    th, td {{ border-bottom: 1px solid #e3e8ee; text-align: left; padding: 8px; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: #fff; }}
    a {{ color: #1f4e79; }}
  </style>
</head>
<body>
  <h1>Address verification</h1>
  <p>Check each map link, then set status in data/verification_report.csv to approved or rejected before a mail-out. New leads stay pending_review until you change them.</p>
  <table>
    <thead>
      <tr>
        <th>Lead</th>
        <th>Address</th>
        <th>Suburb</th>
        <th>Postcode</th>
        <th>Material</th>
        <th>Wear</th>
        <th>Defect</th>
        <th>Confidence</th>
        <th>Status</th>
        <th>Map</th>
      </tr>
    </thead>
    <tbody>
{body}
    </tbody>
  </table>
</body>
</html>
"""
    REPORT_HTML.write_text(page, encoding="utf-8")


def _html_row(row: dict[str, str]) -> str:
    cells = [
        row["lead_id"],
        row["street_address"],
        row["suburb"],
        row["postcode"],
        row["roof_material"].replace("_", " "),
        row["wear_score"],
        row["primary_defect"].replace("_", " "),
        row["confidence"],
        row["status"],
    ]
    rendered = "".join(f"<td>{html.escape(value)}</td>" for value in cells)
    link = html.escape(row["google_maps_link"], quote=True)
    return f'      <tr>{rendered}<td><a href="{link}">Open in Google Maps</a></td></tr>'


def _graded_addresses() -> list[Address]:
    """Address rows whose grade file is already saved."""
    found: dict[str, Address] = {}
    paths = [Path("input_addresses.csv"), *sorted(DATA_DIR.glob("addresses_*.csv"))]
    for path in paths:
        if not path.is_file():
            continue
        try:
            rows = load_addresses(path, allow_any_postcode=True)
        except (OSError, ValueError):
            continue
        for address in rows:
            if (GRADES_DIR / f"{address.lead_id}.json").is_file():
                found[address.lead_id] = address
    return list(found.values())


def _read_grade(lead_id: str) -> dict | None:
    path = GRADES_DIR / f"{lead_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _sort_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (row["suburb"].lower(), row["street_address"].lower(), row["lead_id"])
