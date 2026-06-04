from src.webui.web_server import WebUI


def _base_config(settings_password: str = ""):
    return {
        "station": {"callsign": "TEST"},
        "logging": {},
        "webui": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 5000,
            "debug": False,
            "settings_password": settings_password,
            "map": {
                "default_lat": 52.0,
                "default_lon": 10.0,
                "default_zoom": 8,
                "tile_server": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            },
        },
    }


def test_runtime_config_requires_settings_password_when_configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    webui = WebUI(_base_config(settings_password="secret123"))
    client = webui.app.test_client()

    response = client.post("/api/runtime_config", json={"external_url_provider": "sondehub"})

    assert response.status_code == 401
    data = response.get_json()
    assert data["success"] is False


def test_runtime_config_accepts_valid_settings_password(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    webui = WebUI(_base_config(settings_password="secret123"))
    client = webui.app.test_client()

    response = client.post(
        "/api/runtime_config",
        json={"external_url_provider": "sondehub"},
        headers={"X-Settings-Password": "secret123"},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert "external_url_provider" in data["changed"]


def test_runtime_config_without_settings_password_stays_open(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    webui = WebUI(_base_config(settings_password=""))
    client = webui.app.test_client()

    response = client.post("/api/runtime_config", json={"external_url_provider": "sondehub"})

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
