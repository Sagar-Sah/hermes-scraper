"""Provider CRUD presentation and guarded interactions; synthetic APIs and no credentials."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


FRONTEND = Path(__file__).resolve().parents[1]


def test_provider_management_controls_and_interactions():
    if not shutil.which("node") or not (FRONTEND / "node_modules/typescript").is_dir():
        pytest.skip("Dashboard dependencies are required")
    program = r'''
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const req=require('node:module').createRequire(path.join(process.argv[1],'package.json'));
const ts=req('typescript'),React=req('react'),render=req('react-dom/server').renderToStaticMarkup;
let checks=0;function check(value){assert.ok(value);checks++;}
let runner=null;
const mockReact={...React,
 useState(init){const self=runner,i=self.index++;if(!(i in self.slots))self.slots[i]=typeof init==='function'?init():init;return [self.slots[i],v=>{self.slots[i]=typeof v==='function'?v(self.slots[i]):v}];},
 useRef(init){const self=runner,i=self.index++;return self.slots[i]??(self.slots[i]={current:init});},
 useCallback(fn){runner.index++;return fn;},
 useEffect(fn,deps){const self=runner,i=self.index++,old=self.slots[i];if(!old||deps.some((v,n)=>v!==old[n])){self.effects.push(fn);self.slots[i]=deps;}},
 useId(){runner.index++;return 'fixture-dialog-title';}
};
function harness(component,props={}){return {slots:[],effects:[],index:0,render(){this.index=0;runner=this;try{return component(props)}finally{runner=null}},async effectsNow(){const pending=this.effects.splice(0);for(const fn of pending)await fn();await tick();}};}
function nodes(node,result=[]){if(!node||typeof node!=='object')return result;if(Array.isArray(node)){node.forEach(n=>nodes(n,result));return result;}result.push(node);nodes(node.props?.children,result);return result;}
function text(node){if(typeof node==='string'||typeof node==='number')return String(node);if(Array.isArray(node))return node.map(text).join('');return text(node?.props?.children||'');}
function button(node,label){const result=nodes(node).find(n=>n.type==='button'&&text(n).trim()===label);check(!!result);return result;}
function field(node,type){const result=nodes(node).find(n=>n.type==='input'&&n.props.type===type);check(!!result);return result;}
const tick=()=>new Promise(setImmediate),calls=[];
let responder=async()=>{throw Error('Unexpected fixture request')};
const Dialog=props=>React.createElement('section',{'role':'dialog','aria-modal':'true'},React.createElement('h2',null,props.title),props.children);
const scope={exports:{},require(name){
 if(name==='react')return mockReact;
 if(name.endsWith('.css'))return {};
 if(name==='./Dialog')return {__esModule:true,default:Dialog};
 if(name==='./api')return {api:(...args)=>{calls.push(args);return responder(...args)},errorMessage:e=>e.message};
 return req(name);
}};
const compile=file=>ts.transpileModule(fs.readFileSync(path.join(process.argv[1],'src',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
vm.runInNewContext(compile('ProviderManagement.tsx'),scope);
const {providerStatus,providerEditorValues,providerSaveRequest,providerActionRequest,ProviderForm,ProviderCard}=scope.exports;
const fixture={id:'fixture /provider',name:'Fixture Muse',base_url:'https://fixture.invalid/v1',model:'fixture-model',kind:'openai_compatible',has_credentials:true};
check(providerStatus(fixture)==='active');
const values=providerEditorValues({...fixture,api_key:'must-never-prefill',encrypted_api_key:'never-show'});
check(values.key==='');check(!JSON.stringify(values).includes('must-never-prefill'));check(!values.custom.includes('never-show'));
let save=providerSaveRequest(values,fixture),body=JSON.parse(save.options.body);
check(save.path==='/providers/fixture%20%2Fprovider'&&save.options.method==='PUT');
check(!('id' in body)&&!('api_key' in body)&&!('status' in body));check(body.model==='fixture-model');
check(!('api_key' in JSON.parse(providerSaveRequest({...values,key:'  '},fixture).options.body)));
check(JSON.parse(providerSaveRequest({...values,key:'replacement-fixture'},fixture).options.body).api_key==='replacement-fixture');
check(providerSaveRequest(values).path==='/providers'&&providerSaveRequest(values).options.method==='POST');
const custom={...fixture,kind:'custom',path:'/generate',prompt_field:'message',model_field:'version',response_path:'result.text',auth_header:'X-Key',auth_prefix:'',body_template:{fixed:'value'}};
const customValues=providerEditorValues(custom),customBody=JSON.parse(providerSaveRequest(customValues,custom).options.body);
for(const key of ['path','prompt_field','model_field','response_path','auth_header','auth_prefix','body_template'])check(JSON.stringify(customBody[key])===JSON.stringify(custom[key]));
check(!('path' in JSON.parse(providerSaveRequest({...customValues,kind:'anthropic'},fixture).options.body)));
for(const customText of ['[]','null','{"api_key":"wrong-place"}','{bad json']){assert.throws(()=>providerSaveRequest({...values,kind:'custom',custom:customText},fixture));checks++;}
assert.throws(()=>providerSaveRequest(values,{...fixture,status:'deleted'}),/Restore/);checks++;
for(const [status,action,method,tail] of [['active','suspend','POST','/suspend'],['suspended','resume','POST','/resume'],['deleted','restore','POST','/restore'],['active','delete','DELETE',''],['suspended','delete','DELETE',''],['active','test','POST','/test']]){
 const request=providerActionRequest({...fixture,status},action);check(request.path==='/providers/fixture%20%2Fprovider'+tail&&request.options.method===method);
}
for(const [status,action] of [['suspended','test'],['deleted','test'],['deleted','delete'],['active','restore'],['unknown','resume']]){assert.throws(()=>providerActionRequest({...fixture,status},action),/unavailable/);checks++;}
for(const status of ['active','suspended','deleted']){
 let edited=0,actions=[];
 const card=ProviderCard({provider:{...fixture,status},busy:'',onEdit:()=>edited++,onAction:(_,a)=>actions.push(a)});
 check(button(card,'Test connection').props.disabled===(status!=='active'));
 const html=render(card);check(html.includes(status)&&html.includes('Fixture Muse'));
 if(status==='active'){button(card,'Edit').props.onClick();button(card,'Suspend').props.onClick();check(edited===1&&actions[0]==='suspend');}
 if(status==='suspended'){button(card,'Resume').props.onClick();check(actions[0]==='resume');check(!html.includes('>Suspend<'));}
 if(status==='deleted'){button(card,'Restore').props.onClick();check(actions[0]==='restore');check(!html.includes('>Edit<')&&!html.includes('>Delete<'));}
 check(nodes(ProviderCard({provider:{...fixture,status},busy:'refresh',onEdit(){},onAction(){}})).filter(n=>n.type==='button').every(n=>n.props.disabled));
}
check(calls.length===0); // Building UI and request contracts never calls a model or an API.
(async()=>{
 let saved=null,closed=0,finish;
 const editor=harness(ProviderForm,{provider:fixture,onClose:()=>closed++,onSaved:p=>saved=p});
 let tree=editor.render();check(field(tree,'password').props.value==='');check(render(tree).includes('Leave blank to keep the saved key'));
 check(text(tree).includes('Saving does not call or test the model'));check(calls.length===0);
 responder=(p,o)=>new Promise(resolve=>{finish=resolve});
 const form=nodes(tree).find(n=>n.type==='form');
 const a=form.props.onSubmit({preventDefault(){}}),b=form.props.onSubmit({preventDefault(){}});
 await tick();check(calls.length===1&&calls[0][1].method==='PUT'&&!calls[0][0].endsWith('/test'));
 check(!('api_key' in JSON.parse(calls[0][1].body)));
 tree=editor.render();check(nodes(tree).find(n=>n.type==='fieldset').props.disabled===true);
 tree.props.onClose();check(closed===0); // Escape/backdrop closes are guarded while saving.
 finish(fixture);await Promise.all([a,b]);check(saved===fixture);
 tree=editor.render();tree.props.onClose();check(closed===1);
 let errorSaved=0;
 const bad=harness(ProviderForm,{provider:fixture,onClose(){},onSaved(){errorSaved++}});
 tree=bad.render();field(tree,'password').props.onChange({target:{value:'secret-fixture-value'}});
 tree=bad.render();responder=async()=>{throw Error('Rejected secret-fixture-value')};
 await nodes(tree).find(n=>n.type==='form').props.onSubmit({preventDefault(){}});
 tree=bad.render();check(errorSaved===0);check(render(tree).includes('Rejected [redacted]'));
 check(!nodes(tree).find(n=>n.type==='fieldset').props.disabled);
 calls.length=0;
 let savedProviders=[fixture,{...fixture,id:'deleted',name:'Removed fixture',status:'deleted'}];
 responder=async(p,o)=>{check(p==='/providers?include_deleted=true'&&!o);return {items:savedProviders}};
 const panel=harness(scope.exports.default);tree=panel.render();check(calls.length===0);
 await panel.effectsNow();tree=panel.render();check(calls.length===1);
 let cards=nodes(tree).filter(n=>n.type===ProviderCard);check(cards.length===1&&cards[0].props.provider.id===fixture.id);
 const toggle=field(tree,'checkbox');toggle.props.onChange({target:{checked:true}});tree=panel.render();cards=nodes(tree).filter(n=>n.type===ProviderCard);check(cards.length===2);
 const card=cards[0],before=calls.length;
 card.props.onAction(fixture,'delete');tree=panel.render();check(calls.length===before); // Delete is only a proposal until confirmed.
 let dialog=nodes(tree).find(n=>n.type===Dialog);check(!!dialog&&text(dialog).includes('Audit history and references are retained'));
 check(text(dialog).includes('remain suspended until you choose Resume'));
 button(dialog,'Cancel').props.onClick();tree=panel.render();check(!nodes(tree).some(n=>n.type===Dialog)&&calls.length===before);
 card.props.onAction(fixture,'delete');tree=panel.render();dialog=nodes(tree).find(n=>n.type===Dialog);
 responder=(p,o)=>{
   if(o?.method==='DELETE')return new Promise(resolve=>{finish=resolve});
   check(p==='/providers?include_deleted=true');return Promise.resolve({items:savedProviders});
 };
 const deleteButton=button(dialog,'Delete provider');deleteButton.props.onClick();deleteButton.props.onClick();await tick();
 check(calls.length===before+1&&calls.at(-1)[1].method==='DELETE');
 tree=panel.render();dialog=nodes(tree).find(n=>n.type===Dialog);check(button(dialog,'Delete provider').props.disabled);
 dialog.props.onClose();check(nodes(panel.render()).some(n=>n.type===Dialog));
 savedProviders=[{...fixture,status:'deleted'},savedProviders[1]];finish(savedProviders[0]);await tick();await tick();
 tree=panel.render();check(!nodes(tree).some(n=>n.type===Dialog));check(text(tree).includes('Provider deleted'));
 cards=nodes(tree).filter(n=>n.type===ProviderCard);check(cards.every(n=>n.props.provider.status==='deleted'));
 responder=async(p,o)=>{if(o?.method==='POST'){check(p.endsWith('/restore'));savedProviders=[{...fixture,status:'suspended'},savedProviders[1]];return savedProviders[0]}return {items:savedProviders}};
 cards[0].props.onAction(cards[0].props.provider,'restore');await tick();await tick();tree=panel.render();
 check(text(tree).includes('Provider restored and suspended'));cards=nodes(tree).filter(n=>n.type===ProviderCard);check(cards[0].props.provider.status==='suspended');
 const requestCount=calls.length;responder=async()=>{throw Error('offline fixture')};
 cards[0].props.onAction(cards[0].props.provider,'resume');await tick();tree=panel.render();check(render(tree).includes('offline fixture'));
 check(calls.length===requestCount+1&&nodes(tree).filter(n=>n.type===ProviderCard)[0].props.provider.status==='suspended');
 const app=fs.readFileSync(path.join(process.argv[1],'src/App.tsx'),'utf8');
 check(app.includes('<ProviderManagement />'));check(app.includes("disabled={providerStatus(provider) !== 'active'}"));
 check(!app.includes('function ProviderForm('));
 process.stdout.write(JSON.stringify({status:'PASS',assertions:checks,real_requests:0,real_profile_changes:0}));
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(["node", "-e", program, str(FRONTEND)], capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    assert value["status"] == "PASS" and value["assertions"] >= 90
    assert value["real_requests"] == value["real_profile_changes"] == 0


def test_provider_modal_keyboard_focus_and_restore():
    if not shutil.which("node") or not (FRONTEND / "node_modules/typescript").is_dir():
        pytest.skip("Dashboard dependencies are required")
    program = r'''
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const req=require('node:module').createRequire(path.join(process.argv[1],'package.json'));
const ts=req('typescript'),React=req('react');
let effect,ref,listener,closed=0,focuses=0,cleanup;
const previous={focus(){focuses++}},document={activeElement:previous,body:{style:{overflow:'auto'}},addEventListener(_,fn){listener=fn},removeEventListener(_,fn){assert.equal(listener,fn);listener=null}};
const scope={exports:{},document,require(name){if(name==='react')return {...React,useRef:()=>ref={current:null},useId:()=>'provider-modal-title',useEffect:fn=>effect=fn};return req(name)}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(process.argv[1],'src/Dialog.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,scope);
const tree=scope.exports.default({title:'Edit AI provider',onClose(){closed++},children:'fixture'});
const modal=tree.props.children;assert.equal(modal.props.role,'dialog');assert.equal(modal.props['aria-modal'],'true');assert.equal(modal.props['aria-labelledby'],'provider-modal-title');
const first={focus(){document.activeElement=first}},last={focus(){document.activeElement=last}};
ref.current={focus(){document.activeElement=ref.current},querySelectorAll(){return [first,last]}};
cleanup=effect();assert.equal(document.body.style.overflow,'hidden');assert.equal(document.activeElement,ref.current);
let prevented=0;listener({key:'Tab',shiftKey:true,preventDefault(){prevented++}});assert.equal(document.activeElement,last);assert.equal(prevented,1);
listener({key:'Tab',shiftKey:false,preventDefault(){prevented++}});assert.equal(document.activeElement,first);assert.equal(prevented,2);
listener({key:'Escape'});assert.equal(closed,1);cleanup();assert.equal(document.body.style.overflow,'auto');assert.equal(focuses,1);assert.equal(listener,null);
process.stdout.write(JSON.stringify({status:'PASS'}));
'''
    result = subprocess.run(["node", "-e", program, str(FRONTEND)], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["status"] == "PASS"
