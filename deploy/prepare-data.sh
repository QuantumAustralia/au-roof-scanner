#!/usr/bin/env bash
# Make the data folders and the verification CSV writable by the account that runs the app.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p data/tiles data/annotated data/grades data/logs data/clusters data/view_base data/enhanced

if [[ ! -f data/verification_report.csv ]]; then
  printf '%s\n' 'lead_id,street_address,suburb,postcode,lat,lon,roof_material,wear_score,primary_defect,confidence,google_maps_link,status' > data/verification_report.csv
fi

chmod 775 data data/tiles data/annotated data/grades data/logs data/clusters data/view_base data/enhanced
chmod 664 data/verification_report.csv
