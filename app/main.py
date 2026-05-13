from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
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
AUTH_SECRET = os.getenv("EPAPER_AUTH_SECRET") or ADMIN_TOKEN or "dev-auth-secret"
AUTH_TOKEN_TTL_SECONDS = int(os.getenv("EPAPER_AUTH_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 30)))
PASSWORD_HASH_ITERATIONS = 260_000

# The service is designed for one small Ubuntu ECS instance: local files hold
# generated images and SQLite holds the current device assignment/version.
app = FastAPI(title="ePaper Service", version="0.1.0")
conn = connect(DB_PATH)
init_db(conn)


class AssignRequest(BaseModel):
    image_id: str = Field(min_length=1)


class DeviceCreateRequest(BaseModel):
    token: str | None = Field(default=None, description="Optional fixed token for the device")
    claim_code: str | None = Field(default=None, description="Optional fixed claim code for app pairing")


class AuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=1024)


class User(BaseModel):
    user_id: str
    email: str
    created_at: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: User


class UploadTokenCreateRequest(BaseModel):
    uses: int = Field(default=1, ge=1, le=100)
    label: str | None = Field(default=None, max_length=120)


class DeviceClaimRequest(BaseModel):
    device_id: str = Field(min_length=1)
    claim_code: str = Field(min_length=1)
    nickname: str | None = Field(default=None, max_length=120)


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


def require_user(authorization: Annotated[str | None, Header()] = None) -> dict[str, object]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    payload = _verify_access_token(token)
    row = conn.execute(
        "SELECT user_id, email, created_at FROM users WHERE user_id = ?",
        (payload["user_id"],),
    ).fetchone()
    user = row_to_dict(row)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid bearer token")
    return user


def require_upload_token(
    x_admin_token: Annotated[str | None, Header()] = None,
    x_upload_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    # Upload accepts either the full admin token or a limited-use guest token.
    if ADMIN_TOKEN and x_admin_token == ADMIN_TOKEN:
        return {"kind": "admin"}

    if authorization:
        user = require_user(authorization)
        return {"kind": "user", "user_id": str(user["user_id"])}

    token = x_upload_token or x_admin_token
    if not token:
        raise HTTPException(status_code=401, detail="missing upload token")

    token_hash = _token_hash(token)
    cursor = conn.execute(
        """
        UPDATE upload_tokens
        SET remaining_uses = remaining_uses - 1,
            last_used_at = CURRENT_TIMESTAMP
        WHERE token_hash = ? AND remaining_uses > 0
        """,
        (token_hash,),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise HTTPException(status_code=401, detail="invalid or expired upload token")
    return {"kind": "upload"}


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


@app.post("/api/auth/register", response_model=AuthResponse)
def register(request: AuthRequest) -> AuthResponse:
    email = _normalize_email(request.email)
    user_id = secrets.token_urlsafe(18)
    password_hash = _hash_password(request.password)
    try:
        conn.execute(
            "INSERT INTO users (user_id, email, password_hash) VALUES (?, ?, ?)",
            (user_id, email, password_hash),
        )
        conn.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="email already registered") from exc
        raise

    user = row_to_dict(
        conn.execute(
            "SELECT user_id, email, created_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    )
    assert user is not None
    return AuthResponse(access_token=_create_access_token(user), user=User(**user))


@app.post("/api/auth/login", response_model=AuthResponse)
def login(request: AuthRequest) -> AuthResponse:
    email = _normalize_email(request.email)
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    user_row = row_to_dict(row)
    if user_row is None or not _verify_password(request.password, str(user_row["password_hash"])):
        raise HTTPException(status_code=401, detail="invalid email or password")

    user = {
        "user_id": user_row["user_id"],
        "email": user_row["email"],
        "created_at": user_row["created_at"],
    }
    return AuthResponse(access_token=_create_access_token(user), user=User(**user))


@app.get("/api/me", response_model=User)
def me(user: Annotated[dict[str, object], Depends(require_user)]) -> User:
    return User(**user)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ePaper Service</title>
  <style>
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #17202a;
      background: #f5f7f8;
    }
    main { width: min(960px, calc(100% - 32px)); margin: 28px auto 48px; }
    h1 { margin: 0 0 8px; font-size: 28px; letter-spacing: 0; }
    p { margin: 0 0 20px; color: #51606d; line-height: 1.55; }
    .panel {
      background: #fff;
      border: 1px solid #dce3e8;
      border-radius: 8px;
      padding: 20px;
      box-shadow: 0 1px 2px rgb(20 31 42 / 6%);
    }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
    label { display: block; margin: 0 0 6px; font-weight: 650; font-size: 14px; }
    input, select, button {
      width: 100%;
      min-height: 40px;
      font: inherit;
      border-radius: 6px;
    }
    input, select {
      border: 1px solid #b8c5cf;
      background: #fff;
      padding: 8px 10px;
    }
    input[type="checkbox"] { width: auto; min-height: auto; margin-right: 8px; }
    button {
      border: 0;
      background: #176b87;
      color: #fff;
      font-weight: 700;
      cursor: pointer;
      padding: 10px 14px;
    }
    button:disabled { opacity: .6; cursor: wait; }
    .full { grid-column: 1 / -1; }
    .checkline { display: flex; align-items: center; min-height: 40px; }
    .result { display: none; margin-top: 18px; }
    .meta {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin: 14px 0;
    }
    .meta div {
      border: 1px solid #dce3e8;
      border-radius: 6px;
      padding: 10px;
      background: #f9fbfc;
      min-width: 0;
    }
    .meta b { display: block; font-size: 12px; color: #667582; margin-bottom: 4px; }
    .meta span { overflow-wrap: anywhere; }
    .preview {
      width: 100%;
      border: 1px solid #dce3e8;
      border-radius: 6px;
      background: #fff;
      image-rendering: pixelated;
    }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
    .actions a {
      display: inline-flex;
      align-items: center;
      min-height: 38px;
      padding: 8px 12px;
      border-radius: 6px;
      border: 1px solid #176b87;
      color: #176b87;
      text-decoration: none;
      font-weight: 650;
    }
    .message { margin-top: 12px; color: #a43131; min-height: 24px; }
    @media (max-width: 720px) {
      .grid, .meta { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <h1>ePaper Image Tool</h1>
    <p>上传图片后会生成电子纸使用的 6 色预览图和二进制数据文件。</p>

    <section class="panel">
      <form id="upload-form" class="grid">
        <div class="full">
          <label for="admin-token">上传 Token</label>
          <input id="admin-token" name="admin-token" type="password" autocomplete="current-password" required>
        </div>
        <div class="full">
          <label for="file">图片</label>
          <input id="file" name="file" type="file" accept="image/*,.dng,.DNG" required>
        </div>
        <div>
          <label for="direction">方向</label>
          <select id="direction" name="direction">
            <option value="auto">自动</option>
            <option value="landscape">横屏 800x480</option>
            <option value="portrait">竖屏 480x800</option>
          </select>
        </div>
        <div>
          <label for="mode">适配方式</label>
          <select id="mode" name="mode">
            <option value="scale">铺满并居中裁切</option>
            <option value="cut">完整显示并补白</option>
          </select>
        </div>
        <div class="full checkline">
          <label><input id="dither" name="dither" type="checkbox" checked value="true">启用抖动</label>
        </div>
        <div class="full">
          <button id="submit-button" type="submit">上传并处理</button>
          <div id="message" class="message"></div>
        </div>
      </form>

      <section id="result" class="result">
        <div class="meta">
          <div><b>Image ID</b><span id="image-id"></span></div>
          <div><b>尺寸</b><span id="image-size"></span></div>
          <div><b>数据大小</b><span id="data-size"></span></div>
          <div><b>格式</b><span id="format"></span></div>
        </div>
        <img id="preview" class="preview" alt="Processed preview">
        <div class="actions">
          <a id="preview-link" href="#" download>下载 BMP 预览图</a>
          <a id="data-link" href="#" download>下载 EPD 数据文件</a>
        </div>
      </section>
    </section>
  </main>

  <script>
    const form = document.getElementById("upload-form");
    const tokenInput = document.getElementById("admin-token");
    const button = document.getElementById("submit-button");
    const message = document.getElementById("message");
    const result = document.getElementById("result");

    tokenInput.value = localStorage.getItem("epaperAdminToken") || "";

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      message.textContent = "";
      result.style.display = "none";
      button.disabled = true;
      button.textContent = "处理中...";

      const token = tokenInput.value.trim();
      const body = new FormData();
      body.append("file", document.getElementById("file").files[0]);
      body.append("direction", document.getElementById("direction").value);
      body.append("mode", document.getElementById("mode").value);
      body.append("dither", document.getElementById("dither").checked ? "true" : "false");

      try {
        const response = await fetch("/api/images", {
          method: "POST",
          headers: { "X-Admin-Token": token },
          body,
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail || "upload failed");
        }

        localStorage.setItem("epaperAdminToken", token);
        document.getElementById("image-id").textContent = payload.image_id;
        document.getElementById("image-size").textContent = `${payload.width} x ${payload.height}`;
        document.getElementById("data-size").textContent = `${payload.data_size} bytes`;
        document.getElementById("format").textContent = payload.format;
        document.getElementById("preview").src = `${payload.preview_url}?t=${Date.now()}`;
        document.getElementById("preview-link").href = payload.preview_url;
        document.getElementById("data-link").href = payload.data_url;
        result.style.display = "block";
      } catch (error) {
        message.textContent = error.message;
      } finally {
        button.disabled = false;
        button.textContent = "上传并处理";
      }
    });
  </script>
</body>
</html>
"""


@app.post("/api/devices/{device_id}", dependencies=[Depends(require_admin)])
def create_device(device_id: str, request: DeviceCreateRequest) -> dict[str, str]:
    token = request.token or secrets.token_urlsafe(24)
    claim_code = request.claim_code or secrets.token_urlsafe(12)
    conn.execute(
        """
        INSERT INTO devices (device_id, token, claim_code_hash)
        VALUES (?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            token = excluded.token,
            claim_code_hash = excluded.claim_code_hash,
            owner_user_id = NULL,
            claimed_at = NULL,
            nickname = NULL,
            updated_at = CURRENT_TIMESTAMP
        """,
        (device_id, token, _token_hash(claim_code)),
    )
    conn.commit()
    return {"device_id": device_id, "token": token, "claim_code": claim_code}


@app.post("/api/upload-tokens", dependencies=[Depends(require_admin)])
def create_upload_token(request: UploadTokenCreateRequest) -> dict[str, object]:
    token = f"up_{secrets.token_urlsafe(24)}"
    conn.execute(
        """
        INSERT INTO upload_tokens (token_hash, label, remaining_uses)
        VALUES (?, ?, ?)
        """,
        (_token_hash(token), request.label, request.uses),
    )
    conn.commit()
    return {"token": token, "uses": request.uses, "label": request.label}


@app.get("/api/upload-tokens", dependencies=[Depends(require_admin)])
def list_upload_tokens() -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT label, remaining_uses, created_at, last_used_at
        FROM upload_tokens
        ORDER BY created_at DESC
        LIMIT 100
        """
    ).fetchall()
    return {"tokens": [row_to_dict(row) for row in rows]}


@app.post("/api/me/devices/claim")
def claim_device(
    request: DeviceClaimRequest,
    user: Annotated[dict[str, object], Depends(require_user)],
) -> dict[str, object]:
    device = row_to_dict(
        conn.execute("SELECT * FROM devices WHERE device_id = ?", (request.device_id,)).fetchone()
    )
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    if device["owner_user_id"]:
        raise HTTPException(status_code=409, detail="device already claimed")
    if not device["claim_code_hash"] or not hmac.compare_digest(
        str(device["claim_code_hash"]),
        _token_hash(request.claim_code),
    ):
        raise HTTPException(status_code=401, detail="invalid claim code")

    conn.execute(
        """
        UPDATE devices
        SET owner_user_id = ?,
            claimed_at = CURRENT_TIMESTAMP,
            nickname = ?,
            claim_code_hash = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE device_id = ?
        """,
        (user["user_id"], request.nickname, request.device_id),
    )
    conn.commit()
    return _get_app_device_or_404(request.device_id, str(user["user_id"]))


@app.get("/api/me/devices")
def list_my_devices(user: Annotated[dict[str, object], Depends(require_user)]) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT * FROM devices
        WHERE owner_user_id = ?
        ORDER BY claimed_at DESC, device_id ASC
        """,
        (user["user_id"],),
    ).fetchall()
    return {"devices": [_app_device_response(row_to_dict(row) or {}) for row in rows]}


@app.get("/api/me/devices/{device_id}")
def get_my_device(
    device_id: str,
    user: Annotated[dict[str, object], Depends(require_user)],
) -> dict[str, object]:
    return _get_app_device_or_404(device_id, str(user["user_id"]))


@app.delete("/api/me/devices/{device_id}")
def unclaim_my_device(
    device_id: str,
    user: Annotated[dict[str, object], Depends(require_user)],
) -> dict[str, str]:
    cursor = conn.execute(
        """
        UPDATE devices
        SET owner_user_id = NULL,
            claimed_at = NULL,
            nickname = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE device_id = ? AND owner_user_id = ?
        """,
        (device_id, user["user_id"]),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise HTTPException(status_code=404, detail="unknown device")
    return {"status": "ok"}


@app.post("/api/images")
def upload_image(
    file: Annotated[UploadFile, File()],
    auth: Annotated[dict[str, str], Depends(require_upload_token)],
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
            format, sha256, data_path, preview_path, owner_user_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            auth.get("user_id") if auth["kind"] == "user" else None,
        ),
    )
    conn.commit()

    return _image_response(image_id)


@app.post("/api/me/devices/{device_id}/assign")
def assign_my_device_image(
    device_id: str,
    request: AssignRequest,
    user: Annotated[dict[str, object], Depends(require_user)],
) -> dict[str, object]:
    user_id = str(user["user_id"])
    _get_app_device_or_404(device_id, user_id)
    image = _get_image_or_404(request.image_id)
    if image.get("owner_user_id") != user_id:
        raise HTTPException(status_code=403, detail="image does not belong to user")
    return _assign_image_to_device(device_id, request.image_id)


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
    return _assign_image_to_device(device_id, request.image_id)


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


def _assign_image_to_device(device_id: str, image_id: str) -> dict[str, object]:
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
        (device_id, image_id),
    )
    conn.commit()
    return _current_manifest(device_id)


def _get_app_device_or_404(device_id: str, user_id: str) -> dict[str, object]:
    device = row_to_dict(
        conn.execute(
            "SELECT * FROM devices WHERE device_id = ? AND owner_user_id = ?",
            (device_id, user_id),
        ).fetchone()
    )
    if device is None:
        raise HTTPException(status_code=404, detail="unknown device")
    return _app_device_response(device)


def _app_device_response(device: dict[str, object]) -> dict[str, object]:
    return {
        "device_id": device["device_id"],
        "nickname": device["nickname"],
        "current_image_id": device["current_image_id"],
        "current_version": device["current_version"],
        "updated_at": device["updated_at"],
        "last_seen_at": device["last_seen_at"],
        "last_status": device["last_status"],
        "last_error": device["last_error"],
        "battery_mv": device["battery_mv"],
        "rssi": device["rssi"],
        "claimed_at": device["claimed_at"],
    }


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_HASH_ITERATIONS,
        _b64url_encode(salt),
        _b64url_encode(digest),
    )


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = _b64url_decode(salt_text)
        expected = _b64url_decode(digest_text)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _create_access_token(user: dict[str, object]) -> str:
    payload = {
        "user_id": user["user_id"],
        "email": user["email"],
        "exp": int(time.time()) + AUTH_TOKEN_TTL_SECONDS,
    }
    payload_text = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign_token_payload(payload_text)
    return f"{payload_text}.{signature}"


def _verify_access_token(token: str) -> dict[str, object]:
    try:
        payload_text, signature = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid bearer token") from exc
    expected = _sign_token_payload(payload_text)
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")
    try:
        payload = json.loads(_b64url_decode(payload_text).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid bearer token") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="expired bearer token")
    if not payload.get("user_id"):
        raise HTTPException(status_code=401, detail="invalid bearer token")
    return payload


def _sign_token_payload(payload_text: str) -> str:
    digest = hmac.new(AUTH_SECRET.encode("utf-8"), payload_text.encode("utf-8"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
