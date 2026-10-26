"""Source-free regression for bounded chunks and exact historical skip lookup.

Callbacks simulate ordered saved work, not source prices or native UI acceptance.
"""
import ast
import asyncio
from copy import deepcopy
import hashlib
import itertools
import json
from pathlib import Path
import re
import socket

import pytest

from crawler.printograph_browser_profile import BrowserRunProfile, PROFILE_ID, MODE, job_spec

CRAWLER = Path(__file__).resolve().parents[1] / 'crawler'
ASSOCIATION = {'campaign_id':'synthetic-performance', 'product_id':'18105',
               'supplier':'printograph', 'source_url':'https://www.printograph.com/products/catalogs/order'}
TASK = {'association':ASSOCIATION, 'configuration':{'mode':MODE}}


@pytest.fixture(autouse=True)
def no_remote_connections(monkeypatch):
    original=socket.socket.connect
    def guarded(sock,address):
        if sock.family in {socket.AF_INET,socket.AF_INET6}:
            raise AssertionError('These deterministic fixtures must not connect to any remote service')
        return original(sock,address)
    monkeypatch.setattr(socket.socket,'connect',guarded)
    monkeypatch.setattr(socket.socket,'connect_ex',guarded)


def detached_functions(*names):
    tree = ast.parse((CRAWLER / 'supplier_profile_executor.py').read_text())
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    contract = ast.parse((CRAWLER / 'price_contract.py').read_text())
    literals = {t.id:ast.literal_eval(n.value) for n in contract.body if isinstance(n,ast.Assign)
                for t in n.targets if isinstance(t,ast.Name) and t.id in
                {'SPECIAL_SHAPES_SOURCE_CONTRACT','STAGGERED_CUT_SOURCE_URL'}}
    namespace = {'Path':Path, 'json':json, 'hashlib':hashlib,
                 '_NUM':re.compile(r'[1-9][0-9]{0,11}\Z'), 'LEVELS':('size','paper','color','coating'),
                 'PROOFS':('36','1'), 'MAX_STAGE_QUANTITIES':64,
                 'CONTRACT':literals['SPECIAL_SHAPES_SOURCE_CONTRACT'],
                 'SOURCE_URL':literals['STAGGERED_CUT_SOURCE_URL']}
    exec(compile(ast.Module(body=selected,type_ignores=[]), '<pure historical skip functions>', 'exec'), namespace)
    return namespace


def linear_reference(spent, branch, quantity, proof='36'):
    return any(tuple(row[:3])==tuple(branch[:3]) and row[3] in {'*',branch[3]}
               and row[4]==quantity and row[5] in {'*',proof} for row in spent)


@pytest.mark.parametrize('coating,proof', list(itertools.product(['','5','*',None],['36','1','*',None])))
def test_four_membership_equals_previous_predicate_including_unknowns(coating,proof):
    fn = detached_functions('selection_spent')['selection_spent']
    rows = {('1','2','3',c,q,p) for c,q,p in itertools.product(['','5','*',None],['250','500',None],['36','1','*',None])}
    groups = [set(), rows, {('1','2','3','*','250','*')},
              {('1','2','3','','250','36')}, {('9','2','3','*','250','*')}]
    for spent,quantity in itertools.product(groups,['250','500','999',None]):
        branch=('1','2','3',coating)
        assert fn(spent,branch,quantity,proof) == linear_reference(spent,branch,quantity,proof)


@pytest.mark.parametrize('bad_rows',[
    [['1','2','3','','250']], [['1','2','3','','250',None]],
    [['1','2','3','','','36']], [['1','2','3','*','250','36']], None,
])
def test_uncertain_legacy_scope_and_malformed_receipts_still_hold(tmp_path,bad_rows):
    ns=detached_functions('require','spent_selections','selection_spent')
    journal=tmp_path/'printograph-price-journals';journal.mkdir()
    (journal/'legacy.json').write_text(json.dumps({'source_url':ns['SOURCE_URL'],'epochs':[], 'events':[
        {'request':{'kind':'product_identity','numeric_query':{'productType':'38','size':'1','paper':'2','color':'3'}},
         'projection':{'kind':'product_identity','product_id':'99'}},
        {'epoch':1,'request':{'kind':'base_price','product_id':'99'}}]}))
    exact,unknown=ns['spent_selections'](tmp_path)
    assert exact==set() and unknown=={('1','2','3')}
    assert not ns['selection_spent'](exact,('1','2','3',''),'250')
    receipts=tmp_path/'supplier-profile-spent';receipts.mkdir()
    broken={'schema':'supplier-profile-spent/v1','source_url':ns['SOURCE_URL'],'source_contract':ns['CONTRACT'],
            'result_sha256':'a'*64,'selections':bad_rows}
    raw=json.dumps(broken).encode();(receipts/(hashlib.sha256(raw).hexdigest()+'.json')).write_bytes(raw)
    with pytest.raises(ValueError,match='spent_ledger_invalid'):ns['spent_selections'](tmp_path)


class SavedBackend:
    """Deterministic backend callbacks with finite steps and per-activation startup."""
    def __init__(self, *, total=120, initial=0, startup=10, replay_per_saved=.08):
        self.t=0.;self.total=total;self.saved=list(range(initial));self.permits=[]
        self.startup=startup;self.replay_per_saved=replay_per_saved;self.run=None
        self.active=False;self.next_step=None;self.controls=[];self.activations=[];self.creates=0
        self.auth=False
    async def create(self,spec):
        self.creates+=1;self.run={'id':'a'*32,'status':'paused','spec':deepcopy(spec),
          'row_count':len(self.saved),'scoped_row_count':len(self.saved),'coverage_status':'unknown',
          'products':[{'url':spec['urls'][0],'status':'queued','coverage_status':'unknown'}]}
        return deepcopy(self.run)
    async def find(self,owner):
        return [deepcopy(self.run)] if self.run and self.run['spec']['source_metadata']['automation_owner']==owner else []
    async def control(self,rid,action):
        assert rid==self.run['id'];self.controls.append((action,self.t))
        if action=='resume':
            self.active=True;self.run['status']='running';self.activations.append(self.t)
            self.next_step=self.t+self.startup+len(self.saved)*self.replay_per_saved+1.8
        else:self.active=False;self.run['status']='paused'
        return deepcopy(self.run)
    async def read(self,rid):
        assert rid==self.run['id']
        if self.auth:
            self.run['status']='completed_with_gaps';self.run['products'][0]['status']='access_required'
            return deepcopy(self.run)
        while self.active and self.t>=self.next_step and len(self.saved)<self.total:
            selection=len(self.saved)
            assert selection not in self.saved and selection not in self.permits
            self.permits.append(selection);self.saved.append(selection);self.next_step+=1.8
        self.run.update(row_count=len(self.saved),scoped_row_count=len(self.saved))
        if len(self.saved)==self.total:
            self.active=False;self.run.update(status='completed',coverage_status='complete')
            self.run['products'][0].update(status='completed',coverage_status='complete')
        return deepcopy(self.run)
    def clock(self):return self.t
    async def sleep(self,seconds):self.t+=seconds
    def driver(self,root,seconds):
        return BrowserRunProfile(root,create=self.create,read=self.read,control=self.control,find_owned=self.find,
                                 clock=self.clock,sleep=self.sleep,chunk_seconds=seconds,poll_seconds=.5)


async def drive_to_completion(root,seconds,**kwargs):
    backend=SavedBackend(**kwargs);driver=backend.driver(root,seconds)
    if backend.saved:
        owner=hashlib.sha256(json.dumps({'profile':PROFILE_ID,'association':ASSOCIATION,'configuration':TASK['configuration']},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        await backend.create(job_spec(ASSOCIATION,owner));driver._write(root/(owner+'.owner.json'),
          {'owner':owner,'association':ASSOCIATION,'run_id':backend.run['id'],'rows':len(backend.saved),'intent':'paused'})
    async def check():pass
    results=[]
    for _ in range(20):
        result=await driver.execute(TASK,check);results.append(result)
        if result['state']=='completed':break
        assert result['state']=='continue'
    assert results[-1]['state']=='completed' and backend.saved==list(range(backend.total))
    for i,(action,t) in enumerate(backend.controls):
        if action=='resume':
            next_pause=next((t2 for a2,t2 in backend.controls[i+1:] if a2=='pause'),None)
            if next_pause is not None:assert next_pause-t<=seconds+.5
    return backend,results


@pytest.mark.asyncio
@pytest.mark.parametrize('startup,initial',[(10,0),(0,0),(10,24)])
async def test_bounded_chunks_preserve_cold_warm_and_resumed_order(tmp_path,startup,initial):
    old,a=await drive_to_completion(tmp_path/'ninety',90,startup=startup,initial=initial)
    new,b=await drive_to_completion(tmp_path/'threehundred',300,startup=startup,initial=initial)
    assert old.permits==new.permits==list(range(initial,120))
    assert len(new.activations)<len(old.activations) and len(b)<len(a)
    assert old.creates==new.creates==1
    # Both complete exactly the same scoped work; no source-completion promotion.
    for result in (a[-1],b[-1]):
        path=result['result_reference'].split('#')[0];receipt=json.loads(Path(path).read_text())
        assert receipt['scoped_row_count']==120 and receipt['source_data_finalized'] is False


@pytest.mark.asyncio
async def test_product_yields_by_three_hundred_and_cancel_revokes_permits(tmp_path):
    f=SavedBackend(total=10000);driver=f.driver(tmp_path/'budget',300)
    async def check():pass
    result=await driver.execute(TASK,check)
    assert result['state']=='continue' and f.t<=300.5 and f.controls[-1][0]=='pause'
    g=SavedBackend(total=10000);driver=g.driver(tmp_path/'cancel',300)
    async def cancel():
        if g.t>=17:raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):await driver.execute(TASK,cancel)
    saved=list(g.permits);await g.sleep(100);assert not g.active and g.permits==saved
    again=await driver.execute(TASK,check)
    assert again['state']=='held' and again['reason']=='browser_owned_run_paused' and g.permits==saved


@pytest.mark.asyncio
async def test_three_hundred_without_progress_holds_and_does_not_retry(tmp_path):
    f=SavedBackend(startup=1000);driver=f.driver(tmp_path/'no-progress',300)
    async def check():pass
    result=await driver.execute(TASK,check)
    assert result=={'state':'held','reason':'browser_chunk_no_durable_progress'}
    assert f.t<=300.5 and f.permits==[] and not f.active


@pytest.mark.asyncio
async def test_access_guard_unchanged_and_bounds_still_reject(tmp_path):
    f=SavedBackend();f.auth=True
    async def check():pass
    result=await f.driver(tmp_path/'auth',300).execute(TASK,check)
    assert result['state']=='access_required' and f.permits==[]
    with pytest.raises(ValueError,match='chunk_bound'):f.driver(tmp_path/'bad',301)


def test_only_registered_modern_chunks_expand_and_checkpoint_inputs_exclude_edits():
    profile=ast.parse((CRAWLER/'printograph_browser_profile.py').read_text())
    constructor=next(n for n in ast.walk(profile) if isinstance(n,ast.FunctionDef) and n.name=='__init__')
    default=dict(zip([a.arg for a in constructor.args.kwonlyargs],constructor.args.kw_defaults))
    assert ast.literal_eval(default['chunk_seconds'])==90
    registry=ast.parse((CRAWLER/'campaign_automation_registry.py').read_text())
    calls=[n for n in ast.walk(registry) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='BrowserRunProfile']
    assert len(calls)==2 and all(next(ast.literal_eval(k.value) for k in c.keywords if k.arg=='chunk_seconds')==300 for c in calls)
    outer=next(n for n in ast.walk(registry) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='CampaignAutomation')
    assert next(ast.literal_eval(k.value) for k in outer.keywords if k.arg=='call_timeout')==600
    progress=ast.parse((CRAWLER/'browser_progress.py').read_text())
    hashes=next(n for n in progress.body if isinstance(n,ast.FunctionDef) and n.name=='adapter_semantics')
    inputs={x.value for x in ast.walk(hashes) if isinstance(x,ast.Constant) and isinstance(x.value,str)}
    assert {'campaign_automation_registry.py','supplier_profile_executor.py'}.isdisjoint(inputs)
