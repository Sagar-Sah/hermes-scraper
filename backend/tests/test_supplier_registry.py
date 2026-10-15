"""Custom supplier persistence and public URL validation, without supplier calls."""
import asyncio
from copy import deepcopy
import json
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from crawler.product_diagnosis import ProductDiagnosisManager, SUPPLIERS
from crawler.supplier_registry import SupplierRegistry
from crawler.transport import ExtractionError
from test_product_diagnosis import completed, manager


@pytest.fixture
def public_dns(monkeypatch):
    validate = AsyncMock()
    monkeypatch.setattr("crawler.supplier_registry.validate_url", validate)
    return validate


@pytest.mark.asyncio
async def test_private_atomic_restart_persistence_and_no_builtin_mutation(tmp_path, public_dns):
    defaults = deepcopy(SUPPLIERS)
    registry = SupplierRegistry(tmp_path, SUPPLIERS)
    item = await registry.add("  New Supplier  ", "https://www.vendor.example/", "https://vendor.example/sign-in")
    assert item["name"] == "New Supplier" and item["custom"] is True
    assert item["origin"] == "https://vendor.example"
    assert public_dns.await_count == 2
    assert SUPPLIERS == defaults
    files = list(registry.root.iterdir())
    assert len(files) == 1 and files[0].suffix == ".json"
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(registry.root.stat().st_mode) == 0o700
    restored = SupplierRegistry(tmp_path, SUPPLIERS)
    assert restored.get(item["id"]) == item
    assert len(restored.items()) == len(defaults) + 1
    assert restored.supplier_for("https://www.vendor.example/product") == item["id"]
    assert restored.supplier_for("https://vendor.example/product") == item["id"]
    assert restored.supplier_for("https://account.vendor.example/product") is None
    assert restored.canonical_origin("https://www.vendor.example:443/") == item["origin"]
    for unrelated in ("http://vendor.example", "https://vendor.example:8443", "https://account.vendor.example",
                      "https://vendor.example/product", "https://vendor.example?token=secret", "https://other.example"):
        assert restored.canonical_origin(unrelated) == unrelated
    assert restored.canonical_origin("https://printograph.com") == "https://www.printograph.com"


@pytest.mark.asyncio
async def test_duplicate_builtin_and_custom_www_aliases_reject(tmp_path, public_dns):
    registry = SupplierRegistry(tmp_path, SUPPLIERS)
    for host in ("printograph.com", "www.printograph.com", "b2sign.com", "www.b2sign.com"):
        with pytest.raises(ValueError, match="already registered"):
            await registry.add("Duplicate", "https://" + host)
    assert public_dns.await_count == 0
    original = await registry.add("First", "https://vendor.example")
    with pytest.raises(ValueError, match="already registered"):
        await registry.add("Second", "https://www.vendor.example")
    assert registry.get(original["id"])["name"] == "First"
    assert len(list(registry.root.glob("*.json"))) == 1


@pytest.mark.parametrize("url", [
    "http://vendor.example", "https://user:secret@vendor.example", "https://vendor.example:8443",
    "https://vendor.example:80", "https://vendor.example:bad", "https://vendor.example?token=secret",
    "https://vendor.example?redirect=/account", "https://vendor.example#fragment",
    " https://vendor.example", "https://vendor.example/\nlogin", "https://vendor.example\\@evil.example",
    "https://vendor.example.", "javascript:alert(1)", "https://localhost",
])
@pytest.mark.asyncio
async def test_unsafe_registration_urls_do_not_reach_dns_or_disk(tmp_path, public_dns, url):
    registry = SupplierRegistry(tmp_path, SUPPLIERS)
    with pytest.raises(ValueError):
        await registry.add("Supplier", url)
    assert public_dns.await_count == 0
    assert not list(registry.root.iterdir())


@pytest.mark.parametrize("login", ["https://login.vendor.example", "https://other.example/login",
                                   "https://vendor.example.evil.example/login", "https://vendor.example/login?token=secret"])
@pytest.mark.asyncio
async def test_login_must_be_same_exact_www_bare_domain_without_secrets(tmp_path, public_dns, login):
    registry = SupplierRegistry(tmp_path, SUPPLIERS)
    with pytest.raises(ValueError):
        await registry.add("Supplier", "https://vendor.example", login)
    assert public_dns.await_count == 0


@pytest.mark.asyncio
async def test_public_address_validation_failure_does_not_create_supplier(tmp_path, public_dns):
    public_dns.side_effect = ExtractionError("Private address", "invalid_url")
    registry = SupplierRegistry(tmp_path, SUPPLIERS)
    with pytest.raises(ExtractionError):
        await registry.add("Private supplier", "https://127.0.0.1")
    assert not list(registry.root.iterdir())


@pytest.mark.asyncio
async def test_concurrent_registrations_cannot_overwrite_a_supplier(tmp_path, monkeypatch):
    arrived = 0
    release = asyncio.Event()
    async def validate(url):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            release.set()
        await release.wait()
    monkeypatch.setattr("crawler.supplier_registry.validate_url", validate)
    first, second = SupplierRegistry(tmp_path, SUPPLIERS), SupplierRegistry(tmp_path, SUPPLIERS)
    results = await asyncio.gather(first.add("First", "https://vendor.example"),
                                   second.add("Second", "https://www.vendor.example"), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 1
    saved = [result for result in results if isinstance(result, dict)][0]
    assert first.get(saved["id"]) == saved
    assert len(list(first.root.iterdir())) == 1


@pytest.mark.asyncio
async def test_manager_cards_persist_without_opening_browser_or_jobs(tmp_path, public_dns):
    pool = SimpleNamespace(page=AsyncMock())
    manager = ProductDiagnosisManager(SimpleNamespace(root=tmp_path), pool)
    card = await manager.add_supplier("New supplier", "https://www.vendor.example", "https://vendor.example/login")
    assert card["custom"] is True
    assert card["status"] == card["session_status"] == "not_open"
    assert card["url"] == card["entry_url"] == "https://vendor.example/login"
    assert pool.origin_resolver("https://www.vendor.example") == "https://vendor.example"
    pool.page.assert_not_called()
    await manager.close()
    reopened = ProductDiagnosisManager(SimpleNamespace(root=tmp_path), pool)
    assert next(item for item in reopened.suppliers() if item["id"] == card["id"]) == card
    assert (await reopened.check_supplier(card["id"]))["session_status"] == "not_open"
    await reopened.close()


@pytest.mark.asyncio
async def test_custom_login_and_redirect_alias_product_share_the_same_owned_page_context(manager, public_dns):
    manager, pool = manager
    card = await manager.add_supplier("New supplier", "https://www.vendor.example", "https://vendor.example/login")
    pool.html["/login"] = '<main><form><input type="password"></form></main>'
    opened = await manager.open_supplier(card["id"])
    assert opened["custom"] and opened["session_status"] == "login_required"
    login_page = manager._pages["supplier:" + card["id"]]
    await login_page.real.set_content('<main><a href="/logout">Log out</a></main>')
    assert (await manager.check_supplier(card["id"]))["session_status"] == "signed_in"
    product = await completed(manager, "https://www.vendor.example/products/card")
    assert product["supplier_id"] == card["id"] and product["status"] == "ready"
    assert manager._pages[product["id"]].context is login_page.context
    assert {origin for origin, _ in pool.calls} == {"https://vendor.example"}


@pytest.mark.asyncio
async def test_many_product_diagnoses_open_at_most_three_and_retain_ready_pages(tmp_path, monkeypatch):
    monkeypatch.setattr("crawler.product_diagnosis.validate_url", AsyncMock())
    manager = ProductDiagnosisManager(SimpleNamespace(root=tmp_path), SimpleNamespace())
    active = 0
    maximum = 0
    ready, release = asyncio.Event(), asyncio.Event()
    async def opened(identifier, url, origin, hosts):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 3:
            ready.set()
        try:
            await release.wait()
            manager._pages[identifier] = object()  # Ready pages remain owned after the slot is released.
            manager._records[identifier]["status"] = "ready"
        finally:
            active -= 1
    monkeypatch.setattr(manager, "_open_diagnosis_in_slot", opened)
    try:
        records = [await manager.start(f"https://vendor.example/product/{index}") for index in range(7)]
        await asyncio.wait_for(ready.wait(), 2)
        assert active == maximum == 3
        assert not manager._pages
        release.set()
        await asyncio.gather(*manager._tasks.values())
        assert maximum == 3 and active == 0
        assert len(manager._pages) == 7
        assert all(manager.get(record["id"])["status"] == "ready" for record in records)
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_authenticated_api_add_list_and_validation(tmp_path, public_dns, monkeypatch):
    import crawler.app as api
    manager = ProductDiagnosisManager(SimpleNamespace(root=tmp_path), SimpleNamespace(page=AsyncMock()))
    monkeypatch.setattr(api.app.state, "product_diagnoses", manager, raising=False)
    monkeypatch.setattr(api.settings, "api_token", "supplier-test-token")
    headers = {"Authorization": "Bearer supplier-test-token"}
    payload = {"name": "Custom vendor", "website_url": "https://www.vendor.example", "login_url": "https://vendor.example/login"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
            assert (await client.post("/api/v1/supplier-sessions", json=payload)).status_code == 401
            response = await client.post("/api/v1/supplier-sessions", json=payload, headers=headers)
            assert response.status_code == 200, response.text
            card = response.json()
            assert card["custom"] and card["status"] == "not_open"
            listed = await client.get("/api/v1/supplier-sessions", headers=headers)
            assert len(listed.json()["items"]) == len(SUPPLIERS) + 1
            assert card in listed.json()["items"]
            duplicate = await client.post("/api/v1/supplier-sessions", json=payload, headers=headers)
            assert duplicate.status_code == 422 and "already registered" in duplicate.json()["detail"]
            invalid = await client.post("/api/v1/supplier-sessions", json={**payload, "password": "DO_NOT_SAVE"}, headers=headers)
            assert invalid.status_code == 422
            assert "DO_NOT_SAVE" not in "".join(path.read_text() for path in manager.registry.root.glob("*.json"))
            public_dns.side_effect = ExtractionError("PRIVATE_DIAGNOSTIC", "network_error")
            failed = await client.post("/api/v1/supplier-sessions", json={"name": "Other", "website_url": "https://other.example"}, headers=headers)
            assert failed.status_code == 422 and "PRIVATE_DIAGNOSTIC" not in failed.text
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_authenticated_api_delete_supplier_rejects_builtins(tmp_path, public_dns, monkeypatch):
    import crawler.app as api
    manager = ProductDiagnosisManager(SimpleNamespace(root=tmp_path), SimpleNamespace(page=AsyncMock()))
    monkeypatch.setattr(api.app.state, "product_diagnoses", manager, raising=False)
    monkeypatch.setattr(api.settings, "api_token", "supplier-delete-token")
    headers = {"Authorization": "Bearer supplier-delete-token"}
    payload = {"name": "Removable vendor", "website_url": "https://www.removable.example"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
            assert (await client.delete("/api/v1/supplier-sessions/custom_anything")).status_code == 401
            created = (await client.post("/api/v1/supplier-sessions", json=payload, headers=headers)).json()
            key = created["id"]
            assert len(list(manager.registry.root.glob("*.json"))) == 1

            builtin_key = next(iter(SUPPLIERS))
            rejected = await client.delete(f"/api/v1/supplier-sessions/{builtin_key}", headers=headers)
            assert rejected.status_code == 422 and "builtin_supplier" in rejected.text
            assert len(list(manager.registry.root.glob("*.json"))) == 1

            removed = await client.delete(f"/api/v1/supplier-sessions/{key}", headers=headers)
            assert removed.status_code == 200 and removed.json() == {"deleted": key}
            assert not list(manager.registry.root.glob("*.json"))
            listed = await client.get("/api/v1/supplier-sessions", headers=headers)
            assert all(item["id"] != key for item in listed.json()["items"])

            missing = await client.delete(f"/api/v1/supplier-sessions/{key}", headers=headers)
            assert missing.status_code == 422 and "unknown_supplier" in missing.text
    finally:
        await manager.close()
