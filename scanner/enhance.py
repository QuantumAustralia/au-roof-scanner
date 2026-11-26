"""Polish a yellow-box image for cleared leads.

Real-ESRGAN runs locally and is the default. Edit upscale_factor in
enhance_config.json to change the scale. If Real-ESRGAN cannot run, the
previous Pillow sharpen-and-resize path is used instead. Neither path
modifies the original tile or the annotated image.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

if __name__ == "__main__" and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scanner.config import ANNOTATED_DIR, ENHANCED_DIR, ROOT

logger = logging.getLogger(__name__)

# Change upscale_factor in enhance_config.json, not here.
ENHANCE_CONFIG_PATH = ROOT / "enhance_config.json"
WEIGHTS_DIR = ROOT / "models"

_WEIGHTS = {
    2: (
        "RealESRGAN_x2plus.pth",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
    ),
    4: (
        "RealESRGAN_x4plus.pth",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
    ),
}


def load_enhance_config(path: Path = ENHANCE_CONFIG_PATH) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Enhancement config not found: {path}")
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    for key in (
        "sharpen_percent",
        "sharpen_radius",
        "sharpen_threshold",
        "contrast",
        "clarity_percent",
        "clarity_radius",
        "clarity_threshold",
        "upscale_factor",
    ):
        if key not in config:
            raise ValueError(f"{path} is missing {key}.")
    factor = float(config["upscale_factor"])
    if factor <= 0 or factor > 4:
        raise ValueError("upscale_factor must be greater than 0 and at most 4.")
    return config


def enhance_annotated(source: Path, destination: Path) -> Path:
    """Upscale a copy of an annotated tile. The source file is only read."""
    if not source.is_file():
        raise FileNotFoundError(f"Annotated image not found: {source}")
    config = load_enhance_config()
    try:
        width, height = _enhance_realesrgan(source, destination, config)
        logger.info("Real-ESRGAN enhanced %s -> %s (%sx%s)", source.name, destination, width, height)
    except Exception as exc:  # noqa: BLE001 — Pillow remains available when the model cannot run
        logger.warning("Real-ESRGAN failed (%s). Falling back to the Pillow polish.", exc)
        width, height = _enhance_pillow(source, destination, config)
        logger.info("Pillow enhanced %s -> %s (%sx%s)", source.name, destination, width, height)
    return destination


def _enhance_realesrgan(source: Path, destination: Path, config: dict) -> tuple[int, int]:
    import cv2
    import torch
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer

    factor = float(config["upscale_factor"])
    model_scale = 2 if factor <= 2 else 4
    weights = _weights_path(model_scale)
    network = RRDBNet(
        num_in_ch=3,
        num_out_ch=3,
        num_feat=64,
        num_block=23,
        num_grow_ch=32,
        scale=model_scale,
    )
    upsampler = RealESRGANer(
        scale=model_scale,
        model_path=str(weights),
        model=network,
        tile=256,
        tile_pad=10,
        pre_pad=0,
        half=torch.cuda.is_available(),
    )
    image = cv2.imread(str(source), cv2.IMREAD_COLOR)
    if image is None:
        raise OSError(f"Could not read annotated image: {source}")
    output, _ = upsampler.enhance(image, outscale=factor)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(destination), output):
        raise OSError(f"Could not write enhanced image: {destination}")
    height, width = output.shape[:2]
    return width, height


def _weights_path(model_scale: int) -> Path:
    filename, url = _WEIGHTS[model_scale]
    path = WEIGHTS_DIR / filename
    if path.is_file() and path.stat().st_size > 1_000_000:
        return path
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading Real-ESRGAN weights to %s", path)
    urllib.request.urlretrieve(url, path)
    return path


def _enhance_pillow(source: Path, destination: Path, config: dict) -> tuple[int, int]:
    with Image.open(source) as image:
        polished = image.convert("RGB")
        polished = polished.filter(
            ImageFilter.UnsharpMask(
                radius=float(config["sharpen_radius"]),
                percent=int(config["sharpen_percent"]),
                threshold=int(config["sharpen_threshold"]),
            )
        )
        polished = ImageEnhance.Contrast(polished).enhance(float(config["contrast"]))
        polished = polished.filter(
            ImageFilter.UnsharpMask(
                radius=float(config["clarity_radius"]),
                percent=int(config["clarity_percent"]),
                threshold=int(config["clarity_threshold"]),
            )
        )
        factor = float(config["upscale_factor"])
        if factor != 1:
            size = (round(polished.width * factor), round(polished.height * factor))
            polished = polished.resize(size, Image.Resampling.LANCZOS)
        destination.parent.mkdir(parents=True, exist_ok=True)
        polished.save(destination, format="PNG")
        return polished.size


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polish a yellow-box annotated tile with Real-ESRGAN.")
    parser.add_argument("--lead-id", help="Lead whose annotated PNG should be polished")
    parser.add_argument("--input", type=Path, help="Annotated PNG to polish")
    parser.add_argument("--output", type=Path, help="Enhanced PNG path")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    if args.input is None and not args.lead_id:
        logger.error("Pass --lead-id or --input.")
        return 1
    source = args.input or (ANNOTATED_DIR / f"{args.lead_id}.png")
    lead_id = args.lead_id or source.stem
    destination = args.output or (ENHANCED_DIR / f"{lead_id}_enhanced.png")
    try:
        saved = enhance_annotated(source, destination)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("%s", exc)
        return 1
    print(saved)
    return 0


if __name__ == "__main__":
    sys.exit(main())
