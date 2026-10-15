from types import SimpleNamespace

import httpx
import pytest

import crawler.app as api
from crawler.campaign_progress import CampaignManager


@pytest.mark.asyncio
async def test_exclusion_api_authenticated_durable_and_rejects_milestone_injection(tmp_path,monkeypatch):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    row=manager.create(name='Scope',products=[{'id':'21571','title':'Sample Kit','storefront_url':'https://printitprints.com/shop/sample-kit/'}])
    monkeypatch.setattr(api.app.state,'campaigns',manager,raising=False)
    monkeypatch.setattr(api.settings,'api_token','fixture-token')
    body={'products':[{'id':'21571','reason':'User excluded sample kits.'}]}
    path=f'/api/v1/campaigns/{row["id"]}/exclusions'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://fixture') as client:
        assert (await client.post(path,json=body)).status_code==401
        headers={'Authorization':'Bearer fixture-token'}
        bad={'products':[dict(body['products'][0],finalized_at='today')]}
        assert (await client.post(path,json=bad,headers=headers)).status_code==422
        response=await client.post(path,json=body,headers=headers)
        assert response.status_code==200
        result=response.json()
        assert result['total_products']==1 and result['active_products']==0 and result['excluded_products']==1
        assert result['products'][0]['stage']=='excluded' and result['counts']['finalized']==0
        assert (await client.get(f'/api/v1/campaigns/{row["id"]}',headers=headers)).json()['products'][0]['exclusion_reason']=='User excluded sample kits.'
        unknown={'products':[{'id':'unknown','reason':'No'}]}
        assert (await client.post(path,json=unknown,headers=headers)).status_code==409
        assert (await client.post('/api/v1/campaigns/missing/exclusions',json=body,headers=headers)).status_code==404


@pytest.mark.asyncio
async def test_future_group_supplier_probe_is_rejected_before_browser_work(tmp_path,monkeypatch):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    row=manager.create(name='Grouped',products=[{
        'id':str(index),'title':f'Product {index}',
        'storefront_url':f'https://printitprints.com/shop/{index}/',
        'supplier_url':f'https://4over.com/product-{index}',
        'supplier':'fourover'} for index in range(1,12)])
    monkeypatch.setattr(api.app.state,'campaigns',manager,raising=False)
    monkeypatch.setattr(api.settings,'api_token','fixture-token')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://fixture') as client:
        response=await client.post(f'/api/v1/campaigns/{row["id"]}/fourover-shipping-pilot/11',
                                   headers={'Authorization':'Bearer fixture-token'})
        assert response.status_code==409
        assert 'current product group' in response.json()['detail']
