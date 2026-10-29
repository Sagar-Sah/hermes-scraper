import asyncio,base64,hashlib,importlib.util,json,os
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import sys
import pytest
HERE=Path(__file__).resolve().parent
if os.environ.get('WS_COLLECTOR_MODULE_DIR'):
 for name in ['wsdisplay_source_collector','source_collection_profiles']:
  path=Path(os.environ['WS_COLLECTOR_MODULE_DIR'])/(name+'.py')
  spec=importlib.util.spec_from_file_location('crawler.'+name,path);mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
m=importlib.import_module('crawler.wsdisplay_source_collector')
URL='https://www.wsdisplay.com/SYNTHETIC-Product';MARKER='My Account SYNTHETIC BUSINESS'

def snapshot():
 return {'url':URL,'ready':True,'challenge':False,'login_prompt':False,'account_marker':MARKER,'fields':[{'key':'custcol_matrix_order_detail_option','label':'Order Detail','kind':'select','selected':'A','disabled':False,'choices':[{'value':'A','label':'Package A','disabled':False}]},{'key':'custcol_webstore_locations','label':'Warehouse','kind':'radio','selected':'1','disabled':False,'choices':[{'value':'1','label':'California 5','disabled':False}]}],'required_text':[],'sku':'SYN-A','quantity':1,'quantity_min':'1','quantity_max':None,'tier_rows':[['Quantity','Price'],['1 - 1','$100.00'],['2 +','$90.00']],'unit_prices':['Online Price:$100.00'],'totals':['$100.00'],'stock':5,'out_of_stock':False,'production':{'id':'4','label':'Standard 4 Business Days Turnaround'},'artwork_controls_present':True,'packing':{'item_weight_literal':'10 lbs','shipping_dimension_literal':'SYNBOX(61x15x9)Qty1'}}

def test_native_bands_and_q2_are_not_q1_extrapolation():
 s=snapshot();s.update(quantity=2,unit_prices=['Online Price:$90.00'],totals=['$180.00']);r=m.receipt(s)
 assert r['unit_cost']=='90.00' and r['native_total']=='180.00' and r['bands'][0]['unit_cost']=='100.00'
 assert r['unit_basis_explicit'] and r['bands_are_configured_quotes'] is False and not r['source_data_finalized']

@pytest.mark.parametrize('rows',[[['1 - 2','$10.00'],['2 +','$9.00']],[['1 - 1','$10.00'],['3 +','$9.00']],[['1 +','$10.00'],['2 +','$9.00']],[['1 - 1','Free']],[['1 - 1','$0.00']]])
def test_bad_band_domains_hold(rows):
 with pytest.raises(m.Held):m.bands_from_rows(rows)

@pytest.mark.parametrize('change',[{'totals':['$99.00']},{'unit_prices':['$100.00','$90.00']},{'quantity':True},{'sku':'PARENT-MTX'}])
def test_wrong_or_parent_quote_not_promoted(change):
 s=snapshot();s.update(change)
 with pytest.raises(m.Held):m.receipt(s)

def test_stock_and_required_text_are_not_price():
 s=snapshot();s['stock']=None;r=m.receipt(s);assert r['availability']['available'] is None and 'stock_or_warehouse_unresolved'in r['holds']
 s=snapshot();s['out_of_stock']=True;r=m.receipt(s);assert r['availability']['available'] is False
 s=snapshot();s['required_text']=[{'key':'custcol_pms_number','label':'PMS Number/s','required':True}];assert 'required_text_service'in m.receipt(s)['holds']

def test_witness_is_exact_binding_and_not_fresh_price_reuse():
 s=snapshot();b=m.scope_binding(s);w={'kind':'reviewed','binding':b,'quantity':2,'observed_at':'2026-01-01T00:00:00Z','unit_cost':'85.00','native_total':'170.00'};r=m.receipt(s,w)
 assert r['unit_cost']=='100.00' and r['historical_band_difference']['current_band_unit_cost']=='90.00' and r['historical_band_difference']['changed'] is True and r['unit_basis_witness']['observed_at']==w['observed_at']
 w['binding']={**b,'source_sku':'OTHER'}
 with pytest.raises(m.Held):m.receipt(s,w)

@pytest.mark.parametrize('marker',['My Account','Welcome','My Account ','My Account\nPRIVATE'])
def test_unknown_or_generic_account_not_proof(marker):
 with pytest.raises(m.Held):m.reviewed_witnesses({'account_marker':marker})

def test_reviewed_seed_hash_and_binding_are_kept():
 s=snapshot();s.update(quantity=2,unit_prices=['$90.00'],totals=['$180.00']);r=m.receipt(s);b=json.dumps(r).encode();opts={'account_marker':MARKER,'reviewed_seed_evidence':[{'sha256':hashlib.sha256(b).hexdigest(),'base64':base64.b64encode(b).decode(),'operator_reviewed':True}]};marker,seeds,bound,bodies=m.reviewed_witnesses(opts)
 w=next(iter(seeds.values()));assert w['binding']==r['binding'] and w['observed_at']==r['observed_at'] and bodies[w['evidence_sha256']]==b
 opts['reviewed_seed_evidence'][0]['sha256']='0'*64
 with pytest.raises(m.Held):m.reviewed_witnesses(opts)

@pytest.mark.parametrize('url',['http://www.wsdisplay.com/A','https://wsdisplay.com/A','https://www.wsdisplay.com/A?token=x','https://www.wsdisplay.com/cart','https://user@www.wsdisplay.com/A','https://www.wsdisplay.com/%2e%2e/A'])
def test_only_exact_public_source_urls(url):
 with pytest.raises(ValueError):m.page_url(url)

class FakeStore:
 def __init__(self,path):self.root=path
 def put_object(self,data,suffix):
  key='objects/'+hashlib.sha256(data).hexdigest()+'.'+suffix;f=self.root/key;f.parent.mkdir(exist_ok=True);f.write_bytes(data);return key
 def get_object(self,key):return(self.root/key).read_bytes()
class Limiter:
 def __init__(self):self.sem=asyncio.Semaphore(2);self.active=0;self.max=0;self.grants=[];self.releases=[]
 async def acquire(self,origin,rps,concurrency,check):
  assert (origin,rps,concurrency)==(m.ORIGIN,1,2);self.loop=asyncio.get_running_loop();await self.sem.acquire();await check();self.active+=1;self.max=max(self.max,self.active);token=str(len(self.grants));self.grants.append(token);return token
 def release(self,origin,token,elapsed,status,retry,cap):
  self.active-=1;self.releases.append((token,status));self.loop.call_soon_threadsafe(self.sem.release)

@pytest.fixture
async def browser_pool(monkeypatch):
 from playwright.async_api import async_playwright
 async with async_playwright()as runtime:
  browser=await runtime.chromium.launch(headless=True);context=await browser.new_context(service_workers='block');html=(HERE/'fixtures/wsdisplay_native/native.html').read_text();pool=Pool(context,html);yield pool;await browser.close()
class Pool:
 def __init__(self,context,html):self.context=context;self.html=html;self.pages=[];self.actions=[];self.actual_source_requests=0
 @asynccontextmanager
 async def page(self,origin,headless=False):
  assert origin==m.ORIGIN and headless is False
  p=await self.context.new_page();wrapper=Page(p,self);self.pages.append(wrapper)
  try:yield wrapper
  finally:
   if not p.is_closed():await p.close()
class Page:
 def __init__(self,page,pool):self.p=page;self.pool=pool;self.main_frame=page.main_frame
 async def route(self,pattern,handler):
  async def route(r):await handler(Route(r,self.pool))
  await self.p.route(pattern,route)
 def on(self,*args):self.p.on(*args)
 async def goto(self,*args,**kw):return await self.p.goto(*args,**kw)
 async def evaluate(self,*args):return await self.p.evaluate(*args)
 def is_closed(self):return self.p.is_closed()
 async def close(self):
  if not self.p.is_closed():
   self.pool.actions+=await self.p.evaluate('typeof actions!=="undefined"?actions:[]');await self.p.close()
class Route:
 def __init__(self,r,pool):self.r=r;self.pool=pool;self.request=r.request
 async def continue_(self):
  # No request can escape to WSDisplay or any other source: every continue is fulfilled here.
  if self.request.resource_type=='document':await self.r.fulfill(status=200,content_type='text/html',body=self.pool.html)
  elif '/api/items?'in self.request.url:await asyncio.sleep(.025);await self.r.fulfill(status=200,content_type='application/json',body='{}')
  else:await self.r.abort()
 async def abort(self):await self.r.abort()

@pytest.fixture
def env(tmp_path,monkeypatch):
 async def valid(url):return None
 monkeypatch.setattr(m,'validate_url',valid);monkeypatch.setattr(m,'POLL_SECONDS',.015);monkeypatch.setattr(m,'QUIET_SECONDS',.015);monkeypatch.setattr(m,'ACTION_SETTLE_SECONDS',.03);monkeypatch.setattr(m,'SETTLE_SECONDS',5)
 return FakeStore(tmp_path)

async def finish(c,jid):
 t=c.tasks.get(jid)
 if t:await t
 return c.get(jid,include_data=True)

@pytest.mark.asyncio
async def test_real_intercepted_native_frontier_bands_service_hold_and_qty_reset(env,browser_pool):
 limiter=Limiter();c=m.WsdisplaySourceCollector(env,browser_pool,limiter=limiter);j=await c.start([URL],{'account_marker':MARKER,'native_q2_checks':True});out=await finish(c,j['id'])
 assert out['status']=='completed',out
 data=out['pages'][0]['data'];quotes=[d for d in data if d['schema']=='wsdisplay-native-band-observation/v1'];services=[d for d in data if d['schema']=='wsdisplay-native-required-service-hold/v1']
 assert {q['binding']['source_sku']for q in quotes}=={'SYN-A','SYN-B'}
 assert len(quotes)==3 and len(services)==3
 assert any(q['binding']['source_sku']=='SYN-B' and q['quantity']==1 and q['availability']['stock']==1 for q in quotes)
 assert all(q['quantity']<=q['availability']['stock']for q in quotes)
 assert all('PRIVATE_TEXT_TRAP'not in json.dumps(x)for x in data)
 assert all(q['artwork_template_status']=='source_link_observed' for q in quotes)
 assert {q['binding']['source_sku']:q['artwork_resources'][0]['url'] for q in quotes}=={
  'SYN-A':'https://display-templates.s3.us-west-2.amazonaws.com/PDF/SYN-A_Template.pdf',
  'SYN-B':'https://s3-us-west-2.amazonaws.com/display-templates/PSB/SYN-B_Template.tif.zip'}
 assert all('example.invalid' not in json.dumps(q['artwork_resources']) for q in quotes)
 assert len(limiter.grants)==len(limiter.releases) and limiter.active==0 and limiter.max<=2
 q2=[x for x in browser_pool.actions if x['k']=='quantity'and x['v']=='2'];assert len(q2)==2
 assert all(x.p.is_closed()for x in browser_pool.pages)
 await c.close()


@pytest.mark.asyncio
async def test_template_only_collects_choice_bound_links_without_price_receipts(env,browser_pool):
 limiter=Limiter();c=m.WsdisplaySourceCollector(env,browser_pool,limiter=limiter)
 async def fake_download(url,check):
  assert url.startswith(('https://display-templates.s3.us-west-2.amazonaws.com/PDF/SYN-',
                         'https://s3-us-west-2.amazonaws.com/display-templates/PSB/SYN-'))
  await check()
  return {'status':'verified_zip' if url.endswith('.zip') else 'verified_pdf',
          'path':'wsdisplay-source-collections/templates/synthetic.pdf','sha256':'a'*64,'bytes':50}
 c.save_artwork_template=fake_download
 job=await c.start([URL],{'account_marker':MARKER,'template_only':True})
 out=await finish(c,job['id'])
 assert out['status']=='completed',(out['pages'][0].get('holds'),out['pages'][0].get('error'))
 receipts=[r for r in out['pages'][0]['data'] if r['schema']=='wsdisplay-native-template-observation/v1']
 assert {r['binding']['source_sku'] for r in receipts}=={'SYN-A','SYN-B'}
 assert len(receipts)==2
 assert all(r['template_choice_scope']=='matrix_child_only' for r in receipts)
 assert out['pages'][0]['template_child_scope']['remaining_children']==2
 assert all(r['template_status']=='source_link_observed' and r['asset_files_downloaded'] is True for r in receipts)
 assert {x['file']['status'] for r in receipts for x in r['artwork_resources'] if x['kind']=='template'} == {'verified_pdf','verified_zip'}
 assert not any(r['schema']=='wsdisplay-native-band-observation/v1' for r in out['pages'][0]['data'])
 assert all('example.invalid' not in json.dumps(r) for r in receipts)
 await c.close()


@pytest.mark.asyncio
async def test_template_only_traverses_height_without_fulfillment_cartesian_product(env,browser_pool):
 browser_pool.html=browser_pool.html.replace("let choice='',warehouse=''", "let height='',choice='',warehouse=''")
 browser_pool.html=browser_pool.html.replace(
  '<label for="quantity">Quantity</label>',
  '<label for="custcol76">Select Height</label><select id="custcol76"><option value="">- Select -</option><option value="24" ${height===\'24\'?\'selected\':\'\'}>24 in</option><option value="36" ${height===\'36\'?\'selected\':\'\'}>36 in</option></select><label for="quantity">Quantity</label>')
 browser_pool.html=browser_pool.html.replace(
  "else if(k==='custcol_webstore_locations')warehouse=e.target.value;",
  "else if(k==='custcol76')height=e.target.value;else if(k==='custcol_webstore_locations')warehouse=e.target.value;")
 c=m.WsdisplaySourceCollector(env,browser_pool,limiter=Limiter())
 async def fake_download(url,check):
  await check()
  return {'status':'verified_pdf','path':'wsdisplay-source-collections/templates/synthetic.pdf',
          'sha256':'a'*64,'bytes':50}
 c.save_artwork_template=fake_download
 job=await c.start([URL],{'account_marker':MARKER,'template_only':True})
 out=await finish(c,job['id'])
 assert out['status']=='completed',out['pages'][0].get('holds')
 receipts=[r for r in out['pages'][0]['data'] if r['schema']=='wsdisplay-native-template-observation/v1']
 assert len(receipts)==4
 assert {(r['binding']['selected_child'],r['binding']['choices']['custcol76']) for r in receipts}=={
  ('A','24'),('A','36'),('B','24'),('B','36')}
 assert all(r['template_choice_scope']=='matrix_child_and_dimensions' for r in receipts)
 assert not any(x['k'] in {'custcol_webstore_locations','custcol_pms_match'} for x in browser_pool.actions)
 await c.close()


@pytest.mark.asyncio
async def test_official_library_fallback_uses_selected_shape_width_height(env,browser_pool):
 c=m.WsdisplaySourceCollector(env,browser_pool,limiter=Limiter())
 c._template_library={('square',5,24):'https://s3-us-west-2.amazonaws.com/display-templates/PSB/Skybox%20Square%205ftx24in.tif.zip',
                      ('triangle',12,60):'https://s3-us-west-2.amazonaws.com/display-templates/PDF/12ftx60in_Triangle.tif'}
 async def check():return None
 for title,size,value,expected in [
  ('5 Ft. Hanging Banner Square','24 in','22',c._template_library[('square',5,24)]),
  ('12 Ft. Hanging Banner Triangle','60 in','26',c._template_library[('triangle',12,60)])]:
  state={'title':title,'fields':[{'label':'Select Height','selected':value,
                                 'choices':[{'value':value,'label':size}]}]}
  assert await c.library_template_for(state,check)==expected
 await c.close()


@pytest.mark.asyncio
async def test_legacy_tif_zip_template_is_verified_and_cached_without_refetch(env,monkeypatch):
 class AssetLimiter:
  def __init__(self):self.acquires=0;self.releases=0
  async def acquire(self,origin,rps,concurrency,check):
   assert origin=='https://s3-us-west-2.amazonaws.com';self.acquires+=1;await check();return 'permit'
  def release(self,*args):self.releases+=1
 class Response:
  status_code=200;headers={'content-type':'application/zip','content-length':'8'}
  async def aiter_bytes(self):yield b'PK\x03\x04TEST'
 class Stream:
  async def __aenter__(self):return Response()
  async def __aexit__(self,*args):pass
 class Client:
  async def __aenter__(self):return self
  async def __aexit__(self,*args):pass
  def stream(self,*args,**kwargs):return Stream()
 monkeypatch.setattr(m.httpx,'AsyncClient',lambda **kwargs:Client())
 async def check():pass
 limiter=AssetLimiter();c=m.WsdisplaySourceCollector(env,None,limiter=limiter)
 url='https://s3-us-west-2.amazonaws.com/display-templates/PSB/HB-Wave10ft_36in.tif.zip'
 first=await c.save_artwork_template(url,check);second=await c.save_artwork_template(url,check)
 assert first==second and first['status']=='verified_zip' and first['bytes']==8
 assert (env.root/first['path']).read_bytes()==b'PK\x03\x04TEST'
 assert limiter.acquires==limiter.releases==1
 with pytest.raises(m.Held):await c.save_artwork_template('https://s3-us-west-2.amazonaws.com/private/other.zip',check)

@pytest.mark.asyncio
async def test_out_of_stock_is_verified_unavailable_not_purchaseable(env,browser_pool):
 c=m.WsdisplaySourceCollector(env,browser_pool,limiter=Limiter());j=await c.start(['https://www.wsdisplay.com/SYN-OUT'],{'account_marker':MARKER});out=await finish(c,j['id']);assert out['status']=='completed',out
 d=out['pages'][0]['data'][0];assert d['availability']['available']is False and d['availability']['verified']is True and not d['retail_ready']
 assert not any(a['k']=='quantity'for a in browser_pool.actions);await c.close()

@pytest.mark.asyncio
async def test_wrong_account_stops_before_native_choices(env,browser_pool):
 c=m.WsdisplaySourceCollector(env,browser_pool,limiter=Limiter());j=await c.start([URL],{'account_marker':'My Account OTHER BUSINESS'});out=await finish(c,j['id']);assert out['status']=='attention' and out['error']=='source_account_marker_required';assert not browser_pool.actions;await c.close()

@pytest.mark.asyncio
async def test_reviewed_seed_skips_exact_q2_but_prices_are_current(env,browser_pool):
 b={'source_url':URL,'source_sku':'SYN-A','selected_child':'A','warehouse_id':'1','choices':{'custcol_matrix_order_detail_option':'A','custcol_turn_around_shipping':'4','custcol_webstore_locations':'1','custcol_pms_match':'F'},'production_id':'4'}
 old={'schema':'wsdisplay-native-band-observation/v1','observed_at':'2026-01-01T00:00:00Z','binding':b,'quantity':2,'unit_cost':'85.00','native_total':'170.00'};body=json.dumps(old).encode();seed={'sha256':hashlib.sha256(body).hexdigest(),'base64':base64.b64encode(body).decode(),'operator_reviewed':True}
 c=m.WsdisplaySourceCollector(env,browser_pool,limiter=Limiter());j=await c.start([URL],{'account_marker':MARKER,'native_q2_checks':True,'reviewed_seed_evidence':[seed]});out=await finish(c,j['id']);assert out['status']=='completed',out
 a=next(d for d in out['pages'][0]['data']if d.get('binding',{}).get('source_sku')=='SYN-A'and d['schema']=='wsdisplay-native-band-observation/v1')
 assert a['quantity']==1 and a['unit_cost']=='100.00' and a['unit_basis_witness']['observed_at']==old['observed_at'] and a['historical_band_difference']['changed']
 assert not any(x['k']=='quantity'and x['v']=='2'and x['sku']=='SYN-A'for x in browser_pool.actions)
 await c.close()

@pytest.mark.asyncio
async def test_upsert_race_equivalent_pending_finish_during_permit_wait_releases(env):
 class P:
  main_frame=object()
  def is_closed(self):return True
 class Req:
  url=URL;method='GET';resource_type='xhr';frame=None
 class R:
  request=Req()
  async def continue_(self):raise AssertionError('closed request must not continue')
  async def abort(self):pass
 class L:
  def __init__(self):self.wait=asyncio.Event();self.enter=asyncio.Event();self.released=[]
  async def acquire(self,*args):self.enter.set();await self.wait.wait();return'committed-token'
  def release(self,*args):self.released.append(args[1])
 async def check():pass
 l=L();n=m.NativeSession(P(),l,URL,MARKER,check);r=R();t=asyncio.create_task(n.guard(r));await l.enter.wait();n.finish(r.request);l.wait.set();await t;await n.close();assert l.released==['committed-token']and not n.pending

@pytest.mark.asyncio
async def test_cancellation_after_limiter_commit_does_not_drop_permit(env):
 class P:
  main_frame=object()
  def is_closed(self):return True
 class R:
  request=type('Req',(),{'url':URL,'method':'GET','resource_type':'xhr','frame':None})()
  async def continue_(self):raise AssertionError('cancelled must not continue')
  async def abort(self):pass
 class L:
  def __init__(self):self.wait=asyncio.Event();self.enter=asyncio.Event();self.released=[]
  async def acquire(self,*args):self.enter.set();await self.wait.wait();return'committed-token'
  def release(self,*args):self.released.append(args[1])
 async def check():pass
 l=L();n=m.NativeSession(P(),l,URL,MARKER,check);t=asyncio.create_task(n.guard(R()));await l.enter.wait();t.cancel();l.wait.set();await asyncio.gather(t,return_exceptions=True);await n.close();assert l.released==['committed-token']and not n.pending

@pytest.mark.asyncio
async def test_job_pause_closes_all_pages_and_resume_does_not_duplicate_cached_receipts(env,browser_pool):
 c=m.WsdisplaySourceCollector(env,browser_pool,limiter=Limiter());j=await c.start([URL],{'account_marker':MARKER,'native_q2_checks':True})
 for _ in range(200):
  saved=c.load(j['id'])
  if saved['pages'][0]['receipts']:break
  await asyncio.sleep(.01)
 first=saved['pages'][0]['receipts'][0]['sha256'];await c.pause(j['id']);assert all(x.p.is_closed()for x in browser_pool.pages);assert c.get(j['id'])['status']=='paused'
 await c.resume(j['id']);out=await finish(c,j['id']);assert out['status']=='completed',out
 assert sum(x['sha256']==first for x in out['pages'][0]['receipts'])==1
 assert not c.active(j['id']);await c.close()

@pytest.mark.asyncio
async def test_page_slot_bound_across_collectors_sharing_store(env):
 c=m.WsdisplaySourceCollector(env,object(),limiter=Limiter());other=m.WsdisplaySourceCollector(env,object(),limiter=Limiter());live=0;maximum=0
 async def check():pass
 async def work(owner):
  nonlocal live,maximum
  async with owner.slot(check):live+=1;maximum=max(maximum,live);await asyncio.sleep(.03);live-=1
 await asyncio.gather(*(work(c if i%2 else other)for i in range(8)));assert maximum==2

@pytest.mark.asyncio
async def test_api_profiles_are_generic_and_authenticated(tmp_path):
 import subprocess,textwrap
 code=r'''
import asyncio,json
from types import SimpleNamespace
import httpx
from crawler import app as api
class Fake:
 def __init__(self):self.calls=[]
 async def start(self,urls,**kw):self.calls.append((urls,kw));return{'id':'a'*32,'profile_id':kw.get('profile_id'),'status':'queued'}
 def get(self,jid,include_data=False):return{'id':jid,'include_data':include_data}
 def list(self):return[]
 async def pause(self,jid):return{'id':jid,'status':'paused'}
 async def resume(self,jid):return{'id':jid,'status':'queued'}
f=Fake();api.app.state.source_collections=f
async def main():
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://test')as c:
  assert(await c.post('/api/v1/source-collections',json={'urls':['https://www.wsdisplay.com/SYNTHETIC']})).status_code==401
  h={'Authorization':'Bearer SYNTHETIC-TOKEN'}
  r=await c.post('/api/v1/source-collections',headers=h,json={'urls':['https://www.wsdisplay.com/SYNTHETIC'],'profile_id':'wsdisplay_native_v1','profile_options':{'account_marker':'My Account SYNTHETIC BUSINESS'}});assert r.status_code==200,r.text
  assert f.calls[-1][1]['profile_id']=='wsdisplay_native_v1'
  r=await c.post('/api/v1/source-collections',headers=h,json={'urls':['https://www.b2sign.com/SYNTHETIC']});assert r.status_code==200 and f.calls[-1][1]=={}
  for action in ['pause','resume']:assert(await c.post('/api/v1/source-collections/'+('a'*32)+'/'+action,headers=h)).status_code==200
  assert(await c.get('/api/v1/source-collections/'+('a'*32)+'?include_data=true',headers=h)).json()['include_data']is True
  assert(await c.post('/api/v1/source-collections',headers=h,json={'urls':['https://www.wsdisplay.com/SYNTHETIC'],'unexpected':'x'})).status_code==422
 api.store.engine.dispose()
asyncio.run(main());print('API PASS')
'''
 repo=Path(os.environ.get('HERMES_TEST_REPO',str(HERE.parents[1])))
 env={k:v for k,v in os.environ.items()if not(k.startswith('AWS_')or k in {'DATABASE_URL','ATLAS_DATA_DIR','ATLAS_S3_BUCKET','ATLAS_S3_ENDPOINT','WS_COLLECTOR_MODULE_DIR'})};env.update(ATLAS_DATA_DIR=str(tmp_path/'isolated-api'),ATLAS_API_TOKEN='SYNTHETIC-TOKEN',ATLAS_S3_BUCKET='',PYTHONPATH=str(repo/'backend'),PYTHONDONTWRITEBYTECODE='1')
 result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,text=True,env=env,timeout=45)
 assert result.returncode==0,result.stdout+result.stderr
 assert'API PASS'in result.stdout

@pytest.mark.asyncio
async def test_delayed_account_module_is_waited_for_not_assumed_or_rejected(env,browser_pool):
 browser_pool.html=browser_pool.html.replace('My Account SYNTHETIC BUSINESS</a>','My Account</a>')+"<script>setTimeout(()=>document.querySelector('header a').textContent='My Account SYNTHETIC BUSINESS',60)</script>"
 c=m.WsdisplaySourceCollector(env,browser_pool,limiter=Limiter());j=await c.start(['https://www.wsdisplay.com/SYN-OUT'],{'account_marker':MARKER});out=await finish(c,j['id']);assert out['status']=='completed',out;await c.close()

@pytest.mark.asyncio
async def test_external_private_resource_is_refused_before_continue_or_permit(env,monkeypatch):
 class P:main_frame=object()
 class R:
  request=type('Req',(),{'url':'http://127.0.0.1/private','method':'GET','resource_type':'image','frame':None})()
  aborted=False
  async def abort(self):self.aborted=True
  async def continue_(self):raise AssertionError('private resource must never reach network')
 async def reject(url):raise ValueError('private address')
 async def check():pass
 monkeypatch.setattr(m,'validate_url',reject);l=Limiter();n=m.NativeSession(P(),l,URL,MARKER,check);r=R();await n.guard(r);assert r.aborted and n.failure=='invalid_source_resource' and not l.grants

@pytest.mark.asyncio
async def test_validated_external_static_origin_reused_without_source_body_reads(env,monkeypatch):
 class P:main_frame=object()
 class R:
  request=type('Req',(),{'url':'https://static.example.test/a.js','method':'GET','resource_type':'script','frame':None})()
  continued=0
  async def abort(self):raise AssertionError('public static should pass')
  async def continue_(self):self.continued+=1
 seen=[]
 async def valid(url):seen.append(url)
 async def check():pass
 monkeypatch.setattr(m,'validate_url',valid);l=Limiter();n=m.NativeSession(P(),l,URL,MARKER,check);r=R();await n.guard(r);await n.guard(r);assert r.continued==2 and len(seen)==1 and not l.grants
