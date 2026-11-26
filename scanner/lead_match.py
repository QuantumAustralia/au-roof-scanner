"""Shared rules for the repair and cleaning filters.

The dashboard and the two contractor exports use these same tests, so a
threshold on screen matches the CSV that is downloaded.
"""

from __future__ import annotations

# Materials the grader is asked to choose. Older grades may still say metal
# or unclear, and those stay selectable so they are not hidden from the filter.
ROOF_MATERIALS = (
    "concrete_tile",
    "terracotta_tile",
    "colorbond_metal",
    "corrugated_iron",
    "other",
)
STORED_MATERIALS = ROOF_MATERIALS + ("metal", "unclear")

# A stored needs_cleaning flag uses this fixed cut. The on-screen threshold
# is separate and can be moved without rewriting the grade.
NEEDS_CLEANING_MIN = 3


def needs_cleaning(cleaning_score: int | None) -> bool:
    """True when the graded clean is at or above the stored cut of 3."""
    return cleaning_score is not None and cleaning_score >= NEEDS_CLEANING_MIN


def repair_side_matches(wear: int | None, repair: int | None, threshold: int) -> bool:
    """A lead matches the repair/wear control when either score reaches it."""
    if wear is not None and wear >= threshold:
        return True
    return repair is not None and repair >= threshold


def cleaning_side_matches(cleaning: int | None, threshold: int) -> bool:
    """A missing cleaning score does not meet the cleaning threshold."""
    return cleaning is not None and cleaning >= threshold


def material_matches(material: str, selected: set[str]) -> bool:
    """All, or an empty selection, keeps every material."""
    if not selected or "all" in selected:
        return True
    return (material or "") in selected


def table_matches(
    *,
    wear: int | None,
    repair: int | None,
    cleaning: int | None,
    material: str,
    repair_threshold: int,
    cleaning_threshold: int,
    match_mode: str,
    materials: set[str],
) -> bool:
    """Row visibility: material, then repair/wear, cleaning, or either."""
    if not material_matches(material, materials):
        return False
    repair_ok = repair_side_matches(wear, repair, repair_threshold)
    cleaning_ok = cleaning_side_matches(cleaning, cleaning_threshold)
    if match_mode == "repair":
        return repair_ok
    if match_mode == "cleaning":
        return cleaning_ok
    return repair_ok or cleaning_ok


def export_repair_matches(
    *,
    wear: int | None,
    repair: int | None,
    cleaning: int | None,
    material: str,
    repair_threshold: int,
    cleaning_threshold: int,
    match_mode: str,
    materials: set[str],
) -> bool:
    """Repair CSV: the repair side and the material. Match mode does not add the cleaning side."""
    del cleaning, cleaning_threshold, match_mode
    if not material_matches(material, materials):
        return False
    return repair_side_matches(wear, repair, repair_threshold)


def export_cleaning_matches(
    *,
    wear: int | None,
    repair: int | None,
    cleaning: int | None,
    material: str,
    repair_threshold: int,
    cleaning_threshold: int,
    match_mode: str,
    materials: set[str],
) -> bool:
    """Cleaning CSV: the cleaning side and the material. Match mode does not add the repair side."""
    del wear, repair, repair_threshold, match_mode
    if not material_matches(material, materials):
        return False
    return cleaning_side_matches(cleaning, cleaning_threshold)
