from pathlib import Path
from copy import deepcopy
import json,subprocess
import pytest
import test_printograph_modern_post_prices as base
from crawler import printograph_modern_browser as m
base.m=m
F=Path(__file__).resolve().parent/'fixtures/printograph_production_cache'
PAIR='https://www.printograph.com/products/cardboards/order'

def raw(mode='1'):
 v=base.raw();v['source_url']=PAIR;v['product_type']='10016';v['generic_price_context']['properties']['printProductType']='10016';v['generic_price_context']['turnaround']['printProduct']['productType']='10016';mode_change(v,mode);return v

def mode_change(v,mode):
 v['selection_context']['production']=mode;v['base_context']['production']=mode;v['generic_price_context']['binding']['production']=mode;v['generic_price_context']['properties']['turnaround']=mode

def price(g,v):base.observe(g,v,data={'items':[{'quantity':2,'productPrice':13.43,'subtotal':None,'available':True}]})
def production(g,v):base.observe(g,v,kind='production',data={'items':[{'id':1,'range':{'min':3,'max':4}},{'id':3,'range':{'min':2,'max':None}}]})
def verified(g,v,label=None):return g.verify(v,{'value':v['selection_context']['production'],'selected_label':label or ('Regular 3–4 Business Days' if v['selection_context']['production']=='1' else 'Rush 2 Business Days')},displayed_subtotal='13.43')

def test_current_native_turnaround_response_covers_both_modes_same_owned_page_only():
 v=raw();g=m.FreshQuoteGate();price(g,v);production(g,v);a=verified(g,v);old=deepcopy(v)
 mode_change(v,'3')
 with pytest.raises(m.ModernHeld,match='current_price_evidence_missing'):verified(g,v)
 price(g,v);out=verified(g,v)
 assert a['production_evidence_id']==out['production_evidence_id'] and out['production']=={'min':2,'max':None}
 assert old==raw() and not out['source_data_finalized']
 with pytest.raises(m.ModernHeld,match='current_price_evidence_missing'):verified(m.FreshQuoteGate(),v)

@pytest.mark.parametrize('change',['quantity','size','shape','width','proof','material','color','other_request','domain','product_id'])
def test_other_native_production_dependencies_never_share_cache(change):
 v=raw();g=m.FreshQuoteGate();price(g,v);production(g,v);before=deepcopy(v);mode_change(v,'3')
 if change=='quantity':v['selection_context']['quantity']='10'
 if change in {'size','shape','material','color'}:
  v['selection_context']['specs'][change]='99';v['base_context']['specs'][change]='99';v['generic_price_context']['binding']['specs'][change]='99'
  if change=='size':v['generic_price_context']['properties']['size']='99'
 if change=='width':v['generic_price_context']['dimensions']['width']='48';v['generic_price_context']['properties']['other']['widthId']='48';v['generic_price_context']['turnaround']['other']['hidWidthId']='48'
 if change=='proof':v['model_services']['manualProofType']='1';v['generic_price_context']['turnaround']['options']=['1'];next(g for g in v['groups'] if g['key']=='proofTypeForManual')['choices'][0]['value']='1'
 if change=='other_request':v['generic_price_context']['turnaround']['other']['Material']='Different current native label'
 if change=='domain':v['generic_price_context']['quantity_choices']=['1','2','150']
 if change=='product_id':v['base_context']['product_id']='900013';v['generic_price_context']['binding']['product_id']='900013'
 # Supply a fresh exact new price for the changed context; it cannot substitute
 # for old-context production. Quantity change includes its matching table row.
 data={'items':[{'quantity':int(v['selection_context']['quantity']),'productPrice':13.43,'subtotal':None,'available':True}]};base.observe(g,v,data=data)
 with pytest.raises(m.ModernHeld):verified(g,v)
 assert before==raw()

def test_missing_requested_mode_or_wrong_current_rendered_ID_still_holds():
 v=raw();g=m.FreshQuoteGate();price(g,v);base.observe(g,v,kind='production',data={'items':[{'id':1,'range':{'min':3,'max':4}}]});mode_change(v,'3');price(g,v)
 with pytest.raises(m.ModernHeld,match='rendered_production_stale'):verified(g,v)
 production(g,v)
 with pytest.raises(m.ModernHeld,match='rendered_production_stale'):g.verify(v,{'value':'1','selected_label':'Regular 3–4 Business Days'},displayed_subtotal='13.43')

@pytest.mark.parametrize('change',['valid','missing_projection','unknown_store','wrong_source','wrong_type','one_day_API','range_max','wrong_mode','numeric_stale','wrong_native_rush','numeric_observed','string_observed','false_observed'])
def test_source_native_following_business_day_alias_retains_numeric_two_days(change):
 v=raw('3');v['native_nextday_presentation']={'observed':True,'store':'101','rush':'2 Business Days'};g=m.FreshQuoteGate();price(g,v);production(g,v);label='Rush Following Business Day'
 if change=='numeric_observed':v['native_nextday_presentation']['observed']=1
 if change=='string_observed':v['native_nextday_presentation']['observed']='true'
 if change=='false_observed':v['native_nextday_presentation']['observed']=False
 if change=='missing_projection':v.pop('native_nextday_presentation')
 if change=='unknown_store':v['native_nextday_presentation']['store']='102'
 if change=='wrong_source':v['source_url']='https://www.printograph.com/products/acrylic-boards/order'
 if change=='wrong_type':v['product_type']='10019'
 if change=='one_day_API':base.observe(g,v,kind='production',data={'items':[{'id':3,'range':{'min':1,'max':None}}]})
 if change=='range_max':base.observe(g,v,kind='production',data={'items':[{'id':3,'range':{'min':2,'max':2}}]})
 if change=='wrong_mode':mode_change(v,'1');price(g,v)
 if change=='numeric_stale':label='Rush 1 Business Day'
 if change=='wrong_native_rush':v['native_nextday_presentation']['rush']='1 Business Day'
 if change=='valid':
  out=verified(g,v,label);assert out['production']=={'min':2,'max':None} and out['production_label_basis']=='source_native_nextday_alias_numeric_range_retained'
 else:
  with pytest.raises(m.ModernHeld):verified(g,v,label)

def test_exact_source_nextday_method_type_store_and_range_cases():
 fn=(F/'native-nextday.js').read_text().split(':',1)[1]
 script="""const f=eval('('+JSON.parse(process.argv[1])+')');let o=[];for(const [type,store,rush] of [[10016,101,'2 Business Days'],[10019,101,'2 Business Days'],[10016,102,'2 Business Days'],[10016,101,'1 Business Day']])o.push(f.call({printProductType:type,store,jsonObj:{turnaroundRange:{rush}}}));console.log(JSON.stringify(o));"""
 out=subprocess.run(['node','-e',script,json.dumps(fn)],check=True,capture_output=True,text=True)
 assert json.loads(out.stdout)==[True,False,False,False]

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['valid','unknown_store','wrong_rush','acrylic'])
async def test_readonly_alias_projection_uses_native_public_root_store_and_range(change):
 from playwright.async_api import async_playwright
 async with async_playwright() as rt:
  if not Path(rt.chromium.executable_path).is_file():pytest.skip('Chromium absent')
  b=await rt.chromium.launch(headless=True);c=await b.new_context(service_workers='block');p=await c.new_page();requests=[]
  url=PAIR if change!='acrylic' else 'https://www.printograph.com/products/acrylic-boards/order'
  html=base.html().replace('47001','10016' if change!='acrylic' else '10019')
  async def route(r):
   requests.append(r.request.url)
   if r.request.url==url:await r.fulfill(status=200,content_type='text/html',body=html)
   else:await r.abort()
  await c.route('**/*',route)
  try:
   await p.goto(url);await p.evaluate("controller.$root={$props:{store:101}};controller.jsonObj.turnaroundRange={rush:'2 Business Days'}")
   if change=='unknown_store':await p.evaluate('controller.$root.$props.store=102')
   if change=='wrong_rush':await p.evaluate("controller.jsonObj.turnaroundRange.rush='1 Business Day'")
   raw=await p.evaluate(m.SERVICE_READ_JS,{'source_url':url});assert raw['observed']
   assert raw['native_nextday_presentation']==({'observed':True,'store':'101','rush':'2 Business Days'} if change=='valid' else {'observed':False})
   assert requests==[url]
  finally:await c.close();await b.close()


def test_original_native_get_turnaround_reuses_cached_both_mode_result():
 fn=(F/'native-get-turnaround.js').read_text().split(':',1)[1]
 script="""const method=eval('('+JSON.parse(process.argv[1])+')');let fetches=0,processed=0;
const document={getElementById:()=>({style:{}})},X={A:{turnaroundParam:e=>JSON.stringify({quantity:e.userSelectedOptions.quantity,specifications:{printProduct:{productType:10016,size:e.userSelectedOptions.size,turnarounds:[1,3]},other:{hidWidthId:'24',hidHeightId:'36'}},productOptions:[1],itemType:5})}},w={A:{processTurnaround:()=>{processed++}}},R={A:{getTurnaround:()=>{fetches++;return Promise.resolve({items:[{id:1,range:{min:3,max:4}},{id:3,range:{min:2,max:null}}]})}}};
const e={userSelectedOptions:{size:11,paper:1,color:1,quantity:2,productionTime:1},others:{loaders:[]}},cache={turnaroundsApiData:{}};
(async()=>{method(e,10016,cache,{},{});await Promise.resolve();e.userSelectedOptions.productionTime=3;method(e,10016,cache,{},{});await Promise.resolve();console.log(JSON.stringify({fetches,processed,cache_entries:Object.keys(cache.turnaroundsApiData).length}));})();"""
 out=subprocess.run(['node','-e',script,json.dumps(fn)],check=True,capture_output=True,text=True)
 assert json.loads(out.stdout)=={'fetches':1,'processed':2,'cache_entries':1}


def test_public_production_fixture_provenance():
    import hashlib
    manifest = json.loads((F/'manifest.json').read_text())
    assert len(manifest['files']) == 7
    metadata = json.loads((F/'saved-projections.json').read_text())
    assert metadata['private_capture_records_omitted'] is True
    assert metadata['source_price_evidence'] is False
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((F/name).read_bytes()).hexdigest() == expected
