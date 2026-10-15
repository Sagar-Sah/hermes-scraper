from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import crawler.app as api


@pytest.mark.asyncio
async def test_pilot_api_requires_auth_exact_revision_and_explicit_scope(monkeypatch):
    diagnosis = {'id':'a' * 32, 'revision':1, 'selection':{'fixed_values':[]}}
    pilots = SimpleNamespace(start=AsyncMock(return_value={'id':'b' * 32,'status':'opening'}),
                             get=lambda key: {'id':key,'status':'completed'}, list=lambda: [])
    monkeypatch.setattr(api.settings, 'api_token', 'pilot-test-token')
    monkeypatch.setattr(api.app.state, 'pricing_pilots', pilots, raising=False)
    monkeypatch.setattr(api.app.state, 'product_diagnoses', SimpleNamespace(get=lambda key: deepcopy(diagnosis)), raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://fixture') as client:
        path = '/api/v1/product-diagnoses/' + diagnosis['id'] + '/pricing-pilot'
        body = {'revision':1,'quantities':[1,10,100,101]}
        assert (await client.post(path,json=body)).status_code == 401
        headers = {'Authorization':'Bearer pilot-test-token'}
        assert (await client.post(path,json={**body,'revision':0},headers=headers)).status_code == 409
        assert (await client.post(path,json={**body,'revision':True},headers=headers)).status_code == 422
        assert (await client.post(path,json={**body,'quantities':[True]},headers=headers)).status_code == 422
        response = await client.post(path,json=body,headers=headers)
        assert response.status_code == 200
        pilots.start.assert_awaited_once_with(diagnosis, quantities=[1,10,100,101])
        assert (await client.get('/api/v1/pricing-pilots',headers=headers)).json() == {'items':[]}
        assert (await client.get('/api/v1/pricing-pilots/'+'b'*32)).status_code == 401
