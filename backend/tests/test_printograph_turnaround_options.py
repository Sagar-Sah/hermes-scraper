"""Pure saved-source request identity regressions; no supplier or app/Store import."""
import copy,hashlib,importlib.util,json,os,pathlib,shutil,subprocess
import pytest
ROOT=pathlib.Path(__file__).resolve().parent
FIXTURE=ROOT/'fixtures/printograph_turnaround_options'
MODERN=pathlib.Path(os.environ.get('HERMES_PRINTOGRAPH_MODERN_TEST_OVERLAY',ROOT.parent/'crawler'))/'printograph_modern_browser.py'
spec=importlib.util.spec_from_file_location('portable_turnaround_option_identity',MODERN);M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)

def raw():
 services={'proofType':'100','manualProofType':'36','fileDelivery':'5','designServices':'42'}
 groups=[{'key':k,'control_count':0,'choices':[]} for k in M.SERVICE_KEYS]
 for g,v,label in zip(groups[:3],('100','36','5'),('Manually Processed','No Proof','Upload Files Now')):
  g.update(control_count=1,choices=[{'value':v,'checked':True,'label':label,'disabled':False,'visible':True}])
 specs={'shape':'15','size':'727','orientation':'1','paper':'1','color':'1'}
 return {'observed':True,'source_url':'https://www.printograph.com/products/special-shapes/order','product_type':'39','groups':groups,'model_services':services,'source_item_type':{'observed':True,'is_blank_product':False,'value':'5'},'native_total':{'observed':True,'controller_total':'13.00','applied_product_price':'13.00'},'base_context':{'product_id':'100222293','specs':copy.deepcopy(specs),'production':'1'},'selection_context':{'specs':copy.deepcopy(specs),'options':{'coating':'5','paperFinish':'1'},'services':services,'quantity':'50','production':'1'}}

def body():return {'quantity':50,'specifications':{'printProduct':{'productType':39,'shape':15,'size':727,'paper':1,'color':1,'turnarounds':[1,3]},'other':{}},'productOptions':[36,5],'itemType':5}

def observe(g,kind='price',before=None,after=None):
 value=raw();before=before or value;after=after or value
 g.observe('https://www.printograph.com/service/rest/v1/'+('products/100222293/prices' if kind=='price' else 'rules/turnarounds'),200,{'items':[{'quantity':50,'markupPrice':'13.00'}]} if kind=='price' else {'items':[{'id':1,'range':{'min':3,'max':5,'label':None}},{'id':3,'range':{'min':3,'max':None,'label':None}}]},before,after,'fixture-'+kind,1,1,8,8,'GET' if kind=='price' else 'POST',None if kind=='price' else json.dumps(body()))

def test_exact_original_native_update_and_turnaround_methods_exclude_paperfinish():
 if shutil.which('node') is None:pytest.skip('Node.js required for exact saved-source method replay')
 result=subprocess.run(['node',str(FIXTURE/'source_builder.js'),str(FIXTURE)],capture_output=True,text=True,check=True)
 value=json.loads(result.stdout);assert value['request']==body();assert value['source_options']==[36,5]
 assert value['selected_paperFinish']==1 and value['selected_manualProofType']==36
 assert len(M._turnaround_request(json.dumps(value['request']),raw()))==64

def test_full_source_production_join_now_accepts_but_never_claims_completion():
 g=M.FreshQuoteGate();observe(g);observe(g,'production');v=g.verify(raw(),{'value':'1','selected_label':'Regular 3–5 Business Days'},displayed_subtotal='13.00')
 assert v['production']=={'min':3,'max':5} and v['source_data_finalized'] is False and v['component_arithmetic_verified'] is False

@pytest.mark.parametrize('change',['productType','shape','size','paper','color','quantity','proof','coating','missing-proof','missing-coating','duplicate','catalog-option-id','unexpected-paperfinish','itemType','private-key'])
def test_other_request_context_and_service_guards_remain_strict(change):
 value=body()
 if change in ('productType','shape','size','paper','color'):value['specifications']['printProduct'][change]=999
 elif change=='quantity':value['quantity']=100
 elif change=='proof':value['productOptions'][0]=1
 elif change=='coating':value['productOptions'][1]=6
 elif change=='missing-proof':value['productOptions']=[5]
 elif change=='missing-coating':value['productOptions']=[36]
 elif change=='duplicate':value['productOptions']=[36,5,5]
 elif change=='catalog-option-id':value['productOptions'][0]=103306006
 elif change=='unexpected-paperfinish':value['productOptions'].append(1)
 elif change=='itemType':value['itemType']=13
 else:value['private']='never-persist'
 with pytest.raises(M.ModernHeld,match='turnaround_request_unproven'):M._turnaround_request(json.dumps(value),raw())

@pytest.mark.parametrize('change',['paperFinish','coating','proof','quantity'])
def test_all_full_before_after_dependencies_still_invalidate_context_drift(change):
 before=raw();after=copy.deepcopy(before)
 if change in ('paperFinish','coating'):after['selection_context']['options'][change]='2'
 elif change=='proof':after['selection_context']['services']['manualProofType']='1'
 else:after['selection_context']['quantity']='100'
 g=M.FreshQuoteGate();observe(g);observe(g,'production',before=before,after=after)
 with pytest.raises(M.ModernHeld):g.verify(after,{'value':'1','selected_label':'Regular 3–5 Business Days'},displayed_subtotal='13.00')

def test_prior_production_cannot_be_reused_after_paperfinish_only_change():
 g=M.FreshQuoteGate();observe(g);observe(g,'production');after=raw();after['selection_context']['options']['paperFinish']='2'
 with pytest.raises(M.ModernHeld,match='modern_current_production_evidence_missing'):g.verify(after,{'value':'1','selected_label':'Regular 3–5 Business Days'},displayed_subtotal='13.00')

def test_saved_public_method_fixtures_are_exact_and_self_contained():
 manifest=json.loads((FIXTURE/'manifest.json').read_bytes())
 for rel,digest in manifest['files'].items():assert hashlib.sha256((FIXTURE/rel).read_bytes()).hexdigest()==digest

@pytest.mark.parametrize('key',['paperfinish','unknownPresentation','otherOption'])
def test_no_other_selected_option_is_silently_excluded(key):
 value=raw();value['selection_context']['options'][key]='777'
 with pytest.raises(M.ModernHeld,match='turnaround_request_unproven'):M._turnaround_request(json.dumps(body()),value)

def test_excluding_paperfinish_does_not_exclude_equal_numeric_coating_value():
 value=raw();value['selection_context']['options']['coating']='1';request=body();request['productOptions']=[36]
 with pytest.raises(M.ModernHeld,match='turnaround_request_unproven'):M._turnaround_request(json.dumps(request),value)
