"""AST-only bootstrap regression fragment; explicit candidate path, no crawler imports.
Set HERMES_BOOTSTRAP_FRAGMENT_SOURCE to the prospective module. All methods are
extracted from that file; clock, route lifecycle and source state are synthetic.
"""
import ast,asyncio,copy,math,os,unittest
from pathlib import Path
from types import SimpleNamespace
SOURCE=Path(os.environ.get('HERMES_BOOTSTRAP_FRAGMENT_SOURCE',Path(__file__).resolve().parents[1]/'crawler/fourover_automatic_profile.py'))
NATIVE=Path(os.environ.get('HERMES_BOOTSTRAP_NATIVE_SOURCE',Path(__file__).resolve().parents[1]/'crawler/fourover_native_observation.py'))
class Held(Exception):
 def __init__(self,code):super().__init__(code);self.code=code
class Clock:
 def __init__(self,now=0):self.now=now
 def monotonic(self):return self.now
async def scope_ok():return None
def extract_methods(clock):
 tree=ast.parse(SOURCE.read_text());cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='AutomaticFourOverSession')
 wanted={'begin_bootstrap','note_bootstrap_progress','bootstrap_fields_ready','wait_bootstrap','bootstrap_ready','check','phase_check'}
 nodes=[n for n in cls.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in wanted]
 assert {n.name for n in nodes}==wanted
 funcs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='ui_ready']
 native_tree=ast.parse(NATIVE.read_text());funcs.extend(n for n in native_tree.body if isinstance(n,ast.FunctionDef) and n.name=='services_off')
 async def sleep(seconds):clock.now+=seconds
 ns={'time':clock,'isfinite':math.isfinite,'Held':Held,'asyncio':SimpleNamespace(sleep=sleep),'file_identity':lambda _:('fixture',)}
 exec(compile(ast.Module(body=funcs+nodes,type_ignores=[]),'AST-extracted-bootstrap-only','exec'),ns)
 methods={k:ns[k] for k in wanted}
 return type('Harness',(),methods)
def state():
 flags=('modules_proved','registry_defined','tabs_module_defined','project_module_defined','registry_available','tabs_registered','project_registered','tab_domain_observed','selected_tab_in_domain','guest_restriction_observed')
 return {'attributes':{'211':{'count':1,'enabled':True,'visible':True}},'services':{k:{'count':1,'checked':False,'enabled':True} for k in ('job_sample','digital_proof')},'source_ui':{'schema':'native-project-ui-readiness/v1',**dict.fromkeys(flags,True),'form_count':1,'guest_restricted':False,'next_tab_available':True,'current_is_product_options':True,'fields':{'project_name':{'count':1,'visible_count':1}},'project_name_invalid_observed':True,'project_name_invalid':True}}
def harness(clock=None,session_deadline=300):
 clock=clock or Clock();h=extract_methods(clock)();h.session_deadline=session_deadline;h.stage='bootstrap';h.closed=False;h.failure=None;h.selection=None;h.cursor={'inflight':None};h.summary_request_observed=False;h.counts={'summary':0};h.source='https://4over.com/synthetic-source';h.page=SimpleNamespace(url=h.source);h.static={'a.html':{'pending':1,'response':None},'b.html':{'pending':1,'response':None}};h.name_module_expected=True;h.name_modules=set();h.prefix='synthetic-public-prefix';h.price_pending=set();h.public_asset_tasks=set();h.native_pricing_ready=True;h.last_static=clock.now-1;h.target={'attributes':{'211':'synthetic'}};h.check_scope=scope_ok;h.catalog={'checkpoint_path':'synthetic','checkpoint_identity':('fixture',)};h.begin_bootstrap();h.reserve_calls=0;h.quote_calls=0
 async def current_state():return state()
 h.state=current_state
 return h,clock
class BootstrapFragmentTests(unittest.TestCase):
 def test_old_race_must_stay_bootstrap_with_missing_mounted_essentials(self):
  h,c=harness();s=state();s['services']={k:{'count':0,'checked':False,'enabled':False} for k in ('job_sample','digital_proof')};s['source_ui']['fields']['project_name']={'count':0,'visible_count':0}
  assert h.native_pricing_ready is True and h.static['a.html']['pending']==1
  self.assertFalse(h.bootstrap_ready(s));self.assertEqual(h.stage,'bootstrap');self.assertEqual((h.reserve_calls,h.quote_calls),(0,0))
 def test_mounted_source_essentials_allow_unrelated_pending_static(self):
  h,c=harness();self.assertTrue(h.bootstrap_ready(state()));self.assertEqual(len(h.static),2)
 def test_each_service_shape_is_still_required(self):
  h,c=harness()
  for service in ('job_sample','digital_proof'):
   for value in ({'count':0,'checked':False,'enabled':False},{'count':2,'checked':False,'enabled':True},{'count':1,'checked':True,'enabled':True},{'count':1,'checked':False,'enabled':False}):
    with self.subTest(service=service,value=value):
     s=state();s['services'][service]=value;self.assertFalse(h.bootstrap_ready(s))
 def test_expected_name_exact_count_visibility_but_blank_invalid_allowed(self):
  h,c=harness();self.assertTrue(h.bootstrap_fields_ready(state()))
  for value in ({'count':0,'visible_count':0},{'count':2,'visible_count':1},{'count':1,'visible_count':0},{'count':True,'visible_count':1},{'count':1,'visible_count':True}):
   with self.subTest(value=value):
    s=state();s['source_ui']['fields']['project_name']=value;self.assertFalse(h.bootstrap_fields_ready(s))
 def test_name_expectation_from_verified_module_or_admitted_template(self):
  for mode in ('module','template'):
   h,c=harness();h.name_module_expected=False
   if mode=='module':h.name_modules={'FourOver_Project/js/view/project/name.js'}
   else:h.static['FourOver_Project/template/project/name.html']={'pending':1,'response':None}
   s=state();s['source_ui']['fields']['project_name']={'count':0,'visible_count':0};self.assertFalse(h.bootstrap_ready(s))
 def test_undeclared_name_allowed_only_after_pending_public_inspection_settles(self):
  h,c=harness();h.name_module_expected=False;s=state();s['source_ui']['fields']['project_name']={'count':0,'visible_count':0}
  h.public_asset_tasks={object()};self.assertFalse(h.bootstrap_ready(s));h.public_asset_tasks=set();self.assertTrue(h.bootstrap_ready(s))
 def test_old_attribute_price_and_native_static_alternatives_preserved(self):
  h,c=harness();s=state();s['attributes']['211'].update(visible=False,implicit_fixed_dash=True);self.assertTrue(h.bootstrap_ready(s))
  h.price_pending={'child'};self.assertFalse(h.bootstrap_ready(s));h.price_pending=set();h.native_pricing_ready=False;self.assertFalse(h.bootstrap_ready(s))
  for f in h.static.values():f.update(pending=0,response=object())
  self.assertTrue(h.bootstrap_ready(s));s['source_ui']['current_is_product_options']=False;self.assertFalse(h.bootstrap_ready(s))
 def test_initial_and_hard_budget_clamps_leave_session_reserve(self):
  h,c=harness();self.assertEqual((h.bootstrap_initial,h.bootstrap_hard_deadline,h.cold_deadline),(95,180,95))
  h,c=harness(session_deadline=100);self.assertEqual((h.bootstrap_initial,h.bootstrap_hard_deadline,h.cold_deadline),(64,64,64))
  for deadline in (36,35,0):
   with self.subTest(deadline=deadline),self.assertRaises(Held):harness(session_deadline=deadline)
 def test_distinct_admitted_and_verified_progress_only(self):
  h,c=harness();c.now=80;self.assertTrue(h.note_bootstrap_progress('admitted','a.html'));self.assertEqual(h.cold_deadline,115)
  c.now=100;self.assertFalse(h.note_bootstrap_progress('admitted','a.html'));self.assertEqual(h.cold_deadline,115)
  self.assertTrue(h.note_bootstrap_progress('verified','a.html'));self.assertEqual(h.cold_deadline,135)
  for kind,key in [('error','b.html'),('cancel','b.html'),('admitted','foreign.html')]:self.assertFalse(h.note_bootstrap_progress(kind,key))
  self.assertEqual(h.cold_deadline,135);self.assertEqual(len(h.bootstrap_progress),2)
 def test_progress_never_passes_hard180_or_short_session(self):
  h,c=harness()
  for now,kind,key in [(80,'admitted','a.html'),(110,'verified','a.html'),(140,'admitted','b.html'),(170,'verified','b.html')]:c.now=now;self.assertTrue(h.note_bootstrap_progress(kind,key))
  self.assertEqual(h.cold_deadline,180);c.now=180
  with self.assertRaises(Held):h.note_bootstrap_progress('verified','b.html')
  h,c=harness(session_deadline=100);c.now=60;self.assertTrue(h.note_bootstrap_progress('admitted','a.html'));self.assertEqual(h.cold_deadline,64)
 def test_late_callback_cannot_revive_expired_deadline(self):
  for now in (95,95.1,180):
   h,c=harness();c.now=now
   with self.subTest(now=now),self.assertRaises(Held):h.note_bootstrap_progress('verified','a.html')
   self.assertEqual(h.cold_deadline,95);self.assertEqual(h.bootstrap_progress,set())
 def test_no_progress_after_handoff_or_foreign_source_or_failures(self):
  changes=[('stage','child'),('closed',True),('failure','source_auth_required'),('selection',{}),('summary_request_observed',True)]
  for key,value in changes:
   h,c=harness();setattr(h,key,value);c.now=80;self.assertFalse(h.note_bootstrap_progress('admitted','a.html'));self.assertEqual(h.cold_deadline,95)
  h,c=harness();h.cursor['inflight']={};self.assertFalse(h.note_bootstrap_progress('admitted','a.html'))
  h,c=harness();h.page.url='https://4over.com/other';self.assertFalse(h.note_bootstrap_progress('admitted','a.html'))
 def test_clock_rollback_and_nonfinite_failclosed(self):
  for now in (-1,float('nan'),float('inf'),True):
   with self.subTest(now=now),self.assertRaises(Held):harness(Clock(now))
  h,c=harness();c.now=80;h.note_bootstrap_progress('admitted','a.html')
  for now in (79,float('nan'),float('inf')):
   c.now=now
   with self.subTest(now=now),self.assertRaises(Held):h.note_bootstrap_progress('verified','a.html')
   self.assertEqual(h.cold_deadline,115)
 def test_dynamic_wait_accepts_legitimate_extension_beyond_original95(self):
  h,c=harness();c.now=90
  async def shifted():h.note_bootstrap_progress('admitted','a.html');c.now=110;return state()
  h.state=shifted;observed=asyncio.run(h.wait_bootstrap());self.assertTrue(h.bootstrap_ready(observed));self.assertEqual(h.cold_deadline,125);self.assertEqual(c.now,110)
 def test_post_state_await_crossing_exact_deadline_never_accepts_ready(self):
  for after in (95,95.1):
   h,c=harness();c.now=94
   async def shifted():c.now=after;return state()
   h.state=shifted
   with self.subTest(after=after),self.assertRaises(Held):asyncio.run(h.wait_bootstrap())
 def test_no_progress_wait_expires_and_auth_or_scope_still_stop(self):
  h,c=harness();c.now=94.95
  async def not_mounted():s=state();s['services']={};return s
  h.state=not_mounted
  with self.assertRaises(Held):asyncio.run(h.wait_bootstrap())
  self.assertGreaterEqual(c.now,95)
  h,c=harness();h.failure='source_auth_required'
  with self.assertRaisesRegex(Held,'source_auth_required'):asyncio.run(h.wait_bootstrap())
  h,c=harness()
  async def cancelled():raise asyncio.CancelledError()
  h.check_scope=cancelled
  with self.assertRaises(asyncio.CancelledError):asyncio.run(h.wait_bootstrap())
if __name__=='__main__':unittest.main(verbosity=2)
