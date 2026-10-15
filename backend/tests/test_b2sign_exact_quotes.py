"""Exact quote reuse must preserve source selection and never imply availability."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

import crawler.app as api
from crawler.b2sign_catalog import PINNED_BUNDLE_SHA256, enumerate_catalog, parse_public_product
from crawler.b2sign_exact_quotes import B2SignExactQuoteManager
from crawler.b2sign_harvest import prepare_candidate, complete_custom_grommet_value, complete_custom_hole_value, complete_custom_polepocket_value
from crawler.b2sign_harvest import HarvestError
from test_b2sign_catalog import simple, select


URL = "https://www.b2sign.com/test-open-size"
COLLECTION = "a" * 32


@pytest.mark.parametrize('geometry,accepted', [
    ('TE12,BN,LN,RN', True),
    ('TC,BS,LE6,RN', True),
    ('TE3,BN,LN,RN', False),
    ('TE999,BN,LN,RN', False),
    ('TN,BN,LN', False),
])
def test_custom_grommet_requires_complete_native_edge_geometry(geometry, accepted):
    field={'key':'grommet','options':[{'key':'custom','component':'custom',
           'component_props':{'type':'grommet','size_key':'_size'}}]}
    scope={'_size':'24x36','grommet':'custom','grommet_CUSTOM_VALUE':geometry}
    assert complete_custom_grommet_value(field,'custom',scope) is accepted


def test_pvc_custom_grommet_uses_its_own_geometry_key():
    field={'key':'grommet','options':[{'key':'custom_grommets','component':'custom',
           'component_props':{'type':'hole','is_grommet':True,'size_key':['_size']}}]}
    scope={'_size':'24x36','grommet':'custom_grommets',
           'grommet_CUSTOM_VALUE':'TM1,BMS,G'}
    assert complete_custom_hole_value(field,'custom_grommets',scope)
    assert not complete_custom_hole_value(field,'custom_grommets',dict(scope,grommet_CUSTOM_VALUE='TM1,BMS,P'))


@pytest.mark.parametrize('geometry,size,accepted', [
    ('T2,B0,L0,R0', '24x36', True),
    ('T1.5,B2,L0,R0', '24x36', True),
    ('T2,B0,L0,R0', '12x1', False),
    ('T4,B0,L0,R0', '24x36', False),
    ('T2,B0,R0', '24x36', False),
])
def test_custom_polepocket_requires_pinned_four_edge_sizes(geometry,size,accepted):
    field={'key':'polepocket','options':[{'key':'custom','component':'custom',
           'component_props':{'type':'polepocket','size_key':'_size'}}]}
    scope={'_size':size,'polepocket':'custom','polepocket_CUSTOM_VALUE':geometry}
    assert complete_custom_polepocket_value(field,'custom',scope) is accepted


def item():
    source = simple([{"key": "_size", "component": "size", "options": [
        {"key": "width"}, {"key": "height"}], "component_props": {"max": [
        {"match": [], "value": "100x50"}]}}, select("finish", ["plain", "gloss"])],
        production=[{"match": [{"key": "_qty", "op": ">", "value": 0}],
                     "options": [{"key": "v1", "label": "Next Day"}]}])
    source.update(id=999, url="/test-open-size", title="Test Open Size")
    return source


def candidate():
    catalog = parse_public_product(item(), source_url=URL, source_bundle_sha256=PINNED_BUNDLE_SHA256)
    rows = list(enumerate_catalog(catalog, quantities=[1], explicit_sizes={"_size": ["24x36"]}))
    return catalog, rows[0]


@pytest.mark.asyncio
async def test_step_repeat_low_quantity_bootstraps_same_selection(tmp_path):
    catalog, row = candidate()
    catalog['source_product_id'] = '18'
    row['source_product_id'] = '18'
    start = AsyncMock(return_value={'id': '4' * 32})
    manager = B2SignExactQuoteManager(SimpleNamespace(root=tmp_path),
                                      SimpleNamespace(start=start))
    result = await manager.quote(catalog, row)
    assert result['status'] == 'pending'
    planned = start.await_args.args[1]
    assert [candidate['quantity'] for candidate in planned] == [11, 1]
    assert planned[0]['values'] == planned[1]['values']


@pytest.mark.asyncio
async def test_cached_exact_quote_does_not_start_supplier_request(tmp_path):
    catalog, row = candidate()
    safe = prepare_candidate(catalog, row)
    folder = tmp_path / "b2sign-harvests" / ("b" * 32)
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"candidates": [safe]}))
    result = {"candidate_id": safe["candidate_id"], "status": "quoted",
              "captured_at": "2026-09-28T00:00:00Z", "quote": {"unit_price": "12.34", "subtotal": "12.34"}}
    harvests = SimpleNamespace(get=lambda _: {"id": "b" * 32, "status": "completed", "warnings": [],
                                              "results": [result]}, start=AsyncMock())
    manager = B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests)
    answer = await manager.quote(catalog, row)
    assert answer["status"] == "quoted" and answer["unit_price"] == "12.34"
    assert answer["native_selection_bound"] is False
    assert answer["native_size_bound"] is False
    assert answer["shipping_availability"] == "unverified" and answer["final_eligible"] is False
    harvests.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_quote_refreshes_stale_audit_price_once(tmp_path):
    catalog, row = candidate()
    safe = prepare_candidate(catalog, row)
    folder = tmp_path / 'b2sign-harvests' / ('1' * 32)
    folder.mkdir(parents=True)
    (folder / 'manifest.json').write_text(json.dumps({'candidates': [safe]}))
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    report = {'id': '1' * 32, 'status': 'completed', 'warnings': [],
              'results': [{'candidate_id': safe['candidate_id'], 'status': 'quoted',
                           'captured_at': old,
                           'quote': {'unit_price': '12.34', 'subtotal': '12.34'}}]}
    harvests = SimpleNamespace(get=lambda _: report,
                               start=AsyncMock(return_value={'id': '2' * 32}))
    manager = B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests)
    answer = await manager.quote(catalog, row, require_fresh=True)
    assert answer['status'] == 'pending' and answer['harvest_id'] == '2' * 32
    harvests.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_customer_quote_reuses_recent_supplier_price(tmp_path):
    catalog, row = candidate()
    safe = prepare_candidate(catalog, row)
    folder = tmp_path / 'b2sign-harvests' / ('3' * 32)
    folder.mkdir(parents=True)
    (folder / 'manifest.json').write_text(json.dumps({'candidates': [safe]}))
    recent = datetime.now(timezone.utc).isoformat()
    report = {'id': '3' * 32, 'status': 'completed', 'warnings': [],
              'results': [{'candidate_id': safe['candidate_id'], 'status': 'quoted',
                           'native_selection_bound': True,
                           'native_size_bound': True,
                           'captured_at': recent,
                           'quote': {'unit_price': '12.34', 'subtotal': '12.34'}}]}
    harvests = SimpleNamespace(get=lambda _: report, start=AsyncMock())
    manager = B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests)
    answer = await manager.quote(catalog, row, require_fresh=True)
    assert answer['status'] == 'quoted' and answer['customer_quote_fresh'] is True
    assert answer['native_selection_bound'] is True
    assert answer['native_size_bound'] is True
    assert 0 <= answer['price_age_seconds'] < 300
    harvests.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_quote_uses_fresh_repriced_source_without_old_price_conflict(tmp_path):
    catalog, row = candidate()
    safe = prepare_candidate(catalog, row)
    reports = {}
    for marker, age, price in [('a', timedelta(hours=1), '12.34'),
                               ('b', timedelta(seconds=5), '13.21')]:
        folder = tmp_path / 'b2sign-harvests' / (marker * 32)
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps({'candidates': [safe]}))
        reports[marker * 32] = {'id': marker * 32, 'status': 'completed', 'warnings': [],
            'results': [{'candidate_id': safe['candidate_id'], 'status': 'quoted',
                         'native_size_bound': True,
                         'captured_at': (datetime.now(timezone.utc) - age).isoformat(),
                         'quote': {'unit_price': price, 'subtotal': price}}]}
    harvests = SimpleNamespace(get=lambda hid: reports[hid], start=AsyncMock())
    answer = await B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests).quote(
        catalog, row, require_fresh=True)
    assert answer['status'] == 'quoted' and answer['unit_price'] == '13.21'
    assert answer['harvest_id'] == 'b' * 32
    harvests.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_quote_rejects_conflicting_fresh_source_prices(tmp_path):
    catalog, row = candidate()
    safe = prepare_candidate(catalog, row)
    reports = {}
    for marker, price in [('a', '12.34'), ('b', '13.21')]:
        folder = tmp_path / 'b2sign-harvests' / (marker * 32)
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps({'candidates': [safe]}))
        reports[marker * 32] = {'id': marker * 32, 'status': 'completed', 'warnings': [],
            'results': [{'candidate_id': safe['candidate_id'], 'status': 'quoted',
                         'native_size_bound': True,
                         'captured_at': datetime.now(timezone.utc).isoformat(),
                         'quote': {'unit_price': price, 'subtotal': price}}]}
    harvests = SimpleNamespace(get=lambda hid: reports[hid], start=AsyncMock())
    answer = await B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests).quote(
        catalog, row, require_fresh=True)
    assert answer['status'] == 'conflict_requires_review'
    harvests.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_open_size_quote_refreshes_recent_price_without_native_size_binding(tmp_path):
    catalog, row = candidate()
    safe = prepare_candidate(catalog, row)
    folder = tmp_path / 'b2sign-harvests' / ('5' * 32)
    folder.mkdir(parents=True)
    (folder / 'manifest.json').write_text(json.dumps({'candidates': [safe]}))
    report = {'id': '5' * 32, 'status': 'completed', 'warnings': [],
              'results': [{'candidate_id': safe['candidate_id'], 'status': 'quoted',
                           'captured_at': datetime.now(timezone.utc).isoformat(),
                           'quote': {'unit_price': '12.34', 'subtotal': '12.34'}}]}
    harvests = SimpleNamespace(get=lambda _: report,
                               start=AsyncMock(return_value={'id': '6' * 32}))
    answer = await B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests).quote(
        catalog, row, require_fresh=True)
    assert answer['status'] == 'pending' and answer['harvest_id'] == '6' * 32
    harvests.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_exact_quote_starts_one_source_manifest(tmp_path):
    catalog, row = candidate()
    harvests = SimpleNamespace(get=lambda _: None, start=AsyncMock(return_value={"id": "c" * 32}))
    manager = B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests)
    answer = await manager.quote(catalog, row)
    assert answer["status"] == "pending" and answer["final_eligible"] is False
    assert harvests.start.await_count == 1
    assert harvests.start.await_args.args[1] == [row]


@pytest.mark.asyncio
async def test_pending_exact_selection_reuses_existing_manifest(tmp_path):
    catalog, row = candidate()
    safe = prepare_candidate(catalog, row)
    folder = tmp_path / "b2sign-harvests" / ("d" * 32)
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"candidates": [safe]}))
    harvests = SimpleNamespace(get=lambda _: {"id": "d" * 32, "status": "running", "results": []},
                               start=AsyncMock())
    answer = await B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests).quote(catalog, row)
    assert answer["status"] == "pending" and answer["harvest_id"] == "d" * 32
    harvests.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_saved_prices_require_review(tmp_path):
    catalog, row = candidate()
    safe = prepare_candidate(catalog, row)
    reports = {}
    for marker, price in [("e", "12.34"), ("f", "12.35")]:
        folder = tmp_path / "b2sign-harvests" / (marker * 32)
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({"candidates": [safe]}))
        reports[marker * 32] = {"id": marker * 32, "status": "completed", "warnings": [],
                                 "results": [{"candidate_id": safe["candidate_id"], "status": "quoted",
                                              "quote": {"unit_price": price, "subtotal": price}}]}
    harvests = SimpleNamespace(get=lambda hid: reports[hid], start=AsyncMock())
    answer = await B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests).quote(catalog, row)
    assert answer["status"] == "conflict_requires_review" and answer["final_eligible"] is False
    harvests.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_quote_route_rejects_invalid_size_before_supplier_request(monkeypatch):
    source = {"pages": [{"url": URL, "status": "completed", "public_data": {"item": item()}}]}
    product = {"id": "x", "supplier": "b2sign", "stage": "blocked", "supplier_url": URL,
               "source_collection_id": COLLECTION}
    monkeypatch.setattr(api, "active_campaign_product", lambda *_: {"products": [product]})
    monkeypatch.setattr(api, "source_collector", lambda: SimpleNamespace(get=lambda *_, **__: source))
    monkeypatch.setattr(api.settings, "api_token", "")
    body = {"values": {"_size": "53x100", "finish": "plain"}, "quantity": 1, "turnaround": "v1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
        answer = await client.post("/api/v1/campaigns/c/b2sign-exact-quote/x", json=body)
    assert answer.status_code == 422


@pytest.mark.asyncio
async def test_customer_route_passes_freshness_requirement(monkeypatch):
    source = {'pages': [{'url': URL, 'status': 'completed',
                         'public_data': {'item': item()}}]}
    product = {'id': 'x', 'supplier': 'b2sign', 'stage': 'blocked',
               'supplier_url': URL, 'source_collection_id': COLLECTION}
    quote = AsyncMock(return_value={'status': 'pending', 'harvest_id': '4' * 32})
    monkeypatch.setattr(api, 'active_campaign_product', lambda *_: {'products': [product]})
    monkeypatch.setattr(api, 'source_collector',
                        lambda: SimpleNamespace(get=lambda *_, **__: source))
    monkeypatch.setattr(api.settings, 'api_token', '')
    monkeypatch.setattr(api.app.state, 'b2sign_exact_quotes',
                        SimpleNamespace(quote=quote), raising=False)
    body = {'values': {'_size': '24x36', 'finish': 'plain'}, 'quantity': 1,
            'turnaround': 'v1', 'fulfillment_mode': 'dropship',
            'require_fresh': True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                 base_url='http://fixture') as client:
        answer = await client.post('/api/v1/campaigns/c/b2sign-exact-quote/x', json=body)
    assert answer.status_code == 200
    assert answer.json()['status'] == 'pending'
    quote.assert_awaited_once()
    assert quote.await_args.kwargs == {'require_fresh': True}


@pytest.mark.asyncio
async def test_customer_route_rejects_choice_outside_reviewed_storefront_scope(monkeypatch):
    source={'pages':[{'url':URL,'status':'completed','public_data':{'item':item()}}]}
    product={'id':'x','supplier':'b2sign','stage':'blocked','supplier_url':URL,
             'source_collection_id':COLLECTION,
             'mapping_evidence':[{'required_fixed_options':{'finish':'gloss'}}]}
    quote=AsyncMock()
    monkeypatch.setattr(api,'active_campaign_product',lambda *_:{'products':[product]})
    monkeypatch.setattr(api,'source_collector',lambda:SimpleNamespace(get=lambda *_,**__:source))
    monkeypatch.setattr(api.settings,'api_token','')
    monkeypatch.setattr(api.app.state,'b2sign_exact_quotes',SimpleNamespace(quote=quote),raising=False)
    body={'values':{'_size':'24x36','finish':'plain'},'quantity':1,
          'turnaround':'v1','fulfillment_mode':'dropship'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                 base_url='http://fixture') as client:
        answer=await client.post('/api/v1/campaigns/c/b2sign-exact-quote/x',json=body)
    assert answer.status_code==422
    assert answer.json()['detail']=='selection_violates_storefront_fixed_supplier_choice'
    quote.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_quote_finalized_product_remains_quotable_after_group_advances(monkeypatch):
    source = {'pages': [{'url': URL, 'status': 'completed',
                         'public_data': {'item': item()}}]}
    product = {'id': 'x', 'supplier': 'b2sign', 'stage': 'finalized',
               'supplier_url': URL, 'source_collection_id': COLLECTION,
               'finalization_id': 'f' * 32,
               'finalization_evidence': {'pricing_model': 'source_live_quote'}}
    monkeypatch.setattr(api, 'active_campaign_product',
                        lambda *_: (_ for _ in ()).throw(HTTPException(409, 'not active')))
    monkeypatch.setattr(api.app.state, 'campaigns',
                        SimpleNamespace(get=lambda _: {'products': [product],
                                                       'active_group_product_ids': ['other']}), raising=False)
    monkeypatch.setattr(api, 'source_collector',
                        lambda: SimpleNamespace(get=lambda *_, **__: source))
    monkeypatch.setattr(api.settings, 'api_token', '')
    quote = AsyncMock(return_value={'status': 'pending', 'harvest_id': '4' * 32})
    monkeypatch.setattr(api.app.state, 'b2sign_exact_quotes',
                        SimpleNamespace(quote=quote), raising=False)
    body = {'values': {'_size': '24x36', 'finish': 'plain'}, 'quantity': 1,
            'turnaround': 'v1', 'fulfillment_mode': 'dropship', 'require_fresh': False}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                 base_url='http://fixture') as client:
        answer = await client.post('/api/v1/campaigns/c/b2sign-exact-quote/x', json=body)
    assert answer.status_code == 200 and answer.json()['status'] == 'pending'
    quote.assert_awaited_once()
    assert quote.await_args.kwargs == {'require_fresh': True}
    body.pop('fulfillment_mode')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                 base_url='http://fixture') as client:
        unbound = await client.post('/api/v1/campaigns/c/b2sign-exact-quote/x', json=body)
    assert unbound.status_code == 422
    assert unbound.json()['detail'] == 'finalized_live_quote_requires_explicit_dropship'
    quote.assert_awaited_once()


@pytest.mark.asyncio
async def test_static_finalized_product_cannot_use_live_quote_fallback(monkeypatch):
    product = {'id': 'x', 'supplier': 'b2sign', 'stage': 'finalized',
               'finalization_id': 'f' * 32,
               'finalization_evidence': {'pricing_model': 'additive_unit_price_times_quantity'}}
    monkeypatch.setattr(api, 'active_campaign_product',
                        lambda *_: (_ for _ in ()).throw(HTTPException(409, 'not active')))
    monkeypatch.setattr(api.app.state, 'campaigns',
                        SimpleNamespace(get=lambda _: {'products': [product]}), raising=False)
    monkeypatch.setattr(api.settings, 'api_token', '')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                 base_url='http://fixture') as client:
        answer = await client.post('/api/v1/campaigns/c/b2sign-exact-quote/x',
                                   json={'values': {'_size': '24x36', 'finish': 'plain'},
                                         'quantity': 1, 'turnaround': 'v1'})
    assert answer.status_code == 409


@pytest.mark.asyncio
async def test_campaign_quote_rejects_supplier_pickup_only_offer(monkeypatch):
    product = {"id": "x", "supplier": "b2sign", "stage": "blocked", "supplier_url": URL,
               "source_collection_id": COLLECTION}
    monkeypatch.setattr(api, "active_campaign_product", lambda *_: {"products": [product]})
    monkeypatch.setattr(api.settings, "api_token", "")
    body = {"values": {"_size": "49x38", "finish": "plain"}, "quantity": 1,
            "turnaround": "v1", "fulfillment_mode": "storepickup"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
        answer = await client.post("/api/v1/campaigns/c/b2sign-exact-quote/x", json=body)
    assert answer.status_code == 422
    assert answer.json()["detail"] == "supplier_pickup_only_unavailable_for_storefront"


@pytest.mark.asyncio
async def test_exact_quote_rejects_source_marked_unsaleable(monkeypatch):
    source_item = item()
    source_item['saleable'] = False
    source = {"pages": [{"url": URL, "status": "completed",
                         "public_data": {"item": source_item}}]}
    product = {"id": "x", "supplier": "b2sign", "stage": "blocked", "supplier_url": URL,
               "source_collection_id": COLLECTION}
    monkeypatch.setattr(api, "active_campaign_product", lambda *_: {"products": [product]})
    monkeypatch.setattr(api, "source_collector", lambda: SimpleNamespace(get=lambda *_, **__: source))
    monkeypatch.setattr(api.settings, "api_token", "")
    body = {"values": {"_size": "24x36", "finish": "plain"}, "quantity": 1,
            "turnaround": "v1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
        answer = await client.post("/api/v1/campaigns/c/b2sign-exact-quote/x", json=body)
    assert answer.status_code == 422
    assert answer.json()["detail"] == "source_product_not_saleable"


@pytest.mark.parametrize("component,key", [("divider", "_v1"), ("custom", "custom1")])
def test_flat_exact_quote_rejects_placeholder_and_nested_geometry(component, key):
    source = item()
    source["attrs"]["attrs"][1]["options"].append(
        {"key": key, "label": "Custom Hole Punch" if component == "custom" else "",
         "component": component, "component_props": {"type": "hole"} if component == "custom" else {}})
    catalog = parse_public_product(source, source_url=URL,
                                   source_bundle_sha256=PINNED_BUNDLE_SHA256)
    row = candidate()[1]
    row["contract_sha256"] = catalog["contract_sha256"]
    row["values"]["finish"] = key
    with pytest.raises(HarvestError, match="nested_or_placeholder_option_unsupported"):
        prepare_candidate(catalog, row)


@pytest.mark.parametrize("geometry,accepted", [
    ("TM1,BMS,P", True),
    ("TM1,TC,BMS,BC,P", True),
    ("TM5,BM0,P", True),
    ("TM6,BMS,P", False),
    ("TM1,BMS,G", False),
    ("TM1,BM0,P,unexpected", False),
])
def test_complete_custom_hole_geometry_is_source_and_size_bound(geometry, accepted):
    source = item()
    source["attrs"]["attrs"][1]["key"] = "holespunch"
    source["attrs"]["attrs"][1]["options"].append({
        "key": "custom", "label": "Custom Hole Punch", "component": "custom",
        "component_props": {"type": "hole", "size_key": "_size"}})
    catalog = parse_public_product(source, source_url=URL,
                                   source_bundle_sha256=PINNED_BUNDLE_SHA256)
    row = candidate()[1]
    row["contract_sha256"] = catalog["contract_sha256"]
    row["values"] = {"_size": "12x18", "holespunch": "custom",
                     "holespunch_CUSTOM_VALUE": geometry}
    if accepted:
        assert prepare_candidate(catalog, row)["values"]["holespunch_CUSTOM_VALUE"] == geometry
    else:
        with pytest.raises(HarvestError, match="nested_or_placeholder_option_unsupported"):
            prepare_candidate(catalog, row)


@pytest.mark.asyncio
async def test_exact_quote_route_accepts_complete_custom_geometry_without_finite_expansion(monkeypatch):
    source_item = item()
    source_item["attrs"]["attrs"][1]["key"] = "holespunch"
    source_item["attrs"]["attrs"][1]["options"].append({
        "key": "custom", "label": "Custom Hole Punch", "component": "custom",
        "component_props": {"type": "hole", "size_key": "_size"}})
    collection = {"pages": [{"url": URL, "status": "completed",
                             "public_data": {"item": source_item}}]}
    product = {"id": "x", "supplier": "b2sign", "stage": "blocked", "supplier_url": URL,
               "source_collection_id": COLLECTION}
    monkeypatch.setattr(api, "active_campaign_product", lambda *_: {"products": [product]})
    monkeypatch.setattr(api, "source_collector", lambda: SimpleNamespace(get=lambda *_, **__: collection))
    monkeypatch.setattr(api.settings, "api_token", "")
    manager = SimpleNamespace(quote=AsyncMock(return_value={"status": "pending", "final_eligible": False}))
    monkeypatch.setattr(api.app.state, "b2sign_exact_quotes", manager, raising=False)
    body = {"values": {"_size": "12x18", "holespunch": "custom",
                       "holespunch_CUSTOM_VALUE": "TM1,BMS,P"},
            "quantity": 1, "turnaround": "v1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://fixture") as client:
        answer = await client.post("/api/v1/campaigns/c/b2sign-exact-quote/x", json=body)
    assert answer.status_code == 200
    assert manager.quote.await_args.args[1]["values"]["holespunch_CUSTOM_VALUE"] == "TM1,BMS,P"


@pytest.mark.asyncio
async def test_custom_geometry_bootstraps_native_none_then_quotes_exact_choice(tmp_path):
    source = item()
    source["attrs"]["attrs"][1]["key"] = "holespunch"
    source["attrs"]["attrs"][1]["options"][0]["key"] = "0"
    source["attrs"]["attrs"][1]["options"].append({
        "key": "custom", "label": "Custom Hole Punch", "component": "custom",
        "component_props": {"type": "hole", "size_key": "_size"}})
    catalog = parse_public_product(source, source_url=URL,
                                   source_bundle_sha256=PINNED_BUNDLE_SHA256)
    candidate = {"source_url": URL, "source_product_id": catalog["source_product_id"],
                 "contract_sha256": catalog["contract_sha256"],
                 "values": {"_size": "12x18", "holespunch": "custom",
                            "holespunch_CUSTOM_VALUE": "TM1,BMS,P"},
                 "quantity": 1, "turnaround": "v1"}
    harvests = SimpleNamespace(get=lambda _: None, start=AsyncMock(return_value={"id": "c" * 32}))
    answer = await B2SignExactQuoteManager(SimpleNamespace(root=tmp_path), harvests).quote(catalog, candidate)
    assert answer["status"] == "pending"
    planned = harvests.start.await_args.args[1]
    assert [row["values"]["holespunch"] for row in planned] == ["0", "custom"]
    assert "holespunch_CUSTOM_VALUE" not in planned[0]["values"]
    assert planned[1]["values"]["holespunch_CUSTOM_VALUE"] == "TM1,BMS,P"
