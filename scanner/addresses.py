"""Load and check the lead address CSV."""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path

from scanner.config import PILOT_POSTCODE

REQUIRED_COLUMNS = (
    "lead_id",
    "street_address",
    "suburb",
    "state",
    "postcode",
    "lat",
    "lon",
    "roof_area_m2",
)

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")

# Loose South East Queensland box, used only for the original pilot to catch swapped coordinates.
_SEQ_LAT = (-29.6, -25.8)
_SEQ_LON = (152.0, 153.7)
# Mainland Australia plus Tasmania. Used when a scan is not limited to the pilot,
# so NSW and other states are not rejected.
_AU_LAT = (-44.0, -10.0)
_AU_LON = (112.0, 154.0)


@dataclass(frozen=True)
class Address:
    lead_id: str
    street_address: str
    suburb: str
    state: str
    postcode: str
    lat: float
    lon: float
    roof_area_m2: float
    # Optional Geoscape building-footprint centroid. Both stay unset when the
    # CSV has no roof_centroid_lat / roof_centroid_lon columns, or the cells are blank.
    roof_centroid_lat: float | None = None
    roof_centroid_lon: float | None = None
    # G-NAF geocode type, such as BC or STL. Blank on older address files.
    geocode_type: str = ""

    @property
    def single_line(self) -> str:
        return (
            f"{self.street_address}, {self.suburb} {self.state} {self.postcode}"
        )


# House numbers such as 5R and 110R are council reserves in G-NAF, not homes.
_RESERVE_NUMBER = re.compile(r"(?i)(?:^|\s)\d+R(?:\s|$|-)")
# A neighbour this far away means the lot is rural or very large.
LARGE_LOT_METRES = 150


def is_reserve_number(street: str) -> bool:
    """True when the house number ends in R, as in 5R or 110R."""
    return _RESERVE_NUMBER.search(street.strip()) is not None


def large_lot_lead_ids(points: list[tuple[str, float, float]], metres: float = LARGE_LOT_METRES) -> set[str]:
    """Lead ids whose nearest other address is at least `metres` away, or that are alone."""
    if len(points) < 2:
        return {lead_id for lead_id, _lat, _lon in points}
    cell = 0.0012
    buckets: dict[tuple[int, int], list[int]] = {}
    for index, (_lead_id, lat, lon) in enumerate(points):
        buckets.setdefault((int(lat / cell), int(lon / cell)), []).append(index)
    isolated: set[str] = set()
    for index, (lead_id, lat, lon) in enumerate(points):
        nearest = _nearest_metres(index, points, buckets, cell)
        if nearest is None or nearest >= metres:
            isolated.add(lead_id)
    return isolated


def _nearest_metres(
    index: int,
    points: list[tuple[str, float, float]],
    buckets: dict[tuple[int, int], list[int]],
    cell: float,
) -> float | None:
    _lead_id, lat, lon = points[index]
    centre_x = int(lat / cell)
    centre_y = int(lon / cell)
    best: float | None = None
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            for other in buckets.get((centre_x + dx, centre_y + dy), ()):
                if other == index:
                    continue
                _other_id, other_lat, other_lon = points[other]
                distance = _metres_between(lat, lon, other_lat, other_lon)
                if best is None or distance < best:
                    best = distance
    return best


def _metres_between(lat: float, lon: float, other_lat: float, other_lon: float) -> float:
    mid = (lat + other_lat) / 2
    east = (other_lon - lon) * math.cos(math.radians(mid)) * 111_320
    north = (other_lat - lat) * 110_540
    return (east * east + north * north) ** 0.5


def safe_lead_id(lead_id: str) -> str:
    cleaned = _UNSAFE_ID.sub("-", lead_id.strip()).strip("-")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"lead_id {lead_id!r} cannot be used as a file name")
    return cleaned


def load_addresses(
    path: Path,
    *,
    postcode: str = PILOT_POSTCODE,
    allow_any_postcode: bool = False,
) -> list[Address]:
    if not path.is_file():
        raise FileNotFoundError(f"Address file not found: {path}")

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} has no header row")
        fieldnames = list(reader.fieldnames)
        missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
        if missing:
            raise ValueError(f"{path} is missing columns: {', '.join(missing)}")
        centroid_columns = {"roof_centroid_lat", "roof_centroid_lon"}
        present_centroids = centroid_columns & set(fieldnames)
        if present_centroids and present_centroids != centroid_columns:
            raise ValueError(
                f"{path} must include both roof_centroid_lat and roof_centroid_lon, or neither."
            )

        addresses: list[Address] = []
        seen: set[str] = set()
        for line_no, row in enumerate(reader, start=2):
            address = _parse_row(
                row,
                line_no,
                postcode,
                allow_any_postcode,
                allow_centroid=bool(present_centroids),
            )
            if address.lead_id in seen:
                raise ValueError(f"Duplicate lead_id {address.lead_id} on line {line_no}")
            seen.add(address.lead_id)
            addresses.append(address)
    return addresses


def _parse_row(
    row: dict[str, str | None],
    line_no: int,
    postcode: str,
    allow_any_postcode: bool,
    allow_centroid: bool = False,
) -> Address:
    def cell(name: str) -> str:
        return (row.get(name) or "").strip()

    lead_id = safe_lead_id(cell("lead_id"))
    try:
        lat = float(cell("lat"))
        lon = float(cell("lon"))
        roof_area = float(cell("roof_area_m2"))
    except ValueError as exc:
        raise ValueError(f"Line {line_no} ({lead_id}): lat, lon, and roof_area_m2 must be numbers") from exc

    if roof_area <= 0:
        raise ValueError(f"Line {line_no} ({lead_id}): roof_area_m2 must be greater than zero")
    # The pilot stays inside South East Queensland. Any other loaded postcode
    # only has to fall inside Australia, so NSW and other states are accepted.
    lat_box, lon_box = (_AU_LAT, _AU_LON) if allow_any_postcode else (_SEQ_LAT, _SEQ_LON)
    region = "Australia" if allow_any_postcode else "South East Queensland"
    if not (lat_box[0] <= lat <= lat_box[1] and lon_box[0] <= lon <= lon_box[1]):
        raise ValueError(
            f"Line {line_no} ({lead_id}): coordinates {lat}, {lon} are outside {region}. "
            "Check that latitude and longitude are not swapped."
        )

    row_postcode = cell("postcode")
    if not allow_any_postcode and row_postcode != postcode:
        raise ValueError(
            f"Line {line_no} ({lead_id}): postcode {row_postcode} is outside the {postcode} pilot. "
            "Pass --allow-any-postcode to include it."
        )

    street = cell("street_address")
    suburb = cell("suburb")
    state = cell("state").upper()
    if not street or not suburb or not state:
        raise ValueError(f"Line {line_no} ({lead_id}): street, suburb, and state are required")

    centroid_lat, centroid_lon = _parse_centroid(
        cell, line_no, lead_id, allow_centroid, allow_any_postcode
    )

    return Address(
        lead_id=lead_id,
        street_address=street,
        suburb=suburb,
        state=state,
        postcode=row_postcode,
        lat=lat,
        lon=lon,
        roof_area_m2=roof_area,
        roof_centroid_lat=centroid_lat,
        roof_centroid_lon=centroid_lon,
        geocode_type=cell("geocode_type").upper(),
    )


def _parse_centroid(
    cell,
    line_no: int,
    lead_id: str,
    allow_centroid: bool,
    allow_any_postcode: bool = False,
) -> tuple[float | None, float | None]:
    """Read an optional roof centroid. Blank cells fall back to the address point."""
    if not allow_centroid:
        return None, None

    raw_lat = cell("roof_centroid_lat")
    raw_lon = cell("roof_centroid_lon")
    if not raw_lat and not raw_lon:
        return None, None
    if not raw_lat or not raw_lon:
        raise ValueError(
            f"Line {line_no} ({lead_id}): roof_centroid_lat and roof_centroid_lon "
            "must both be filled in, or both left blank."
        )
    try:
        centroid_lat = float(raw_lat)
        centroid_lon = float(raw_lon)
    except ValueError as exc:
        raise ValueError(
            f"Line {line_no} ({lead_id}): roof centroid coordinates must be numbers"
        ) from exc
    lat_box, lon_box = (_AU_LAT, _AU_LON) if allow_any_postcode else (_SEQ_LAT, _SEQ_LON)
    region = "Australia" if allow_any_postcode else "South East Queensland"
    if not (lat_box[0] <= centroid_lat <= lat_box[1] and lon_box[0] <= centroid_lon <= lon_box[1]):
        raise ValueError(
            f"Line {line_no} ({lead_id}): roof centroid {centroid_lat}, {centroid_lon} "
            f"is outside {region}."
        )
    return centroid_lat, centroid_lon
