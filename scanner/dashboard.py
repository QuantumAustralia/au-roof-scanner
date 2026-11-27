"""Local dashboard for reviewing leads and starting a postcode scan.

Run from the project directory:

    python -m scanner.dashboard

Then open http://127.0.0.1:5000 on this machine. There is no login.
"""

from __future__ import annotations

import csv
import json
import os
import re
import logging
import smtplib
import sys
import zipfile
from datetime import datetime
from email.message import EmailMessage
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from PIL import Image, ImageDraw

if __name__ == "__main__" and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.addresses import load_addresses, safe_lead_id
from scanner.config import (
    ANNOTATED_DIR,
    CLUSTERS_DIR,
    DATA_DIR,
    ENHANCED_DIR,
    GRADES_DIR,
    MAILOUT_PATH,
    PASS2_WEAR_MIN,
    ROOT,
    TILES_DIR,
    ensure_data_dirs,
    env,
    require_env,
)
from scanner.fetch_tiles import closer_tile_unavailable_reason, fetch_all, fetch_closer_tile
from scanner.flag_highres import parse_high_res, set_high_res
from scanner.grade_roofs import grade_all
from scanner.clusters import suburb_prefix
from scanner.lead_match import (
    STORED_MATERIALS,
    export_cleaning_matches,
    export_repair_matches,
)
from scanner.load_gnaf import load_postcode
from scanner.mailout import write_mailout
from scanner import marketplace as market
from scanner.quote import QUOTES_PATH, quote_cleared_csv
from scanner.scan_guard import is_locked, load_lockout, record_scanned
from scanner.triage import RoofTriage

logger = logging.getLogger(__name__)

STATUS_PATH = DATA_DIR / "manual_status.json"
OWNER_PATH = DATA_DIR / "owner_flag.json"
MANUAL_PATH = DATA_DIR / "manual_inputs.json"
EXPORT_SETTINGS_PATH = DATA_DIR / "export_settings.json"
REFERENCE_PATH = DATA_DIR / "reference_numbers.json"
_READABLE_REFERENCE = re.compile(r"^[A-Z]{2,4}-\d{4}$")
MAILOUT_EXPORT_DIR = DATA_DIR / "mailout"
DEFAULT_EXPORT_WEAR = PASS2_WEAR_MIN
EXPORT_FLAGS = {"auto", "include", "exclude"}
_MAILOUT_FILE = re.compile(r"^mailout_\d{8}_\d{4}(?:\d{2})?\.csv$")
OWNERS = (
    ("owner", "Owner"),
    ("rental", "Rental"),
    ("unknown", "Unknown"),
)
OWNER_VALUES = {key for key, _label in OWNERS}
STATUSES = (
    ("new", "New"),
    ("flagged", "Flagged"),
    ("contacted", "Contacted"),
    ("not_interested", "Not interested"),
)
STATUS_VALUES = {key for key, _label in STATUSES}
ROOFER_OUTCOMES = (
    ("not_visited", "Not visited"),
    ("visited_no_quote", "Visited – no quote"),
    ("visited_quoted", "Visited – quoted"),
    ("job_won", "Job won"),
)
ROOFER_VALUES = {key for key, _label in ROOFER_OUTCOMES}
SALE_STATUSES = (
    ("available", "Available"),
    ("offered", "Offered"),
    ("reserved", "Reserved"),
    ("sold", "Sold"),
    ("rejected", "Rejected"),
)
SALE_STATUS_VALUES = {key for key, _label in SALE_STATUSES}
SALE_LABELS = dict(SALE_STATUSES)
UNOFFERABLE = {"offered", "reserved", "sold"}

app = Flask(__name__)
app.secret_key = "au-roof-scanner-local"


def latest_mailout_path() -> Path | None:
    candidates = [path for path in DATA_DIR.glob("*cleared*.csv") if path.is_file()]
    if not candidates:
        return MAILOUT_PATH if MAILOUT_PATH.is_file() else None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _read_csv(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _lead_index(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for row in rows:
        lead_id = (row.get("lead_id") or "").strip()
        if lead_id:
            index[lead_id] = row
    return index


def load_statuses() -> dict[str, str]:
    if not STATUS_PATH.is_file():
        return {}
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if str(value) in STATUS_VALUES}


def load_owners() -> dict[str, str]:
    if not OWNER_PATH.is_file():
        return {}
    try:
        data = json.loads(OWNER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if str(value) in OWNER_VALUES}


def save_owner(lead_id: str, occupancy: str) -> None:
    if occupancy not in OWNER_VALUES:
        raise ValueError("Choose Owner, Rental, or Unknown.")
    owners = load_owners()
    owners[lead_id] = occupancy
    OWNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    OWNER_PATH.write_text(json.dumps(owners, indent=2) + "\n", encoding="utf-8")


def load_manual_inputs() -> dict[str, dict[str, object]]:
    if not MANUAL_PATH.is_file():
        return {}
    try:
        data = json.loads(MANUAL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, dict[str, object]] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            cleaned[str(key)] = value
    return cleaned


def load_export_threshold() -> int:
    if not EXPORT_SETTINGS_PATH.is_file():
        return DEFAULT_EXPORT_WEAR
    try:
        data = json.loads(EXPORT_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_EXPORT_WEAR
    if not isinstance(data, dict):
        return DEFAULT_EXPORT_WEAR
    try:
        threshold = int(data.get("repair_likelihood_threshold", data.get("wear_threshold")))
    except (TypeError, ValueError):
        return DEFAULT_EXPORT_WEAR
    if 1 <= threshold <= 5:
        return threshold
    return DEFAULT_EXPORT_WEAR


def save_export_threshold(threshold: int) -> None:
    if not 1 <= threshold <= 5:
        raise ValueError("Repair likelihood threshold must be a whole number from 1 to 5.")
    EXPORT_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_SETTINGS_PATH.write_text(
        json.dumps(
            {"repair_likelihood_threshold": threshold, "wear_threshold": threshold},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def export_decision(
    repair_likelihood: int | None,
    export_flag: str,
    threshold: int,
) -> tuple[bool, str]:
    """Include a lead when repair likelihood meets the threshold, unless a manual flag overrides it."""
    if export_flag == "include":
        return True, "Manually flagged – included"
    if export_flag == "exclude":
        return False, "Manually excluded"
    if repair_likelihood is None:
        return False, "No repair score – excluded"
    if repair_likelihood >= threshold:
        return True, f"Repair likelihood {repair_likelihood} – included"
    return False, f"Repair likelihood {repair_likelihood} – excluded"


def save_status(lead_id: str, status: str) -> None:
    if status not in STATUS_VALUES:
        raise ValueError("Choose a status from the list.")
    statuses = load_statuses()
    statuses[lead_id] = status
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(statuses, indent=2) + "\n", encoding="utf-8")


def _money(value: object) -> str:
    try:
        amount = float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return ""
    return f"${amount:,.0f}"


def _quote_range(mailout: dict[str, str] | None, quote: dict[str, str] | None) -> str:
    low = _money((mailout or {}).get("price_low_aud"))
    high = _money((mailout or {}).get("price_high_aud"))
    if low and high:
        return f"{low} – {high}"
    single = _money((quote or {}).get("quote_aud"))
    return single or "—"


def _address(mailout: dict[str, str] | None, quote: dict[str, str] | None) -> str:
    if mailout:
        street = (mailout.get("street_address") or "").strip()
        locality = " ".join(
            part
            for part in (
                (mailout.get("suburb") or "").strip(),
                (mailout.get("state") or "").strip(),
                (mailout.get("postcode") or "").strip(),
            )
            if part
        )
        if street and locality:
            return f"{street}, {locality}"
        if street:
            return street
    return ((quote or {}).get("address") or "").strip() or "—"


def _grades() -> dict[str, dict]:
    found: dict[str, dict] = {}
    if not GRADES_DIR.is_dir():
        return found
    for path in GRADES_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        lead_id = str(record.get("lead_id") or path.stem)
        found[lead_id] = record
    return found


def _notes(manual: dict[str, object], grade: dict | None) -> str:
    """Saved notes win. A visibility rejection is shown when notes are still empty."""
    saved = str(manual.get("notes") or "").strip()
    if saved or not grade:
        return saved
    reason = str(grade.get("rejection_reason") or "").strip()
    if reason:
        return reason
    first = grade.get("pass1")
    if isinstance(first, dict):
        return str(first.get("rejection_reason") or "").strip()
    return ""


def _wear_score(mailout: dict[str, str] | None, quote: dict[str, str] | None, grade: dict | None) -> int | None:
    raw = None
    if grade and isinstance(grade.get("pass1"), dict):
        raw = grade["pass1"].get("wear_score", grade["pass1"].get("visual_wear"))
    if raw is None and mailout:
        raw = mailout.get("wear_score")
    if raw is None and quote:
        raw = quote.get("wear_score")
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _repair_likelihood(mailout: dict[str, str] | None, quote: dict[str, str] | None, grade: dict | None) -> int | None:
    raw = None
    if grade and isinstance(grade.get("pass1"), dict):
        raw = grade["pass1"].get("repair_likelihood")
    if raw in {None, ""} and mailout:
        raw = mailout.get("repair_likelihood")
    if raw in {None, ""} and quote:
        raw = quote.get("repair_likelihood")
    if raw in {None, ""}:
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    if 1 <= value <= 5:
        return value
    return None


def _optional_score(raw: object) -> int | None:
    if raw in {None, ""}:
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    if 1 <= value <= 5:
        return value
    return None


def _cleaning_score(mailout: dict[str, str] | None, grade: dict | None) -> int | None:
    """Cleaning score from the grade, then from a mail-out row written after a re-grade."""
    raw = None
    if grade and isinstance(grade.get("pass1"), dict):
        raw = grade["pass1"].get("cleaning_score")
    if raw in {None, ""} and mailout:
        raw = mailout.get("cleaning_score")
    return _optional_score(raw)


def _cleaning_defect(mailout: dict[str, str] | None, grade: dict | None) -> str:
    raw = ""
    if grade and isinstance(grade.get("pass1"), dict):
        raw = str(grade["pass1"].get("primary_cleaning_defect") or "")
    if not raw and mailout:
        raw = str(mailout.get("primary_cleaning_defect") or "")
    return raw.strip()


def _material_code(mailout: dict[str, str] | None, grade: dict | None) -> str:
    """Stored roof material code, unchanged, for the material filter."""
    if grade and isinstance(grade.get("pass1"), dict):
        code = str(grade["pass1"].get("material") or "").strip()
        if code:
            return code
    if mailout:
        return str(mailout.get("roof_material") or mailout.get("material") or "").strip()
    return ""


def _coordinate(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _roof_area(mailout: dict[str, str] | None) -> float | None:
    """Roof area in square metres from the mail-out row, when it was recorded."""
    raw = (mailout or {}).get("roof_area_m2")
    try:
        number = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _primary_defect(mailout: dict[str, str] | None, grade: dict | None) -> str:
    """Structural defect code. Cleaning defects stay on their own field."""
    if grade:
        text = str((grade.get("pass1") or {}).get("primary_defect") or "").strip()
        if text:
            return text
    return str((mailout or {}).get("primary_defect") or "").strip()


def _postcode(mailout: dict[str, str] | None, quote: dict[str, str] | None, grade: dict | None) -> str:
    if mailout and (mailout.get("postcode") or "").strip():
        return mailout["postcode"].strip()
    texts = [
        (quote or {}).get("address") or "",
        str((grade or {}).get("address") or ""),
    ]
    for text in texts:
        for token in reversed(text.replace(",", " ").split()):
            if token.isdigit() and len(token) == 4:
                return token
    return ""


def _suburb(mailout: dict[str, str] | None, quote: dict[str, str] | None, grade: dict | None) -> str:
    if mailout and (mailout.get("suburb") or "").strip():
        return mailout["suburb"].strip()
    for text in (
        (quote or {}).get("address") or "",
        str((grade or {}).get("address") or ""),
    ):
        if "," not in text:
            continue
        tail = text.split(",", 1)[1].strip().split()
        if len(tail) >= 3:
            return " ".join(tail[:-2])
    return ""


def _manual_override(record: dict[str, object]) -> int | None:
    raw = record.get("wear_override")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= value <= 5:
        return value
    return None


def load_leads() -> tuple[list[dict[str, object]], Path | None, Path]:
    """Every graded lead, with the mailout decision for the saved export threshold."""
    mailout_path = latest_mailout_path()
    mailouts = _lead_index(_read_csv(mailout_path))
    quotes = _lead_index(_read_csv(QUOTES_PATH))
    grades = _grades()
    statuses = load_statuses()
    owners = load_owners()
    manuals = load_manual_inputs()
    threshold = load_export_threshold()
    leads: list[dict[str, object]] = []
    for lead_id in sorted(set(mailouts) | set(quotes) | set(grades)):
        mailout = mailouts.get(lead_id)
        quote = quotes.get(lead_id)
        grade = grades.get(lead_id)
        wear = _wear_score(mailout, quote, grade)
        if wear is None:
            continue
        source = mailout or quote or {}
        manual = manuals.get(lead_id, {})
        csv_flag = parse_high_res((mailout or {}).get("high_res_requested")) or parse_high_res(
            (quote or {}).get("high_res_requested")
        )
        if "high_res" in manual:
            flagged = bool(manual.get("high_res"))
        else:
            flagged = csv_flag
        override = _manual_override(manual)
        export_flag = str(manual.get("export_flag") or "auto")
        if export_flag not in EXPORT_FLAGS:
            export_flag = "auto"
        outcome = str(manual.get("roofer_outcome") or "not_visited")
        if outcome not in ROOFER_VALUES:
            outcome = "not_visited"
        sale_status = str(manual.get("sale_status") or "available")
        if sale_status not in SALE_STATUS_VALUES:
            sale_status = "available"
        offered_date = str(manual.get("offered_date") or "")
        repair = _repair_likelihood(mailout, quote, grade)
        cleaning = _cleaning_score(mailout, grade)
        cleaning_defect = _cleaning_defect(mailout, grade)
        material_code = _material_code(mailout, grade)
        included, reason = export_decision(repair, export_flag, threshold)
        effective = wear if override is None else override
        image_name = f"{safe_lead_id(lead_id)}_enhanced.png"
        annotated = ANNOTATED_DIR / f"{safe_lead_id(lead_id)}.png"
        highres_name = f"{safe_lead_id(lead_id)}_highres.png"
        leads.append(
            {
                "lead_id": lead_id,
                "address": _address(mailout, quote) if (mailout or quote) else str((grade or {}).get("address") or "—"),
                "suburb": _suburb(mailout, quote, grade) or "Unknown suburb",
                "state": ((mailout or {}).get("state") or "").strip(),
                "postcode": _postcode(mailout, quote, grade),
                "lat": _coordinate((source or {}).get("lat")),
                "lon": _coordinate((source or {}).get("lon")),
                "material_code": material_code,
                "material": material_code.replace("_", " ") if material_code else "—",
                "roof_area_m2": _roof_area(mailout),
                "primary_defect": _primary_defect(mailout, grade),
                "wear_score": wear,
                "repair_likelihood": "" if repair is None else repair,
                "cleaning_score": "" if cleaning is None else cleaning,
                "cleaning_defect_code": cleaning_defect,
                "primary_cleaning_defect": cleaning_defect.replace("_", " ") if cleaning_defect else "",
                "needs_cleaning": cleaning is not None and cleaning >= 3,
                "wear_override": "" if override is None else str(override),
                "effective_wear": effective,
                "quote_range": _quote_range(mailout, quote),
                "high_res": flagged,
                "high_res_note": str(manual.get("high_res_note") or ""),
                "has_highres": (TILES_DIR / highres_name).is_file(),
                "status": statuses.get(lead_id, "new"),
                "sale_status": sale_status,
                "contractor": str(manual.get("contractor") or ""),
                "offered_date": offered_date,
                "invoice_number": str(manual.get("invoice_number") or ""),
                "owner": owners.get(lead_id, "unknown"),
                "roofer_outcome": outcome,
                "notes": _notes(manual, grade),
                "export_flag": export_flag,
                "export_included": included,
                "export_reason": reason,
                "annotated_image_path": annotated.relative_to(ROOT).as_posix() if annotated.is_file() else "",
                "has_image": (ENHANCED_DIR / image_name).is_file() or _source_image(lead_id) is not None,
            }
        )
    leads.sort(key=lambda lead: (-int(lead["wear_score"]), str(lead["lead_id"])))
    assign_references(leads)
    return leads, mailout_path, QUOTES_PATH


def _load_reference_map() -> dict[str, str]:
    if not REFERENCE_PATH.is_file():
        return {}
    try:
        data = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value).strip().upper() for key, value in data.items()}


def assign_references(leads: list[dict[str, object]]) -> None:
    """Keep a short stable reference. Readable lead ids such as ASH-0001 are reused."""
    stored = _load_reference_map()
    assigned: dict[str, str] = {}
    used: set[str] = set()
    for lead_id, reference in stored.items():
        if _READABLE_REFERENCE.match(reference) and reference not in used:
            assigned[lead_id] = reference
            used.add(reference)
    for lead in leads:
        lead_id = str(lead["lead_id"]).upper()
        if _READABLE_REFERENCE.match(lead_id):
            used.add(lead_id)
    counters: dict[str, int] = {}

    def allocate(prefix: str) -> str:
        number = counters.get(prefix, 1)
        while True:
            candidate = f"{prefix}-{number:04d}"
            number += 1
            if candidate not in used:
                counters[prefix] = number
                used.add(candidate)
                return candidate

    for lead in leads:
        lead_id = str(lead["lead_id"])
        readable = lead_id.upper()
        if _READABLE_REFERENCE.match(readable):
            reference = readable
        elif lead_id in assigned:
            reference = assigned[lead_id]
        else:
            reference = allocate(suburb_prefix(str(lead["suburb"])))
        assigned[lead_id] = reference
        lead["reference"] = reference
    if assigned != stored:
        _write_json(REFERENCE_PATH, assigned)


def _files_for_lead(lead_id: str) -> list[Path]:
    paths: list[Path] = []
    mailout = latest_mailout_path()
    for path in (mailout, QUOTES_PATH):
        if path is None or not path.is_file():
            continue
        if lead_id in _lead_index(_read_csv(path)):
            paths.append(path)
    return paths


def addresses_for_postcode(postcode: str) -> list:
    """Addresses for one postcode, from any Australian state.

    Load addresses writes data/addresses_{postcode}.csv. A scan of that
    postcode reads that file first. allow_any_postcode keeps NSW and other
    states; the pilot postcode is not hard-coded here.
    """
    found = []
    seen: set[str] = set()
    gnaf_csv = DATA_DIR / f"addresses_{postcode}.csv"
    candidates: list[Path] = []
    if gnaf_csv.is_file():
        candidates.append(gnaf_csv)
    candidates.extend(sorted(ROOT.glob("*.csv")))
    for path in candidates:
        try:
            rows = load_addresses(path, allow_any_postcode=True)
        except (OSError, ValueError):
            # A bad project CSV is skipped. The file just written for this
            # postcode is the scan source, so its error is shown instead.
            if path == gnaf_csv:
                raise
            continue
        for address in rows:
            if address.postcode == postcode and address.lead_id not in seen:
                seen.add(address.lead_id)
                found.append(address)
    return found


_APARTMENT_ADDRESS = re.compile(
    r"(?i)^(?:unit|apt|apartment|flat|suite|level|lvl|floor)\b|\d+\s*/\s*\d+"
)


def is_apartment_address(street: str) -> bool:
    """True for unit, apartment, flat, suite, level, floor, or a slash number such as 12/45."""
    return _APARTMENT_ADDRESS.search(street.strip()) is not None


def plan_scan(
    postcode: str,
    *,
    entire: bool,
    maximum: int | None,
    skip_apartments: bool = True,
) -> tuple[list, int, str]:
    """Choose which addresses a scan will run, skipping any still inside the lockout."""
    addresses = addresses_for_postcode(postcode)
    if not addresses:
        raise ValueError(f"No address file in the project contains postcode {postcode}.")
    if not entire:
        if maximum is None or maximum < 1:
            raise ValueError("Enter how many addresses to scan, or tick Scan entire postcode.")
    apartments = 0
    if skip_apartments:
        houses = []
        for address in addresses:
            if is_apartment_address(address.street_address):
                apartments += 1
            else:
                houses.append(address)
        addresses = houses
    lockout = load_lockout()
    unlocked = [address for address in addresses if not is_locked(address, lockout)]
    locked = len(addresses) - len(unlocked)
    chosen = unlocked if entire else unlocked[:maximum]
    months = lockout.months
    message = (
        f"This will scan {len(chosen)} address(es) in {postcode}. "
        f"{locked} already scanned within {months} months will be skipped."
    )
    if skip_apartments:
        message += f" Skipped {apartments} apartment addresses."
    return chosen, locked, message


def run_postcode_scan(addresses: list) -> str:
    if not addresses:
        raise ValueError("Nothing to scan. Every matching address is inside the lockout period.")
    previous = _read_csv(MAILOUT_PATH)
    saved, skipped, failed = fetch_all(addresses, api_key=require_env("GOOGLE_MAPS_API_KEY"))
    grade_all(addresses, triage=RoofTriage(), output=MAILOUT_PATH, push=False)
    fresh = _read_csv(MAILOUT_PATH)
    scanned_ids = {address.lead_id for address in addresses}
    kept = [row for row in previous if (row.get("lead_id") or "") not in scanned_ids]
    write_mailout(MAILOUT_PATH, kept + fresh)
    quoted = quote_cleared_csv(MAILOUT_PATH, QUOTES_PATH)
    record_scanned(addresses)
    return (
        f"Scanned {len(addresses)} address(es). "
        f"Tiles saved {saved}, already on disk {skipped}, failed {failed}. "
        f"Mail-out now has {len(quoted)} cleared lead(s)."
    )


def _scan_request() -> tuple[str, bool, int | None, bool]:
    postcode = (request.form.get("postcode") or "").strip()
    if not postcode.isdigit() or len(postcode) != 4:
        raise ValueError("Enter a four-digit postcode.")
    entire = request.form.get("entire") == "on"
    raw_limit = (request.form.get("maximum") or "").strip()
    maximum = None
    if not entire:
        if not raw_limit:
            raise ValueError("Enter how many addresses to scan, or tick Scan entire postcode.")
        if not raw_limit.isdigit() or int(raw_limit) < 1:
            raise ValueError("Maximum addresses must be a whole number greater than zero.")
        maximum = int(raw_limit)
    skip_apartments = request.form.get("skip_apartments") == "on"
    return postcode, entire, maximum, skip_apartments


def roof_image_file(lead_id: str) -> Path | None:
    """Annotated roof first, then the enhanced copy, then the original tile."""
    safe = safe_lead_id(lead_id)
    annotated = ANNOTATED_DIR / f"{safe}.png"
    if annotated.is_file():
        return annotated
    return _source_image(lead_id)


def _source_image(lead_id: str) -> Path | None:
    safe = safe_lead_id(lead_id)
    for path in (
        ENHANCED_DIR / f"{safe}_enhanced.png",
        ANNOTATED_DIR / f"{safe}.png",
        TILES_DIR / f"{safe}.png",
    ):
        if path.is_file():
            return path
    return None


def _enhanced_path(lead_id: str) -> Path:
    path = _source_image(lead_id)
    if path is None:
        abort(404)
    return path


def _roofer_image(lead_id: str, suburb: str) -> bytes | None:
    """Roof image with the suburb only. The street address is not drawn or named."""
    source = _source_image(lead_id)
    if source is None:
        return None
    with Image.open(source) as image:
        picture = image.convert("RGB")
        draw = ImageDraw.Draw(picture)
        label = suburb.strip() or "Unknown suburb"
        bar_height = max(36, picture.height // 18)
        draw.rectangle((0, picture.height - bar_height, picture.width, picture.height), fill=(20, 28, 38))
        draw.text((16, picture.height - bar_height + 8), label, fill=(255, 255, 255))
        buffer = BytesIO()
        picture.save(buffer, format="PNG")
    return buffer.getvalue()


def _file_slug(value: str) -> str:
    cleaned = "".join(character if character.isalnum() else "_" for character in value.strip())
    return cleaned.strip("_") or "suburb"


def _zip_bytes(files: list[tuple[str, bytes]]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in files:
            bundle.writestr(name, payload)
    return buffer.getvalue()


def _email_zip(recipient: str, payload: bytes) -> str:
    host = env("SMTP_HOST")
    sender = env("SMTP_FROM") or env("SMTP_USER")
    if not host or not sender:
        return "ZIP was downloaded. Email was not sent because SMTP_HOST and SMTP_FROM are not set."
    message = EmailMessage()
    message["Subject"] = "Roof images"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("Roof images for the selected suburbs are attached. Street addresses are not included.")
    message.add_attachment(payload, maintype="application", subtype="zip", filename="roofer_roofs.zip")
    try:
        with smtplib.SMTP(host, int(env("SMTP_PORT") or "587"), timeout=30) as client:
            client.starttls()
            user = env("SMTP_USER")
            password = env("SMTP_PASSWORD")
            if user:
                client.login(user, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        return f"ZIP was downloaded. Email to {recipient} failed: {exc}"
    return f"A copy of the ZIP was emailed to {recipient}."


def _parse_threshold(value: object) -> int:
    text = str(value or "").strip()
    if not text.isdigit():
        raise ValueError("Repair likelihood threshold must be a whole number from 1 to 5.")
    threshold = int(text)
    if not 1 <= threshold <= 5:
        raise ValueError("Repair likelihood threshold must be a whole number from 1 to 5.")
    return threshold


def _clean_manual_row(item: object, known_ids: set[str]) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ValueError("Each lead must be an object.")
    lead_id = str(item.get("lead_id") or "").strip()
    if lead_id not in known_ids:
        raise ValueError(f"Unknown lead {lead_id}.")
    raw_override = str(item.get("wear_override") if item.get("wear_override") is not None else "").strip()
    override: int | None = None
    if raw_override:
        if not raw_override.isdigit() or not 1 <= int(raw_override) <= 5:
            raise ValueError(f"{lead_id}: wear override must be a whole number from 1 to 5.")
        override = int(raw_override)
    notes = str(item.get("notes") or "").strip()
    if len(notes) > 500:
        raise ValueError(f"{lead_id}: notes must be 500 characters or fewer.")
    export_flag = str(item.get("export_flag") or "auto").strip()
    if export_flag not in EXPORT_FLAGS:
        raise ValueError(f"{lead_id}: choose Auto, Include, or Exclude.")
    owner = str(item.get("owner") or "unknown").strip()
    if owner not in OWNER_VALUES:
        raise ValueError(f"{lead_id}: choose Owner, Rental, or Unknown.")
    status = str(item.get("status") or "new").strip()
    if status not in STATUS_VALUES:
        raise ValueError(f"{lead_id}: choose a status from the list.")
    high_res = item.get("high_res") in {True, "true", "on", "1", 1}
    sale_status = str(item.get("sale_status") or "available").strip()
    if sale_status not in SALE_STATUS_VALUES:
        raise ValueError(f"{lead_id}: choose Available, Offered, Reserved, Sold, or Rejected.")
    contractor = str(item.get("contractor") or "").strip()
    if len(contractor) > 120:
        raise ValueError(f"{lead_id}: contractor must be 120 characters or fewer.")
    offered_date = str(item.get("offered_date") or "").strip()
    if offered_date:
        try:
            datetime.strptime(offered_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"{lead_id}: offered date must be a valid date.") from None
    invoice_number = str(item.get("invoice_number") or "").strip()
    if len(invoice_number) > 120:
        raise ValueError(f"{lead_id}: invoice number must be 120 characters or fewer.")
    outcome = str(item.get("roofer_outcome") or "not_visited").strip()
    if outcome not in ROOFER_VALUES:
        raise ValueError(f"{lead_id}: choose a roofer outcome from the list.")
    return {
        "lead_id": lead_id,
        "wear_override": override,
        "notes": notes,
        "export_flag": export_flag,
        "owner": owner,
        "status": status,
        "high_res": high_res,
        "roofer_outcome": outcome,
        "sale_status": sale_status,
        "contractor": contractor,
        "offered_date": offered_date,
        "invoice_number": invoice_number,
    }


def _capture_highres(lead: dict[str, object]) -> tuple[str, str]:
    limit = closer_tile_unavailable_reason()
    if limit:
        return "unavailable", limit
    return fetch_closer_tile(
        str(lead["lead_id"]),
        lead.get("lat") if isinstance(lead.get("lat"), float) else None,
        lead.get("lon") if isinstance(lead.get("lon"), float) else None,
        api_key=require_env("GOOGLE_MAPS_API_KEY"),
    )


def save_manual_batch(items: list[object]) -> tuple[int, str]:
    """Store edited manual fields. Returns how many leads changed and any high-res note."""
    leads, _mailout, _quotes = load_leads()
    current = {str(lead["lead_id"]): lead for lead in leads}
    cleaned = [_clean_manual_row(item, set(current)) for item in items]
    manuals = load_manual_inputs()
    owners = load_owners()
    statuses = load_statuses()
    changed = 0
    high_res_updates: list[tuple[str, bool]] = []
    high_res_note = ""
    for row in cleaned:
        lead_id = str(row["lead_id"])
        lead = current[lead_id]
        stored_override = None if lead.get("wear_override") in {None, ""} else int(str(lead["wear_override"]))
        same = (
            stored_override == row["wear_override"]
            and str(lead.get("notes") or "") == row["notes"]
            and lead.get("export_flag") == row["export_flag"]
            and lead.get("owner") == row["owner"]
            and lead.get("status") == row["status"]
            and lead.get("roofer_outcome") == row["roofer_outcome"]
            and lead.get("sale_status") == row["sale_status"]
            and str(lead.get("contractor") or "") == row["contractor"]
            and str(lead.get("offered_date") or "") == row["offered_date"]
            and str(lead.get("invoice_number") or "") == row["invoice_number"]
            and bool(lead.get("high_res")) == row["high_res"]
        )
        if same:
            continue
        stored_sale = str(lead.get("sale_status") or "available")
        if row["sale_status"] == "offered" and stored_sale in UNOFFERABLE and stored_sale != "offered":
            label = SALE_LABELS.get(stored_sale, stored_sale)
            raise ValueError(f"{lead_id} cannot be offered while it is {label}.")
        changed += 1
        note = "" if not row["high_res"] else str(lead.get("high_res_note") or "")
        if row["high_res"] and not bool(lead.get("high_res")):
            _status, note = _capture_highres(lead)
            high_res_note = note
        manuals[lead_id] = {
            "wear_override": row["wear_override"],
            "notes": row["notes"],
            "export_flag": row["export_flag"],
            "high_res": row["high_res"],
            "high_res_note": note,
            "roofer_outcome": row["roofer_outcome"],
            "sale_status": row["sale_status"],
            "contractor": row["contractor"],
            "offered_date": row["offered_date"],
            "invoice_number": row["invoice_number"],
        }
        owners[lead_id] = str(row["owner"])
        statuses[lead_id] = str(row["status"])
        if bool(lead.get("high_res")) != row["high_res"]:
            high_res_updates.append((lead_id, bool(row["high_res"])))
    _write_json(MANUAL_PATH, manuals)
    _write_json(OWNER_PATH, owners)
    _write_json(STATUS_PATH, statuses)
    for lead_id, flagged in high_res_updates:
        for path in _files_for_lead(lead_id):
            set_high_res(path, lead_id, flagged)
    return changed, high_res_note


MAILOUT_EXPORT_FIELDS = (
    "reference",
    "address",
    "wear_score",
    "repair_likelihood",
    "roofer_outcome",
    "image_link",
    "lead_id",
    "suburb",
    "state",
    "postcode",
    "material",
    "scored_wear",
    "wear_override",
    "notes",
    "high_res_requested",
    "occupancy",
    "status",
    "sale_status",
    "contractor",
    "offered_date",
    "invoice_number",
    "export_reason",
    "image_path",
    "quote_range",
)


def _mailout_rows(leads: list[dict[str, object]]) -> list[dict[str, object]]:
    rows = []
    for lead in leads:
        image = roof_image_file(str(lead["lead_id"]))
        image_path = ""
        image_link = ""
        if image is not None:
            image_path = image.relative_to(ROOT).as_posix()
            image_link = request.host_url.rstrip("/") + url_for("roof_image", lead_id=lead["lead_id"])
        reference = str(lead["reference"])
        rows.append(
            {
                "reference": reference,
                "address": f"{reference} — {lead['address']}",
                "wear_score": lead["effective_wear"],
                "repair_likelihood": lead["repair_likelihood"],
                "roofer_outcome": lead["roofer_outcome"],
                "image_link": image_link,
                "lead_id": lead["lead_id"],
                "suburb": lead["suburb"],
                "state": lead["state"],
                "postcode": lead["postcode"],
                "material": lead["material"],
                "scored_wear": lead["wear_score"],
                "wear_override": lead["wear_override"],
                "notes": lead["notes"],
                "high_res_requested": "true" if lead["high_res"] else "false",
                "occupancy": lead["owner"],
                "status": lead["status"],
                "sale_status": lead["sale_status"],
                "contractor": lead["contractor"],
                "offered_date": lead["offered_date"],
                "invoice_number": lead["invoice_number"],
                "export_reason": lead["export_reason"],
                "image_path": image_path,
                "quote_range": lead["quote_range"],
            }
        )
    return rows


def mailout_email_body(rows: list[dict[str, object]]) -> str:
    """Text a roofer can read in an email. Links open while this dashboard is running."""
    lines = [
        "Roof mailout.",
        "Each image link opens the annotated roof while the dashboard is running on this computer.",
        "If a link does not open, use the file path on this computer.",
        "",
    ]
    if not rows:
        lines.append("No leads are in this mailout.")
        return "\n".join(lines)
    for row in rows:
        repair = row["repair_likelihood"]
        repair_text = "not scored" if repair in {None, ""} else str(repair)
        lines.append(
            f"{row['reference']} — {row['address'].split(' — ', 1)[-1]} — wear {row['wear_score']} — repair likelihood {repair_text}"
        )
        if row["image_link"]:
            lines.append(f"Image: {row['image_link']}")
        else:
            lines.append("Image: no roof image saved yet")
        if row["image_path"]:
            lines.append(f"File: {row['image_path']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _parse_reference_list(value: object) -> list[str]:
    if isinstance(value, list):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"[\s,;]+", str(value or ""))
    found: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = part.strip().upper()
        if not token or token in seen:
            continue
        seen.add(token)
        found.append(token)
    return found


def _reject_unofferable(leads: list[dict[str, object]]) -> None:
    blocked = [lead for lead in leads if str(lead.get("sale_status") or "") in UNOFFERABLE]
    if not blocked:
        return
    names = ", ".join(
        f"{lead['reference']} ({SALE_LABELS.get(str(lead['sale_status']), lead['sale_status'])})"
        for lead in blocked
    )
    raise ValueError(
        f"These leads cannot be offered because they are already offered, reserved, or sold: {names}."
    )


def choose_mailout_leads(
    leads: list[dict[str, object]],
    references: list[str],
    selected_ids: list[str],
) -> tuple[list[dict[str, object]], list[str], str]:
    """Full mailout, pasted references, or Send checkboxes."""
    if references:
        by_reference = {str(lead["reference"]).upper(): lead for lead in leads}
        chosen: list[dict[str, object]] = []
        missing: list[str] = []
        for reference in references:
            lead = by_reference.get(reference)
            if lead is None:
                missing.append(reference)
            else:
                chosen.append(lead)
        return chosen, missing, "references"
    if selected_ids:
        wanted = {lead_id.strip() for lead_id in selected_ids if lead_id.strip()}
        chosen = [lead for lead in leads if str(lead["lead_id"]) in wanted]
        return chosen, [], "selected"
    return [lead for lead in leads if lead.get("export_included")], [], "threshold"


def write_mailout_export(rows: list[dict[str, object]]) -> Path:
    MAILOUT_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = MAILOUT_EXPORT_DIR / f"mailout_{stamp}.csv"
    if path.exists():
        path = MAILOUT_EXPORT_DIR / f"mailout_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAILOUT_EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _smtp_ready() -> bool:
    return bool(env("SMTP_HOST") and (env("SMTP_FROM") or env("SMTP_USER")))


def _email_mailout(recipient: str, path: Path, body: str) -> str:
    host = env("SMTP_HOST")
    sender = env("SMTP_FROM") or env("SMTP_USER")
    if not host or not sender:
        return "CSV ready – email sending not configured. Download and send manually."
    message = EmailMessage()
    message["Subject"] = "Roof mailout"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body + f"\n\nThe mailout CSV is attached ({path.name}).")
    message.add_attachment(
        path.read_bytes(),
        maintype="text",
        subtype="csv",
        filename=path.name,
    )
    try:
        with smtplib.SMTP(host, int(env("SMTP_PORT") or "587"), timeout=30) as client:
            client.starttls()
            user = env("SMTP_USER")
            password = env("SMTP_PASSWORD")
            if user:
                client.login(user, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        return f"CSV ready. Email to {recipient} failed: {exc}"
    return f"Mailout emailed to {recipient}."


@app.get("/")
def home():
    leads, mailout_path, quotes_path = load_leads()
    return render_template(
        "dashboard.html",
        leads=leads,
        statuses=STATUSES,
        owners=OWNERS,
        mailout_name=mailout_path.name if mailout_path else "none",
        quotes_name=quotes_path.name,
        postcodes=sorted({str(lead["postcode"]) for lead in leads if lead.get("postcode")}),
        wear_threshold=load_export_threshold(),
        repairable_wear=PASS2_WEAR_MIN,
        roof_materials=STORED_MATERIALS,
        roofer_outcomes=ROOFER_OUTCOMES,
        sale_statuses=SALE_STATUSES,
        marketplace_config=market.load_config(),
        live_listings=market.live_rows(leads),
        archive_listings=market.archive_rows(leads),
    )


CONTRACTOR_FIELDS = (
    "reference",
    "address",
    "suburb",
    "state",
    "postcode",
    "roof_material",
    "wear_score",
    "repair_likelihood",
    "cleaning_score",
    "primary_cleaning_defect",
    "needs_cleaning",
    "image_link",
    "lead_id",
)


@app.post("/export/contractor")
def export_contractor():
    """CSV of the repair list or the cleaning list, using the on-screen filters."""
    kind = (request.form.get("kind") or "").strip()
    if kind not in {"repair", "cleaning"}:
        flash("Choose Export Repair Leads or Export Cleaning Leads.")
        return redirect(url_for("home"))
    try:
        repair_threshold = int(request.form.get("repair_threshold") or 3)
        cleaning_threshold = int(request.form.get("cleaning_threshold") or 3)
    except ValueError:
        flash("Thresholds must be whole numbers from 1 to 5.")
        return redirect(url_for("home"))
    repair_threshold = min(5, max(1, repair_threshold))
    cleaning_threshold = min(5, max(1, cleaning_threshold))
    match_mode = request.form.get("match") or "either"
    if match_mode not in {"repair", "cleaning", "either"}:
        match_mode = "either"
    materials = {item for item in request.form.getlist("material") if item in STORED_MATERIALS}
    leads, _, _ = load_leads()
    matcher = export_repair_matches if kind == "repair" else export_cleaning_matches
    chosen = [
        lead
        for lead in leads
        if matcher(
            wear=int(lead["wear_score"]),
            repair=None if lead["repair_likelihood"] == "" else int(lead["repair_likelihood"]),
            cleaning=None if lead["cleaning_score"] == "" else int(lead["cleaning_score"]),
            material=str(lead["material_code"]),
            repair_threshold=repair_threshold,
            cleaning_threshold=cleaning_threshold,
            match_mode=match_mode,
            materials=materials,
        )
    ]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CONTRACTOR_FIELDS)
    writer.writeheader()
    for lead in chosen:
        image_link = ""
        if lead.get("has_image"):
            image_link = request.host_url.rstrip("/") + url_for("roof_image", lead_id=lead["lead_id"])
        writer.writerow(
            {
                "reference": lead.get("reference") or lead["lead_id"],
                "address": lead["address"],
                "suburb": lead["suburb"],
                "state": lead["state"],
                "postcode": lead["postcode"],
                "roof_material": lead["material_code"],
                "wear_score": lead["wear_score"],
                "repair_likelihood": lead["repair_likelihood"],
                "cleaning_score": lead["cleaning_score"],
                "primary_cleaning_defect": lead["cleaning_defect_code"],
                "needs_cleaning": "true" if lead["needs_cleaning"] else "false",
                "image_link": image_link,
                "lead_id": lead["lead_id"],
            }
        )
    filename = "repair-leads.csv" if kind == "repair" else "cleaning-leads.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/load-gnaf")
def load_gnaf_route():
    postcode = (request.form.get("postcode") or "").strip()
    if not postcode.isdigit() or len(postcode) != 4:
        flash("Enter a four-digit postcode.")
        return redirect(url_for("home"))
    output = DATA_DIR / f"addresses_{postcode}.csv"
    try:
        count = load_postcode(postcode, output)
    except (OSError, ValueError) as exc:
        flash(str(exc))
        return redirect(url_for("home"))
    flash(f"Loaded {count} G-NAF address(es) to {output.name}. No tiles were downloaded.")
    return redirect(url_for("home"))


@app.post("/scan/preview")
def scan_preview():
    try:
        postcode, entire, maximum, skip_apartments = _scan_request()
        _chosen, _locked, message = plan_scan(
            postcode,
            entire=entire,
            maximum=maximum,
            skip_apartments=skip_apartments,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return jsonify(ok=False, message=str(exc))
    return jsonify(ok=True, message=message)


@app.post("/scan")
def scan():
    if request.form.get("confirmed") != "yes":
        flash("Confirm the number of addresses before the scan starts.")
        return redirect(url_for("home"))
    try:
        postcode, entire, maximum, skip_apartments = _scan_request()
        chosen, _locked, message = plan_scan(
            postcode,
            entire=entire,
            maximum=maximum,
            skip_apartments=skip_apartments,
        )
        if not chosen:
            flash(message)
            return redirect(url_for("home"))
        flash(f"{message} {run_postcode_scan(chosen)}")
    except (OSError, ValueError, RuntimeError) as exc:
        flash(str(exc))
    return redirect(url_for("home"))


@app.post("/manual/save")
def save_manual():
    payload = request.get_json(silent=True) or {}
    items = payload.get("leads")
    if not isinstance(items, list):
        return jsonify(ok=False, message="No lead changes were sent."), 400
    try:
        threshold_note = ""
        if "threshold" in payload and str(payload.get("threshold") or "").strip():
            threshold = _parse_threshold(payload.get("threshold"))
            if threshold != load_export_threshold():
                save_export_threshold(threshold)
                threshold_note = " Export threshold saved."
        saved, high_res_note = save_manual_batch(items)
    except (OSError, ValueError, RuntimeError) as exc:
        return jsonify(ok=False, message=str(exc)), 400
    noun = "lead" if saved == 1 else "leads"
    message = f"Saved {saved} {noun}.{threshold_note}"
    if high_res_note:
        message = f"{message} {high_res_note}"
    return jsonify(ok=True, saved=saved, message=message, high_res_note=high_res_note)


@app.post("/mailout/prepare")
def prepare_mailout():
    payload = request.get_json(silent=True) or {}
    items = payload.get("leads")
    if not isinstance(items, list):
        return jsonify(ok=False, message="No leads were sent."), 400
    send = bool(payload.get("send"))
    recipient = str(payload.get("email") or "").strip()
    try:
        threshold = _parse_threshold(payload.get("threshold"))
        save_export_threshold(threshold)
        save_manual_batch(items)
        leads, _mailout, _quotes = load_leads()
        references = _parse_reference_list(payload.get("references"))
        selected = payload.get("selected") if isinstance(payload.get("selected"), list) else []
        chosen, missing, mode = choose_mailout_leads(leads, references, [str(item) for item in selected])
        _reject_unofferable(chosen)
        rows = _mailout_rows(chosen)
        path = write_mailout_export(rows)
        preview = mailout_email_body(rows)
    except (OSError, ValueError, RuntimeError) as exc:
        return jsonify(ok=False, message=str(exc)), 400
    relative = path.relative_to(ROOT).as_posix()
    if mode == "references":
        message = f"CSV ready with {len(rows)} selected references: {relative}."
    elif mode == "selected":
        message = f"CSV ready with {len(rows)} selected leads: {relative}."
    else:
        message = f"CSV ready with {len(rows)} leads: {relative}."
    if missing:
        message += " Not found: " + ", ".join(missing) + "."
    emailed = False
    if send:
        if not recipient:
            message += " Enter an email address to send the mailout."
        elif not _smtp_ready():
            message += " CSV ready – email sending not configured. Download and send manually."
        else:
            message += " " + _email_mailout(recipient, path, preview)
            emailed = "emailed" in message
    elif not _smtp_ready():
        message += " CSV ready – email sending not configured. Download and send manually."
    return jsonify(
        ok=True,
        count=len(rows),
        path=relative,
        download=url_for("download_mailout", filename=path.name),
        message=message,
        preview=preview,
        emailed=emailed,
    )


@app.get("/mailout/file/<filename>")
def download_mailout(filename: str):
    if not _MAILOUT_FILE.match(filename):
        abort(404)
    path = MAILOUT_EXPORT_DIR / filename
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)


@app.post("/export")
def export_roofer():
    lead_ids = [lead_id for lead_id in request.form.getlist("lead_id") if lead_id.strip()]
    if not lead_ids:
        flash("Select at least one repairable roof.")
        return redirect(url_for("home"))
    leads, _mailout, _quotes = load_leads()
    by_id = {str(lead["lead_id"]): lead for lead in leads}
    try:
        _reject_unofferable([by_id[lead_id] for lead_id in lead_ids if lead_id in by_id])
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("home"))
    images: list[tuple[str, bytes]] = []
    for lead_id in lead_ids:
        lead = by_id.get(lead_id)
        if lead is None or int(lead["wear_score"]) < 3:
            continue
        image = _roofer_image(lead_id, str(lead["suburb"]))
        if image is None:
            continue
        suburb_slug = _file_slug(str(lead["suburb"]))
        images.append((f"{suburb_slug}_{safe_lead_id(lead_id)}.png", image))
    if not images:
        flash("None of the selected leads has an image yet.")
        return redirect(url_for("home"))
    email_to = (request.form.get("email") or "").strip()
    note = _email_zip(email_to, _zip_bytes(images)) if email_to else ""
    files = list(images)
    if note:
        files.append(("email_note.txt", (note + "\n").encode("utf-8")))
    archive = BytesIO(_zip_bytes(files))
    archive.seek(0)
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name="roofer_roofs.zip",
    )


@app.post("/leads/<lead_id>/high-res")
def toggle_high_res(lead_id: str):
    paths = _files_for_lead(lead_id)
    if not paths:
        flash(f"{lead_id} is not in the current quote or mail-out file.")
        return redirect(url_for("home"))
    flagged = any(
        parse_high_res(row.get("high_res_requested"))
        for path in paths
        for row in (_lead_index(_read_csv(path)).get(lead_id),)
        if row
    )
    for path in paths:
        set_high_res(path, lead_id, not flagged)
    flash(f"{lead_id} high-res flag is now {'on' if not flagged else 'off'}.")
    return redirect(url_for("home"))


@app.post("/leads/<lead_id>/owner")
def update_owner(lead_id: str):
    occupancy = (request.form.get("owner") or "").strip()
    try:
        save_owner(lead_id, occupancy)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("home"))
    flash(f"{lead_id} occupancy saved.")
    return redirect(url_for("home"))


@app.post("/leads/<lead_id>/status")
def update_status(lead_id: str):
    status = (request.form.get("status") or "").strip()
    try:
        save_status(lead_id, status)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("home"))
    flash(f"{lead_id} status saved.")
    return redirect(url_for("home"))


@app.post("/clusters")
def open_clusters():
    """Open the clusters folder in File Explorer on this computer."""
    ensure_data_dirs()
    CLUSTERS_DIR.mkdir(parents=True, exist_ok=True)
    os.startfile(CLUSTERS_DIR)
    return redirect(url_for("home"))


@app.get("/roof/<lead_id>")
def roof_image(lead_id: str):
    path = roof_image_file(lead_id)
    if path is None:
        abort(404)
    return send_file(path, mimetype="image/png")


@app.get("/highres/<lead_id>")
def highres_image(lead_id: str):
    path = TILES_DIR / f"{safe_lead_id(lead_id)}_highres.png"
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="image/png")


@app.get("/highres-thumb/<lead_id>")
def highres_thumbnail(lead_id: str):
    path = TILES_DIR / f"{safe_lead_id(lead_id)}_highres.png"
    if not path.is_file():
        abort(404)
    with Image.open(path) as image:
        copy = image.convert("RGB")
        copy.thumbnail((240, 180))
        buffer = BytesIO()
        copy.save(buffer, format="PNG")
        buffer.seek(0)
    return send_file(buffer, mimetype="image/png")


@app.get("/enhanced/<lead_id>")
def enhanced_image(lead_id: str):
    return send_file(_enhanced_path(lead_id), mimetype="image/png")


@app.get("/thumb/<lead_id>")
def thumbnail(lead_id: str):
    # The table thumb is the annotated (or original) tile, not the upscaled copy.
    path = roof_image_file(lead_id)
    if path is None:
        abort(404)
    with Image.open(path) as image:
        copy = image.convert("RGB")
        copy.thumbnail((240, 180))
        buffer = BytesIO()
        copy.save(buffer, format="PNG")
        buffer.seek(0)
    return send_file(buffer, mimetype="image/png")


def _marketplace_leads() -> list[dict]:
    leads, _, _ = load_leads()
    return leads


def _optional_price(field: str) -> int | None:
    raw = (request.form.get(field) or "").strip()
    if not raw:
        return None
    return int(raw)


def _checkout_redirect(cards: list[dict]):
    query = urlencode([("lead_id", card["lead_id"]) for card in cards])
    target = url_for("marketplace_checkout")
    return redirect(f"{target}?{query}" if query else target)


@app.get("/marketplace")
def marketplace_home():
    """Public catalogue. Street addresses are not on this page."""
    config = market.load_config()
    return render_template(
        "marketplace.html",
        cards=market.public_cards(_marketplace_leads()),
        minimum_order=config["minimum_order"],
    )


@app.get("/marketplace/cart")
def marketplace_cart():
    config = market.load_config()
    return render_template(
        "marketplace_cart.html",
        cards=market.public_cards(_marketplace_leads()),
        minimum_order=config["minimum_order"],
    )


@app.route("/marketplace/checkout", methods=["GET", "POST"])
def marketplace_checkout():
    """Cart review and payment. The pay button stays off under the minimum."""
    config = market.load_config()
    lead_ids = request.form.getlist("lead_id") or request.args.getlist("lead_id")
    cards = market.cards_for_checkout(_marketplace_leads(), lead_ids)
    shortfall = market.order_shortfall(len(cards))
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if shortfall:
            flash(f"Minimum order is {config['minimum_order']} leads.")
            return _checkout_redirect(cards)
        if not email or "@" not in email:
            flash("Enter an email address so the lead details can be sent after payment.")
            return _checkout_redirect(cards)
        order = market.create_order(email, cards)
        return _begin_payment(order, cards)
    total = sum(int(card["price_ex_gst"]) for card in cards)
    return render_template(
        "marketplace_checkout.html",
        cards=cards,
        minimum_order=config["minimum_order"],
        shortfall=shortfall,
        total_label=market.format_price(total),
        stripe_ready=bool(env("STRIPE_SECRET_KEY")),
    )


@app.get("/marketplace/success")
def marketplace_success():
    """Confirm a Stripe session, or a local test payment, then email the addresses."""
    order = None
    session_id = (request.args.get("session_id") or "").strip()
    if session_id and session_id != "{CHECKOUT_SESSION_ID}":
        order = market.order_by_session(session_id)
        paid = _stripe_session_paid(session_id)
        if order and paid:
            order = market.mark_paid(order["order_id"])
    else:
        order = market.get_order((request.args.get("order_id") or "").strip())
    if not order or order.get("status") != "paid":
        flash("Payment is not complete.")
        return redirect(url_for("marketplace_home"))
    details = market.purchased_details(_marketplace_leads(), order)
    message = "A copy of these addresses has been sent to your email."
    if not order.get("emailed"):
        message = _email_purchase(order, details)
        if message.startswith("Lead details emailed"):
            market.mark_emailed(order["order_id"])
    return render_template("marketplace_success.html", details=details, message=message)


@app.post("/marketplace/settings")
def marketplace_settings():
    repair = {str(score): request.form.get(f"repair_{score}") for score in (2, 3, 4, 5)}
    cleaning = {str(score): request.form.get(f"cleaning_{score}") for score in (2, 3, 4, 5)}
    market.save_config(
        {
            "listing_days": request.form.get("listing_days"),
            "minimum_order": request.form.get("minimum_order"),
            "repair_prices_ex_gst": repair,
            "cleaning_prices_ex_gst": cleaning,
        }
    )
    flash("Marketplace prices, listing length, and minimum order saved. Amounts are exclusive of GST.")
    return redirect(url_for("home"))


@app.post("/marketplace/publish")
def marketplace_publish():
    category = (request.form.get("category") or "").strip()
    try:
        price = _optional_price("price_override")
        count = market.publish(_marketplace_leads(), request.form.getlist("lead_id"), category, price)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("home"))
    flash(f"Published {count} lead{'s' if count != 1 else ''}. Prices are exclusive of GST.")
    return redirect(url_for("home"))


@app.post("/marketplace/relist")
def marketplace_relist():
    try:
        price = _optional_price("price_override")
        count = market.relist(request.form.getlist("lead_id"), price)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("home"))
    flash(f"Re-listed {count} lead{'s' if count != 1 else ''}. The listing timer has restarted.")
    return redirect(url_for("home"))


@app.post("/marketplace/price")
def marketplace_price():
    lead_id = safe_lead_id(request.form.get("lead_id") or "")
    try:
        market.set_price(lead_id, int(request.form.get("price_ex_gst") or ""))
    except (TypeError, ValueError) as exc:
        flash(str(exc) or "Enter a price exclusive of GST.")
        return redirect(url_for("home"))
    flash(f"Price for {lead_id} is now {market.format_price(int(request.form.get('price_ex_gst')))}.")
    return redirect(url_for("home"))


def _begin_payment(order: dict, cards: list[dict]):
    """Open Stripe Checkout, or record a local test payment when no secret key is set."""
    secret = env("STRIPE_SECRET_KEY")
    if not secret:
        market.mark_paid(order["order_id"])
        return redirect(url_for("marketplace_success", order_id=order["order_id"]))
    success_url = url_for("marketplace_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = url_for("marketplace_home", _external=True)
    try:
        session_id, checkout_url = _stripe_checkout(secret, order, cards, success_url, cancel_url)
    except Exception as exc:
        logger.warning("Stripe checkout failed: %s", exc)
        flash("Stripe could not start the payment. Check STRIPE_SECRET_KEY and try again.")
        return _checkout_redirect(cards)
    market.attach_session(order["order_id"], session_id)
    return redirect(checkout_url)


def _stripe_checkout(secret: str, order: dict, cards: list[dict], success_url: str, cancel_url: str) -> tuple[str, str]:
    """Create a one-off Stripe Checkout session. Amounts are cents, exclusive of GST."""
    import httpx

    data = [
        ("mode", "payment"),
        ("success_url", success_url),
        ("cancel_url", cancel_url),
        ("customer_email", order["email"]),
        ("metadata[order_id]", order["order_id"]),
    ]
    for index, card in enumerate(cards):
        prefix = f"line_items[{index}]"
        name = f"{card['category_label']} lead in {card['suburb']} (ex GST)"
        data.extend(
            [
                (f"{prefix}[quantity]", "1"),
                (f"{prefix}[price_data][currency]", "aud"),
                (f"{prefix}[price_data][unit_amount]", str(int(card["price_ex_gst"]) * 100)),
                (f"{prefix}[price_data][product_data][name]", name),
                (
                    f"{prefix}[price_data][product_data][description]",
                    "Price exclusive of GST. Satellite imagery and roof condition are not guaranteed.",
                ),
            ]
        )
    response = httpx.post(
        "https://api.stripe.com/v1/checkout/sessions",
        auth=(secret, ""),
        data=data,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return str(payload["id"]), str(payload["url"])


def _stripe_session_paid(session_id: str) -> bool:
    secret = env("STRIPE_SECRET_KEY")
    if not secret:
        return False
    import httpx

    response = httpx.get(
        f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
        auth=(secret, ""),
        timeout=30,
    )
    if response.status_code >= 400:
        return False
    return response.json().get("payment_status") == "paid"


def _email_purchase(order: dict, details: list[dict]) -> str:
    """Send the street addresses after payment. Prices in the letter are exclusive of GST."""
    host = env("SMTP_HOST")
    sender = env("SMTP_FROM") or env("SMTP_USER")
    lines = [
        f"{item['address']}, {item['suburb']} {item['postcode']} — {item['price_label']} ({item['category']}, wear {item['wear_score']})"
        for item in details
    ]
    body = (
        "Your roof leads are below. Every price is exclusive of GST.\n\n"
        + "\n".join(lines)
        + f"\n\nTotal {market.format_price(order['amount_ex_gst'])}. GST is not included.\n\n"
        "There are no warranties or guarantees as to the accuracy of the images or the condition of the roofs. "
        "Images are third-party satellite imagery and we cannot guarantee the age, currency, or accuracy of any image. "
        "Leads are provided for information purposes only and buyers should conduct their own inspections.\n"
    )
    if not host or not sender:
        return "Payment recorded. Email is not configured, so the addresses are shown on this page."
    message = EmailMessage()
    message["Subject"] = "Your roof leads (prices exclusive of GST)"
    message["From"] = sender
    message["To"] = order["email"]
    message.set_content(body)
    try:
        with smtplib.SMTP(host, int(env("SMTP_PORT") or "587"), timeout=30) as client:
            client.starttls()
            user = env("SMTP_USER")
            password = env("SMTP_PASSWORD")
            if user:
                client.login(user, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        return f"Payment recorded. The email could not be sent ({exc}). The addresses are shown on this page."
    return f"Lead details emailed to {order['email']}."


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ensure_data_dirs()
    host = env("SCANNER_HOST") or "127.0.0.1"
    port = int(env("SCANNER_PORT") or "5000")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
