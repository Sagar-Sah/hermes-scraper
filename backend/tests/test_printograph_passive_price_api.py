"""Isolated app boundary with fake managers; no lifespan/browser/source/HTTP transport."""
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

SOURCE='https://www.printograph.com/products/special-shapes/order'

@pytest.fixture
async def api_client(monkeypatch,tmp_path):
    from crawler.config import settings
    (tmp_path/'private-data').mkdir(mode=0o700)
    monkeypatch.setattr(settings,'data_dir',tmp_path/'private-data')
    monkeypatch.setattr(settings,'database_url','sqlite:///'+str(tmp_path/'private.sqlite'))
    monkeypatch.setattr(settings,'api_token','passive-api-fixture-token')
    file=Path(__file__).parents[1]/'crawler/app.py'
    spec=importlib.util.spec_from_file_location('crawler.passive_price_api_fixture',file)
    api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
    class Manager:
        async def start(self,url,*,focus=False):return {'url':url,'focus':focus}
        async def read_passive_price_evidence(self,key,revision):return {'status':'held','reasons':['passive_observation_not_enabled'],'source_requests_initiated':0,'source_data_finalized':False}
    manager=Manager();api.app.state.product_diagnoses=manager
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://fixture')as client:
        yield client,manager,api
    api.store.engine.dispose()

HEADERS={'Authorization':'Bearer passive-api-fixture-token'}

@pytest.mark.asyncio
async def test_default_off_preserves_legacy_manager_signature_and_background(api_client):
    client,manager,_=api_client
    response=await client.post('/api/v1/product-diagnoses',json={'url':SOURCE},headers=HEADERS)
    assert response.status_code==200 and response.json()=={'url':SOURCE,'focus':False}
    response=await client.post('/api/v1/product-diagnoses',json={'url':SOURCE,'observe_printograph_prices':False,'focus':True},headers=HEADERS)
    assert response.status_code==200 and response.json()['focus']is True

@pytest.mark.asyncio
async def test_opt_in_only_for_exact_url_and_only_explicit_keyword(api_client):
    client,manager,_=api_client;manager.start=AsyncMock(return_value={'status':'opening'})
    response=await client.post('/api/v1/product-diagnoses',json={'url':SOURCE,'observe_printograph_prices':True},headers=HEADERS)
    assert response.status_code==200
    manager.start.assert_awaited_once_with(SOURCE,focus=False,observe_printograph_prices=True)
    manager.start.reset_mock()
    for url in [SOURCE+'/',SOURCE+'?x=1',SOURCE+'#private',SOURCE.replace('www.',''),SOURCE.replace('https:','http:'),'https://example.invalid/product',SOURCE.replace('special-shapes','postcards')]:
        response=await client.post('/api/v1/product-diagnoses',json={'url':url,'observe_printograph_prices':True},headers=HEADERS)
        assert response.status_code==422
    manager.start.assert_not_called()

@pytest.mark.asyncio
@pytest.mark.parametrize('flag',[1,0,'true','false',None,{},[]])
async def test_nonboolean_opt_in_rejected_before_manager(api_client,flag):
    client,manager,_=api_client;manager.start=AsyncMock()
    response=await client.post('/api/v1/product-diagnoses',json={'url':SOURCE,'observe_printograph_prices':flag},headers=HEADERS)
    assert response.status_code==422;manager.start.assert_not_called()

@pytest.mark.asyncio
async def test_auth_and_strict_read_payload_no_source_dispatch(api_client):
    client,manager,_=api_client;method=AsyncMock(return_value={'status':'held','source_requests_initiated':0,'source_data_finalized':False});manager.read_passive_price_evidence=method
    path='/api/v1/product-diagnoses/'+'a'*32+'/passive-price-evidence'
    assert(await client.post(path,json={'revision':1})).status_code==401
    assert(await client.post('/api/v1/product-diagnoses',json={'url':SOURCE,'observe_printograph_prices':True})).status_code==401
    for body in [{},{'revision':True},{'revision':'1'},{'revision':1.0},{'revision':None},{'revision':1,'url':SOURCE},{'revision':1,'refresh':True}]:
        assert(await client.post(path,json=body,headers=HEADERS)).status_code==422
    method.assert_not_called()
    response=await client.post(path,json={'revision':7},headers=HEADERS)
    assert response.status_code==200 and response.json()=={'status':'held','source_requests_initiated':0,'source_data_finalized':False}
    method.assert_awaited_once_with('a'*32,7)

@pytest.mark.asyncio
@pytest.mark.parametrize('error,status',[(LookupError('missing'),404),(ValueError('diagnosis_revision_conflict'),409),(ValueError('passive_observation_not_enabled'),422)])
async def test_reader_held_missing_and_stale_are_not_success(api_client,error,status):
    client,manager,_=api_client;manager.read_passive_price_evidence=AsyncMock(side_effect=error)
    path='/api/v1/product-diagnoses/'+'a'*32+'/passive-price-evidence'
    assert(await client.post(path,json={'revision':1},headers=HEADERS)).status_code==status
