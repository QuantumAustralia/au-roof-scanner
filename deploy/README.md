# Deploy the review dashboard to Lightsail (Sydney)

Use an Ubuntu instance in `ap-southeast-2`. These steps copy the project, install it in a virtual environment, and leave the review page listening on port 8080. Grading is unchanged.

Do not copy `.env` into git. Create it on the instance.

## 1. Open port 8080

In the Lightsail console, open the instance, then **Networking**. Add a firewall rule:

- Application: Custom
- Protocol: TCP
- Port: 8080

## 2. Copy the code

On this Windows machine, from the project directory, replace `YOUR_KEY.pem` and `PUBLIC_IP`:

```powershell
ssh -i YOUR_KEY.pem ubuntu@PUBLIC_IP "sudo mkdir -p /opt/au-roof-scanner/data && sudo chown -R ubuntu:ubuntu /opt/au-roof-scanner"
tar --exclude .venv --exclude .git --exclude .env --exclude data/gnaf --exclude data/tiles --exclude data/annotated --exclude data/view_base --exclude data/grades --exclude data/clusters -cf - . | ssh -i YOUR_KEY.pem ubuntu@PUBLIC_IP "tar -xf - -C /opt/au-roof-scanner"
scp -i YOUR_KEY.pem .env ubuntu@PUBLIC_IP:/opt/au-roof-scanner/.env
```

Copy `data/verification_report.csv`, `data/tiles`, and `data/annotated` as well when the instance should show the leads you already graded:

```powershell
scp -i YOUR_KEY.pem data\verification_report.csv ubuntu@PUBLIC_IP:/opt/au-roof-scanner/data/verification_report.csv
scp -i YOUR_KEY.pem -r data\tiles ubuntu@PUBLIC_IP:/opt/au-roof-scanner/data/
scp -i YOUR_KEY.pem -r data\annotated ubuntu@PUBLIC_IP:/opt/au-roof-scanner/data/
```

## 3. Install and start

On the instance:

```bash
ssh -i YOUR_KEY.pem ubuntu@PUBLIC_IP
cd /opt/au-roof-scanner
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

`.env` is already on the instance. Confirm it sets the public bind:

```bash
grep -E '^(APP_HOST|APP_PORT)=' .env
```

Those lines should be `APP_HOST=0.0.0.0` and `APP_PORT=8080`. The start script loads `.env` before Python starts. `scanner.config` also reads the same file for the API keys.

Make the data folders writable by `ubuntu`, then install the service so the page comes back after a reboot:

```bash
bash deploy/prepare-data.sh
chmod +x deploy/start-dashboard.sh deploy/prepare-data.sh deploy/docker-entrypoint.sh
sudo cp deploy/au-roof-scanner.service /etc/systemd/system/au-roof-scanner.service
sudo systemctl daemon-reload
sudo systemctl enable --now au-roof-scanner
sudo systemctl status au-roof-scanner
```

## 4. Check it

On the instance:

```bash
curl -I http://127.0.0.1:8080/
```

From this PC, open `http://PUBLIC_IP:8080/`.

`deploy/prepare-data.sh` creates `data/tiles`, `data/annotated`, and `data/verification_report.csv` when they are missing, and sets them writable by the service user. Status changes and the approved-leads download write into `data/`.

## Docker

From the project root, if you would rather run the same page in a container on the instance:

```bash
docker build -t au-roof-scanner .
docker run --env-file .env -p 8080:8080 -v /opt/au-roof-scanner/data:/app/data au-roof-scanner
```

The image listens on 8080. The data volume is created at start and chowned to the app user inside the container, so the verification CSV, tiles, and annotated images stay writable.
