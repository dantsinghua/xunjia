"""Explicit post-build acceptance of same-origin production static serving."""
from fastapi.testclient import TestClient
from server.app import create_app
from server.auth import AuthConfig


def test_only_explicit_web_dist_serves_the_real_built_index_and_assets(tmp_path, monkeypatch):
    from pathlib import Path
    import re

    built = Path(__file__).resolve().parents[2] / "web" / "dist"
    assert (built / "index.html").is_file(), "Build the real frontend before this production serving acceptance gate"
    config = AuthConfig(sso_secret="synthetic-sso", session_secret="synthetic-static-session-secret-32")
    monkeypatch.delenv("XUNJIA_WEB_DIST", raising=False)
    with TestClient(create_app(database_url="sqlite:///" + str(tmp_path / "no-web.db"), auth_config=config)) as client:
        assert client.get("/").status_code == 404
    monkeypatch.setenv("XUNJIA_WEB_DIST", str(built))
    with TestClient(create_app(database_url="sqlite:///" + str(tmp_path / "web.db"), auth_config=config)) as client:
        for path in ("/", "/quote", "/monitor", "/chat", "/link"):
            response = client.get(path)
            assert response.status_code == 200 and response.content == (built / "index.html").read_bytes()
            assert "script-src 'self'" in response.headers["content-security-policy"]
        assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', response.text)
        assert assets
        for asset in assets:
            result = client.get(asset)
            assert result.status_code == 200
            assert result.content == (built / asset.lstrip("/")).read_bytes()
            assert result.headers["x-content-type-options"] == "nosniff"
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/assets/%2e%2e/%2e%2e/server/app.py").status_code == 404

