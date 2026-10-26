import json
from pathlib import Path
import pytest
from crawler.printograph_modern_browser import SERVICE_READ_JS,prepare_services,service_signature,modern_bound,ModernHeld
import test_printograph_modern_browser as old
def source_body():
 # Actual source relationship: unselected Let Us Design uses the current numeric
 # proofType as its id and for; it remains an observed sibling of safe Manual.
 body=old.html().replace('<fieldset><input type=radio name="proofType"','<fieldset><input type=radio name="proofType"',1)
 added='<input type="radio" name="proofType" id="901" value="101"><label for="901">Let Us Design</label>'
 body=body.replace('<input type=radio name="proofTypeForManual"',added+'<input type=radio name="proofTypeForManual"',1)
 body=body.replace('</script>',"const ds=root.querySelector('input[name=proofType][value=\"101\"]'),dslabel=[...ds.labels][0];for(const e of root.querySelectorAll('input[name=proofType]'))e.addEventListener('change',()=>{ds.id=String(selected.proofType);dslabel.htmlFor=ds.id});</script>")
 return body
@pytest.fixture
async def page_fixture():
 from playwright.async_api import async_playwright
 async with async_playwright() as p:
  assert Path(p.chromium.executable_path).exists(),'Local Chromium required'
  browser=await p.chromium.launch(headless=True);ctx=await browser.new_context(service_workers='block');page=await ctx.new_page();seen=[]
  async def route(r):
   seen.append(r.request.url)
   if r.request.url==old.URL:await r.fulfill(status=200,content_type='text/html',body=source_body())
   elif r.request.url.startswith('https://www.printograph.com/service/rest/v1/products/'):await r.fulfill(status=200,json=old.PRICE)
   elif r.request.url=='https://www.printograph.com/service/rest/v1/rules/turnarounds':await r.fulfill(status=200,json=old.PRODUCTION)
   else:await r.abort();raise AssertionError('External fixture request forbidden')
  await ctx.route('**/*',route);await page.goto(old.URL)
  try:yield page,seen
  finally:await ctx.close();await browser.close()
async def prepare(page):
 async def check():pass
 async def mark():pass
 async def settle():await page.wait_for_timeout(30)
 return await prepare_services(page,old.URL,check,mark,settle)
@pytest.mark.asyncio
async def test_source_numeric_sibling_prepares_exact_safe_policy(page_fixture):
 page,seen=page_fixture
 observed,policy=await prepare(page)
 assert policy=={'proofType':'902','proofTypeForManual':'712','fileDeliveryForManual':'502','designServices':'608'}
 assert modern_bound(service_signature(observed))
 assert await page.evaluate('actions')==['process','proofType:902','proofTypeForManual:712','fileDeliveryForManual:502','designServices:608']
 ds=next(c for g in observed['groups'] if g['key']=='proofType' for c in g['choices'] if c['value']=='101')
 assert ds['id']=='902' and ds['checked'] is False
 assert all(u==old.URL or '/service/rest/v1/' in u for u in seen)
@pytest.mark.asyncio
@pytest.mark.parametrize('id',['31','100','101','9safe-id_7'])
async def test_bounded_numeric_start_id_is_identity_not_source_service_value(page_fixture,id):
 page,_=page_fixture;await page.evaluate('id=>{const e=document.querySelector("input[value=\\\"101\\\"]");const l=e.labels[0];e.id=id;l.htmlFor=id}',id)
 raw=await page.evaluate(SERVICE_READ_JS,{'source_url':old.URL});assert raw['observed'] is True
 assert next(c for g in raw['groups'] if g['key']=='proofType' for c in g['choices'] if c['value']=='101')['id']==id
@pytest.mark.asyncio
@pytest.mark.parametrize('id',['','bad id','<private>','a'*129,'proofType901'])
async def test_empty_unsafe_oversized_and_duplicate_ids_stay_held(page_fixture,id):
 page,_=page_fixture;await page.evaluate('id=>{const e=document.querySelector("input[value=\\\"101\\\"]");const l=e.labels[0];e.id=id;l.htmlFor=id}',id)
 raw=await page.evaluate(SERVICE_READ_JS,{'source_url':old.URL});assert raw=={'observed':False,'reason':'modern_service_group_unproven'}
 with pytest.raises(ModernHeld):await prepare(page)
 assert await page.evaluate('actions')==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('value,accepted',[('0',True),('9'+'a'*127,True),('1'*129,False),('١٠١',False),('_numeric',False),('9:other',False)])
async def test_numeric_identity_ascii_bounds_in_actual_dom(page_fixture,value,accepted):
 page,seen=page_fixture
 await page.evaluate('value=>{const e=document.querySelector("input[value=\\\"101\\\"]"),label=e.labels[0];e.id=value;label.htmlFor=value}',value)
 raw=await page.evaluate(SERVICE_READ_JS,{'source_url':old.URL})
 assert raw['observed'] is accepted
 if not accepted:assert raw['reason']=='modern_service_group_unproven'
 assert await page.evaluate('actions')==[] and seen==[old.URL]
