import asyncio
from datetime import datetime
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pytest

from crawler.autonomous_companion import AutonomousCompanion, monthly_due, next_monthly_due
from crawler.autonomous_companion_routes import autonomous_router


SITE = "https://printitprints.com"
CATEGORIES = [
    {"id": 1, "name": "Standard Print", "slug": "standard-print", "parent": 0},
    {"id": 2, "name": "Business Cards", "slug": "business-cards", "parent": 1},
    {"id": 3, "name": "Apparel", "slug": "apparel", "parent": 0},
]


def product(pid, category=2):
    return {"id": pid, "name": "Product " + str(pid), "permalink": SITE + "/shop/product-" + str(pid) + "/",
        "categories": [{"id": category, "name": "Category"}]}


def verified():
    return {"status": "verified", "coverage": {"complete": True, "fresh": True},
        "required_checks": ["options", "prices", "shipping"],
        "checks": {"options": "match", "prices": "match", "shipping": "match"},
        "evidence_ids": ["current-fixture-evidence"]}


class Catalog:
    def __init__(self, products, *, page_size=100, categories=None):
        self.products, self.categories = products, categories or CATEGORIES
        self.page_size, self.calls = page_size, []
        self.fail_page = None
        self.repeat_page = False
        self.headers = False

    async def __call__(self, url):
        self.calls.append(url)
        parts = urlsplit(url)
        if parts.path.endswith("/categories") and parse_qs(parts.query).get("orderby") != ["slug"]:
            return 400, url, b'{"code":"rest_invalid_param","message":"orderby is not one of name, slug, and count."}'
        page = int(parse_qs(parts.query)["page"][0])
        rows = self.categories if parts.path.endswith("/categories") else self.products
        if self.fail_page == page and not parts.path.endswith("/categories"):
            return 503, url, b'{"code":"unavailable"}'
        offset = (page - 1) * self.page_size
        values = rows[:self.page_size] if self.repeat_page else rows[offset:offset + self.page_size]
        response = (200, url, json.dumps(values).encode())
        return (*response, {"X-WP-Total": str(len(rows))}) if self.headers else response


@pytest.fixture
def managers(tmp_path):
    values = []
    def make(products=None, *, fetch=None, handler=None, director=None, clock=None, subdir="data"):
        store = SimpleNamespace(root=tmp_path / subdir)
        catalog = fetch or Catalog(products or [product(1)])
        async def default_handler(cycle, item):
            return verified()
        manager = AutonomousCompanion(store, product_handler=handler or default_handler,
            director_review=director, fetch=catalog, clock=clock)
        manager.mapping_resolver = lambda p: {"supplier": "4over", "source_url": "https://4over.com/product-" + p["product_id"]}
        values.append(manager)
        return manager, catalog
    yield make, values


async def prepare(manager):
    cycle = manager.create_cycle()
    await manager.inventory(cycle["id"])
    await manager.start(cycle["id"])
    return cycle["id"]


async def settle(manager, iterations=50):
    for _ in range(iterations):
        await manager.tick()
        await asyncio.sleep(0)
    await manager.tick()


@pytest.mark.asyncio
async def test_default_off_has_no_network_model_or_product_work(managers):
    make, _ = managers
    manager, catalog = make()
    try:
        for _ in range(3):
            await manager.tick()
        assert manager.status()["enabled"] is False
        assert manager.status()["schedule_enabled"] is False
        assert manager.list_cycles() == []
        assert catalog.calls == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_unresolved_mapping_is_a_product_exception_not_inventory_failure(managers):
    make, _ = managers
    manager, catalog = make([product(1), product(2)])
    manager.mapping_resolver = lambda item: {"mapping_status": "unknown", "reason": "exact_supplier_mapping_required"}
    try:
        cycle = manager.create_cycle()
        result = await manager.inventory(cycle["id"])
        assert result["inventory"]["complete"] is True
        assert result["inventory"]["count"] == 2
        assert all(row["mapping_status"] == "unknown" and row["source_url"] is None
                   and row["mapping_reason"] == "exact_supplier_mapping_required" for row in result["products"])
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_inventory_pages_entire_catalog_deduplicates_prioritizes_and_pins_baseline(managers):
    make, _ = managers
    rows = [product(1, 3)] + [product(i) for i in range(2, 207)]
    rows.insert(110, product(110))  # Identical overlap across pages, one product identity.
    manager, catalog = make(rows)
    try:
        cycle = manager.create_cycle(baseline={"release_sha256": "a" * 64})
        actual = await manager.inventory(cycle["id"])
        assert actual["inventory"]["complete"] is True
        assert actual["inventory"]["count"] == 206
        assert actual["inventory"]["duplicate_product_ids"] == 1
        assert actual["inventory"]["pages"] == 3
        assert len(actual["working_window"]) == len(actual["next_window"]) == 99
        assert actual["products"][0]["product_id"] == "2"
        assert actual["products"][-1]["product_id"] == "1"
        assert actual["baseline"] == {"release_sha256": "a" * 64}
        assert actual["inventory"]["evidence_pages"]
        for evidence in actual["inventory"]["evidence_pages"]:
            assert hashlib.sha256((manager.root / evidence["path"]).read_bytes()).hexdigest() == evidence["sha256"]
        calls = len(catalog.calls)
        await manager.inventory(cycle["id"])
        assert len(catalog.calls) == calls
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_short_pages_are_not_mistaken_for_end_of_inventory(managers):
    make, _ = managers
    fetch = Catalog([product(i) for i in range(1, 56)], page_size=25)
    manager, _ = make(fetch=fetch)
    try:
        result = await manager.inventory(manager.create_cycle()["id"])
        assert result["inventory"]["complete"]
        assert result["inventory"]["count"] == 55
        assert result["inventory"]["pages"] == 3
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_declared_catalog_total_must_match_unique_observed_inventory(managers):
    make, _ = managers
    catalog = Catalog([product(1), product(2)])
    catalog.headers = True
    async def inconsistent(url):
        status, final, raw, headers = await catalog(url)
        if not urlsplit(url).path.endswith("categories"):
            headers["X-WP-Total"] = "3"
        return status, final, raw, headers
    manager, _ = make(fetch=inconsistent)
    try:
        actual = await manager.inventory(manager.create_cycle()["id"])
        assert actual["inventory"]["complete"] is False
        assert actual["inventory"]["reason"] == "catalog_total_does_not_match_inventory"
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["later_failure", "repeated_page", "bad_category", "changing_identity"])
async def test_incomplete_or_unstable_inventory_cannot_start_work(managers, failure):
    make, _ = managers
    fetch = Catalog([product(i) for i in range(1, 105)])
    if failure == "later_failure":
        fetch.fail_page = 2
    if failure == "repeated_page":
        fetch.repeat_page = True
    if failure == "bad_category":
        fetch.products[0]["categories"] = [{"id": 404}]
    if failure == "changing_identity":
        changed = product(100)
        changed["name"] = "Changed between pages"
        fetch.products.insert(100, changed)
    manager, _ = make(fetch=fetch)
    try:
        result = await manager.inventory(manager.create_cycle()["id"])
        assert result["inventory"]["complete"] is False
        assert result["status"] == "inventory_failed"
        with pytest.raises(ValueError, match="complete_inventory_required"):
            await manager.start(result["id"])
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_independent_suppliers_share_capacity_but_same_supplier_is_serial(managers):
    make, _ = managers
    active, source_active, seen, peak = 0, {}, [], 0
    async def handler(cycle, item):
        nonlocal active, peak
        source = item["supplier"]
        active += 1
        source_active[source] = source_active.get(source, 0) + 1
        assert source_active[source] == 1
        peak = max(peak, active)
        seen.append(item["product_id"])
        await asyncio.sleep(0.005)
        active -= 1
        source_active[source] -= 1
        return verified()
    manager, _ = make([product(i) for i in range(1, 13)], handler=handler)
    manager.mapping_resolver = lambda p: {"supplier": "source" + str(int(p["product_id"]) % 3), "source_url": "https://supplier.example/" + p["product_id"]}
    try:
        cycle_id = await prepare(manager)
        for _ in range(40):
            await manager.tick()
            await asyncio.sleep(0.005)
        result = manager.get_cycle(cycle_id)
        assert len(set(seen)) == 12
        assert peak == 3
        assert result["status"] == "completed"
        assert result["summary"]["verified"] == 12
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_next_batches_continue_during_director_outage_without_ai_retries(managers):
    make, _ = managers
    reviews, handled = [], []
    async def handler(cycle, item):
        handled.append(item["product_id"])
        return verified()
    async def broken_director(cycle, batch):
        reviews.append(batch["batch_index"])
        raise RuntimeError("Unavailable model; never put provider exception into receipt")
    manager, _ = make([product(i) for i in range(1, 203)], handler=handler, director=broken_director)
    manager.configure(director_enabled=True, director_provider_id="cheap-model")
    manager.mapping_resolver = lambda p: {"supplier": "s" + str(int(p["product_id"]) % 4), "source_url": "https://supplier.example/" + p["product_id"]}
    try:
        cycle_id = await prepare(manager)
        await settle(manager, 420)
        result = manager.get_cycle(cycle_id)
        assert len(handled) == len(set(handled)) == 202
        assert result["status"] == "completed"
        assert sorted(reviews) == [0, 1, 2]
        assert all(r["status"] == "failed" for r in result["director_reviews"].values())
        assert not result["working_window"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_auth_blocks_only_one_supplier_and_does_not_block_later_window(managers):
    make, _ = managers
    called = []
    async def handler(cycle, item):
        called.append(item["product_id"])
        return {"status": "access_required", "reason": "supplier_authentication_required"} if item["supplier"] == "4over" else verified()
    rows = [product(i) for i in range(1, 104)]
    manager, _ = make(rows, handler=handler)
    manager.mapping_resolver = lambda p: {"supplier": "4over" if int(p["product_id"]) <= 101 else "b2sign", "source_url": "https://supplier.example/" + p["product_id"]}
    try:
        cycle_id = await prepare(manager)
        await settle(manager, 40)
        result = manager.get_cycle(cycle_id)
        assert set(called) == {"1", "102", "103"}
        assert result["summary"]["access_required"] == 101
        assert result["summary"]["verified"] == 2
        assert result["status"] == "completed_with_attention"
        assert result["supplier_blocks"] == {"4over": "supplier_authentication_required"}
        # Recovery releases only never-dispatched blocked products; original
        # auth receipt and any uncertain selection remain retained.
        manager.product_handler = lambda cycle, item: asyncio.sleep(0, result=verified())
        await manager.resume(cycle_id, supplier="4over")
        await settle(manager, 800)
        result = manager.get_cycle(cycle_id)
        assert result["products"][0]["status"] == "access_required"
        assert result["summary"]["verified"] == 102
        assert result["products"][0]["attempts"] == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_continuation_requires_progress_respects_cooldown_and_yields(managers):
    make, _ = managers
    now, calls = [1000.0], []
    async def handler(cycle, item):
        calls.append(item["product_id"])
        if item["product_id"] == "1" and not item["progress_count"]:
            return {"status": "continue", "progress_count": 1, "progress_token": "a" * 64, "retry_after_seconds": 20}
        return verified()
    manager, _ = make([product(1), product(2)], handler=handler, clock=lambda: now[0])
    try:
        cycle_id = await prepare(manager)
        await settle(manager, 15)
        assert calls == ["1"]
        now[0] += 21
        await settle(manager, 30)
        assert calls == ["1", "2", "1"]
        assert manager.get_cycle(cycle_id)["summary"]["verified"] == 2
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_restart_fences_uncertain_intent_and_new_cycle_has_fresh_namespace(managers):
    make, _ = managers
    calls = []
    async def handler(cycle, item):
        calls.append((cycle["evidence_namespace"], item["product_id"]))
        return verified()
    manager, fetch = make([product(1), product(2)], handler=handler)
    cycle_id = await prepare(manager)
    record = manager.cycles[cycle_id]
    record["products"][0].update(status="running", attempts=1)
    manager._save_cycle(record)  # Simulate crash after dispatch intent, before receipt.
    await manager.close()
    replacement, _ = make(fetch=fetch, handler=handler)
    try:
        await settle(replacement, 30)
        result = replacement.get_cycle(cycle_id)
        assert result["products"][0]["status"] == "held"
        assert result["products"][0]["reason"] == "handler_interrupted_unknown_effect"
        assert [pid for _, pid in calls] == ["2"]
        next_id = await prepare(replacement)
        await settle(replacement, 30)
        assert next_id != cycle_id
        assert replacement.get_cycle(next_id)["evidence_namespace"] != result["evidence_namespace"]
        assert len(calls) == 3
    finally:
        await replacement.close()


@pytest.mark.asyncio
async def test_unknown_incomplete_or_historical_receipt_never_becomes_green(managers):
    make, _ = managers
    receipts = [verified() for _ in range(5)]
    receipts[0]["checks"]["prices"] = "unknown"
    receipts[1]["coverage"]["fresh"] = False
    receipts[2]["evidence_ids"] = []
    receipts[3]["required_checks"] = ["missing_check"]
    receipts[4].pop("required_checks")
    async def handler(cycle, item):
        return receipts[int(item["product_id"]) - 1]
    manager, _ = make([product(i) for i in range(1, 6)], handler=handler)
    try:
        cycle_id = await prepare(manager)
        await settle(manager, 40)
        result = manager.get_cycle(cycle_id)
        assert result["summary"]["verified"] == 0
        assert result["summary"]["needs_attention"] == 5
        assert all(p["result"]["evidence_complete"] is False for p in result["products"])
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_changed_baseline_or_mapping_cannot_dispatch(managers):
    make, _ = managers
    manager, _ = make()
    try:
        cycle_id = manager.create_cycle(baseline={"release": "first"})["id"]
        await manager.inventory(cycle_id)
        manager.cycles[cycle_id]["products"][0]["source_url"] = "https://other.example/changed"
        with pytest.raises(ValueError, match="cycle_inventory_mapping_changed"):
            await manager.start(cycle_id)
        manager.cycles[cycle_id]["baseline"]["release"] = "changed"
        with pytest.raises(ValueError, match="cycle_baseline_changed"):
            await manager.start(cycle_id)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_manual_standard_print_scope_includes_descendants_after_complete_inventory(managers):
    make, _ = managers
    manager, catalog = make([product(1, 3)] + [product(i) for i in range(2, 106)])
    try:
        identifier = manager.create_cycle(category_slugs=["standard-print"], product_ids=["2", "105"])["id"]
        actual = await manager.inventory(identifier)
        assert actual["inventory"]["complete"]
        assert actual["inventory"]["catalog_count"] == 105
        assert actual["inventory"]["selected_count"] == actual["inventory"]["count"] == 2
        assert actual["inventory"]["pages"] == 2
        assert [p["product_id"] for p in actual["products"]] == ["2", "105"]
        assert actual["scope"] == {"category_slugs": ["standard-print"], "product_ids": ["2", "105"]}
        assert any("hide_empty=false" in url for url in catalog.calls)
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,reason", [({"category_slugs": ["missing"]}, "catalog_requested_category_missing"),
    ({"product_ids": ["999"]}, "catalog_requested_product_missing")])
async def test_requested_missing_manual_scope_is_explicit_not_silently_excluded(managers, scope, reason):
    make, _ = managers
    manager, _ = make()
    try:
        actual = await manager.inventory(manager.create_cycle(**scope)["id"])
        assert actual["inventory"]["complete"] is False
        assert actual["inventory"]["reason"] == reason
        assert not actual["products"]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_scope_overrides_never_change_scheduled_all_catalog_default(managers):
    make, _ = managers
    manager, _ = make()
    try:
        for changes in ({"category_slugs": ["standard-print"]}, {"product_ids": ["1"]}):
            with pytest.raises(ValueError, match="scheduled_scope_must_cover_all_catalog"):
                manager.create_cycle(kind="scheduled", **changes)
        for changes in ({"category_slugs": []}, {"product_ids": ["0"]}, {"product_ids": ["1", "1"]},
                {"category_slugs": ["standard-print?bad"]}):
            with pytest.raises(ValueError, match="invalid_manual_catalog_scope"):
                manager.create_cycle(**changes)
        actual = manager.create_cycle()
        assert actual["scope"] == {"category_slugs": None, "product_ids": None}
        assert "category_slugs" not in manager.config
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_fair_work_window_does_not_strand_other_source_behind_first_99(managers):
    make, _ = managers
    gate, calls = asyncio.Event(), []
    async def handler(cycle, item):
        calls.append(item["product_id"])
        if item["supplier"] == "4over":
            await gate.wait()
        return verified()
    manager, _ = make([product(i) for i in range(1, 106)], handler=handler)
    manager.mapping_resolver = lambda p: {"supplier": "4over" if int(p["product_id"]) < 105 else "b2sign", "source_url": "https://supplier.example/" + p["product_id"]}
    try:
        identifier = await prepare(manager)
        await settle(manager, 12)
        assert set(calls) == {"1", "105"}
        result = manager.get_cycle(identifier)
        assert result["summary"]["running"] == 1
        assert result["summary"]["verified"] == 1
        assert len(result["working_window"]) == 99
        assert len(result["next_window"]) == 5
    finally:
        gate.set()
        await manager.close()


@pytest.mark.asyncio
async def test_nonadvancing_continue_is_held_and_never_retried(managers):
    make, _ = managers
    calls = []
    async def handler(cycle, item):
        calls.append(item["product_id"])
        return {"status": "continue", "progress_count": 0, "progress_token": "b" * 64}
    manager, _ = make(handler=handler)
    try:
        identifier = await prepare(manager)
        await settle(manager, 40)
        result = manager.get_cycle(identifier)
        assert calls == ["1"]
        assert result["products"][0]["status"] == "held"
        assert result["status"] == "completed_with_attention"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_pause_during_handler_preserves_uncertainty_and_resume_does_not_repeat(managers):
    make, _ = managers
    gate, called = asyncio.Event(), []
    async def handler(cycle, item):
        called.append(item["product_id"])
        await gate.wait()
        return verified()
    manager, _ = make([product(1), product(2)], handler=handler)
    try:
        identifier = await prepare(manager)
        await settle(manager, 6)
        paused = await manager.pause(identifier)
        assert paused["status"] == "paused"
        assert paused["products"][0]["status"] == "held"
        manager.product_handler = lambda cycle, item: asyncio.sleep(0, result=verified())
        await manager.resume(identifier)
        await settle(manager, 30)
        result = manager.get_cycle(identifier)
        assert result["products"][0]["attempts"] == 1
        assert result["products"][1]["status"] == "verified"
        assert called == ["1"]
    finally:
        gate.set()
        await manager.close()


@pytest.mark.asyncio
async def test_pause_during_readonly_inventory_can_be_explicitly_retried(managers):
    make, _ = managers
    gate = asyncio.Event()
    catalog = Catalog([product(1)])
    async def waiting(url):
        await gate.wait()
        return await catalog(url)
    manager, _ = make(fetch=waiting)
    identifier = manager.create_cycle()["id"]
    work = asyncio.create_task(manager.inventory(identifier))
    try:
        await asyncio.sleep(0)
        assert manager.get_cycle(identifier)["status"] == "inventorying"
        paused = await manager.pause(identifier)
        assert paused["status"] == "paused"
        assert paused["inventory"]["complete"] is False
        assert paused["inventory"]["reason"] == "inventory_paused"
        gate.set()
        retried = await manager.inventory(identifier)
        assert retried["status"] == "ready"
        assert retried["inventory"]["complete"] is True
    finally:
        gate.set()
        await asyncio.gather(work, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_monthly_calendar_catchup_once_and_no_overlapping_cycle(managers):
    make, _ = managers
    zone = ZoneInfo("America/Phoenix")
    now = [datetime(2026, 11, 1, 1, 59, tzinfo=zone).timestamp()]
    manager, _ = make(clock=lambda: now[0])
    manager.configure(enabled=True, schedule_enabled=True)
    try:
        await manager.tick()
        assert not manager.list_cycles()
        now[0] = datetime(2026, 11, 14, 8, tzinfo=zone).timestamp()
        await settle(manager, 50)
        assert len(manager.list_cycles()) == 1
        assert manager.list_cycles()[0]["scheduled_month"] == "2026-11"
        assert manager.list_cycles()[0]["status"] == "completed"
        await settle(manager, 20)
        assert len(manager.list_cycles()) == 1
        # A paused manual cycle must not be overwritten by next month's job.
        manual = await prepare(manager)
        await manager.pause(manual)
        now[0] = datetime(2026, 12, 5, 8, tzinfo=zone).timestamp()
        await settle(manager, 20)
        assert len(manager.list_cycles()) == 2
        assert manager.status()["active_cycle_id"] == manual
    finally:
        await manager.close()


def test_phoenix_schedule_handles_short_months_year_boundary_and_no_dst():
    zone = ZoneInfo("America/Phoenix")
    before = datetime(2026, 12, 31, 23, tzinfo=zone).timestamp()
    assert datetime.fromtimestamp(next_monthly_due(before), zone) == datetime(2027, 1, 1, 2, tzinfo=zone)
    february = datetime(2027, 2, 28, 23, tzinfo=zone).timestamp()
    assert datetime.fromtimestamp(next_monthly_due(february), zone) == datetime(2027, 3, 1, 2, tzinfo=zone)
    assert datetime.fromtimestamp(monthly_due(february), zone).day == 1


@pytest.mark.asyncio
async def test_private_owner_and_strict_configuration_and_authenticated_routes(managers):
    make, _ = managers
    manager, _ = make()
    async def authenticated(authorization: str | None = Header(default=None)):
        if authorization != "Bearer fixture-token":
            raise HTTPException(401, "authentication_required")
    app = FastAPI()
    app.include_router(autonomous_router(lambda: manager, authenticated))
    try:
        with pytest.raises(ValueError, match="autonomy_already_owned"):
            make()
        for settings in ({"worker_capacity": True}, {"worker_capacity": 99}, {"batch_size": 100},
                {"director_budget_usd": "NaN"}, {"site_url": "https://127.0.0.1"},
                {"director_budget_usd": "25.01"}, {"director_max_input_tokens": 8193},
                {"director_max_output_tokens": 2049}, {"director_max_calls": 513},
                {"unknown": 1}, {"director_enabled": True}, {"schedule_enabled": 1}):
            with pytest.raises(ValueError):
                manager.configure(**settings)
        client = TestClient(app)
        assert client.get("/api/v1/companion/autonomy/status").status_code == 401
        headers = {"Authorization": "Bearer fixture-token"}
        assert client.get("/api/v1/companion/autonomy/status", headers=headers).json()["enabled"] is False
        assert client.put("/api/v1/companion/autonomy/config", headers=headers, json={"enabled": "yes"}).status_code == 422
        assert client.put("/api/v1/companion/autonomy/config", headers=headers, json={"worker_capacity": 99}).status_code == 409
        created = client.post("/api/v1/companion/autonomy/cycles", headers=headers, json={})
        assert created.status_code == 200
        assert client.post("/api/v1/companion/autonomy/cycles", headers=headers, json={}).status_code == 409
        assert client.get("/api/v1/companion/autonomy/cycles", headers=headers).json()["items"]
        assert client.get("/api/v1/companion/autonomy/cycles/not-an-id", headers=headers).status_code == 404
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_html_http_failure_is_preserved_after_category_pages_and_cannot_start(managers):
    make, _ = managers
    categories = [{"id": i, "name": "Category " + str(i), "slug": "standard-print" if i == 1 else "category-" + str(i), "parent": 0}
                  for i in range(1, 701)]
    catalog = Catalog([product(1)], categories=categories)
    failed_body = b"<html>HTTP500 private-diagnostic-marker; no JSON available</html>"
    async def failed_products(url):
        if urlsplit(url).path.endswith("/products"):
            catalog.calls.append(url)
            return 500, url, failed_body, {"Content-Type": "text/html"}
        return await catalog(url)
    manager, _ = make(fetch=failed_products)
    try:
        result = await manager.inventory(manager.create_cycle()["id"])
        assert result["status"] == "inventory_failed"
        assert result["inventory"]["complete"] is False
        assert result["inventory"]["reason"] == "catalog_page_http_500"
        receipts = result["inventory"]["evidence_pages"]
        assert result["inventory"]["response_pages"] == len(receipts) == 9
        assert len([row for row in receipts if row["resource"] == "products/categories"]) == 8
        failed = receipts[-1]
        assert failed["resource"] == "products" and failed["page"] == 1 and failed["status"] == 500
        path = manager.root / failed["path"]
        assert path.read_bytes() == failed_body
        assert path.stat().st_mode & 0o777 == 0o600
        assert hashlib.sha256(failed_body).hexdigest() == failed["sha256"]
        assert "private-diagnostic-marker" not in json.dumps(result)
        assert result["products"] == [] and result["summary"]["verified"] == 0
        with pytest.raises(ValueError, match="complete_inventory_required"):
            await manager.start(result["id"])
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_body", [b"<html>not JSON even with HTTP200</html>", b"\xff\xfeinvalid-encoding"])
async def test_http200_malformed_json_is_explicit_and_keeps_private_evidence(managers, failed_body):
    make, _ = managers
    catalog = Catalog([product(1)])
    async def malformed(url):
        if urlsplit(url).path.endswith("/products"):
            return 200, url, failed_body
        return await catalog(url)
    manager, _ = make(fetch=malformed)
    try:
        result = await manager.inventory(manager.create_cycle()["id"])
        assert result["status"] == "inventory_failed" and result["inventory"]["complete"] is False
        assert result["inventory"]["reason"] == "catalog_page_invalid_json"
        receipt = result["inventory"]["evidence_pages"][-1]
        assert receipt["status"] == 200
        assert (manager.root / receipt["path"]).read_bytes() == failed_body
        with pytest.raises(ValueError, match="complete_inventory_required"):
            await manager.start(result["id"])
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_page,expected", [(True, "catalog_page_http_400"), (False, None)])
async def test_invalid_page_json_is_end_only_after_successful_prior_page(managers, first_page, expected):
    make, _ = managers
    catalog = Catalog([product(1)])
    async def end_page(url):
        if urlsplit(url).path.endswith("/products"):
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            if first_page or page > 1:
                return 400, url, b'{"code":"rest_post_invalid_page_number"}'
        return await catalog(url)
    manager, _ = make(fetch=end_page)
    try:
        result = await manager.inventory(manager.create_cycle()["id"])
        if expected:
            assert result["status"] == "inventory_failed" and result["inventory"]["reason"] == expected
            assert result["inventory"]["complete"] is False
        else:
            assert result["inventory"]["complete"] is True
            assert result["inventory"]["count"] == 1
        assert result["inventory"]["evidence_pages"][-1]["status"] == 400
    finally:
        await manager.close()
