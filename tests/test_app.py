from __future__ import annotations

import json
from pathlib import Path

import pytest

import app as central


@pytest.fixture()
def client(tmp_path, monkeypatch):
    mount = tmp_path / "mnt"
    mount.mkdir()
    state = tmp_path / "state"
    monkeypatch.setattr(central, "MOUNT_ROOT", mount.resolve())
    monkeypatch.setattr(central, "STATE_DIR", state.resolve())
    monkeypatch.setattr(central, "SETTINGS_PATH", (state / "settings.json").resolve())
    monkeypatch.setattr(central, "ADMIN_TOKEN", "")
    central.set_embedded_services({})
    central.app.config.update(TESTING=True)
    return central.app.test_client()


def test_index_contains_all_tabs(client):
    response = client.get("/")
    assert response.status_code == 200
    for label in (b"Files", b"HLS", b"YouTube", b"Health & settings"):
        assert label in response.data


def test_capture_route_serves_fragment_receiver(client):
    response = client.get("/capture")
    assert response.status_code == 200
    assert b"function takeCaptureRequest()" in response.data
    assert b"const pendingCapture=takeCaptureRequest();" in response.data


def test_settings_reject_path_outside_mount(client):
    response = client.post(
        "/api/settings",
        json={"paths": {"files": "/tmp/files", "hls": "/tmp/hls", "youtube": "/tmp/youtube"}},
    )
    assert response.status_code == 400
    assert "inside" in response.get_json()["error"] or "stay" in response.get_json()["error"]


def test_admin_token_is_required(client, monkeypatch):
    monkeypatch.setattr(central, "ADMIN_TOKEN", "secret")
    response = client.post("/api/settings", json={"paths": {}})
    assert response.status_code == 401


def test_proxy_forwards_to_selected_backend(client, monkeypatch):
    seen = {}

    class FakeResponse:
        status_code = 202
        content = b'{"ok":true}'
        headers = {"Content-Type": "application/json"}

    def fake_request(method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        return FakeResponse()

    monkeypatch.setattr(central.requests, "request", fake_request)
    response = client.post("/backend/hls/api/download", json={"url": "https://example.test/a.m3u8"})
    assert response.status_code == 202
    assert seen["url"] == "http://127.0.0.1:99/api/download"
    assert json.loads(seen["kwargs"]["data"])["url"].endswith(".m3u8")


def test_directory_browser_stays_below_mount(client, tmp_path):
    mount = Path(central.MOUNT_ROOT)
    (mount / "Videos").mkdir()
    response = client.get("/api/directories", query_string={"path": str(mount)})
    assert response.status_code == 200
    assert response.get_json()["children"][0]["name"] == "Videos"


def test_health_aggregates_all_services(client, monkeypatch):
    def fake_backend(service, path, timeout=None):
        values = {
            "files": {"ok": True, "workers": 2},
            "hls": {"ok": True, "ffmpeg": "/usr/bin/ffmpeg"},
            "youtube": {"ok": False, "yt_dlp_ok": False, "yt_dlp_error": "missing"},
        }
        return values[service], None

    monkeypatch.setattr(central, "backend_json", fake_backend)
    monkeypatch.setattr(central, "command_version", lambda command: {"ok": True, "version": "ffmpeg test", "error": None})
    response = client.get("/api/health")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is False
    assert payload["services"]["files"]["ok"] is True
    assert payload["services"]["youtube"]["ok"] is False


def test_embedded_health_does_not_use_network(client, monkeypatch):
    engine = central.Flask("test-embedded-engine")

    @engine.get("/api/health")
    def embedded_health():
        return {"ok": True, "workers": 2}

    central.set_embedded_services({"files": engine})
    monkeypatch.setattr(central.requests, "get", lambda *args, **kwargs: pytest.fail("network should not be used"))
    payload, error = central.backend_json("files", "api/health")
    assert error is None
    assert payload == {"ok": True, "workers": 2}


def test_resource_summary_contains_pi_metrics(client, monkeypatch):
    monkeypatch.setattr(central, "cpu_usage_percent", lambda sample_seconds=0.1: 37.5)
    monkeypatch.setattr(
        central,
        "network_summary",
        lambda: {
            "rx_bytes": 1000,
            "tx_bytes": 500,
            "received_bytes_per_second": 125.0,
            "sent_bytes_per_second": 50.0,
            "interfaces": [{"name": "eth0", "state": "up"}],
        },
    )
    response = client.get("/api/resources")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["cpu"]["usage_percent"] == 37.5
    assert "temperature_celsius" in payload["cpu"]
    assert "usage_percent" in payload["memory"]
    assert payload["network"]["interfaces"][0]["name"] == "eth0"
    assert len(payload["storage"]["destinations"]) == 3
    assert payload["process"]["pid"] > 0
