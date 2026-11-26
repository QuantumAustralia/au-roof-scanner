"""Re-grade roofs that already have a tile, without downloading imagery.

The upgraded grader adds cleaning_score and primary_cleaning_defect. Wear,
repair likelihood, and roof material are scored again on the same image.

    python -m scanner.regrade_saved --limit 2
    python -m scanner.regrade_saved --lead GANSW704689557

Cleared rows for the leads in this run are replaced. Other mail-out rows are kept.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.addresses import Address, load_addresses
from scanner.clusters import write_cleared_clusters
from scanner.config import DATA_DIR, MAILOUT_PATH, TILES_DIR, ensure_data_dirs
from scanner.grade_roofs import grade_one
from scanner.mailout import write_mailout
from scanner.quote import QUOTES_PATH, quote_cleared_csv
from scanner.triage import RoofTriage

logger = logging.getLogger(__name__)


def saved_addresses() -> dict[str, Address]:
    """Every address row in the project whose tile is already on disk."""
    found: dict[str, Address] = {}
    paths = [Path("input_addresses.csv"), *sorted(DATA_DIR.glob("addresses_*.csv"))]
    for path in paths:
        if not path.is_file():
            continue
        try:
            rows = load_addresses(path, allow_any_postcode=True)
        except (OSError, ValueError) as exc:
            logger.warning("Skipped %s: %s", path, exc)
            continue
        for address in rows:
            if (TILES_DIR / f"{address.lead_id}.png").is_file():
                found[address.lead_id] = address
    return found


def regrade_saved(lead_ids: list[str] | None, limit: int | None) -> int:
    """Grade saved tiles and merge the mail-out. Returns the number graded."""
    ensure_data_dirs()
    available = saved_addresses()
    chosen = [available[lead_id] for lead_id in lead_ids if lead_id in available] if lead_ids else list(available.values())
    missing = [lead_id for lead_id in (lead_ids or []) if lead_id not in available]
    for lead_id in missing:
        logger.error("No saved tile and address for %s", lead_id)
    if limit is not None:
        chosen = chosen[:limit]
    if not chosen:
        logger.error("No saved tiles to re-grade.")
        return 0
    logger.info("Re-grading %s saved roof(s). Tiles are not downloaded again.", len(chosen))
    triage = RoofTriage()
    fresh: list[dict[str, object]] = []
    graded = 0
    for address in chosen:
        tile = TILES_DIR / f"{address.lead_id}.png"
        try:
            row, _annotated, outcome = grade_one(address, tile, triage)
        except Exception as exc:  # noqa: BLE001 — one roof should not stop the rest
            logger.error("Re-grade failed for %s: %s", address.lead_id, exc)
            continue
        graded += 1
        logger.info("%s %s", address.lead_id, outcome)
        if row is not None:
            fresh.append(row)
    batch = {address.lead_id for address in chosen}
    previous: list[dict[str, str]] = []
    if MAILOUT_PATH.is_file():
        with MAILOUT_PATH.open(encoding="utf-8", newline="") as handle:
            previous = list(csv.DictReader(handle))
    kept = [row for row in previous if (row.get("lead_id") or "") not in batch]
    write_mailout(MAILOUT_PATH, kept + fresh)
    quote_cleared_csv(MAILOUT_PATH, QUOTES_PATH)
    write_cleared_clusters()
    logger.info("Re-graded %s. Mail-out kept %s and added %s cleared.", graded, len(kept), len(fresh))
    return graded


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Re-grade saved roof tiles, including cleaning scores.")
    parser.add_argument("--lead", action="append", default=[], help="One lead id. Repeat for several.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after this many saved roofs.")
    args = parser.parse_args(argv)
    count = regrade_saved(args.lead or None, args.limit)
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
