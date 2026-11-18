"""User settings persisted in the settings key-value table.

Values are stored as TEXT; `source_dirs` is stored as a JSON array so the
round-trip through get_settings() always yields a list.
"""

import json
import sqlite3

_DEFAULTS = {
    "source_dirs": [],       # list of directories to scan
    "phash_threshold": 10,   # pHash hamming distance threshold
    "time_window_sec": 60,   # capture-time clustering window
    "rejected_root": "",     # empty -> default computed from first source dir
    "face_engine": "auto",   # auto | mediapipe | haar | none
}


def _decode(key: str, raw: str):
    """Parse a stored TEXT value back into the setting's natural type."""
    default = _DEFAULTS[key]
    if isinstance(default, list):
        value = json.loads(raw)
        return value if isinstance(value, list) else default
    if isinstance(default, int):
        return int(raw)
    return raw


def _encode(key: str, value) -> str:
    if isinstance(_DEFAULTS[key], list):
        return json.dumps(value)
    return str(value)


def get_settings(conn: sqlite3.Connection) -> dict:
    """Return the full settings dict, filling defaults for missing keys."""
    stored = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM settings")
    }
    result = {}
    for key, default in _DEFAULTS.items():
        raw = stored.get(key)
        result[key] = _decode(key, raw) if raw is not None else default
    return result


def update_settings(conn: sqlite3.Connection, **kwargs) -> None:
    """Update whitelisted settings; unknown keys raise ValueError.

    Validation happens before any write, so a bad call never applies
    a partial update.
    """
    unknown = [key for key in kwargs if key not in _DEFAULTS]
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    for key, value in kwargs.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, _encode(key, value)),
        )
    conn.commit()
