"""Provider CRUD is offline, authenticated, recoverable and credential-safe."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from crawler.config import Settings
from crawler.provider_management import provider_management_router
from crawler.store import Event, Profile, Store
from crawler.transport import ExtractionError


BASE = {"name": "Test director", "kind": "openai_compatible",
        "base_url": "https://provider.example/v1", "model": "fixture-model"}


@pytest.fixture
def workspace(tmp_path):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    validated_urls = []

    async def authenticated(authorization: str = Header(default="")):
        if authorization != "Bearer fixture-token":
            raise HTTPException(401, "API token required")

    async def validate_url(url):
        validated_urls.append(url)
        if url == "https://blocked.example":
            raise ExtractionError("Unsafe provider address")

    app = FastAPI()
    app.include_router(provider_management_router(lambda: store, authenticated, validate_url))
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer fixture-token"
        yield client, store, validated_urls
    store.engine.dispose()


def create(client, **fields):
    result = client.post("/api/v1/providers", json={**BASE, **fields})
    assert result.status_code == 200, result.text
    return result.json()


def stored(store, provider_id):
    with store.session() as session:
        profile = session.get(Profile, provider_id)
        return dict(profile.data), profile.secret


@pytest.mark.parametrize("method,path,body", [
    ("GET", "", None), ("GET", "?include_deleted=true", None),
    ("POST", "", BASE), ("PUT", "/fixture", BASE),
    ("POST", "/fixture/suspend", None), ("POST", "/fixture/resume", None),
    ("DELETE", "/fixture", None), ("POST", "/fixture/restore", None),
])
def test_management_routes_require_auth(workspace, method, path, body):
    client, store, urls = workspace
    client.headers.pop("Authorization")
    result = client.request(method, "/api/v1/providers" + path, json=body)
    assert result.status_code == 401
    assert store.profiles("provider") == [] and urls == []


def test_edit_preserves_blank_or_absent_key_and_never_returns_secret(workspace):
    client, store, urls = workspace
    provider = create(client, api_key="synthetic-original-key")
    provider_id = provider["id"]
    assert provider["status"] == "active" and provider["revision"] == 1
    assert provider["has_credentials"] is True
    before_cipher = stored(store, provider_id)[1]
    for index, key_field in enumerate(({}, {"api_key": ""}, {"api_key": "   "}, {"api_key": None})):
        result = client.put(f"/api/v1/providers/{provider_id}",
                            json={**BASE, "model": f"updated-{index}", **key_field})
        assert result.status_code == 200, result.text
        assert result.json()["id"] == provider_id
        assert stored(store, provider_id)[1] == before_cipher
        assert "synthetic-original-key" not in result.text
    replaced = client.put(f"/api/v1/providers/{provider_id}",
                          json={**BASE, "api_key": "synthetic-replaced-key"})
    assert replaced.status_code == 200
    data, encrypted = stored(store, provider_id)
    assert encrypted != before_cipher and "synthetic-replaced-key" not in encrypted
    assert "synthetic-replaced-key" not in json.dumps(data)
    profiles = store.profiles("provider", secrets=True)
    assert profiles[0]["credentials"] == {"api_key": "synthetic-replaced-key"}
    listed = client.get("/api/v1/providers")
    assert "synthetic-" not in listed.text
    assert "credentials" not in listed.json()["items"][0]
    assert urls == [BASE["base_url"]] * 6


def test_suspend_delete_restore_are_explicit_and_recoverable(workspace):
    client, store, urls = workspace
    provider = create(client, api_key="synthetic-key")
    provider_id = provider["id"]
    before_cipher = stored(store, provider_id)[1]
    with store.session() as session:
        event = Event(run_id="history", type="director_fixture", payload={"provider_id": provider_id})
        session.add(event)
        session.flush()
        event_id = event.id
    suspend = client.post(f"/api/v1/providers/{provider_id}/suspend")
    assert suspend.status_code == 200 and suspend.json()["status"] == "suspended"
    assert suspend.json()["revision"] == 2
    assert len(client.get("/api/v1/providers").json()["items"]) == 1
    edit = client.post("/api/v1/providers", json={**BASE, "id": provider_id, "model": "new"})
    assert edit.status_code == 200 and edit.json()["status"] == "suspended"
    deleted = client.delete(f"/api/v1/providers/{provider_id}")
    assert deleted.status_code == 200 and deleted.json()["status"] == "deleted"
    assert client.get("/api/v1/providers").json()["items"] == []
    assert client.get("/api/v1/providers?include_deleted=true").json()["items"][0]["id"] == provider_id
    assert len(store.profiles("provider")) == 1
    for method, path, data in (("PUT", f"/{provider_id}", BASE),
                               ("POST", "", {**BASE, "id": provider_id}),
                               ("POST", f"/{provider_id}/resume", None)):
        assert client.request(method, "/api/v1/providers" + path, json=data).status_code == 409
    restored = client.post(f"/api/v1/providers/{provider_id}/restore")
    assert restored.status_code == 200 and restored.json()["status"] == "suspended"
    resumed = client.post(f"/api/v1/providers/{provider_id}/resume")
    assert resumed.status_code == 200 and resumed.json()["status"] == "active"
    assert stored(store, provider_id)[1] == before_cipher
    with store.session() as session:
        assert session.get(Event, event_id).payload == {"provider_id": provider_id}
    # Lifecycle operations never call a provider or even URL validation.
    assert urls == [BASE["base_url"]] * 4


def test_legacy_records_are_active_without_read_side_migration(workspace):
    client, store, _ = workspace
    provider_id = store.save_profile("provider", dict(BASE), {"api_key": "synthetic-key"})
    before = stored(store, provider_id)
    item = client.get("/api/v1/providers").json()["items"][0]
    assert item["status"] == "active" and item["revision"] == 0
    assert stored(store, provider_id) == before
    assert client.post(f"/api/v1/providers/{provider_id}/suspend").json()["status"] == "suspended"
    result = client.post("/api/v1/providers", json={**BASE, "id": provider_id})
    assert result.status_code == 200 and result.json()["status"] == "suspended"


@pytest.mark.parametrize("extra", [
    {"status": "suspended"}, {"status": "active"}, {"revision": 999},
    {"credentials": {"api_key": "synthetic-secret"}}, {"secret": "synthetic-secret"},
    {"api_key": 12}, {"api_key": "synthetic-secret\n"},
    {"base_url": "https://user:synthetic-secret@provider.example"},
    {"unexpected": "synthetic-secret"}, {"kind": "unsupported"},
])
def test_client_cannot_inject_state_secrets_or_invalid_metadata(workspace, extra):
    client, store, urls = workspace
    result = client.post("/api/v1/providers", json={**BASE, **extra})
    assert result.status_code == 422
    assert "synthetic-secret" not in result.text
    assert store.profiles("provider") == [] and urls == []


def test_missing_id_id_mismatch_and_cross_kind_collisions_are_safe(workspace):
    client, store, _ = workspace
    source_id = store.save_profile("source", {"host": "supplier.example"}, {"token": "source-key"})
    original = stored(store, source_id)
    for method, path, body in (("POST", "", {**BASE, "id": source_id}),
                               ("PUT", f"/{source_id}", BASE),
                               ("POST", f"/{source_id}/suspend", None),
                               ("DELETE", f"/{source_id}", None)):
        assert client.request(method, "/api/v1/providers" + path, json=body).status_code == 409
        assert stored(store, source_id) == original
    assert client.put("/api/v1/providers/missing", json=BASE).status_code == 404
    assert client.post("/api/v1/providers/missing/suspend").status_code == 404
    provider = create(client)
    assert client.put(f"/api/v1/providers/{provider['id']}", json={**BASE, "id": "other"}).status_code == 409
    for action in ("suspend", "resume", "restore"):
        result = client.post(f"/api/v1/providers/{provider['id']}/{action}", json={"status": "active"})
        assert result.status_code == 422
    assert client.post(f"/api/v1/providers/{provider['id']}/restore").status_code == 409
    assert client.post("/api/v1/providers", json={**BASE, "base_url": "https://blocked.example"}).status_code == 422


def test_atomic_metadata_edit_cannot_overwrite_concurrent_key_update(workspace):
    client, store, _ = workspace
    provider_id = create(client, api_key="synthetic-old")["id"]
    key_written, release_key = ThreadEvent(), ThreadEvent()

    def update_key_in_transaction():
        with store.session(immediate=True) as session:
            profile = session.get(Profile, provider_id)
            profile.secret = store.cipher.encrypt(b'{"api_key":"synthetic-new"}').decode()
            key_written.set()
            assert release_key.wait(5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        credential_write = executor.submit(update_key_in_transaction)
        assert key_written.wait(5)
        metadata_write = executor.submit(store.save_provider_profile,
                                         {**BASE, "model": "changed-metadata"},
                                         profile_id=provider_id, require_existing=True)
        release_key.set()
        credential_write.result(timeout=5)
        assert metadata_write.result(timeout=5) == provider_id
    saved = store.profiles("provider", secrets=True)[0]
    assert saved["model"] == "changed-metadata"
    assert saved["credentials"]["api_key"] == "synthetic-new"
