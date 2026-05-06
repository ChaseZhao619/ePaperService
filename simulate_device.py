from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate an ESP32 e-paper device over HTTP.")
    parser.add_argument("--server", required=True, help="Server base URL, for example http://127.0.0.1:8000")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--token", default="")
    parser.add_argument("--known-version", type=int, default=-1)
    args = parser.parse_args()

    headers = {}
    if args.token:
        headers["X-Device-Token"] = args.token

    manifest_url = f"{args.server.rstrip('/')}/api/devices/{args.device_id}/current"
    manifest = _json_request(manifest_url, headers=headers)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))

    if not manifest.get("has_image"):
        _post_status(args.server, args.device_id, args.token, "idle", manifest.get("version"))
        print("No image assigned.")
        return 0

    version = int(manifest["version"])
    if version == args.known_version:
        _post_status(args.server, args.device_id, args.token, "unchanged", version)
        print(f"Version {version} is unchanged.")
        return 0

    download_url = manifest["download_url"]
    if download_url.startswith("/"):
        download_url = f"{args.server.rstrip('/')}{download_url}"
    data = _bytes_request(download_url, headers=headers)
    # This mirrors the firmware-side integrity checks before refreshing the
    # e-paper display. A failed check should never update the panel.
    expected_size = int(manifest["width"]) * int(manifest["height"]) // 2
    actual_hash = hashlib.sha256(data).hexdigest()

    if len(data) != expected_size:
        error = f"size mismatch: got {len(data)}, expected {expected_size}"
        _post_status(args.server, args.device_id, args.token, "error", version, error)
        raise RuntimeError(error)
    if actual_hash != manifest["sha256"]:
        error = f"sha256 mismatch: got {actual_hash}, expected {manifest['sha256']}"
        _post_status(args.server, args.device_id, args.token, "error", version, error)
        raise RuntimeError(error)

    _post_status(args.server, args.device_id, args.token, "displayed", version)
    print(f"Downloaded version {version}: {len(data)} bytes, sha256 ok.")
    return 0


def _json_request(url: str, *, headers: dict[str, str]) -> dict[str, object]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _bytes_request(url: str, *, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _post_status(
    server: str,
    device_id: str,
    token: str,
    status: str,
    version: object,
    error: str | None = None,
) -> None:
    body = json.dumps({"status": status, "version": version, "error": error}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Device-Token"] = token
    request = urllib.request.Request(
        f"{server.rstrip('/')}/api/devices/{device_id}/status",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except urllib.error.HTTPError as exc:
        print(f"status report failed: HTTP {exc.code}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
