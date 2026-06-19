import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.dependencies import get_settings
from config.settings import Settings

app = create_app()


def test_default_freecc_token_is_localhost_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    settings = Settings()
    settings.anthropic_auth_token = "freecc"
    app.dependency_overrides[get_settings] = lambda: settings

    local_client = TestClient(app, client=("127.0.0.1", 50000))
    remote_client = TestClient(app, client=("203.0.113.10", 50000))

    local = local_client.get("/v1/models", headers={"X-API-Key": "freecc"})
    assert local.status_code == 200

    remote = remote_client.get("/v1/models", headers={"X-API-Key": "freecc"})
    assert remote.status_code == 401

    app.dependency_overrides.clear()


def test_custom_anthropic_auth_token_accepts_remote_client(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    settings = Settings()
    settings.anthropic_auth_token = "my-secret"
    app.dependency_overrides[get_settings] = lambda: settings

    remote_client = TestClient(app, client=("203.0.113.10", 50000))
    response = remote_client.get("/v1/models", headers={"X-API-Key": "my-secret"})
    assert response.status_code == 200

    app.dependency_overrides.clear()


def test_anthropic_auth_token_required_and_accepts_x_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "s3cr3t"
    app.dependency_overrides[get_settings] = lambda: settings

    payload = {
        "model": "claude-3-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch("api.routes.get_token_count", return_value=1):
        # No header -> 401
        r = client.post("/v1/messages/count_tokens", json=payload)
        assert r.status_code == 401

        # X-API-Key header -> 200
        r = client.post(
            "/v1/messages/count_tokens", json=payload, headers={"X-API-Key": "s3cr3t"}
        )
        assert r.status_code == 200
        assert r.json()["input_tokens"] == 1

    app.dependency_overrides.clear()


def test_anthropic_auth_token_accepts_bearer_authorization(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "b3artoken"
    app.dependency_overrides[get_settings] = lambda: settings

    payload = {
        "model": "claude-3-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch("api.routes.get_token_count", return_value=2):
        # Authorization Bearer -> 200
        r = client.post(
            "/v1/messages/count_tokens",
            json=payload,
            headers={"Authorization": "Bearer b3artoken"},
        )
        assert r.status_code == 200
        assert r.json()["input_tokens"] == 2

    app.dependency_overrides.clear()


def test_anthropic_auth_token_normalizes_configured_whitespace(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "  spaced-token  \n"
    app.dependency_overrides[get_settings] = lambda: settings

    payload = {
        "model": "claude-3-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch("api.routes.get_token_count", return_value=3):
        r = client.post(
            "/v1/messages/count_tokens",
            json=payload,
            headers={"Authorization": "Bearer spaced-token"},
        )
        assert r.status_code == 200
        assert r.json()["input_tokens"] == 3

    app.dependency_overrides.clear()


def test_anthropic_auth_token_applies_to_models_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "models-token"
    app.dependency_overrides[get_settings] = lambda: settings

    r = client.get("/v1/models")
    assert r.status_code == 401

    r = client.get("/v1/models", headers={"X-API-Key": "models-token"})
    assert r.status_code == 200
    assert "data" in r.json()

    app.dependency_overrides.clear()


def test_root_get_requires_auth_but_root_probes_are_public(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "root-token"
    app.dependency_overrides[get_settings] = lambda: settings

    response = client.get("/")
    assert response.status_code == 401

    head = client.head("/")
    assert head.status_code == 204
    assert head.headers["Allow"] == "GET, HEAD, OPTIONS"

    options = client.options("/")
    assert options.status_code == 204
    assert options.headers["Allow"] == "GET, HEAD, OPTIONS"

    app.dependency_overrides.clear()


def test_model_visibility_blocks_unlisted_request_models(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    access_dir = tmp_path / ".fcc"
    access_dir.mkdir(parents=True)
    (access_dir / "access.json").write_text(
        json.dumps(
            {
                "password_hash": "pbkdf2_sha256$600000$abc$def",
                "api_keys": [],
                "sessions": {},
                "visible_model_ids": ["claude-sonnet-4-20250514"],
            }
        ),
        encoding="utf-8",
    )

    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "scoped-token"
    app.dependency_overrides[get_settings] = lambda: settings
    allowed_payload = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "hello"}],
    }
    blocked_payload = {
        "model": "claude-opus-4-20250514",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch("api.routes.get_token_count", return_value=5):
        allowed = client.post(
            "/v1/messages/count_tokens",
            json=allowed_payload,
            headers={"X-API-Key": "scoped-token"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["input_tokens"] == 5

        blocked = client.post(
            "/v1/messages/count_tokens",
            json=blocked_payload,
            headers={"X-API-Key": "scoped-token"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "Model is not enabled for client access"

    app.dependency_overrides.clear()


def test_model_visibility_allows_codex_catalog_slug_for_visible_gateway_model(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    access_dir = tmp_path / ".fcc"
    access_dir.mkdir(parents=True)
    (access_dir / "access.json").write_text(
        json.dumps(
            {
                "password_hash": "pbkdf2_sha256$600000$abc$def",
                "api_keys": [],
                "sessions": {},
                "visible_model_ids": ["anthropic/nvidia_nim/provider-model"],
            }
        ),
        encoding="utf-8",
    )

    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "scoped-token"
    app.dependency_overrides[get_settings] = lambda: settings
    payload = {
        "model": "nvidia_nim/provider-model",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch("api.routes.get_token_count", return_value=5):
        allowed = client.post(
            "/v1/messages/count_tokens",
            json=payload,
            headers={"X-API-Key": "scoped-token"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["input_tokens"] == 5

    app.dependency_overrides.clear()


def test_model_visibility_allows_codex_catalog_slug_for_responses_request(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    access_dir = tmp_path / ".fcc"
    access_dir.mkdir(parents=True)
    (access_dir / "access.json").write_text(
        json.dumps(
            {
                "password_hash": "pbkdf2_sha256$600000$abc$def",
                "api_keys": [],
                "sessions": {},
                "visible_model_ids": ["anthropic/nvidia_nim/provider-model"],
            }
        ),
        encoding="utf-8",
    )

    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "scoped-token"
    app.dependency_overrides[get_settings] = lambda: settings

    with patch(
        "api.routes.ApiRequestPipeline.create_response",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ):
        allowed = client.post(
            "/v1/responses",
            json={"model": "nvidia_nim/provider-model", "input": "hello"},
            headers={"X-API-Key": "scoped-token"},
        )
        assert allowed.status_code == 200
        assert allowed.json() == {"ok": True}

    app.dependency_overrides.clear()


def test_explicit_empty_model_visibility_blocks_all_request_models(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    access_dir = tmp_path / ".fcc"
    access_dir.mkdir(parents=True)
    (access_dir / "access.json").write_text(
        json.dumps(
            {
                "password_hash": "pbkdf2_sha256$600000$abc$def",
                "api_keys": [],
                "sessions": {},
                "visible_model_ids": [],
            }
        ),
        encoding="utf-8",
    )

    client = TestClient(app)
    settings = Settings()
    settings.anthropic_auth_token = "scoped-token"
    app.dependency_overrides[get_settings] = lambda: settings
    payload = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "hello"}],
    }

    with patch("api.routes.get_token_count", return_value=5):
        blocked = client.post(
            "/v1/messages/count_tokens",
            json=payload,
            headers={"X-API-Key": "scoped-token"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "Model is not enabled for client access"

    app.dependency_overrides.clear()
