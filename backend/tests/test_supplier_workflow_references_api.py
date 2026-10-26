"""Metadata endpoint authentication, strict bodies and old open compatibility."""
from unittest.mock import AsyncMock
import httpx
import pytest
import pytest_asyncio
import crawler.app as api
from crawler.dps_workflow_references import DPS_SUPPLIER_ID as KEY, DPS_ACCOUNT_URL as URL


@pytest_asyncio.fixture
async def workflow_api(monkeypatch):
    class Manager:
        opens=[]
        reads=[]
        async def open_supplier(self,key,**kwargs):
            if kwargs and key!=KEY:raise ValueError('supplier_workflow_scope_unsupported')
            self.opens.append((key,kwargs));return {'session_status':'signed_in'}
        async def read_supplier_workflow_references(self,key,url):
            if key!=KEY:raise ValueError('supplier_workflow_scope_unsupported')
            self.reads.append((key,url));return {'source_requests_initiated':0,'navigation_performed':False}
    manager=Manager()
    monkeypatch.setattr(api.app.state,'product_diagnoses',manager,raising=False)
    monkeypatch.setattr(api.settings,'api_token','workflow-fixture-token')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://fixture') as client:
        yield client,manager


def headers():return {'Authorization':'Bearer workflow-fixture-token'}


@pytest.mark.asyncio
async def test_both_routes_retain_existing_auth_dependency(workflow_api):
    client,manager=workflow_api
    for action,body in [('workflow-references',{'expected_url':URL}),('open',{'target_url':URL})]:
        response=await client.post(f'/api/v1/supplier-sessions/{KEY}/{action}',json=body)
        assert response.status_code==401
    assert manager.opens==manager.reads==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('body',[{},None,[],{'expected_url':True},{'expected_url':URL+'/'},{'expected_url':URL+'?token=PRIVATE'}, {'expected_url':URL,'value':'PRIVATE'}])
async def test_metadata_body_rejects_wrong_shape_url_extra_fields(workflow_api,body):
    client,manager=workflow_api
    response=await client.post(f'/api/v1/supplier-sessions/{KEY}/workflow-references',json=body,headers=headers())
    assert response.status_code==422 and manager.reads==[] and manager.opens==[]


@pytest.mark.asyncio
async def test_workflow_only_dispatches_existing_page_reader(workflow_api):
    client,manager=workflow_api
    response=await client.post(f'/api/v1/supplier-sessions/{KEY}/workflow-references',json={'expected_url':URL},headers=headers())
    assert response.status_code==200 and response.json()['source_requests_initiated']==0
    assert manager.reads==[(KEY,URL)] and manager.opens==[]
    response=await client.post('/api/v1/supplier-sessions/fourover/workflow-references',json={'expected_url':URL},headers=headers())
    assert response.status_code==422 and manager.reads==[(KEY,URL)]


@pytest.mark.asyncio
@pytest.mark.parametrize('body',[[],{'target_url':None},{'target_url':False},{'target_url':URL+'?token=PRIVATE'}, {'target_url':URL+'/'},{'target_url':'https://www.digital-print-solutions.com/cart'},{'target_url':URL,'extra':True}])
async def test_open_explicit_target_strict_before_manager(workflow_api,body):
    client,manager=workflow_api
    response=await client.post(f'/api/v1/supplier-sessions/{KEY}/open',json=body,headers=headers())
    assert response.status_code==422 and manager.opens==[]


@pytest.mark.asyncio
async def test_old_empty_body_and_exact_explicit_open_compatibility(workflow_api):
    client,manager=workflow_api;path=f'/api/v1/supplier-sessions/{KEY}/open'
    assert (await client.post(path,headers=headers())).status_code==200
    assert (await client.post(path,json={},headers=headers())).status_code==200
    assert manager.opens==[(KEY,{}),(KEY,{})]
    assert (await client.post(path,json={'target_url':URL},headers=headers())).status_code==200
    assert manager.opens[-1]==(KEY,{'target_url':URL})
    response=await client.post('/api/v1/supplier-sessions/fourover/open',json={'target_url':URL},headers=headers())
    assert response.status_code==422
