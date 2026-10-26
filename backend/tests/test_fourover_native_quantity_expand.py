"""Native ShowMore only; saved original methods + intercepted Chromium."""
import asyncio,hashlib,json,time,subprocess,shutil
from pathlib import Path
from copy import deepcopy
import pytest
from test_fourover_automatic_profile import load_driver,session_fixture,PRIVATE
from test_fourover_native_proceed_transition import browser_fixture,bind,method
F=Path(__file__).resolve().parent/'fixtures/fourover_native_quantity_expand'
Q=[250,500,1000,2500,5000,10000,15000,30000,35000]

async def install_collapsed(page):
 source=(F/'product-run-size.js').read_text();methods={n:method(source,n) for n in ['isCollapsable','isExpanded','collapsibleToggle','showMore','showLess']}
 await page.evaluate('''({methods,quantities})=>{
  const old=document.querySelector('ul');old.remove();const ul=document.createElement('ul');ul.className='runsizes-table';document.querySelector('#options').prepend(ul);
  fixture.rows(quantities.map(q=>({qty:q,price:10.006})));
  const view={lineLimitThreshold:7,filteredPackagePrices:()=>fixture.rows.peek()};let limit=7;
  const render=()=>{ul.replaceChildren();for(const row of view.filteredPackagePrices().slice(0,limit)){
   const li=document.createElement('li');li.className='runsizes_row'+(fixture.quantity.peek()===row.qty?' selected-qty':'');
   const span=document.createElement('span');span.className='runsize';span.textContent=String(row.qty);li.append(span);
   li.addEventListener('click',()=>{fixture.quantity(row.qty);fixture.data.runSize(row.qty);render();});ul.append(li);}
   const li=document.createElement('li');li.className='runsizes-table_actions'+(limit>7?' active':'');li.setAttribute('data-bind',"css: {'active': isExpanded()}, visible: isCollapsable()");
   const span=document.createElement('span');span.setAttribute('data-bind',"click: collapsibleToggle, i18n: isExpanded() ? 'Show Less' : 'Show More'");span.textContent=limit>7?'Show Less':'Show More';
   span.addEventListener('click',()=>{fixture.expandClicks++;view.collapsibleToggle();});li.append(span);ul.append(li);};
  view.lineLimit=(...args)=>{if(args.length){limit=args[0];render();}return limit;};
  Object.assign(view,Object.fromEntries(Object.entries(methods).map(([k,v])=>[k,eval('('+v+')')])));fixture.expandClicks=0;fixture.quantityView=view;render();
 }''',{'methods':methods,'quantities':Q})

def prepare(m,s,page):
 selection=bind(m,s,page);s.stage='child';selection['run_size']=30000
 table={'rows':[{'quantity':q,'price':'10.006'} for q in Q],'page_generation':s.session_id,'attributes':dict(selection['attributes']),'sha256':'e'*64,'evidence':{'object_path':'synthetic','sha256':'e'*64},'price_epoch':0}
 s.tables['700']=table
 s.public_assets.update({n:{'sha256':sha,'source_static_prefix':s.prefix} for n,sha in m.QUANTITY_EXPAND_PUBLIC_ASSETS.items()})
 return selection,table

def test_two_native_source_files_and_default_collapsed_limit_are_pinned():
 manifest=json.loads((F/'manifest.json').read_text())
 for n,sha in manifest['files'].items():assert hashlib.sha256((F/n).read_bytes()).hexdigest()==sha
 source=(F/'product-run-size.js').read_text();template=(F/'product-run-size.html').read_text()
 assert 'lineLimitThreshold: 7' in source and 'lineLimit: 7' in source and 'this.lineLimit(this.lineLimitThreshold);' in source
 assert 'filteredPackagePrices().slice(0, lineLimit())' in template and "click: collapsibleToggle, i18n: isExpanded() ? 'Show Less' : 'Show More'" in template

@pytest.mark.asyncio
async def test_actual_native_ShowMore_reveals_eighth_source_quantity_without_selecting_or_quoting(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection,table=prepare(m,s,page);await install_collapsed(page)
  assert '30000' not in (await s.state())['available_run_sizes']
  await s.reveal_quantity(selection,table)
  after=await s.state();assert '30000' in after['available_run_sizes'] and after['run_size']=='250'
  assert await page.evaluate('fixture.expandClicks')==1 and await page.evaluate('fixture.quantity.peek()')==250
  await s.reveal_quantity(selection,table)
  assert await page.evaluate('fixture.expandClicks')==1 and await page.evaluate('fixture.requests')==0 and await page.evaluate('privateReads')==0
  assert not s.cursor['receipts'] and s.cursor['inflight'] is None and s.counts['summary']==0
  assert not (tmp_path/'fourover-native-observation-selections').exists() and len(seen)==1

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',[
 'unproved','source','child','attribute','service','shipping','loading','controller','quantity_domain','price','duplicate_table','partial','search','label','binding','active','hidden','duplicate_action','cart_anchor'])
async def test_atomic_native_expansion_rejects_changed_or_unproved_context_without_click(tmp_path,mode):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection,table=prepare(m,s,page);await install_collapsed(page)
  js={'source':"history.replaceState(null,'','/other-source')",'child':"fixture.selected('701')",'attribute':"document.querySelector('select').value='11'",
   'service':"document.querySelector('[name=job_sample]').checked=true",'shipping':"fixture.tabs.selectedTab('product.shipping')",'loading':"fixture.tabs.isLoading(true)",
   'controller':"fixture.modules['FourOver_Project/js/model/product-price-processor'].getItemTotalPrice=()=>0",
   'quantity_domain':"fixture.rows.peek()[8].qty=36000",'price':"fixture.rows.peek()[8].price=999",
   'duplicate_table':"const e=document.querySelector('.runsizes-table');e.after(e.cloneNode(true))",'partial':"document.querySelector('.runsizes_row').remove()",
   'search':"const e=document.createElement('input');e.id='run-size-search';e.value='250';document.querySelector('form').append(e)",
   'label':"document.querySelector('.runsizes-table_actions span').textContent='Add To Cart'",'binding':"document.querySelector('.runsizes-table_actions span').setAttribute('data-bind','click: unknownAction')",
   'active':"document.querySelector('.runsizes-table_actions').classList.add('active')",'hidden':"document.querySelector('.runsizes-table_actions').hidden=true",
   'duplicate_action':"const e=document.querySelector('.runsizes-table_actions span');e.after(e.cloneNode(true))",
   'cart_anchor':"const e=document.querySelector('.runsizes-table_actions span'),a=document.createElement('a');a.href='/checkout/cart/add';e.replaceWith(a);a.append(e)"}
  if mode in js:await page.evaluate(js[mode])
  result=await page.evaluate(m.quantity_expand_js(selection['attributes'],s.source,controller_proved=True,ui_proved=True,assets_proved=mode!='unproved'),{'selection':selection,'rows':table['rows']})
  assert result['status']=='unavailable' and set(result)=={'status','reason'} and PRIVATE not in json.dumps(result)
  assert await page.evaluate('fixture.expandClicks')==0 and await page.evaluate('fixture.requests')==0 and await page.evaluate('privateReads')==0

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['foreign_table','wrong_generation','wrong_attributes','spent','inflight','summary','expired'])
async def test_python_preaction_scope_and_budget_guards_prevent_expansion(tmp_path,mode):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection,table=prepare(m,s,page);await install_collapsed(page)
  if mode=='foreign_table':s.tables['700']=deepcopy(table)
  if mode=='wrong_generation':table['page_generation']='anotherpage'
  if mode=='wrong_attributes':table['attributes']={'111':'11','999':'20'}
  if mode=='spent':assert m.reserve_once(tmp_path,s.source,selection,'f'*64)
  if mode=='inflight':s.cursor['inflight']={'selection':selection}
  if mode=='summary':s.summary_request_observed=True
  if mode=='expired':s.deadline=time.monotonic()-.01
  with pytest.raises(m.Held):await s.reveal_quantity(selection,table)
  assert await page.evaluate('fixture.expandClicks')==0 and await page.evaluate('fixture.requests')==0

@pytest.mark.asyncio
async def test_partial_collapsed_render_waits_within_existing_budget_then_expands_once(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection,table=prepare(m,s,page);await install_collapsed(page);s.deadline=time.monotonic()+2
  await page.evaluate("document.querySelector('.runsizes_row').remove();setTimeout(()=>fixture.quantityView.lineLimit(7),100)")
  await s.reveal_quantity(selection,table)
  assert await page.evaluate('fixture.expandClicks')==1 and s.deadline<time.monotonic()+2
  assert not m.spent(tmp_path,s.source,'700',30000)

@pytest.mark.parametrize('value',[None,{}, {'status':[]},{'status':'unavailable','reason':PRIVATE},{'status':'unavailable','reason':[]},{'status':'clicked','private':PRIVATE}])
def test_diagnostics_are_closed_public_status_and_reason_enums(value):
 assert load_driver().safe_quantity_expand_diagnostic(value) is None

@pytest.mark.asyncio
async def test_collector_expands_then_selects_eighth_unspent_q_and_captures_exact_quote_once(tmp_path):
 from types import SimpleNamespace
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection,table=prepare(m,s,page);await install_collapsed(page);s.selection=None;s.wait=m.AutomaticFourOverSession.wait.__get__(s)
  s.catalog['entries'][0]['historical_prices']={q:'8.00' for q in Q}
  for q in Q[:7]:assert m.reserve_once(tmp_path,s.source,{'child_id':'700','run_size':q,'attributes':dict(selection['attributes'])},'f'*64)
  await page.evaluate('''()=>{const original=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(body){const p=new URLSearchParams(body);p.set('run_size',String(fixture.quantity.peek()));return original.call(this,p.toString());};}''')
  class Response:
   status=200;url='https://4over.com/print/project/summary/';headers={'content-type':'application/json'}
   async def body(self):return b'{"totals":{"subtotal":"10.01","grand_total":"11.26","small_qty_fee":"1.25"}}'
   async def dispose(self):pass
  async def acquire(*args):return 'synthetic'
  async def fetch(route):return Response()
  async def fulfill(route,response):await route.fulfill(status=200,content_type='application/json',body=await response.body())
  s.observer._acquire=acquire;s.observer._fetch=fetch;s.observer._fulfill=fulfill;s.observer.limiter=SimpleNamespace(release=lambda *args:None)
  await page.route('https://4over.com/print/project/summary/',s.guard)
  assert await asyncio.wait_for(s.collect(),15) is False
  saved=next(iter(s.cursor['receipts'].values()));assert saved['status']=='verified' and saved['quantity']==30000
  record=json.loads((tmp_path/saved['evidence']['object_path']).read_bytes())
  assert record['selection']['run_size']==30000 and record['request_context']['run_size']==30000
  assert record['production']=={'value':'10','label':'2-4 Business Days'} and s.counts['summary']==1
  assert await page.evaluate('fixture.expandClicks')==1 and await page.evaluate('fixture.clicks')==1 and await page.evaluate('privateReads')==0
  assert s.cursor['processed_count']==s.cursor['verified_count']==1 and s.result(False)['status']=='continuation_required'
  await s.proceed_identity.dispose();s.proceed_identity=None


def test_exact_original_AMD_collapse_methods_replay_without_quantity_writes():
 result=subprocess.run([shutil.which('node'),str(F/'replay.js')],check=True,text=True,capture_output=True)
 row=json.loads(result.stdout);assert row['status']=='PASS_NATIVE_DEFAULT_COLLAPSE_REPRODUCED' and row['selected_quantity_unchanged'] is True
 assert row['selected_model_writes']==row['supplier_calls']==row['runtime_mutations']==0 and row['after_target_visible'] is True
