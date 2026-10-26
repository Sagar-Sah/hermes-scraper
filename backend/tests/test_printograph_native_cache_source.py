"""Execute exact saved native cache builder; all collaborators are local stubs."""
from pathlib import Path
import json,subprocess,shutil
import pytest
import printograph_price_dependency_support as s
from test_printograph_price_dependencies import m
s.m=m
C=Path(__file__).resolve().parent/'fixtures/printograph_price_dependencies'
@pytest.mark.parametrize('kind',[10016,10019])
def test_actual_native_builder_reuses_only_same_wire_properties_and_observed_process(kind):
 source=C/'source';payload={name:(source/file).read_text().split(':',1)[1] for name,file in [('parameters','generic-parameters.js'),('price','generic-price-builder.js')]}
 # Source fields10019 is public; Cardboards uses the same declared scalar
 # mapping here solely as a synthetic function argument, not a current catalog.
 fields=json.loads(subprocess.check_output([shutil.which('node'),'-e',"process.stdout.write(JSON.stringify(eval('({'+require('fs').readFileSync(0,'utf8')+'})')[10019]))"],input=(source/'type10019-fields.js.txt').read_text(),text=True))
 v=s.raw();selected={**v['selection_context']['specs'],**v['model_services'],'quantity':'2','productionTime':'1','productId':'900012','paper':'','shoppingCartItemId':'','priceGroupId':'','centerSplit':'','standType':'','frame':''};selected['color']='2'
 schema={'size':{'list':[{'id':'11','width':24,'height':36,'label':'24 x 36'}]},'quantity':{'list':[{'quantity':1},{'quantity':2},{'quantity':10},{'quantity':150}]},'orientation':{'list':[{'id':'1','label':'Horizontal'},{'id':'2','label':'Vertical'}]}}
 payload.update(kind=kind,fields=fields,schema=schema,selected=selected)
 code=r'''(async()=>{const p=JSON.parse(require('fs').readFileSync(0,'utf8')),params=eval('('+p.parameters+')'),native=eval('('+p.price+')');global.window={};global.document={getElementById:()=>({style:{}})};
 const e={userSelectedOptions:p.selected,others:{subItemProducts:[],embroideryProducts:[],genericOrientation:[],redirectFromMediaClip:false,defaultSizeOptionCategories:{Tall:[]},itemId:'null'}};
 const requests=[],applied=[];const fields={[p.kind]:p.fields};
 const X={A:{parametersOnAddToCart:(e,t,i,r)=>JSON.stringify(params(e,t,i,r,{itemDetail:{type:e.userSelectedOptions.fileDelivery,productOptions:[],specifications:{printProduct:{},other:{}}}}))}};
 const w={A:{getUrlParameter:()=>''}},R={A:{getGenericProductsPrice:(url,body)=>{requests.push({url,body:JSON.parse(JSON.stringify(body))});return Promise.resolve({items:[{quantity:2,productPrice:13.43,available:true},{quantity:10,productPrice:25,available:true}]});}}};
 const owner={storedGenericPrice:{},validParams:()=>true,setGenericProductsPrice:(row,body)=>applied.push({row:JSON.parse(JSON.stringify(row)),body:JSON.parse(JSON.stringify(body))})};
 const scalar=v=>v&&typeof v==='object'?Array.isArray(v)?v.map(scalar):Object.fromEntries(Object.entries(v).map(([k,z])=>[k,scalar(z)])):String(v);const tick=()=>new Promise(resolve=>setImmediate(resolve));native.call(owner,e,p.kind,p.schema,fields);await tick();if(requests.length!==1)throw Error('First native table missing');
 e.userSelectedOptions.color='3';native.call(owner,e,p.kind,p.schema,fields);await tick();if(requests.length!==1||applied.length!==2||JSON.stringify(scalar(applied[0].body))!==JSON.stringify(scalar(applied[1].body)))throw Error('Native color alias cache not reused');
 e.userSelectedOptions.quantity='10';native.call(owner,e,p.kind,p.schema,fields);await tick();if(requests.length!==1||applied[2].row.productPrice!==25)throw Error('Native quantity range not reused');
 e.userSelectedOptions.proofType='101';native.call(owner,e,p.kind,p.schema,fields);await tick();if(requests.length!==2)throw Error('Observed process type not separated');
 process.stdout.write(JSON.stringify({requests:requests.length,cached_alias_without_HTTP:true,cached_quantity_without_HTTP:true,process_separate:true,original_body:applied[0].body}));})().catch(e=>{process.stderr.write(String(e));process.exitCode=1});'''
 run=subprocess.run([shutil.which('node'),'-e',code],input=json.dumps(payload),text=True,capture_output=True,check=False);assert run.returncode==0,run.stderr;out=json.loads(run.stdout)
 assert out['requests']==2 and out['cached_alias_without_HTTP'] and out['cached_quantity_without_HTTP'] and out['process_separate']
 assert str(out['original_body']['properties']['other']['color'])=='3' and out['original_body']['properties']['other']['sideId']=='2'


def test_public_native_cache_excerpts_keep_original_hashes():
 import hashlib
 manifest=json.loads((C/'source/manifest.json').read_text())
 for name,digest in manifest['files'].items():
  assert hashlib.sha256((C/'source'/name).read_bytes()).hexdigest()==digest
