# AGENTS.md

This file is for AI agents or external projects that need to understand,
operate, or integrate with this ePaper cloud service.

Do not put real admin tokens, device tokens, SSH passwords, or private keys in
this file. Use placeholders in shared documentation.

## Project Summary

`ePaperService` is a FastAPI service that converts uploaded images into a
compact 6-color e-paper binary format and serves the result to web users or
ESP32 devices over HTTP.

Primary use cases:

- Browser users upload an image and download a processed BMP preview or `.epd`
  binary.
- ESP32 devices poll the cloud service for the currently assigned image,
  download new binary data only when the version changes, verify it, display it,
  and report status.

Current deployed base URL:

```text
http://47.113.120.232
```

Public health check:

```text
GET http://47.113.120.232/health
```

Expected response:

```json
{"status":"ok"}
```

## Runtime Stack

- Language: Python 3
- Web framework: FastAPI
- ASGI server: uvicorn
- Reverse proxy: nginx
- Storage: local filesystem plus SQLite
- Image processing: Pillow
- DNG/RAW support: rawpy / LibRaw

Python dependencies are pinned in:

```text
requirements.txt
```

Current dependencies:

```text
fastapi==0.115.12
uvicorn[standard]==0.34.2
pillow==11.2.1
python-multipart==0.0.20
rawpy==0.27.0
```

## Server Plan

Current server:

```text
Provider: Alibaba Cloud ECS
OS: Ubuntu 24.04
Public IP: 47.113.120.232
HTTP port: 80
Application port: 127.0.0.1:8000
```

Filesystem layout on the server:

```text
/opt/ePaperService              project code
/opt/ePaperService/.venv        Python virtual environment
/var/lib/epaper-service         persistent data directory
/var/lib/epaper-service/epaper.db
/var/lib/epaper-service/images
/etc/systemd/system/epaper.service
/etc/nginx/sites-available/epaper
/etc/nginx/sites-enabled/epaper
```

Service user:

```text
epaper
```

systemd service:

```text
epaper.service
```

nginx proxies public HTTP traffic from port 80 to:

```text
http://127.0.0.1:8000
```

Current nginx upload limit:

```text
client_max_body_size 100m;
```

If large iPhone ProRAW/DNG uploads fail, check this limit first.

## Secrets And Tokens

There are three token classes.

### Admin Token

Header:

```http
X-Admin-Token: YOUR_ADMIN_TOKEN
```

Purpose:

- Create/reset devices.
- Assign images to devices.
- Create limited upload tokens.
- List upload token metadata.
- Inspect device state.
- Upload images directly as an administrator.

Never share the real admin token with casual testers, ESP32 firmware, or public
docs. The deployed server reads it from:

```text
/etc/systemd/system/epaper.service
Environment=EPAPER_ADMIN_TOKEN=...
```

### Upload Token

Header:

```http
X-Upload-Token: YOUR_UPLOAD_TOKEN
```

Purpose:

- Let another user upload images and download generated results without
  receiving the admin token.
- Limited by remaining use count.
- Stored in SQLite as a SHA-256 hash, not plaintext.

Upload tokens can call:

```text
POST /api/images
GET  /api/images/{image_id}
GET  /api/images/{image_id}/preview
GET  /api/images/{image_id}/data
```

Upload tokens cannot create devices, assign images to devices, inspect devices,
or create other tokens.

### Device Token

Header:

```http
X-Device-Token: DEVICE_TOKEN
```

Purpose:

- Used by ESP32 devices for polling and status reporting.
- Created or reset by `POST /api/devices/{device_id}`.

Device tokens can call:

```text
GET  /api/devices/{device_id}/current
POST /api/devices/{device_id}/status
```

## Data Model

SQLite schema is defined in:

```text
app/db.py
```

Tables:

- `images`: uploaded image metadata, output file paths, hashes.
- `devices`: device token, current image assignment, version, latest status.
- `status_events`: append-only device status history.
- `upload_tokens`: hashed limited-use upload tokens.

Generated files are stored under:

```text
/var/lib/epaper-service/images/{image_id}/
```

Typical contents:

```text
original upload file
image.epd
preview.bmp
```

## Image Processing

Main implementation:

```text
app/image_processing.py
```

Supported input:

- Common Pillow-readable formats, such as jpg, png, bmp, webp.
- `.dng` / `.DNG` via rawpy.

Output sizes:

```text
landscape: 800x480
portrait:  480x800
```

Direction options:

```text
auto
landscape
portrait
```

Fit mode options:

```text
scale
cut
```

`scale` fills the target screen and may crop. `cut` preserves the image inside
the target screen and pads with white.

Palette order is protocol-critical. Do not reorder it unless firmware is
updated at the same time:

```text
0 black  RGB(0, 0, 0)
1 white  RGB(255, 255, 255)
2 yellow RGB(255, 255, 0)
3 red    RGB(255, 0, 0)
4 blue   RGB(0, 0, 255)
5 green  RGB(0, 255, 0)
```

Wire format:

```text
epd4bit-indexed-v1
```

Each byte stores two palette indexes:

```text
high nibble: first pixel
low nibble:  second pixel
```

For both `800x480` and `480x800`, the binary payload size is:

```text
800 * 480 / 2 = 192000 bytes
```

Firmware unpacking logic:

```c
uint8_t first = (byte >> 4) & 0x0F;
uint8_t second = byte & 0x0F;
```

## HTTP API

Base URL:

```text
http://47.113.120.232
```

### Health

```http
GET /health
```

No auth.

### Web UI

```http
GET /
```

No auth to load the page. Upload still requires either `X-Admin-Token` or
`X-Upload-Token`.

### Create Upload Token

Admin only.

```http
POST /api/upload-tokens
Content-Type: application/json
X-Admin-Token: YOUR_ADMIN_TOKEN
```

Body:

```json
{"uses":1,"label":"guest-test"}
```

Response:

```json
{
  "token": "up_xxxxxxxxxxxxxxxxxxxxx",
  "uses": 1,
  "label": "guest-test"
}
```

The plaintext token is only returned at creation time. Store or share it then.

### List Upload Tokens

Admin only.

```http
GET /api/upload-tokens
X-Admin-Token: YOUR_ADMIN_TOKEN
```

Response includes labels, remaining uses, creation time, and last-used time. It
does not expose plaintext token values.

### Upload Image

Admin or upload token.

```http
POST /api/images
X-Upload-Token: YOUR_UPLOAD_TOKEN
Content-Type: multipart/form-data
```

Form fields:

```text
file: image file
direction: auto | landscape | portrait
mode: scale | cut
dither: true | false
```

curl example:

```bash
curl -X POST http://47.113.120.232/api/images \
  -H 'X-Upload-Token: YOUR_UPLOAD_TOKEN' \
  -F 'file=@/path/to/image.jpg' \
  -F 'direction=auto' \
  -F 'mode=scale' \
  -F 'dither=true'
```

Response:

```json
{
  "image_id": "IMAGE_ID",
  "width": 800,
  "height": 480,
  "format": "epd4bit-indexed-v1",
  "palette": [[0,0,0],[255,255,255],[255,255,0],[255,0,0],[0,0,255],[0,255,0]],
  "sha256": "SHA256",
  "data_size": 192000,
  "data_url": "/api/images/IMAGE_ID/data",
  "preview_url": "/api/images/IMAGE_ID/preview",
  "created_at": "..."
}
```

### Get Image Metadata

No auth currently.

```http
GET /api/images/{image_id}
```

### Download BMP Preview

No auth currently.

```http
GET /api/images/{image_id}/preview
```

Response media type:

```text
image/bmp
```

### Download EPD Binary

No auth currently.

```http
GET /api/images/{image_id}/data
```

Response media type:

```text
application/octet-stream
```

Response headers:

```text
X-Image-Sha256
X-Image-Format
```

### Create Or Reset Device

Admin only.

```http
POST /api/devices/{device_id}
Content-Type: application/json
X-Admin-Token: YOUR_ADMIN_TOKEN
```

Body:

```json
{}
```

Optional fixed device token:

```json
{"token":"DEVICE_TOKEN"}
```

Response:

```json
{
  "device_id": "device001",
  "token": "DEVICE_TOKEN"
}
```

### Assign Image To Device

Admin only.

```http
POST /api/devices/{device_id}/assign
Content-Type: application/json
X-Admin-Token: YOUR_ADMIN_TOKEN
```

Body:

```json
{"image_id":"IMAGE_ID"}
```

Every assignment increments `current_version`. ESP32 devices should compare the
manifest `version` with their stored version to decide whether to download.

### Device Current Manifest

Device token required.

```http
GET /api/devices/{device_id}/current
X-Device-Token: DEVICE_TOKEN
```

No image assigned:

```json
{
  "device_id": "device001",
  "version": 0,
  "has_image": false
}
```

Image assigned:

```json
{
  "device_id": "device001",
  "version": 1,
  "has_image": true,
  "image_id": "IMAGE_ID",
  "width": 800,
  "height": 480,
  "format": "epd4bit-indexed-v1",
  "palette": [[0,0,0],[255,255,255],[255,255,0],[255,0,0],[0,0,255],[0,255,0]],
  "sha256": "SHA256",
  "download_url": "/api/images/IMAGE_ID/data"
}
```

If `download_url` starts with `/`, clients must prefix the service base URL:

```text
http://47.113.120.232/api/images/IMAGE_ID/data
```

### Device Status Report

Device token required.

```http
POST /api/devices/{device_id}/status
Content-Type: application/json
X-Device-Token: DEVICE_TOKEN
```

Body examples:

```json
{"version":1,"status":"displayed"}
```

```json
{"version":1,"status":"unchanged"}
```

```json
{"version":1,"status":"error","error":"sha256 mismatch"}
```

Optional telemetry:

```json
{
  "version": 1,
  "status": "displayed",
  "battery_mv": 3800,
  "rssi": -62
}
```

### Get Device State

Admin only.

```http
GET /api/devices/{device_id}
X-Admin-Token: YOUR_ADMIN_TOKEN
```

## ESP32 Integration Contract

Recommended ESP32 behavior:

1. Wake from deep sleep.
2. Connect Wi-Fi.
3. `GET /api/devices/{device_id}/current` with `X-Device-Token`.
4. If `has_image` is false, report `idle` and sleep.
5. If `version` equals the stored version, report `unchanged` and sleep.
6. If version changed, download `download_url`.
7. Verify binary size equals `width * height / 2`.
8. Verify SHA-256 equals manifest `sha256`.
9. Decode two 4-bit pixels per byte.
10. Convert palette indexes to the display driver's color encoding.
11. Refresh the e-paper display.
12. Report `displayed` or `error`.
13. Sleep.

There is a local simulator:

```bash
python3 simulate_device.py \
  --server http://47.113.120.232 \
  --device-id device001 \
  --token DEVICE_TOKEN
```

## Local Development

From the project root:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export EPAPER_ADMIN_TOKEN=dev-admin-token
uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000/health
```

Local default data directory:

```text
./data
```

Override data paths:

```bash
export EPAPER_DATA_DIR=/path/to/data
export EPAPER_DB_PATH=/path/to/epaper.db
```

## Deployment Procedure

Sync from local machine to the ECS server:

```bash
rsync -az --delete \
  --exclude .venv \
  --exclude data \
  --exclude __pycache__ \
  --exclude "*.pyc" \
  /path/to/ePaperService/ \
  root@47.113.120.232:/opt/ePaperService/
```

Then on the server:

```bash
cd /opt/ePaperService
chown -R epaper:epaper /opt/ePaperService /var/lib/epaper-service
sudo -u epaper .venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r requirements.txt
sudo -u epaper .venv/bin/python -m py_compile app/main.py app/db.py app/image_processing.py simulate_device.py
cp scripts/nginx.conf /etc/nginx/sites-available/epaper
nginx -t
systemctl reload nginx
systemctl restart epaper
curl -fsS http://127.0.0.1/health
```

The server uses the Aliyun PyPI mirror because direct access to public PyPI may
fail from this ECS environment.

## Operations

SSH:

```bash
ssh root@47.113.120.232
```

Service status:

```bash
systemctl status epaper
systemctl status nginx
```

Logs:

```bash
journalctl -u epaper -f
tail -f /var/log/nginx/error.log
```

Restart:

```bash
systemctl restart epaper
systemctl reload nginx
```

Health:

```bash
curl http://127.0.0.1/health
curl http://47.113.120.232/health
```

Check upload limit:

```bash
grep -R "client_max_body_size" -n /etc/nginx /opt/ePaperService/scripts/nginx.conf
```

## Important Implementation Notes

- Do not reorder `PALETTE_RGB` without coordinating firmware changes.
- Current quantization uses the original pure RGB 6-color palette directly.
- DNG support depends on `rawpy`; not every RAW variant is guaranteed to decode.
- Upload tokens are consumed before image conversion. A failed conversion still
  spends one use. Change `require_upload_token` if refund-on-failure is needed.
- Image metadata and binary downloads are currently unauthenticated if the
  caller knows `image_id`.
- The service currently runs over HTTP, not HTTPS. Tokens are transmitted in
  cleartext over the network. Configure HTTPS before long-term production use.
- SQLite is sufficient for this single-ECS deployment. Move to an external
  database only if concurrency, multi-server deployment, or backup requirements
  grow.
- Keep generated image files and SQLite data under `/var/lib/epaper-service`;
  do not store persistent data inside `/opt/ePaperService`.

## Common Errors

`{"detail":"invalid admin token"}`:

- The admin header is missing or contains the placeholder `YOUR_ADMIN_TOKEN`.
- Replace it with the real server admin token from the systemd environment.

`{"detail":"invalid or expired upload token"}`:

- The upload token is wrong or its remaining use count is zero.
- Create a new upload token with the admin API.

nginx `client intended to send too large body`:

- Uploaded file exceeds `client_max_body_size`.
- Current configured value is 100 MB.

`image conversion failed`:

- Input is not a valid image.
- DNG/RAW format may not be supported by rawpy/LibRaw.
- Try exporting to jpg/png and uploading again.

`sha256 mismatch` on ESP32:

- Download was corrupted or incomplete.
- Do not refresh the panel; report status `error`.

