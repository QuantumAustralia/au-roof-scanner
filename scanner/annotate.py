"""Draw the yellow defect box and the short defect label on view images."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

from scanner.config import ANNOTATED_DIR, GRADES_DIR, TILES_DIR, VIEW_BASE_DIR

YELLOW = (255, 214, 0)
STROKE = 8  # sized for a tile about 1000px wide
MIN_SPAN = 10  # 1% of the 0–1000 grid; smaller boxes are not drawn
_LABEL_FONT = (
    Path(r"C:\Windows\Fonts\arialbd.ttf"),
    Path(r"C:\Windows\Fonts\arial.ttf"),
    Path(r"C:\Windows\Fonts\segoeui.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
)
# Short labels for the view image. Wear scores are not shown.
_DEFECT_LABELS = {
    "cracked_or_slipped_tiles": "Missing tiles",
    "failed_ridge_capping": "Failed ridge",
    "rust_or_corrosion": "Rust",
    "loose_or_lifting_sheets": "Lifting sheets",
    "biological_growth": "Heavy moss",
    "ponding": "Ponding",
    "failed_flashing": "Cracked flashing",
    "general_ageing": "General ageing",
    "other": "Other defect",
}
# Cleaning is separate from structural wear. These labels are drawn when the
# cleaning score is high enough to count as a cleaning lead.
_CLEANING_LABELS = {
    "heavy_moss_lichen": "Heavy moss",
    "algae_staining": "Algae staining",
    "dirt_grime_soiling": "Dirt and grime",
    "organic_growth": "Organic growth",
}
_NO_DEFECT = {"", "none", "none_or_cosmetic"}


def stroke_width(image_width: int) -> int:
    """Thinner on the smaller NSW crops. A 1000px tile stays at 8px; a 174px tile is 2px."""
    return max(2, round(STROKE * image_width / 1000))


def box_is_drawable(box: list[int]) -> bool:
    if len(box) != 4:
        return False
    ymin, xmin, ymax, xmax = box
    if any(value < 0 or value > 1000 for value in box):
        return False
    return (ymax - ymin) >= MIN_SPAN and (xmax - xmin) >= MIN_SPAN


def draw_target_box(source: Path, destination: Path, box: list[int]) -> None:
    """Save a copy of source with a yellow rectangle. The tile itself is left unchanged."""
    with Image.open(source) as image:
        rgb = _draw_box(image.convert("RGB"), box)
        destination.parent.mkdir(parents=True, exist_ok=True)
        rgb.save(destination, format="PNG")


def _draw_box(image: Image.Image, box: list[int]) -> Image.Image:
    ymin, xmin, ymax, xmax = box
    marked = image.copy()
    width, height = marked.size
    left = int(round(xmin / 1000 * width))
    top = int(round(ymin / 1000 * height))
    right = int(round(xmax / 1000 * width))
    bottom = int(round(ymax / 1000 * height))
    draw = ImageDraw.Draw(marked)
    draw.rectangle((left, top, right, bottom), outline=YELLOW, width=stroke_width(width))
    return marked


def defect_caption(grade: dict) -> str:
    """One defect, or two joined as 'Rust · Missing tiles' when both were graded."""
    labels = defect_labels(grade)
    return " · ".join(labels[:2])


def defect_labels(grade: dict) -> list[str]:
    """Up to two distinct defects. A second label is added only when the grade names one."""
    if grade.get("outcome") == "not_assessable":
        return []
    labels: list[str] = []
    # A rejected repair finding does not get a structural caption. A cleaning
    # score still does, because that score is separate from the repair check.
    if grade.get("outcome") != "false_positive":
        second = grade.get("pass2")
        if isinstance(second, dict) and not second.get("false_positive"):
            _add_labels(labels, _short_labels(str(second.get("verified_defect") or "")))
        primary = str((grade.get("pass1") or {}).get("primary_defect") or "")
        primary_label = _DEFECT_LABELS.get(primary, "")
        if primary_label:
            _add_labels(labels, [primary_label])
        secondary = str((grade.get("pass1") or {}).get("secondary_defect") or "")
        _add_labels(labels, _short_labels(secondary))
        if labels and len(labels) < 2:
            text = str((grade.get("pass1") or {}).get("observation") or "")
            if isinstance(second, dict):
                text = f"{text} {second.get('summary') or ''}"
            _add_labels(labels, _phrases_in(text))
    _add_labels(labels, _cleaning_labels(grade))
    return labels[:2]


def draw_defect_caption(image: Image.Image, text: str) -> Image.Image:
    """Yellow label in the top-left. The source image is not modified."""
    marked = image.copy()
    if not text.strip():
        return marked
    draw = ImageDraw.Draw(marked)
    padding = max(12, marked.width // 64)
    # Stay left of the centre cross.
    max_width = max(40, marked.width // 2 - padding - 48)
    # Large enough to read on the full tile. The old size shrank "Heavy Moss" to a speck.
    font_size = max(64, marked.width // 14)
    font = _font(font_size)
    while font_size > 48 and draw.textlength(text, font=font) > max_width:
        font_size -= 2
        font = _font(font_size)
    stroke = max(2, font_size // 14)
    draw.text(
        (padding, padding),
        text,
        font=font,
        fill=YELLOW,
        stroke_width=stroke,
        stroke_fill=(20, 20, 20),
    )
    return marked


def save_view_base(lead_id: str, image: Image.Image) -> None:
    """Store the clean view, then write the labelled copy. The tile file is not touched."""
    VIEW_BASE_DIR.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(VIEW_BASE_DIR / f"{lead_id}.png", format="PNG")
    publish_view(lead_id)


def publish_view(lead_id: str, defect_box: list[int] | None = None) -> Path | None:
    """Write the view copy from the clean base, with the defect label when one was graded."""
    base = _ensure_view_base(lead_id)
    if base is None:
        return None
    with Image.open(base) as image:
        frame = image.convert("RGB")
    if defect_box and box_is_drawable(defect_box):
        frame = _draw_box(frame, defect_box)
    grade = _read_grade(lead_id)
    caption = defect_caption(grade)
    if caption:
        frame = draw_defect_caption(frame, caption)
    destination = ANNOTATED_DIR / f"{lead_id}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A fresh PNG. Do not copy EXIF, XMP, or text chunks from the source image.
    # The only text kept is the defect caption, which does not name the address.
    details = PngInfo()
    if caption:
        details.add_text("defects", caption)
    frame.save(destination, format="PNG", pnginfo=details)
    return destination


def _ensure_view_base(lead_id: str) -> Path | None:
    """Keep the first clean view. A later label is drawn onto a copy of this file."""
    base = VIEW_BASE_DIR / f"{lead_id}.png"
    if base.is_file():
        return base
    annotated = ANNOTATED_DIR / f"{lead_id}.png"
    tile = TILES_DIR / f"{lead_id}.png"
    source = annotated if annotated.is_file() else tile if tile.is_file() else None
    if source is None:
        return None
    VIEW_BASE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, base)
    return base


def _read_grade(lead_id: str) -> dict:
    path = GRADES_DIR / f"{lead_id}.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _cleaning_labels(grade: dict) -> list[str]:
    """Short cleaning label when the cleaning score is 3 or higher."""
    pass1 = grade.get("pass1")
    if not isinstance(pass1, dict):
        return []
    try:
        score = int(pass1.get("cleaning_score"))
    except (TypeError, ValueError):
        return []
    if score < 3:
        return []
    label = _CLEANING_LABELS.get(str(pass1.get("primary_cleaning_defect") or ""), "")
    return [label] if label else []


def _add_labels(labels: list[str], extra: list[str]) -> None:
    for label in extra:
        if label and not _already_listed(label, labels):
            labels.append(label)
        if len(labels) >= 2:
            return


def _phrases_in(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for needle, label in (
        ("missing tile", "Missing tiles"),
        ("broken tile", "Missing tiles"),
        ("cracked tile", "Missing tiles"),
        ("slipped tile", "Missing tiles"),
        ("displaced tile", "Missing tiles"),
        ("rust", "Rust"),
        ("corros", "Rust"),
        ("heavy moss", "Heavy moss"),
        ("lichen", "Heavy moss"),
        ("moss", "Heavy moss"),
        ("peeling", "Faded coating"),
        ("faded coating", "Faded coating"),
        ("coating", "Faded coating"),
        ("flashing", "Cracked flashing"),
        ("ridge", "Failed ridge"),
        ("sagging", "Sagging"),
        ("ponding", "Ponding"),
    ):
        start = 0
        while True:
            index = lowered.find(needle, start)
            if index < 0:
                break
            before = lowered[max(0, index - 24):index]
            if needle == "moss" and before.endswith("light "):
                start = index + len(needle)
                continue
            if re.search(r"(no|not|without|none|nor|free of)\W*$", before):
                start = index + len(needle)
                continue
            if not _already_listed(label, found):
                found.append(label)
            break
    return found


def _short_labels(text: str) -> list[str]:
    raw = " ".join(text.split())
    if raw.lower() in _NO_DEFECT or len(raw) > 48 or "." in raw:
        return []
    labels: list[str] = []
    for part in raw.split("·"):
        label = _label_part(part.strip())
        if label and label not in labels:
            labels.append(label)
    return labels


def _label_part(part: str) -> str:
    """Turn a graded defect into a short label. Codes and sentences are not shown."""
    if not part:
        return ""
    key = part.lower().replace(" ", "_").replace("-", "_")
    if key in _DEFECT_LABELS:
        return _DEFECT_LABELS[key]
    if part in _DEFECT_LABELS.values():
        return part
    lowered = part.lower().replace("_", " ")
    for needle, label in (
        ("rust", "Rust"),
        ("corros", "Rust"),
        ("missing tile", "Missing tiles"),
        ("broken tile", "Missing tiles"),
        ("cracked tile", "Missing tiles"),
        ("slipped tile", "Missing tiles"),
        ("moss", "Heavy moss"),
        ("lichen", "Heavy moss"),
        ("coating", "Faded coating"),
        ("peel", "Faded coating"),
        ("flash", "Cracked flashing"),
        ("ridge", "Failed ridge"),
        ("sag", "Sagging"),
        ("pond", "Ponding"),
    ):
        if needle in lowered:
            return label
    words = part.split()
    if "_" in part or len(words) > 2 or len(part) > 24:
        return ""
    return part[:1].upper() + part[1:]


def _already_listed(label: str, labels: list[str]) -> bool:
    needle = label.lower()
    return any(needle in item.lower() or item.lower() in needle for item in labels)


def _font(size: int) -> ImageFont.ImageFont:
    for path in _LABEL_FONT:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()
