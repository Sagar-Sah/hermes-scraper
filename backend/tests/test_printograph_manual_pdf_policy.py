from copy import deepcopy
import ast,hashlib,importlib.util,json,sys
from pathlib import Path
import pytest
from crawler import printograph_modern_browser as m
from crawler import printograph_browser_profile as p
from crawler.service_context import canonical_service_context
from crawler.adapters.browser import make_record,normalize_snapshot
import test_printograph_modern_browser as support
ROOT=Path(__file__).resolve().parents[2]
URL='https://www.printograph.com/products/cardboards/order'
ASSOCIATION={'campaign_id':'pdf-test','product_id':'17898','supplier':'printograph','source_url':URL}
CONFIG={'mode':p.PDF_MODE,'service_policy':m.PDF_SERVICE_POLICY}
FIXTURES=Path(__file__).resolve().parent/'fixtures/printograph_manual_pdf_policy'
COMPAT=json.loads((FIXTURES/'legacy-default-compatibility.json').read_bytes())

def raw():
 r=support.raw();r['source_url']=URL
 services={'proofType':'100','manualProofType':'1','fileDelivery':'5','designServices':'42'}
 r['model_services']=deepcopy(services);r['selection_context']['services']=deepcopy(services);r['source_item_type']['value']='5'
 for group,value,label in zip(r['groups'][:3],('100','1','5'),('Manually Processed','PDF Proof','Upload Files Now')):
  group['choices'][0].update(value=value,label=label)
 r['native_total']['controller_total']='16.00'
 return m.bind_service_policy(r,m.PDF_SERVICE_POLICY,URL)
def observe(g,kind='price',before=None,after=None,**changes):
 r=raw();url='https://www.printograph.com/service/rest/v1/'+('products/900012/prices' if kind=='price' else 'rules/turnarounds')
 body={'quantity':100,'specifications':{'printProduct':{'productType':47,'size':11,'turnarounds':[1]},'other':{}},'productOptions':[1],'itemType':5}
 body.update(changes.pop('body',{}))
 g.observe(url,changes.pop('status',200),changes.pop('data',support.PRICE if kind=='price' else support.PRODUCTION),before or r,after or r,'native-'+kind,1,1,7,7,'GET' if kind=='price' else 'POST',None if kind=='price' else json.dumps(body))
def gate():return m.FreshQuoteGate(service_policy=m.PDF_SERVICE_POLICY,source_url=URL)
def test_default_job_spec_and_configuration_are_byte_identical():
 for case in COMPAT['cases']:
  assert p._bytes(p.job_spec(case['association'],'a'*64))==p._bytes(case['spec'])
  assert p.normalize_configuration(case['configuration'])==case['configuration']
  assert p._sha(p._bytes({'profile':p.PROFILE_ID,'association':case['association'],'configuration':case['configuration']}))==case['owner_identity_sha256']
 assert 'printograph_modern_service_policy' not in p.job_spec(ASSOCIATION,'a'*64)['source_metadata']
@pytest.mark.parametrize('url',[URL,'https://www.printograph.com/products/acrylic-boards/order'])
def test_explicit_pdf_spec_has_distinct_owner_and_policy(url):
 a={**ASSOCIATION,'source_url':url};normal={'mode':p.MODE}
 old=p._sha(p._bytes({'profile':p.PROFILE_ID,'association':a,'configuration':normal}));new=p._sha(p._bytes({'profile':p.PDF_PROFILE_ID,'association':a,'configuration':CONFIG}))
 assert old!=new
 spec=p.job_spec(a,new,profile_id=p.PDF_PROFILE_ID,mode=p.PDF_MODE,service_policy=m.PDF_SERVICE_POLICY)
 assert spec['source_metadata']['automation_owner']==new and spec['source_metadata']['printograph_modern_service_policy']==m.PDF_SERVICE_POLICY
 assert spec['provider_id'] is None and spec['instructions']=='' and spec['shipping_contexts']==[]
@pytest.mark.parametrize('url',['https://www.printograph.com/products/catalogs/order',URL+'?x=1',URL+'/',URL.replace('www.',''),URL.replace('https:','http:')])
def test_pdf_policy_exact_source_scope(url):
 with pytest.raises((m.ModernHeld,ValueError)):m.validate_service_policy(m.PDF_SERVICE_POLICY,url)
 with pytest.raises(ValueError):p.job_spec({**ASSOCIATION,'source_url':url},'a'*64,profile_id=p.PDF_PROFILE_ID,mode=p.PDF_MODE,service_policy=m.PDF_SERVICE_POLICY)
def test_no_proof_gate_cannot_accept_pdf_or_change_policy_after_receipts():
 r=raw();g=m.FreshQuoteGate();observe(g);observe(g,'production')
 with pytest.raises(m.ModernHeld,match='policy_scope'):g.verify(r,support.CONTROL,displayed_subtotal='16.00')
 g=gate();observe(g);observe(g,'production');untag=deepcopy(r);untag.pop('service_policy')
 with pytest.raises(m.ModernHeld,match='policy_scope'):g.verify(untag,support.CONTROL,displayed_subtotal='16.00')
def test_current_pdf_native_total_includes_fee_without_backend_formula():
 g=gate();observe(g);observe(g,'production');result=g.verify(raw(),support.CONTROL,displayed_subtotal='16.00')
 assert result['service_policy']==m.PDF_SERVICE_POLICY and result['native_computed_subtotal_matches_display']
 assert result['component_arithmetic_verified'] is False and result['source_data_finalized'] is False
 assert raw()['native_total']['controller_total']!='13.00'
@pytest.mark.parametrize('change',['no_proof','wrong_pdf_id','disabled','hidden','stale_shown','stale_base','wrong_production','source','unknown_policy'])
def test_pdf_current_service_price_production_and_scope_guards(change):
 g=gate();r=raw();observe(g);observe(g,'production');shown='16.00';control=deepcopy(support.CONTROL)
 if change=='no_proof':r['groups'][1]['choices'][0].update(value='36',label='No Proof');r['model_services']['manualProofType']='36';r['selection_context']['services']['manualProofType']='36'
 elif change=='wrong_pdf_id':r['groups'][1]['choices'][0]['value']='38';r['model_services']['manualProofType']='38';r['selection_context']['services']['manualProofType']='38'
 elif change=='disabled':r['groups'][1]['choices'][0]['disabled']=True
 elif change=='hidden':r['groups'][1]['choices'][0]['visible']=False
 elif change=='stale_shown':shown='13.00'
 elif change=='stale_base':r['native_total']['applied_product_price']='12.00'
 elif change=='wrong_production':control['selected_label']='Regular 1–2 Business Days'
 elif change=='source':r['source_url']='https://www.printograph.com/products/acrylic-boards/order'
 else:r['service_policy']='unknown'
 with pytest.raises(m.ModernHeld):g.verify(r,control,displayed_subtotal=shown)
def test_pdf_signature_is_distinct_and_roundtrips_existing_record_identity():
 r=raw();signature=m.service_signature(r);assert signature['service_policy']==m.PDF_SERVICE_POLICY and m.modern_bound(signature)
 assert canonical_service_context({'service_context_schema':m.SCHEMA,'modern_service_observation':r})==signature
 legacy=support.raw();assert m.service_signature(legacy)==COMPAT['service_signature']

def pdf_html(mode='good'):
 s=support.html(mode)
 for a,b in [('901','31'),('902','100'),('711','1'),('712','36'),('501','6'),('502','5'),('607','41'),('608','42')]:s=s.replace(a,b)
 # Remove NoProof entirely: this is the exact observed manual PDF-only scope.
 s=s.replace('<input type=radio name="proofTypeForManual" id="proofTypeForManual36" value="36" ><label for="proofTypeForManual36">No Proof</label>','')
 s=s.replace('<label for="proofTypeForManual1">PDF Proof</label>','<label for="proofTypeForManual1">PDF Proof <span class="badge badge-danger">$3.00</span></label>')
 s=s.replace("appliedPrices:{productPrice:'13.00'}","appliedPrices:{productPrice:'13.00',proofType:{1:'3.00'}}")
 s=s.replace("c.jsonObj.appliedPrices.productPrice}})","String(Number(c.jsonObj.appliedPrices.productPrice)+3)}})")
 s=s.replace("selected.quantity==='100'?'$13.00':'$28.00'","selected.quantity==='100'?'$16.00':'$31.00'")
 return s

@pytest.mark.asyncio
@pytest.mark.parametrize('policy',[None,m.PDF_SERVICE_POLICY])
async def test_actual_native_pdf_only_controls_require_explicit_intent(policy):
 from playwright.async_api import async_playwright
 async with async_playwright() as runtime:
  browser=await runtime.chromium.launch(headless=True);context=await browser.new_context(service_workers='block');page=await context.new_page();requests=[]
  async def route(r):
   requests.append(r.request.url)
   if r.request.url==URL:await r.fulfill(status=200,content_type='text/html',body=pdf_html())
   elif '/service/rest/v1/products/' in r.request.url:await r.fulfill(status=200,json=support.PRICE)
   elif r.request.url.endswith('/rules/turnarounds'):await r.fulfill(status=200,json=support.PRODUCTION)
   else:await r.abort();raise AssertionError('external fixture request')
  await context.route('**/*',route);await page.goto(URL)
  async def check():pass
  async def mark():pass
  async def settle():await page.wait_for_timeout(30)
  try:
   if policy is None:
    with pytest.raises(m.ModernHeld,match='safe_service_choice_unavailable'):await m.prepare_services(page,URL,check,mark,settle)
   else:
    value,chosen=await m.prepare_services(page,URL,check,mark,settle,service_policy=policy)
    assert chosen['proofTypeForManual']=='1' and m.policy_available(value) and m.modern_bound(m.service_signature(value))
    choice=value['groups'][1]['choices'][0];assert choice['label']=='PDF Proof $3.00' and choice['policy_label']=='PDF Proof'
    assert 'proofTypeForManual:36' not in await page.evaluate('actions')
   assert all(u==URL or '/service/rest/v1/' in u for u in requests)
  finally:await context.close();await browser.close()

class Runs:
 def __init__(self):self.values={};self.created=[];self.controls=[];self.time=0
 async def create(self,spec):
  rid=hashlib.md5(p._bytes(spec)).hexdigest();run={'id':rid,'status':'paused','spec':deepcopy(spec),'row_count':0,'scoped_row_count':0,'coverage_status':'unknown','products':[{'url':spec['urls'][0],'status':'queued','coverage_status':'unknown'}]};self.values[rid]=run;self.created.append(rid);return deepcopy(run)
 async def find(self,owner):return [deepcopy(v) for v in self.values.values() if v['spec']['source_metadata']['automation_owner']==owner]
 async def control(self,rid,action):self.controls.append((rid,action));self.values[rid]['status']='paused' if action=='pause' else 'queued';return deepcopy(self.values[rid])
 async def read(self,rid):
  v=self.values[rid];v.update(status='completed',row_count=2,scoped_row_count=2,coverage_status='complete');v['products'][0].update(status='completed',coverage_status='complete');return deepcopy(v)
 async def sleep(self,s):self.time+=s
 def profile(self,path,pdf=False):
  options={'profile_id':p.PDF_PROFILE_ID,'mode':p.PDF_MODE,'service_policy':m.PDF_SERVICE_POLICY} if pdf else {}
  return p.BrowserRunProfile(path,create=self.create,read=self.read,control=self.control,find_owned=self.find,clock=lambda:self.time,sleep=self.sleep,**options)
async def check():pass
@pytest.mark.asyncio
async def test_pdf_new_owned_scope_never_reinterprets_or_resumes_no_proof_run(tmp_path):
 f=Runs();normal=f.profile(tmp_path);owner=p._sha(p._bytes({'profile':p.PROFILE_ID,'association':ASSOCIATION,'configuration':{'mode':p.MODE}}));oldspec=normal.job_spec(ASSOCIATION,owner);old=await f.create(oldspec);before=deepcopy(f.values[old['id']]);driver=f.profile(tmp_path,True);task={'association':ASSOCIATION,'configuration':CONFIG}
 result=await driver.execute(task,check);again=await driver.execute(task,check)
 assert result==again and result['state']=='completed' and len(f.created)==2
 assert f.values[old['id']]==before and all(rid!=old['id'] for rid,action in f.controls)
 spec=f.values[f.created[-1]]['spec'];assert spec['source_metadata']['printograph_modern_service_policy']==m.PDF_SERVICE_POLICY
 assert spec['source_metadata']['automation_owner']!=owner
 assert len(list(tmp_path.glob('*.owner.json')))==1
 receipt=json.loads(Path(result['result_reference'].split('#')[0]).read_bytes());assert receipt['service_policy']==m.PDF_SERVICE_POLICY and not receipt['source_data_finalized']

@pytest.mark.asyncio
async def test_actual_registry_plans_pdf_new_jobs_and_preserves_held_no_proof(tmp_path):
 from types import SimpleNamespace
 from crawler.campaign_automation import CampaignAutomation,Profile
 from crawler.campaign_automation_bindings import family_profile,active_associations
 f=Runs();default=f.profile(tmp_path/'owners');assocs=[ASSOCIATION,{**ASSOCIATION,'product_id':'arbitrary-other-id','source_url':'https://www.printograph.com/products/acrylic-boards/order'}]
 resolve=lambda campaign:deepcopy(assocs)
 old=CampaignAutomation(tmp_path,[default.profile()],resolve)
 prior=await old.plan('pdf-test');await old.start('pdf-test',prior['plan_id']);record=old._record('pdf-test')
 for job in record['jobs'].values():job.update(status='held',reason='browser_chunk_no_durable_progress',attempts=4)
 old._save(record);held=deepcopy(record['jobs']);await old.close()
 class Campaigns:
  def get(self,campaign):return {'active_group_product_ids':[a['product_id'] for a in assocs],'products':[{'id':a['product_id'],'supplier':a['supplier'],'supplier_url':a['source_url'],'stage':'active'} for a in assocs]}
  def require_active_group(self,*args):pass
 def unsupported(*args):raise ValueError('outside fixture profile')
 pg=SimpleNamespace(PROFILE_ID='other-pg',association_for=unsupported,discover=lambda *a:{'configurations':[],'blockers':[]},normalize_configuration=lambda v:v,queue_handler=lambda d:None)
 four=SimpleNamespace(PROFILE_ID='other-four',association_context=unsupported,Held=ValueError,normalize_configuration=lambda v:v)
 ns={'__package__':'crawler','CampaignAutomation':CampaignAutomation,'Profile':Profile,'family_profile':family_profile,'active_associations':active_associations,'printograph':pg,'fourover':four}
 tree=ast.parse(Path(importlib.util.find_spec('crawler.campaign_automation_registry').origin).read_text());nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))];exec(compile(ast.Module(nodes,type_ignores=[]),'candidate-registry','exec'),ns)
 callbacks={'create':f.create,'read':f.read,'control':f.control,'find_owned':f.find}
 queue=ns['build_campaign_automation'](SimpleNamespace(root=tmp_path),Campaigns(),None,None,browser_run_callbacks=callbacks)
 try:
  plan=await queue.plan('pdf-test');assert len(plan['jobs'])==2 and not plan['blockers']
  assert all(j['profile_id']==p.PDF_PROFILE_ID and j['configuration']==CONFIG for j in plan['jobs'])
  await queue.start('pdf-test',plan['plan_id']);after=queue._record('pdf-test')['jobs']
  assert all(after[k]==v for k,v in held.items()) and len(after)==4 and f.created==[]
 finally:await queue.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('mode,expected',[('good',2),('stale_display',0)])
async def test_full_adapter_pdf_native_fee_and_production_current(monkeypatch,mode,expected):
 from contextlib import asynccontextmanager
 from types import SimpleNamespace
 from unittest.mock import AsyncMock
 from playwright.async_api import async_playwright,Route
 from crawler.adapters.browser import BrowserAdapter
 from crawler.adapters.printograph import AdapterError
 import crawler.browser_session as session_module
 import crawler.transport as transport_module
 class Limiter:
  def __init__(self):self.issued=0;self.released=0
  async def acquire(self,*a):await a[-1]();self.issued+=1;return 'fixture'+str(self.issued)
  def release(self,*a):self.released+=1
 class Transport:
  def __init__(self):self.limiter=Limiter();self.store=SimpleNamespace(config=SimpleNamespace(requests_per_second=2,source_concurrency=2));self.count=0
  async def check(self):pass
  async def record_observation(self,url,status,headers,body,context=None,count_request=True):
   self.count+=1;return SimpleNamespace(data=json.loads(body),response_id='receipt'+str(self.count),status=status,cached=False)
 seen=[];events=[];transport=Transport()
 async def fulfill(route,**kw):
  url=route.request.url;seen.append(url)
  if url==URL:await route.fulfill(status=200,content_type='text/html',body=pdf_html(mode))
  elif url=='https://www.printograph.com/service/rest/v1/products/900012/prices':await route.fulfill(status=200,json=support.PRICE)
  elif url=='https://www.printograph.com/service/rest/v1/rules/turnarounds':await route.fulfill(status=200,json=support.PRODUCTION)
  else:await route.abort();raise AssertionError('external fixture request '+url)
 monkeypatch.setattr(Route,'continue_',fulfill);monkeypatch.setattr(transport_module,'validate_url',AsyncMock());monkeypatch.setenv('ATLAS_BROWSER_INACTIVITY_SECONDS','2')
 async with async_playwright() as runtime:
  browser=await runtime.chromium.launch(headless=True)
  @asynccontextmanager
  async def context(*a,**kw):
   c=await browser.new_context(service_workers='block');page=await c.new_page()
   try:yield page
   finally:await c.close()
  monkeypatch.setattr(session_module,'browser_page',context)
  async def emit(name,data):events.append((name,data))
  rows=[]
  try:
   async for row in BrowserAdapter().extract({'url':URL,'source_metadata':{'printograph_modern_services':True,'printograph_modern_service_policy':m.PDF_SERVICE_POLICY},'selection_filters':{},'shipping_contexts':[]},transport,emit):rows.append(row)
  except AdapterError as exc:assert expected==0 and exc.code=='no_matching_configuration'
  finally:await browser.close()
 assert len(rows)==expected,[(n,d) for n,d in events if n in {'modern_quote_held','modern_quote_recovery'}]
 assert transport.limiter.issued==transport.limiter.released and all(u==URL or '/service/rest/v1/' in u for u in seen)
 for row in rows:
  assert row['subtotal'] in ('16.00','31.00') and row['pricing_context']['service_context']['service_policy']==m.PDF_SERVICE_POLICY
  assert row['pricing_context']['service_context']['model_services']['manualProofType']=='1'
  assert row['pricing_context']['native_freshness']['service_policy']==m.PDF_SERVICE_POLICY
  assert not row['source_data_finalized'] and not row['woocommerce_import_ready']

def test_saved_native_pdf_builder_and_catalog_proof_are_exact():
 import subprocess
 value=json.loads(subprocess.run(['node',str(FIXTURES/'native-pdf-builder.js'),str(FIXTURES)],check=True,capture_output=True,text=True).stdout)
 assert value['source_options']==[1,5] and value['selected_manualProofType']==1
 proof=json.loads((FIXTURES/'manifest.json').read_bytes())
 for name,digest in proof['files'].items():assert hashlib.sha256((FIXTURES/name).read_bytes()).hexdigest()==digest
 catalog=json.loads((FIXTURES/'observed-public-proof-options.json').read_bytes())
 for record in catalog['records']:
  rows=record['saved_public_product_options'];assert len(rows)==1
  ids={v['type']['id'] for v in rows[0]['proof_rows']};assert ids=={1,31}

@pytest.mark.parametrize('change',['good','unavailable','proof_drift','missing_production','stale_total'])
def test_pdf_generic_post_current_price_and_production_scope(change):
 import test_printograph_modern_post_prices as post
 r=post.raw();r['source_url']=URL;r['model_services']['manualProofType']='1';r['groups'][1]['choices'][0].update(value='1',label='PDF Proof')
 r['generic_price_context']['turnaround']['options']=['1'];r['native_total']['controller_total']='15.43';r=m.bind_service_policy(r,m.PDF_SERVICE_POLICY,URL)
 g=gate();data=post.future_response()
 if change=='unavailable':data['items'][0]['available']=False
 post.observe(g,r,data=data)
 if change!='missing_production':post.observe(g,r,kind='production')
 final=deepcopy(r);shown='15.43'
 if change=='proof_drift':final['selection_context']['services']['manualProofType']='36'
 if change=='stale_total':shown='13.43'
 if change=='good':
  out=g.verify(final,support.CONTROL,displayed_subtotal=shown);assert out['native_price_schema']=='generic-product-price/v1' and out['service_policy']==m.PDF_SERVICE_POLICY and out['api_subtotal_reference_only']
 else:
  with pytest.raises(m.ModernHeld):g.verify(final,support.CONTROL,displayed_subtotal=shown)

@pytest.mark.parametrize('policy',[{},False,'unknown'])
def test_unknown_policy_types_never_enable_pdf(policy):
 with pytest.raises(m.ModernHeld):m.validate_service_policy(policy,URL)
 with pytest.raises(ValueError):p.normalize_configuration({'mode':p.PDF_MODE,'service_policy':policy},profile_id=p.PDF_PROFILE_ID,mode=p.PDF_MODE,service_policy=policy)

@pytest.mark.asyncio
async def test_invalid_pdf_source_policy_fails_before_url_validation(monkeypatch):
 from unittest.mock import AsyncMock
 from crawler.adapters.browser import BrowserAdapter
 from crawler.adapters.printograph import AdapterError
 import crawler.transport as t
 validate=AsyncMock();monkeypatch.setattr(t,'validate_url',validate)
 async def emit(*args):pass
 with pytest.raises(AdapterError,match='modern_service_policy_scope_unproven'):
  async for row in BrowserAdapter().extract({'url':'https://www.printograph.com/products/other/order','source_metadata':{'printograph_modern_services':True,'printograph_modern_service_policy':m.PDF_SERVICE_POLICY}},None,emit):raise AssertionError('no row')
 assert validate.await_count==0

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['wrong_id','duplicate_label'])
async def test_pdf_native_label_never_substitutes_unknown_identity(change):
 value=raw()
 if change=='wrong_id':value['groups'][1]['choices'][0]['value']='38'
 else:value['groups'][1]['choices'].append({**value['groups'][1]['choices'][0],'value':'555','checked':False});value['groups'][1]['control_count']=2
 class Page:
  async def evaluate(self,script,arg):return deepcopy(value)
 async def noop(*a):pass
 value['open']=True
 with pytest.raises(m.ModernHeld):await m.prepare_services(Page(),URL,noop,noop,noop,service_policy=m.PDF_SERVICE_POLICY)
