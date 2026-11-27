# Review dashboard for Lightsail. Build from the project root.
# Data (tiles, annotated images, verification CSV) is a volume so it stays writable
# and is not baked into the image.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scanner ./scanner
COPY dashboard ./dashboard
COPY deploy/docker-entrypoint.sh /entrypoint.sh
COPY pricing_config.json enhance_config.json scan_config.json ./

RUN useradd --create-home --uid 1000 scanner \
    && mkdir -p data/tiles data/annotated data/grades data/logs data/clusters data/view_base data/enhanced \
    && printf '%s\n' 'lead_id,street_address,suburb,postcode,lat,lon,roof_material,wear_score,primary_defect,confidence,google_maps_link,status' > data/verification_report.csv \
    && chown -R scanner:scanner /app \
    && chmod 775 data data/tiles data/annotated data/grades data/logs data/clusters data/view_base data/enhanced \
    && chmod 664 data/verification_report.csv \
    && chmod 755 /entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
