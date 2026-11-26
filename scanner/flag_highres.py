"""Mark a lead for later high-resolution imagery.

This only sets high_res_requested on a ranked quote CSV or a cleared mail-out
CSV. It does not call Nearmap or any other paid imagery API.
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

HIGH_RES_FIELD = "high_res_requested"
_TRUE = {"1", "true", "yes", "y"}
_FALSE = {"0", "false", "no", "n"}


def parse_high_res(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE or text == "":
        return False
    raise ValueError(f"{HIGH_RES_FIELD} must be true or false. Got {value!r}.")


def high_res_text(flagged: bool) -> str:
    return "true" if flagged else "false"


def resolve_high_res(row: dict[str, object], previous: bool | None = None) -> bool:
    """Keep a saved true flag when a later grade or quote would write false."""
    if previous:
        return True
    if HIGH_RES_FIELD not in row:
        return False
    return parse_high_res(row.get(HIGH_RES_FIELD))


def read_high_res_index(path: Path) -> dict[str, bool]:
    if not path.is_file():
        return {}
    index: dict[str, bool] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or HIGH_RES_FIELD not in reader.fieldnames:
            return {}
        for record in reader:
            lead_id = (record.get("lead_id") or "").strip()
            if lead_id:
                index[lead_id] = parse_high_res(record.get(HIGH_RES_FIELD))
    return index


def set_high_res(path: Path, lead_id: str, flagged: bool) -> None:
    """Set high_res_requested for one lead. Other columns are left as they are."""
    if not path.is_file():
        raise FileNotFoundError(f"CSV not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "lead_id" not in fieldnames:
        raise ValueError(f"{path} has no lead_id column.")
    if HIGH_RES_FIELD not in fieldnames:
        fieldnames.append(HIGH_RES_FIELD)

    found = False
    for row in rows:
        if (row.get("lead_id") or "").strip() != lead_id:
            continue
        row[HIGH_RES_FIELD] = high_res_text(flagged)
        found = True
    if not found:
        raise ValueError(f"{lead_id} is not in {path}.")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Set %s %s=%s in %s", lead_id, HIGH_RES_FIELD, high_res_text(flagged), path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Flag a lead for later Nearmap or other paid high-resolution imagery."
    )
    parser.add_argument("--lead-id", required=True)
    parser.add_argument("--file", type=Path, required=True, help="Ranked quote CSV or cleared mail-out CSV")
    parser.add_argument("--unflag", action="store_true", help="Set high_res_requested back to false")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    flagged = not args.unflag
    try:
        set_high_res(args.file, args.lead_id, flagged)
    except (OSError, ValueError) as exc:
        logger.error("%s", exc)
        return 1
    print(f"{args.lead_id}: {HIGH_RES_FIELD}={high_res_text(flagged)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
