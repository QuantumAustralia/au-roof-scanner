"""Minor-repair quotes from pricing_config.json.

Edit pricing_config.json to change the call-out, minimum, material rates,
and the share of roof area repaired at each wear score. This module reads
that file on every quote. These are minor-repair estimates, not full replacements.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

if __name__ == "__main__" and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.config import DATA_DIR, ROOT
from scanner.flag_highres import high_res_text, read_high_res_index, resolve_high_res
from scanner.occupancy import read_occupancy_index, resolve_occupancy

logger = logging.getLogger(__name__)

# Change rates, the call-out, and repair fractions in pricing_config.json, not here.
PRICING_CONFIG_PATH = ROOT / "pricing_config.json"
QUOTES_PATH = DATA_DIR / "quotes.csv"

QUOTE_FIELDS = (
    "lead_id",
    "address",
    "material",
    "roof_area_m2",
    "wear_score",
    "repair_fraction",
    "repaired_area_m2",
    "material_rate",
    "base_callout",
    "variable_aud",
    "minimum_repair",
    "minimum_applied",
    "quote_aud",
    "notes",
    "occupancy_type",
    "occupancy_source",
    "high_res_requested",
)


@dataclass(frozen=True)
class RepairQuote:
    material: str
    roof_area_m2: float
    wear_score: int
    repair_fraction: float
    repaired_area_m2: float
    material_rate: float
    base_callout: float
    variable_aud: float
    minimum_repair: float
    minimum_applied: bool
    quote_aud: int
    notes: str


def load_pricing_config(path: Path = PRICING_CONFIG_PATH) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Pricing config not found: {path}")
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    rates = config.get("rates_aud_per_m2")
    fractions = config.get("repair_fraction_by_wear")
    if not isinstance(rates, dict) or "other" not in rates:
        raise ValueError(f"{path} needs a rates_aud_per_m2 object that includes 'other'.")
    if not isinstance(fractions, dict):
        raise ValueError(f"{path} needs a repair_fraction_by_wear object.")
    for key in ("base_callout", "minimum_repair"):
        if key not in config:
            raise ValueError(f"{path} is missing {key}.")
    return config


def quote_repair(material: str, roof_area_m2: float, wear_score: int) -> RepairQuote:
    """Price a minor repair. Reads pricing_config.json at call time.

    quote = max(minimum_repair, base_callout + (roof_area_m2 * repair_fraction * material_rate))
    """
    config = load_pricing_config()
    rates: dict[str, float] = config["rates_aud_per_m2"]
    requested = (material or "other").strip().lower()
    used = requested if requested in rates else "other"
    material_rate = float(rates[used])
    repair_fraction = _repair_fraction(wear_score, config)
    base_callout = float(config["base_callout"])
    minimum_repair = float(config["minimum_repair"])

    repaired_area_m2 = roof_area_m2 * repair_fraction
    variable_aud = repaired_area_m2 * material_rate
    # quote = max(minimum_repair, base_callout + (roof_area_m2 * repair_fraction * material_rate))
    subtotal = base_callout + variable_aud
    minimum_applied = subtotal < minimum_repair
    quote_aud = int(round(max(minimum_repair, subtotal)))
    notes = _notes(
        requested,
        used,
        roof_area_m2,
        repaired_area_m2,
        material_rate,
        repair_fraction,
        base_callout,
        minimum_repair,
        minimum_applied,
        wear_score,
    )
    return RepairQuote(
        material=used,
        roof_area_m2=roof_area_m2,
        wear_score=wear_score,
        repair_fraction=repair_fraction,
        repaired_area_m2=round(repaired_area_m2, 2),
        material_rate=material_rate,
        base_callout=base_callout,
        variable_aud=round(variable_aud, 2),
        minimum_repair=minimum_repair,
        minimum_applied=minimum_applied,
        quote_aud=quote_aud,
        notes=notes,
    )


def quote_cleared_csv(source: Path, destination: Path = QUOTES_PATH) -> list[dict[str, object]]:
    if not source.is_file():
        raise FileNotFoundError(f"Cleared-lead file not found: {source}")
    previous_occupancy = read_occupancy_index(destination)
    previous_high_res = read_high_res_index(destination)
    rows: list[dict[str, object]] = []
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for line_no, record in enumerate(reader, start=2):
            lead_id = (record.get("lead_id") or "").strip()
            if not lead_id:
                continue
            try:
                area = float(record["roof_area_m2"])
                wear = int(float(record["wear_score"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Line {line_no} ({lead_id}) is missing area or wear") from exc
            quoted = quote_repair((record.get("material") or "other"), area, wear)
            occupancy_type, occupancy_source = resolve_occupancy(record, previous_occupancy.get(lead_id))
            high_res_requested = high_res_text(
                resolve_high_res(record, previous_high_res.get(lead_id))
            )
            rows.append(
                {
                    "lead_id": lead_id,
                    "address": _address(record),
                    "material": quoted.material,
                    "roof_area_m2": quoted.roof_area_m2,
                    "wear_score": quoted.wear_score,
                    "repair_fraction": quoted.repair_fraction,
                    "repaired_area_m2": quoted.repaired_area_m2,
                    "material_rate": quoted.material_rate,
                    "base_callout": quoted.base_callout,
                    "variable_aud": quoted.variable_aud,
                    "minimum_repair": quoted.minimum_repair,
                    "minimum_applied": quoted.minimum_applied,
                    "quote_aud": quoted.quote_aud,
                    "notes": quoted.notes,
                    "occupancy_type": occupancy_type,
                    "occupancy_source": occupancy_source,
                    "high_res_requested": high_res_requested,
                }
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=QUOTE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %s quote(s) to %s", len(rows), destination)
    return rows


def _repair_fraction(wear_score: int, config: dict) -> float:
    fractions: dict[str, float] = config["repair_fraction_by_wear"]
    if wear_score >= 5:
        key = "5"
    elif wear_score <= 3:
        key = "3" if wear_score == 3 else ""
    else:
        key = str(wear_score)
    if not key:
        return 0.0
    if key not in fractions:
        raise ValueError(
            f"pricing_config.json has no repair fraction for wear {key}. "
            "Add it under repair_fraction_by_wear."
        )
    return float(fractions[key])


def _notes(
    requested: str,
    used: str,
    area: float,
    repaired_area: float,
    rate: float,
    fraction: float,
    callout: float,
    minimum: float,
    minimum_applied: bool,
    wear_score: int,
) -> str:
    material_note = ""
    if used != requested:
        material_note = f" Material {requested or 'blank'} is not in the rate file, so the 'other' rate was used."
    floor = f" Minimum of ${minimum:g} applied." if minimum_applied else ""
    return (
        f"Minor repair, not a full replacement. "
        f"Wear {wear_score} repairs {fraction * 100:g}% of {area:g} m² "
        f"({repaired_area:.2f} m²) at ${rate:g}/m², plus a ${callout:g} call-out."
        f"{floor} Not a site quotation."
        + material_note
    )


def _address(record: dict[str, str | None]) -> str:
    street = (record.get("street_address") or "").strip()
    suburb = (record.get("suburb") or "").strip()
    state = (record.get("state") or "").strip()
    postcode = (record.get("postcode") or "").strip()
    locality = " ".join(part for part in (suburb, state, postcode) if part)
    if street and locality:
        return f"{street}, {locality}"
    single = (record.get("address") or "").strip()
    return single or street


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write minor-repair quotes from a cleared-lead CSV.")
    parser.add_argument("--input", type=Path, required=True, help="Cleared mail-out CSV")
    parser.add_argument("--output", type=Path, default=QUOTES_PATH, help="Quote CSV")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        rows = quote_cleared_csv(args.input, args.output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("%s", exc)
        return 1
    for row in rows:
        print(
            f"{row['lead_id']}: ${row['quote_aud']:,} "
            f"({row['material']}, wear {row['wear_score']}, "
            f"{row['repaired_area_m2']} m² of {row['roof_area_m2']:g} m² at ${row['material_rate']:g}/m², "
            f"call-out ${row['base_callout']:g})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
