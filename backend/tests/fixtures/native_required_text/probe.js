// No browser or network. Exact reader helpers run against explicit DOM-shape
// metadata; layout visibility is synthetic, not a browser-render claim.
const fs=require('fs'),vm=require('vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
function element(attrs={},label=''){
 const e={nodeType:1,tagName:attrs.tagName||'SELECT',id:attrs.id||'',name:attrs.name||'',type:attrs.type||'select-one',
  disabled:!!attrs.disabled,readOnly:!!attrs.readOnly,required:!!attrs.required,labels:[],children:[],childNodes:[],
  parentElement:null,style:{visibility:'visible'},validity:{valueMissing:attrs.valueMissing!==false},
  getAttribute:k=>k==='data-id'?(attrs.dataId??null):k==='aria-required'?(attrs.ariaRequired??null):null,
  getClientRects:()=>attrs.hidden?[]:[{}],closest:selector=>attrs.excludedAncestor&&selector.includes('header')?{}:attrs.nonProductContainer&&selector.includes(attrs.nonProductContainer)?{}:selector.includes('.control-group')?e.parentElement:null,
  matches:selector=>selector===':disabled'?e.disabled:selector.split(',').some(s=>s.trim().toLowerCase()===e.tagName.toLowerCase())};
 Object.defineProperty(e,'value',{get(){if(e.tagName==='SELECT')return attrs.value||'F';throw Error('PRIVATE_TEXT_VALUE_READ');}});
 e.options=[{value:'F',label:'No',textContent:'No',disabled:false,closest:()=>null},{value:'T',label:'Yes',textContent:'Yes',disabled:false,closest:()=>null}];
 e.selectedOptions=e.options.filter(o=>o.value===(attrs.value||'F'));
 const text={nodeType:3,textContent:label};
 const labelNode={nodeType:1,tagName:'LABEL',textContent:label,innerText:label,getClientRects:()=>[{}],style:{visibility:'visible'},
  matches:s=>s.includes('label'),closest:()=>null};
 const parent={nodeType:1,tagName:'DIV',id:'',children:[labelNode,e],childNodes:[labelNode,e],parentElement:null,
  matches:()=>false,querySelectorAll:()=>[e],querySelector:()=>labelNode};
 e.parentElement=parent;
 return e;
}
function hooks(e,duplicate=false){
 const document={querySelectorAll:()=>duplicate?[e,e]:[e]};
 const context={document,WeakMap,Map,Set,CSS:{escape:x=>x},getComputedStyle:e=>e.style||{visibility:'visible'}};
 const prefix=input.snapshot.slice(0,input.snapshot.indexOf(' const excluded ='));
 const result=vm.runInNewContext('('+prefix+'return {path,label,rawKey,parentLabel};})({})',context);
 return {result,context};
}
function dependencies(e){
 const {context}=hooks(e);context.root={querySelectorAll:()=>[e]};
 const prefix=input.snapshot.slice(0,input.snapshot.indexOf(' const excluded ='));
 const excluded=input.snapshot.match(/ const excluded = [^\n]+/)[0];
 const nonProduct=input.snapshot.match(/ const nonProductText=[^\n]+/)[0];
 const start=input.snapshot.indexOf(' for(const e of root.querySelectorAll(\'input[type="text"]');
 const end=input.snapshot.indexOf(' for(const e of root.querySelectorAll(\'input[type=checkbox]',start);
 return vm.runInNewContext('('+prefix+excluded+nonProduct+'\nconst root=globalThis.root,requiredText=[];'+input.snapshot.slice(start,end)+'return requiredText;})({})',context);
}
const fallback=element({dataId:'custcol_pms_match'},'Use PMS Match');
const standard=element({id:'size_dynamic123',name:'size',dataId:'do_not_use'},'Visible new label');
const named=element({name:'paper',dataId:'do_not_use'},'Paper visible');
const required=element({tagName:'INPUT',type:'text',id:'custcol_pms_number',dataId:'custcol_pms_number',required:true},'PMS Number/s');
const filled=element({tagName:'INPUT',type:'text',id:'custcol_pms_number',required:true,valueMissing:false},'PMS Number/s');
const readonly=element({tagName:'INPUT',type:'text',id:'custom_required',required:true,readOnly:true,valueMissing:false},'Required product text');
const excluded=element({tagName:'INPUT',type:'text',id:'email',required:true},'Email');
const width=element({tagName:'INPUT',type:'text',id:'width',required:true},'Width');
const hidden=element({tagName:'INPUT',type:'text',id:'secret',required:true,hidden:true},'Secret');
const optional=element({tagName:'INPUT',type:'text',id:'notes'},'Notes');
const containers=['form#commentform','wp-comments-post','.woocommerce-Reviews','.contact-form','[data-widget="chat"]'];
const external=containers.map(nonProductContainer=>element({tagName:'TEXTAREA',type:'textarea',id:'comment',required:true,nonProductContainer},'Message'));
external.push(element({tagName:'INPUT',type:'text',id:'author',required:true,nonProductContainer:'form#commentform'},'Name'));
external.push(element({tagName:'INPUT',type:'text',id:'reviewer',required:true,nonProductContainer:'wp-comments-post'},'Name'));
const message=element({tagName:'TEXTAREA',type:'textarea',id:'custom_message',required:true,dataId:'custcol_message'},'Message');
const name=element({tagName:'INPUT',type:'text',id:'custom_name',required:true},'Name');
const output={fallback:{key:hooks(fallback).result.rawKey(fallback),label:hooks(fallback).result.label(fallback),selector:hooks(fallback).result.path(fallback)},
 standard:{key:hooks(standard).result.rawKey(standard),label:hooks(standard).result.label(standard),selector:hooks(standard).result.path(standard)},
 named:{key:hooks(named).result.rawKey(named),label:hooks(named).result.label(named)},
 duplicate_selector:hooks(fallback,true).result.path(fallback),required:dependencies(required),filled:dependencies(filled),readonly:dependencies(readonly),
 excluded:dependencies(excluded),width:dependencies(width),hidden:dependencies(hidden),optional:dependencies(optional),external:external.map(dependencies),message:dependencies(message),name:dependencies(name)};
const initial=input.diagnosis.slice(input.diagnosis.indexOf('  const clean ='),input.diagnosis.indexOf('  const explicitLogout'));
const labelHelpers=input.diagnosis.slice(input.diagnosis.indexOf('  const parentLabel ='),input.diagnosis.indexOf('  const controls ='));
const diagnosisLabel=e=>vm.runInNewContext('(()=>{'+initial+labelHelpers+'return localLabel(globalThis.e)})()',
 {e,document:{getElementById:()=>null},getComputedStyle:e=>e.style||{visibility:'visible'}});
output.diagnosis_labels={select:diagnosisLabel(fallback),required:diagnosisLabel(required)};
function diagnosis(fields){
 const root={id:'',tagName:'MAIN',getClientRects:()=>[{}],closest:()=>null,
  querySelector:()=>null,querySelectorAll:selector=>selector.startsWith('select,input')?fields:[]};
 const document={title:'Fixture',readyState:'complete',
  querySelectorAll:selector=>selector.startsWith('#order-form,')?[root]:[],
  querySelector:selector=>selector==='[id^="product-"],main,article'?root:null,getElementById:()=>null};
 return vm.runInNewContext('('+input.diagnosis+')()', {document,
  location:{hostname:'fixture.example',href:'https://fixture.example/product'},
  getComputedStyle:e=>e.style||{visibility:'visible'}});
}
output.diagnosis={no:diagnosis([fallback]),yes:diagnosis([element({dataId:'custcol_pms_match',value:'T'},'Use PMS Match'),required]),external:external.map(e=>diagnosis([e])),message:diagnosis([message]),name:diagnosis([name])};
process.stdout.write(JSON.stringify(output));
