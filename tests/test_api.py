"""API tests. Run with:  pip install pytest httpx && pytest -q"""
import base64
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "test-key"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"anyone:family").decode()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Fresh app + empty SQLite database for every test."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "family")
    monkeypatch.setenv("EMPTY_DISTANCE_CM", "200")
    monkeypatch.setenv("FULL_DISTANCE_CM", "25")
    sys.modules.pop("app", None)
    app_module = importlib.import_module("app")
    with TestClient(app_module.app) as c:
        yield c, app_module


def post(c, dist=112.5, key=KEY, **extra):
    return c.post("/api/readings", json={"distance_cm": dist, **extra}, headers={"X-API-Key": key})


def test_healthz_needs_no_login(client):
    c, _ = client
    assert c.get("/healthz").json() == {"ok": True}


def test_reading_with_wrong_key_is_rejected(client):
    c, _ = client
    assert post(c, key="wrong").status_code == 401


def test_reading_without_key_is_rejected(client):
    c, _ = client
    assert c.post("/api/readings", json={"distance_cm": 100}).status_code == 401


def test_valid_reading_is_stored_and_returns_level(client):
    c, _ = client
    r = post(c, 112.5)  # halfway between 25 (full) and 200 (empty)
    assert r.status_code == 201
    assert r.json()["level_pct"] == 50.0
    latest = c.get("/api/latest", headers=AUTH).json()
    assert latest["distance_cm"] == 112.5 and latest["level_pct"] == 50.0


def test_low_flag_when_tank_nearly_empty(client):
    c, _ = client
    assert post(c, 190).json()["low"] is True
    assert post(c, 40).json()["low"] is False


@pytest.mark.parametrize("bad", [0, -5, 1000, 5000])
def test_out_of_range_distance_is_rejected(client, bad):
    c, _ = client
    assert post(c, bad).status_code == 422


def test_level_is_clamped_between_0_and_100(client):
    _, m = client
    assert m.level_pct(10) == 100.0
    assert m.level_pct(500) == 0.0


def test_buffered_reading_gets_older_timestamp(client):
    c, _ = client
    post(c, 100, age_s=3 * 3600)  # measured 3 h ago, delivered late
    post(c, 100)
    rows = c.get("/api/readings?hours=24", headers=AUTH).json()
    assert len(rows) == 2
    assert rows[1]["ts"] - rows[0]["ts"] >= 3 * 3600 - 5


def test_dashboard_and_api_require_password(client):
    c, _ = client
    for path in ("/", "/api/latest", "/api/readings", "/api/usage"):
        assert c.get(path).status_code == 401
    assert c.get("/", headers=AUTH).status_code == 200


def test_wrong_dashboard_password_is_rejected(client):
    c, _ = client
    bad = {"Authorization": "Basic " + base64.b64encode(b"x:nope").decode()}
    assert c.get("/api/latest", headers=bad).status_code == 401


def test_recalibrating_changes_old_readings(tmp_path, monkeypatch):
    """Raw distance is stored, so a new calibration fixes history."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "family")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    levels = []
    for empty in ("200", "300"):
        monkeypatch.setenv("EMPTY_DISTANCE_CM", empty)
        sys.modules.pop("app", None)
        m = importlib.import_module("app")
        with TestClient(m.app) as c:
            if empty == "200":
                post(c, 112.5)
            levels.append(c.get("/api/latest", headers=AUTH).json()["level_pct"])
    assert levels[0] != levels[1]


def test_server_refuses_default_api_key_in_production(monkeypatch, tmp_path):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    sys.modules.pop("app", None)
    with pytest.raises(RuntimeError):
        importlib.import_module("app")
