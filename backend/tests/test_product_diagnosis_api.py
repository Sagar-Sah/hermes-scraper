"""Authenticated diagnosis and finalization API boundaries, without browsers."""
from copy import deepcopy

import httpx
import pytest

import crawler.app as api
from crawler.config import Settings
from crawler.store import Store, Run
from test_product_finalization import diagnosis


@pytest.fixture
async def client(monkeypatch, tmp_path):
    class Manager:
        value = diagnosis()
        def get(self, key):
            if key != self.value['id']: raise LookupError('missing')
            return deepcopy(self.value)
        def list(self): return [self.get(self.value['id'])]
        def suppliers(self): return []
        async def save_selection(self, key, revision, selection):
            if revision != self.value['revision']: raise ValueError('diagnosis_revision_conflict')
            self.value['selection'] = selection
            return self.get(key)
    manager = Manager()
    monkeypatch.setattr(api.app.state, 'product_diagnoses', manager, raising=False)
    monkeypatch.setattr(api.settings, 'api_token', 'diagnosis-test-token')
    store = Store(Settings(data_dir=tmp_path, s3_bucket=''))
    monkeypatch.setattr(api, 'store', store)
    async def public_url(url): return url
    monkeypatch.setattr(api, 'validate_url', public_url)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://fixture') as client:
        yield client, manager
    store.engine.dispose()


def headers(): return {'Authorization':'Bearer diagnosis-test-token'}


@pytest.mark.asyncio
async def test_all_diagnosis_operations_require_auth(client):
    client, _ = client
    for method, path in [('GET','supplier-sessions'), ('POST','supplier-sessions/printograph/open'),
            ('POST','supplier-sessions/printograph/check'), ('GET','product-diagnoses'),
            ('POST','product-diagnoses'), ('GET','product-diagnoses/a'),
            ('POST','product-diagnoses/a/selection'), ('POST','product-diagnoses/a/refresh'),
            ('GET','product-diagnoses/a/saved-products'), ('POST','product-diagnoses/a/finalization'),
            ('GET','finalizations/a/report')]:
        response = await client.request(method, '/api/v1/' + path, json={} if method == 'POST' else None)
        assert response.status_code == 401, (method, path, response.text)


@pytest.mark.asyncio
async def test_hidden_choices_persist_but_missing_fixed_choice_and_stale_revision_do_not(client):
    client, manager = client
    path = '/api/v1/product-diagnoses/' + manager.value['id'] + '/selection'
    fields = [{k:f[k] for k in ('key','include','fixed_values')} for f in manager.value['selection']['fields']]
    response = await client.post(path, json={'revision':1,'fields':fields}, headers=headers())
    assert response.status_code == 200, response.text
    saved = deepcopy(response.json()['selection'])
    assert saved['hidden_fixed'] == {'page-proofType':['31']}
    fields[1]['fixed_values'] = []
    assert (await client.post(path,json={'revision':1,'fields':fields},headers=headers())).status_code == 422
    assert manager.value['selection'] == saved
    assert (await client.post(path,json={'revision':True,'fields':fields},headers=headers())).status_code == 422
    response = await client.post(path,json={'revision':0,'fields':fields},headers=headers())
    assert response.status_code == 409
    assert manager.value['selection'] == saved


@pytest.mark.asyncio
async def test_future_browser_run_consumes_saved_fixed_policy_without_starting_a_campaign(client):
    client, manager = client
    payload = {'name':'Policy test','adapter':'browser','urls':[manager.value['url']],
               'diagnosis_id': manager.value['id'], 'diagnosis_revision':1}
    response = await client.post('/api/v1/runs',json=payload,headers=headers())
    assert response.status_code == 200, response.text
    spec = response.json()['spec']
    assert spec['field_policy']['hidden_fixed'] == {'page-proofType':['31']}
    assert spec['filters']['proofType'] == '31'
    assert spec['adapter'] == 'browser'
    with api.store.session() as session:
        saved = session.get(Run, response.json()['id'])
        assert saved.spec['field_policy'] == spec['field_policy']
    response = await client.post('/api/v1/runs',json={**payload,'filters':{'proofType':['36']}},headers=headers())
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_review_result_is_rejected_if_policy_changes_during_check(client, monkeypatch):
    client, manager = client
    def changed(store, snapshot, run_id, product_id, source):
        manager.value['selection'] = None
        return {'eligible_rows':1}
    monkeypatch.setattr(api, 'review_product', changed)
    response = await client.post('/api/v1/product-diagnoses/' + manager.value['id'] + '/finalization',
        json={'revision':1,'run_id':'run','product_id':'product'},headers=headers())
    assert response.status_code == 409
