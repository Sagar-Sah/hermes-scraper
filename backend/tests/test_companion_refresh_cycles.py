"""Monthly child-table cache identities, using an entirely local fake collector."""
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

import crawler.product_companion_service as module
from crawler.product_companion_service import ProductCompanionService


SOURCE = "https://4over.com/brown-kraft-business-cards"
SITE = "https://printitprints.com/shop/brown-kraft/"


class Collector:
    def __init__(self, option_map):
        self.option_map = option_map
        self.calls = []
        self.values = {}
        self.fail = False
    async def collect(self, snapshot_id, source_url, children, **kwargs):
        self.calls.append((snapshot_id, tuple(children)))
        await kwargs["check"]()
        if self.fail:
            raise TimeoutError("Unknown source outcome")
        self.values[snapshot_id] = {
            "source_url": SOURCE, "status": "completed_requested_subset",
            "scope": {"sets": 1, "job_sample": False, "digital_proof": False},
            "declared_children": [{"child_id": "1"}],
            "option_contract": {"binding_complete": True, "rows": deepcopy(self.option_map["rows"])},
            "observations": [{"child_id": "1", "status": "collected", "source_url": SOURCE,
                "scope": {"sets": 1, "job_sample": False, "digital_proof": False},
                "tier_prices": [], "package_prices": [{"quantity": 100, "price": "10.00"}]}],
        }
        return self.values[snapshot_id]
    def get(self, identifier, url):
        return deepcopy(self.values[identifier])


def service(tmp_path, collector=None):
    raw = ('Size,Quantity,Supplier Table Price,Source URL\n'
           '2 x3.5,100,10.00,' + SOURCE + '\n').encode()
    mapping = {"source_url": SOURCE, "complete_child_mapping": True,
        "rows": [{"child_id": "1", "fields": [{"label": "Size", "value": "10", "choice": "2 x3.5"}]}]}
    encoded_map = json.dumps(mapping).encode()
    evidence = {hashlib.sha256(raw).hexdigest(): raw, hashlib.sha256(encoded_map).hexdigest(): encoded_map}
    product = {"id": "9532", "title": "Brown Kraft", "stage": "finalized", "supplier": "4over", "supplier_url": SOURCE,
        "storefront_url": SITE, "fourover_rows_evidence": {"sha256": hashlib.sha256(raw).hexdigest()},
        "fourover_option_map_evidence": {"sha256": hashlib.sha256(encoded_map).hexdigest()}}
    campaigns = SimpleNamespace(get=lambda identifier: {"id": "campaign", "products": [product]})
    value = ProductCompanionService(SimpleNamespace(root=tmp_path), campaigns, browser_pool=object())
    value._evidence = lambda info: evidence[info["sha256"]]
    value._child_collector = collector or Collector(mapping)
    value.add_pair({"product_id": "9532", "campaign_id": "campaign", "source_url": SOURCE, "storefront_url": SITE})
    return value, value._child_collector


@pytest.mark.asyncio
async def test_same_cycle_reuses_receipt_and_new_cycle_observes_fresh_snapshot(tmp_path):
    value, collector = service(tmp_path)
    first = await value.refresh_child_tables("woo-9532", cycle_id="a" * 32)
    cached = await value.refresh_child_tables("woo-9532", cycle_id="a" * 32)
    assert len(collector.calls) == 1
    assert first["snapshot_id"] == cached["snapshot_id"]
    next_month = await value.refresh_child_tables("woo-9532", cycle_id="b" * 32)
    assert len(collector.calls) == 2
    assert next_month["snapshot_id"] != first["snapshot_id"]
    assert value.pairs["woo-9532"]["fresh_source_cycle_id"] == "b" * 32


@pytest.mark.asyncio
async def test_same_cycle_success_survives_service_restart_without_source_request(tmp_path):
    value, collector = service(tmp_path)
    first = await value.refresh_child_tables("woo-9532", cycle_id="a" * 32)
    reopened, _ = service(tmp_path, collector=collector)
    cached = await reopened.refresh_child_tables("woo-9532", cycle_id="a" * 32)
    assert cached["snapshot_id"] == first["snapshot_id"]
    assert len(collector.calls) == 1


@pytest.mark.asyncio
async def test_uncertain_failed_dispatch_is_not_repeated_in_same_cycle_after_restart(tmp_path):
    value, collector = service(tmp_path)
    collector.fail = True
    with pytest.raises(Exception):
        await value.refresh_child_tables("woo-9532", cycle_id="a" * 32)
    assert len(collector.calls) == 1
    reopened, _ = service(tmp_path, collector=collector)
    try:
        await reopened.refresh_child_tables("woo-9532", cycle_id="a" * 32)
    except Exception:
        pass
    assert len(collector.calls) == 1
    collector.fail = False
    await reopened.refresh_child_tables("woo-9532", cycle_id="b" * 32)
    assert len(collector.calls) == 2


@pytest.mark.asyncio
async def test_same_cycle_expired_receipt_is_not_silently_requoted(tmp_path, monkeypatch):
    now = [1793491200.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    value, collector = service(tmp_path)
    first = await value.refresh_child_tables("woo-9532", cycle_id="a" * 32)
    now[0] += 7 * 3600
    try:
        current = await value.refresh_child_tables("woo-9532", cycle_id="a" * 32)
    except ValueError:
        current = None  # An explicit stale-cycle hold is also a safe outcome.
    assert len(collector.calls) == 1
    if current:
        assert current["snapshot_id"] == first["snapshot_id"]
    next_cycle = await value.refresh_child_tables("woo-9532", cycle_id="b" * 32)
    assert next_cycle["snapshot_id"] != first["snapshot_id"]
