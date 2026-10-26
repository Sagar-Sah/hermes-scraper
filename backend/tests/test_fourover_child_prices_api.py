"""Offline boundary tests for the bounded source-child collection route."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import crawler.app as api
import crawler.fourover_child_prices as collection


CAMPAIGN = "bounded-fixture"
PRODUCT = "10359"
SOURCE = "https://4over.com/z-fold-brochures"
SNAPSHOT = "a" * 32
PATH = f"/api/v1/campaigns/{CAMPAIGN}/fourover-child-prices/{PRODUCT}"
HEADERS = {"Authorization": "Bearer fixture-token"}


def body(**changes):
    return {
        "snapshot_id": SNAPSHOT,
        "child_ids": ["5296", "5294"],
        "request_budget": 3,
        "sets": 1,
        "job_sample": False,
        "digital_proof": False,
        **changes,
    }


class Campaigns:
    def __init__(self):
        self.product = {
            "id": PRODUCT, "supplier": "fourover", "supplier_url": SOURCE,
            "stage": "mapped",
        }
        self.active = {PRODUCT}
        self.missing = False
        self.calls = []

    def require_active_group(self, campaign_id, product_id):
        self.calls.append((campaign_id, product_id))
        if self.missing or campaign_id != CAMPAIGN:
            raise FileNotFoundError(campaign_id)
        if product_id not in self.active:
            raise ValueError("Finish the current product group before starting another")
        return {"products": [deepcopy(self.product)] if self.product else []}


@pytest.fixture
def scoped_api(monkeypatch):
    campaigns = Campaigns()
    result = {
        "snapshot_id": SNAPSHOT, "source_url": SOURCE,
        "requested_child_ids": ["5296", "5294"],
        "declared_children": [{"child_id": "5296", "sku": "SOURCE-A"}],
        "observations": [], "requests_this_call": 3,
        "scope": {"sets": 1, "job_sample": False, "digital_proof": False},
        "supplier_total_verified": False, "services_verified": False,
        "catalog_complete": False, "final_eligible": False,
    }
    collect = AsyncMock(return_value=result)
    accessor = Mock(return_value=SimpleNamespace(collect=collect))
    monkeypatch.setattr(api.app.state, "campaigns", campaigns, raising=False)
    monkeypatch.setattr(api.settings, "api_token", "fixture-token")
    monkeypatch.setattr(api, "fourover_child_price_collector", accessor)
    legacy = []
    for name in (
        "harvest_fourover_price_tables", "fourover_product_probe",
        "save_fourover_default_probe", "harvest_fourover_option_map",
        "materialize_fourover_draft", "fourover_shipping_pilot",
    ):
        helper = AsyncMock(side_effect=AssertionError("Legacy collection must not run"))
        monkeypatch.setattr(api, name, helper)
        legacy.append(helper)
    yield SimpleNamespace(campaigns=campaigns, collect=collect, accessor=accessor,
                          result=result, legacy=legacy)
    assert all(helper.call_count == 0 for helper in legacy)


async def post(payload, *, path=PATH, headers=HEADERS):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="http://fixture"
    ) as client:
        return await client.post(path, json=payload, headers=headers)


@pytest.mark.asyncio
async def test_child_collection_requires_authentication_before_campaign_or_collector(scoped_api):
    response = await post(body(), headers={})
    assert response.status_code == 401
    assert scoped_api.campaigns.calls == []
    scoped_api.accessor.assert_not_called()
    scoped_api.collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_forwards_only_exact_mapped_source_snapshot_children_budget_and_scope(scoped_api):
    before = deepcopy(scoped_api.campaigns.product)
    response = await post(body())
    assert response.status_code == 200
    scoped_api.collect.assert_awaited_once()
    call = scoped_api.collect.await_args
    assert call.args == (SNAPSHOT, SOURCE, ["5296", "5294"])
    assert call.kwargs["request_budget"] == 3
    assert call.kwargs["scope"] == {"sets": 1, "job_sample": False, "digital_proof": False}
    assert callable(call.kwargs["check"])
    result = response.json()
    assert result == scoped_api.result
    assert all(result[key] is False for key in (
        "supplier_total_verified", "services_verified", "catalog_complete", "final_eligible"))
    assert scoped_api.campaigns.product == before
    assert not any(key in result for key in ("finalized_at", "source_data_complete", "retail_price"))


@pytest.mark.asyncio
async def test_empty_children_reaches_discovery_only_with_document_budget(scoped_api):
    scoped_api.collect.return_value = {
        **scoped_api.result, "requested_child_ids": [], "requests_this_call": 1,
    }
    response = await post(body(child_ids=[], request_budget=1))
    assert response.status_code == 200
    call = scoped_api.collect.await_args
    assert call.args == (SNAPSHOT, SOURCE, [])
    assert call.kwargs["request_budget"] == 1
    assert response.json()["final_eligible"] is False


@pytest.mark.asyncio
async def test_maximum_explicit_subset_and_budget_are_forwarded_without_expansion(scoped_api):
    children = [str(5290 + index) for index in range(10)]
    response = await post(body(child_ids=children, request_budget=11))
    assert response.status_code == 200
    call = scoped_api.collect.await_args
    assert call.args == (SNAPSHOT, SOURCE, children)
    assert call.kwargs["request_budget"] == 11


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation, expected", [
    (lambda state: setattr(state.campaigns, "missing", True), 404),
    (lambda state: state.campaigns.active.clear(), 409),
    (lambda state: state.campaigns.product.update(supplier="b2sign"), 409),
    (lambda state: state.campaigns.product.update(stage="excluded"), 409),
    (lambda state: setattr(state.campaigns, "product", None), 409),
])
async def test_missing_inactive_or_wrong_mapping_stops_before_collector(scoped_api, mutation, expected):
    mutation(scoped_api)
    response = await post(body())
    assert response.status_code == expected
    scoped_api.accessor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    None, "", "http://4over.com/z-fold-brochures",
    "https://4over.com.evil.test/z-fold-brochures", "https://example.test/z-fold-brochures",
    "https://user:secret@4over.com/z-fold-brochures", "https://4over.com:444/z-fold-brochures",
    SOURCE + "?child=5296", SOURCE + "#child=5296",
    "https://4over.com/rest/default/V1/product/prices/5296", "https://4over.com/customer",
    " " + SOURCE, SOURCE + " ",
])
async def test_invalid_mapped_source_is_rejected_before_collector(scoped_api, source):
    scoped_api.campaigns.product["supplier_url"] = source
    response = await post(body())
    assert response.status_code == 422
    scoped_api.accessor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", [
    "snapshot_id", "child_ids", "request_budget", "sets", "job_sample", "digital_proof",
])
async def test_explicit_snapshot_budget_and_scope_fields_are_required(scoped_api, field):
    payload = body()
    del payload[field]
    assert (await post(payload)).status_code == 422
    scoped_api.accessor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"snapshot_id": "a" * 31}, {"snapshot_id": "A" * 32},
    {"snapshot_id": "../" + "a" * 32}, {"snapshot_id": 123},
    {"child_ids": [5296]}, {"child_ids": [True]}, {"child_ids": None},
    {"child_ids": [str(index + 1) for index in range(11)]},
    {"request_budget": 0}, {"request_budget": 12}, {"request_budget": True},
    {"request_budget": 3.0}, {"request_budget": "3"},
    {"sets": 0}, {"sets": 2}, {"sets": True}, {"sets": "1"},
    {"job_sample": "false"}, {"digital_proof": 0},
    {"source_url": "https://4over.com/another-product"},
    {"final_eligible": True}, {"finalized_at": "injected"},
])
async def test_strict_bounded_model_and_extra_fields_fail_before_collector(scoped_api, changes):
    response = await post(body(**changes))
    assert response.status_code == 422
    scoped_api.accessor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["job_sample", "digital_proof"])
async def test_enabled_service_scope_is_not_silently_ignored(scoped_api, flag):
    response = await post(body(**{flag: True}))
    assert response.status_code == 422
    scoped_api.accessor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("children", [
    ["5296", "5296"], [""], ["0"], ["-1"], ["1.0"], [" 5296"],
    ["5296/path"], ["1" * 14], ["٥٢٩٦"],
])
async def test_bad_child_ids_reach_module_guard_but_never_browser_or_dns(
    scoped_api, monkeypatch, tmp_path, children
):
    class NoBrowser:
        def page(self, *args, **kwargs):
            raise AssertionError("Invalid child IDs must not open a source browser")

    validate = AsyncMock(side_effect=AssertionError("Invalid child IDs must not resolve a source"))
    monkeypatch.setattr(collection, "validate_url", validate)
    collector = collection.FourOverChildPriceCollector(SimpleNamespace(root=tmp_path), NoBrowser())
    scoped_api.accessor.return_value = collector
    response = await post(body(child_ids=children))
    assert response.status_code == 422
    validate.assert_not_awaited()
    assert not (tmp_path / "fourover-child-prices").exists()


@pytest.mark.asyncio
async def test_module_validation_error_is_a_client_error_with_no_legacy_fallback(scoped_api):
    scoped_api.collect.side_effect = ValueError("Child price checkpoint context mismatch")
    response = await post(body())
    assert response.status_code == 422
    assert response.json()["detail"] == "Child price checkpoint context mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("change, expected", [
    ("inactive", 409), ("excluded", 422), ("supplier", 422), ("source", 422),
])
async def test_scope_callback_stops_a_midcall_campaign_or_source_change(scoped_api, change, expected):
    async def changed_scope(*args, check, **kwargs):
        await check()
        if change == "inactive":
            scoped_api.campaigns.active.clear()
        elif change == "excluded":
            scoped_api.campaigns.product["stage"] = "excluded"
        elif change == "supplier":
            scoped_api.campaigns.product["supplier"] = "b2sign"
        else:
            scoped_api.campaigns.product["supplier_url"] = "https://4over.com/other-product"
        await check()
        raise AssertionError("Collection must stop after a scope change")

    scoped_api.collect.side_effect = changed_scope
    response = await post(body())
    assert response.status_code == expected
