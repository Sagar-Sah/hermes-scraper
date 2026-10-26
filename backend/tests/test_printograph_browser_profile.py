import asyncio
from copy import deepcopy
import json
from pathlib import Path
import pytest
from crawler.printograph_browser_profile import BrowserRunProfile,PROFILE_ID,MODE,supports,discover,normalize_configuration,job_spec
ASSOCIATION={'campaign_id':'new-repair','product_id':'18105','supplier':'printograph','source_url':'https://www.printograph.com/products/catalogs/order'}
TASK={'association':ASSOCIATION,'configuration':{'mode':MODE}}
class Fake:
 def __init__(self):self.runs=[];self.creates=0;self.controls=[];self.t=0;self.reads=0;self.mode='complete';self.failcreate=False
 async def create(self,spec):
  self.creates+=1;run={'id':'a'*32,'status':'paused','spec':deepcopy(spec),'row_count':0,'scoped_row_count':None,'coverage_status':'unknown','products':[{'url':spec['urls'][0],'status':'queued','coverage_status':'unknown'}]};self.runs.append(run)
  if self.failcreate:raise RuntimeError('unknown creation outcome')
  return deepcopy(run)
 async def find(self,owner):return [deepcopy(r) for r in self.runs if r['spec']['source_metadata']['automation_owner']==owner]
 async def read(self,run_id):
  self.reads+=1;r=self.runs[0]
  if self.mode=='complete':r.update(status='completed',row_count=max(2,r.get('row_count',0)),scoped_row_count=max(2,r.get('row_count',0)),coverage_status='complete');r['products'][0].update(status='completed',coverage_status='complete')
  elif self.mode=='partial':r.update(status='completed_with_gaps',row_count=2,scoped_row_count=2,coverage_status='partial');r['products'][0].update(status='partial',coverage_status='partial')
  elif self.mode=='auth':r.update(status='completed_with_gaps',row_count=0,scoped_row_count=0);r['products'][0]['status']='access_required'
  elif self.mode=='advancing':r.update(status='running' if r['status']!='paused' else 'paused',row_count=self.reads,scoped_row_count=self.reads)
  elif self.mode=='invalid_scope':r['spec']['urls']=['https://evil.invalid'];r['status']='running'
  return deepcopy(r)
 async def control(self,run_id,action):self.controls.append((run_id,action));self.runs[0]['status']='paused' if action=='pause' else 'queued';return deepcopy(self.runs[0])
 async def sleep(self,seconds):self.t+=seconds
 def clock(self):return self.t
 def profile(self,tmp):return BrowserRunProfile(tmp,create=self.create,read=self.read,control=self.control,find_owned=self.find,clock=self.clock,sleep=self.sleep,chunk_seconds=1,poll_seconds=.5)
async def check():pass

def test_discover_exact_trusted_source_and_no_ai_spec():
 assert supports(ASSOCIATION) and discover(ASSOCIATION)=={'configurations':[{'mode':MODE}],'blockers':[]}
 assert not supports({**ASSOCIATION,'source_url':ASSOCIATION['source_url']+'?account=private'})
 assert not supports({**ASSOCIATION,'supplier':'other'})
 assert not supports({**ASSOCIATION,'extra':'private'})
 spec=job_spec(ASSOCIATION,'a'*64)
 assert spec['adapter']=='browser' and spec['source_metadata']['printograph_modern_services'] is True
 assert spec['provider_id'] is None and spec['instructions']=='' and spec['shipping_contexts']==[]
 with pytest.raises(ValueError):normalize_configuration({'mode':MODE,'price':1})
@pytest.mark.asyncio
async def test_success_same_owner_no_duplicate_run(tmp_path):
 f=Fake();p=f.profile(tmp_path);a=await p.execute(TASK,check);b=await p.execute(TASK,check)
 assert a['state']==b['state']=='completed' and f.creates==1 and a==b
 path,digest=a['result_reference'].split('#');value=json.loads(Path(path).read_bytes())
 assert value['harvest_id']=='a'*32 and not value['source_data_finalized'] and not value['woocommerce_import_ready']
 assert f.controls==[('a'*32,'resume')]
@pytest.mark.asyncio
@pytest.mark.parametrize('mode,state',[('partial','held'),('auth','access_required')])
async def test_partial_and_auth_never_complete(tmp_path,mode,state):
 f=Fake();f.mode=mode;r=await f.profile(tmp_path).execute(TASK,check)
 assert r['state']==state and f.creates==1
@pytest.mark.asyncio
async def test_advancing_chunk_reuses_same_owned_run(tmp_path):
 f=Fake();f.mode='advancing';p=f.profile(tmp_path);one=await p.execute(TASK,check)
 assert one['state']=='continue' and one['progress_count']>0 and ('a'*32,'pause') in f.controls
 f.mode='complete';two=await p.execute(TASK,check)
 assert two['state']=='completed' and f.creates==1 and ('a'*32,'resume') in f.controls
@pytest.mark.asyncio
async def test_scope_revocation_before_create_has_no_child(tmp_path):
 f=Fake();f.mode='idle';p=f.profile(tmp_path);checks=0
 async def cancel():
  nonlocal checks;checks+=1
  if checks==3:raise asyncio.CancelledError()
 with pytest.raises(asyncio.CancelledError):await p.execute(TASK,cancel)
 assert f.controls==[] and f.creates==0
@pytest.mark.asyncio
async def test_unknown_creation_outcome_looked_up_never_created_again(tmp_path):
 f=Fake();f.failcreate=True;p=f.profile(tmp_path)
 with pytest.raises(RuntimeError):await p.execute(TASK,check)
 f.failcreate=False;r=await p.execute(TASK,check)
 assert r['state']=='held' and r['reason']=='browser_owned_run_paused' and f.creates==1
 assert ('a'*32,'pause') in f.controls and ('a'*32,'resume') not in f.controls
@pytest.mark.asyncio
async def test_invalid_changed_saved_scope_pauses_without_completing(tmp_path):
 f=Fake();f.mode='invalid_scope'
 with pytest.raises(ValueError,match='scope_changed'):await f.profile(tmp_path).execute(TASK,check)
 assert ('a'*32,'pause') in f.controls
@pytest.mark.asyncio
async def test_no_progress_chunk_held_not_spinning(tmp_path):
 f=Fake();f.mode='idle';r=await f.profile(tmp_path).execute(TASK,check)
 assert r=={'state':'held','reason':'browser_chunk_no_durable_progress'} and f.creates==1

@pytest.mark.asyncio
async def test_real_queue_bridge_plans_runs_once_and_reuses_completion(tmp_path):
 from crawler.campaign_automation import CampaignAutomation
 f=Fake();driver=f.profile(tmp_path/'receipts')
 queue=CampaignAutomation(tmp_path/'automation',[driver.profile()],lambda campaign:[deepcopy(ASSOCIATION)])
 try:
  plan=await queue.plan('new-repair');assert len(plan['jobs'])==1 and not plan['blockers']
  await queue.start('new-repair',plan['plan_id'])
  for _ in range(20):
   await queue.tick();await asyncio.sleep(.01)
   if queue.snapshot('new-repair')['summary']['completed']:break
  assert queue.snapshot('new-repair')['summary']['completed']==1 and f.creates==1
  await queue.start('new-repair',plan['plan_id']);await queue.tick();await asyncio.sleep(.01)
  assert f.creates==1 and queue.snapshot('new-repair')['summary']['completed']==1
 finally:await queue.close()

@pytest.mark.asyncio
async def test_parent_cancel_after_activation_pauses_unique_owned_child(tmp_path):
 f=Fake();f.mode='idle';p=f.profile(tmp_path);checks=0
 async def cancel():
  nonlocal checks;checks+=1
  if checks==6:raise asyncio.CancelledError()
 with pytest.raises(asyncio.CancelledError):await p.execute(TASK,cancel)
 assert f.creates==1 and f.controls==[('a'*32,'resume'),('a'*32,'pause')]

@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['cancel','exception'])
async def test_create_fault_after_durable_commit_finds_and_pauses_owned_child(tmp_path,fault):
 f=Fake();p=f.profile(tmp_path)
 async def create(spec):
  await f.create(spec)
  if fault=='cancel':raise asyncio.CancelledError()
  raise RuntimeError('synthetic-after-commit')
 p.create=create
 with pytest.raises(asyncio.CancelledError if fault=='cancel' else RuntimeError):await p.execute(TASK,check)
 assert f.creates==1 and f.controls==[('a'*32,'pause')]
 assert f.runs[0]['status']=='paused'

@pytest.mark.asyncio
async def test_scope_revoked_inside_owner_lookup_never_creates(tmp_path):
 f=Fake();p=f.profile(tmp_path);revoked=False
 async def scope():
  if revoked:raise asyncio.CancelledError()
 async def find(owner):
  nonlocal revoked;revoked=True
  return []
 p.find=find
 with pytest.raises(asyncio.CancelledError):await p.execute(TASK,scope)
 assert f.creates==0 and f.controls==[]

@pytest.mark.asyncio
async def test_create_queued_child_contract_violation_paused_and_held(tmp_path):
 f=Fake();p=f.profile(tmp_path)
 async def queued(spec):
  result=await f.create(spec);f.runs[0]['status']='queued';result['status']='queued';return result
 p.create=queued
 with pytest.raises(ValueError,match='not_created_paused'):await p.execute(TASK,check)
 assert f.controls==[('a'*32,'pause')]
