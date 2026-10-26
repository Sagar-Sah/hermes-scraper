"""Native held reasons reach the real scheduler without leaking unsafe values."""
import asyncio, json, tempfile, unittest
from crawler import campaign_automation as m, campaign_automation_bindings as b

ASSOC={'campaign_id':'fixture','product_id':'one','supplier':'4over','source_url':'https://supplier.invalid/product'}
def profile(raw):
 async def execute(a,c,*,check_scope):await check_scope();return raw
 return b.family_profile(profile_id='fixture_profile',supplier='4over',supports=lambda _:True,discover=lambda a,n:{'configurations':[{'scope':'fixture'}],'blockers':[]},normalize=lambda c:c,execute=execute)
class Tests(unittest.IsolatedAsyncioTestCase):
 async def direct(self,raw):
  async def check():pass
  return await profile(raw).execute({'association':ASSOC,'configuration':{'scope':'fixture'}},check)
 async def managed(self,raw):
  with tempfile.TemporaryDirectory() as directory:
   runner=m.CampaignAutomation(directory,[profile(raw)],lambda cid:[ASSOC],capacity=1)
   try:
    plan=await runner.plan('fixture');await runner.start('fixture',plan['plan_id']);await runner.tick();await asyncio.gather(*(task for _,task in runner._active.values()));await runner.tick();return runner.snapshot('fixture')
   finally:await runner.close()
 async def test_native_held_reason_code_is_preserved_and_precedes_legacy(self):
  for raw in ({'status':'held','reason_code':'native_summary_timeout'},{'status':'held','reason_code':'native_summary_timeout','reason':'held'}):
   self.assertEqual(await self.direct(raw),{'state':'held','reason':'native_summary_timeout'})
   state=await self.managed(raw);self.assertEqual(state['jobs'][0]['reason'],'native_summary_timeout');self.assertEqual(state['jobs'][0]['status'],'held')
 async def test_legacy_reason_is_preserved_when_code_absent_or_empty(self):
  for raw in ({'status':'held','reason':'source_context_unproven'},{'status':'held','reason':'source_context_unproven','reason_code':None},{'status':'held','reason':'source_context_unproven','reason_code':''}):self.assertEqual(await self.direct(raw),{'state':'held','reason':'source_context_unproven'})
 async def test_manager_redacts_unsafe_native_values_without_saving_private_source_text(self):
  for value in ('https://supplier.invalid/SECRET','SECRET personal source message',{'private':'SECRET'},['SECRET'],True,123):
   with self.subTest(value=value):
    state=await self.managed({'status':'held','reason_code':value});self.assertEqual(state['jobs'][0]['reason'],'profile_held');self.assertNotIn('SECRET',json.dumps(state))
 async def test_status_fallback_unchanged_without_any_reason(self):
  for status in ('already_spent','needs_profile','unsupported',None):self.assertEqual(await self.direct({'status':status}),{'state':'held','reason':status})
 async def test_access_and_completion_routes_are_unchanged(self):
  self.assertEqual(await self.direct({'status':'matched_current_source_quote','result_reference':'receipts/current.json','reason_code':'unused'}),{'state':'completed','result_reference':'receipts/current.json'})
  self.assertEqual((await self.direct({'status':'access_required','reason_code':'b2c_profile_required'}))['state'],'access_required')
