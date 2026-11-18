"""Shared pytest fixtures for the photo-picker backend tests."""

import os
import sys
from pathlib import Path

# Make `import app...` work whether pytest runs from the repo root or backend/.
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.db import connect, init_db


@pytest.fixture()
def db(tmp_path):
    """A fresh, fully migrated SQLite connection, one per test."""
    conn = connect(str(tmp_path / "test.db"))
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def make_fake_file(directory, name, content=b"not-a-real-image", mtime=None):
    """Create a fake photo file with undecodable bytes (pure stdlib, DSH-safe).

    Used by the logic-layer scanner tests: the bytes fail every decoder, so
    files exercise the failure branch — indexed anyway, empty EXIF, error
    recorded — without needing Pillow to actually run.
    """
    path = directory / name
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path
