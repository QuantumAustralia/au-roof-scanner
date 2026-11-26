"""Indicative AUD ranges for South East Queensland roof work.

These bands are for mail-out guidance. They are not quotations. Edit the
rates as local contractor prices come in. Figures include GST and exclude
scaffolding, asbestos removal, solar removal, and difficult access.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RateBand:
    low_per_m2: float
    high_per_m2: float
    basis: str


@dataclass(frozen=True)
class PriceRange:
    low_aud: int
    high_aud: int
    basis: str


# Wear 3: maintenance or restoration is likely.
# Wear 4: significant repair or restoration.
# Wear 5: replacement is likely.
_BANDS: dict[str, dict[int, RateBand]] = {
    "concrete_tile": {
        3: RateBand(32, 48, "concrete tile clean, repoint and reseal"),
        4: RateBand(55, 90, "concrete tile repairs and restoration"),
        5: RateBand(120, 180, "concrete tile roof replacement"),
    },
    "terracotta_tile": {
        3: RateBand(40, 60, "terracotta clean, repoint and reseal"),
        4: RateBand(70, 110, "terracotta repairs and restoration"),
        5: RateBand(150, 220, "terracotta roof replacement"),
    },
    "metal": {
        3: RateBand(28, 45, "metal roof wash and protective coating"),
        4: RateBand(50, 85, "metal roof repairs and recoating"),
        5: RateBand(110, 170, "metal roof replacement"),
    },
    "colorbond_metal": {
        3: RateBand(28, 45, "Colorbond wash and protective coating"),
        4: RateBand(50, 85, "Colorbond repairs and recoating"),
        5: RateBand(110, 170, "Colorbond roof replacement"),
    },
    "corrugated_iron": {
        3: RateBand(28, 45, "corrugated iron wash and protective coating"),
        4: RateBand(50, 85, "corrugated iron repairs and recoating"),
        5: RateBand(110, 170, "corrugated iron roof replacement"),
    },
}

_FALLBACK = {
    3: RateBand(35, 70, "inspection and maintenance (roof material unclear)"),
    4: RateBand(60, 120, "repairs (roof material unclear)"),
    5: RateBand(120, 200, "roof replacement (roof material unclear)"),
}


def indicative_price(roof_area_m2: float, material: str, wear_score: int) -> PriceRange | None:
    """Return a rounded GST-inclusive range, or None when wear does not justify one."""
    if wear_score < 3 or roof_area_m2 <= 0:
        return None

    band = _BANDS.get(material, _FALLBACK).get(wear_score) or _FALLBACK[min(wear_score, 5)]
    low = _round_aud(band.low_per_m2 * roof_area_m2)
    high = _round_aud(band.high_per_m2 * roof_area_m2)
    if high <= low:
        high = low + 50
    basis = (
        f"{band.basis} at {roof_area_m2:g} m². "
        "Indicative range including GST. Not a quotation."
    )
    return PriceRange(low_aud=low, high_aud=high, basis=basis)


def _round_aud(amount: float) -> int:
    return int(round(amount / 50.0) * 50)
