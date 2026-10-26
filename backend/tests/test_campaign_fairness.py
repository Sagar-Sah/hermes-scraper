"""Actual queue with fake handlers/private temp files; no runtime or source."""
import asyncio,hashlib,importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('fairness_queue',ROOT/'backend/crawler/campaign_automation.py');m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
def assoc(pid,supplier='a',campaign='campaign'):
 return {'campaign_id':campaign,'product_id':pid,'supplier':supplier,'source_url':'https://fixture.invalid/'+pid}
def continuing(job):
 n=job['progress_count']+1
 return {'state':'continue','result_reference':'receipts/'+job['product_id']+'.json','progress_count':n,'progress_token':hashlib.sha256(str(n).encode()).hexdigest()}
class FairnessTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.runners=[];self.now=1000.;self.rows={'campaign':[assoc('one'),assoc('two')]}
 async def asyncTearDown(self):
  for runner in self.runners:
   if not runner._owner.closed:await runner.close()
  self.tmp.cleanup()
 def make(self,execute,poll=None,capacity=2,root=None):
  profiles=[m.Profile(s,s,lambda a:True,lambda a,n:{'configurations':[{'scope':'native'}],'blockers':[]},lambda c:c,execute,poll) for s in ('a','b')]
  runner=m.CampaignAutomation(root or self.tmp.name,profiles,lambda cid:self.rows[cid],clock=lambda:self.now,capacity=capacity)
  self.runners.append(runner);return runner
 async def start(self,runner,cid='campaign'):
  plan=await runner.plan(cid);await runner.start(cid,plan['plan_id']);return plan
 async def finish_current(self,runner):
  # Await fake handler completion without dispatching a following chunk.
  await asyncio.gather(*(task for _,task in runner._active.values()))
 async def test_constant_and_rollback_clock_continuation_yields_to_other_product(self):
  calls=[]
  async def execute(job,check):await check();calls.append(job['product_id']);return continuing(job)
  runner=self.make(execute);await self.start(runner)
  for _ in range(4):
   await runner.tick();await self.finish_current(runner)
  self.assertEqual(calls[:2],list(dict.fromkeys(calls)))
  self.assertEqual(calls[:2],calls[2:4]);self.assertNotEqual(calls[0],calls[1])
  # A rollback changes throttle eligibility, not persisted round-robin order.
  self.now-=10
  await runner.tick();self.assertEqual(len(calls),4)
  # Existing source pacing holds until its saved deadline even on rollback.
  # Once eligible again, the persisted round-robin order remains unchanged.
  self.now+=10
  await runner.tick();await self.finish_current(runner)
  self.assertEqual(calls[4],calls[0])
  sequences=sorted(j['last_dispatch_sequence'] for j in runner._records['campaign']['jobs'].values())
  self.assertEqual(sequences,[4,5])
 async def test_not_before_waiting_and_auth_remain_admission_gates(self):
  calls=[];polls=[]
  async def execute(job,check):
   await check();calls.append(job['product_id'])
   if job['supplier']=='b':return {'state':'completed','result_reference':'receipts/b.json'}
   return {'state':'pending','pending_token':{'job_id':'a'*32}}
  async def poll(job,check):await check();polls.append(job['product_id']);return {'state':'access_required','reason':'login_required'}
  self.rows['campaign']=[assoc('one'),assoc('three','b')]
  runner=self.make(execute,poll);await self.start(runner);await runner.tick();await self.finish_current(runner)
  await runner.tick();self.assertEqual(polls,[])
  self.now+=3;runner._records['campaign']['supplier_not_before']['a']=self.now+5
  await runner.tick();self.assertEqual(polls,[])
  self.now+=5;await runner.tick();await self.finish_current(runner)
  self.assertEqual(polls,['one'])
  runner._records['campaign']['supplier_not_before']['a']=0
  await runner.tick();self.assertEqual(polls,['one'])
  self.assertEqual(runner._records['campaign']['supplier_blocks'],{'a':'supplier_authentication_required'})
  self.assertEqual(set(calls),{'one','three'})
 async def test_other_vendor_parallel_and_same_vendor_never_overlap(self):
  calls=[];gate=asyncio.Event();active={};peak={}
  async def execute(job,check):
   await check();s=job['supplier'];active[s]=active.get(s,0)+1;peak[s]=max(peak.get(s,0),active[s]);calls.append(job['product_id'])
   try:
    if s=='b':await gate.wait();return {'state':'completed','result_reference':'receipts/b.json'}
    return continuing(job)
   finally:active[s]-=1
  self.rows['campaign']=[assoc('one'),assoc('two'),assoc('three','b')]
  runner=self.make(execute,capacity=2);await self.start(runner);await runner.tick()
  for _ in range(5):await asyncio.sleep(0)
  self.assertIn('three',calls);self.assertEqual(len(calls),2)
  a_first=next(v for v in calls if v!='three')
  await runner.tick()
  for _ in range(5):await asyncio.sleep(0)
  self.assertEqual(len(calls),3);self.assertNotEqual(calls[-1],a_first)
  self.assertEqual(peak,{'a':1,'b':1})
  gate.set();await self.finish_current(runner)
 async def test_restart_uses_saved_sequence_even_with_constant_clock(self):
  calls=[]
  async def execute(job,check):await check();calls.append(job['product_id']);return continuing(job)
  runner=self.make(execute);await self.start(runner);await runner.tick();await self.finish_current(runner);await runner.close()
  file=Path(self.tmp.name)/'campaign-automation/campaign.json';saved=json.loads(file.read_text());old=next(j for j in saved['jobs'].values() if j.get('last_dispatch_sequence')==1)
  self.assertEqual(old['status'],'queued');self.assertEqual(old['progress_count'],1)
  again=self.make(execute);await again.tick();await self.finish_current(again)
  self.assertEqual(len(calls),2);self.assertNotEqual(calls[0],calls[1])
  self.assertEqual(sorted(j.get('last_dispatch_sequence',0) for j in again._records['campaign']['jobs'].values()),[1,2])
  self.assertEqual(old['result_reference'],again._records['campaign']['jobs'][old['id']]['result_reference'])
 async def test_eligible_work_in_later_campaign_is_not_starved(self):
  calls=[]
  async def execute(job,check):await check();calls.append(job['association']['campaign_id']);return continuing(job)
  self.rows={'alpha':[assoc('one',campaign='alpha')],'beta':[assoc('two',campaign='beta')]}
  runner=self.make(execute,capacity=1);await self.start(runner,'alpha');await self.start(runner,'beta')
  for _ in range(4):await runner.tick();await self.finish_current(runner)
  self.assertEqual(calls,['alpha','beta','alpha','beta'])
 async def test_invalid_or_exhausted_persisted_sequence_does_not_invoke_source(self):
  for bad in (True,-1,'1',m.MAX_DISPATCH_SEQUENCE+1):
   with self.subTest(value=bad),tempfile.TemporaryDirectory() as directory:
    calls=[]
    async def execute(job,check):calls.append(1);return continuing(job)
    runner=self.make(execute,root=directory)
    await self.start(runner);job=next(iter(runner._records['campaign']['jobs'].values()));job['last_dispatch_sequence']=bad
    with self.assertRaises(ValueError):await runner.tick()
    self.assertEqual(calls,[]);await runner.close()
if __name__=='__main__':unittest.main(verbosity=2)
