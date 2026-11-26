"""Append-only log of Google Static Maps requests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from scanner.config import LOG_DIR, TILE_MAPTYPE, TILE_SCALE, TILE_SIZE, TILE_ZOOM, USAGE_LOG_PATH


def log_static_maps_usage(**fields: Any) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "zoom": TILE_ZOOM,
        "size": TILE_SIZE,
        "scale": TILE_SCALE,
        "maptype": TILE_MAPTYPE,
        **fields,
    }
    with USAGE_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
