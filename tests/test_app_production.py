from __future__ import annotations

import importlib
import re
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
    monkeypatch.setenv("EPAPER_DEBUG_RETURN_EMAIL_TOKENS", "1")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    with TestClient(module.app) as client:
        yield client, module


def _png_bytes(color: str = "red") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 32), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _register(client: TestClient, email: str) -> dict[str, object]:
    response = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert response.status_code == 200, response.text
    return response.json()


def _auth_header(auth: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['access_token']}"}


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "admin-token"}


def _verify_user(client: TestClient, auth: dict[str, object]) -> dict[str, object]:
    request = client.post("/api/auth/verify-email/request", headers=_auth_header(auth))
    assert request.status_code == 200, request.text
    code = request.json()["code"]
    assert re.fullmatch(r"\d{6}", code)
    response = client.post("/api/auth/verify-email/confirm", json={"code": code})
    assert response.status_code == 200, response.text
    auth["user"] = response.json()
    return auth


def _register_verified(client: TestClient, email: str) -> dict[str, object]:
    return _verify_user(client, _register(client, email))


def _create_device(client: TestClient, device_id: str = "device001") -> dict[str, object]:
    response = client.post(f"/api/devices/{device_id}", headers=_admin_headers(), json={})
    assert response.status_code == 200, response.text
    return response.json()


def _claim_device(
    client: TestClient,
    auth: dict[str, object],
    device: dict[str, object],
    nickname: str | None = None,
) -> dict[str, object]:
    response = client.post(
        "/api/me/devices/claim",
        headers=_auth_header(auth),
        json={"device_id": device["device_id"], "claim_code": device["claim_code"], "nickname": nickname},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload_image(client: TestClient, headers: dict[str, str], color: str = "red") -> dict[str, object]:
    response = client.post(
        "/api/images",
        headers=headers,
        files={"file": ("test.png", _png_bytes(color), "image/png")},
        data={"direction": "auto", "mode": "scale", "dither": "true"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _invite(
    client: TestClient,
    auth: dict[str, object],
    device_id: str,
    email: str,
    role: str,
) -> dict[str, object]:
    response = client.post(
        f"/api/me/devices/{device_id}/invites",
        headers=_auth_header(auth),
        json={"email": email, "role": role},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _accept_invite(client: TestClient, auth: dict[str, object], token: str) -> dict[str, object]:
    response = client.post(
        "/api/me/device-invites/accept",
        headers=_auth_header(auth),
        json={"token": token},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_register_login_me_and_duplicate_email(app_context):
    client, _ = app_context
    auth = _register(client, "User@Example.com")
    assert auth["token_type"] == "bearer"
    assert auth["user"]["email"] == "user@example.com"
    assert auth["user"]["email_verified"] is False
    assert auth["user"]["email_verified_at"] is None

    login = client.post("/api/auth/login", json={"email": "user@example.com", "password": "password123"})
    assert login.status_code == 200, login.text

    me = client.get("/api/me", headers=_auth_header(login.json()))
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "user@example.com"

    duplicate = client.post("/api/auth/register", json={"email": "user@example.com", "password": "password123"})
    assert duplicate.status_code == 409


def test_email_verification_success_reuse_and_expiry(app_context):
    client, module = app_context
    auth = _register(client, "verify@example.com")
    request = client.post("/api/auth/verify-email/request", headers=_auth_header(auth))
    token = request.json()["code"]
    assert re.fullmatch(r"\d{6}", token)

    confirm = client.post("/api/auth/verify-email/confirm", json={"token": token})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["email_verified"] is True

    repeat = client.post("/api/auth/verify-email/confirm", json={"token": token})
    assert repeat.status_code == 400

    verified_request = client.post("/api/auth/verify-email/request", headers=_auth_header(auth))
    assert verified_request.status_code == 200
    assert verified_request.json() == {"status": "ok"}

    expired_auth = _register(client, "expired@example.com")
    expired_request = client.post("/api/auth/verify-email/request", headers=_auth_header(expired_auth))
    expired_token = expired_request.json()["code"]
    assert re.fullmatch(r"\d{6}", expired_token)
    module.conn.execute(
        "UPDATE email_verification_tokens SET expires_at = '2000-01-01 00:00:00' WHERE token_hash = ?",
        (module._token_hash(expired_token),),
    )
    module.conn.commit()
    expired = client.post("/api/auth/verify-email/confirm", json={"token": expired_token})
    assert expired.status_code == 400


def test_unverified_email_cannot_claim_upload_or_assign(app_context):
    client, _ = app_context
    auth = _register(client, "unverified@example.com")
    device = _create_device(client, "unverified-device")

    claim = client.post(
        "/api/me/devices/claim",
        headers=_auth_header(auth),
        json={"device_id": device["device_id"], "claim_code": device["claim_code"]},
    )
    assert claim.status_code == 403
    assert claim.json()["detail"] == "email not verified"

    upload = client.post(
        "/api/images",
        headers=_auth_header(auth),
        files={"file": ("test.png", _png_bytes(), "image/png")},
        data={"direction": "auto", "mode": "scale", "dither": "true"},
    )
    assert upload.status_code == 403


def test_password_reset_request_and_confirm(app_context):
    client, module = app_context
    _register(client, "reset@example.com")
    missing = client.post("/api/auth/password-reset/request", json={"email": "missing@example.com"})
    assert missing.status_code == 200
    assert missing.json() == {"status": "ok"}

    request = client.post("/api/auth/password-reset/request", json={"email": "reset@example.com"})
    assert request.status_code == 200
    token = request.json()["code"]
    assert re.fullmatch(r"\d{6}", token)

    confirm = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": "newpassword123"},
    )
    assert confirm.status_code == 200

    repeat = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "new_password": "anotherpassword123"},
    )
    assert repeat.status_code == 400

    login = client.post("/api/auth/login", json={"email": "reset@example.com", "password": "newpassword123"})
    assert login.status_code == 200

    expired_request = client.post("/api/auth/password-reset/request", json={"email": "reset@example.com"})
    expired_token = expired_request.json()["code"]
    assert re.fullmatch(r"\d{6}", expired_token)
    module.conn.execute(
        "UPDATE password_reset_tokens SET expires_at = '2000-01-01 00:00:00' WHERE token_hash = ?",
        (module._token_hash(expired_token),),
    )
    module.conn.commit()
    expired = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": expired_token, "new_password": "newpassword456"},
    )
    assert expired.status_code == 400


def test_claim_creates_owner_membership_and_repeat_binding_fails(app_context):
    client, _ = app_context
    owner = _register_verified(client, "owner@example.com")
    other = _register_verified(client, "other@example.com")
    device = _create_device(client, "claim-device")

    claimed = _claim_device(client, owner, device, "Desk")
    assert claimed["role"] == "owner"
    assert claimed["nickname"] == "Desk"
    assert "token" not in claimed
    assert "claim_code_hash" not in claimed

    members = client.get("/api/me/devices/claim-device/members", headers=_auth_header(owner))
    assert members.status_code == 200
    assert members.json()["members"][0]["role"] == "owner"

    repeat = client.post(
        "/api/me/devices/claim",
        headers=_auth_header(other),
        json={"device_id": "claim-device", "claim_code": device["claim_code"]},
    )
    assert repeat.status_code == 409


def test_owner_admin_viewer_permissions_and_status_history(app_context):
    client, _ = app_context
    owner = _register_verified(client, "owner@example.com")
    admin = _register_verified(client, "admin@example.com")
    viewer = _register_verified(client, "viewer@example.com")
    stranger = _register_verified(client, "stranger@example.com")
    device = _create_device(client, "shared-device")
    _claim_device(client, owner, device)

    admin_invite = _invite(client, owner, "shared-device", "admin@example.com", "admin")
    _accept_invite(client, admin, admin_invite["token"])
    viewer_invite = _invite(client, admin, "shared-device", "viewer@example.com", "viewer")
    _accept_invite(client, viewer, viewer_invite["token"])

    assert client.patch(
        "/api/me/devices/shared-device",
        headers=_auth_header(admin),
        json={"nickname": "Updated"},
    ).status_code == 200
    assert client.patch(
        "/api/me/devices/shared-device",
        headers=_auth_header(viewer),
        json={"nickname": "Nope"},
    ).status_code == 403

    admin_image = _upload_image(client, _auth_header(admin), "blue")
    assert client.post(
        "/api/me/devices/shared-device/assign",
        headers=_auth_header(admin),
        json={"image_id": admin_image["image_id"]},
    ).status_code == 200

    viewer_image = _upload_image(client, _auth_header(viewer), "green")
    assert client.post(
        "/api/me/devices/shared-device/assign",
        headers=_auth_header(viewer),
        json={"image_id": viewer_image["image_id"]},
    ).status_code == 403

    assert client.get("/api/me/devices/shared-device", headers=_auth_header(stranger)).status_code == 404
    assert client.get("/api/me/devices/shared-device/members", headers=_auth_header(stranger)).status_code == 404
    assert client.get("/api/me/devices/shared-device/status-events", headers=_auth_header(stranger)).status_code == 404
    assert client.get("/api/me/devices/shared-device/status-events", headers=_auth_header(viewer)).status_code == 200


def test_invite_email_mismatch_expiry_and_repeat_accept(app_context):
    client, module = app_context
    owner = _register_verified(client, "owner@example.com")
    invited = _register_verified(client, "invited@example.com")
    wrong = _register_verified(client, "wrong@example.com")
    device = _create_device(client, "invite-device")
    _claim_device(client, owner, device)

    invite = _invite(client, owner, "invite-device", "invited@example.com", "viewer")
    mismatch = client.post(
        "/api/me/device-invites/accept",
        headers=_auth_header(wrong),
        json={"token": invite["token"]},
    )
    assert mismatch.status_code == 403
    assert _accept_invite(client, invited, invite["token"])["role"] == "viewer"
    repeat = client.post(
        "/api/me/device-invites/accept",
        headers=_auth_header(invited),
        json={"token": invite["token"]},
    )
    assert repeat.status_code == 400

    expired_invite = _invite(client, owner, "invite-device", "wrong@example.com", "viewer")
    module.conn.execute(
        "UPDATE device_invites SET expires_at = '2000-01-01 00:00:00' WHERE token_hash = ?",
        (module._token_hash(expired_invite["token"]),),
    )
    module.conn.commit()
    expired = client.post(
        "/api/me/device-invites/accept",
        headers=_auth_header(wrong),
        json={"token": expired_invite["token"]},
    )
    assert expired.status_code == 400


def test_member_delete_and_unbind_behaviors(app_context):
    client, _ = app_context
    owner = _register_verified(client, "owner@example.com")
    admin = _register_verified(client, "admin@example.com")
    viewer = _register_verified(client, "viewer@example.com")
    device = _create_device(client, "delete-device")
    _claim_device(client, owner, device)
    admin_invite = _invite(client, owner, "delete-device", "admin@example.com", "admin")
    _accept_invite(client, admin, admin_invite["token"])
    viewer_invite = _invite(client, owner, "delete-device", "viewer@example.com", "viewer")
    _accept_invite(client, viewer, viewer_invite["token"])

    owner_id = owner["user"]["user_id"]
    admin_id = admin["user"]["user_id"]
    viewer_id = viewer["user"]["user_id"]

    last_owner = client.delete(f"/api/me/devices/delete-device/members/{owner_id}", headers=_auth_header(owner))
    assert last_owner.status_code == 400

    removed = client.delete(f"/api/me/devices/delete-device/members/{viewer_id}", headers=_auth_header(owner))
    assert removed.status_code == 200

    left = client.delete("/api/me/devices/delete-device", headers=_auth_header(admin))
    assert left.status_code == 200
    assert client.get("/api/me/devices/delete-device", headers=_auth_header(admin)).status_code == 404
    assert client.get("/api/me/devices/delete-device", headers=_auth_header(owner)).status_code == 200

    # Re-add admin, then owner unbind clears all members.
    admin_invite2 = _invite(client, owner, "delete-device", "admin@example.com", "admin")
    _accept_invite(client, admin, admin_invite2["token"])
    unbound = client.delete("/api/me/devices/delete-device", headers=_auth_header(owner))
    assert unbound.status_code == 200
    assert client.get("/api/me/devices/delete-device", headers=_auth_header(owner)).status_code == 404
    assert client.get("/api/me/devices/delete-device", headers=_auth_header(admin)).status_code == 404


def test_image_metadata_preview_authorization(app_context):
    client, _ = app_context
    owner = _register_verified(client, "owner@example.com")
    member = _register_verified(client, "member@example.com")
    stranger = _register_verified(client, "stranger@example.com")
    device = _create_device(client, "image-device")
    _claim_device(client, owner, device)
    image = _upload_image(client, _auth_header(owner), "yellow")

    assert client.get(f"/api/images/{image['image_id']}").status_code == 401
    assert client.get(f"/api/images/{image['image_id']}", headers=_auth_header(stranger)).status_code == 403
    assert client.get(f"/api/images/{image['image_id']}", headers=_auth_header(owner)).status_code == 200
    assert client.get(f"/api/images/{image['image_id']}/preview", headers=_auth_header(owner)).status_code == 200

    client.post(
        "/api/me/devices/image-device/assign",
        headers=_auth_header(owner),
        json={"image_id": image["image_id"]},
    )
    invite = _invite(client, owner, "image-device", "member@example.com", "viewer")
    _accept_invite(client, member, invite["token"])
    assert client.get(f"/api/images/{image['image_id']}", headers=_auth_header(member)).status_code == 200

    legacy = _upload_image(client, _admin_headers(), "red")
    assert client.get(f"/api/images/{legacy['image_id']}").status_code == 200
    assert client.get(f"/api/images/{legacy['image_id']}/preview").status_code == 200


def test_admin_upload_token_and_esp32_protocol_still_work(app_context):
    client, _ = app_context
    admin_image = _upload_image(client, _admin_headers(), "red")
    token_response = client.post("/api/upload-tokens", headers=_admin_headers(), json={"uses": 1, "label": "guest"})
    upload_token = token_response.json()["token"]
    guest_image = _upload_image(client, {"X-Upload-Token": upload_token}, "blue")
    assert admin_image["data_size"] == 192000
    assert guest_image["data_size"] == 192000

    device = _create_device(client, "esp32-device")
    assigned = client.post(
        "/api/devices/esp32-device/assign",
        headers=_admin_headers(),
        json={"image_id": admin_image["image_id"]},
    )
    assert assigned.status_code == 200

    manifest = client.get(
        "/api/devices/esp32-device/current",
        headers={"X-Device-Token": device["token"]},
    )
    assert manifest.status_code == 200
    body = manifest.json()
    data = client.get(body["download_url"])
    assert data.status_code == 200
    assert len(data.content) == 192000

    status = client.post(
        "/api/devices/esp32-device/status",
        headers={"X-Device-Token": device["token"]},
        json={"version": body["version"], "status": "displayed", "battery_mv": 3800, "rssi": -62},
    )
    assert status.status_code == 200
