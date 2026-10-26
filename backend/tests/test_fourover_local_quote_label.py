"""Fixed nonpersonal native quote label; no supplier, profile or private body I/O."""
import ast,asyncio,hashlib,importlib.util,json,shutil,subprocess,sys,types,uuid
from pathlib import Path
from copy import deepcopy
import pytest
from test_fourover_automatic_profile import load_driver,session_fixture,ui_ready_state,PRIVATE
C=Path(__file__).resolve().parents[2]
F=Path(__file__).resolve().parent/'fixtures/fourover_local_quote_label'

NODE=r'''
const vm=require('vm'),input=JSON.parse(require('fs').readFileSync(0,'utf8')),mode=input.mode;
let inputs=0,changes=0,writes=0,privateReads=0,requests=0;
function obs(v){const f=()=>{throw Error('No source observable callbacks from action')};f.peek=()=>v;return f}
class Field{constructor(){this.tagName='INPUT';this.type='text';this.name='project_name';this.maxLength=60;this.disabled=false;this.readOnly=false;this._value='';this.hidden=false;this.parentElement=null}get value(){return this._value}set value(v){writes++;this._value=v}getClientRects(){return this.hidden?[]:[{}]}hasAttribute(){return false}dispatchEvent(e){if(e.type==='input'){inputs++;if(mode==='throw-event')throw Error('source failed');name.peek=()=>this._value}if(e.type==='change')changes++;return true}}
const field=new Field(),form={contains:e=>e===field},step={getAttribute:()=>selected,contains:e=>e===field};
let selected='product.configuration';const name=obs(''),data={state:'default',name},tabs={guestShippingRestricted:false,selectedTab:obs(selected),tabsData:[{alias:selected},{alias:'product.shipping'}],shippingAndSetsTab:'product.shipping'},view={projectName:name,isShippingAndSetsActive:obs(false)},nameView={projectName:name},model=obs(data);
if(mode==='named'){field._value=input.private;name.peek=()=>input.private}if(mode==='whitespace'){field._value=' ';name.peek=()=> ' '}
if(['bookmark','cart','unknown-state'].includes(mode))data.state=mode;if(mode==='binding')nameView.projectName=obs('');if(mode==='model-drift')name.peek=()=>input.private;
if(mode==='guest')tabs.guestShippingRestricted=true;if(mode==='shipping')view.isShippingAndSetsActive=obs(true);if(mode==='no-next')tabs.tabsData.pop();if(mode==='duplicate-tabs')tabs.tabsData.push({alias:selected});
if(mode==='readonly')field.readOnly=true;if(mode==='disabled')field.disabled=true;if(mode==='hidden')field.hidden=true;if(mode==='wrongtype')field.type='password';if(mode==='wrongname')field.name='other';if(mode==='too-short')field.maxLength=5;if(mode==='too-long')field.maxLength=61;if(mode==='noninteger')field.maxLength=true;
if(mode==='private-getter')Object.defineProperty(data,'uuid',{get(){privateReads++;throw Error('Private uuid read')}});
const registry={get:key=>({'project-tabs':tabs,project:view,'project-name':nameView})[key]};
function requireModule(key){if(key==='uiRegistry')return registry;if(key==='FourOver_Project/js/model/project')return model;throw Error('Unexpected module read')};requireModule.defined=()=>mode!=='undefined';
const context={window:{require:requireModule},location:{href:input.source,origin:'https://4over.com',search:mode==='query'?'?edit=1':'',hash:mode==='hash'?'#bookmark=1':''},HTMLInputElement:Field,Event:class{constructor(type){this.type=type}},getComputedStyle:()=>({visibility:'visible',display:'block'}),document:{querySelectorAll:q=>q==='#product_addtocart_form'?(mode==='two-form'?[form,form]:[form]):q==='#project_name'?(mode==='two-input'?[field,field]:[field]):q==='[data-tab-id]'?[mode==='foreign'?{...step,contains:()=>false}:step]:[]}};
if(mode==='source')context.location.href+='-changed';if(mode==='no-module')delete context.window.require;
vm.createContext(context);const result=vm.runInContext('('+input.action+')',context)({source:input.source,modules_proved:mode!=='unproved'});
process.stdout.write(JSON.stringify({result,inputs,changes,writes,privateReads,requests,valueIsFixed:field._value===input.label,existingRetained:mode==='named'?field._value===input.private:true}));
'''

@pytest.fixture
def mod():return load_driver()

def action(mod,mode):
 p=subprocess.run([shutil.which('node'),'-e',NODE],input=json.dumps({'mode':mode,'action':mod.QUOTE_LABEL_ACTION_JS,'source':'https://4over.com/synthetic-package','label':mod.QUOTE_LABEL,'private':PRIVATE}),capture_output=True,text=True,check=True)
 assert PRIVATE not in p.stdout
 return json.loads(p.stdout)

@pytest.mark.parametrize('mode,status',[
 ('normal','filled'),('private-getter','filled'),('named','already_named'),('whitespace','already_named'),('bookmark','not_default'),('cart','not_default'),('unknown-state','not_default'),
 ('binding','binding_changed'),('model-drift','binding_changed'),('guest','not_options'),('shipping','not_options'),('no-next','not_options'),('duplicate-tabs','not_options'),
 ('readonly','field_changed'),('disabled','field_changed'),('hidden','field_changed'),('wrongtype','field_changed'),('wrongname','field_changed'),('too-short','field_changed'),('too-long','field_changed'),('noninteger','field_changed'),('two-form','field_changed'),('two-input','field_changed'),('foreign','field_changed'),
 ('query','unproved'),('hash','unproved'),('source','unproved'),('no-module','unproved'),('undefined','unproved'),('unproved','unproved'),('throw-event','unproved')])
def test_atomic_visible_default_local_name_action(mod,mode,status):
 out=action(mod,mode);assert out['result']=={'status':status} and out['requests']==out['privateReads']==0
 if status=='filled':assert out['inputs']==out['changes']==out['writes']==1 and out['valueIsFixed']
 elif mode=='throw-event':assert out['writes']==out['inputs']==1 and out['changes']==0
 else:assert out['inputs']==out['changes']==out['writes']==0
 assert out['existingRetained']

def test_saved_public_source_proves_local_binding_and_separate_explicit_persistence(mod):
 proof=json.loads((F/'source-proof.json').read_text());sources={r['public_asset_suffix']:(F/r['path']).read_bytes() for r in proof['modules']}
 for r in proof['modules']:assert hashlib.sha256(sources[r['public_asset_suffix']]).hexdigest()==r['sha256']
 for name,sha in mod.NAME_PUBLIC_MODULES.items():assert hashlib.sha256(sources[name]).hexdigest()==sha
 name=sources['FourOver_Project/js/view/project/name.js'].decode();template=sources['FourOver_Project/template/project/name.html'].decode();view=sources['FourOver_Project/js/view/project.js'].decode()
 assert 'projectName: project().name' in name and 'project().name.subscribe' in name and 'textInput: projectName' in template and 'maxlength="60"' in template
 save=view.split('saveProject: function () {',1)[1].split('},',1)[0];assert 'this.projectData = projectManager.getProjectData(true)' in save and not any(x in save for x in ('post(', 'ajax(', 'fetch('))
 assert "'/project/bookmark/save'" in sources['FourOver_ProjectBookmark/js/action/save-project-bookmark.js'].decode()
 assert "'/project/to-cart'" in sources['FourOver_Project/js/action/project/to-cart.js'].decode()

@pytest.mark.asyncio
async def test_label_preparation_finishes_before_any_semantic_quantity_reservation(mod,tmp_path):
 s,store,page=session_fixture(mod,tmp_path);original=page.evaluate;ui=ui_ready_state();ui['current_project_name_present']=True;ui['fields']['project_name'].update(count=1,visible_count=1)
 s.name_modules=set(mod.NAME_PUBLIC_MODULES);calls=[]
 async def evaluate(code,arg=None):
  if code==mod.QUOTE_LABEL_ACTION_JS:
   assert s.stage=='child' and s.cursor['inflight'] is None and page.quantity_clicks==0
   assert not (tmp_path/'fourover-native-observation-selections').exists();calls.append('local_label');return {'status':'filled'}
  result=await original(code,arg)
  if code.startswith('() =>') and type(result) is dict:result['source_ui']=deepcopy(ui)
  return result
 page.evaluate=evaluate
 assert await s.collect() is True and s.cursor['verified_count']==1 and calls==['local_label'] and page.quantity_clicks==1

@pytest.mark.asyncio
async def test_unproved_label_never_spends_quantity_and_preserves_typed_requirement(mod,tmp_path):
 s,store,page=session_fixture(mod,tmp_path);original=page.evaluate;ui=ui_ready_state();ui.update(current_project_name_present=True,project_name_invalid=True);ui['fields']['project_name'].update(count=1,visible_count=1)
 async def evaluate(code,arg=None):
  result=await original(code,arg)
  if code.startswith('() =>') and type(result) is dict:result['source_ui']=deepcopy(ui)
  return result
 page.evaluate=evaluate
 with pytest.raises(mod.Held,match='source_project_details_required'):await s.collect()
 assert page.quantity_clicks==0 and s.cursor['inflight'] is None and not (tmp_path/'fourover-native-observation-selections').exists()


def queue_modules():
 name='_label_queue_'+uuid.uuid4().hex;pkg=types.ModuleType(name);pkg.__path__=[str(C/'backend/crawler')];sys.modules[name]=pkg
 result=[]
 for key in ('campaign_automation','campaign_automation_bindings'):
  spec=importlib.util.spec_from_file_location(name+'.'+key,C/'backend/crawler'/(key+'.py'));m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m);result.append(m)
 return result

@pytest.mark.asyncio
@pytest.mark.parametrize('reason,expected',[('source_project_details_required','source_project_details_required'),('source_account_profile_action_required','source_account_profile_action_required'),('source_auth_required','supplier_authentication_required'),(PRIVATE,'supplier_authentication_required'),({},'supplier_authentication_required'),(None,'supplier_authentication_required')])
async def test_family_and_scheduler_keep_whitelisted_access_reason_without_leaking_values(tmp_path,reason,expected):
 m,b=queue_modules();calls=[]
 rows=[{'campaign_id':'test','product_id':pid,'supplier':supplier,'source_url':'https://supplier.example/'+pid} for pid,supplier in [('one','a'),('two','a'),('three','b')]]
 async def execute(association,configuration,check_scope):
  calls.append(association['product_id']);return {'status':'access_required','reason_code':reason} if association['supplier']=='a' else {'status':'quoted','result_reference':'objects/synthetic.json'}
 profiles=[b.family_profile(profile_id=x,supplier=x,supports=lambda a:True,discover=lambda a,n:{'configurations':[{}],'blockers':[]},normalize=lambda c:c,execute=execute) for x in ('a','b')]
 r=m.CampaignAutomation(tmp_path,profiles,lambda cid:rows)
 try:
  p=await r.plan('test');await r.start('test',p['plan_id'])
  for _ in range(30):await r.tick();await asyncio.sleep(0)
  snap=r.snapshot('test');held=next(j for j in snap['jobs'] if j['status']=='access_required');assert held['reason']==expected and snap['supplier_blocks']=={'a':expected}
  assert len(calls)==2 and 'three' in calls and next(j for j in snap['jobs'] if j['product_id']=='three')['status']=='completed'
  assert sum(j['supplier']=='a' and j['status']=='queued' for j in snap['jobs'])==1
  assert PRIVATE not in json.dumps(snap)
  for saved in r._records['test']['jobs'].values():saved.update(progress_count=17,continuations=16)
  snap=r.snapshot('test');assert all(j['progress_count']==17 and j['continuations']==16 and 'progress_token' not in j for j in snap['jobs'])
  for saved in r._records['test']['jobs'].values():saved.update(progress_count=True,continuations=-1)
  assert all(j['progress_count']==j['continuations']==0 for j in r.snapshot('test')['jobs'])
 finally:await r.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('existing,state',[('', 'default'),(PRIVATE,'default'),('', 'bookmark')])
async def test_actual_chromium_source_name_validation_is_local_only(mod,existing,state):
 from playwright.async_api import async_playwright
 name_source=(F/'source/FourOver_Project__js__view__project__name.js').read_text();url='https://4over.com/synthetic-label';network=[]
 doc='''<!doctype html><form id="product_addtocart_form"><div data-tab-id="product.configuration"><input type="text" id="project_name" name="project_name" maxlength="60"></div></form><script>
 const obs=initial=>{let v=initial;const listeners=[];function fn(next){if(!arguments.length)return v;v=next;listeners.forEach(f=>f(v));return fn}fn.peek=()=>v;fn.subscribe=f=>listeners.push(f);fn.extend=()=>fn;return fn};
 const data={state:STATE,name:obs(EXISTING)},model=obs(data);window.define=(deps,factory)=>window.nameFactory=factory;
 </script><script>NAMESOURCE</script><script>
 const spec=window.nameFactory({extend:s=>s},model),vm={...spec.defaults,...spec,_super(){return this},observe(keys){keys.forEach(k=>this[k]=obs(this[k]));return this}};vm.initObservable();
 const tabs={guestShippingRestricted:false,selectedTab:obs('product.configuration'),tabsData:[{alias:'product.configuration'},{alias:'product.shipping'}],shippingAndSetsTab:'product.shipping'},view={projectName:data.name,isShippingAndSetsActive:obs(false)};
 const registry={get:key=>({'project-tabs':tabs,project:view,'project-name':vm})[key]};window.require=key=>key==='uiRegistry'?registry:model;window.require.defined=()=>true;
 const field=document.querySelector('input');field.value=EXISTING;window.fixture={inputs:0,changed:0,invalid:false};field.addEventListener('input',()=>{window.fixture.inputs++;data.name(field.value);window.fixture.invalid=vm.isProjectNameInvalid.peek()});field.addEventListener('change',()=>window.fixture.changed++);
 for(const key of ['uuid','address','customer'])Object.defineProperty(data,key,{get(){throw Error('Private source field read')}});
 </script>'''.replace('STATE',json.dumps(state)).replace('EXISTING',json.dumps(existing)).replace('NAMESOURCE',name_source)
 async with async_playwright() as pw:
  browser=await pw.chromium.launch(headless=True);context=await browser.new_context(service_workers='block');page=await context.new_page()
  async def serve(route):
   network.append(route.request.url)
   if route.request.url==url:await route.fulfill(status=200,content_type='text/html',body=doc)
   else:await route.abort();raise AssertionError('Unexpected external request')
  await context.route('**/*',serve)
  try:
   await page.goto(url);out=await page.evaluate(mod.QUOTE_LABEL_ACTION_JS,{'source':url,'modules_proved':True});f=await page.evaluate('window.fixture')
   assert out=={'status':'not_default' if state!='default' else 'already_named' if existing else 'filled'}
   assert f['inputs']==f['changed']==(1 if state=='default' and not existing else 0)
   assert network==[url] and PRIVATE not in json.dumps(out) and f['invalid'] is False
  finally:await context.close();await browser.close()
