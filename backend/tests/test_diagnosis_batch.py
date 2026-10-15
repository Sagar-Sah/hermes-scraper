from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

import crawler.app as api
import crawler.diagnosis_batch as batch_module
from crawler.config import Settings
from crawler.diagnosis_batch import prepare_batch, queue_batch
from crawler.store import Store, Run, Product
from test_product_finalization import diagnosis


class Manager:
    def __init__(self):
        first = diagnosis()
        second = deepcopy(first)
        second.update(id='b' * 32, url='https://www.printograph.com/products/club-flyers/order', title='Club Flyers')
        self.values = {first['id']: first, second['id']: second}
    def get(self, key): return deepcopy(self.values[key])
    def payload(self):
        return [{'diagnosis_id': key, 'revision': d['revision'],
                 'fields': [{k: f[k] for k in ('key','include','fixed_values')} for f in d['selection']['fields']]}
                for key, d in self.values.items()]


@pytest.fixture
def store(tmp_path):
    value = Store(Settings(data_dir=tmp_path, s3_bucket=''))
    yield value
    value.engine.dispose()


def test_each_product_gets_immutable_policy_and_retry_does_not_duplicate(store):
    manager = Manager()
    payload = manager.payload()
    plan = prepare_batch(manager, payload)
    assert plan['ready'] and plan['pricing_request_count'] is None
    key = str(uuid4())
    ids = queue_batch(store, plan, plan['plan_token'], key)
    assert queue_batch(store, plan, plan['plan_token'], key) == ids
    manager.values[payload[0]['diagnosis_id']]['selection'] = None
    with store.session() as session:
        runs = list(session.scalars(select(Run)))
        assert len(runs) == 2
        assert len(list(session.scalars(select(Product)))) == 2
        assert all(run.spec['field_policy']['hidden_fixed'] == {'page-proofType':['31']} for run in runs)
        assert {run.spec['urls'][0] for run in runs} == {d['url'] for d in manager.values.values()}


def test_one_unsupported_or_stale_product_starts_nothing(store):
    manager = Manager()
    payload = manager.payload()
    manager.values[payload[1]['diagnosis_id']]['fields'][0]['kind'] = 'custom_select'
    plan = prepare_batch(manager, payload)
    assert not plan['ready'] and plan['products'][1]['reasons']
    with pytest.raises(ValueError): queue_batch(store, plan, plan['plan_token'], str(uuid4()))
    payload[0]['revision'] = 0
    assert prepare_batch(manager, payload)['products'][0]['status'] == 'blocked'
    with store.session() as session: assert list(session.scalars(select(Run))) == []


def test_plan_change_and_reused_launch_key_rejected_atomically(store):
    manager = Manager()
    payload = manager.payload()
    plan = prepare_batch(manager, payload)
    key = str(uuid4())
    queue_batch(store, plan, plan['plan_token'], key)
    changed = prepare_batch(manager, payload[:1])
    with pytest.raises(ValueError): queue_batch(store, changed, changed['plan_token'], key)
    with pytest.raises(ValueError): queue_batch(store, plan, 'x' * 64, str(uuid4()))
    with store.session() as session: assert len(list(session.scalars(select(Run)))) == 2


def test_duplicate_source_url_and_unbounded_number_blocked():
    manager = Manager()
    payload = manager.payload()
    manager.values[payload[1]['diagnosis_id']]['url'] = manager.values[payload[0]['diagnosis_id']]['url']
    assert not prepare_batch(manager, payload)['ready']
    manager = Manager()
    d = next(iter(manager.values.values()))
    d['fields'][0].update(kind='number', choices=[], min=None, max=None, step=None)
    assert not prepare_batch(manager, manager.payload())['ready']


@pytest.mark.asyncio
async def test_api_preflight_auth_and_launch(store, monkeypatch):
    manager = Manager()
    monkeypatch.setattr(api, 'store', store)
    monkeypatch.setattr(api.app.state, 'product_diagnoses', manager, raising=False)
    monkeypatch.setattr(api.settings, 'api_token', 'batch-test-token')
    async def safe_url(url): return url
    monkeypatch.setattr(api, 'validate_url', safe_url)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://fixture') as client:
        body = {'products': manager.payload()}
        headers = {'Authorization': 'Bearer batch-test-token'}
        base = '/api/v1/product-diagnoses/'
        assert (await client.post(base+'preflight',json=body)).status_code == 401
        response = await client.post(base+'preflight',json=body,headers=headers)
        assert response.status_code == 200, response.text
        plan = response.json()
        assert plan['ready'] and '_specs' not in plan
        body.update(plan_token=plan['plan_token'],idempotency_key=str(uuid4()))
        first = await client.post(base+'launch',json=body,headers=headers)
        assert first.status_code == 200, first.text
        for item in manager.values.values(): item['status'] = 'closed'
        again = await client.post(base+'launch',json=body,headers=headers)
        assert again.status_code == 200, again.text
        assert [r['id'] for r in first.json()['runs']] == [r['id'] for r in again.json()['runs']]
        assert len(first.json()['runs']) == 2
        body['products'][0]['fields'][0]['include'] = False
        assert (await client.post(base+'launch',json=body,headers=headers)).status_code == 409


@pytest.mark.asyncio
async def test_api_recovers_a_concurrent_identical_commit(store, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    manager = Manager()
    monkeypatch.setattr(api, 'store', store)
    monkeypatch.setattr(api.app.state, 'product_diagnoses', manager, raising=False)
    monkeypatch.setattr(api.settings, 'api_token', '')
    async def safe_url(url): return url
    monkeypatch.setattr(api, 'validate_url', safe_url)
    original = batch_module.queue_batch
    def concurrent(*args, **kwargs):
        original(*args, **kwargs)
        raise IntegrityError('concurrent insert', {}, Exception('duplicate'))
    monkeypatch.setattr(batch_module, 'queue_batch', concurrent)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://fixture') as client:
        body = {'products': manager.payload()}
        plan = (await client.post('/api/v1/product-diagnoses/preflight', json=body)).json()
        body.update(plan_token=plan['plan_token'], idempotency_key=str(uuid4()))
        response = await client.post('/api/v1/product-diagnoses/launch', json=body)
        assert response.status_code == 200, response.text
        assert len(response.json()['runs']) == 2
    with store.session() as session: assert len(list(session.scalars(select(Run)))) == 2
