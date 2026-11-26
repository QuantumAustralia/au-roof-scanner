"""Occupancy on cleared leads.

New leads are unknown. Edit the CSV, or run this module, to set a lead to
owner_occupier or rental. Automatic values will come from Geoscape or other
property data later; this module does not look them up.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger(__name__)

OCCUPANCY_TYPES = ("owner_occupier", "rental", "unknown")
DEFAULT_OCCUPANCY = "unknown"
DEFAULT_SOURCE = "default"
MANUAL_SOURCE = "manual"
OCCUPANCY_FIELDS = ("occupancy_type", "occupancy_source")


def occupancy_from_row(row: dict[str, object] | None) -> tuple[str, str] | None:
    """Read a row's occupancy, or None when both fields are blank or absent."""
    if not row:
        return None
    has_type = "occupancy_type" in row
    has_source = "occupancy_source" in row
    if not has_type and not has_source:
        return None
    raw_type = str(row.get("occupancy_type") or "").strip().lower()
    raw_source = str(row.get("occupancy_source") or "").strip().lower()
    if not raw_type and not raw_source:
        return None
    if not raw_type:
        raw_type = DEFAULT_OCCUPANCY
    if raw_type not in OCCUPANCY_TYPES:
        allowed = ", ".join(OCCUPANCY_TYPES)
        raise ValueError(f"occupancy_type must be one of: {allowed}. Got {raw_type!r}.")
    if not raw_source:
        raw_source = DEFAULT_SOURCE if raw_type == DEFAULT_OCCUPANCY else MANUAL_SOURCE
    return raw_type, raw_source


def resolve_occupancy(
    row: dict[str, object],
    previous: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Choose the occupancy to keep.

    A fresh unknown/default does not replace a value already stored for the lead.
    """
    incoming = occupancy_from_row(row)
    if previous and (incoming is None or incoming == (DEFAULT_OCCUPANCY, DEFAULT_SOURCE)):
        return previous
    if incoming:
        return incoming
    if previous:
        return previous
    return DEFAULT_OCCUPANCY, DEFAULT_SOURCE


def read_occupancy_index(path: Path) -> dict[str, tuple[str, str]]:
    if not path.is_file():
        return {}
    index: dict[str, tuple[str, str]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            lead_id = (record.get("lead_id") or "").strip()
            if not lead_id:
                continue
            parsed = occupancy_from_row(record)
            if parsed:
                index[lead_id] = parsed
    return index


def update_occupancy(path: Path, lead_id: str, occupancy_type: str) -> None:
    """Set one lead's occupancy in a CSV. The source becomes manual."""
    chosen = occupancy_type.strip().lower()
    if chosen not in OCCUPANCY_TYPES:
        allowed = ", ".join(OCCUPANCY_TYPES)
        raise ValueError(f"occupancy_type must be one of: {allowed}.")
    if not path.is_file():
        raise FileNotFoundError(f"CSV not found: {path}")

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "lead_id" not in fieldnames:
        raise ValueError(f"{path} has no lead_id column.")
    for field in OCCUPANCY_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)

    found = False
    for row in rows:
        if (row.get("lead_id") or "").strip() != lead_id:
            continue
        row["occupancy_type"] = chosen
        row["occupancy_source"] = MANUAL_SOURCE
        found = True
    if not found:
        raise ValueError(f"{lead_id} is not in {path}.")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Set %s occupancy to %s in %s", lead_id, chosen, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Set occupancy on a cleared-lead or quote CSV.")
    parser.add_argument("--lead-id", required=True)
    parser.add_argument("--occupancy", required=True, choices=OCCUPANCY_TYPES)
    parser.add_argument(
        "--file",
        dest="files",
        type=Path,
        action="append",
        required=True,
        help="CSV to update. Repeat for the mail-out and the quote file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        for path in args.files:
            update_occupancy(path, args.lead_id, args.occupancy)
    except (OSError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    print(f"{args.lead_id}: {args.occupancy} (manual)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
