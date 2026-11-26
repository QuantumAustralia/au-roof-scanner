"""Fetch a roof tile for one address.

NSW addresses use NSW Spatial Services imagery only. If that service fails,
the fetch stops with an error and Google Static Maps is not called.
A saved NSW tile must be the sharp 512 px composite with a yellow crosshair.
A soft Google-style image, a frame near 1020 px, or a missing crosshair is
deleted and is not graded.
QLD addresses use the Queensland Latest State Program imagery, then Google
Static Maps when that service cannot return a usable image. Any other state
uses Google Static Maps. A Google request is billable. Existing tiles are
skipped unless --force is set, and every attempt is appended to
data/logs/static_maps_usage.jsonl. The API key is never written to the log.
"""

from __future__ import annotations

import argparse
import logging
import math
import re
import sys
import time
from io import BytesIO
from pathlib import Path

if __name__ == "__main__" and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageStat

from scanner.addresses import Address, load_addresses
from scanner.annotate import save_view_base
from scanner.config import (
    ANNOTATED_DIR,
    DEFAULT_INPUT,
    ENHANCED_DIR,
    TILE_FORMAT,
    TILE_MAPTYPE,
    TILE_SCALE,
    TILE_SIZE,
    TILE_ZOOM,
    TILES_DIR,
    VIEW_BASE_DIR,
    ensure_data_dirs,
    require_env,
)
from scanner.usage_log import log_static_maps_usage

logger = logging.getLogger(__name__)

STATIC_MAPS_URL = "https://maps.googleapis.com/maps/api/staticmap"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8"
MIN_IMAGE_BYTES = 10_000
# Keep the centre of the downloaded frame so the house fills more of the working tile.
CENTRE_CROP_FRACTION = 0.85
MAX_ATTEMPTS = 3

# NSWWebImageryCold now returns {"error":{"code":499,"message":"Token Required"}}.
# These two public MapServers are the current NSW Imagery tiles (Web Mercator, 256px JPEG).
NSW_TILE_URLS = (
    "https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Imagery/MapServer/tile/{z}/{y}/{x}",
    "https://portal.spatial.nsw.gov.au/aid/tile/rest/services/NSWWebImagery/MapServer/tile/{z}/{y}/{x}",
)
# Zoom 20 is the sharpest level with its own imagery around Sydney. Zoom 21 is the
# same photograph enlarged, so a composite there looks softer. Zoom 19 is the fallback.
NSW_ZOOMS = (20, 19)
NSW_TILE_PX = 256
# 512px at zoom 20 is the native stitch (about 63 m). It is saved whole.
# A separate 2x copy is only for viewing, so a roofer can zoom in.
NSW_COMPOSITE_PX = 512
NSW_DISPLAY_SCALE = 2
NSW_CROSS_YELLOW = (255, 214, 0)
NSW_CROSS_ARM = 16
NSW_CROSS_WIDTH = 2
NSW_VIEW_PX = NSW_COMPOSITE_PX * NSW_DISPLAY_SCALE
# Google Static Maps is cropped to about 85% of a 1200 or 1280 px frame, so the
# saved file lands near 1020 px. That size is never an NSW Imagery tile.
GOOGLE_CROP_MIN = 1000
GOOGLE_CROP_MAX = 1100
# A native tile stays sharper than a half-size copy enlarged back to full size.
SOFT_DETAIL_RATIO = 1.35
NSW_POSTCODE_MIN = 2000
NSW_POSTCODE_MAX = 2999

# Public QLD aerial imagery. The cache stops at level 20; exportImage can be
# finer where a project was captured at 5–10 cm.
QLD_IMAGE_SERVER = (
    "https://spatial-img.information.qld.gov.au/arcgis/rest/services/"
    "Basemaps/LatestStateProgram_AllUsers/ImageServer"
)
QLD_TILE_URL = QLD_IMAGE_SERVER + "/tile/{z}/{y}/{x}"
QLD_GROUND_M = 36
QLD_PIXEL_SIZES_M = (0.075, 0.15)
QLD_COARSE_FACTOR = 4
QLD_DETAIL_RATIO = 1.15
QLD_MAX_COARSE_M = 0.35
QLD_CACHE_ZOOM = 20
QLD_POSTCODE_MIN = 4000
QLD_POSTCODE_MAX = 4999

_KEY_PATTERN = re.compile(r"([?&]key=)[^&\s]+")


def fetch_all(
    addresses: list[Address],
    *,
    api_key: str,
    force: bool = False,
    delay_seconds: float = 0.0,
    source: str = "auto",
) -> tuple[int, int, int]:
    """Return (saved, skipped, failed) counts."""
    ensure_data_dirs()
    log_imagery_source(addresses, source)
    saved = skipped = failed = 0
    with httpx.Client(timeout=40.0, follow_redirects=True) as client:
        for index, address in enumerate(addresses):
            if index and delay_seconds:
                time.sleep(delay_seconds)
            outcome = fetch_one(client, address, api_key=api_key, force=force, source=source)
            if outcome == "saved":
                saved += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
    return saved, skipped, failed


def is_qld_address(address: Address) -> bool:
    """QLD comes from the address state, or from postcode 4000–4999 when the state is blank."""
    state = (address.state or "").strip().upper()
    if state:
        return state == "QLD"
    postcode = (address.postcode or "").strip()
    return postcode.isdigit() and QLD_POSTCODE_MIN <= int(postcode) <= QLD_POSTCODE_MAX


def is_nsw_address(address: Address) -> bool:
    """NSW comes from the address state, or from postcode 2000–2999 when the state is blank."""
    state = (address.state or "").strip().upper()
    if state:
        return state == "NSW"
    postcode = (address.postcode or "").strip()
    return postcode.isdigit() and NSW_POSTCODE_MIN <= int(postcode) <= NSW_POSTCODE_MAX


def resolve_imagery_source(address: Address, source: str = "auto") -> str:
    """NSW addresses always use NSW Imagery, including an explicit google or qld request."""
    if is_nsw_address(address):
        return "nsw"
    if source in {"nsw", "qld", "google"}:
        return source
    if is_qld_address(address):
        return "qld"
    return "google"


def log_imagery_source(addresses: list[Address], source: str = "auto") -> None:
    """Say which imagery service this batch will call before any tile is downloaded."""
    counts = {"nsw": 0, "qld": 0, "google": 0}
    for address in addresses:
        counts[resolve_imagery_source(address, source)] += 1
    logger.info(
        "Imagery source check: %s address(es), selection=%s. "
        "NSW Imagery %s (Google fallback off). QLD Imagery %s. Google Static Maps %s.",
        len(addresses),
        source,
        counts["nsw"],
        counts["qld"],
        counts["google"],
    )


def _is_google_crop(width: int, height: int) -> bool:
    """A Static Maps centre crop is about 1020 or 1088 px. 512 and 1024 are NSW sizes."""
    def side_is_google(side: int) -> bool:
        if side in {NSW_COMPOSITE_PX, NSW_VIEW_PX}:
            return False
        return GOOGLE_CROP_MIN <= side <= GOOGLE_CROP_MAX

    return side_is_google(width) or side_is_google(height)


def _has_centre_cross(image: Image.Image) -> bool:
    """True when the yellow crosshair is still on the address point."""
    rgb = image.convert("RGB")
    cx = rgb.width // 2
    cy = rgb.height // 2

    def yellow(x: int, y: int) -> bool:
        if not (0 <= x < rgb.width and 0 <= y < rgb.height):
            return False
        pixel = rgb.getpixel((x, y))
        return all(abs(int(pixel[channel]) - NSW_CROSS_YELLOW[channel]) <= 8 for channel in range(3))

    count = 0
    horizontal = False
    vertical = False
    for offset in range(-12, 13):
        if yellow(cx + offset, cy) or yellow(cx + offset, cy - 1):
            horizontal = True
            count += 1
        if yellow(cx, cy + offset) or yellow(cx - 1, cy + offset):
            vertical = True
            count += 1
    return horizontal and vertical and count >= 8


def _is_soft(image: Image.Image) -> bool:
    """True when the centre has no detail beyond a half-size copy enlarged back."""
    half = image.resize((max(1, image.width // 2), max(1, image.height // 2)), Image.Resampling.LANCZOS)
    enlarged = half.resize(image.size, Image.Resampling.LANCZOS)
    native = _edge_variance(image)
    if native <= 0:
        return True
    return native < _edge_variance(enlarged) * SOFT_DETAIL_RATIO


def nsw_tile_problem(image: Image.Image) -> str | None:
    """Why this file cannot be kept as an NSW Imagery tile. None means it passed."""
    width, height = image.size
    if _is_google_crop(width, height):
        return (
            f"soft Google-style tile {width}x{height}. "
            f"NSW Imagery tiles are {NSW_COMPOSITE_PX}x{NSW_COMPOSITE_PX}."
        )
    if width != NSW_COMPOSITE_PX or height != NSW_COMPOSITE_PX:
        return (
            f"wrong size {width}x{height}. "
            f"Expected {NSW_COMPOSITE_PX}x{NSW_COMPOSITE_PX} NSW Imagery."
        )
    if not _has_centre_cross(image):
        return "missing the yellow centre crosshair"
    if _is_soft(image):
        return "tile is soft"
    return None


def nsw_view_problem(image: Image.Image) -> str | None:
    """The viewing copy is the 1024 px enlargement with the same crosshair."""
    width, height = image.size
    if _is_google_crop(width, height):
        return f"soft Google-style view {width}x{height}. Expected {NSW_VIEW_PX}x{NSW_VIEW_PX}."
    if width != NSW_VIEW_PX or height != NSW_VIEW_PX:
        return f"wrong view size {width}x{height}. Expected {NSW_VIEW_PX}x{NSW_VIEW_PX}."
    if not _has_centre_cross(image):
        return "view is missing the yellow centre crosshair"
    return None


def saved_nsw_problem(lead_id: str) -> str | None:
    """Check the tile and the clean view. A missing view is not a failure."""
    tile = TILES_DIR / f"{lead_id}.png"
    if not tile.is_file():
        return "tile is missing"
    with Image.open(tile) as image:
        problem = nsw_tile_problem(image.convert("RGB"))
    if problem:
        return problem
    view = VIEW_BASE_DIR / f"{lead_id}.png"
    if not view.is_file():
        return None
    with Image.open(view) as image:
        problem = nsw_view_problem(image.convert("RGB"))
    if problem:
        return problem
    return None


def discard_rejected_images(lead_id: str) -> None:
    """Remove a rejected tile and the views copied from it so it cannot be graded."""
    for path in (
        TILES_DIR / f"{lead_id}.png",
        VIEW_BASE_DIR / f"{lead_id}.png",
        ANNOTATED_DIR / f"{lead_id}.png",
        ENHANCED_DIR / f"{lead_id}_enhanced.png",
    ):
        if path.is_file():
            path.unlink()


def fetch_one(
    client: httpx.Client,
    address: Address,
    *,
    api_key: str,
    force: bool,
    source: str = "auto",
) -> str:
    destination = TILES_DIR / f"{address.lead_id}.png"
    chosen = resolve_imagery_source(address, source)
    if destination.exists() and not force:
        if chosen == "nsw":
            problem = saved_nsw_problem(address.lead_id)
            if problem:
                _reject_nsw_tile(address, problem, destination)
            else:
                logger.info(
                    "Imagery for %s: source=nsw endpoint=saved NSW Imagery tile (not refetched)",
                    address.lead_id,
                )
                _, _, centre_source = map_centre(address)
                log_static_maps_usage(
                    lead_id=address.lead_id,
                    outcome="skipped_existing",
                    billed=False,
                    bytes=destination.stat().st_size,
                    http_status=None,
                    center_source=centre_source,
                    source="nsw",
                )
                return "skipped"
        else:
            logger.info("Skipping %s; tile already exists", address.lead_id)
            _, _, centre_source = map_centre(address)
            log_static_maps_usage(
                lead_id=address.lead_id,
                outcome="skipped_existing",
                billed=False,
                bytes=destination.stat().st_size,
                http_status=None,
                center_source=centre_source,
            )
            return "skipped"

    centre_lat, centre_lon, centre_source = map_centre(address)
    if chosen == "qld":
        try:
            return _fetch_qld(client, address, destination, centre_lat, centre_lon, centre_source)
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            logger.warning(
                "QLD Imagery failed for %s (%s). Falling back to Google Static Maps.",
                address.lead_id,
                exc,
            )
            return _fetch_google(client, address, destination, centre_lat, centre_lon, centre_source, api_key)
    if chosen == "nsw":
        try:
            return _fetch_nsw(client, address, destination, centre_lat, centre_lon, centre_source)
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            discard_rejected_images(address.lead_id)
            message = (
                f"NSW Imagery failed for {address.lead_id} ({address.single_line}): {exc}. "
                "Google Static Maps was not used."
            )
            logger.error(message)
            raise RuntimeError(message) from exc
    logger.info("Imagery for %s: source=google endpoint=%s", address.lead_id, STATIC_MAPS_URL)
    return _fetch_google(client, address, destination, centre_lat, centre_lon, centre_source, api_key)


def _reject_nsw_tile(address: Address, problem: str, destination: Path) -> None:
    """Delete a bad NSW tile and fetch a replacement. The bad file is not graded."""
    logger.error(
        "Imagery check failed for %s (%s): %s Discarded. Google Static Maps was not used.",
        address.lead_id,
        address.single_line,
        problem,
    )
    _, _, centre_source = map_centre(address)
    log_static_maps_usage(
        lead_id=address.lead_id,
        outcome="rejected_tile",
        billed=False,
        bytes=destination.stat().st_size if destination.is_file() else 0,
        http_status=None,
        center_source=centre_source,
        source="rejected",
        error=problem,
    )
    discard_rejected_images(address.lead_id)


def _fetch_nsw(
    client: httpx.Client,
    address: Address,
    destination: Path,
    centre_lat: float,
    centre_lon: float,
    centre_source: str,
) -> str:
    """Stitch an NSW Imagery composite centred on the address and save it as the tile."""
    last_error = "NSW Imagery returned no tile"
    for zoom in NSW_ZOOMS:
        try:
            image, endpoint = _nsw_composite(client, centre_lat, centre_lon, zoom, NSW_COMPOSITE_PX)
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            last_error = str(exc)
            logger.warning("NSW Imagery zoom %s failed for %s: %s", zoom, address.lead_id, exc)
            continue
        native = image.convert("RGB")
        marked = _draw_centre_cross(native, NSW_CROSS_ARM, NSW_CROSS_WIDTH)
        problem = nsw_tile_problem(marked)
        if problem:
            last_error = problem
            logger.error(
                "Imagery check failed for %s: %s endpoint=%s zoom=%s. Not saved.",
                address.lead_id,
                problem,
                endpoint,
                zoom,
            )
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        marked.save(destination, format="PNG")
        _save_display_copy(native, address.lead_id)
        view_path = VIEW_BASE_DIR / f"{address.lead_id}.png"
        if view_path.is_file():
            with Image.open(view_path) as view:
                view_problem = nsw_view_problem(view.convert("RGB"))
        else:
            view_problem = "view was not written"
        if view_problem:
            discard_rejected_images(address.lead_id)
            last_error = view_problem
            logger.error(
                "Imagery check failed for %s: %s endpoint=%s zoom=%s. Discarded.",
                address.lead_id,
                view_problem,
                endpoint,
                zoom,
            )
            continue
        logger.info(
            "Imagery for %s: source=nsw endpoint=%s zoom=%s",
            address.lead_id,
            endpoint,
            zoom,
        )
        log_static_maps_usage(
            lead_id=address.lead_id,
            outcome="saved",
            billed=False,
            bytes=destination.stat().st_size,
            downloaded_bytes=native.width * native.height,
            http_status=200,
            center_source=centre_source,
            zoom=zoom,
            size=f"{native.width}x{native.height}",
            scale=1,
            maptype="nsw_imagery",
            source="nsw",
            endpoint=endpoint,
        )
        logger.info(
            "Saved %s from NSW Imagery zoom %s (%sx%s native, %sx display, centre=%s)",
            destination.name,
            zoom,
            native.width,
            native.height,
            NSW_DISPLAY_SCALE,
            centre_source,
        )
        return "saved"
    raise RuntimeError(last_error)


def _draw_centre_cross(image: Image.Image, arm: int, width: int) -> Image.Image:
    """Thin yellow cross at the address point. The source image is not modified."""
    marked = image.copy()
    draw = ImageDraw.Draw(marked)
    cx = marked.width // 2
    cy = marked.height // 2
    draw.line((cx - arm, cy, cx + arm, cy), fill=NSW_CROSS_YELLOW, width=width)
    draw.line((cx, cy - arm, cx, cy + arm), fill=NSW_CROSS_YELLOW, width=width)
    return marked


def _save_display_copy(native: Image.Image, lead_id: str) -> None:
    """2x viewing copy. The cross is drawn after the enlargement so it stays thin. The tile is unchanged."""
    size = (native.width * NSW_DISPLAY_SCALE, native.height * NSW_DISPLAY_SCALE)
    enlarged = native.resize(size, Image.Resampling.LANCZOS)
    marked = _draw_centre_cross(enlarged, NSW_CROSS_ARM * NSW_DISPLAY_SCALE, NSW_CROSS_WIDTH)
    save_view_base(lead_id, marked)


def _fetch_qld(
    client: httpx.Client,
    address: Address,
    destination: Path,
    centre_lat: float,
    centre_lon: float,
    centre_source: str,
) -> str:
    """Save a tight QLD aerial tile centred on the address, or raise so Google can be used."""
    logger.info("Imagery for %s: source=qld endpoint=%s", address.lead_id, QLD_IMAGE_SERVER)
    native, pixel_m = _qld_best_image(client, centre_lat, centre_lon)
    marked = _draw_centre_cross(native, NSW_CROSS_ARM, NSW_CROSS_WIDTH)
    destination.parent.mkdir(parents=True, exist_ok=True)
    marked.save(destination, format="PNG")
    _save_display_copy(native, address.lead_id)
    log_static_maps_usage(
        lead_id=address.lead_id,
        outcome="saved",
        billed=False,
        bytes=destination.stat().st_size,
        http_status=200,
        center_source=centre_source,
        zoom=0,
        size=f"{native.width}x{native.height}",
        scale=1,
        maptype="qld_imagery",
        source="qld",
        pixel_m=round(pixel_m, 3),
    )
    logger.info(
        "Saved %s from QLD Imagery (%sx%s at %.3f m/px, centre=%s)",
        destination.name,
        native.width,
        native.height,
        pixel_m,
        centre_source,
    )
    return "saved"


def _qld_best_image(client: httpx.Client, lat: float, lon: float) -> tuple[Image.Image, float]:
    """Finest export that still contains real detail. Level 20 tiles are the backup."""
    last_error = "QLD Imagery did not return a usable image"
    for pixel_m in QLD_PIXEL_SIZES_M:
        try:
            fine = _qld_export(client, lat, lon, QLD_GROUND_M, pixel_m)
            coarse = _qld_export(client, lat, lon, QLD_GROUND_M, pixel_m * QLD_COARSE_FACTOR)
        except (OSError, RuntimeError, httpx.HTTPError) as exc:
            last_error = str(exc)
            logger.warning("QLD export at %.3f m/px failed: %s", pixel_m, exc)
            continue
        if _has_extra_detail(fine, coarse):
            return fine, pixel_m
        last_error = f"QLD Imagery at {pixel_m:.3f} m/px is only an enlargement of coarser imagery"
    try:
        cached, pixel_m = _qld_cached_window(client, lat, lon)
    except (OSError, RuntimeError, httpx.HTTPError) as exc:
        raise RuntimeError(last_error) from exc
    if pixel_m > QLD_MAX_COARSE_M:
        raise RuntimeError(
            f"QLD Imagery is {pixel_m:.2f} m/px here, which is too coarse for a roof."
        )
    return cached, pixel_m


def _qld_export(client: httpx.Client, lat: float, lon: float, ground_m: float, pixel_m: float) -> Image.Image:
    """One tight square from exportImage. ground_m is the width on the ground."""
    size = max(64, min(2048, round(ground_m / pixel_m)))
    x, y = _web_mercator_metres(lat, lon)
    half = (ground_m / math.cos(math.radians(lat))) / 2
    bbox = f"{x - half},{y - half},{x + half},{y + half}"
    response = _qld_get(
        client,
        f"{QLD_IMAGE_SERVER}/exportImage",
        {
            "bbox": bbox,
            "bboxSR": "3857",
            "imageSR": "3857",
            "size": f"{size},{size}",
            "format": "png",
            "f": "image",
            "adjustAspectRatio": "false",
        },
    )
    return _open_raster(response.content)


def _qld_cached_window(client: httpx.Client, lat: float, lon: float) -> tuple[Image.Image, float]:
    """Centre crop of the level-20 tile cache, about QLD_GROUND_M across."""
    pixel_m = math.cos(math.radians(lat)) * 2 * math.pi * 6378137 / (NSW_TILE_PX * 2**QLD_CACHE_ZOOM)
    pixels = max(64, round(QLD_GROUND_M / pixel_m))
    centre_x, centre_y = _web_mercator_pixel(lat, lon, QLD_CACHE_ZOOM)
    half = pixels / 2
    left = centre_x - half
    top = centre_y - half
    tile_x0 = math.floor(left / NSW_TILE_PX)
    tile_y0 = math.floor(top / NSW_TILE_PX)
    tile_x1 = math.floor((left + pixels - 1) / NSW_TILE_PX)
    tile_y1 = math.floor((top + pixels - 1) / NSW_TILE_PX)
    canvas = Image.new("RGB", ((tile_x1 - tile_x0 + 1) * NSW_TILE_PX, (tile_y1 - tile_y0 + 1) * NSW_TILE_PX))
    for tile_x in range(tile_x0, tile_x1 + 1):
        for tile_y in range(tile_y0, tile_y1 + 1):
            url = QLD_TILE_URL.format(z=QLD_CACHE_ZOOM, y=tile_y, x=tile_x)
            response = _qld_get(client, url, None)
            with _open_raster(response.content) as tile:
                canvas.paste(tile.convert("RGB"), ((tile_x - tile_x0) * NSW_TILE_PX, (tile_y - tile_y0) * NSW_TILE_PX))
    crop_left = int(round(left - tile_x0 * NSW_TILE_PX))
    crop_top = int(round(top - tile_y0 * NSW_TILE_PX))
    return canvas.crop((crop_left, crop_top, crop_left + pixels, crop_top + pixels)), pixel_m


def _qld_get(client: httpx.Client, url: str, params: dict[str, str] | None) -> httpx.Response:
    last_error = "QLD Imagery request failed"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as exc:
            last_error = str(exc)
            time.sleep(attempt)
            continue
        if response.status_code in {429, 500, 502, 503, 504} and attempt < MAX_ATTEMPTS:
            last_error = f"HTTP {response.status_code}"
            time.sleep(attempt)
            continue
        if response.status_code == 200 and _is_raster(response.content):
            return response
        last_error = f"HTTP {response.status_code}"
        break
    raise RuntimeError(last_error)


def _open_raster(body: bytes) -> Image.Image:
    if not _is_raster(body):
        raise RuntimeError("QLD Imagery response was not an image")
    return Image.open(BytesIO(body)).convert("RGB")


def _has_extra_detail(fine: Image.Image, coarse: Image.Image) -> bool:
    """True when the finer export is sharper than an enlargement of the coarse one."""
    reduced = fine.resize(coarse.size, Image.Resampling.LANCZOS)
    return _edge_variance(reduced) >= _edge_variance(coarse) * QLD_DETAIL_RATIO


def _edge_variance(image: Image.Image) -> float:
    grey = image.convert("L")
    side = min(grey.width, grey.height) // 2
    left = (grey.width - side) // 2
    top = (grey.height - side) // 2
    centre = grey.crop((left, top, left + side, top + side))
    return float(ImageStat.Stat(centre.filter(ImageFilter.FIND_EDGES)).var[0])


def _web_mercator_metres(lat: float, lon: float) -> tuple[float, float]:
    radius = 6378137.0
    x = math.radians(lon) * radius
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * radius
    return x, y


def _nsw_composite(client: httpx.Client, lat: float, lon: float, zoom: int, pixels: int) -> tuple[Image.Image, str]:
    """Download the Web Mercator tiles that cover a square centred on lat/lon and crop to it."""
    centre_x, centre_y = _web_mercator_pixel(lat, lon, zoom)
    half = pixels / 2
    left = centre_x - half
    top = centre_y - half
    tile_x0 = math.floor(left / NSW_TILE_PX)
    tile_y0 = math.floor(top / NSW_TILE_PX)
    tile_x1 = math.floor((left + pixels - 1) / NSW_TILE_PX)
    tile_y1 = math.floor((top + pixels - 1) / NSW_TILE_PX)
    width = (tile_x1 - tile_x0 + 1) * NSW_TILE_PX
    height = (tile_y1 - tile_y0 + 1) * NSW_TILE_PX
    canvas = Image.new("RGB", (width, height))
    endpoints: list[str] = []
    for tile_x in range(tile_x0, tile_x1 + 1):
        for tile_y in range(tile_y0, tile_y1 + 1):
            body, endpoint = _download_nsw_tile(client, zoom, tile_x, tile_y)
            if endpoint not in endpoints:
                endpoints.append(endpoint)
            with Image.open(BytesIO(body)) as tile:
                canvas.paste(
                    tile.convert("RGB"),
                    ((tile_x - tile_x0) * NSW_TILE_PX, (tile_y - tile_y0) * NSW_TILE_PX),
                )
    crop_left = int(round(left - tile_x0 * NSW_TILE_PX))
    crop_top = int(round(top - tile_y0 * NSW_TILE_PX))
    crop = canvas.crop((crop_left, crop_top, crop_left + pixels, crop_top + pixels))
    return crop, " and ".join(endpoints)


def _web_mercator_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    scale = NSW_TILE_PX * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * scale
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * scale
    return x, y


def _download_nsw_tile(client: httpx.Client, zoom: int, tile_x: int, tile_y: int) -> tuple[bytes, str]:
    last_error = "NSW Imagery tile was not an image"
    for pattern in NSW_TILE_URLS:
        url = pattern.format(z=zoom, y=tile_y, x=tile_x)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                time.sleep(attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < MAX_ATTEMPTS:
                last_error = f"HTTP {response.status_code}"
                time.sleep(attempt)
                continue
            if response.status_code == 200 and _is_raster(response.content):
                return response.content, pattern
            last_error = f"HTTP {response.status_code}"
            break
    raise RuntimeError(last_error)


def _is_raster(body: bytes) -> bool:
    return len(body) > 500 and (body.startswith(JPEG_MAGIC) or body.startswith(PNG_MAGIC))


def _fetch_google(
    client: httpx.Client,
    address: Address,
    destination: Path,
    centre_lat: float,
    centre_lon: float,
    centre_source: str,
    api_key: str,
) -> str:
    params = tile_request_params(centre_lat, centre_lon, api_key=api_key)

    last_error = "unknown error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.get(STATIC_MAPS_URL, params=params)
        except httpx.HTTPError as exc:
            last_error = redact(str(exc), api_key)
            logger.warning("Tile request failed for %s (attempt %s): %s", address.lead_id, attempt, last_error)
            time.sleep(attempt)
            continue

        if response.status_code in {429, 500, 502, 503, 504} and attempt < MAX_ATTEMPTS:
            last_error = f"HTTP {response.status_code}"
            logger.warning("Tile request for %s returned %s; retrying", address.lead_id, response.status_code)
            time.sleep(attempt)
            continue

        if response.status_code != 200:
            last_error = f"HTTP {response.status_code}: {_error_body(response, api_key)}"
            break

        content_type = response.headers.get("content-type", "")
        body = response.content
        if not content_type.startswith("image/") or not body.startswith(PNG_MAGIC):
            last_error = f"expected a PNG image, received {content_type or 'an empty body'}"
            break
        if len(body) < MIN_IMAGE_BYTES:
            last_error = f"image is only {len(body)} bytes; refusing a likely error tile"
            log_static_maps_usage(
                lead_id=address.lead_id,
                outcome="rejected_small_image",
                billed=True,
                bytes=len(body),
                http_status=response.status_code,
                error=last_error,
                center_source=centre_source,
            )
            logger.error("%s: %s", address.lead_id, last_error)
            return "failed"

        with Image.open(BytesIO(body)) as downloaded:
            downloaded_size = downloaded.size
        # The crop keeps the centre pixels as Google sent them. It does not resize,
        # so the saved tile is not an enlargement of a smaller image.
        cropped = crop_centre(body, CENTRE_CROP_FRACTION)
        destination.write_bytes(cropped)
        with Image.open(BytesIO(cropped)) as view:
            save_view_base(address.lead_id, view.convert("RGB"))
        log_static_maps_usage(
            lead_id=address.lead_id,
            outcome="saved",
            billed=True,
            bytes=len(cropped),
            downloaded_bytes=len(body),
            http_status=response.status_code,
            center_source=centre_source,
            source="google",
        )
        logger.info(
            "Saved %s (%s bytes after centre crop, Google returned %sx%s, centre=%s)",
            destination.name,
            f"{len(cropped):,}",
            downloaded_size[0],
            downloaded_size[1],
            centre_source,
        )
        return "saved"

    log_static_maps_usage(
        lead_id=address.lead_id,
        outcome="failed",
        billed=False,
        bytes=0,
        http_status=None,
        error=last_error,
        center_source=centre_source,
    )
    logger.error("Could not fetch %s: %s", address.lead_id, last_error)
    return "failed"


def crop_centre(png_bytes: bytes, fraction: float = CENTRE_CROP_FRACTION) -> bytes:
    """Return the centre portion of a tile. 0.85 keeps 85% of the width and height."""
    with Image.open(BytesIO(png_bytes)) as image:
        frame = image.convert("RGB")
        width, height = frame.size
        crop_w = max(1, int(round(width * fraction)))
        crop_h = max(1, int(round(height * fraction)))
        left = (width - crop_w) // 2
        top = (height - crop_h) // 2
        cropped = frame.crop((left, top, left + crop_w, top + crop_h))
        buffer = BytesIO()
        cropped.save(buffer, format="PNG")
        return buffer.getvalue()


def tile_request_params(lat: float, lon: float, *, api_key: str, zoom: int = TILE_ZOOM, size: str = TILE_SIZE) -> dict[str, str]:
    """Static Maps parameters. The centre is the address (or roof centroid) point."""
    return {
        "center": f"{lat:.7f},{lon:.7f}",
        "zoom": str(zoom),
        "size": size,
        "scale": str(TILE_SCALE),
        "maptype": TILE_MAPTYPE,
        "format": TILE_FORMAT,
        "key": api_key,
    }


def map_centre(address: Address) -> tuple[float, float, str]:
    """Choose the Static Maps centre for one lead.

    Production runs should write Geoscape building-footprint centroids into
    roof_centroid_lat and roof_centroid_lon. When both cells are populated,
    the tile is centred on the roof rather than the street address. A CSV
    with only lat/lon keeps using that address point.
    """
    if address.roof_centroid_lat is not None and address.roof_centroid_lon is not None:
        return address.roof_centroid_lat, address.roof_centroid_lon, "roof_centroid"
    return address.lat, address.lon, "address"


def redact(text: str, api_key: str) -> str:
    cleaned = _KEY_PATTERN.sub(r"\1REDACTED", text)
    if api_key:
        cleaned = cleaned.replace(api_key, "REDACTED")
    return cleaned


def _error_body(response: httpx.Response, api_key: str) -> str:
    text = response.text.strip().replace("\n", " ")
    return redact(text[:300], api_key)


MAX_SATELLITE_ZOOM = 21
MAX_STATIC_SCALE = 2
HIGHRES_SIZE = "640x640"
HIGHRES_LIMIT_NOTE = (
    "Closer image not available: Google Static Maps is already at maximum satellite zoom 21 and scale 2. "
    "The request is saved and the original tile is unchanged."
)


def closer_tile_unavailable_reason() -> str | None:
    """The ordinary tile already uses the highest zoom and scale Static Maps allows."""
    if TILE_ZOOM >= MAX_SATELLITE_ZOOM and TILE_SCALE >= MAX_STATIC_SCALE:
        return HIGHRES_LIMIT_NOTE
    return None


def fetch_closer_tile(
    lead_id: str,
    lat: float | None,
    lon: float | None,
    *,
    api_key: str,
) -> tuple[str, str]:
    """Save a closer satellite image beside the original tile. The original file is not replaced."""
    limit = closer_tile_unavailable_reason()
    if limit:
        return "unavailable", limit
    if lat is None or lon is None:
        return "unavailable", "The high-res request is saved. This lead has no coordinates, so a closer image was not fetched."
    destination = TILES_DIR / f"{lead_id}_highres.png"
    zoom = min(MAX_SATELLITE_ZOOM, TILE_ZOOM + 1)
    params = tile_request_params(lat, lon, api_key=api_key, zoom=zoom, size=HIGHRES_SIZE)
    try:
        with httpx.Client(timeout=40.0) as client:
            response = client.get(STATIC_MAPS_URL, params=params)
    except httpx.HTTPError as exc:
        return "failed", f"The high-res request is saved. The closer image was not downloaded: {redact(str(exc), api_key)}"
    if response.status_code != 200 or not response.content.startswith(PNG_MAGIC):
        return "failed", "The high-res request is saved. Google did not return a closer satellite image."
    destination.write_bytes(response.content)
    log_static_maps_usage(
        lead_id=lead_id,
        outcome="saved_highres",
        billed=True,
        bytes=len(response.content),
        http_status=response.status_code,
        center_source="address",
    )
    return "saved", f"Saved a zoom {zoom} image beside the original tile."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch satellite roof tiles for the 4218 pilot.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Address CSV")
    parser.add_argument("--force", action="store_true", help="Refetch tiles that are already saved")
    parser.add_argument(
        "--source",
        choices=("auto", "nsw", "qld", "google"),
        default="auto",
        help="NSW addresses always use NSW Imagery. auto uses QLD Imagery for QLD and Google elsewhere. NSW never falls back to Google",
    )
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to wait between requests")
    parser.add_argument(
        "--allow-any-postcode",
        action="store_true",
        help="Include addresses outside the 4218 pilot",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    try:
        api_key = require_env("GOOGLE_MAPS_API_KEY")
        addresses = load_addresses(args.input, allow_any_postcode=args.allow_any_postcode)
        saved, skipped, failed = fetch_all(
            addresses,
            api_key=api_key,
            force=args.force,
            delay_seconds=args.delay,
            source=args.source,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Static Maps usage: %s saved, %s already on disk, %s failed, %s lead(s) in the file",
        saved,
        skipped,
        failed,
        len(addresses),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
