"""Intercepted local Chromium using the saved source's Process card/spec-radio structure."""
import ast,json
from pathlib import Path
import pytest
from crawler.printograph_modern_browser import SERVICE_READ_JS,SERVICE_ACTION_JS,prepare_services,ModernHeld
from crawler.adapters.browser import SNAPSHOT_JS,normalize_snapshot
import test_printograph_modern_browser as old

HEADER='<div class="card-header" id="headingOne"><h5 class="mb-0"><button class="btn btn-link" type="button" aria-expanded="true" aria-controls="collapseOne"><span style="float:left;font-weight:bold">Process Type</span><i id="icon" class="fas fa-plus"></i></button></h5></div>'
RADIOS=''.join(f'<div role="radiogroup" aria-labelledby="thicknessLabel{i}"><span id="thicknessLabel{i}" class="sr-only">{label}</span><input class="custom-control-input" type="radio" name="thickness{i}" id="thickness{i}" value="{i+1}" '+('checked' if i==0 else '')+f'><label class="custom-control-label" for="thickness{i}">{label}</label></div>' for i,label in enumerate(['1/4 inch','1/2 inch']))
def source_structure(body,radios=True):
 body=body.replace('<button type=button aria-controls=collapseOne>Process</button>',HEADER)
 if radios:
  body=body.replace('<label for=quantity>',RADIOS+'<label for=quantity>')
  body=body.replace("specs:['size']","specs:['size','thickness']").replace("const selected={size:'11',","const selected={thickness:'1',size:'11',")
  body=body.replace('</script>',"for(const e of root.querySelectorAll('input[name^=thickness]'))e.onchange=()=>{selected.thickness=e.value;for(const x of root.querySelectorAll('input[name^=thickness]'))x.checked=x===e;actions.push('thickness:'+e.value);nativeQuote()};</script>")
 return body
@pytest.fixture
async def source_page():
 from playwright.async_api import async_playwright
 async with async_playwright() as runtime:
  if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
  browser=await runtime.chromium.launch(headless=True);context=await browser.new_context(service_workers='block');page=await context.new_page();seen=[]
  async def route(r):
   seen.append(r.request.url)
   if r.request.url==old.URL:await r.fulfill(status=200,content_type='text/html',body=source_structure(old.html()))
   elif r.request.url.startswith('https://www.printograph.com/service/rest/v1/products/'):await r.fulfill(status=200,json=old.PRICE)
   elif r.request.url=='https://www.printograph.com/service/rest/v1/rules/turnarounds':await r.fulfill(status=200,json=old.PRODUCTION)
   else:await r.abort();raise AssertionError('External source forbidden')
  await context.route('**/*',route);await page.goto(old.URL)
  try:yield page,seen
  finally:await context.close();await browser.close()
async def prepared(page):
 async def check():pass
 async def mark():pass
 async def settle():await page.wait_for_timeout(40)
 return await prepare_services(page,old.URL,check,mark,settle)

@pytest.mark.asyncio
async def test_actual_process_type_structure_is_native_action(source_page):
 page,seen=source_page
 value,policy=await prepared(page)
 assert value['process_header']['text_class']=='process_type' and value['process_header']['panel_count']==value['process_header']['button_count']==1
 assert await page.evaluate('actions')==['process','proofType:902','proofTypeForManual:712','fileDeliveryForManual:502','designServices:608']
 assert all(u==old.URL or '/service/rest/v1/' in u for u in seen)

@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['private_label','duplicate','hidden','disabled','missing_panel','submit'])
async def test_unproven_process_header_diagnostics_are_typed_and_private_text_free(source_page,mode):
 page,_=source_page
 script={'private_label':"document.querySelector('button').textContent='"+old.PRIVATE+"'",'duplicate':"document.querySelector('button').after(document.querySelector('button').cloneNode(true))",'hidden':"document.querySelector('button').style.display='none'",'disabled':"document.querySelector('button').disabled=true",'missing_panel':"document.querySelector('#collapseOne').remove()",'submit':"document.querySelector('button').type='submit'"}[mode]
 await page.evaluate(script);value=await page.evaluate(SERVICE_READ_JS,{'source_url':old.URL})
 assert value['observed'] is False and value['reason']=='modern_process_header_unproven'
 d=value['process_header'];assert d['schema']=='printograph-process-header-diagnostic/v1'
 assert set(d)=={'schema','panel_count','button_count','button_type','enabled','visible','text_class'}
 assert 0<=d['panel_count']<=3 and 0<=d['button_count']<=3 and d['text_class'] in {'process','process_type','other','unavailable'}
 assert old.PRIVATE not in json.dumps(value)
 with pytest.raises(ModernHeld):await prepared(page)
 assert await page.evaluate('actions')==[]

@pytest.mark.asyncio
async def test_source_declared_spec_with_distinct_native_names_is_one_logical_group(source_page):
 page,_=source_page
 state=normalize_snapshot(await page.evaluate(SNAPSHOT_JS,{'modernServices':True}))
 groups=[x for x in state['controls'] if x['kind']=='radio'];assert [x['key'] for x in groups]==['thickness']
 assert [x['value'] for x in groups[0]['choices']]==['1','2'] and groups[0]['value']=='1'
 await page.locator('#thickness1').click()
 state=normalize_snapshot(await page.evaluate(SNAPSHOT_JS,{'modernServices':True}));group=next(x for x in state['controls'] if x['key']=='thickness')
 assert group['value']=='2' and await page.evaluate('controller.jsonObj.userSelectedOptions.thickness')=='2'
 legacy=normalize_snapshot(await page.evaluate(SNAPSHOT_JS,{'modernServices':False}))
 assert {'thickness0','thickness1'}<={x['key'] for x in legacy['controls']}

@pytest.mark.asyncio
@pytest.mark.parametrize('drift',['undeclared','id','private','duplicate_value'])
async def test_radio_grouping_requires_actual_declared_normal_native_identity(source_page,drift):
 page,_=source_page
 script={'undeclared':"controller.fields['47'].specs=['size']",'id':"document.querySelector('#thickness1').id='foreign'",'private':"controller.jsonObj.others.isAdmin=true",'duplicate_value':"document.querySelector('#thickness1').value='1'"}[drift]
 await page.evaluate(script)
 state=normalize_snapshot(await page.evaluate(SNAPSHOT_JS,{'modernServices':True}))
 # Invalid identity is not collapsed into one valid two-choice logical field.
 assert not any(c['key']=='thickness' and len(c['choices'])==2 for c in state['controls'])

@pytest.mark.asyncio
async def test_full_browser_walk_with_source_process_card_and_logical_radios(monkeypatch):
 original=old.html
 monkeypatch.setattr(old,'html',lambda mode='good':source_structure(original(mode)))
 await old.test_full_adapter_native_service_walk_freshness_and_bounded_recovery(monkeypatch,'good',4)

@pytest.mark.asyncio
async def test_exact_adapter_read_persists_bounded_typed_header_diagnostic_once():
 from types import SimpleNamespace
 source=Path(__file__).resolve().parents[1]/'crawler/adapters/browser.py'
 tree=ast.parse(source.read_text());fn=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='modern_read')
 value={'observed':False,'reason':'modern_process_header_unproven','process_header':{'schema':'printograph-process-header-diagnostic/v1','panel_count':1,'button_count':1,'button_type':'button','enabled':True,'visible':True,'text_class':'other'}}
 async def check():pass
 async def evaluate(*args):return json.loads(json.dumps(value))
 events=[]
 async def emit(name,data):events.append((name,data))
 ns={'transport':SimpleNamespace(check=check),'page':SimpleNamespace(evaluate=evaluate),'url':old.URL,'SERVICE_READ_JS':SERVICE_READ_JS,'navigation':{'generation':1},'modern_header_diagnostics':set(),'emit':emit,'json':json}
 exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),'exec'),ns)
 await ns['modern_read']();await ns['modern_read']()
 assert len(events)==1 and events[0][0]=='modern_process_header_diagnostic' and events[0][1]==value['process_header']
 value['process_header']['visible']=False;await ns['modern_read']();assert len(events)==2
 for i in range(50):ns['navigation']['generation']=i+2;await ns['modern_read']()
 assert len(events)==32 and all(old.PRIVATE not in json.dumps(data) for _,data in events)
