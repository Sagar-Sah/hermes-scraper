import json
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import select

import crawler.app as api
from crawler import providers
from crawler.config import Settings
from crawler.store import Profile, Run, Store


@pytest.fixture
async def service(tmp_path, monkeypatch):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(api.settings, "api_token", "fixture-api-token")

    async def public_url(url):
        return urlparse(url)  # no network is used by these fixtures

    monkeypatch.setattr(api, "validate_url", public_url)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://testserver"
    ) as client:
        yield client, store
    store.engine.dispose()


def auth():
    return {"Authorization": "Bearer fixture-api-token"}


def create_run(store):
    with store.session() as session:
        run = Run(name="Export fixture", spec={"adapter": "fixture"})
        session.add(run)
        session.flush()
        return run.id


@pytest.mark.asyncio
async def test_health_is_public_but_operational_endpoints_require_auth(service):
    client, _ = service
    assert (await client.get("/api/v1/health")).json()[
        "authentication_required"
    ] is True
    endpoints = [
        ("GET", "/runs"),
        ("POST", "/runs"),
        ("GET", "/providers"),
        ("POST", "/providers"),
        ("GET", "/sources"),
        ("POST", "/sources"),
        ("GET", "/evidence/example"),
        ("GET", "/runs/example/download/file.csv"),
        ("POST", "/commerce/shipping"),
    ]
    for method, endpoint in endpoints:
        response = await client.request(
            method, "/api/v1" + endpoint, json={} if method == "POST" else None
        )
        assert response.status_code == 401, (method, endpoint, response.text)
    assert (
        await client.get("/api/v1/runs", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    assert (await client.get("/api/v1/runs", headers=auth())).status_code == 200


@pytest.mark.asyncio
async def test_provider_credentials_are_encrypted_and_never_listed(service):
    client, store = service
    payload = {
        "name": "Fixture",
        "base_url": "https://provider.example/v1",
        "model": "m",
        "api_key": "synthetic-provider-secret",
    }
    response = await client.post("/api/v1/providers", json=payload, headers=auth())
    assert response.status_code == 200
    provider_id = response.json()["id"]
    listed = await client.get("/api/v1/providers", headers=auth())
    assert listed.json()["items"][0]["has_credentials"] is True
    assert "synthetic-provider-secret" not in listed.text
    with store.session() as session:
        row = session.get(Profile, provider_id)
        assert "synthetic-provider-secret" not in json.dumps(row.data)
        assert "synthetic-provider-secret" not in row.secret
    assert (
        store.profiles("provider", True)[0]["credentials"]["api_key"]
        == "synthetic-provider-secret"
    )
    response = await client.post(
        "/api/v1/providers",
        json={
            "id": provider_id,
            "base_url": "https://provider.example/v1",
            "model": "updated",
        },
        headers=auth(),
    )
    assert response.status_code == 200
    assert (
        store.profiles("provider", True)[0]["credentials"]["api_key"]
        == "synthetic-provider-secret"
    )


@pytest.mark.asyncio
async def test_provider_misplaced_credentials_and_unknown_fields_rejected(service):
    client, store = service
    for extra in (
        {"credentials": {"api_key": "synthetic-secret"}},
        {"token": "synthetic-secret"},
        {"kind": "unsupported"},
        {"unexpected": "value"},
    ):
        response = await client.post(
            "/api/v1/providers",
            json={"base_url": "https://provider.example", "model": "m", **extra},
            headers=auth(),
        )
        assert response.status_code == 422
        assert "synthetic-secret" not in response.text
    assert store.profiles("provider") == []


@pytest.mark.asyncio
async def test_custom_null_body_configuration_round_trips(service):
    client, _ = service
    payload = {
        "base_url": "https://provider.example",
        "model": "m",
        "kind": "custom",
        "body_template": None,
        "prompt_field": None,
        "model_field": None,
        "response_path": None,
        "auth_header": None,
    }
    assert (
        await client.post("/api/v1/providers", json=payload, headers=auth())
    ).status_code == 200
    profile = (await client.get("/api/v1/providers", headers=auth())).json()["items"][0]
    assert "body_template" in profile and profile["body_template"] is None
    assert profile["prompt_field"] is None


@pytest.mark.asyncio
async def test_inactive_provider_connection_tests_never_dispatch(service, monkeypatch):
    client, store = service
    provider_id = store.save_provider_profile({
        "name": "Fixture", "base_url": "https://provider.example/v1", "model": "m",
    }, {"api_key": "synthetic-secret"})
    calls = []

    async def complete(*args):
        calls.append(args)
        return "connected"

    monkeypatch.setattr(providers, "complete", complete)
    endpoint = f"/api/v1/providers/{provider_id}/test"
    store.provider_lifecycle(provider_id, "suspend")
    result = await client.post(endpoint, headers=auth())
    assert result.status_code == 409 and result.json()["detail"] == "Provider is suspended"
    store.provider_lifecycle(provider_id, "delete")
    result = await client.post(endpoint, headers=auth())
    assert result.status_code == 409 and result.json()["detail"] == "Provider is deleted"
    assert calls == []
    assert (await client.post("/api/v1/providers/missing/test", headers=auth())).status_code == 404
    store.provider_lifecycle(provider_id, "restore")
    store.provider_lifecycle(provider_id, "resume")
    assert (await client.post(endpoint, headers=auth())).status_code == 200
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_source_credentials_are_encrypted_and_never_listed(service):
    client, store = service
    payload = {
        "host": "source.example",
        "name": "Fixture source",
        "credentials": {"headers": {"Authorization": "Bearer synthetic-source-secret"}},
    }
    response = await client.post("/api/v1/sources", json=payload, headers=auth())
    assert response.status_code == 200
    listed = await client.get("/api/v1/sources", headers=auth())
    assert "synthetic-source-secret" not in listed.text
    with store.session() as session:
        row = session.get(Profile, response.json()["id"])
        assert "synthetic-source-secret" not in json.dumps(row.data)
        assert "synthetic-source-secret" not in row.secret


@pytest.mark.asyncio
async def test_download_rejects_encoded_traversal_symlinks_and_other_runs(service):
    client, store = service
    run_id, other_id = create_run(store), create_run(store)
    run_root, other_root = (
        store.root / "exports" / run_id,
        store.root / "exports" / other_id,
    )
    run_root.mkdir(parents=True)
    other_root.mkdir(parents=True)
    (run_root / "valid.csv").write_text("quantity,subtotal\n500,10.00\n")
    (other_root / "other.csv").write_text("other snapshot")
    secret = store.root / "private.txt"
    secret.write_text("synthetic-private-file")
    (run_root / "outside.csv").symlink_to(secret)
    response = await client.get(
        f"/api/v1/runs/{run_id}/download/valid.csv", headers=auth()
    )
    assert response.status_code == 200 and "500,10.00" in response.text
    attempts = [
        f"/api/v1/runs/{run_id}/download/%2e%2e/%2e%2e/private.txt",
        f"/api/v1/runs/{run_id}/download/%2e%2e/{other_id}/other.csv",
        f"/api/v1/runs/{run_id}/download/outside.csv",
        "/api/v1/runs/%2e%2e/download/private.txt",
        "/api/v1/runs/%2e%2e/exports",
        "/api/v1/runs/missing/download/valid.csv",
    ]
    for path in attempts:
        response = await client.get(path, headers=auth())
        assert response.status_code == 404, (path, response.text)
        assert "synthetic-private-file" not in response.text


@pytest.mark.asyncio
async def test_export_directory_symlink_cannot_redefine_run_boundary(service):
    client, store = service
    run_id = create_run(store)
    root = store.root / "exports"
    root.mkdir()
    outside = store.root / "other-files"
    outside.mkdir()
    (outside / "private.csv").write_text("synthetic-private-file")
    (root / run_id).symlink_to(outside, target_is_directory=True)
    assert (
        await client.get(f"/api/v1/runs/{run_id}/download/private.csv", headers=auth())
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/runs/{run_id}/exports", headers=auth())
    ).status_code == 404


@pytest.mark.asyncio
async def test_instruction_schema_failure_creates_no_run(service, monkeypatch):
    client, store = service

    async def invalid(*args):
        raise ValueError("Instruction schema requires exactly the documented fields")

    monkeypatch.setattr(providers, "interpret", invalid)
    response = await client.post(
        "/api/v1/runs",
        json={
            "adapter": "fixture",
            "instructions": "use specified stock",
            "provider_id": "fixture",
        },
        headers=auth(),
    )
    assert response.status_code == 422
    with store.session() as session:
        assert list(session.scalars(select(Run))) == []


@pytest.mark.asyncio
async def test_provider_failure_is_safe_and_creates_no_run(service, monkeypatch):
    client, store = service

    async def failed(*args):
        raise RuntimeError("synthetic-secret in remote response")

    monkeypatch.setattr(providers, "interpret", failed)
    response = await client.post(
        "/api/v1/runs",
        json={
            "adapter": "fixture",
            "instructions": "use specified stock",
            "provider_id": "fixture",
        },
        headers=auth(),
    )
    assert response.status_code == 502
    assert "synthetic-secret" not in response.text
    with store.session() as session:
        assert list(session.scalars(select(Run))) == []


@pytest.mark.asyncio
async def test_source_rejects_plaintext_credentials_and_invalid_policy(service):
    client, store = service
    cases = [
        {"host": "source.example", "headers": {"Authorization": "synthetic-secret"}},
        {"host": "source.example", "api_key": "synthetic-secret"},
        {"host": "user:synthetic-secret@source.example"},
        {"host": "source.example/path"},
        {"host": "source.example", "requests_per_second": 0},
        {"host": "source.example", "max_concurrency": 65},
        {
            "host": "source.example",
            "credentials": {"headers": {"Authorization": "Bearer synthetic\nsecret"}},
        },
    ]
    for payload in cases:
        response = await client.post("/api/v1/sources", json=payload, headers=auth())
        assert response.status_code == 422, response.text
        assert "synthetic-secret" not in response.text
    assert store.profiles("source") == []


@pytest.mark.asyncio
async def test_source_update_by_host_preserves_credentials_and_avoids_duplicates(
    service,
):
    client, store = service
    first = await client.post(
        "/api/v1/sources",
        json={
            "host": "source.example",
            "credentials": {"headers": {"Authorization": "Bearer synthetic-secret"}},
        },
        headers=auth(),
    )
    second = await client.post(
        "/api/v1/sources",
        json={"host": "SOURCE.EXAMPLE", "requests_per_second": 3, "max_concurrency": 2},
        headers=auth(),
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    profiles = store.profiles("source", True)
    assert (
        len(profiles) == 1
        and profiles[0]["credentials"]["headers"]["Authorization"]
        == "Bearer synthetic-secret"
    )
    assert profiles[0]["requests_per_second"] == 3


@pytest.mark.asyncio
async def test_rows_api_matches_export_projection_without_modifying_checkpoint(
    service, tmp_path
):
    import csv
    from crawler.exporter import export_rows
    from crawler.store import Product
    from test_presentation import corner_row

    client, store = service
    run_id = create_run(store)
    with store.session() as session:
        product = Product(run_id=run_id, url="https://fixture.test/cards")
        session.add(product)
        session.flush()
        product_id = product.id
    claim = store.claim("fixture-reader")
    original = corner_row()
    store.append_rows(product_id, claim["epoch"], [original])
    response = await client.get(f"/api/v1/runs/{run_id}/rows", headers=auth())
    assert response.status_code == 200
    projected = response.json()["items"][0]
    assert projected["configuration"]["roundedCorners"]["label"] == "Round Corners"
    assert list(store.iter_rows(run_id=run_id)) == [original]
    manifest = export_rows(
        store.iter_rows(run_id=run_id), tmp_path / "projection-export"
    )
    csv_path = next(
        item["path"] for item in manifest["items"] if item["name"] == "combined.csv"
    )
    with open(csv_path, newline="", encoding="utf-8") as handle:
        exported = next(csv.DictReader(handle))
    # API-only comparison/audit annotations do not alter immutable row bytes.
    from crawler.commerce import price_comparison_context

    assert projected.pop("price_comparison_context") == price_comparison_context(
        original
    )
    assert projected.pop("record_scope") == "current"
    assert exported["attr:roundedCorners"] == projected["configuration"]["roundedCorners"]["label"]
    assert exported["subtotal"] == projected["subtotal"]
    assert "record_json" not in exported and "source_product_id" not in exported
    assert list(store.iter_rows(run_id=run_id)) == [original]
