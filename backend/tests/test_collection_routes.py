"""Offline delivery tests: a browser save never silently authorizes data loss."""

import io
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from crawler.config import Settings
from crawler.store import Product, Run, Store
from crawler.collection_routes import collection_router


@pytest.fixture
def workspace(tmp_path):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    with store.session() as session:
        run = Run(name="Delivery fixture", status="paused", spec={"adapter": "fixture"})
        session.add(run)
        session.flush()
        products = [Product(run_id=run.id, url=f"fixture://product/{index}",
                            title=f"Product {index}", status="running", epoch=1)
                    for index in range(3)]
        session.add_all(products)
        session.flush()
        run_id, ids = run.id, [p.id for p in products]
    for index, product_id in enumerate(ids):
        store.append_rows(product_id, 1, [{
            "id": f"row-{index}", "product_id": product_id,
            "product_url": f"fixture://product/{index}", "title": f"Product {index}",
            "configuration": {"size": "24 x 36", "finish": "Matte"},
            "quantity": 1, "currency": "USD", "subtotal": "12.50", "unit_price": "12.50",
            "verification_status": "verified", "images": [],
        }])
    with store.session() as session:
        for index, product_id in enumerate(ids):
            p = session.get(Product, product_id)
            p.status = "completed" if index == 0 else "paused"
            p.coverage_status = "complete" if index == 0 else "partial"
            p.owner, p.lease_until = "", 0

    async def authenticate(authorization: str = Header(default="")):
        if authorization != "Bearer test-token":
            raise HTTPException(401, "API token required")

    app = FastAPI()
    app.include_router(collection_router(lambda: store, authenticate))
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer test-token"
        yield client, store, run_id, ids
    store.engine.dispose()


def test_collection_delivery_is_authenticated_and_strict(workspace):
    client, store, _, ids = workspace
    result = client.get("/api/v1/collection/products")
    assert result.status_code == 200
    states = {p["id"]: p for p in result.json()["items"]}
    assert states[ids[0]]["complete"] is True
    assert states[ids[1]]["complete"] is False
    assert result.json()["archives"] == []
    for body in ({"product_ids": []}, {"product_ids": [True]},
                 {"product_ids": [ids[0]], "destination": "/tmp/server-secret"}):
        assert client.post("/api/v1/collection/exports", json=body).status_code == 422
    client.headers.pop("Authorization")
    assert client.get("/api/v1/collection/products").status_code == 401
    assert client.post("/api/v1/collection/exports", json={"product_ids": ids}).status_code == 401
    assert client.get("/api/v1/collection/exports/unknown/download").status_code == 401
    assert client.get("/api/v1/collection/archives/unknown/download").status_code == 401


def test_each_unfinished_product_needs_export_ack(workspace):
    client, store, _, ids = workspace
    base = "/api/v1/collection/exports"
    assert client.post(base, json={"product_ids": ids[:2]}).status_code in (409, 422)
    assert client.post(base, json={"product_ids": ids, "allow_incomplete_product_ids": [ids[1]]}).status_code in (409, 422)
    result = client.post(base, json={"product_ids": ids[:2], "allow_incomplete_product_ids": [ids[1]]})
    assert result.status_code == 200, result.text
    receipt = result.json()
    pending = client.get("/api/v1/collection/products").json()["pending_exports"]
    assert receipt["id"] in [item["id"] for item in pending]
    downloaded = client.get(receipt["url"])
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        assert any(name.endswith(".csv") for name in names)
        assert any(name.endswith(".parquet") for name in names)
        assert any(name.endswith(".jsonl") for name in names)
        assert all(not name.startswith("/") and ".." not in name.split("/") for name in names)
    with store.session() as session:
        assert all(session.get(Product, product_id) is not None for product_id in ids)


def test_save_then_archive_selection_only_cleans_exported_products(workspace):
    client, store, _, ids = workspace
    result = client.post("/api/v1/collection/exports", json={
        "product_ids": ids[:2], "allow_incomplete_product_ids": [ids[1]]})
    assert result.status_code == 200, result.text
    receipt = result.json()
    endpoint = f"/api/v1/collection/exports/{receipt['id']}/finalize"
    assert client.post(endpoint, json={"archive_product_ids": [ids[0]]}).status_code == 422
    assert client.post(endpoint, json={"archive_product_ids": [ids[0]], "saved": False}).status_code in (409, 422)
    assert client.post(endpoint, json={"archive_product_ids": [ids[2]], "saved": True}).status_code in (409, 422)
    with store.session() as session:
        assert len(list(session.scalars(select(Product)))) == 3
    response = client.post(endpoint, json={"archive_product_ids": [ids[0]], "saved": True})
    assert response.status_code == 200, response.text
    # Same request is safe to repeat after a lost response.
    assert client.post(endpoint, json={"archive_product_ids": [ids[0]], "saved": True}).status_code == 200
    with store.session() as session:
        assert session.get(Product, ids[0]) is None
        assert session.get(Product, ids[1]) is None
        assert session.get(Product, ids[2]) is not None
    listed = client.get("/api/v1/collection/products").json()
    assert not listed["pending_exports"]
    assert [p["id"] for p in listed["items"]] == [ids[2]]
    assert len(listed["archives"]) == 1
    archived = listed["archives"][0]
    response = client.get(archived.get("download_url") or archived["url"])
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        assert any(name.endswith(".csv") for name in archive.namelist())


def test_post_export_changes_prevent_destructive_cleanup(workspace):
    client, store, _, ids = workspace
    result = client.post("/api/v1/collection/exports", json={"product_ids": [ids[0]]})
    assert result.status_code == 200, result.text
    receipt = result.json()
    with store.session() as session:
        session.get(Product, ids[0]).coverage_status = "partial"
    result = client.post(f"/api/v1/collection/exports/{receipt['id']}/finalize", json={
        "archive_product_ids": [], "saved": True})
    assert result.status_code == 409, result.text
    with store.session() as session:
        assert session.get(Product, ids[0]) is not None
    assert client.get(receipt["url"]).status_code == 200
    # A stale pending receipt must not trap the customer: fresh snapshots remain
    # exportable while the old download is retained for reference.
    fresh = client.post("/api/v1/collection/exports", json={
        "product_ids": [ids[0]], "allow_incomplete_product_ids": [ids[0]]})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["id"] != receipt["id"]


def test_discard_only_removes_server_zip_and_requires_confirmation(workspace):
    client, store, _, ids = workspace
    response = client.post("/api/v1/collection/exports", json={"product_ids": [ids[0]]})
    assert response.status_code == 200, response.text
    receipt = response.json()
    endpoint = f"/api/v1/collection/exports/{receipt['id']}/discard"
    assert client.post(endpoint, json={}).status_code == 422
    assert client.post(endpoint, json={"discard": False}).status_code == 422
    assert client.get(receipt["url"]).status_code == 200
    assert client.post(endpoint, json={"discard": True}).status_code == 200
    assert client.post(endpoint, json={"discard": True}).status_code == 200
    assert client.get(receipt["url"]).status_code in (404, 409, 422)
    assert not client.get("/api/v1/collection/products").json()["pending_exports"]
    with store.session() as session:
        assert all(session.get(Product, product_id) is not None for product_id in ids)


def test_main_app_registers_authenticated_delivery_before_spa_without_workers(tmp_path):
    # Importing the real app needs an isolated Store; entering its lifespan would
    # start workers and is intentionally avoided in this registration check.
    backend = Path(__file__).resolve().parents[1]
    code = """from crawler.app import app
from fastapi.testclient import TestClient
client = TestClient(app)
response = client.get('/api/v1/collection/products', headers={'Authorization':'Bearer offline-test-token'})
assert response.status_code == 200, response.text
assert response.json() == {'items': [], 'archives': [], 'pending_exports': []}
assert client.get('/api/v1/collection/products').status_code == 401
"""
    result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, timeout=30,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(backend), "ATLAS_DATA_DIR": str(tmp_path),
             "DATABASE_URL": "sqlite:///" + str(tmp_path / "test.db"), "ATLAS_API_TOKEN": "offline-test-token"})
    assert result.returncode == 0, result.stderr
