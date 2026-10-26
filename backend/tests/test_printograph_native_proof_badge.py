import ast,copy,json,hashlib
from pathlib import Path
import pytest
import crawler.adapters.browser as adapter_module
from crawler.printograph_modern_browser import SERVICE_READ_JS,SERVICE_ACTION_JS,prepare_services,policy_available,ModernHeld,_service_choice_details,_INTENTS,FreshQuoteGate
import test_printograph_modern_browser as old
import test_printograph_numeric_service_ids as native
ROOT=Path(__file__).resolve().parents[1]
FIXTURES=Path(__file__).resolve().parent/"fixtures/printograph_native_proof_badge"
def body(badge='<span class="badge badge-danger"><span>$</span>3.00</span>'):
 return native.source_body().replace('>No Proof</label>','>No Proof '+badge+'</label>')
@pytest.fixture
async def page_fixture():
 from playwright.async_api import async_playwright
 async with async_playwright() as p:
  assert Path(p.chromium.executable_path).exists()
  browser=await p.chromium.launch(headless=True);ctx=await browser.new_context(service_workers='block');page=await ctx.new_page();seen=[]
  async def route(r):
   seen.append(r.request.url)
   if r.request.url==old.URL:await r.fulfill(status=200,content_type='text/html',body=body())
   elif r.request.url.startswith('https://www.printograph.com/service/rest/v1/products/'):await r.fulfill(status=200,json=old.PRICE)
   elif r.request.url=='https://www.printograph.com/service/rest/v1/rules/turnarounds':await r.fulfill(status=200,json=old.PRODUCTION)
   else:await r.abort();raise AssertionError('External fixture request forbidden')
  await ctx.route('**/*',route);await page.goto(old.URL)
  try:yield page,seen
  finally:await ctx.close();await browser.close()
async def prepared(page,module=None):
 async def check():pass
 async def mark():pass
 async def settle():await page.wait_for_timeout(30)
 return await (module.prepare_services if module else prepare_services)(page,old.URL,check,mark,settle)
@pytest.mark.asyncio
async def test_source_priced_proof_exact_native_policy(page_fixture):
 page,seen=page_fixture
 raw,policy=await prepared(page);proof=next(c for g in raw['groups'] if g['key']=='proofTypeForManual' for c in g['choices'] if c['checked'])
 assert proof['label']=='No Proof $3.00' and proof['policy_label']=='No Proof';assert policy['proofTypeForManual']=='712';assert policy_available(raw)
 assert await page.evaluate('actions')==['process','proofType:902','proofTypeForManual:712','fileDeliveryForManual:502','designServices:608']
 assert all(u==old.URL or '/service/rest/v1/' in u for u in seen)
@pytest.mark.asyncio
@pytest.mark.parametrize('html',['<span class="badge badge-danger">$3</span>','<span class="badge badge-danger"><span><span style="text-decoration:line-through">$5.00</span> </span><span>$</span>3.00</span>'])
async def test_source_single_and_struck_list_price_badges(html,page_fixture):
 page,_=page_fixture;await page.evaluate('h=>document.querySelector("label[for=proofTypeForManual712]").innerHTML="No Proof "+h',html);raw,_=await prepared(page)
 proof=next(c for g in raw['groups'] if g['key']=='proofTypeForManual' for c in g['choices'] if c['checked']);assert proof['policy_label']=='No Proof' and '$' in proof['label']
@pytest.mark.asyncio
@pytest.mark.parametrize('html',['<span class="other">$3.00</span>','<span class="badge badge-danger">private-secret</span>','<span><span class="badge badge-danger">$3.00</span></span>','<span class="badge badge-danger">$3.00</span><span class="badge badge-danger">$4.00</span>','<span class="badge badge-danger"><a>$3.00</a></span>','<span class="badge badge-danger extra">$3.00</span>','<span class="badge badge-danger">$3 $4 $5</span>','<span class="badge badge-danger">$3.00</span> Extra paid service'])
async def test_unknown_price_decorations_or_extra_policy_words_stay_held(html,page_fixture):
 page,_=page_fixture;await page.evaluate('h=>document.querySelector("label[for=proofTypeForManual712]").innerHTML="No Proof "+h',html)
 with pytest.raises(ModernHeld) as caught:await prepared(page)
 error=caught.value;assert error.reason=='modern_safe_service_choice_unavailable';assert error.details['group_key']=='proofTypeForManual';assert error.details['policy_match_count']==0;assert error.details['eligible_match_count']==0;assert 'private-secret' not in json.dumps(error.details);assert '$3' not in json.dumps(error.details)
@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['disabled','hidden','ambiguous'])
async def test_diagnostic_distinguishes_unavailable_or_ambiguous_safe_choice(mode,page_fixture):
 page,_=page_fixture
 script={'disabled':"document.querySelector('#proofTypeForManual712').disabled=true",'hidden':"document.querySelector('#proofTypeForManual712').style.display='none';document.querySelector('label[for=proofTypeForManual712]').style.display='none'",'ambiguous':"document.querySelector('#collapseOne').insertAdjacentHTML('beforeend','<input type=radio name=proofTypeForManual id=thirdProof value=713><label for=thirdProof>No Proof</label>')"}[mode]
 await page.evaluate(script)
 with pytest.raises(ModernHeld) as caught:await prepared(page)
 d=caught.value.details;assert d['group_key']=='proofTypeForManual';assert d['policy_match_count']==(2 if mode=='ambiguous' else 1);assert d['eligible_match_count']==(2 if mode=='ambiguous' else 0)
@pytest.mark.asyncio
async def test_full_label_identity_still_rejects_changed_price_badge_before_click(page_fixture):
 page,_=page_fixture;await prepared(page);await page.locator('#proofTypeForManual711').click();raw=await page.evaluate(SERVICE_READ_JS,{'source_url':old.URL});group=next(g for g in raw['groups'] if g['key']=='proofTypeForManual');target=next(c for c in group['choices'] if c['value']=='712');before=await page.evaluate('actions.length')
 await page.evaluate('document.querySelector("label[for=proofTypeForManual712] .badge").textContent="$4.00"')
 with pytest.raises(Exception,match='identity changed'):await page.evaluate(SERVICE_ACTION_JS,{'source_url':old.URL,'action':'radio','target':{**target,'retained':[c['value'] for c in group['choices'] if c['checked']],'expected_base_context':raw['base_context'],'expected_selection_context':raw['selection_context']}})
 assert await page.evaluate('actions.length')==before

def test_held_details_drop_unknown_private_fields_and_noninteger_counts():
 details={'schema':'printograph-safe-service-choice-diagnostic/v1','group_key':'proofTypeForManual','offered_count':2,'visible_count':2,'enabled_count':2,'checked_count':1,'policy_match_count':0,'eligible_match_count':0}
 assert ModernHeld('held',details).details==details
 for field,value in [('label','secret'),('offered_count',False),('offered_count',101),('group_key','secret'),('schema','foreign')]:
  d=copy.deepcopy(details);d[field]=value;assert ModernHeld('held',d).details is None

def test_current_native_total_may_include_fee_without_backend_arithmetic():
 g=FreshQuoteGate();old.observe(g);old.observe(g,'production');r=old.raw();r['native_total']['controller_total']='16.00';v=g.verify(r,old.CONTROL,displayed_subtotal='16.00');assert v['component_arithmetic_verified'] is False and v['source_data_finalized'] is False

def test_pinned_native_source_fixtures_include_schema_badge_render_and_scoped_fee():
 manifest=json.loads((FIXTURES/'manifest.json').read_bytes())
 assert manifest['schema']=='printograph-native-proof-price-badge-fixtures/v1'
 for name,digest in manifest['members'].items():assert hashlib.sha256((FIXTURES/name).read_bytes()).hexdigest()==digest
 assert b'fg={key:0,class:"badge badge-danger"}' in (FIXTURES/'proof-badge-class.js.txt').read_bytes()
 assert b'(0,o.CE)("span",fg' in (FIXTURES/'proof-label-render.js.txt').read_bytes()
 assert b'id:36,label:"No Proof"' in (FIXTURES/'proof-schema.js.txt').read_bytes()
 mapping=json.loads((FIXTURES/'observed-proof-option-mapping.json').read_bytes())['items']
 assert any(str(row['id'])=='103306006' and str(row['type']['id'])=='36' for row in mapping)
 price_fixture=json.loads((FIXTURES/'observed-no-proof-price-table.json').read_bytes())
 assert price_fixture['fixture_provenance']['synthetic'] is True
 assert price_fixture['fixture_provenance']['source_price_evidence'] is False
 table=price_fixture['items']
 assert len(table)==27 and all(row['markupPrice']=='7.25' for row in table)
 assert manifest['supplier_response_scope']['full_domain_or_current_freshness_claim'] is False

@pytest.mark.asyncio
async def test_actual_adapter_emit_expressions_preserve_only_sanitized_details():
 source=Path(adapter_module.__file__);tree=ast.parse(source.read_text());calls=[n for n in ast.walk(tree) if isinstance(n,ast.Await) and isinstance(n.value,ast.Call) and isinstance(n.value.func,ast.Name) and n.value.func.id=='emit' and n.value.args and isinstance(n.value.args[0],ast.Constant) and n.value.args[0].value in {'modern_quote_recovery','modern_quote_held'}];assert len(calls)==2
 details=_service_choice_details([{'label':old.PRIVATE,'visible':True,'disabled':False,'checked':True}],'proofTypeForManual',_INTENTS[1][1]);events=[]
 async def emit(name,value):events.append((name,value))
 for call in calls:
  fn=ast.AsyncFunctionDef(name='actual_emit',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),body=[ast.Expr(value=call)],decorator_list=[]);module=ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[]));error=ModernHeld('modern_safe_service_choice_unavailable',details);ns={'emit':emit,'first':error,'final_error':error};exec(compile(module,str(source),'exec'),ns);await ns['actual_emit']()
 assert {name for name,_ in events}=={'modern_quote_recovery','modern_quote_held'}
 assert all(value['details']==details and old.PRIVATE not in json.dumps(value) for _,value in events)
