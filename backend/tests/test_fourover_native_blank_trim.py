"""Exact public native blank trim field; no supplier, app or actual DB actions."""
import asyncio,hashlib,json,os,subprocess,shutil,time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
import pytest
from test_fourover_automatic_profile import load_driver,session_fixture,SELECTION,PRIVATE
from test_fourover_native_proceed_transition import browser_fixture,bind,FORM
F=Path(__file__).resolve().parent/'fixtures/fourover_native_blank_trim'

SOURCE_MAP={
 'FourOver_TrimSize/js/view/register-options-manager.js':'register-options-manager.js',
 'FourOver_TrimSize/js/model/product/trim-size-options-manager.js':'trim-size-options-manager.js',
 'FourOver_TrimSize/js/model/custom-trim-size.js':'custom-trim-size.js'}

async def install_source_trim(page,*,custom=False,value=''):
 # Run the exact native processors/trim/register factories locally. The native
 # source parser, rather than this test, adds trim_size to the public model map.
 sources={n:(F/n).read_text() for n in ['product-options-processors.js','trim-size-options-manager.js','register-options-manager.js']}
 await page.evaluate('''({sources,custom,value})=>{
  const underscore={each:(rows,fn)=>rows.forEach(fn)};
  let processors,parser;window.define=(deps,factory)=>processors=factory(underscore);eval(sources['product-options-processors.js']);
  const flag=()=>custom;flag.peek=()=>custom;fixture.modules['FourOver_TrimSize/js/model/custom-trim-size']=flag;
  window.define=(deps,factory)=>parser=factory(()=>value,flag,()=>{throw Error('No apply action')});eval(sources['trim-size-options-manager.js']);
  window.define=(deps,factory)=>factory({},processors,parser);eval(sources['register-options-manager.js']);
  fixture.data.productOptions(processors.parse(fixture.data.productOptions.peek()));
  const original=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(body){
   if(typeof body==='string'&&Object.prototype.hasOwnProperty.call(fixture.data.productOptions.peek(),'trim_size')){
    body+='&product_options%5Btrim_size%5D='+encodeURIComponent(fixture.data.productOptions.peek().trim_size);
   }return original.call(this,body);
  };
 }''',{'sources':sources,'custom':custom,'value':value})

def test_exact_public_sources_prove_noncustom_blank_and_native_post_payload_unchanged():
 manifest=json.loads((F/'manifest.json').read_text())
 for n,h in manifest['files'].items():assert hashlib.sha256((F/n).read_bytes()).hexdigest()==h
 node=r'''
 const vm=require('vm'),input=JSON.parse(require('fs').readFileSync(0,'utf8'));let processors,trim,post;
 const context={define:(deps,factory)=>processors=factory({each:(rows,fn)=>rows.forEach(fn)})};vm.createContext(context);
 vm.runInContext(input['product-options-processors.js'],context);
 const flag=()=>false,size=()=>{throw Error('No fabricated trim dimension')};
 context.define=(deps,factory)=>trim=factory(size,flag,()=>{throw Error('No source apply')});vm.runInContext(input['trim-size-options-manager.js'],context);
 context.define=(deps,factory)=>factory({},processors,trim);vm.runInContext(input['register-options-manager.js'],context);
 const options=processors.parse({super_attribute:{111:'10',999:'20'}});if(options.trim_size!=='')throw Error('Source blank not retained');
 let call;context.define=(deps,factory)=>post=factory({ajax:request=>{call=request;return {fail(){return this}}}},{});vm.runInContext(input['post-request.js'],context);
 post('/print/project/summary/',{product_id:900,run_size:250,custom_run_size:0,product_options:options});
 if(call.type!=='post'||call.data.product_options.trim_size!==''||call.url!=='/print/project/summary/')throw Error('Source payload changed');
 context.define=(deps,factory)=>{const observable=v=>{if(v!==0)throw Error('Custom run default not0');return v};factory({observable})};vm.runInContext(input['selected-custom-run-size.js'],context);
 process.stdout.write(JSON.stringify({sourceBlank:true,nativePayload:true,customRunZero:true}));
 '''
 names=['product-options-processors.js','trim-size-options-manager.js','register-options-manager.js','post-request.js','selected-custom-run-size.js']
 result=subprocess.run([shutil.which('node'),'-e',node],input=json.dumps({n:(F/n).read_text() for n in names}),text=True,capture_output=True,check=True)
 assert json.loads(result.stdout)=={'sourceBlank':True,'nativePayload':True,'customRunZero':True}

@pytest.mark.asyncio
async def test_source_generated_blank_passes_exact_handoff_and_wire_schema_only_with_receipt(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection=bind(m,s,page);s.trim_modules=set(m.TRIM_PUBLIC_MODULES);await install_source_trim(page)
  # Exact saved parser has populated the literal key, not a guessed default.
  assert await page.evaluate("fixture.data.productOptions.peek().trim_size === ''") is True
  assert (await s.ordinary_quote_action(selection))['status']=='clicked'
  await page.wait_for_function('fixture.finished===true');observed=await s.state()
  assert s.proceed_receipt['project_context']['trim_size_binding']=='source_literal_blank'
  body=urlencode({**FORM,'product_options[trim_size]':''})
  with pytest.raises(m.Held):m.automatic_request_context(body,s.quote_context_state(observed,selection),selection=selection)
  projected=s.quote_request_context(body,observed,selection)
  assert projected['native_trim_size_binding']=='source_literal_blank' and projected['product_id']=='900'
  for wrong in [urlencode(FORM),urlencode({**FORM,'product_options[trim_size]':'6x9'}),body+'&product_options%5Btrim_size%5D=']:
   with pytest.raises(m.Held):s.quote_request_context(wrong,observed,selection)
  assert await page.evaluate('fixture.clicks')==1 and await page.evaluate('privateReads')==0
  assert PRIVATE not in json.dumps(s.proceed_receipt)
  await s.proceed_identity.dispose();s.proceed_identity=None

@pytest.mark.asyncio
@pytest.mark.parametrize('mode,reason',[
 ('unproved','trim_literal_unproved'),('custom','trim_literal_unproved'),('nonblank','trim_literal_unproved'),
 ('null','trim_literal_unproved'),('false','trim_literal_unproved'),('getter','trim_literal_unproved'),
 ('unknown','options_keys_unrecognized'),('custom-run-null','product_quantity_binding_changed')])
async def test_blank_trim_guards_hold_before_click_with_only_fixed_rejection_reason(tmp_path,mode,reason):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection=bind(m,s,page);s.trim_modules=set(m.TRIM_PUBLIC_MODULES);await install_source_trim(page)
  if mode=='unproved':s.trim_modules=set()
  if mode=='custom':await page.evaluate("fixture.modules['FourOver_TrimSize/js/model/custom-trim-size'].peek=()=>true")
  if mode=='nonblank':await page.evaluate("fixture.data.productOptions.peek().trim_size='6x9'")
  if mode=='null':await page.evaluate("fixture.data.productOptions.peek().trim_size=null")
  if mode=='false':await page.evaluate("fixture.data.productOptions.peek().trim_size=false")
  if mode=='getter':await page.evaluate("Object.defineProperty(fixture.data.productOptions.peek(),'trim_size',{get(){throw Error('Do not read custom field getter')}})")
  if mode=='unknown':await page.evaluate("fixture.data.productOptions.peek()['SYNTHETIC-PRIVATE-NAME']=null")
  if mode=='custom-run-null':await page.evaluate("fixture.data.customRunSize(null)")
  result=await s.ordinary_quote_action(selection)
  assert result['status']=='context_changed' and result['proceed_probe']=={'schema':'native-proceed-probe-diagnostic/v1','phase':'model_before','reason':reason}
  assert s.proceed_probe_diagnostic==result['proceed_probe'] and s.proceed_receipt is None
  assert await page.evaluate('fixture.clicks')==0 and await page.evaluate('fixture.requests')==0
  assert PRIVATE not in json.dumps(result) and 'SYNTHETIC-PRIVATE-NAME' not in json.dumps(result)

@pytest.mark.parametrize('value',[None,{},True,{'schema':'native-proceed-probe-diagnostic/v1','phase':'model_before','reason':PRIVATE},
 {'schema':'native-proceed-probe-diagnostic/v1','phase':PRIVATE,'reason':'options_keys_unrecognized'},
 {'schema':'native-proceed-probe-diagnostic/v1','phase':'model_before','reason':'options_keys_unrecognized','private':PRIVATE}])
def test_probe_diagnostics_reject_unknown_private_or_extra_values(value):
 assert load_driver().safe_proceed_diagnostic(value) is None

@pytest.mark.asyncio
async def test_three_loaded_trim_source_hashes_and_default_wire_parser_remain_strict(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 for suffix,name in SOURCE_MAP.items():
  raw=(F/name).read_bytes();assert hashlib.sha256(raw).hexdigest()==m.TRIM_PUBLIC_MODULES[suffix]
  url=s.prefix+suffix
  class Response:
   status=200;headers={'content-type':'application/javascript'}
   async def body(self):return raw
  response=Response();response.url=url;response.request=SimpleNamespace(frame=s.page.main_frame,method='GET',resource_type='script',url=url)
  await s.capture_public_script(response)
 assert s.trim_modules==set(m.TRIM_PUBLIC_MODULES)
 with pytest.raises(m.Held):m.automatic_request_context(urlencode({**FORM,'product_options[trim_size]':''}),{},selection=SELECTION)

@pytest.mark.asyncio
async def test_actual_guard_and_collector_persist_source_blank_trim_exact_quote_then_continue(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  bind(m,s,page);s.selection=None;s.stage='child';s.trim_modules=set(m.TRIM_PUBLIC_MODULES);s.wait=m.AutomaticFourOverSession.wait.__get__(s)
  await install_source_trim(page)
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
  saved=next(iter(s.cursor['receipts'].values()));assert saved['status']=='verified'
  raw=(tmp_path/saved['evidence']['object_path']).read_bytes();record=json.loads(raw)
  assert record['request_context']['native_trim_size_binding']=='source_literal_blank'
  assert record['production']=={'value':'10','label':'2-4 Business Days'} and record['native_quote_transition']['same_project_model_identity_retained'] is True
  assert record['source_data_finalized'] is record['woocommerce_import_ready'] is record['shipping_verified'] is False
  assert s.counts['summary']==1 and s.result(False)['status']=='continuation_required' and PRIVATE.encode() not in raw
  await s.proceed_identity.dispose();s.proceed_identity=None
