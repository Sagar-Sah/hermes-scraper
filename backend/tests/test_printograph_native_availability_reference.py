"""A native price quote is distinct from a stock/checkout guarantee."""
from copy import deepcopy
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess

import pytest
import test_printograph_modern_post_prices as base
from crawler import printograph_modern_browser as m
base.m=m
ROOT=Path(__file__).resolve().parent/"fixtures/printograph_native_availability"
PAIRS=[('https://www.printograph.com/products/cardboards/order','10016'),('https://www.printograph.com/products/acrylic-boards/order','10019')]

def raw(pair=PAIRS[0]):
    v=base.raw();url,kind=pair;v['source_url']=url;v['product_type']=kind
    g=v['generic_price_context'];g['properties']['printProductType']=kind;g['turnaround']['printProduct']['productType']=kind
    return v

def controls(q='2'):
    return [{'key':'quantity','kind':'select','value':q,'choices':[{'value':q,'disabled':False,'placeholder':False}]}]

def data(available=False,price=13.43):
    return {'items':[{'quantity':2,'productPrice':price,'subtotal':None,'available':available}]}

def gate_for(v=None,price=None):
    v=v or raw();g=m.FreshQuoteGate();base.observe(g,v,data=price or data());base.observe(g,v,kind='production');return g

def verify(g,v=None,c=None,production=None,display='13.43'):
    return g.verify(v or raw(),production or {'value':'1','selected_label':'Regular 3–4 Business Days'},controls() if c is None else c,display)

@pytest.mark.parametrize('pair',PAIRS)
@pytest.mark.parametrize('flag',[False,True])
def test_exact_native_pair_quotes_positive_current_price_and_keeps_original_flag(pair,flag):
    v=raw(pair);out=verify(gate_for(v,data(flag)),v)
    assert out['source_api_available_flag'] is flag
    assert out['stock_availability_verified'] is False and out['native_enabled_quantity_observed'] is True
    assert 'source_quantity_available' not in out
    assert out['native_computed_subtotal_matches_display'] and out['current_quantity_applied_base_matches_table']
    assert not out['component_arithmetic_verified'] and not out['source_data_finalized']

@pytest.mark.parametrize('change',['other_type','other_url','query','swapped_pair','http','unknown_family'])
def test_false_flag_is_not_general_availability_relaxation(change):
    v=raw()
    if change=='other_type':v['product_type']='47001'
    if change=='other_url':v['source_url']='https://www.printograph.com/products/another/order'
    if change=='query':v['source_url']+='?x=1'
    if change=='swapped_pair':v['source_url']=PAIRS[1][0]
    if change=='http':v['source_url']=v['source_url'].replace('https:','http:')
    if change=='unknown_family':v=base.raw()
    v['generic_price_context']['properties']['printProductType']=v['product_type'];v['generic_price_context']['turnaround']['printProduct']['productType']=v['product_type']
    with pytest.raises(m.ModernHeld):verify(gate_for(v),v)

@pytest.mark.parametrize('change',['absent','duplicate_control','radio','wrong_selected','disabled','placeholder','unknown_disabled','unknown_placeholder','missing_choice','duplicate_choice'])
def test_false_flag_requires_actual_unique_enabled_nonplaceholder_quantity(change):
    c=controls()
    if change=='absent':c=[]
    if change=='duplicate_control':c+=deepcopy(c)
    if change=='radio':c[0]['kind']='radio'
    if change=='wrong_selected':c[0]['value']='10'
    if change=='disabled':c[0]['choices'][0]['disabled']=True
    if change=='placeholder':c[0]['choices'][0]['placeholder']=True
    if change=='unknown_disabled':c[0]['choices'][0].pop('disabled')
    if change=='unknown_placeholder':c[0]['choices'][0].pop('placeholder')
    if change=='missing_choice':c[0]['choices']=[]
    if change=='duplicate_choice':c[0]['choices']+=deepcopy(c[0]['choices'])
    with pytest.raises(m.ModernHeld):verify(gate_for(),c=c)

def test_false_flag_with_no_controls_never_qualifies():
    with pytest.raises(m.ModernHeld,match='quantity_unavailable'):
        gate_for().verify(raw(),{'value':'1','selected_label':'Regular 3–4 Business Days'},displayed_subtotal='13.43')

@pytest.mark.parametrize('change',['display','controller','applied','zero_base','missing_base','no_price','no_production','wrong_production','wrong_range','service_drift','dimension_drift','old_epoch','old_generation','wrong_request_quantity'])
def test_false_reference_never_bypasses_existing_quote_and_freshness_guards(change):
    v=raw();g=gate_for(v);display='13.43';prod={'value':'1','selected_label':'Regular 3–4 Business Days'}
    if change=='display':display='13.44'
    if change=='controller':v['native_total']['controller_total']='13.44'
    if change=='applied':v['native_total']['applied_product_price']='13.44'
    if change=='zero_base':v['native_total'].update(controller_total='0',applied_product_price='0');display='0';g=gate_for(v,data(False,0))
    if change=='missing_base':g=gate_for(v,data(False,None))
    if change=='no_price':g=m.FreshQuoteGate();base.observe(g,v,kind='production')
    if change=='no_production':g=m.FreshQuoteGate();base.observe(g,v,data=data())
    if change=='wrong_production':prod['value']='3'
    if change=='wrong_range':prod['selected_label']='Regular 1–2 Business Days'
    if change=='service_drift':v['selection_context']['services']['manualProofType']='1'
    if change=='dimension_drift':v['generic_price_context']['dimensions']['width']='48'
    if change in {'old_epoch','old_generation'}:
        g=m.FreshQuoteGate();base.observe(g,v,data=data(),**({'epoch':8} if change=='old_epoch' else {'generation':2}));base.observe(g,v,kind='production')
    if change=='wrong_request_quantity':
        g=m.FreshQuoteGate();body=base.price_body(v);body['properties']['quantity']='1-200';base.observe(g,v,data=data(),body=body);base.observe(g,v,kind='production')
    with pytest.raises(m.ModernHeld):verify(g,v,production=prod,display=display)

def test_real_archived_false_tables_are_only_reference_data_without_new_native_proof():
    summary=json.loads((ROOT/'source/readonly-evidence.json').read_text())
    assert summary['private_capture_records_and_price_values_omitted'] is True
    assert summary['current_quote_or_stock_proof'] is False
    refs=[v for v in summary['public_response_projections'] if v['ref']['path']=='/service/rest/v1/product/prices']
    assert len(refs)==4 and all(x['available_false_count']==150 for x in refs)
    domain=json.loads((ROOT/'source/reason-domain-projection.json').read_text())
    assert len(domain)==4 and all(x['unavailable_positive_product_price_count']==150 for x in domain)
    d=json.loads((base.FIXTURES/'native-unavailable-price-response.json').read_text())
    assert len(m._generic_prices(d))==150
    # No raw historical response or synthetic matching prices can stand in for
    # a fresh exact body's native current applied total and production capture.
    g=m.FreshQuoteGate()
    with pytest.raises(m.ModernHeld,match='current_price_evidence_missing'):verify(g)

def test_original_native_applier_ignores_available_and_retains_price_and_options():
    js=(ROOT/'source/generic-price-applier.js').read_text().split(':',1)[1]
    script="""const apply=eval('('+JSON.parse(process.argv[1])+')');
const K={getAppliedPricesForGenericProducts:()=>0},document={getElementById:()=>({style:{}})};
let out=[];for(const type of [10016,10019])for(const flag of [false,true,undefined]){
 const row={productPrice:11.34,itemOptionsPrice:{53:49}};if(flag!==undefined)row.available=flag;
 const model={userSelectedOptions:{color:2},others:{subItemProducts:[],odImagesLoading:false,loaders:[]},appliedPrices:{designServices:{}}};
 apply(row,{},model,type,{},true);out.push({type,flag:flag===undefined?'absent':flag,base:model.appliedPrices.productPrice,design:model.appliedPrices.designServices[53]});}
console.log(JSON.stringify(out));"""
    r=subprocess.run(['node','-e',script,json.dumps(js)],check=True,capture_output=True,text=True)
    out=json.loads(r.stdout);assert len(out)==6 and all(x['base']==11.34 and x['design']==49 for x in out)

def test_bounded_saved_public_source_scan_and_exact_excerpt_pins():
    proof=json.loads((ROOT/'source-proof.json').read_text());assert len(proof['sources'])==2
    assert all(v['count']==0 for v in proof['property_lookup_scan'].values())
    for s in proof['slices']:assert hashlib.sha256((ROOT/s['file']).read_bytes()).hexdigest()==s['sha256']
    for name in ('button-disabled.js','button-render.js','generic-price-builder.js','generic-price-applier.js'):
        assert not re.search(r'\.available\b|\[\s*[\'\"]available[\'\"]\s*\]|\bavailable\s*:',(ROOT/'source'/name).read_text())

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['plain','hidden','disabled','option_disabled','placeholder'])
async def test_actual_snapshot_quantity_shape_and_visibility_enablement_in_chromium(change):
    from playwright.async_api import async_playwright
    async with async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file():pytest.skip('local Chromium absent')
        browser=await runtime.chromium.launch(headless=True);context=await browser.new_context(service_workers='block');page=await context.new_page();requests=[]
        html=base.html().replace('47001','10016').replace('<button type=button','<label for="quantity">Quantity</label><select id="quantity"><option value="1">1</option><option value="2" selected>2</option><option value="150">150</option></select><button type=button',1)
        async def route(r):
            requests.append(r.request.url)
            if r.request.url==PAIRS[0][0]:await r.fulfill(status=200,content_type='text/html',body=html)
            else:await r.abort()
        await context.route('**/*',route)
        try:
            await page.goto(PAIRS[0][0]);scripts={'plain':'0','hidden':"document.querySelector('#quantity').style.display='none'",'disabled':"document.querySelector('#quantity').disabled=true",'option_disabled':"document.querySelector('#quantity').selectedOptions[0].disabled=true",'placeholder':"document.querySelector('#quantity').selectedOptions[0].textContent='Select quantity'"}
            await page.evaluate(scripts[change])
            scope={'re':re,'deepcopy':deepcopy,'AdapterError':ValueError}
            for name in ('ALIASES','field_key','normalize_snapshot','SNAPSHOT_JS'):exec((ROOT/'source'/('adapter-'+name+'.py')).read_text(),scope)
            snap=scope['normalize_snapshot'](await page.evaluate(scope['SNAPSHOT_JS'],{'priceSelector':None,'modernServices':True}))
            c=[x for x in snap['controls'] if x['key']=='quantity']
            if c:assert 'disabled' not in c[0] and c[0]['kind']=='select'
            g=gate_for()
            if change=='plain':assert verify(g,c=c)['native_enabled_quantity_observed']
            else:
                with pytest.raises(m.ModernHeld):verify(g,c=c)
            assert requests==[PAIRS[0][0]]
        finally:await context.close();await browser.close()

@pytest.mark.parametrize('pair',PAIRS)
def test_explicit_pdf_policy_false_reference_retains_actual_native_fee_total(pair):
    v=raw(pair);v['model_services']['manualProofType']='1'
    group=next(g for g in v['groups'] if g['key']=='proofTypeForManual')
    group['choices'][0].update(value='1',label='PDF Proof')
    v['generic_price_context']['turnaround']['options']=['1']
    v['native_total']['controller_total']='15.43'
    v=m.bind_service_policy(v,m.PDF_SERVICE_POLICY,v['source_url'])
    g=m.FreshQuoteGate(service_policy=m.PDF_SERVICE_POLICY,source_url=v['source_url'])
    base.observe(g,v,data=data());base.observe(g,v,kind='production')
    out=verify(g,v,display='15.43')
    assert out['service_policy']==m.PDF_SERVICE_POLICY and out['source_api_available_flag'] is False
    assert out['native_computed_subtotal_matches_display'] and out['api_subtotal_reference_only']
    assert not out['component_arithmetic_verified'] and not out['source_data_finalized']

@pytest.mark.parametrize('flag',[None,0,1,'false',[],{}])
def test_reference_flag_remains_explicit_strict_boolean(flag):
    with pytest.raises(m.ModernHeld):m._generic_prices(data(flag))
