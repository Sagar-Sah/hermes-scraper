import asyncio
import base64
import copy
import json
import os

import httpx
import pytest

from crawler.companion_environments import CompanionEnvironmentRegistry


TOKEN = "private" + "x" * 50


def test_private_dual_connections_identity_and_roles(tmp_path):
    calls = []
    async def transport(cfg, method, path, payload):
        calls.append((cfg, method, path))
        return {"environment": cfg["environment"], "role": cfg["role"], "site_origin": cfg["base_url"],
                "connector_version": "0.2.0", "inventory": True, "read_families": ["custom_csv"],
                "write_families": ["custom_csv"], "accidental_secret": TOKEN}
    registry = CompanionEnvironmentRegistry(tmp_path, transport=transport)
    async def exercise():
        await registry.configure("production", "https://printitprints.com", TOKEN)
        await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", TOKEN,
                                 {"username": "preview", "password": "protected-password"})
        await registry.configure("production", "https://printitprints.com", TOKEN, role="release")
        with pytest.raises(ValueError, match="manual_production"):
            await registry.request("production", "POST", "/previews", {})
        with pytest.raises(ValueError, match="unsupported_companion"):
            await registry.request("staging", "DELETE", "/products/9532")
        authorization = {"actor": "human", "manifest_sha256": "a" * 64, "approval_id": "b" * 32}
        await registry.request("production", "POST", "/previews", {}, release_authorization=authorization)
        assert calls[-1][0]["role"] == "release"
        assert calls[-1][0]["release_authorization"] == authorization
        await registry.snapshot("production", "9532")
        assert calls[-1][0]["role"] == "read"
        await registry.inventory("production", 2)
        assert calls[-1][2] == "/inventory?page=2"
        with pytest.raises(ValueError, match="origin_not_approved"):
            await registry.configure("production", "https://staging3.tonyl36.sg-host.com", TOKEN)
        with pytest.raises(ValueError, match="invalid_companion_connector_path"):
            await registry.request("staging", "GET", "/../../secrets")
    asyncio.run(exercise())
    public = json.dumps(registry.status())
    assert TOKEN not in public and "protected-password" not in public
    for path in registry.root.glob("*.json"):
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(registry.root).st_mode & 0o777 == 0o700


def test_failed_handshake_stays_unconnected_and_does_not_replace_working_connection(tmp_path):
    fail = False
    async def transport(cfg, *_):
        if fail:
            raise ValueError("companion_connector_http_401")
        return {"site_origin": cfg["base_url"], "environment": cfg["environment"], "role": cfg["role"]}
    registry = CompanionEnvironmentRegistry(tmp_path, transport=transport)
    async def exercise():
        nonlocal fail
        fail = True
        result = await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", TOKEN)
        assert result["connected"] is False and result["reason"].endswith("401")
        with pytest.raises(ValueError, match="connection_unverified"):
            await registry.snapshot("staging", "9532")
        fail = False
        await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", TOKEN)
        before = registry._path("staging").read_bytes()
        fail = True
        with pytest.raises(ValueError):
            await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", "different" + "z" * 50)
        assert registry._path("staging").read_bytes() == before
    asyncio.run(exercise())


def test_protected_preview_uses_basic_and_separate_connector_header(monkeypatch):
    seen = []
    async def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    cfg = {"base_url": "https://staging3.tonyl36.sg-host.com", "token": TOKEN,
           "protection_auth": {"username": "preview", "password": "password"}}
    assert asyncio.run(CompanionEnvironmentRegistry._http(cfg, "GET", "/capabilities", None)) == {"ok": True}
    assert seen[0].headers["authorization"] == "Basic " + base64.b64encode(b"preview:password").decode()
    assert seen[0].headers["x-hermes-companion-token"] == TOKEN
    assert seen[0].url.host == "staging3.tonyl36.sg-host.com"


def test_saved_connector_token_can_complete_preview_auth_without_resupplying_it(tmp_path):
    calls = []
    async def transport(cfg, *_):
        calls.append(copy.deepcopy(cfg))
        if not cfg.get("protection_auth"):
            raise ValueError("companion_connector_http_401")
        return {"site_origin": cfg["base_url"], "environment": cfg["environment"], "role": cfg["role"]}
    registry = CompanionEnvironmentRegistry(tmp_path, transport=transport)
    async def exercise():
        initial = await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", TOKEN)
        assert initial["configured"] is True and initial["connected"] is False
        protected = await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", None,
                                            {"username": "preview", "password": "protected-password"})
        assert protected["connected"] is True and protected["protected_preview_credentials_present"] is True
        assert calls[-1]["token"] == TOKEN
        assert calls[-1]["protection_auth"] == {"username": "preview", "password": "protected-password"}
        public = json.dumps(protected)
        assert TOKEN not in public and "protected-password" not in public
    asyncio.run(exercise())


def test_blank_recheck_and_token_rotation_preserve_saved_preview_credentials(tmp_path):
    calls = []
    async def transport(cfg, *_):
        calls.append(copy.deepcopy(cfg))
        return {"site_origin": cfg["base_url"], "environment": cfg["environment"], "role": cfg["role"]}
    registry = CompanionEnvironmentRegistry(tmp_path, transport=transport)
    auth = {"username": "preview", "password": "protected-password"}
    rotated = "rotated" + "y" * 50
    async def exercise():
        await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", TOKEN, auth)
        await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", "")
        assert calls[-1]["token"] == TOKEN and calls[-1]["protection_auth"] == auth
        await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", rotated)
        assert calls[-1]["token"] == rotated and calls[-1]["protection_auth"] == auth
        await registry.configure("staging", "https://staging3.tonyl36.sg-host.com")
        assert calls[-1]["token"] == rotated and calls[-1]["protection_auth"] == auth
        assert registry.status("staging")["connected"] is True
    asyncio.run(exercise())


def test_blank_token_cannot_create_connection_change_origin_or_elevate_role(tmp_path):
    calls = []
    async def transport(cfg, *_):
        calls.append(copy.deepcopy(cfg))
        return {"site_origin": cfg["base_url"], "environment": cfg["environment"], "role": cfg["role"]}
    registry = CompanionEnvironmentRegistry(tmp_path, transport=transport)
    async def exercise():
        for token in (None, ""):
            with pytest.raises(ValueError, match="saved_companion_connection_required"):
                await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", token)
        assert calls == []
        await registry.configure("production", "https://printitprints.com", TOKEN)
        for origin, role in (("https://www.printitprints.com", "read"), ("https://printitprints.com", "release")):
            with pytest.raises(ValueError, match="saved_companion_connection_required"):
                await registry.configure("production", origin, None, role=role)
        assert len(calls) == 1
        assert registry.status("production", role="release")["configured"] is False
        assert registry._config("production")["base_url"] == "https://printitprints.com"
    asyncio.run(exercise())


def test_failed_all_blank_recheck_downgrades_expired_session_without_erasing_secrets(tmp_path):
    failed = False
    async def transport(cfg, *_):
        if failed:
            raise ValueError("companion_connector_http_401")
        return {"site_origin": cfg["base_url"], "environment": cfg["environment"], "role": cfg["role"]}
    registry = CompanionEnvironmentRegistry(tmp_path, transport=transport)
    async def exercise():
        nonlocal failed
        await registry.configure("staging", "https://staging3.tonyl36.sg-host.com", TOKEN,
                                 {"username": "preview", "password": "protected-password"})
        failed = True
        result = await registry.configure("staging", "https://staging3.tonyl36.sg-host.com")
        assert result["connected"] is False and result["reason"] == "companion_connector_http_401"
        assert registry._config("staging")["token"] == TOKEN
        assert registry._config("staging")["protection_auth"]["password"] == "protected-password"
        with pytest.raises(ValueError, match="connection_unverified"):
            await registry.snapshot("staging", "9532")
    asyncio.run(exercise())
