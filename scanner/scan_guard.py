"""Remember when an address was scanned, and skip it during the lockout.

Edit lockout_months in scan_config.json, or set SCAN_LOCKOUT_MONTHS.
The history file is data/scan_history.json.
"""

from __future__ import annotations

import calendar
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from scanner.addresses import Address
from scanner.config import DATA_DIR, ROOT

SCAN_CONFIG_PATH = ROOT / "scan_config.json"
HISTORY_PATH = DATA_DIR / "scan_history.json"
DEFAULT_LOCKOUT_MONTHS = 6


@dataclass(frozen=True)
class Lockout:
    months: int
    by_lead: dict[str, date]
    by_address: dict[str, date]


def lockout_months(path: Path = SCAN_CONFIG_PATH) -> int:
    """Months before the same address may be scanned again."""
    configured = DEFAULT_LOCKOUT_MONTHS
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if "lockout_months" in data:
            configured = int(data["lockout_months"])
    override = os.getenv("SCAN_LOCKOUT_MONTHS", "").strip()
    if override:
        configured = int(override)
    if configured < 0:
        raise ValueError("lockout_months cannot be negative.")
    return configured


def load_lockout(path: Path = HISTORY_PATH) -> Lockout:
    by_lead: dict[str, date] = {}
    by_address: dict[str, date] = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        by_lead = {key: _parse_date(value) for key, value in (data.get("by_lead") or {}).items()}
        by_address = {
            key: _parse_date(value) for key, value in (data.get("by_address") or {}).items()
        }
    return Lockout(months=lockout_months(), by_lead=by_lead, by_address=by_address)


def is_locked(address: Address, lockout: Lockout, today: date | None = None) -> bool:
    today = today or date.today()
    last = lockout.by_lead.get(address.lead_id) or lockout.by_address.get(address_key(address))
    if last is None or lockout.months == 0:
        return False
    return today < _add_months(last, lockout.months)


def record_scanned(addresses: list[Address], path: Path = HISTORY_PATH, on: date | None = None) -> None:
    """Store today's scan date for each address. Existing dates outside this batch stay."""
    on = on or date.today()
    lockout = load_lockout(path)
    for address in addresses:
        lockout.by_lead[address.lead_id] = on
        lockout.by_address[address_key(address)] = on
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "by_lead": {key: value.isoformat() for key, value in sorted(lockout.by_lead.items())},
                "by_address": {
                    key: value.isoformat() for key, value in sorted(lockout.by_address.items())
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def address_key(address: Address) -> str:
    return "|".join(
        (
            address.street_address.strip().lower(),
            address.suburb.strip().lower(),
            address.postcode.strip(),
        )
    )


def _parse_date(value: object) -> date:
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
