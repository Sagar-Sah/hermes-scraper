"""Offline API scope filters preserve source contracts and candidate validation."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

import crawler.app as api
from crawler.b2sign_catalog import PINNED_BUNDLE_SHA256, parse_public_product
from test_b2sign_catalog import simple, select

URL = 'https://www.b2sign.com/table-runner'
COLLECTION_ID = 'a' * 32


def runner_item():
    runner = select('runner_throw', ['tro', 'tr_tc_combo'])
    runner['options'][0]['default'] = False
    runner['options'][1]['default'] = True
    color = select('solid_color_tablethrow', ['white', 'blue6', 'blue8', 'yellow8', 'unavailable'],
                   visible=[{'key':'runner_throw','op':'=','value':'tr_tc_combo'}])
    color['options'][-1].update(visible=False, disabled=True)
    item = simple([
        {'key':'size','component':'size','options':[
            {'key':'width','component_props':{'range':[str(n) for n in range(24,61,2)]}},
            {'key':'height','component_props':{'range':['72','80','90']}}]}, runner, color],
        production=[{'match':[{'key':'_qty','op':'>','value':100}],
                     'options':[{'key':'v3','label':'3 Business Days'}]},
                    {'match':[{'key':'_qty','op':'>','value':0}],
                     'options':[{'key':'v1','label':'Next Day'}]}])
    item.update(id=29, url='/table-runner', title='Table Runner')
    return item


def bind_source(monkeypatch, item=None):
    source = {'pages':[{'url':URL,'status':'completed','public_data':{'item':item or runner_item()}}]}
    calls = []
    def get(identifier, *, include_data):
        calls.append((identifier, include_data))
        assert identifier == COLLECTION_ID and include_data is True
        return deepcopy(source)
    monkeypatch.setattr(api, 'source_collector', lambda: SimpleNamespace(get=get))
    return calls


def payload(**changes):
    return api.B2HarvestRequest(source_collection_id=COLLECTION_ID, source_url=URL,
                               quantities=[1,10,100,101], **changes)


def test_combo_scope_is_912_with_unchanged_contract_and_no_runner_only(monkeypatch):
    bind_source(monkeypatch)
    original, full, _ = api.b2sign_harvest_plan(payload())
    catalog, rows, summary = api.b2sign_harvest_plan(payload(fixed_options={'runner_throw':'tr_tc_combo'}))
    assert len(full) == 1140 and len(rows) == 912
    assert catalog == original
    assert all(row['contract_sha256'] == original['contract_sha256'] for row in rows)
    assert all(row['values']['runner_throw'] == 'tr_tc_combo' for row in rows)
    assert {row['quantity'] for row in rows} == {1,10,100,101}
    assert {row['turnaround'] for row in rows} == {'v1','v3'}
    assert summary['fixed_options'] == {'runner_throw':'tr_tc_combo'}
    assert summary['unfiltered_candidate_count'] == 1140
    assert summary['fixed_scope_candidate_count'] == 912
    assert summary['catalog_complete'] is False and summary['prices_verified'] is False
    assert all(row['final_eligible'] is False for row in rows)


def test_scope_applied_before_baseline_selection(monkeypatch):
    bind_source(monkeypatch)
    _, rows, summary = api.b2sign_harvest_plan(payload(fixed_options={'runner_throw':'tr_tc_combo'}, baseline_only=True))
    assert len(rows) == 1 and rows[0]['values']['runner_throw'] == 'tr_tc_combo'
    assert summary['fixed_scope_candidate_count'] == 912


def test_low_quantity_tent_preflight_accounts_for_session_bootstrap(monkeypatch):
    item=simple([select('tent_half_wall',['0','1x1'])],production=[
        {'match':[{'key':'_qty','op':'>','value':0}],
         'options':[{'key':'v1','label':'Next Day'}]}])
    item.update(id=238,url='/table-runner',title='Event Tent')
    bind_source(monkeypatch,item)
    _,rows,summary=api.b2sign_harvest_plan(api.B2HarvestRequest(
        source_collection_id=COLLECTION_ID,source_url=URL,
        quantities=[10],baseline_only=True,fixed_options={'tent_half_wall':'1x1'}))
    assert [row['quantity'] for row in rows]==[11,10]
    assert rows[0]['values']==rows[1]['values']=={'tent_half_wall':'1x1'}
    assert summary['bootstrap_quantity']==11
    assert summary['bootstrap_request_count']==1
    assert summary['deduplicated_request_count']==2


def test_canary_scope_selects_baseline_and_one_change_per_choice(monkeypatch):
    item=simple([select('finish',['plain','gloss','matte']),
                 select('color',['white','black'])])
    item['url']='/table-runner'
    bind_source(monkeypatch,item)
    _,rows,summary=api.b2sign_harvest_plan(api.B2HarvestRequest(
        source_collection_id=COLLECTION_ID,source_url=URL,
        quantities=[11],canary_only=True))
    assert len(rows)==4
    assert {(row['values']['finish'],row['values']['color']) for row in rows}=={
        ('plain','white'),('gloss','white'),('matte','white'),('plain','black')}
    assert summary['canary_mode']=='baseline_plus_one_visible_option_change'
    assert summary['canary_request_count']==4


def test_canary_and_baseline_flags_are_mutually_exclusive(monkeypatch):
    bind_source(monkeypatch)
    with pytest.raises(ValueError,match='cannot be combined'):
        api.b2sign_harvest_plan(payload(canary_only=True,baseline_only=True))


@pytest.mark.parametrize('fixed', [
    {'unknown':'x'}, {'_time':'10:00:00'}, {'_qty':1}, {'turnaround':'v0'},
    {'runner_throw':'invented'}, {'runner_throw':1}, {'solid_color_tablethrow':'unavailable'}, {'size':'25x72'}
])
def test_unknown_keys_unavailable_values_and_runtime_overrides_fail(monkeypatch, fixed):
    bind_source(monkeypatch)
    with pytest.raises(ValueError, match='fixed_options'):
        api.b2sign_harvest_plan(payload(fixed_options=fixed))


@pytest.mark.parametrize('fixed', [{'runner_throw':True}, {'runner_throw':None}, {'runner_throw':['tro']},
                                  {'runner_throw':{}}, {'runner_throw':float('nan')}, {'runner_throw':float('inf')}])
def test_non_scalar_or_nonfinite_fixed_values_fail_model(fixed):
    with pytest.raises(ValidationError): payload(fixed_options=fixed)


def test_jointly_impossible_known_values_fail_instead_of_zero_row_success(monkeypatch):
    finish = select('finish', ['gloss_finish','matte_finish'])
    for option, material in zip(finish['options'], ['gloss','matte']):
        option['visible'] = [{'key':'material','op':'=','value':material}]
    item = simple([select('material',['gloss','matte']), finish])
    item['url'] = '/table-runner'
    bind_source(monkeypatch, item)
    with pytest.raises(ValueError, match='no matching'):
        api.b2sign_harvest_plan(payload(fixed_options={'material':'gloss','finish':'matte_finish'}))


def test_exact_numeric_and_empty_source_values_are_retained(monkeypatch):
    item = simple([select('numeric',[0,1]),select('optional',['','x'])]); item['url']='/table-runner'
    bind_source(monkeypatch,item)
    _, rows, _ = api.b2sign_harvest_plan(payload(fixed_options={'numeric':0,'optional':''}))
    assert len(rows) == 4 and all(row['values']=={'numeric':0,'optional':''} for row in rows)
    with pytest.raises(ValueError, match='unavailable'):
        api.b2sign_harvest_plan(payload(fixed_options={'numeric':'0'}))


def test_filtered_candidates_still_pass_original_validator(monkeypatch):
    bind_source(monkeypatch)
    def reject(catalog, candidate): raise ValueError('fixture_source_binding_failure')
    monkeypatch.setattr('crawler.b2sign_harvest.prepare_candidate', reject)
    with pytest.raises(ValueError, match='fixture_source_binding_failure'):
        api.b2sign_harvest_plan(payload(fixed_options={'runner_throw':'tr_tc_combo'}))


def test_preflight_counts_distinct_quote_requests_after_source_state_collapse(monkeypatch):
    bind_source(monkeypatch)
    monkeypatch.setattr('crawler.b2sign_harvest.prepare_candidate',
                        lambda catalog, candidate: {'candidate_id': 'same-request'})
    _, rows, summary = api.b2sign_harvest_plan(payload(baseline_only=False))
    assert len(rows) == summary['deduplicated_request_count'] == 1
    assert summary['duplicate_source_states'] == summary['fixed_scope_candidate_count'] - 1


@pytest.mark.asyncio
async def test_preflight_and_start_expose_and_budget_only_filtered_scope(monkeypatch):
    bind_source(monkeypatch)
    monkeypatch.setattr(api.settings, 'api_token', 'fixture-token')
    starter = AsyncMock(return_value={'id':'b'*32,'requested_count':912,'status':'opening'})
    monkeypatch.setattr(api, 'b2sign_harvest_manager', lambda: SimpleNamespace(start=starter))
    body = payload(fixed_options={'runner_throw':'tr_tc_combo'}, attempt_budget=912).model_dump()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://fixture') as client:
        assert (await client.post('/api/v1/b2sign-harvests/preflight',json=body)).status_code == 401
        headers={'Authorization':'Bearer fixture-token'}
        response=await client.post('/api/v1/b2sign-harvests/preflight',json=body,headers=headers)
        assert response.status_code == 200
        result=response.json()
        assert result['fixed_options']==body['fixed_options'] and result['requested_count']==912
        assert result['minimum_pricing_seconds']==456 and result['catalog_complete'] is False
        assert result['verification_status']=='pending'
        started=await client.post('/api/v1/b2sign-harvests',json=body,headers=headers)
        assert started.status_code==200
        args, kwargs=starter.await_args
        assert len(args[1])==912 and kwargs['attempt_budget']==912 and kwargs['diagnosis'] is None
        assert all(c['values']['runner_throw']=='tr_tc_combo' for c in args[1])
        bad=await client.post('/api/v1/b2sign-harvests',json={**body,'fixed_options':{'runner_throw':'bad'}},headers=headers)
        assert bad.status_code==422 and starter.await_count==1
