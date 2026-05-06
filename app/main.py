from __future__ import annotations

import hashlib
import os
import secrets
import shutil
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .db import connect, init_db, row_to_dict
from .image_processing import FORMAT_NAME, PALETTE_RGB, convert_image_file


DATA_DIR = Path(os.getenv("EPAPER_DATA_DIR", "./data")).resolve()
DB_PATH = Path(os.getenv("EPAPER_DB_PATH", str(DATA_DIR / "epaper.db"))).resolve()
ADMIN_TOKEN = os.getenv("EPAPER_ADMIN_TOKEN", "")

# The service is designed for one small Ubuntu ECS instance: local files hold
# generated images and SQLite holds the current device assignment/version.
app = FastAPI(title="ePaper Service", version="0.1.0")
conn = connect(DB_PATH)
init_db(conn)


class AssignRequest(BaseModel):
    image_id: str = Field(min_length=1)


class DeviceCreateRequest(BaseModel):
    token: str | None = Field(default=None, description="Optional fixed token for the device")


class StatusRequest(BaseModel):
    version: int | None = None
    status: str = Field(min_length=1, max_length=64)
    error: str | None = None
    battery_mv: int | None = None
    rssi: int | None = None


def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    # Admin token protects write APIs such as upload and device assignment.
    # Leave EPAPER_ADMIN_TOKEN empty only for isolated local development.
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="invalid admin token")


def require_device_token(
    device_id: str,
    x_device_token: Annotated[str | None, Header()] = None,
) -> None:
    # ESP32 uses this token on polling/status requests. If a device has no token,
    # it is intentionally treated as open for quick lab testing.
    row = conn.execute("SELECT token FROM devices WHERE device_id = ?", (device_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown device")
    token = row["token"]
    if token and x_device_token != token:
        raise HTTPException(status_code=401, detail="invalid device token")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>ePaper Service</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 32px; max-width: 920px; }
    label { display: block; margin: 12px 0 4px; }
    input, select, button { font-size: 16px; padding: 6px; }
    code { background: #f4f4f4; padding: 2px 4px; }
  </style>
</head>
<body>
  <h1>ePaper Service</h1>
  <p>API is running. Use <code>POST /api/images</code> to upload an image.</p>
  <form method="post" action="/api/images" enctype="multipart/form-data">
    <label>Image</label><input name="file" type="file" required>
    <label>Direction</label>
    <select name="direction"><option>auto</option><option>landscape</option><option>portrait</option></select>
    <label>Mode</label>
    <select name="mode"><option>scale</option><option>cut</option></select>
    <label><input name="dither" type="checkbox" checked value="true"> Dither</label>
    <button type="submit">Upload</button>
  </form>
</body>
</html>
"""


@app.post("/api/devices/{device_id}", dependencies=[Depends(require_admin)])
def create_device(device_id: str, request: DeviceCreateRequest) -> dict[str, str | None]:
    token = request.token or secrets.token_urlsafe(24)
    conn.execute(
        """
        INSERT INTO devices (device_id, token)
        VALUES (?, ?)
        ON CONFLICT(device_id) DO UPDATE SET token = excluded.token, updated_at = CURRENT_TIMESTAMP
        """,
        (device_id, token),
    )
    conn.commit()
    return {"device_id": device_id, "token": token}


@app.post("/api/images", dependencies=[Depends(require_admin)])
def upload_image(
    file: Annotated[UploadFile, File()],
    direction: Annotated[str, Form()] = "auto",
    mode: Annotated[str, Form()] = "scale",
    dither: Annotated[bool, Form()] = True,
) -> dict[str, object]:
    if direction not in {"auto", "landscape", "portrait"}:
        raise HTTPException(status_code=400, detail="direction must be auto, landscape, or portrait")
    if mode not in {"scale", "cut"}:
        raise HTTPException(status_code=400, detail="mode must be scale or cut")

    image_id = secrets.token_hex(12)
    image_dir = DATA_DIR / "images" / image_id
    image_dir.mkdir(parents=True, exist_ok=False)

    # Store the original upload next to generated artifacts so failed
    # conversions can be reproduced on the cloud server.
    original_name = Path(file.filename or "upload").name
    original_path = image_dir / original_name
    with original_path.open("wb") as output:
        shutil.copyfileobj(file.file, output)

    try:
        converted = convert_image_file(original_path, direction=direction, mode=mode, dither=dither)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"image conversion failed: {exc}") from exc

    data_path = image_dir / "image.epd"
    preview_path = image_dir / "preview.bmp"
    data_path.write_bytes(converted.epd_data)
    preview_path.write_bytes(converted.preview_bmp)
    sha256 = hashlib.sha256(converted.epd_data).hexdigest()

    conn.execute(
        """
        INSERT INTO images (
            image_id, original_filename, width, height, direction, mode, dither,
            format, sha256, data_path, preview_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            image_id,
            original_name,
            converted.width,
            converted.height,
            direction,
            mode,
            int(dither),
            FORMAT_NAME,
            sha256,
            str(data_path),
            str(preview_path),
        ),
    )
    conn.commit()

    return _image_response(image_id)


@app.get("/api/images/{image_id}")
def get_image(image_id: str) -> dict[str, object]:
    return _image_response(image_id)


@app.get("/api/images/{image_id}/preview")
def get_preview(image_id: str) -> FileResponse:
    image = _get_image_or_404(image_id)
    return FileResponse(image["preview_path"], media_type="image/bmp", filename=f"{image_id}.bmp")


@app.get("/api/images/{image_id}/data")
def get_data(image_id: str) -> FileResponse:
    image = _get_image_or_404(image_id)
    return FileResponse(
        image["data_path"],
        media_type="application/octet-stream",
        filename=f"{image_id}.epd",
        headers={"X-Image-Sha256": image["sha256"], "X-Image-Format": image["format"]},
    )


@app.post("/api/devices/{device_id}/assign", dependencies=[Depends(require_admin)])
def assign_image(device_id: str, request: AssignRequest) -> dict[str, object]:
    _get_image_or_404(request.image_id)
    # Version increments on every assignment. The ESP32 only downloads when the
    # manifest version differs from the version stored in RTC/NVS.
    conn.execute(
        """
        INSERT INTO devices (device_id, current_image_id, current_version)
        VALUES (?, ?, 1)
        ON CONFLICT(device_id) DO UPDATE SET
            current_image_id = excluded.current_image_id,
            current_version = devices.current_version + 1,
            updated_at = CURRENT_TIMESTAMP
        """,
        (device_id, request.image_id),
    )
    conn.commit()
    return _current_manifest(device_id)


@app.get("/api/devices/{device_id}/current")
def current_manifest(
    device_id: str,
    _: Annotated[None, Depends(require_device_token)],
) -> dict[str, object]:
    # This is the low-power polling endpoint: wake, fetch manifest, compare
    # version, optionally download, then deep sleep again.
    conn.execute(
        "UPDATE devices SET last_seen_at = CURRENT_TIMESTAMP WHERE device_id = ?",
        (device_id,),
    )
    conn.commit()
    return _current_manifest(device_id)


@app.post("/api/devices/{device_id}/status")
def update_status(
    device_id: str,
    request: StatusRequest,
    _: Annotated[None, Depends(require_device_token)],
) -> dict[str, str]:
    conn.execute(
        """
        UPDATE devices
        SET last_seen_at = CURRENT_TIMESTAMP,
            last_status = ?,
            last_error = ?,
            battery_mv = ?,
            rssi = ?
        WHERE device_id = ?
        """,
        (request.status, request.error, request.battery_mv, request.rssi, device_id),
    )
    conn.execute(
        """
        INSERT INTO status_events (device_id, version, status, error, battery_mv, rssi)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (device_id, request.version, request.status, request.error, request.battery_mv, request.rssi),
    )
    conn.commit()
    return {"status": "ok"}


@app.get("/api/devices/{device_id}", dependencies=[Depends(require_admin)])
def get_device(device_id: str) -> dict[str, object]:
    device = row_to_dict(conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone())
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    return device


def _get_image_or_404(image_id: str) -> dict[str, object]:
    image = row_to_dict(conn.execute("SELECT * FROM images WHERE image_id = ?", (image_id,)).fetchone())
    if image is None:
        raise HTTPException(status_code=404, detail="unknown image")
    return image


def _image_response(image_id: str) -> dict[str, object]:
    image = _get_image_or_404(image_id)
    data_size = Path(str(image["data_path"])).stat().st_size
    return {
        "image_id": image["image_id"],
        "width": image["width"],
        "height": image["height"],
        "format": image["format"],
        "palette": PALETTE_RGB,
        "sha256": image["sha256"],
        "data_size": data_size,
        "data_url": f"/api/images/{image_id}/data",
        "preview_url": f"/api/images/{image_id}/preview",
        "created_at": image["created_at"],
    }


def _current_manifest(device_id: str) -> dict[str, object]:
    # Manifest intentionally stays small; the large binary is fetched from
    # download_url only when the version changes.
    device = row_to_dict(
        conn.execute(
            """
            SELECT d.device_id, d.current_version, i.*
            FROM devices d
            LEFT JOIN images i ON i.image_id = d.current_image_id
            WHERE d.device_id = ?
            """,
            (device_id,),
        ).fetchone()
    )
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    if device["image_id"] is None:
        return {"device_id": device_id, "version": device["current_version"], "has_image": False}

    return {
        "device_id": device_id,
        "version": device["current_version"],
        "has_image": True,
        "image_id": device["image_id"],
        "width": device["width"],
        "height": device["height"],
        "format": device["format"],
        "palette": PALETTE_RGB,
        "sha256": device["sha256"],
        "download_url": f"/api/images/{device['image_id']}/data",
    }
