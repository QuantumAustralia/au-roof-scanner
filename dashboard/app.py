"""Local page for reviewing graded leads before a mail-out.

Reads data/verification_report.csv. A status change is written back to that
file immediately. Approved rows can be saved to data/approved_for_mailout.csv.

    python dashboard/app.py

Listens on APP_HOST:APP_PORT from .env. Defaults are 0.0.0.0:8080.
"""

from __future__ import annotations

import csv
import os
import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image

from scanner.access import require_password
from scanner.addresses import safe_lead_id
from scanner.config import ANNOTATED_DIR, DATA_DIR, TILES_DIR
from scanner.verification import REPORT_CSV, REPORT_FIELDS

APPROVED_CSV = DATA_DIR / "approved_for_mailout.csv"
STATUSES = ("pending_review", "approved", "rejected")
HOST = os.environ.get("APP_HOST") or "0.0.0.0"
PORT = int(os.environ.get("APP_PORT") or "8080")

class _ForwardedPrefix:
    """Honour /review when nginx serves this app under that path."""

    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        prefix = environ.get("HTTP_X_FORWARDED_PREFIX", "")
        if prefix:
            environ["SCRIPT_NAME"] = prefix.rstrip("/")
        return self.wsgi(environ, start_response)


app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
app.wsgi_app = _ForwardedPrefix(app.wsgi_app)


@app.before_request
def _review_password():
    return require_password()


def _read_rows() -> list[dict[str, str]]:
    if not REPORT_CSV.is_file():
        return []
    with REPORT_CSV.open(newline="", encoding="utf-8-sig") as handle:
        return [
            {name: (row.get(name) or "").strip() for name in REPORT_FIELDS}
            for row in csv.DictReader(handle)
        ]


def _write_rows(rows: list[dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_CSV.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    try:
        temporary.replace(REPORT_CSV)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise PermissionError(
            "Close data/verification_report.csv if it is open in Excel, then try again."
        ) from exc


def _image_path(lead_id: str) -> Path | None:
    safe = safe_lead_id(lead_id)
    annotated = ANNOTATED_DIR / f"{safe}.png"
    if annotated.is_file():
        return annotated
    tile = TILES_DIR / f"{safe}.png"
    if tile.is_file():
        return tile
    return None


@app.errorhandler(PermissionError)
def file_locked(_exc: PermissionError):
    return jsonify(
        ok=False,
        error="Close data/verification_report.csv if it is open in Excel, then try again.",
    ), 423


@app.get("/")
def home():
    rows = _read_rows()
    approved = sum(1 for row in rows if row["status"] == "approved")
    return render_template(
        "index.html",
        rows=rows,
        statuses=STATUSES,
        approved_count=approved,
    )


@app.post("/status")
def update_status():
    """Save one lead's review status back to the verification CSV."""
    payload = request.get_json(silent=True) or {}
    lead_id = str(payload.get("lead_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    if status not in STATUSES:
        return jsonify(ok=False, error="Choose pending review, approved, or rejected."), 400
    rows = _read_rows()
    matched = False
    for row in rows:
        if row["lead_id"] == lead_id:
            row["status"] = status
            matched = True
            break
    if not matched:
        return jsonify(ok=False, error="That lead is not in the verification report."), 404
    _write_rows(rows)
    approved = sum(1 for row in rows if row["status"] == "approved")
    return jsonify(ok=True, approved_count=approved)


@app.get("/image/<lead_id>")
def thumbnail(lead_id: str):
    """Small copy of the annotated view, or the saved tile when no annotation exists."""
    path = _image_path(lead_id)
    if path is None:
        abort(404)
    with Image.open(path) as image:
        copy = image.convert("RGB")
        copy.thumbnail((160, 120))
        buffer = BytesIO()
        copy.save(buffer, format="PNG")
        buffer.seek(0)
    return send_file(buffer, mimetype="image/png")


@app.post("/approved")
def download_approved():
    """Write approved leads to disk and download that CSV."""
    approved = [row for row in _read_rows() if row["status"] == "approved"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with APPROVED_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(approved)
    return send_file(
        APPROVED_CSV,
        mimetype="text/csv",
        as_attachment=True,
        download_name="approved_for_mailout.csv",
    )


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
