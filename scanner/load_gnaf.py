"""Write a scanner address CSV from the local G-NAF extract.

No network calls. The extract is read from data/gnaf. Roof area is a
placeholder because G-NAF does not publish roof size.

    python -m scanner.load_gnaf --postcode 4218 --output data/addresses_4218.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from collections import Counter
from pathlib import Path

if __name__ == "__main__" and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.addresses import REQUIRED_COLUMNS, large_lot_lead_ids, safe_lead_id
from scanner.config import DATA_DIR

logger = logging.getLogger(__name__)

GNAF_DIR = DATA_DIR / "gnaf"
# G-NAF has no roof area. The scanner requires a positive number, so every
# loaded lead uses this placeholder until a measured area replaces it.
PLACEHOLDER_ROOF_AREA_M2 = 200

# Australia Post ranges, used only to open the matching state file first.
_STATE_RANGES = (
    ("NT", 800, 899),
    ("NSW", 1000, 2599),
    ("ACT", 2600, 2618),
    ("NSW", 2619, 2899),
    ("ACT", 2900, 2920),
    ("NSW", 2921, 2999),
    ("VIC", 3000, 3999),
    ("QLD", 4000, 4999),
    ("SA", 5000, 5999),
    ("WA", 6000, 6999),
    ("TAS", 7000, 7999),
    ("VIC", 8000, 8999),
    ("QLD", 9000, 9999),
)


def find_standard_dir(gnaf_dir: Path = GNAF_DIR) -> Path:
    if not gnaf_dir.is_dir():
        raise FileNotFoundError(f"G-NAF folder not found: {gnaf_dir}")
    matches = [path for path in gnaf_dir.rglob("Standard") if path.is_dir()]
    if not matches:
        raise FileNotFoundError(f"No G-NAF Standard folder under {gnaf_dir}")
    return sorted(matches)[-1]


def load_postcode(postcode: str, output: Path, *, gnaf_dir: Path = GNAF_DIR) -> int:
    """Write principal street addresses for one postcode. Returns the row count."""
    postcode = postcode.strip()
    if not postcode.isdigit() or len(postcode) != 4:
        raise ValueError("Postcode must be four digits.")
    standard = find_standard_dir(gnaf_dir)
    street_types = _pipe_lookup(standard.parent / "Authority Code" / "Authority_Code_STREET_TYPE_AUT_psv.psv", "CODE")
    street_suffixes = _pipe_lookup(
        standard.parent / "Authority Code" / "Authority_Code_STREET_SUFFIX_AUT_psv.psv",
        "CODE",
        value_column="DESCRIPTION",
    )
    states = _state_order(postcode, standard)
    rows: list[dict[str, str]] = []
    for state in states:
        rows.extend(_rows_for_state(standard, state, postcode, street_types, street_suffixes))
        if rows:
            break
    if not rows:
        raise ValueError(f"No principal street addresses for postcode {postcode} in {standard}.")
    rows.sort(key=lambda row: (row["suburb"], row["street_address"], row["lead_id"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*REQUIRED_COLUMNS, "geocode_type"])
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %s address(es) for %s to %s", len(rows), postcode, output)
    return len(rows)


def _rows_for_state(
    standard: Path,
    state: str,
    postcode: str,
    street_types: dict[str, str],
    street_suffixes: dict[str, str],
) -> list[dict[str, str]]:
    detail_path = standard / f"{state}_ADDRESS_DETAIL_psv.psv"
    geocode_path = standard / f"{state}_ADDRESS_DEFAULT_GEOCODE_psv.psv"
    street_path = standard / f"{state}_STREET_LOCALITY_psv.psv"
    locality_path = standard / f"{state}_LOCALITY_psv.psv"
    if not detail_path.is_file() or not geocode_path.is_file():
        return []
    localities = _localities(locality_path)
    streets = _streets(street_path)
    candidates: list[dict[str, str]] = []
    unit_counts: Counter[tuple[str, str, str, str]] = Counter()
    logger.info("Reading %s addresses for postcode %s", state, postcode)
    for record in _pipe_rows(detail_path):
        if record.get("DATE_RETIRED") or record.get("POSTCODE") != postcode:
            continue
        if not (record.get("NUMBER_FIRST") or "").strip():
            continue
        if _has_flat(record):
            unit_counts[_site_key(record)] += 1
        if (record.get("ALIAS_PRINCIPAL") or "").upper() != "P":
            continue
        if (record.get("PRIMARY_SECONDARY") or "").upper() == "S":
            continue
        candidates.append(record)
    chosen: dict[str, dict[str, str]] = {}
    skipped_reserve = 0
    skipped_strata = 0
    for record in candidates:
        if _reserve_number(record):
            skipped_reserve += 1
            continue
        if _strata_building(record, unit_counts):
            skipped_strata += 1
            continue
        street = streets.get(record.get("STREET_LOCALITY_PID") or "")
        locality = localities.get(record.get("LOCALITY_PID") or "")
        if street is None or locality is None:
            continue
        lead_id = safe_lead_id(record["ADDRESS_DETAIL_PID"])
        chosen[lead_id] = {
            "lead_id": lead_id,
            "street_address": _street_address(record, street, street_types, street_suffixes),
            "suburb": locality,
            "state": state,
            "postcode": postcode,
            "lat": "",
            "lon": "",
            "geocode_type": "",
            "roof_area_m2": str(PLACEHOLDER_ROOF_AREA_M2),
        }
    if not chosen:
        return []
    # Building and property centroids sit on the house. A street-locality point
    # often sits in the road or on a roundabout, so it is kept only when nothing
    # better was published for that address.
    for record in _geocode_records(standard, state):
        if record.get("DATE_RETIRED"):
            continue
        lead_id = safe_lead_id((record.get("ADDRESS_DETAIL_PID") or "").strip()) if (record.get("ADDRESS_DETAIL_PID") or "").strip() else ""
        row = chosen.get(lead_id)
        if row is None:
            continue
        lat = (record.get("LATITUDE") or "").strip()
        lon = (record.get("LONGITUDE") or "").strip()
        kind = (record.get("GEOCODE_TYPE_CODE") or "").strip().upper()
        if not lat or not lon:
            continue
        if not row["lat"] or _geocode_rank(kind) < _geocode_rank(row["geocode_type"]):
            row["lat"] = lat
            row["lon"] = lon
            row["geocode_type"] = kind
    rows = [row for row in chosen.values() if row["lat"] and row["lon"]]
    points: list[tuple[str, float, float]] = []
    for row in rows:
        try:
            points.append((row["lead_id"], float(row["lat"]), float(row["lon"])))
        except ValueError:
            continue
    isolated = large_lot_lead_ids(points)
    kept = [row for row in rows if row["lead_id"] not in isolated]
    logger.info(
        "Postcode %s %s: kept %s, skipped %s reserve numbers, %s strata buildings, %s large lots",
        postcode,
        state,
        len(kept),
        skipped_reserve,
        skipped_strata,
        len(rows) - len(kept),
    )
    return kept


_STRATA_NAME = re.compile(r"(?i)\bSP\s*\d+\b|\bstrata\b")
_STRATA_PARCEL = re.compile(r"(?i)(?:^|/)SP\s*\d+\b")


def _has_flat(record: dict[str, str]) -> bool:
    return bool((record.get("FLAT_NUMBER") or "").strip() or (record.get("FLAT_TYPE_CODE") or "").strip())


def _site_key(record: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        record.get("STREET_LOCALITY_PID") or "",
        (record.get("NUMBER_FIRST") or "").strip(),
        (record.get("NUMBER_LAST") or "").strip(),
        (record.get("NUMBER_FIRST_SUFFIX") or "").strip().upper(),
    )


def _reserve_number(record: dict[str, str]) -> bool:
    """G-NAF number suffix R marks a council reserve, such as 5R or 110R."""
    for column in ("NUMBER_FIRST_SUFFIX", "NUMBER_LAST_SUFFIX"):
        if (record.get(column) or "").strip().upper() == "R":
            return True
    return False


def _strata_building(record: dict[str, str], unit_counts: Counter[tuple[str, str, str, str]]) -> bool:
    """The building address of a strata plan, with no unit number of its own."""
    if _has_flat(record):
        return False
    name = record.get("BUILDING_NAME") or ""
    parcel = record.get("LEGAL_PARCEL_ID") or ""
    if _STRATA_NAME.search(name) or _STRATA_PARCEL.search(parcel):
        return True
    return unit_counts.get(_site_key(record), 0) >= 2


# Lower is better. Street-locality points are last.
_GEOCODE_RANK = {"BCM": 0, "BC": 1, "PCM": 2, "PC": 3, "FCS": 4, "GAP": 8, "STL": 9}


def _geocode_rank(kind: str) -> int:
    return _GEOCODE_RANK.get((kind or "").upper(), 5)


def _geocode_records(standard: Path, state: str):
    """Every published geocode for the state. The default file is the fallback."""
    names = (
        f"{state}_ADDRESS_GEOCODE_psv.psv",
        f"{state}_ADDRESS_SITE_GEOCODE_psv.psv",
        f"{state}_ADDRESS_DEFAULT_GEOCODE_psv.psv",
    )
    seen: set[Path] = set()
    for name in names:
        path = standard / name
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        yield from _pipe_rows(path)


# Words the dashboard uses to recognise an apartment or multi-unit address.
_FLAT_WORDS = {
    "UNIT": "Unit",
    "APT": "Apartment",
    "FLAT": "Flat",
    "SE": "Suite",
}
_LEVEL_WORDS = {
    "L": "Level",
    "FL": "Floor",
    "LVL": "Lvl",
}


def _street_address(
    record: dict[str, str],
    street: tuple[str, str, str],
    street_types: dict[str, str],
    street_suffixes: dict[str, str],
) -> str:
    name, type_code, suffix_code = street
    number = _number(
        record.get("NUMBER_FIRST_PREFIX", ""),
        record.get("NUMBER_FIRST", ""),
        record.get("NUMBER_FIRST_SUFFIX", ""),
    )
    last = _number(
        record.get("NUMBER_LAST_PREFIX", ""),
        record.get("NUMBER_LAST", ""),
        record.get("NUMBER_LAST_SUFFIX", ""),
    )
    if last:
        number = f"{number}-{last}"
    flat_number = _number(
        record.get("FLAT_NUMBER_PREFIX", ""),
        record.get("FLAT_NUMBER", ""),
        record.get("FLAT_NUMBER_SUFFIX", ""),
    )
    if flat_number and number:
        number = f"{flat_number}/{number}"
    elif flat_number:
        number = flat_number
    flat_word = _FLAT_WORDS.get((record.get("FLAT_TYPE_CODE") or "").strip().upper(), "")
    level_number = _number(
        record.get("LEVEL_NUMBER_PREFIX", ""),
        record.get("LEVEL_NUMBER", ""),
        record.get("LEVEL_NUMBER_SUFFIX", ""),
    )
    level_word = _LEVEL_WORDS.get((record.get("LEVEL_TYPE_CODE") or "").strip().upper(), "")
    level = " ".join(part for part in (level_word, level_number) if part)
    street_type = street_types.get(type_code, type_code.title())
    suffix = street_suffixes.get(suffix_code, "")
    return " ".join(part for part in (level, flat_word, number, name.title(), street_type, suffix) if part)


def _number(prefix: str, value: str, suffix: str) -> str:
    value = value.strip()
    if value.endswith(".0"):
        value = value[:-2]
    if not value:
        return ""
    return f"{prefix.strip()}{value}{suffix.strip()}"


def _localities(path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    if not path.is_file():
        return names
    for record in _pipe_rows(path):
        if record.get("DATE_RETIRED"):
            continue
        pid = record.get("LOCALITY_PID") or ""
        name = (record.get("LOCALITY_NAME") or "").strip()
        if pid and name:
            names[pid] = name.title()
    return names


def _streets(path: Path) -> dict[str, tuple[str, str, str]]:
    streets: dict[str, tuple[str, str, str]] = {}
    if not path.is_file():
        return streets
    for record in _pipe_rows(path):
        if record.get("DATE_RETIRED"):
            continue
        pid = record.get("STREET_LOCALITY_PID") or ""
        name = (record.get("STREET_NAME") or "").strip()
        if pid and name:
            streets[pid] = (
                name,
                (record.get("STREET_TYPE_CODE") or "").strip(),
                (record.get("STREET_SUFFIX_CODE") or "").strip(),
            )
    return streets


def _pipe_lookup(path: Path, key_column: str, value_column: str = "CODE") -> dict[str, str]:
    """Map an authority code to a display word. Street types use the code itself, title case."""
    lookup: dict[str, str] = {}
    if not path.is_file():
        return lookup
    for record in _pipe_rows(path):
        key = (record.get(key_column) or "").strip()
        if not key:
            continue
        if value_column == "CODE":
            lookup[key] = key.title()
        else:
            lookup[key] = (record.get(value_column) or key).title()
    return lookup


def _pipe_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle, delimiter="|")


def _state_order(postcode: str, standard: Path) -> list[str]:
    number = int(postcode)
    preferred = [state for state, low, high in _STATE_RANGES if low <= number <= high]
    present = []
    for path in sorted(standard.glob("*_ADDRESS_DETAIL_psv.psv")):
        state = path.name.split("_", 1)[0]
        if state not in present:
            present.append(state)
    ordered = [state for state in preferred if state in present]
    ordered.extend(state for state in present if state not in ordered)
    return ordered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load G-NAF street addresses for one postcode.")
    parser.add_argument("--postcode", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        count = load_postcode(args.postcode, args.output)
    except (OSError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    print(f"Wrote {count} address(es) to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
