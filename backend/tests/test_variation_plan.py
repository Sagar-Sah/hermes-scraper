"""Offline public plans and authenticated, complete, provisional downloads."""

import asyncio
import csv
from copy import deepcopy
import io

import httpx
import pytest

import crawler.app as api
import crawler.variation_plan as plans
from crawler.config import Settings
from crawler.store import Product, Run, Store
from crawler.variation_matrix import VariationMatrix
from test_variation_matrix import inventory as matrix_inventory


@pytest.fixture
def store(tmp_path):
    result = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    with result.session() as session:
        session.add_all([Run(id="run-a", name="A"), Run(id="run-b", name="B"),
                         Product(id="product-a", run_id="run-a", url="https://fixture.test/a"),
                         Product(id="product-b", run_id="run-b", url="https://fixture.test/b")])
    yield result
    result.engine.dispose()


def inventory():
    value = matrix_inventory()
    value["bases"][0]["base_prices"] = None
    value["bases"][0]["table_evidence"] = {
        "catalog": "catalog-evidence", "base_prices": None,
        "base_support": "quantity-evidence", "option_support": ["option-support-evidence"], "option_prices": {},
    }
    value.update(product_id="product-a", title="Cards", product_url="https://fixture.test/a",
                 specification={"sizes": [{"id": 101, "label": "3.5 x 2"}],
                                "papers": [{"id": 1, "label": "16 pt."}],
                                "colors": [{"id": 2, "label": "Color front, B&W back"}],
                                "turnarounds": [{"id": 1, "label": "Regular"}]})
    return value


def test_public_plan_keeps_counts_and_unknowns_without_private_context(store, monkeypatch):
    value = inventory()
    value["context"]["private_marker"] = "PRIVATE-SESSION-DATA"
    value["bases"][0]["lookup"]["request"]["context"] = deepcopy(value["context"])
    monkeypatch.setattr(plans, "load_inventories", lambda store, run: [value])
    result = plans.build_plan(store, "run-a")
    public = result["products"][0]
    report = VariationMatrix(value).report()
    assert public["counts"]["candidate_rows"] == report["candidate_rows"] == 8
    assert public["size_plans"][0]["candidate_rows"] == 8
    assert public["counts"]["valid_rows"] is public["counts"]["priced_rows"] is None
    assert public["size_plans"][0]["priced_rows"] is None
    assert result["supplier_requests_made"] == 0 and not public["catalog_complete"]
    assert "PRIVATE-SESSION-DATA" not in str(result)


def test_no_inventory_does_not_become_zero_complete():
    public = plans.public_product_plan({"product_id": "p", "title": "Awaiting", "product_url": "url",
                                       "product_type": None, "specification": {}, "bases": []})
    assert public["status"] == "awaiting_inventory"
    assert public["counts"] == {"candidate_rows": None, "valid_rows": None, "priced_rows": None}
    assert not public["catalog_complete"]


def test_export_streams_all_rows_with_pending_status_and_no_ids(store, monkeypatch):
    value = inventory()
    value["title"] = " \n=HYPERLINK(\"https://untrusted.test\")"
    monkeypatch.setattr(plans, "load_inventories", lambda store, run: [value])
    class FiniteMatrix:
        def __init__(self, value): pass
        def iter_rows(self):
            for index in range(2057):
                yield {"quantity": index + 1, "base_product_id": "PRIVATE-SUPPLIER-ID", "evidence_ids": ["PRIVATE-EVIDENCE-ID"],
                    "selection": {"productionTime": "1", "proofType": "31", "color": "2"},
                    "configuration": {"size": {"value": "PRIVATE-OPTION-ID", "label": "  @untrusted"},
                                      "proofType": {"value": "PRIVATE-SERVICE-ID", "label": "Instant"}},
                    "table_subtotal": None if index == 0 else "0.00" if index == 1 else "3.67"}
    monkeypatch.setattr(plans, "VariationMatrix", FiniteMatrix)
    path, count = plans.export_candidate_rows(store, "run-a", "product-a")
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert count == len(rows) == 2057 and rows[-1]["Quantity"] == "2057"
    assert "PRIVATE-" not in text and not any(key.endswith("_json") for key in rows[0])
    assert rows[0]["Product"].startswith("'") and rows[0]["Size"].startswith("'")
    amount = "Calculated component subtotal (USD, before coupons)"
    assert rows[0][amount] == "" and rows[1][amount] == "0.00"
    unit = 'Calculated component unit price (USD, before coupons)'
    assert rows[0][unit] == '' and rows[1][unit] == '0.00000000'
    assert rows[2][unit] == '1.22333333'
    assert rows[0]["Price verification"].startswith("Unavailable")
    assert rows[-1]["Price verification"] == "Calculated; verification pending"
    assert all("pending" in row["Availability"] for row in rows)


@pytest.mark.parametrize('product_type', ['27', '36'])
def test_export_other_products_keeps_shape_finish_wind_and_source(store, monkeypatch, product_type):
    value = inventory()
    value['product_type'] = product_type
    value['product_url'] = 'https://www.printograph.com/products/roll-labels/order'
    monkeypatch.setattr(plans, 'load_inventories', lambda store, run: [value])
    class FiniteMatrix:
        def __init__(self, value): pass
        def iter_rows(self):
            yield {'quantity': 100, 'selection': {'productionTime':'1','proofType':'31','color':'1'},
                   'configuration': {'shape':{'value':'8','label':'Oval'},
                                     'finish':{'value':'2','label':'Matte'},
                                     'windPosition':{'value':'54','label':'Not Important'},
                                     'proofType':{'value':'31','label':'Online'}},
                   'table_subtotal':'12.34'}
    monkeypatch.setattr(plans, 'VariationMatrix', FiniteMatrix)
    path, count = plans.export_candidate_rows(store, 'run-a', 'product-a')
    with path.open(encoding='utf-8-sig') as stream:
        rows = list(csv.DictReader(stream))
    assert count == 1
    assert (rows[0]['Shape'], rows[0]['Finish'], rows[0]['Wind position']) == ('Oval','Matte','Not Important')
    assert rows[0]['Source URL'] == value['product_url']
    assert rows[0]['Price verification'] == 'Calculated; verification pending'


@pytest.mark.asyncio
async def test_cache_deduplicates_concurrent_builds_and_invalidates_revision(store, monkeypatch):
    cache = plans.VariationPlanCache(store)
    revision = [1]
    calls = []
    monkeypatch.setattr(cache, "_revision", lambda run: tuple(revision))
    def build(store, run):
        calls.append(run)
        return {"products": [], "generation": len(calls)}
    monkeypatch.setattr(plans, "build_plan", build)
    first, second = await asyncio.gather(cache.get("run-a"), cache.get("run-a"))
    assert first == second and calls == ["run-a"]
    revision[0] = 2
    assert (await cache.get("run-a"))["generation"] == 2


@pytest.mark.asyncio
async def test_api_auth_scope_and_download_are_preserved(store, monkeypatch):
    value = inventory()
    monkeypatch.setattr(plans, "load_inventories", lambda store, run: [value] if run == "run-a" else [])
    monkeypatch.setattr(api.app.state, "variation_plans", plans.VariationPlanCache(store), raising=False)
    monkeypatch.setattr(api.settings, "api_token", "plan-fixture-token")
    headers = {"Authorization": "Bearer plan-fixture-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
        plan = "/api/v1/runs/run-a/variation-plan"
        export = plan + "/export?product_id=product-a"
        assert (await client.get(plan)).status_code == 401
        assert (await client.get(export)).status_code == 401
        assert (await client.get(plan, headers=headers)).json()["products"][0]["counts"]["candidate_rows"] == 8
        download = await client.get(export, headers=headers)
        assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
        assert download.headers["x-hermes-candidate-rows"] == "8"
        assert len(list(csv.DictReader(io.StringIO(download.content.decode("utf-8-sig"))))) == 8
        assert (await client.get("/api/v1/runs/run-b/variation-plan/export?product_id=product-a", headers=headers)).status_code == 404
        assert (await client.get("/api/v1/runs/absent/variation-plan", headers=headers)).status_code == 404
        assert (await client.get("/api/v1/runs/run-a/variation-plan/export?product_id=../../secret", headers=headers)).status_code == 404
