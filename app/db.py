from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    image_id TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    direction TEXT NOT NULL,
    mode TEXT NOT NULL,
    dither INTEGER NOT NULL,
    format TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    data_path TEXT NOT NULL,
    preview_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    token TEXT,
    current_image_id TEXT,
    current_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT,
    last_status TEXT,
    last_error TEXT,
    battery_mv INTEGER,
    rssi INTEGER,
    FOREIGN KEY (current_image_id) REFERENCES images(image_id)
);

CREATE TABLE IF NOT EXISTS status_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    version INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    battery_mv INTEGER,
    rssi INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS upload_tokens (
    token_hash TEXT PRIMARY KEY,
    label TEXT,
    remaining_uses INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    # SQLite is enough for the first single-server deployment. The image files
    # stay on disk; the database only stores metadata and device state.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
