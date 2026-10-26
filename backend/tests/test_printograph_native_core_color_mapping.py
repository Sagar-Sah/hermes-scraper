"""Native10019 core-color mapping; actual installed module, intercepted browser only."""
import hashlib,json
from pathlib import Path
import pytest
from crawler import printograph_modern_browser as m
import test_printograph_modern_post_prices as post
FIXTURES=Path(__file__).resolve().parent/'fixtures/printograph_native_core_color'
URL='https://www.printograph.com/products/acrylic-boards/order'
class Prior:
 SERVICE_READ_JS=(FIXTURES/'prior-service-read.js').read_text()
old=Prior()
@pytest.mark.asyncio
@pytest.mark.parametrize('change',['actual10019','material_color_different','foreign_type','private_field','canonical10016'])
async def test_current_mapping_matches_original_native_source_builders(change):
 from playwright.async_api import async_playwright
 async with async_playwright() as runtime:
  browser=await runtime.chromium.launch(headless=True);context=await browser.new_context(service_workers='block');page=await context.new_page();requests=[]
  async def route(r):
   requests.append(r.request.url)
   if r.request.url==URL:await r.fulfill(status=200,content_type='text/html',body=post.html())
   else:await r.abort();raise AssertionError('external request forbidden')
  await context.route('**/*',route)
  try:
   await page.goto(URL)
   field_source=(FIXTURES/'type10019-fields.js.txt').read_text()
   await page.evaluate('''({fields,change})=>{
    const nativeFields=Function('return ({'+fields+'})')()[10019],t=change==='canonical10016'||change==='foreign_type'?10016:10019;
    const selected=controller.jsonObj.userSelectedOptions;selected.coreColor='9';selected.manualProofType='1';
    selected.materialColor=change==='material_color_different'?'2':'1';
    controller.schema.materialColor.list=[{id:'1',label:'Clear'},{id:'2',label:'White'}];controller.schema.coreColor.list=[{id:'9',label:'Unrelated core value'}];
    controller.printProductType=t;controller.fields={[t]:nativeFields};controller.jsonObj.others.getGenericProductsPriceFromApiList=[t];
    if(change==='canonical10016'){controller.fields[t].other.hidCoreColorId='coreColor';controller.fields[t].specs.push('coreColor');}
    if(change==='private_field')controller.fields[t].other.hidCoreColorId='billingAddress';
    document.querySelector('#order-form').setAttribute('data-printproducttype',String(t));
    const e=document.querySelector('[name=proofTypeForManual]');e.value='1';e.labels[0].textContent='PDF Proof';
   }''',{'fields':field_source,'change':change})
   before=await page.evaluate(old.SERVICE_READ_JS,{'source_url':URL});observed=await page.evaluate(m.SERVICE_READ_JS,{'source_url':URL})
   assert before['observed'] and observed['observed']
   if change in {'foreign_type','private_field'}:
    assert before['generic_price_context']['observed'] is False and observed['generic_price_context']['observed'] is False
   else:
    assert observed['generic_price_context']['observed'] is True
    if change=='canonical10016':
     comparable=dict(observed)
     assert comparable.pop('native_nextday_presentation')=={'observed':False}
     assert 'native_nextday_presentation' not in before
     assert before==comparable
    else:assert before['generic_price_context']['observed'] is False
    snippets={name:(post.FIXTURES/file).read_text().split(':',1)[1] for name,file in [('parameters','generic-parameters.js'),('price','generic-price-builder.js'),('turn','native-turnaround-builder.js'),('options','native-options.js')]}
    native=await page.evaluate('''snippets=>{const parameters=eval('('+snippets.parameters+')'),options=eval('('+snippets.options+')');
     const o={A:{getPrintProductType:(e,t)=>t}},s={};s.parametersOnAddToCartStateless=(e,t,i,fields)=>JSON.stringify(options(e,t,fields,parameters(e,t,i,fields,{itemDetail:{type:e.userSelectedOptions.fileDelivery,productOptions:[],specifications:{printProduct:{},other:{}}}})));
     const X={A:{parametersOnAddToCart:s.parametersOnAddToCartStateless}},w={A:{getUrlParameter:()=>''}},R={A:{getGenericProductsPrice:(url,body)=>{window.captured={url,body};return {then:()=>({catch:()=>{}})}}}};
     eval('('+snippets.price+')').call({validParams:()=>true,storedGenericPrice:{}},controller.jsonObj,controller.printProductType,controller.schema,controller.fields);
     return {price:window.captured,turn:JSON.parse(eval('('+snippets.turn+')')(controller.jsonObj,controller.printProductType,controller.schema,controller.fields))};}''',snippets)
    bound=m.bind_service_policy(observed,m.PDF_SERVICE_POLICY,URL)
    assert m._scalar_tree(native['price']['body'])==m._scalar_tree({'properties':observed['generic_price_context']['properties']})
    m._generic_price_request(json.dumps(native['price']['body']),bound);m._turnaround_request(json.dumps(native['turn']),bound)
    selected='9' if change=='canonical10016' else '2' if change=='material_color_different' else '1'
    assert observed['generic_price_context']['properties']['other']['coreColorId']==selected
   assert requests==[URL]
  finally:await context.close();await browser.close()

def test_native_mapping_alias_is_exact_and_preserves_prior_reader_contract():
 new="(k.startsWith('hid')?(!own(hid,k)||(hid[k]!==v&&!(String(t)==='10019'&&k==='hidCoreColorId'&&v==='materialColor'))):!/^[A-Za-z][A-Za-z ]{0,63}$/.test(k))"
 original="(k.startsWith('hid')?(!own(hid,k)||hid[k]!==v):!/^[A-Za-z][A-Za-z ]{0,63}$/.test(k))"
 assert m.SERVICE_READ_JS.count(new)==1
 start=" const generic_price_context=(()=>{try{"
 end=" }catch{return {observed:false};}})();"
 def generic_projection(reader):
  assert reader.count(start)==1 and reader.count(end)==1
  first=reader.index(start)
  return reader[first:reader.index(end,first)+len(end)]
 assert generic_projection(m.SERVICE_READ_JS).replace(new,original)==generic_projection(old.SERVICE_READ_JS)

def test_original_public_source_slice_and_type_witness_are_hash_bound():
 proof=json.loads((FIXTURES/'source-proof.json').read_bytes());part=(FIXTURES/proof['source_file']).read_bytes()
 assert len(part)==proof['length'] and hashlib.sha256(part).hexdigest()==proof['sha256']
 assert hashlib.sha256(old.SERVICE_READ_JS.encode()).hexdigest()==proof['prior_service_read_sha256']
 assert proof['actual_source_product_type']==10019 and proof['recorded_type_witness']
 assert all(row['source_product_type_query']==['10019'] for row in proof['recorded_type_witness'])
 assert b'hidCoreColorId:"materialColor"' in part
