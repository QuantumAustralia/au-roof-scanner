"""Grade roof tiles with a two-pass Gemini vision triage.

Pass 1 first decides whether a residential roof is visible. A failed check
forces wear and repair to 1 and records a rejection reason. Wear is scored
only when the roof is assessable.
Pass 2 runs only when that roof is visible and wear is 3 or higher. It rejects
trees, solar panels, skylights, and rainwater tanks, then returns a verified
defect, a confidence score, a 0–1000 box, and a plain-English summary.

Images at or above ANNOTATION_CONFIDENCE are copied to data/annotated/ with a
yellow target box, then polished into data/enhanced/. That threshold is
temporarily 0.85 and should be returned to 0.92 for production. Cleared leads
are written to data/cleared_for_mailout.csv.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.addresses import Address, load_addresses
from scanner.clusters import write_cleared_clusters
from scanner.annotate import box_is_drawable, publish_view
from scanner.config import (
    ANNOTATION_CONFIDENCE,
    ENHANCED_DIR,
    DEFAULT_INPUT,
    GRADES_DIR,
    MAILOUT_PATH,
    PASS2_WEAR_MIN,
    TILES_DIR,
    ensure_data_dirs,
    gemini_model,
)
from scanner.enhance import enhance_annotated
from scanner.fetch_tiles import discard_rejected_images, is_nsw_address, saved_nsw_problem
from scanner.mailout import push_supabase, write_mailout
from scanner.lead_match import needs_cleaning
from scanner.pricing import indicative_price
from scanner.triage import Pass2Result, RoofTriage
from scanner.verification import write_verification_report

logger = logging.getLogger(__name__)


def grade_all(
    addresses: list[Address],
    *,
    triage: RoofTriage,
    output: Path,
    push: bool,
) -> int:
    ensure_data_dirs()
    cleared: list[dict[str, object]] = []
    processed: list[Address] = []
    skipped_missing = 0
    skipped_wear = 0
    not_assessable = 0
    false_positives = 0
    annotated = 0
    errors = 0

    for address in addresses:
        tile = TILES_DIR / f"{address.lead_id}.png"
        if not tile.is_file():
            logger.warning("No tile for %s at %s", address.lead_id, tile)
            skipped_missing += 1
            continue
        if is_nsw_address(address):
            problem = saved_nsw_problem(address.lead_id)
            if problem:
                logger.error(
                    "Not grading %s: %s Discarded. Google Static Maps was not used.",
                    address.lead_id,
                    problem,
                )
                discard_rejected_images(address.lead_id)
                skipped_missing += 1
                continue
        try:
            row, did_annotate, outcome = grade_one(address, tile, triage)
        except Exception as exc:  # noqa: BLE001 — one bad lead should not stop the batch
            logger.error("Grading failed for %s: %s", address.lead_id, exc)
            errors += 1
            continue
        processed.append(address)

        if outcome == "low_wear":
            skipped_wear += 1
        elif outcome == "not_assessable":
            not_assessable += 1
        elif outcome == "false_positive":
            false_positives += 1
        elif row is not None:
            cleared.append(row)
            annotated += int(did_annotate)

    cleared = write_mailout(output, cleared)
    logger.info("Wrote %s cleared lead(s) to %s", len(cleared), output)
    logger.info(
        "Grading summary: %s cleared, %s annotated, %s below wear %s, "
        "%s not assessable, %s false positives, %s missing tiles, %s errors",
        len(cleared),
        annotated,
        skipped_wear,
        PASS2_WEAR_MIN,
        not_assessable,
        false_positives,
        skipped_missing,
        errors,
    )

    if push:
        push_supabase(cleared)
    # Clusters use every cleared grade, not only the batch just written.
    write_cleared_clusters()
    report = write_verification_report(processed)
    logger.info("Wrote address verification report to %s", report)
    return 1 if errors else 0


def grade_one(
    address: Address,
    tile: Path,
    triage: RoofTriage,
) -> tuple[dict[str, object] | None, bool, str]:
    first = triage.pass1(tile)
    record: dict[str, object] = {
        "lead_id": address.lead_id,
        "address": address.single_line,
        "model": triage.last_model,
        "pass1": first.model_dump(),
    }

    if not first.roof_visible:
        record["outcome"] = "not_assessable"
        record["rejection_reason"] = first.rejection_reason
        _write_grade(address.lead_id, record)
        _refresh_view(address.lead_id)
        logger.warning(
            "%s not assessable, wear forced to 1: %s",
            address.lead_id,
            first.rejection_reason,
        )
        return None, False, "not_assessable"

    if first.wear_score < PASS2_WEAR_MIN:
        record["outcome"] = "low_wear"
        _write_grade(address.lead_id, record)
        _refresh_view(address.lead_id)
        logger.info("%s wear %s; pass 2 not required", address.lead_id, first.wear_score)
        return None, False, "low_wear"

    pass1_model = str(record["model"])
    second = triage.pass2(tile, address, first)
    if triage.last_model != pass1_model:
        record["model"] = f"{pass1_model} then {triage.last_model}"
    record["pass2"] = second.model_dump()

    if second.false_positive or second.verified_defect.strip().lower() in {"", "none"}:
        record["outcome"] = "false_positive"
        _write_grade(address.lead_id, record)
        _refresh_view(address.lead_id)
        logger.info(
            "%s rejected as a false positive (%s)",
            address.lead_id,
            second.false_positive_cause,
        )
        return None, False, "false_positive"

    price = indicative_price(address.roof_area_m2, first.material, first.wear_score)
    did_annotate = False
    box = second.defect_box if second.pass2_confidence >= ANNOTATION_CONFIDENCE and box_is_drawable(second.defect_box) else None
    if second.pass2_confidence >= ANNOTATION_CONFIDENCE and box is None:
        logger.warning("%s confidence is high but the defect box cannot be drawn", address.lead_id)
    row = _mailout_row(
        address,
        first.material,
        first.wear_score,
        first.repair_likelihood,
        first.primary_defect,
        first.cleaning_score,
        first.primary_cleaning_defect,
        second,
        price,
        tile,
        "",
    )
    record["outcome"] = "cleared"
    record["mailout"] = row
    _write_grade(address.lead_id, record)
    view = _refresh_view(address.lead_id, box)
    if view is not None and box is not None:
        row["annotated_path"] = str(view)
        record["mailout"] = row
        _write_grade(address.lead_id, record)
        did_annotate = True
        logger.info("Annotated %s", view)
        enhance_annotated(view, ENHANCED_DIR / f"{address.lead_id}_enhanced.png")
    return row, did_annotate, "cleared"


def _mailout_row(
    address: Address,
    material: str,
    wear_score: int,
    repair_likelihood: int,
    primary_defect: str,
    cleaning_score: int,
    primary_cleaning_defect: str,
    second: Pass2Result,
    price,
    tile: Path,
    annotated_path: str,
) -> dict[str, object]:
    ymin, xmin, ymax, xmax = second.defect_box
    return {
        "lead_id": address.lead_id,
        "street_address": address.street_address,
        "suburb": address.suburb,
        "state": address.state,
        "postcode": address.postcode,
        "lat": address.lat,
        "lon": address.lon,
        "roof_area_m2": address.roof_area_m2,
        "material": material,
        "roof_material": material,
        "wear_score": wear_score,
        "repair_likelihood": repair_likelihood,
        "cleaning_score": cleaning_score,
        "primary_cleaning_defect": primary_cleaning_defect,
        "needs_cleaning": "true" if needs_cleaning(cleaning_score) else "false",
        "primary_defect": primary_defect,
        "verified_defect": second.verified_defect,
        "pass2_confidence": round(second.pass2_confidence, 4),
        "summary": " ".join(second.summary.split()),
        "defect_box": f"{ymin},{xmin},{ymax},{xmax}",
        "price_low_aud": price.low_aud if price else "",
        "price_high_aud": price.high_aud if price else "",
        "pricing_basis": price.basis if price else "",
        "tile_path": str(tile),
        "annotated_path": annotated_path,
        "occupancy_type": "unknown",
        "occupancy_source": "default",
        "high_res_requested": "false",
    }


def _refresh_view(lead_id: str, defect_box: list[int] | None = None) -> Path | None:
    """Redraw the view copy from the clean image. The original tile is left unchanged."""
    try:
        return publish_view(lead_id, defect_box=defect_box)
    except OSError as exc:
        logger.warning("Could not update the view image for %s: %s", lead_id, exc)
        return None


def _write_grade(lead_id: str, record: dict[str, object]) -> None:
    path = GRADES_DIR / f"{lead_id}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grade roof tiles and export the mail-out list.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Address CSV")
    parser.add_argument("--output", type=Path, default=MAILOUT_PATH, help="Cleared mail-out CSV")
    parser.add_argument("--model", default=None, help="Gemini model (default from GEMINI_MODEL)")
    parser.add_argument(
        "--push-supabase",
        action="store_true",
        help="Upsert cleared leads into Supabase",
    )
    parser.add_argument(
        "--allow-any-postcode",
        action="store_true",
        help="Include addresses outside the 4218 pilot",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        addresses = load_addresses(args.input, allow_any_postcode=args.allow_any_postcode)
        triage = RoofTriage(model=args.model or gemini_model())
        return grade_all(addresses, triage=triage, output=args.output, push=args.push_supabase)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
