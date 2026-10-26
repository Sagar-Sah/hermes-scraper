"""Exact native summary structure, callback barrier and bounded session proof."""
import asyncio,hashlib,json,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_fourover_automatic_profile import load_driver,session_fixture,PRIVATE
from test_fourover_native_proceed_transition import browser_fixture,bind
F=Path(__file__).resolve().parent/'fixtures/fourover_native_summary_totals'

async def install_summary(page,*,classic=False,fill=True):
 source=(F/'native-summary.html').read_text();a=source.index('<div class="project-summary_totals">');b=source.index('</div>',a)+6
 await page.evaluate('''({markup,classic,fill})=>{
  document.querySelectorAll('.project-summary').forEach(e=>e.remove());
  const t=document.createElement('template');t.innerHTML=markup;const totals=t.content.firstElementChild;
  for(const li of totals.querySelectorAll('li'))if(!li.classList.contains('grand-total')&&!li.querySelector('[data-bind*="Subtotal"]'))li.remove();
  const sub=totals.querySelector('li:not(.grand-total)'),grand=totals.querySelector('.grand-total');
  sub.children[0].className='label';sub.children[0].textContent='Subtotal';sub.children[1].className='value';
  for(const label of [...grand.children].filter(e=>e.getAttribute('data-bind')?.includes('label'))){const isClassic=label.getAttribute('data-bind').includes("'Total:'");if(isClassic!==classic)label.remove();else {label.className='label';label.textContent=classic?'Total:':'Total';}}
  const value=grand.lastElementChild;value.className='value';value.querySelector('i').textContent='Taxes may apply';
  const root=document.createElement('div');root.className='project-summary';root.append(totals);document.querySelector('main').append(root);
  for(const e of document.querySelectorAll('#subtotal,#total'))e.hidden=true;
  fixture.fillSummary=(subTotal,grandTotal)=>{sub.children[1].textContent='$'+subTotal;value.querySelector('span').textContent='$'+grandTotal;};
  if(fill)fixture.fillSummary('10.01','11.26');
 }''',{'markup':source[a:b],'classic':classic,'fill':fill})

def prove(m,s):
 s.retain_public_asset(m.SUMMARY_PUBLIC_MODULE,(F/'native-summary.js').read_bytes(),'application/javascript')
 assert s.summary_module_proved is True

def test_native_source_fixture_pins_and_summary_module_default_template():
 for n,h in json.loads((F/'manifest.json').read_text())['files'].items():assert hashlib.sha256((F/n).read_bytes()).hexdigest()==h
 assert "template: 'FourOver_Project/project/summary'" in (F/'native-summary.js').read_text()
 assert "this.projectTotals()[projectKey] = response['totals'];" in (F/'native-summary.js').read_text()

@pytest.mark.asyncio
@pytest.mark.parametrize('classic',[False,True])
async def test_exact_native_structure_reads_subtotal_and_total_without_colon_or_tax_text_falsehold(tmp_path,classic):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  bind(m,s,page);prove(m,s);await install_summary(page,classic=classic)
  # Generic unrelated per-set text must not contaminate native summary subtotal.
  await page.evaluate("const e=document.createElement('div');e.textContent='Price Per Set: $999.00';document.querySelector('main').append(e)")
  state=await s.state()
  assert state['native_summary_money']['status']=='observed'
  assert state['money']==[{'kind':'subtotal','value':'10.01'},{'kind':'total','value':'11.26'}]
  assert await page.evaluate('privateReads')==0 and len(seen)==1

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['unproved','duplicate_root','duplicate_totals','duplicate_subtotal','duplicate_total','subtotal_binding','grand_binding','total_label','subtotal_text','extra_value_child','hidden_value','malformed_money','private_label','tax_binding'])
async def test_source_native_money_probe_rejects_unknown_ambiguous_or_partial_structure(tmp_path,mode):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  bind(m,s,page);prove(m,s);await install_summary(page)
  js={'duplicate_root':"const e=document.querySelector('.project-summary');e.after(e.cloneNode(true))",'duplicate_totals':"const e=document.querySelector('.project-summary_totals');e.after(e.cloneNode(true))",
   'duplicate_subtotal':"const e=document.querySelector('.project-summary_totals li:not(.grand-total)');e.after(e.cloneNode(true))",'duplicate_total':"const e=document.querySelector('.grand-total');e.after(e.cloneNode(true))",
   'subtotal_binding':"document.querySelector('.project-summary_totals li:not(.grand-total) .value').setAttribute('data-bind',\"css:'value',text:formatPrice(getTotals()['shipping'])\")",
   'grand_binding':"document.querySelector('.grand-total .value span').setAttribute('data-bind',\"text:formatPrice(getTotals()['tax'])\")",
   'total_label':"document.querySelector('.grand-total .label').textContent='Cart Total'",'subtotal_text':"document.querySelector('.project-summary_totals li:not(.grand-total) .label').textContent='Cart Subtotal'",
   'extra_value_child':"document.querySelector('.grand-total .value').append(document.createElement('span'))",'hidden_value':"document.querySelector('.grand-total .value span').hidden=true",
   'malformed_money':"fixture.fillSummary('1,0.01','11.26')",'private_label':"document.querySelector('.grand-total .label').textContent='SYNTHETIC-PRIVATE-CUSTOMER-NAME'",
   'tax_binding':"document.querySelector('.grand-total i').setAttribute('data-bind',\"i18n:'Private'\")"}
  if mode in js:await page.evaluate(js[mode])
  if mode=='unproved':s.summary_module_proved=False
  state=await s.state();assert state['native_summary_money']['status']!='observed' and state['money']==[]
  diag=m.public_money_diagnostic(state);assert PRIVATE not in json.dumps(diag) and 'SYNTHETIC-PRIVATE-CUSTOMER-NAME' not in json.dumps(diag)
  assert await page.evaluate('privateReads')==0 and await page.evaluate('fixture.requests')==0

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['delayed','wrong','context_drift','epoch_drift','summary_changed'])
async def test_render_wait_preserves_exact_money_context_once_and_original_deadline(tmp_path,mode):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection=bind(m,s,page);prove(m,s);await install_summary(page,fill=False);s.wait=m.AutomaticFourOverSession.wait.__get__(s)
  s.summary={'epoch':s.epoch,'request_context':{'fixture':'only'},'totals':{'subtotal':'10.01','grand_total':'11.26'}}
  # Full context remains independently tested by the native handoff suite;
  # this fixture supplies its exact normalized projection to isolate the wait.
  s.quote_context_state=lambda state,selection:state
  original=m.q1000_context_retained;m.q1000_context_retained=lambda *a,**k:True
  s.deadline=time.monotonic()+(.15 if mode=='wrong' else 2);deadline=s.deadline
  async def mutate():
   await asyncio.sleep(.04)
   if mode=='epoch_drift':s.epoch+=1
   elif mode=='summary_changed':s.summary=dict(s.summary)
   elif mode=='context_drift':await page.evaluate("fixture.modules['FourOver_Project/js/model/product-price-processor'].getItemTotalPrice=()=>0")
   else:await page.evaluate("fixture.fillSummary('10.01','99.99')" if mode=='wrong' else "fixture.fillSummary('10.01','11.26')")
  task=asyncio.create_task(mutate())
  try:
   if mode=='delayed':
    state=await s.wait_rendered_quote(selection);assert state['money'][1]['value']=='11.26'
   else:
    with pytest.raises(m.Held):await s.wait_rendered_quote(selection)
  finally:m.q1000_context_retained=original;await task
  assert s.deadline==deadline and await page.evaluate('fixture.requests')==0 and not s.cursor['receipts']

@pytest.mark.asyncio
async def test_full_collector_waits_for_delayed_source_shaped_render_then_records_exact_native_quote_once(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  bind(m,s,page);prove(m,s);await install_summary(page,fill=False);s.selection=None;s.stage='child';s.wait=m.AutomaticFourOverSession.wait.__get__(s)
  # The native browser callback renders later than intercepted response capture.
  await page.evaluate("()=>{const original=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(body){this.addEventListener('load',()=>setTimeout(()=>fixture.fillSummary('10.01','11.26'),150));return original.call(this,body);};}")
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
  row=next(iter(s.cursor['receipts'].values()));assert row['status']=='verified'
  record=json.loads((tmp_path/row['evidence']['object_path']).read_bytes())
  assert record['totals']['subtotal']=='10.01' and record['totals']['grand_total']=='11.26' and record['totals']['small_qty_fee']=='1.25'
  assert record['production']=={'value':'10','label':'2-4 Business Days'} and s.counts['summary']==1
  assert await page.evaluate('fixture.clicks')==1 and await page.evaluate('privateReads')==0 and len(s.cursor['receipts'])==1
  await s.proceed_identity.dispose();s.proceed_identity=None

@pytest.mark.parametrize('value',[None,{}, {'totals':{'private':PRIVATE}},{'totals':{'subtotal':PRIVATE,'grand_total':'1.00','small_qty_fee':'0.00','turnaround_time':'0.00','job_sample':'0.00','digital_proof':'0.00'}}])
def test_public_capture_diagnostics_reject_unknown_keys_or_private_text(value):assert load_driver().public_captured_totals(value) is None

@pytest.mark.asyncio
async def test_required_current_session_proof_survives_full_diagnostic_cap(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 # Fill all diagnostic slots. Dedicated current proof is intentionally empty.
 s.public_assets={f'opaque{i}':{} for i in range(256)}
 assert not s.quantity_expand_assets and s.summary_module_proved is False
 for name,h in m.QUANTITY_EXPAND_PUBLIC_ASSETS.items():
  source=Path(__file__).resolve().parents[3]/'tests/fixtures/fourover_native_quantity_expand'/('product-run-size.html' if name.endswith('.html') else 'product-run-size.js')
  # Candidate tests reuse normal portable public fixtures, no task-root paths.
  if not source.exists():source=Path(__import__('test_fourover_native_quantity_expand').__file__).parent/'fixtures/fourover_native_quantity_expand'/source.name
  raw=source.read_bytes();assert hashlib.sha256(raw).hexdigest()==h
  s.retain_public_asset(name,raw,'text/html' if name.endswith('.html') else 'application/javascript')
 s.retain_public_asset(m.SUMMARY_PUBLIC_MODULE,(F/'native-summary.js').read_bytes(),'application/javascript')
 assert s.quantity_expand_assets==set(m.QUANTITY_EXPAND_PUBLIC_ASSETS) and s.summary_module_proved is True and len(s.public_assets)==256

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['suffix','bytes','foreign_page','mime','closed'])
async def test_dedicated_proof_rejects_wrong_or_foreign_public_body(tmp_path,mode):
 m=load_driver();s,_,_=session_fixture(m,tmp_path);s.public_assets={f'opaque{i}':{} for i in range(256)}
 name=m.SUMMARY_PUBLIC_MODULE;raw=(F/'native-summary.js').read_bytes();mime='application/javascript'
 if mode=='suffix':name='Unknown/summary.js'
 if mode=='bytes':raw+=b' '
 if mode=='foreign_page':s.page.url='https://4over.com/other-source'
 if mode=='mime':mime='text/html'
 if mode=='closed':s.closed=True
 s.retain_public_asset(name,raw,mime)
 assert not s.summary_module_proved and not s.quantity_expand_assets

@pytest.mark.asyncio
async def test_informational_tax_notice_may_be_hidden_while_both_native_money_values_remain_visible(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  bind(m,s,page);prove(m,s);await install_summary(page);await page.evaluate("document.querySelector('.grand-total i').hidden=true")
  state=await s.state();assert state['native_summary_money']['status']=='observed' and len(state['money'])==2

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['valid','mime','status','frame','url','prefix','foreign_page','oversize'])
async def test_script_capture_caller_enforces_exact_current_public_response_before_dedicated_proof(tmp_path,mode):
 m=load_driver();s,_,_=session_fixture(m,tmp_path);s.public_assets={f'opaque{i}':{} for i in range(256)};raw=(F/'native-summary.js').read_bytes();reads=[]
 class Response:
  status=200;headers={'content-type':'application/javascript'}
  async def body(self):reads.append('public');return raw
 response=Response();response.url=s.prefix+m.SUMMARY_PUBLIC_MODULE
 response.request=SimpleNamespace(frame=s.page.main_frame,method='GET',resource_type='script',url=response.url)
 if mode=='mime':response.headers={'content-type':'text/plain'}
 if mode=='status':response.status=403
 if mode=='frame':response.request.frame=object()
 if mode=='url':response.url='https://foreign.invalid/'+m.SUMMARY_PUBLIC_MODULE;response.request.url=response.url
 if mode=='prefix':response.url=s.prefix.replace('version1','version2')+m.SUMMARY_PUBLIC_MODULE;response.request.url=response.url
 if mode=='foreign_page':s.page.url='https://4over.com/other-source'
 if mode=='oversize':response.headers={'content-type':'application/javascript','content-length':str(3*1024*1024)}
 await s.capture_public_script(response)
 assert s.summary_module_proved is (mode=='valid') and len(s.public_assets)==256
 if mode not in {'valid','foreign_page'}:assert reads==[]
