#!/bin/sh
# Create the data folders, make them writable, then start the review dashboard.
set -eu

mkdir -p /app/data/tiles /app/data/annotated /app/data/grades /app/data/logs /app/data/clusters /app/data/view_base /app/data/enhanced

if [ ! -f /app/data/verification_report.csv ]; then
  printf '%s\n' 'lead_id,street_address,suburb,postcode,lat,lon,roof_material,wear_score,primary_defect,confidence,google_maps_link,status' > /app/data/verification_report.csv
fi

chown -R scanner:scanner /app/data
chmod 775 /app/data /app/data/tiles /app/data/annotated /app/data/grades /app/data/logs /app/data/clusters /app/data/view_base /app/data/enhanced
chmod 664 /app/data/verification_report.csv

export APP_HOST="${APP_HOST:-0.0.0.0}"
export APP_PORT="${APP_PORT:-8080}"

exec gosu scanner python dashboard/app.py
