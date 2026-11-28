"""Two-pass Gemini vision triage with structured JSON."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, field_validator, model_validator

from scanner.addresses import Address
from scanner.config import gemini_model, gemini_model_order, require_env

logger = logging.getLogger(__name__)

Material = Literal[
    "concrete_tile",
    "terracotta_tile",
    "colorbond_metal",
    "corrugated_iron",
    "metal",
    "other",
    "unclear",
]
CleaningDefect = Literal[
    "heavy_moss_lichen",
    "algae_staining",
    "dirt_grime_soiling",
    "organic_growth",
    "none",
]
PrimaryDefect = Literal[
    "none",
    "none_or_cosmetic",
    "cracked_or_slipped_tiles",
    "failed_ridge_capping",
    "rust_or_corrosion",
    "loose_or_lifting_sheets",
    "biological_growth",
    "ponding",
    "failed_flashing",
    "general_ageing",
    "other",
]
FalsePositiveCause = Literal["none", "trees", "solar", "skylights", "tanks", "other"]
CentreContent = Literal[
    "residential_roof",
    "vacant_or_construction",
    "road",
    "lawn_driveway_or_pool",
    "trees",
    "not_residential",
    "not_assessable",
]
_CENTRE_REASONS = {
    "vacant_or_construction": "The image is centred on vacant land or a construction site, not a house roof.",
    "road": "The image is centred on a road or intersection, not a house roof.",
    "lawn_driveway_or_pool": "The image is centred on a lawn, driveway, or pool, not a house roof.",
    "trees": "Trees cover the roof, so it cannot be assessed.",
    "not_residential": "The building is not a residential house roof.",
    "not_assessable": "The house roof is not visible enough to assess.",
}


_EMPTY_REASONS = {"", "none", "n/a", "na", "-", "null", "nil"}


class Pass1Result(BaseModel):
    centre: CentreContent = Field(
        description=(
            "What the crosshair in the centre of the image sits on. "
            "residential_roof only when a house roof surface is under the crosshair. "
            "Use vacant_or_construction for vacant land, a slab, or a site with no roof. "
            "Use road for a road, intersection, roundabout, or car park. "
            "Use lawn_driveway_or_pool for a lawn, driveway, garden, or pool. "
            "Use trees when canopy hides the roof. "
            "Use not_residential for a shop, warehouse, crane, reserve, oval, or other non-house. "
            "Use not_assessable when the roof cannot be scored for another reason."
        )
    )
    roof_visible: bool = Field(
        description=(
            "True only when the target residential roof is in frame and its surface "
            "can be assessed. False when the image is centred on a road, the roof is "
            "hidden by trees, the land is vacant, the building is not residential, "
            "or the roof is out of frame."
        )
    )
    rejection_reason: str = Field(
        description=(
            "Empty string when roof_visible is true. Otherwise one short sentence "
            "naming why this image cannot be scored, suitable for a notes field."
        )
    )
    material: Material
    visual_wear: int = Field(ge=1, le=5)
    repair_likelihood: int = Field(ge=1, le=5)
    primary_defect: PrimaryDefect
    secondary_defect: str = ""
    cleaning_score: int = Field(ge=1, le=5)
    primary_cleaning_defect: CleaningDefect
    confidence: float = Field(ge=0, le=1)
    observation: str

    @property
    def wear_score(self) -> int:
        """Visual wear. Kept under the existing name for quotes and mail-out rows."""
        return self.visual_wear

    def model_dump(self, *args, **kwargs):
        data = super().model_dump(*args, **kwargs)
        data["wear_score"] = self.visual_wear
        data["needs_cleaning"] = self.cleaning_score >= 3
        return data

    @field_validator("confidence", mode="before")
    @classmethod
    def _unit_interval(cls, value: object) -> float:
        return _as_unit_interval(value)

    @field_validator("visual_wear", "repair_likelihood", "cleaning_score", mode="before")
    @classmethod
    def _score(cls, value: object) -> int:
        return int(value)

    @field_validator("rejection_reason", "secondary_defect", mode="before")
    @classmethod
    def _reason(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @model_validator(mode="after")
    def _visibility_gate(self) -> Pass1Result:
        """A failed visibility check cannot keep a wear or repair score of 3 or higher."""
        reason = self.rejection_reason.strip()
        if reason.lower() in _EMPTY_REASONS:
            reason = ""
        if self.centre != "residential_roof":
            self.roof_visible = False
            if not reason:
                reason = _CENTRE_REASONS.get(self.centre, _CENTRE_REASONS["not_assessable"])
        if self.roof_visible and not reason:
            self.rejection_reason = ""
            # Heavy moss across the roof is saleable wear. Light soiling stays at 1 or 2.
            if self._moss_covers_roof() and self.visual_wear < 3:
                self.visual_wear = 3
            return self
        self.roof_visible = False
        self.visual_wear = 1
        self.repair_likelihood = 1
        self.primary_defect = "none_or_cosmetic"
        self.secondary_defect = ""
        self.cleaning_score = 1
        self.primary_cleaning_defect = "none"
        self.material = "unclear"
        if not reason:
            reason = "Residential roof is not visible enough to assess."
        self.rejection_reason = reason
        self.observation = reason
        return self

    def _moss_covers_roof(self) -> bool:
        """Heavy moss across the tiles, not a light stain or ordinary dirt."""
        if self.cleaning_score < 3:
            return False
        if self.primary_cleaning_defect == "heavy_moss_lichen" or self.primary_defect == "biological_growth":
            return True
        text = f"{self.observation} {self.secondary_defect}".casefold()
        return "moss" in text or "lichen" in text


class Pass2Result(BaseModel):
    false_positive: bool
    false_positive_cause: FalsePositiveCause
    verified_defect: str
    pass2_confidence: float = Field(ge=0, le=1)
    defect_box: list[int]
    summary: str

    @field_validator("pass2_confidence", mode="before")
    @classmethod
    def _unit_interval(cls, value: object) -> float:
        return _as_unit_interval(value)

    @field_validator("defect_box", mode="before")
    @classmethod
    def _box(cls, value: object) -> list[int]:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("defect_box must be [ymin, xmin, ymax, xmax]")
        box = [int(round(float(item))) for item in value]
        box = [min(1000, max(0, item)) for item in box]
        return box


def _as_unit_interval(value: object) -> float:
    number = float(value)  # type: ignore[arg-type]
    if number > 1:
        number = number / 100.0
    if not 0 <= number <= 1:
        raise ValueError(f"confidence {value} is outside 0–1")
    return number


_PASS1_PROMPT = """You assess a nadir aerial or satellite image of one residential roof in Australia. State imagery is sharp enough to show individual tiles, ridge lines, and small patches of rust. Look closely before you score anything.

First, set centre to what the crosshair in the middle of the image sits on. This decides whether the image can be scored at all.
- residential_roof: the crosshair is on a house roof and the roof surface can be seen. A clean roof with nothing wrong is still residential_roof.
- vacant_or_construction: vacant land, a concrete slab, a building site, or a block with no house roof
- road: a road, intersection, roundabout, footpath, verge, or car park
- lawn_driveway_or_pool: a lawn, driveway, garden, or pool, even when a house is elsewhere in the frame
- trees: tree canopy covers the roof
- not_residential: a shop, warehouse, commercial building, crane, sports ground, oval, or reserve
- not_assessable: only a sliver of roof, the roof is out of frame, or blur, cloud, or shade hides the surface

Do not choose residential_roof unless you can see house roofing under the crosshair. A photo of land, a road, a lawn, a pool, trees, or a non-residential site is not a wear score of 1.

Set roof_visible to true only when centre is residential_roof. Otherwise set roof_visible to false.

A wear score of 1 and a cleaning score of 1 means a visible house roof with nothing to report. Do not use 1 and 1 for a photo that cannot be assessed.

When roof_visible is false, you must set visual_wear to 1, repair_likelihood to 1, cleaning_score to 1, material to unclear, primary_defect to none_or_cosmetic, and primary_cleaning_defect to none. Write rejection_reason as one short sentence in Australian spelling that names why the image cannot be scored, such as vacant land, road centre, lawn, driveway, or trees covering the roof. A person will copy that sentence into notes. Put the same sentence in observation. Do not describe damage, and do not give a score of 3 or higher.

When roof_visible is true, rejection_reason must be an empty string. Then score the target roof as follows.

Score the roof surface only. Do not treat trees, solar panels, skylights, or rainwater tanks as defects. Ignore neighbouring roofs, driveways, and pools.

Inspect the target roof for these details:
- individual broken, cracked, slipped, or missing tiles
- rust spots and corrosion along ridges, valleys, and sheet laps
- moss or lichen, especially in gutters and shaded areas
- faded or peeling coating on metal roofs
- cracked or lifted flashing
- obvious sagging or other structural movement

material is the roof covering, one of:
- concrete_tile: interlocking concrete tiles
- terracotta_tile: clay or terracotta tiles
- colorbond_metal: pre-painted steel sheet, including Colorbond-style roofs
- corrugated_iron: unpainted or weathered corrugated iron
- other: a clear covering that is none of the above
Use unclear only when roof_visible is false.

visual_wear is structural wear only, from 1 to 5. Dirt, algae, light moss, and ordinary soiling are cleaning issues, not wear.
Set visual_wear to 4 or 5 only when the close inspection shows clear damage: missing or broken tiles, open ridges, rust that has perforated the sheet, moss that is lifting tiles, peeling coating that exposes the substrate, failed flashing, or obvious sagging.
If the roof only looks aged, dirty, or lightly stained, set visual_wear to 1 or 2.
Heavy moss that covers the roof is different. When that moss is clearly visible across the tiles and cleaning_score is 3 or more, set visual_wear to at least 3. Do not leave that roof at 2.
When one of the details above is visible, name that defect. Do not call visible tile, rust, coating, flashing, or sagging damage "general ageing".
Solar panels are a regular grid of dark rectangles. Scattered pale spots on a dark roof are not solar panels.
If two of those defects are visible, set primary_defect to the more important one and secondary_defect to the other. Use a short label such as "Missing tiles", "Rust", "Heavy moss", "Faded coating", "Cracked flashing", or "Sagging". If only one is visible, secondary_defect must be none. Do not invent a second defect.

repair_likelihood is a separate score from 1 to 5: how likely a roofer would find a quoteable repair on site.
1 cosmetic only
2 unlikely to produce a quote
3 a quoteable repair is possible
4 a quoteable repair is likely
5 clear waterproofing or structural failure a roofer would quote
Do not set repair_likelihood to 3 or more for fading, light moss, colour variation, dirt, algae, or general age alone.

Cleaning is a separate score from wear and repair. Moss, lichen, algae, dirt, grime, and other organic growth belong here even when the roof is structurally sound.
cleaning_score is 1 to 5:
1 no cleaning issue
2 only a light stain or a few scattered specks a cleaner would not quote
3 a quoteable clean: obvious moss, algae, or grime on the tiles
4 heavy cover over much of the roof
5 the surface is largely covered
Heavy moss that is clearly visible across the tiles is at least 3. Do not score that roof as 2.
primary_cleaning_defect is one of heavy_moss_lichen, algae_staining, dirt_grime_soiling, organic_growth, or none.
Use none when cleaning_score is 1. Do not use a cleaning defect as the repair primary_defect unless the growth is lifting tiles, opening laps, or exposing the substrate.

primary_defect is the single most important repair issue, or none_or_cosmetic:
none_or_cosmetic, cracked_or_slipped_tiles, failed_ridge_capping, rust_or_corrosion, loose_or_lifting_sheets, biological_growth, ponding, failed_flashing, other
Use cracked_or_slipped_tiles for individual broken or missing tiles.
Use rust_or_corrosion for visible rust spots, including rust along ridges and valleys.
Use biological_growth for moss or lichen that is more than a light stain, including growth in gutters and shade.
Use failed_flashing for cracked or lifted flashing.
Use none_or_cosmetic only when none of that damage is visible.

confidence is 0 to 1. Lower it when trees, shadow, or image quality hide the surface.
observation is one plain-English sentence in Australian spelling. Name the specific detail you can see. If two of the listed defects are visible, name both in that sentence. Do not invent damage you cannot see. Do not mention asbestos.
"""


def _pass2_prompt(address: Address, first: Pass1Result) -> str:
    return f"""A first pass scored this Australian roof at wear {first.wear_score}/5.
Suspected material: {first.material}.
Suspected primary defect: {first.primary_defect}.
First-pass note: {first.observation}
Property: {address.single_line}.

Look again at the target roof for individual broken or missing tiles, rust along ridges and valleys, moss or lichen in gutters and shade, faded or peeling metal coating, cracked or lifted flashing, and obvious sagging.

Decide whether the finding is a real quoteable defect on the target roof, or a false positive.
Fading, light moss, colour variation, and general age are not quoteable defects. If that is all you can see, set false_positive true and verified_defect to none.
Heavy moss covering the roof is a real finding. Do not mark that roof as a false positive.

Reject these false positives: tree canopy or tree shadow (trees), solar panels (solar), skylights, rainwater tanks (tanks). Solar panels are a regular grid of dark rectangles. Scattered pale spots on a dark roof are not solar panels; if they are lichen, moss, or a failed coating, name that defect. Use other for a different non-defect, or none when the defect is real.

If it is a false positive:
- false_positive true
- verified_defect "none"
- pass2_confidence is how sure you are of the rejection
- defect_box [0, 0, 0, 0]
- summary explains what was mistaken for damage, in plain Australian English

If it is a real defect:
- false_positive false
- false_positive_cause none
- verified_defect lists the defects you can see, with no sentence and no wear score. When two are visible, use exactly two short labels separated by " · ", such as "Rust · Missing tiles" or "Heavy moss · Faded coating". When only one is visible, use that single label. Do not add a second label unless a second defect is actually visible.
- defect_box is [ymin, xmin, ymax, xmax] on a 0–1000 grid covering the full image (0, 0 is the top-left). Fit the box tightly around the defect on the target roof.
- pass2_confidence is 0 to 1
- summary is two sentences a homeowner can read. Australian spelling. No prices. No asbestos claim. Name the specific detail that is visible and say that an on-site inspection is required before any work. Do not invent damage.
"""


class RoofTriage:
    def __init__(self, model: str | None = None) -> None:
        self.model = model or gemini_model()
        self.last_model = self.model
        # Retry busy servers, but not 429. A quota error must reach the fallback below.
        self.client = genai.Client(
            api_key=require_env("GEMINI_API_KEY"),
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    http_status_codes=[408, 500, 502, 503, 504],
                )
            ),
        )

    def pass1(self, image_path: Path) -> Pass1Result:
        response = self._generate(image_path, _PASS1_PROMPT, Pass1Result)
        result = _parse(response, Pass1Result)
        if result.roof_visible:
            logger.info(
                "Pass 1 %s model=%s material=%s wear=%s repair=%s cleaning=%s clean_defect=%s defect=%s confidence=%.2f",
                image_path.stem,
                self.last_model,
                result.material,
                result.visual_wear,
                result.repair_likelihood,
                result.cleaning_score,
                result.primary_cleaning_defect,
                result.primary_defect,
                result.confidence,
            )
        else:
            logger.warning(
                "Pass 1 %s rejected before wear scoring: %s",
                image_path.stem,
                result.rejection_reason,
            )
        return result

    def pass2(self, image_path: Path, address: Address, first: Pass1Result) -> Pass2Result:
        response = self._generate(image_path, _pass2_prompt(address, first), Pass2Result)
        result = _parse(response, Pass2Result)
        logger.info(
            "Pass 2 %s model=%s false_positive=%s defect=%s confidence=%.2f",
            address.lead_id,
            self.last_model,
            result.false_positive,
            result.verified_defect,
            result.pass2_confidence,
        )
        return result

    def _generate(self, image_path: Path, prompt: str, schema: type[BaseModel]):
        image = image_path.read_bytes()
        order = _call_order(self.model)
        last_error: Exception | None = None
        for index, model in enumerate(order):
            try:
                response = self.client.models.generate_content(
                    model=model,
                    contents=[
                        types.Part.from_bytes(data=image, mime_type="image/png"),
                        prompt,
                    ],
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        response_mime_type="application/json",
                        response_schema=schema,
                    ),
                )
            except Exception as exc:
                last_error = exc
                if index + 1 < len(order) and _switch_model(exc):
                    logger.warning(
                        "Gemini fallback for %s: %s failed (%s). Trying %s.",
                        image_path.stem,
                        model,
                        _error_label(exc),
                        order[index + 1],
                    )
                    continue
                raise
            self.last_model = model
            if model != order[0]:
                logger.warning(
                    "Gemini graded %s with fallback model %s. Primary %s is tried again on the next call.",
                    image_path.stem,
                    model,
                    order[0],
                )
            return response
        raise last_error or RuntimeError("No Gemini model was called")


def _call_order(primary: str) -> tuple[str, ...]:
    """Start at the requested free model, then the shared fallback list."""
    order = gemini_model_order()
    if primary == order[0]:
        return order
    rest = [name for name in order if name != primary]
    return (primary, *rest)


def _switch_model(exc: BaseException) -> bool:
    """True when this model should be skipped for the current call."""
    return _is_quota_error(exc) or _is_missing_model(exc)


def _is_quota_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    status = str(getattr(exc, "status", "") or "").upper()
    text = str(exc).lower()
    if isinstance(exc, errors.ClientError) and code == 429:
        return True
    if status == "RESOURCE_EXHAUSTED" or code == 429:
        return True
    return "quota" in text or "resource_exhausted" in text or "rate limit" in text


def _is_missing_model(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    text = str(exc).lower()
    return code == 404 or "no longer available" in text or "not found" in text


def _error_label(exc: BaseException) -> str:
    status = getattr(exc, "status", None) or type(exc).__name__
    code = getattr(exc, "code", None)
    if code:
        return f"{code} {status}"
    return str(status)


def _parse(response: types.GenerateContentResponse, schema: type[Pass1Result] | type[Pass2Result]):
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema):
        return parsed
    if parsed is not None:
        return schema.model_validate(parsed)
    text = getattr(response, "text", None) or ""
    if not text.strip():
        raise RuntimeError("Gemini returned an empty response")
    return schema.model_validate_json(text)
