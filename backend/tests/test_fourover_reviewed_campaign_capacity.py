"""Large declaration attachment/join API tests with synthetic local evidence only."""
import hashlib
import json

import httpx
import pytest

from crawler.campaign_progress import CampaignManager
from crawler.config import Settings
from crawler.fourover_rows import package_rows
from crawler.store import Store
import crawler.app as api


URLS = ("https://4over.com/16pt-postcards", "https://4over.com/specialty-folds-brochures")
HEADERS = {"Authorization": "Bearer capacity-fixture"}


def source_rows(count):
    # Invented data exercises cardinality/integrity; these are not supplier quotes.
    children = [{"child_id": str(n), "sku": "FIXTURE-" + str(n)} for n in range(1, count + 1)]
    prices = [{"child_id": row["child_id"], "status": "priced", "package_prices": [
        {"quantity": 100, "price": "12.340001"}], "tier_prices": []} for row in children]
    mappings = [{"child_id": row["child_id"], "fields": [
        {"label": "Size", "choice": "Source option " + row["child_id"]}]} for row in children]
    return children, prices, mappings


@pytest.fixture
def manager(tmp_path):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    try:
        yield CampaignManager(store)
    finally:
        store.engine.dispose()


def campaign(manager, url):
    return manager.create(id="capacity-fixture", name="Local capacity fixture", products=[{
        "id": "9866", "title": "Synthetic fixture", "storefront_url": "https://printitprints.com/shop/fixture/",
        "supplier": "fourover", "supplier_url": url}])


@pytest.mark.parametrize("url,count", [(URLS[0], 2314), (URLS[1], 2901), (URLS[0], 3000)])
def test_complete_large_attachments_join_without_promoting_source_review(manager, url, count):
    c = campaign(manager, url)
    children, prices, mappings = source_rows(count)
    evidence = manager.attach_fourover_price_tables(c["id"], "9866", url, children, prices)
    assert evidence["complete_child_routes"] and evidence["child_count"] == count
    assert evidence["final_eligible"] is False
    attached = manager.attach_fourover_option_map(c["id"], "9866", url, mappings)
    assert attached["complete_child_mapping"] and attached["mapped_children"] == count
    assert attached["final_eligible"] is False
    p = manager.get(c["id"])["products"][0]
    table = json.loads(manager.store.get_object(p["fourover_price_table_evidence"]["object_path"]))
    mapping = json.loads(manager.store.get_object(p["fourover_option_map_evidence"]["object_path"]))
    rows, _ = package_rows("Synthetic fixture", url, mapping, table)
    assert len(rows) == count
    assert rows[-1]["Supplier Table Price"] == "12.340001"
    assert all("Pending" in row["Verification"] for row in rows)
    assert p["stage"] == "queued" and p["finalization_id"] is None
    assert not p.get("verified_at") and not p.get("finalized_at")


@pytest.mark.parametrize("url,count", [(URLS[0], 3001), (URLS[1], 3001),
    ("https://4over.com/all-inclusive-postcards", 2001), ("https://4over.com/z-fold-brochures", 2001)])
def test_larger_counts_and_unreviewed_families_cannot_attach(manager, url, count):
    c = campaign(manager, url)
    children, prices, _ = source_rows(count)
    with pytest.raises(ValueError, match="mapped 4over"):
        manager.attach_fourover_price_tables(c["id"], "9866", url, children, prices)
    assert not manager.get(c["id"])["products"][0].get("fourover_price_table_evidence")


def test_large_attachments_retain_duplicate_source_and_evidence_hash_guards(manager):
    url = URLS[0]
    c = campaign(manager, url)
    children, prices, mappings = source_rows(2314)
    with pytest.raises(ValueError, match="Duplicate or unexpected"):
        manager.attach_fourover_price_tables(c["id"], "9866", url, children, prices + [prices[0]])
    with pytest.raises(ValueError, match="mapped 4over"):
        manager.attach_fourover_price_tables(c["id"], "9866", URLS[1], children, prices)
    manager.attach_fourover_price_tables(c["id"], "9866", url, children, prices)
    with pytest.raises(ValueError, match="Duplicate or unexpected"):
        manager.attach_fourover_option_map(c["id"], "9866", url, mappings + [mappings[0]])
    with pytest.raises(ValueError, match="complete source"):
        manager.attach_fourover_option_map(c["id"], "9866", url, source_rows(3001)[2])
    p = manager.get(c["id"])["products"][0]
    path = manager.store.root / p["fourover_price_table_evidence"]["object_path"]
    path.write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        manager.attach_fourover_option_map(c["id"], "9866", url, mappings)


@pytest.mark.asyncio
@pytest.mark.parametrize("url,count", [(URLS[0], 2314), (URLS[1], 2901)])
async def test_existing_draft_api_joins_large_evidence_but_finalizer_still_requires_native_review(manager, monkeypatch, url, count):
    c = campaign(manager, url)
    children, prices, mappings = source_rows(count)
    manager.attach_fourover_price_tables(c["id"], "9866", url, children, prices)
    manager.attach_fourover_option_map(c["id"], "9866", url, mappings)
    public = {"requested_url": url, "public_data": {
        "product_content": {"source_url": url, "images": [{"url": "https://4over.com/fixture.jpg"}]},
        "observed_specifications": [{"label": "Turnaround Time", "values": ["Next Business Day"]}]}}
    encoded = json.dumps(public).encode()
    path = manager.store.put_object(encoded, "public-source.json")
    p = manager.get(c["id"])
    p["products"][0].update(source_collection_status="page_collected", source_collection_evidence={
        "object_path": path, "sha256": hashlib.sha256(encoded).hexdigest()})
    manager._write(p)
    monkeypatch.setattr(api.app.state, "campaigns", manager, raising=False)
    monkeypatch.setattr(api, "store", manager.store)
    monkeypatch.setattr(api.settings, "api_token", "capacity-fixture")
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        endpoint = f"/api/v1/campaigns/{c['id']}/fourover-draft/9866"
        assert (await client.post(endpoint)).status_code == 401
        draft = await client.post(endpoint, headers=HEADERS)
        assert draft.status_code == 200 and draft.json()["row_count"] == count
        before = manager.get(c["id"])["products"][0]
        assert before["stage"] == "extracted" and not before.get("verified_at")
        assert before["fourover_rows_evidence"]["final_eligible"] is False
        review = await client.post(f"/api/v1/campaigns/{c['id']}/fourover-one-set-review/9866",
            headers=HEADERS, json={"sets": 1, "job_sample": False, "digital_proof": False})
        assert review.status_code == 409 and "diagnosis" in review.json()["detail"]
        after = manager.get(c["id"])["products"][0]
        assert after["stage"] == "extracted" and after["finalization_id"] is None
        assert not after.get("verified_at") and not after.get("finalized_at")
