"""Paths, pilot limits, and environment settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

PILOT_POSTCODE = "4218"
PILOT_STATE = "QLD"

# Static Maps returns size × scale pixels. 640×640 at scale 2 is the largest
# standard image: a 1280×1280 PNG. Zoom 21 covers about 40 m across that frame
# in southern Australia, so a typical house roof stays centred with a small
# margin of garden. Zoom 20 would double that ground distance and give the
# roof fewer pixels.
TILE_SIZE = "640x640"
TILE_ZOOM = 21
TILE_SCALE = 2
TILE_MAPTYPE = "satellite"
TILE_FORMAT = "png"

PASS2_WEAR_MIN = 3
# Temporary test threshold so the enhancement pipeline can be checked.
# Return this to 0.92 for production.
ANNOTATION_CONFIDENCE = 0.85

# Free-tier Gemini models only, tried in this order. Each call starts again at
# the first model. A later model is used only after a quota or 429 error.
# Edit this tuple to change the fallback list.
FREE_GEMINI_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
)
DEFAULT_GEMINI_MODEL = FREE_GEMINI_MODELS[0]

DATA_DIR = ROOT / "data"
TILES_DIR = DATA_DIR / "tiles"
ANNOTATED_DIR = DATA_DIR / "annotated"
# Clean view image, before the defect label is drawn. The labelled copy is ANNOTATED_DIR.
VIEW_BASE_DIR = DATA_DIR / "view_base"
ENHANCED_DIR = DATA_DIR / "enhanced"
GRADES_DIR = DATA_DIR / "grades"
CLUSTERS_DIR = DATA_DIR / "clusters"
LOG_DIR = DATA_DIR / "logs"
USAGE_LOG_PATH = LOG_DIR / "static_maps_usage.jsonl"
MAILOUT_PATH = DATA_DIR / "cleared_for_mailout.csv"
DEFAULT_INPUT = ROOT / "input_addresses.csv"


def env(name: str) -> str:
    return os.getenv(name, "").strip()


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def gemini_model() -> str:
    requested = env("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    if requested in FREE_GEMINI_MODELS:
        return requested
    return DEFAULT_GEMINI_MODEL


def gemini_model_order() -> tuple[str, ...]:
    """Primary free model first, then the rest of FREE_GEMINI_MODELS."""
    primary = gemini_model()
    return (primary, *[name for name in FREE_GEMINI_MODELS if name != primary])


def ensure_data_dirs() -> None:
    for path in (TILES_DIR, ANNOTATED_DIR, VIEW_BASE_DIR, ENHANCED_DIR, GRADES_DIR, CLUSTERS_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
