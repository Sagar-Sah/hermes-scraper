import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from crawler import campaign_sources as public
from crawler import wsdisplay_source_collector as native
from crawler.source_collection_profiles import SourceCollectionProfiles
from crawler.source_collection_scope import resume_scope

A = 'https://www.wsdisplay.com/SYNTHETIC-A'
B = 'https://www.wsdisplay.com/SYNTHETIC-B'
C = 'https://www.wsdisplay.com/SYNTHETIC-C'
P = 'https://www.b2sign.com/SYNTHETIC-A'
Q = 'https://www.b2sign.com/SYNTHETIC-B'


class Store:
    def __init__(self, root):
        self.root = root

    def put_object(self, value, suffix):
        key = 'objects/' + hashlib.sha256(value).hexdigest() + '.' + suffix
        path = self.root / key
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(value)
        return key


def stable(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def npage(url, status='held'):
    return {'url': url, 'status': status, 'nodes': 2,
            'frontier': [{'custcol_pms_match': 'F'}, {'custcol_pms_match': 'T'}],
            'seen': [native.digest({'custcol_pms_match': 'F'})],
            'pending': None, 'receipts': [], 'holds': [],
            'basis_witnesses': {'preserved': {'sha256': 'a' * 64}},
            'q2_reservations': {'closed': {'state': 'dispatched', 'quantity': 2}},
            'native_catalogue_ref': {'sha256': 'b' * 64}, 'error': 'native_frontier_bound'}


def njob(jid='a' * 32):
    return {'id': jid, 'status': 'attention', 'error': None,
            'max_nodes_per_product': 2, 'native_q2_checks': False,
            'account_marker': 'My Account SYNTHETIC BUSINESS', 'seeds': {},
            'pages': [npage(A), npage(B), npage(C, 'completed')]}


def pjob(jid='d' * 32):
    return {'id': jid, 'status': 'attention', 'error': None,
            'pages': [{'url': P, 'status': 'failed', 'attempts': 3, 'evidence': None,
                       'error': {'code': 'old'}, 'custom_preserved': [1, 2]},
                      {'url': Q, 'status': 'pending', 'attempts': 0, 'evidence': None}]}


@pytest.mark.parametrize('urls', [[], A, [A, A], ['https://www.wsdisplay.com/OTHER'],
                                      [A + '/'], [A, 3], [True], [A] * 1001])
def test_invalid_or_cross_job_selectors_fail_without_mutation(urls):
    job = njob()
    before = stable(job)
    with pytest.raises(ValueError):
        resume_scope(job, urls)
    assert stable(job) == before


def test_exact_membership_job_order_and_immutable_detached_scope():
    job = njob()
    selector = [B, A]
    selected = resume_scope(job, selector)
    assert selected == (A, B)
    selector[:] = ['changed']
    assert selected == (A, B)
    job['pages'].append(deepcopy(job['pages'][0]))
    with pytest.raises(ValueError):
        resume_scope(job, [A])


@pytest.mark.parametrize('limit', [True, 2, 1, 0, -1, 8193, 3.1, '4'])
def test_invalid_or_non_increasing_budget_is_not_admitted(limit):
    with pytest.raises(ValueError):
        resume_scope(njob(), [A], max_nodes_per_product=limit, node_limit_max=native.MAX_NODES)


def test_budget_requires_selection_existing_bound_and_room_for_current_counts():
    job = njob()
    with pytest.raises(ValueError):
        resume_scope(job, max_nodes_per_product=3, node_limit_max=native.MAX_NODES)
    with pytest.raises(ValueError):
        resume_scope(job, [A], max_nodes_per_product=3)
    job['pages'][0]['nodes'] = 4
    with pytest.raises(ValueError):
        resume_scope(job, [A], max_nodes_per_product=4, node_limit_max=native.MAX_NODES)
    assert resume_scope(job, [A], max_nodes_per_product=8192, node_limit_max=native.MAX_NODES) == (A,)
    assert native.MAX_NODES == 8192


class NativeSession:
    opened = []

    def __init__(self, page, limiter, url, marker, check):
        self.url = url
        self.counts = {'guarded_gets': 0, 'blocked_mutations': 0}
        self.blocked_requests = []
        self.observed_item_requests = []
        self.pms = 'F'
        self.last_readiness = {}

    async def open(self):
        self.opened.append(self.url)

    async def snapshot(self, **kwargs):
        self.last_readiness = {'url': self.url, 'ready': True, 'challenge': False,
                'login_prompt': False, 'account_marker': 'My Account SYNTHETIC BUSINESS',
                'fields': [{'key': 'custcol_pms_match', 'label': 'Use PMS Match',
                            'kind': 'select', 'selected': self.pms, 'disabled': False,
                            'choices': [{'value': v, 'label': v, 'disabled': False} for v in ['F', 'T']]}],
                'required_text': ([{'key': 'custcol_pms_number', 'label': 'PMS Number/s', 'required': True}]
                                  if self.pms == 'T' else []),
                'sku': 'SYN-A', 'quantity': 1, 'quantity_min': '1',
                'stock': 5, 'production': {'id': '12', 'label': 'Standard 3 Business Days Turnaround'},
                'packing': {}, 'artwork_controls_present': True}
        return self.last_readiness

    async def choose(self, key, value, retained):
        assert key == 'custcol_pms_match' and value == 'T'
        self.pms = value
        return await self.snapshot()

    async def quantity(self, *args, **kwargs):
        raise AssertionError('A selected resume must not reset quantity admission')

    quantity_two = quantity

    def diagnostic(self):
        return {}

    async def close(self):
        pass


class Pool:
    def page(self, *args, **kwargs):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def context():
            yield object()
        return context()


@pytest.mark.asyncio
async def test_selected_held_native_page_continues_exact_frontier_and_audits_budget(tmp_path, monkeypatch):
    NativeSession.opened = []
    monkeypatch.setattr(native, 'NativeSession', NativeSession)
    collector = native.WsdisplaySourceCollector(Store(tmp_path), Pool())
    job = njob()
    collector.save(job)
    before = deepcopy(collector.load(job['id']))
    result = await collector.resume(job['id'], urls=[B], max_nodes_per_product=3)
    task = collector.tasks[job['id']]
    await task
    saved = collector.load(job['id'])
    assert NativeSession.opened == [B]
    assert stable(saved['pages'][0]) == stable(before['pages'][0])
    assert stable(saved['pages'][2]) == stable(before['pages'][2])
    target = saved['pages'][1]
    assert target['nodes'] == 3 and target['status'] == 'completed'
    assert target['seen'] == before['pages'][1]['seen'] + [native.digest({'custcol_pms_match': 'T'})]
    assert target['frontier'] == []
    assert target['basis_witnesses'] == before['pages'][1]['basis_witnesses']
    assert target['q2_reservations'] == before['pages'][1]['q2_reservations']
    assert len(target['receipts']) == 1 and target['receipts'][0]['holds'] == ['required_text_service']
    assert saved['max_nodes_per_product'] == 2 and saved['native_q2_checks'] is False
    assert saved['status'] == 'attention'  # unselected held A is still unfinished.
    assert saved['resume_history'][-1]['urls'] == [B]
    assert saved['resume_history'][-1]['requested_max_nodes_per_product'] == 3
    assert saved['resume_history'][-1]['original_job_max_nodes_per_product'] == 2
    await collector.close()


@pytest.mark.asyncio
async def test_selected_resume_without_explicit_budget_does_not_raise_original_cap(tmp_path, monkeypatch):
    NativeSession.opened = []
    monkeypatch.setattr(native, 'NativeSession', NativeSession)
    collector = native.WsdisplaySourceCollector(Store(tmp_path), Pool())
    job = njob()
    collector.save(job)
    await collector.resume(job['id'], urls=[B])
    await collector.tasks[job['id']]
    saved = collector.load(job['id'])
    assert saved['pages'][1]['nodes'] == 2
    assert saved['pages'][1]['error'] == 'native_frontier_bound'
    assert saved['pages'][1]['receipts'] == []
    assert saved['max_nodes_per_product'] == 2
    await collector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['public', 'native'])
async def test_invalid_selector_and_completed_selection_never_queue_or_write(tmp_path, kind):
    collector = (public.SourceCollector(Store(tmp_path)) if kind == 'public'
                 else native.WsdisplaySourceCollector(Store(tmp_path), Pool()))
    job = pjob() if kind == 'public' else njob()
    if kind == 'public':
        job['pages'][1]['status'] = 'completed'
        collector._save(job)
        path = collector._path(job['id'])
        completed = Q
    else:
        collector.save(job)
        path = collector.path(job['id'])
        completed = C
    before = path.read_bytes()
    for urls in [[completed, completed], ['https://www.b2sign.com/OTHER-JOB']]:
        with pytest.raises(ValueError):
            await collector.resume(job['id'], urls=urls)
    await collector.resume(job['id'], urls=[completed])
    assert path.read_bytes() == before
    assert not (collector._tasks if kind == 'public' else collector.tasks)
    await collector.close()


@pytest.mark.asyncio
async def test_public_subset_preserves_unselected_failed_page_and_global_status(tmp_path, monkeypatch):
    collector = public.SourceCollector(Store(tmp_path), http_transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError('fetch must stay in synthetic adapter'))))
    job = pjob()
    collector._save(job)
    before = stable(job['pages'][0])
    called = []

    async def fetch(j, page, client):
        called.append(page['url'])
        page['attempts'] += 1
        return {'sha256': 'c' * 64}

    monkeypatch.setattr(collector, '_fetch', fetch)
    await collector.resume(job['id'], urls=[Q])
    await collector._tasks[job['id']]
    saved = collector._load(job['id'])
    assert called == [Q]
    assert stable(saved['pages'][0]) == before
    assert saved['pages'][1]['status'] == 'completed' and saved['status'] == 'attention'
    assert saved['resume_history'][-1]['urls'] == [Q]
    await collector.resume(job['id'])
    await collector._tasks[job['id']]
    assert called == [Q, P]
    assert collector._load(job['id'])['status'] == 'completed'
    await collector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['public', 'native'])
async def test_active_owner_rejects_narrower_scope_without_hijacking(tmp_path, monkeypatch, kind):
    collector = (public.SourceCollector(Store(tmp_path)) if kind == 'public'
                 else native.WsdisplaySourceCollector(Store(tmp_path), Pool()))
    job = pjob() if kind == 'public' else njob()
    save = collector._save if kind == 'public' else collector.save
    save(job)
    urls = [P] if kind == 'public' else [A]
    owner = '_owner_active' if kind == 'public' else 'active'
    monkeypatch.setattr(collector, owner, lambda jid: True)
    with pytest.raises(ValueError, match='Pause'):
        await collector.resume(job['id'], urls=urls)
    tasks = collector._tasks if kind == 'public' else collector.tasks
    assert not tasks
    monkeypatch.setattr(collector, owner, lambda jid: False)
    blocked = asyncio.create_task(asyncio.Event().wait())
    tasks[job['id']] = blocked
    with pytest.raises(ValueError, match='Pause'):
        await collector.resume(job['id'], urls=urls)
    await collector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['public', 'native'])
async def test_under_lock_membership_is_revalidated_before_any_write(tmp_path, kind):
    collector = (public.SourceCollector(Store(tmp_path)) if kind == 'public'
                 else native.WsdisplaySourceCollector(Store(tmp_path), Pool()))
    job = pjob() if kind == 'public' else njob()
    save = collector._save if kind == 'public' else collector.save
    path = collector._path(job['id']) if kind == 'public' else collector.path(job['id'])
    save(job)
    before = path.read_bytes()
    run = collector._run if kind == 'public' else collector.run
    with pytest.raises(ValueError):
        await run(job['id'], urls=('https://www.b2sign.com/ANOTHER-JOB',))
    assert path.read_bytes() == before
    await collector.close()


@pytest.mark.asyncio
async def test_profile_router_does_not_drop_scope_or_budget(tmp_path):
    router = SourceCollectionProfiles(Store(tmp_path), Pool())
    job = njob()
    router.native.save(job)
    called = []

    async def resume(jid, **options):
        called.append((jid, options))
        return {'id': jid}

    router.native.resume = resume
    await router.resume(job['id'])
    await router.resume(job['id'], urls=[B], max_nodes_per_product=3)
    assert called == [(job['id'], {}), (job['id'], {'urls': [B], 'max_nodes_per_product': 3})]
    await router.close()


def test_authenticated_api_optional_body_and_strict_selected_budget(tmp_path):
    code = r'''
import asyncio
import httpx
from crawler import app as api
class Fake:
    def __init__(self): self.calls=[]
    async def resume(self,jid,**kw):
        self.calls.append(kw)
        if kw.get('urls') in [['https://unknown.test/'], ['A','A']]: raise ValueError('invalid selection')
        return {'id':jid,'status':'attention'}
f=Fake();api.app.state.source_collections=f
async def main():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test') as client:
        url='/api/v1/source-collections/'+'a'*32+'/resume'
        assert (await client.post(url,json={'urls':['A']})).status_code==401
        h={'Authorization':'Bearer SYNTHETIC'}
        for kwargs in [{},{'json':None},{'json':{}},{'json':{'urls':None}}]:
            assert (await client.post(url,headers=h,**kwargs)).status_code==200
            assert f.calls[-1]=={}
        r=await client.post(url,headers=h,json={'urls':['A'],'max_nodes_per_product':3})
        assert r.status_code==200,r.text
        assert f.calls[-1]=={'urls':['A'],'max_nodes_per_product':3}
        for data in [{'urls':[]},{'urls':[3]},{'urls':['A'],'extra':True},
                     {'max_nodes_per_product':3},{'urls':['A'],'max_nodes_per_product':True},
                     {'urls':['A'],'max_nodes_per_product':8193}]:
            assert (await client.post(url,headers=h,json=data)).status_code==422
        for data in [{'urls':['A','A']},{'urls':['https://unknown.test/']}]:
            assert (await client.post(url,headers=h,json=data)).status_code==409
    api.store.engine.dispose()
asyncio.run(main());print('TARGETED API PASS')
'''
    env = {key: value for key, value in os.environ.items()
           if not (key.startswith('AWS_') or key in {'DATABASE_URL', 'ATLAS_DATA_DIR', 'ATLAS_S3_BUCKET', 'ATLAS_S3_ENDPOINT'})}
    env.update(ATLAS_DATA_DIR=str(tmp_path / 'api'), ATLAS_API_TOKEN='SYNTHETIC', ATLAS_S3_BUCKET='',
               PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-B', '-c', code], env=env,
                            text=True, capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'TARGETED API PASS' in result.stdout
