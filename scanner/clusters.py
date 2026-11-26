"""Group graded leads into short walking clusters.

Repair/wear and cleaning are separate. A cluster stays in one suburb, within
a few hundred metres, prefers the same street, and stops at 12 properties.
Each folder is named with the suburb abbreviation, a sequence number, and
the work type, for example BRO-01-repair or BRO-01-cleaning.
"""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
from pathlib import Path

from scanner.config import ANNOTATED_DIR, CLUSTERS_DIR, DATA_DIR, GRADES_DIR
from scanner.lead_match import NEEDS_CLEANING_MIN

NEAR_M = 400
MAX_SPAN_M = 500
MAX_SIZE = 12
EARTH_M = 6_371_000
_CLUSTER_KINDS = ("repair", "cleaning")


def write_cleared_clusters() -> list[Path]:
    """Rebuild repair/wear folders and cleaning folders from saved grades."""
    leads = load_grade_leads()
    folders: list[Path] = []
    folders.extend(write_clusters([lead for lead in leads if lead.get("repair_work")], kind="repair"))
    folders.extend(write_clusters([lead for lead in leads if lead.get("cleaning_work")], kind="cleaning"))
    return folders


def write_clusters(leads: list[dict[str, object]], *, kind: str) -> list[Path]:
    """Write one folder per suburb cluster. Earlier folders of this kind are replaced."""
    if kind not in _CLUSTER_KINDS:
        raise ValueError(f"Cluster kind must be repair or cleaning, not {kind}.")
    CLUSTERS_DIR.mkdir(parents=True, exist_ok=True)
    _clear_cluster_folders(kind)
    by_suburb: dict[str, list[dict[str, object]]] = {}
    for lead in leads:
        by_suburb.setdefault(str(lead.get("suburb") or ""), []).append(lead)
    folders: list[Path] = []
    sequence: dict[str, int] = {}
    for suburb in sorted(by_suburb, key=lambda name: (suburb_prefix(name), name.lower())):
        prefix = suburb_prefix(suburb)
        for group in build_clusters(by_suburb[suburb]):
            sequence[prefix] = sequence.get(prefix, 0) + 1
            label = f"{prefix}-{sequence[prefix]:02d}-{kind}"
            ordered = walking_order(group)
            folder = CLUSTERS_DIR / label
            folder.mkdir(parents=True, exist_ok=True)
            _write_summary(folder / "summary.csv", ordered, label=label, kind=kind)
            for place, lead in enumerate(ordered, start=1):
                source = ANNOTATED_DIR / f"{lead['lead_id']}.png"
                if source.is_file():
                    shutil.copy2(source, folder / f"{place:02d}-{_file_slug(str(lead['street']))}.png")
            folders.append(folder)
    return folders


def suburb_prefix(suburb: str) -> str:
    """Same short suburb code used on lead references, such as BRO or ENG."""
    words = [
        word
        for word in re.findall(r"[A-Za-z]+", suburb or "")
        if word.lower() not in {"the", "of"}
    ]
    if not words:
        return "RF"
    if len(words) == 1:
        prefix = words[0][:3].upper()
    else:
        prefix = "".join(word[0] for word in words[:4]).upper()
    prefix = re.sub(r"[^A-Z]", "", prefix)[:4]
    return prefix or "RF"


def _clear_cluster_folders(kind: str) -> None:
    """Remove this work type, plus older folders that were not split by work type."""
    for path in CLUSTERS_DIR.iterdir():
        if not path.is_dir():
            continue
        name = path.name
        if name.endswith(f"-{kind}") or name.startswith("cleared-") or "-repair" not in name and "-cleaning" not in name:
            shutil.rmtree(path)


def load_grade_leads(*, outcome: str | None = None, postcode: str | None = None) -> list[dict[str, object]]:
    """Leads with a saved grade and coordinates. Outcome and postcode are optional filters."""
    located = _coordinates_by_lead()
    leads: list[dict[str, object]] = []
    for path in GRADES_DIR.glob("*.json"):
        grade = json.loads(path.read_text(encoding="utf-8"))
        if outcome and grade.get("outcome") != outcome:
            continue
        lead_id = str(grade.get("lead_id") or path.stem)
        mailout = grade.get("mailout") if isinstance(grade.get("mailout"), dict) else {}
        address = str(grade.get("address") or "")
        street = str(mailout.get("street_address") or address.split(",", 1)[0]).strip()
        suburb = str(mailout.get("suburb") or _suburb(address)).strip()
        code = str(mailout.get("postcode") or _postcode(address)).strip()
        if postcode and code != postcode:
            continue
        point = located.get(lead_id)
        lat = _float(mailout.get("lat")) if mailout else None
        lon = _float(mailout.get("lon")) if mailout else None
        if lat is None or lon is None:
            if point is None:
                continue
            lat, lon = point
        pass1 = grade.get("pass1") if isinstance(grade.get("pass1"), dict) else {}
        cleaning = _score(pass1.get("cleaning_score"))
        cleaning_defect = str(pass1.get("primary_cleaning_defect") or "")
        graded_outcome = str(grade.get("outcome") or "")
        leads.append(
            {
                "lead_id": lead_id,
                "street": street,
                "suburb": suburb,
                "postcode": code,
                "lat": lat,
                "lon": lon,
                "repair_work": graded_outcome == "cleared",
                "cleaning_work": (
                    cleaning is not None
                    and cleaning >= NEEDS_CLEANING_MIN
                    and cleaning_defect not in {"", "none"}
                ),
            }
        )
    return leads


def build_clusters(leads: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    """Tight groups. A property joins only when it is within 400 m and the group still fits in 500 m."""
    unused = list(leads)
    clusters: list[list[dict[str, object]]] = []
    while unused:
        seed = max(unused, key=lambda lead: _nearby_count(lead, unused))
        unused.remove(seed)
        group = [seed]
        while len(group) < MAX_SIZE:
            choices: list[tuple[int, float, dict[str, object]]] = []
            for lead in unused:
                nearest = min(_metres(lead, member) for member in group)
                span = max(_metres(lead, member) for member in group)
                if nearest > NEAR_M or span > MAX_SPAN_M:
                    continue
                same_street = int(not any(_street_name(lead) == _street_name(member) for member in group))
                choices.append((same_street, nearest, lead))
            if not choices:
                break
            choices.sort(key=lambda item: (item[0], item[1]))
            chosen = choices[0][2]
            group.append(chosen)
            unused.remove(chosen)
        clusters.append(walking_order(group))
    return clusters


def walking_order(group: list[dict[str, object]]) -> list[dict[str, object]]:
    """Same-street groups follow the house numbers. Mixed streets follow the nearest next house."""
    if len({_street_name(lead) for lead in group}) == 1:
        return sorted(group, key=lambda lead: (_house_number(str(lead["street"])), str(lead["street"])))
    remaining = list(group)
    start = min(remaining, key=lambda lead: (float(lead["lat"]), float(lead["lon"])))
    ordered = [start]
    remaining.remove(start)
    while remaining:
        last = ordered[-1]
        nxt = min(remaining, key=lambda lead: _metres(last, lead))
        ordered.append(nxt)
        remaining.remove(nxt)
    return ordered


def _write_summary(path: Path, ordered: list[dict[str, object]], *, label: str, kind: str) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "cluster",
                "work",
                "order",
                "lead_id",
                "street_address",
                "suburb",
                "postcode",
                "lat",
                "lon",
                "metres_from_previous",
            ),
        )
        writer.writeheader()
        previous: dict[str, object] | None = None
        for place, lead in enumerate(ordered, start=1):
            gap = "" if previous is None else str(round(_metres(previous, lead)))
            writer.writerow(
                {
                    "cluster": label,
                    "work": kind,
                    "order": place,
                    "lead_id": lead["lead_id"],
                    "street_address": lead["street"],
                    "suburb": lead["suburb"],
                    "postcode": lead["postcode"],
                    "lat": f"{float(lead['lat']):.6f}",
                    "lon": f"{float(lead['lon']):.6f}",
                    "metres_from_previous": gap,
                }
            )
            previous = lead


def _nearby_count(lead: dict[str, object], pool: list[dict[str, object]]) -> int:
    return sum(1 for other in pool if other is not lead and _metres(lead, other) <= NEAR_M)


def _metres(left: dict[str, object], right: dict[str, object]) -> float:
    lat1 = math.radians(float(left["lat"]))
    lat2 = math.radians(float(right["lat"]))
    dlat = lat2 - lat1
    dlon = math.radians(float(right["lon"]) - float(left["lon"]))
    arc = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(arc))


def _street_name(lead: dict[str, object]) -> str:
    parts = str(lead["street"]).split(" ", 1)
    return parts[1].lower() if len(parts) == 2 else str(lead["street"]).lower()


def _house_number(street: str) -> int:
    digits = ""
    for character in street.split(" ", 1)[0]:
        if character.isdigit():
            digits += character
        elif digits:
            break
    return int(digits) if digits else 0


def _coordinates_by_lead() -> dict[str, tuple[float, float]]:
    found: dict[str, tuple[float, float]] = {}
    for path in DATA_DIR.glob("addresses_*.csv"):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                lead_id = (row.get("lead_id") or "").strip()
                lat = _float(row.get("lat"))
                lon = _float(row.get("lon"))
                if lead_id and lat is not None and lon is not None:
                    found[lead_id] = (lat, lon)
    return found


def _suburb(address: str) -> str:
    parts = [part.strip() for part in address.split(",")]
    if len(parts) < 2:
        return ""
    return parts[1].rsplit(" ", 2)[0]


def _postcode(address: str) -> str:
    token = address.strip().rsplit(" ", 1)[-1]
    return token if token.isdigit() else ""


def _score(*values: object) -> int | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 5:
            return number
    return None


def _float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _file_slug(street: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "-" for character in street)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "address"
