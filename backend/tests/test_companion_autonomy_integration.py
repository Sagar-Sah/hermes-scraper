"""Isolated integration tests; never import app's global Store or use a network."""
import asyncio
import ast
from contextlib import asynccontextmanager
from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pytest

from crawler.autonomous_companion_routes import autonomous_router
from crawler.companion_autonomy_integration import (IntegratedAutonomousCompanion,
    MonthlyShippingModels, autonomy_integration_router)
from crawler.companion_environments import private_save


ROOT = Path(__file__).resolve().parents[2]
CID = "a" * 32
SECRET = "fixture-private-connector-token-" + "x" * 45


class Store:
    def __init__(self, root):
        self.root = root
    def profiles(self, kind, secrets=False):
        if kind != "provider":
            return []
        profile = {"id": "muse", "kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
            "model": "meta/muse-fixture"}
        if secrets:
            profile["credentials"] = {"api_key": "fixture-private-muse-key"}
        return [profile]


def companion():
    return SimpleNamespace(pairs={}, campaigns=SimpleNamespace(list=lambda: []))


async def catalog(url):
    parts = urlsplit(url)
    page = int(parse_qs(parts.query)["page"][0])
    rows = ([{"id": 1, "name": "Standard Print", "slug": "standard-print", "parent": 0}]
        if parts.path.endswith("categories") else [{"id": 9532, "name": "Fixture cards",
            "permalink": "https://printitprints.com/shop/fixture/", "categories": [{"id": 1, "name": "Standard Print"}]}])
    return 200, url, json.dumps(rows if page == 1 else []).encode()


def make(tmp_path):
    return IntegratedAutonomousCompanion(Store(tmp_path), companion(), SimpleNamespace(), fetch=catalog)


async def no_green(cycle, product):
    return {"status": "needs_attention", "reason": "fixture_supplier_coverage_unknown"}


async def setup_cycle(service):
    identifier = service.create_cycle(category_slugs=["standard-print"])["id"]
    await service.inventory(identifier)
    service.product_handler = no_green
    return identifier


async def authenticated(authorization: str | None = Header(default=None)):
    if authorization != "Bearer fixture-session":
        raise HTTPException(401, "authentication_required")


def client_for(service):
    app = FastAPI()
    app.include_router(autonomous_router(lambda: service, authenticated))
    app.include_router(autonomy_integration_router(lambda: service, authenticated))
    return TestClient(app), {"Authorization": "Bearer fixture-session"}


@pytest.mark.asyncio
async def test_constructor_default_off_open_close_and_public_status_never_expose_secrets(tmp_path):
    service = make(tmp_path)
    called = []
    async def forbidden(*args, **kwargs):
        called.append(1)
        raise AssertionError("Disabled integration attempted source/model I/O")
    service.fetch = forbidden
    service.director.transport = forbidden
    try:
        status = service.status()
        assert set(("config", "director", "environments", "baseline", "maintenance")).issubset(status)
        assert not status["enabled"] and not status["schedule_enabled"]
        assert status["config"]["director_provider_id"] == "muse"
        assert status["director"]["ready"] is False
        assert status["baseline"]["ready"] is False
        assert "fixture-private" not in json.dumps(status)
        await service.open()
        await asyncio.sleep(0.01)
        assert called == []
    finally:
        await service.close()
    assert service._owner.closed
    assert service._loop.done()
    assert not service._active and not service._review_tasks


@pytest.mark.asyncio
async def test_api_status_auth_and_separate_release_credentials_are_role_bound(tmp_path):
    service = make(tmp_path)
    transport_calls = []
    async def transport(cfg, method, path, payload=None):
        transport_calls.append(deepcopy(cfg))
        return {"environment": cfg["environment"], "role": cfg["role"], "site_origin": cfg["base_url"],
            "connector_version": "0.2.0", "inventory": True, "read_families": ["custom_csv"],
            "write_families": ["custom_csv"], "write_operations": ["set_cost"], "token": SECRET,
            "unexpected_private": {"password": "fixture-preview-password"}}
    service.environments.transport = transport
    client, headers = client_for(service)
    try:
        assert client.get("/api/v1/companion/autonomy/status").status_code == 401
        status = client.get("/api/v1/companion/autonomy/status", headers=headers)
        assert status.status_code == 200
        assert "director" in status.json() and "baseline" in status.json()
        generic_release = client.put("/api/v1/companion/autonomy/environments/production", headers=headers,
            json={"base_url": "https://printitprints.com", "token": SECRET, "role": "release"})
        assert generic_release.status_code == 409
        assert transport_calls == []
        no_ack = client.put("/api/v1/companion/autonomy/production-release-connection", headers=headers,
            json={"base_url": "https://printitprints.com", "token": SECRET, "acknowledge_manual_releases_only": False})
        assert no_ack.status_code == 409 and transport_calls == []
        staging = client.put("/api/v1/companion/autonomy/environments/staging", headers=headers,
            json={"base_url": "https://staging3.tonyl36.sg-host.com", "token": SECRET, "role": "write",
                "protection_auth": {"username": "fixture-preview-user", "password": "fixture-preview-password"}})
        assert staging.status_code == 200
        assert staging.json()["role"] == "write"
        assert staging.json()["protected_preview_credentials_present"] is True
        release = client.put("/api/v1/companion/autonomy/production-release-connection", headers=headers,
            json={"base_url": "https://printitprints.com", "token": SECRET, "acknowledge_manual_releases_only": True})
        assert release.status_code == 200 and release.json()["role"] == "release"
        for response in (staging, release, client.get("/api/v1/companion/autonomy/status", headers=headers)):
            assert SECRET not in response.text
            assert "fixture-preview-password" not in response.text
            assert "fixture-private-muse-key" not in response.text
        assert [x["role"] for x in transport_calls] == ["write", "release"]
        assert service.environments.status("production")["configured"] is False
        assert service.environments.status("production", role="release")["connected"] is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_batch_review_filters_product_scope_and_chunks_twenty_with_distinct_ids(tmp_path):
    service = make(tmp_path)
    proposals = [{"id": "p" + str(i), "product_id": "9532", "eligible": False, "kind": "source_options"} for i in range(45)]
    proposals += [{"id": "other", "product_id": "6862", "eligible": True, "kind": "set_cost"}]
    calls, decisions = [], []
    service.pipeline.proposals_for_cycle = lambda cid: proposals
    async def review(cycle, batch, *, config, proposals):
        calls.append((deepcopy(batch), deepcopy(proposals)))
        return {"status": "reviewed", "review_id": batch["id"], "decision": {"decisions": [], "summary": "Unknown source remains unknown"}}
    guarded = []
    def guard(review_id, config):
        guarded.append(review_id)
    async def apply(cid, values, *, authorization_guard):
        authorization_guard()
        decisions.append(values)
        return []
    service.director.review = review
    service.director.require_review_current = guard
    service.pipeline.apply_decisions = apply
    try:
        result = await service._review_batch({"id": CID, "config": deepcopy(service.config)},
            {"batch_index": 7, "products": [{"product_id": "9532", "status": "needs_attention"}]})
        assert [len(p) for _, p in calls] == [20, 20, 5]
        assert [b["id"] for b, _ in calls] == ["7/0", "7/1", "7/2"]
        assert all(p["id"] != "other" for _, chunk in calls for p in chunk)
        assert result["production_changes"] == 0
        assert decisions == [[], [], []]
        assert guarded == ["7/0", "7/0", "7/1", "7/1", "7/2", "7/2"]
        assert not any(name in inspect.getsource(service._review_batch) for name in ("approve_production", ".promote("))
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_provider_suspended_after_review_cannot_stage_its_decisions(tmp_path):
    service = make(tmp_path)
    service.pipeline.proposals_for_cycle = lambda cid: [{"id": "p1", "product_id": "9532", "eligible": True}]
    calls = []
    async def review(cycle, batch, **kwargs):
        return {"status": "reviewed", "review_id": "fixture-review", "decision": {"decisions": []}}
    def guard(review_id, config):
        assert review_id == "fixture-review"
        raise ValueError("provider_suspended")
    async def forbidden(*args, **kwargs):
        calls.append("write")
        raise AssertionError("suspended provider staged a decision")
    service.director.review = review
    service.director.require_review_current = guard
    service.pipeline.apply_decisions = forbidden
    try:
        result = await service._review_batch({"id": CID, "config": deepcopy(service.config)},
            {"batch_index": 0, "products": [{"product_id": "9532"}]})
        assert result["status"] == "held"
        assert result["reviews"][0]["staging_repairs"][0]["status"] == "held"
        assert calls == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_disabled_director_produces_no_model_requests_or_decisions(tmp_path):
    service = make(tmp_path)
    calls = []
    async def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("Disabled director made a request")
    service.director.transport = forbidden
    try:
        result = await service._review_batch({"id": CID, "config": deepcopy(service.config)},
            {"batch_index": 0, "products": []})
        assert result["status"] == "held"
        assert result["reviews"][0]["reason"] == "director_disabled"
        assert result["production_changes"] == 0
        assert calls == []
        assert service.director.budget.status()["calls"] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cycle_request_cannot_override_server_baseline_or_software_identity(tmp_path):
    service = make(tmp_path)
    service.software_identity = lambda: {"source_build": "fixture-build"}
    try:
        cycle = service.create_cycle(baseline={"ready": True, "server_baseline": {"ready": True}})
        assert cycle["baseline"]["server_baseline"]["ready"] is False
        assert cycle["baseline"]["software_build"] == {"source_build": "fixture-build"}
        assert cycle["baseline"]["request_context"]["ready"] is True
        assert not service.status()["baseline"]["ready"]
        assert cycle["evidence_namespace"].endswith(cycle["id"])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_maintenance_blocks_direct_inventory_and_admission_until_released(tmp_path):
    service = make(tmp_path)
    identifier = service.create_cycle()["id"]
    called = []
    async def observed(url):
        called.append(url)
        return await catalog(url)
    service.fetch = observed
    try:
        await service.begin_maintenance()
        try:
            await service.inventory(identifier)
        except ValueError:
            pass
        assert called == []
        assert service.status()["maintenance"] is True
        await service.end_maintenance()
        actual = await service.inventory(identifier)
        assert actual["inventory"]["complete"] is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_maintenance_fences_scheduled_handoff_even_before_inventory_task_starts(tmp_path):
    service = make(tmp_path)
    service.configure(enabled=True, schedule_enabled=True)
    called = []
    async def observed(url):
        called.append(url)
        return await catalog(url)
    service.fetch = observed
    try:
        identifier = service.create_cycle(kind="scheduled")["id"]
        # This is the exact gap between coordinator scheduling the handoff and
        # the new coroutine receiving its first event-loop turn.
        work = asyncio.create_task(service._scheduled_start(identifier))
        service._inventory_tasks[identifier] = work
        await service.begin_maintenance()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert called == []
        assert work.done()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_maintenance_drains_owned_product_and_ai_review_without_cancelling_source(tmp_path):
    service = make(tmp_path)
    gate = asyncio.Event()
    calls = []
    cancelled = []
    async def handler(cycle, product):
        calls.append(product["product_id"])
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.append(product["product_id"])
            raise
        return {"status": "needs_attention", "reason": "fixture_unverified"}
    identifier = await setup_cycle(service)
    service.product_handler = handler
    try:
        await service.start(identifier)
        for _ in range(5):
            await service.tick()
            await asyncio.sleep(0)
        assert calls == ["9532"]
        review = asyncio.create_task(gate.wait())
        service._review_tasks[(identifier, 0)] = review
        maintenance = asyncio.create_task(service.begin_maintenance(timeout=1))
        await asyncio.sleep(0)
        assert not maintenance.done()
        assert not cancelled
        gate.set()
        await maintenance
        assert service.get_cycle(identifier)["products"][0]["status"] == "needs_attention"
        assert review.done()
        assert not cancelled
        await service.tick()
        assert calls == ["9532"]
    finally:
        gate.set()
        await service.close()
    assert service._owner.closed and not service._active and not service._review_tasks


@pytest.mark.asyncio
async def test_explicit_maintenance_recovery_allows_resume_without_replaying_held_product(tmp_path):
    service = make(tmp_path)
    identifier = await setup_cycle(service)
    record = service.cycles[identifier]
    record["products"][0].update(status="held", reason="handler_interrupted_unknown_effect", attempts=1)
    service._save_cycle(record)
    await service.start(identifier)
    await service.begin_maintenance()
    client, headers = client_for(service)
    try:
        response = client.post("/api/v1/companion/autonomy/maintenance/end", headers=headers, json={})
        assert response.status_code == 200
        assert service.status()["maintenance"] is False
        await service.resume(identifier)
        await service.tick()
        assert service.get_cycle(identifier)["products"][0]["status"] == "held"
        assert service.get_cycle(identifier)["products"][0]["attempts"] == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_maintenance_between_paid_chunks_prevents_next_ai_request_or_staging_action(tmp_path):
    service = make(tmp_path)
    service.pipeline.proposals_for_cycle = lambda cid: [{"id": "p" + str(i), "product_id": "9532", "eligible": False} for i in range(25)]
    calls, writes = [], []
    async def review(cycle, batch, **kwargs):
        calls.append(batch["id"])
        service._maintenance = True
        return {"status": "reviewed", "decision": {"decisions": [], "summary": "Fixture"}}
    async def apply(*args):
        writes.append(args)
        return []
    service.director.review = review
    service.pipeline.apply_decisions = apply
    try:
        result = await service._review_batch({"id": CID, "config": deepcopy(service.config)},
            {"batch_index": 5, "products": [{"product_id": "9532", "status": "needs_attention"}]})
        assert result["status"] == "held"
        assert calls == ["5/0"]
        assert writes == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_shipping_model_adapter_is_cycle_scoped_and_unknown_never_counts_as_match(tmp_path):
    models = MonthlyShippingModels(tmp_path)
    manifest = {"product_ids": ["9532"], "collector_profile": "fixture", "model_sha256": "b" * 64,
        "scope": {"supplier": "fixture"}}
    private_save(models.root / "fixture-model.json", manifest)
    calls = []
    async def collect(cycle, model):
        calls.append(cycle["id"])
        return []  # No invented quote or passing benchmark.
    models.collectors["fixture"] = collect
    snapshot = {"shipping": {"model_id": "fixture-model", "model_sha256": "b" * 64}}
    first = await models({"id": CID, "created_at": 1}, {"product_id": "9532"}, snapshot)
    cached = await models({"id": CID, "created_at": 1}, {"product_id": "9532"}, snapshot)
    later = await models({"id": "c" * 32, "created_at": 1}, {"product_id": "9532"}, snapshot)
    assert first["status"] == cached["status"] == later["status"] == "unknown"
    assert calls == [CID, "c" * 32]


def isolated_lifespan(monkeypatch, *, interrupted_update=False):
    """Execute only the real lifespan AST, injecting every constructor/import.

    This deliberately does not import crawler.app: its module-level Store must
    never initialize against a user's runtime data during a test.
    """
    source = (ROOT / "backend/crawler/app.py").read_text()
    definition = next(node for node in ast.parse(source).body if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
    events = []
    class Component:
        def __init__(self, *args, **kwargs):
            self.browser_pool = object()
            self.factory = self.validate_context = lambda *a: None
        async def start(self): events.append(type(self).__name__ + ".start")
        async def open(self): events.append(type(self).__name__ + ".open")
        async def close(self): events.append(type(self).__name__ + ".close")
        async def stop(self): events.append(type(self).__name__ + ".stop")
        async def finish_run(self, *args): pass
        async def begin_maintenance(self): events.append(type(self).__name__ + ".maintenance")
        async def end_maintenance(self): events.append(type(self).__name__ + ".released")
    def named(name):
        return type(name, (Component,), {})
    class Updater:
        def __init__(self, root, data, prepare, **kwargs):
            self.prepare, self.release = prepare, kwargs.get("release")
        def startup_pause_required(self): return interrupted_update
    def module(name, **members):
        result = ModuleType(name)
        result.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, result)
    async def reconcile(app, store): events.append("reconcile")
    async def drain(app, store): events.append("drain")
    module("crawler.product_companion_service", ProductCompanionService=named("Companion"))
    module("crawler.b2sign_harvest", SharedQuoteGate=lambda n: object())
    module("crawler.campaign_automation_registry", build_campaign_automation=lambda *a, **k: named("CampaignAutomation")())
    module("crawler.companion_autonomy_integration", IntegratedAutonomousCompanion=named("Autonomy"))
    module("crawler.managed_update", ManagedUpdater=Updater)
    module("crawler.update_routes", drain_for_update=drain, reconcile_interrupted_update=reconcile)
    namespace = {"__name__": "crawler._isolated_lifespan_fixture", "__package__": "crawler",
        "__file__": str(ROOT / "backend/crawler/app.py"), "asynccontextmanager": asynccontextmanager,
        "Path": Path, "asyncio": asyncio, "runtime_build": SimpleNamespace(diagnostic=lambda: "fixture", snapshot=lambda: {}),
        "store": SimpleNamespace(root=Path("/unused-fixture"))}
    for name in ("Engine", "ProbeRunner", "SourceDomainSessions", "VariationPlanCache", "ProductDiagnosisManager", "CampaignManager", "SourceDomainCoordinator"):
        namespace[name] = named(name)
    exec(compile(ast.Module(body=[definition], type_ignores=[]), "<isolated real app lifespan>", "exec"), namespace)
    return namespace["lifespan"], SimpleNamespace(state=SimpleNamespace()), events


@pytest.mark.asyncio
@pytest.mark.parametrize("body_error", [False, True])
async def test_real_app_lifespan_wires_autonomy_and_closes_before_supplier_browser(monkeypatch, body_error):
    lifespan, app, events = isolated_lifespan(monkeypatch)
    try:
        async with lifespan(app):
            assert "Autonomy.open" in events
            assert app.state.autonomous_companion.software_identity() == {}
            if body_error:
                raise RuntimeError("fixture body failure")
    except RuntimeError as exc:
        assert body_error and str(exc) == "fixture body failure"
    assert "Autonomy.close" in events
    assert events.index("Autonomy.close") < events.index("ProductDiagnosisManager.close") < events.index("Engine.stop")


@pytest.mark.asyncio
async def test_real_app_update_callbacks_include_autonomy_fences(monkeypatch):
    lifespan, app, events = isolated_lifespan(monkeypatch)
    async with lifespan(app):
        await app.state.updater.prepare()
        assert events.index("Autonomy.maintenance") < events.index("drain")
        await app.state.updater.release()
        assert "Autonomy.released" in events


@pytest.mark.asyncio
async def test_real_app_failed_autonomy_drain_always_releases_its_fence(monkeypatch):
    lifespan, app, events = isolated_lifespan(monkeypatch)
    async with lifespan(app):
        async def fail_drain():
            events.append("Autonomy.drain_failed")
            raise ValueError("autonomy_workers_still_draining")
        app.state.autonomous_companion.begin_maintenance = fail_drain
        with pytest.raises(ValueError, match="draining"):
            await app.state.updater.prepare()
        assert "Autonomy.released" in events
        assert "drain" not in events


@pytest.mark.asyncio
async def test_real_app_shutdown_keeps_closing_other_resources_after_one_close_fails(monkeypatch):
    lifespan, app, events = isolated_lifespan(monkeypatch)
    async with lifespan(app):
        async def fail_close():
            events.append("Autonomy.close_failed")
            raise RuntimeError("fixture-private-shutdown-detail")
        app.state.autonomous_companion.close = fail_close
    assert "ProductDiagnosisManager.close" in events and "Engine.stop" in events
    assert app.state.shutdown_incomplete == ["autonomous_companion"]
    assert "fixture-private" not in json.dumps(app.state.shutdown_incomplete)


def test_actual_app_startup_and_shutdown_in_isolated_process_with_network_forbidden(tmp_path):
    data = tmp_path / "isolated-runtime"
    environment = dict(os.environ)
    environment.update(ATLAS_DATA_DIR=str(data), DATABASE_URL="sqlite:///" + str(data / "fixture.sqlite3"),
        ATLAS_API_TOKEN="fixture-private-api-token", ATLAS_SECRET_KEY="", ATLAS_S3_BUCKET="",
        PYTHONPATH=str(ROOT / "backend"), ATLAS_BROWSER_PERSIST_SESSION="false")
    script = r'''
import os, socket
from pathlib import Path
import httpx
from fastapi.testclient import TestClient

calls = []
async def forbid_http(self, *args, **kwargs):
    calls.append('http')
    raise AssertionError('Network forbidden in isolated integration test')
def forbid_socket(self, *args, **kwargs):
    calls.append('socket')
    raise AssertionError('Network forbidden in isolated integration test')
httpx.AsyncClient.request = forbid_http
socket.socket.connect = forbid_socket
from crawler import app as module
assert module.store.root == Path(os.environ['ATLAS_DATA_DIR']).resolve()
with TestClient(module.app) as client:
    response = client.get('/api/v1/companion/autonomy/status', headers={'Authorization':'Bearer fixture-private-api-token'})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['enabled'] is False and value['schedule_enabled'] is False
    assert value['director']['ready'] is False and value['baseline']['ready'] is False
    assert set(['config','director','environments','baseline','maintenance']).issubset(value)
    assert 'fixture-private-api-token' not in response.text
    assert not module.app.state.engine.browser_pool._contexts
    assert module.app.state.engine.browser_pool._playwright is None
assert module.app.state.autonomous_companion._owner.closed
assert module.app.state.autonomous_companion._closed
assert module.app.state.shutdown_incomplete == []
assert calls == [], calls
print('ISOLATED_APP_LIFECYCLE_PASS')
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=environment,
        text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ISOLATED_APP_LIFECYCLE_PASS" in result.stdout
    assert (data / "fixture.sqlite3").is_file()
