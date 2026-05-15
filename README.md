# ePaperService

Cloud-side service for converting uploaded images into a compact 6-color e-paper
format and serving them to an ESP32 over outbound HTTP.

## Protocol

The ESP32 does not need a public IP. It wakes up, requests the current manifest,
downloads the binary only when the version changes, reports status, and sleeps.

```text
GET  /api/devices/{device_id}/current
GET  /api/images/{image_id}/data
POST /api/devices/{device_id}/status
```

Image data uses `epd4bit-indexed-v1`: two pixels per byte, high nibble first.
Palette indexes are:

```text
0 black
1 white
2 yellow
3 red
4 blue
5 green
```

For both `800x480` and `480x800`, the binary payload is `192000` bytes.

## App API

InkSplash app clients use email/password accounts plus Bearer tokens:

```text
POST /api/auth/register
POST /api/auth/login
GET  /api/me
POST /api/auth/verify-email/request
POST /api/auth/verify-email/confirm
POST /api/auth/password-reset/request
POST /api/auth/password-reset/confirm
```

App users can claim devices, share devices with family members, upload images,
assign their own images to devices where they are `owner` or `admin`, and read
device status history:

```text
POST   /api/me/devices/claim
GET    /api/me/devices
GET    /api/me/devices/{device_id}
PATCH  /api/me/devices/{device_id}
DELETE /api/me/devices/{device_id}
POST   /api/me/devices/{device_id}/assign
POST   /api/me/devices/{device_id}/invites
POST   /api/me/device-invites/accept
GET    /api/me/devices/{device_id}/members
DELETE /api/me/devices/{device_id}/members/{user_id}
GET    /api/me/devices/{device_id}/status-events
```

User write operations require verified email. The ESP32 protocol above remains
token-based and does not require an app account.

## Local Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export EPAPER_ADMIN_TOKEN=dev-admin-token
export EPAPER_AUTH_SECRET=dev-auth-secret
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/health`.

## Basic API Flow

Create or reset a device:

```bash
curl -X POST http://127.0.0.1:8000/api/devices/device001 \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: dev-admin-token' \
  -d '{}'
```

Upload an image:

```bash
curl -X POST http://127.0.0.1:8000/api/images \
  -H 'X-Admin-Token: dev-admin-token' \
  -F 'file=@/path/to/image.jpg' \
  -F 'direction=auto' \
  -F 'mode=scale' \
  -F 'dither=true'
```

Assign an uploaded image to a device:

```bash
curl -X POST http://127.0.0.1:8000/api/devices/device001/assign \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: dev-admin-token' \
  -d '{"image_id":"IMAGE_ID_FROM_UPLOAD"}'
```

Simulate an ESP32:

```bash
python3 simulate_device.py \
  --server http://127.0.0.1:8000 \
  --device-id device001 \
  --token DEVICE_TOKEN_FROM_CREATE
```

## Ubuntu Deployment

On an Alibaba Cloud ECS Ubuntu instance with a public IP:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx
sudo adduser --system --group --home /opt/ePaperService epaper
sudo mkdir -p /opt/ePaperService /var/lib/epaper-service
sudo chown -R epaper:epaper /opt/ePaperService /var/lib/epaper-service
```

Use Ubuntu 22.04 or newer, or any image with Python 3.10+. Copy this project
into `/opt/ePaperService`, then:

```bash
cd /opt/ePaperService
sudo -u epaper python3 -m venv .venv
sudo -u epaper .venv/bin/pip install -r requirements.txt
sudo cp scripts/epaper.service /etc/systemd/system/epaper.service
sudo systemctl daemon-reload
sudo systemctl enable --now epaper
```

Put nginx in front of uvicorn:

```bash
sudo cp scripts/nginx.conf /etc/nginx/sites-available/epaper
sudo ln -s /etc/nginx/sites-available/epaper /etc/nginx/sites-enabled/epaper
sudo nginx -t
sudo systemctl reload nginx
```

For first cloud testing, HTTP on port 80 is enough; switch to HTTPS before
long-term deployment. In the Alibaba Cloud security group, open inbound TCP 80
for HTTP testing and TCP 443 after HTTPS is configured.

## Production Configuration

Production app builds should use an HTTPS base URL, for example:

```text
https://api.example.com
```

Set these server environment variables:

```text
EPAPER_ADMIN_TOKEN=...
EPAPER_AUTH_SECRET=...
PUBLIC_APP_URL=https://api.example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_FROM=no-reply@example.com
SMTP_USE_TLS=1
```

If SMTP is not configured, verification/reset codes and invite links are written
to the service log for local testing. Do not rely on log-based codes or links in
production.

For HTTPS, put nginx in front of uvicorn with a certificate from your preferred
CA, listen on 443, and redirect port 80 to 443. Do not store real secrets in
repo files.

Backups:

```bash
sudo EPAPER_DATA_DIR=/var/lib/epaper-service \
  EPAPER_BACKUP_DIR=/var/backups/epaper-service \
  /opt/ePaperService/scripts/backup.sh
```

## ESP32 Notes

Recommended firmware behavior:

1. Wake from deep sleep.
2. Connect Wi-Fi.
3. `GET /api/devices/{device_id}/current` with `X-Device-Token`.
4. If `version` is unchanged, post `unchanged` status and sleep.
5. If changed, download `download_url`.
6. Verify payload size and `sha256`.
7. Convert 4-bit palette indexes into the display driver's buffer.
8. Refresh the screen, post `displayed` or `error`, then sleep.
