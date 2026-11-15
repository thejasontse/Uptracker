"""Shared fixtures: an isolated data dir + a TestClient with lifespan running."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SM_DISABLE_SCHEDULER", "1")
    # Reset cached singletons so the new data dir takes effect.
    from app import crypto, db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "data" / "monitor.db")
    monkeypatch.setattr(db, "KEY_PATH", tmp_path / "data" / "secret.key")
    crypto._box = None
    yield tmp_path / "data"
    crypto._box = None


@pytest.fixture()
def client(data_dir):
    from fastapi.testclient import TestClient
    from app import main
    with TestClient(main.app) as c:
        yield c
