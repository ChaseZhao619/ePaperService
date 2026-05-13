from __future__ import annotations

import importlib
import sys
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def app_context(tmp_path, monkeypatch):
    monkeypatch.setenv("EPAPER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EPAPER_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("EPAPER_AUTH_SECRET", "test-auth-secret")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    with TestClient(module.app) as client:
        yield client, module


def _png_bytes(color: str = "red") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 32), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _register(client: TestClient, email: str) -> dict[str, object]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _auth_header(auth: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['access_token']}"}


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "admin-token"}


def _create_device(client: TestClient, device_id: str = "device001") -> dict[str, object]:
    response = client.post(
        f"/api/devices/{device_id}",
        headers=_admin_headers(),
        json={},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload_image(
    client: TestClient,
    headers: dict[str, str],
    color: str = "red",
) -> dict[str, object]:
    response = client.post(
        "/api/images",
        headers=headers,
        files={"file": ("test.png", _png_bytes(color), "image/png")},
        data={"direction": "auto", "mode": "scale", "dither": "true"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_register_login_and_me(app_context):
    client, _ = app_context
    auth = _register(client, "User@Example.com")
    assert auth["token_type"] == "bearer"
    assert auth["user"]["email"] == "user@example.com"

    login = client.post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "password123"},
    )
    assert login.status_code == 200, login.text

    me = client.get("/api/me", headers=_auth_header(login.json()))
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "user@example.com"


def test_duplicate_email_register_fails(app_context):
    client, _ = app_context
    _register(client, "dupe@example.com")
    response = client.post(
        "/api/auth/register",
        json={"email": "dupe@example.com", "password": "password123"},
    )
    assert response.status_code == 409


def test_claim_code_success_and_wrong_code_failure(app_context):
    client, _ = app_context
    auth = _register(client, "owner@example.com")
    device = _create_device(client, "claim-ok")

    wrong = client.post(
        "/api/me/devices/claim",
        headers=_auth_header(auth),
        json={"device_id": "claim-ok", "claim_code": "wrong"},
    )
    assert wrong.status_code == 401

    claimed = client.post(
        "/api/me/devices/claim",
        headers=_auth_header(auth),
        json={"device_id": "claim-ok", "claim_code": device["claim_code"], "nickname": "Desk"},
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["device_id"] == "claim-ok"
    assert body["nickname"] == "Desk"
    assert "token" not in body
    assert "claim_code_hash" not in body


def test_device_repeat_binding_fails(app_context):
    client, _ = app_context
    first = _register(client, "first@example.com")
    second = _register(client, "second@example.com")
    device = _create_device(client, "repeat-device")
    assert client.post(
        "/api/me/devices/claim",
        headers=_auth_header(first),
        json={"device_id": "repeat-device", "claim_code": device["claim_code"]},
    ).status_code == 200

    response = client.post(
        "/api/me/devices/claim",
        headers=_auth_header(second),
        json={"device_id": "repeat-device", "claim_code": device["claim_code"]},
    )
    assert response.status_code == 409


def test_user_cannot_view_unbind_or_assign_other_users_device(app_context):
    client, _ = app_context
    owner = _register(client, "owner@example.com")
    other = _register(client, "other@example.com")
    device = _create_device(client, "private-device")
    claim = client.post(
        "/api/me/devices/claim",
        headers=_auth_header(owner),
        json={"device_id": "private-device", "claim_code": device["claim_code"]},
    )
    assert claim.status_code == 200

    assert client.get("/api/me/devices/private-device", headers=_auth_header(other)).status_code == 404
    assert client.delete("/api/me/devices/private-device", headers=_auth_header(other)).status_code == 404

    image = _upload_image(client, _auth_header(other), "blue")
    assign = client.post(
        "/api/me/devices/private-device/assign",
        headers=_auth_header(other),
        json={"image_id": image["image_id"]},
    )
    assert assign.status_code == 404


def test_user_cannot_assign_other_users_image(app_context):
    client, _ = app_context
    owner = _register(client, "owner@example.com")
    other = _register(client, "other@example.com")
    device = _create_device(client, "owned-device")
    assert client.post(
        "/api/me/devices/claim",
        headers=_auth_header(owner),
        json={"device_id": "owned-device", "claim_code": device["claim_code"]},
    ).status_code == 200

    other_image = _upload_image(client, _auth_header(other), "green")
    assign = client.post(
        "/api/me/devices/owned-device/assign",
        headers=_auth_header(owner),
        json={"image_id": other_image["image_id"]},
    )
    assert assign.status_code == 403


def test_bearer_upload_sets_owner_user_id(app_context):
    client, module = app_context
    auth = _register(client, "uploader@example.com")
    image = _upload_image(client, _auth_header(auth), "yellow")
    row = module.conn.execute(
        "SELECT owner_user_id FROM images WHERE image_id = ?",
        (image["image_id"],),
    ).fetchone()
    assert row["owner_user_id"] == auth["user"]["user_id"]


def test_admin_and_upload_token_upload_still_work(app_context):
    client, module = app_context
    admin_image = _upload_image(client, _admin_headers(), "red")
    admin_row = module.conn.execute(
        "SELECT owner_user_id FROM images WHERE image_id = ?",
        (admin_image["image_id"],),
    ).fetchone()
    assert admin_row["owner_user_id"] is None

    token_response = client.post(
        "/api/upload-tokens",
        headers=_admin_headers(),
        json={"uses": 1, "label": "guest"},
    )
    assert token_response.status_code == 200, token_response.text
    upload_token = token_response.json()["token"]
    guest_image = _upload_image(client, {"X-Upload-Token": upload_token}, "blue")
    guest_row = module.conn.execute(
        "SELECT owner_user_id FROM images WHERE image_id = ?",
        (guest_image["image_id"],),
    ).fetchone()
    assert guest_row["owner_user_id"] is None


def test_esp32_current_status_and_data_protocol_still_work(app_context):
    client, _ = app_context
    device = _create_device(client, "esp32-device")
    image = _upload_image(client, _admin_headers(), "red")
    assigned = client.post(
        "/api/devices/esp32-device/assign",
        headers=_admin_headers(),
        json={"image_id": image["image_id"]},
    )
    assert assigned.status_code == 200, assigned.text

    manifest = client.get(
        "/api/devices/esp32-device/current",
        headers={"X-Device-Token": device["token"]},
    )
    assert manifest.status_code == 200, manifest.text
    manifest_body = manifest.json()
    assert manifest_body["has_image"] is True
    assert manifest_body["download_url"].endswith("/data")

    data = client.get(manifest_body["download_url"])
    assert data.status_code == 200
    assert len(data.content) == 192000

    status = client.post(
        "/api/devices/esp32-device/status",
        headers={"X-Device-Token": device["token"]},
        json={"version": manifest_body["version"], "status": "displayed"},
    )
    assert status.status_code == 200, status.text
    assert status.json() == {"status": "ok"}
