"""Authenticated, fixed-product evidence endpoints never invoke legacy harvests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
import crawler.app as api
from crawler.fourover_native_observation import NativeObserver
from crawler.fourover_collected_evidence import load_cache, SOURCE, PRODUCT, CHILD, RUN_SIZE, SCOPE, digest
from crawler.fourover_child_prices import FourOverChildPriceCollector, _encoded
from fourover_observer_fixture import build,CAMPAIGN,SNAPSHOT

HEADERS={'Authorization':'Bearer fixture-token'}

@pytest.fixture
def scoped(tmp_path,monkeypatch):
    env=build(tmp_path)
    monkeypatch.setattr(api,'store',env.store)
    monkeypatch.setattr(api.app.state,'campaigns',env.manager,raising=False)
    monkeypatch.setattr(api.settings,'api_token','fixture-token')
    # Actual native observer input validation runs; its source attempt is a local stub.
    observer=NativeObserver(env.store,None)
    attempt=AsyncMock(return_value={'status':'held','reason_code':'fixture_no_source',
        'supplier_requests_this_call':0,'controlled_requests':{'document':0,'price':0,'summary':0}})
    monkeypatch.setattr(observer,'_attempt',attempt)
    monkeypatch.setattr(api.app.state,'fourover_native_observer',observer,raising=False)
    legacy=[]
    for name in ('harvest_fourover_price_tables','fourover_product_probe','save_fourover_default_probe',
                 'harvest_fourover_option_map','materialize_fourover_draft','fourover_shipping_pilot'):
        stub=AsyncMock(side_effect=AssertionError('No legacy transport/import operation permitted'))
        monkeypatch.setattr(api,name,stub);legacy.append(stub)
    yield SimpleNamespace(env=env,attempt=attempt)
    assert all(not stub.called for stub in legacy)
    env.store.engine.dispose()


def payload(scoped,native,**changes):
    common={'snapshot_id':SNAPSHOT,'expected_checkpoint_sha256':scoped.env.checkpoint,
        'expected_option_rows_sha256':scoped.env.rows,'sets':1,'job_sample':False,'digital_proof':False}
    if native:common.update(child_id='160399',package_quantity=500,
        allow_summary_panel_without_shipping=True,attach_canary=False)
    else:common.update(materialize_rows=False,finalize=False)
    return {**common,**changes}

async def post(scoped,native,*,headers=HEADERS,product='10359',**changes):
    kind='fourover-native-observation' if native else 'fourover-collected-evidence'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://fixture') as client:
        return await client.post(f'/api/v1/campaigns/{CAMPAIGN}/{kind}/{product}',headers=headers,json=payload(scoped,native,**changes))

@pytest.mark.parametrize('native',[False,True])
@pytest.mark.asyncio
async def test_authentication_required_before_cache_or_source(scoped,native):
    original=scoped.env.manager._path(CAMPAIGN).read_bytes()
    response=await post(scoped,native,headers={})
    assert response.status_code==401 and not scoped.attempt.called
    assert scoped.env.manager._path(CAMPAIGN).read_bytes()==original

@pytest.mark.parametrize('native',[False,True])
@pytest.mark.parametrize('changes',[{'sets':True},{'sets':2},{'job_sample':True},{'digital_proof':True},
    {'snapshot_id':'../invalid'},{'expected_checkpoint_sha256':'bad'},{'unknown_option':'x'}])
@pytest.mark.asyncio
async def test_request_scope_is_strict_and_forbids_extra_fields(scoped,native,changes):
    response=await post(scoped,native,**changes)
    assert response.status_code==422 and not scoped.attempt.called

@pytest.mark.parametrize('changes',[{'package_quantity':501},{'child_id':'160496'},
    {'allow_summary_panel_without_shipping':False},{'attach_canary':True}])
@pytest.mark.asyncio
async def test_native_endpoint_cannot_widen_selection_or_attach_canary(scoped,changes):
    response=await post(scoped,True,**changes)
    assert response.status_code==422 and not scoped.attempt.called


@pytest.mark.asyncio
async def test_reviewed_historical_pair_missing_from_pinned_mapping_conflicts_before_source(scoped):
    # Historical pairs may be read only when their exact pinned cache mapping
    # exists. The synthetic corpus intentionally contains160399, not160492.
    response=await post(scoped,True,child_id='160492')
    assert response.status_code==409 and not scoped.attempt.called

@pytest.mark.parametrize('changes',[{'materialize_rows':True},{'finalize':True}])
@pytest.mark.asyncio
async def test_cache_endpoint_cannot_materialize_or_finalize(scoped,changes):
    response=await post(scoped,False,**changes)
    assert response.status_code==422

@pytest.mark.parametrize('native',[False,True])
@pytest.mark.asyncio
async def test_other_product_is_not_authorized_for_this_contract(scoped,native):
    response=await post(scoped,native,product='999')
    assert response.status_code==422 and not scoped.attempt.called

@pytest.mark.parametrize('native',[False,True])
@pytest.mark.parametrize('change',['mapping','task','stage','hash'])
@pytest.mark.asyncio
async def test_changed_mapping_task_stage_and_pin_hold_without_source(scoped,native,change):
    changes={}
    if change=='task':scoped.env.manager.tasks[CAMPAIGN]=object()
    elif change=='hash':changes['expected_checkpoint_sha256']='0'*64
    else:
        data=scoped.env.manager.get(CAMPAIGN);data['products'][0].update(
            {'supplier_url':'https://4over.com/other'} if change=='mapping' else {'stage':'finalized','finalized_at':'x'})
        scoped.env.manager._write(data)
    response=await post(scoped,native,**changes)
    assert response.status_code==409 and not scoped.attempt.called,response.text

@pytest.mark.asyncio
async def test_cache_endpoint_attaches_only_preliminary_atomic_pair(scoped):
    response=await post(scoped,False)
    assert response.status_code==200,response.text
    assert response.json()['rows_materialized']==0 and not response.json()['final_eligible']
    current=scoped.env.manager.get(CAMPAIGN)['products'][0]
    assert current['stage']=='queued' and current['row_count']==0
    assert current.get('fourover_option_map_evidence') and not current.get('finalized_at')
    assert not scoped.attempt.called

@pytest.mark.asyncio
async def test_historical_context_without_saved_journal_stays_closed_without_fabricated_evidence(scoped):
    before=scoped.env.manager._path(CAMPAIGN).read_bytes()
    response=await post(scoped,True)
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='held' and not result['price_fresh'] and not result['checkout_eligible']
    assert result['reason_code']=='prior_native_context_historical_only'
    assert result['supplier_requests_this_call']==0 and 'evidence' not in result
    assert scoped.env.manager._path(CAMPAIGN).read_bytes()==before
    assert scoped.attempt.await_count==0
    second=await post(scoped,True)
    assert second.json()['supplier_requests_this_call']==0 and scoped.attempt.await_count==0


def seed_historical_journal(scoped,mutate=None):
    """A real hashed local record; never bypasses the observer's live gate."""
    loaded=load_cache(scoped.env.store,SNAPSHOT,scoped.env.checkpoint,scoped.env.rows)
    captured={'schema':'fourover-native-observation/v1','status':'held','reason_code':'fixture_no_source',
        'product_id':PRODUCT,'source_url':SOURCE,'selection':{'child_id':CHILD,'run_size':RUN_SIZE,**SCOPE},
        'cache_provenance':loaded['provenance'],'observed_at':'2026-10-22T00:00:00+00:00',
        'price_fresh':False,'checkout_eligible':False,'final_eligible':False,'supplier_requests_this_call':0}
    if mutate:mutate(captured)
    raw=_encoded(captured);path=scoped.env.store.put_object(raw,'fourover-native-observation.json')
    record={'object_path':path,'sha256':digest(raw)}
    key=digest(_encoded({'campaign':CAMPAIGN,'snapshot':SNAPSHOT,'checkpoint':scoped.env.checkpoint,
        'child':CHILD,'run_size':RUN_SIZE,'scope':SCOPE}))
    journal=scoped.env.store.root/'fourover-native-observations'/key[:2]/(key+'.json')
    FourOverChildPriceCollector(scoped.env.store,None)._save(journal,{'key':key,'status':'captured','record':record})
    return record


@pytest.mark.asyncio
async def test_saved_native_held_report_is_separate_hashed_evidence_and_never_attaches_canary(scoped):
    record=seed_historical_journal(scoped)
    before=scoped.env.manager._path(CAMPAIGN).read_bytes()
    for _ in range(2):
        response=await post(scoped,True)
        assert response.status_code==200,response.text
        result=response.json()
        assert result['status']=='held' and not result['price_fresh'] and not result['checkout_eligible']
        assert result['evidence']==record and result['evidence']['object_path'].endswith('.fourover-native-observation.json')
        assert result['cache']=='historical_observation_no_new_requests' and result['supplier_requests_this_call']==0
    assert scoped.env.manager._path(CAMPAIGN).read_bytes()==before and scoped.attempt.await_count==0


@pytest.mark.parametrize('change',['source','product','quantity','scope','provenance'])
@pytest.mark.asyncio
async def test_hashed_saved_record_still_requires_exact_context_and_provenance(scoped,change):
    def mutate(record):
        if change=='source':record['source_url']='https://4over.com/other'
        elif change=='product':record['product_id']='999'
        elif change=='quantity':record['selection']['run_size']=501
        elif change=='scope':record['selection']['digital_proof']=True
        else:record['cache_provenance']={}
    seed_historical_journal(scoped,mutate)
    response=await post(scoped,True)
    assert response.status_code==409 and not scoped.attempt.called
