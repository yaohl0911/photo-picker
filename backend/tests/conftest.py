"""Shared pytest fixtures for the photo-picker backend tests."""

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
