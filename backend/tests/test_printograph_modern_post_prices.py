"""Future native POST support only; saved unavailable receipts stay held."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT=Path(__file__).resolve().parents[2]
FIXTURES=Path(__file__).resolve().parent/'fixtures/printograph_modern_post'
SPEC=importlib.util.spec_from_file_location('isolated_modern_post',ROOT/'backend/crawler/printograph_modern_browser.py')
m=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(m)
URL='https://www.printograph.com/products/synthetic-rigid/order'
PRICE_URL='https://www.printograph.com/service/rest/v1/product/prices?itemId=null&cid='
TURN_URL='https://www.printograph.com/service/rest/v1/rules/turnarounds'
HID={'hidHoleDrillingId':'materialHoleDrilling','hidSideId':'color','hidSizeId':'size','hidWidthId':'width','hidHeightId':'height','hidDesignServiceId':'designServices','hidMaterialColorId':'materialColor','hidCoreColorId':'coreColor','hidMaterialId':'material','hidShapeId':'shape','hidAccessoriesId':'accessories','hidThicknessId':'thickness','hidOrientationId':'orientation'}
LABELS={'Size':'size','Orientation':'orientation','Thickness':'thickness','Material':'material','Accessories':'accessories','Material Color':'materialColor','Material Core Color':'coreColor'}


def raw():
    specs={'material':'1','thickness':'2','materialColor':'1','coreColor':'1','shape':'8','size':'11','orientation':'1','color':'1','materialHoleDrilling':'0','accessories':''}
    services={'proofType':'100','manualProofType':'36','fileDelivery':'5','designServices':'53'}
    groups=[{'key':key,'control_count':0,'choices':[]} for key in m.SERVICE_KEYS]
    for group,value,label in zip(groups[:3],('100','36','5'),('Manually Processed','No Proof','Upload Files Now')):
        group.update(control_count=1,choices=[{'value':value,'checked':True,'label':label,'disabled':False,'visible':True}])
    groups[4].update(control_count=1,choices=[{'value':'53','checked':True,'label':'No Design Services','disabled':False,'visible':True}])
    other={k:('24' if k=='hidWidthId' else '36' if k=='hidHeightId' else services['designServices'] if v=='designServices' else specs[v] if specs[v]!='0' else '') for k,v in HID.items()}
    other.update({'Size':'24 x 36','Thickness':'3/16 inch','Material':'Rigid stock','Material Color':'White','Material Core Color':'White'})
    native={'paper':'','color':'1',**{k[3].lower()+k[4:]:v for k,v in other.items() if k.startswith('hid')}}
    binding={'product_id':'900012','specs':specs,'options':{},'services':services,'production':'1'}
    generic={'observed':True,'properties':{'size':'11','printProductType':'47001','turnaround':'1','quantity':'1-150','other':native},
             'turnaround':{'printProduct':{'productType':'47001','size':'11','color':'1','paper':'','shape':'8'},'other':other,'options':['36']},
             'quantity_choices':['1','2','10','150'],'dimensions':{'width':'24','height':'36'},'binding':binding}
    return {'observed':True,'source_url':URL,'product_type':'47001','groups':groups,'model_services':services,
            'source_item_type':{'observed':True,'is_blank_product':False,'value':'5'},
            'native_total':{'observed':True,'controller_total':'13.43','applied_product_price':'13.43'},
            'base_context':{'product_id':'900012','specs':specs,'production':'1'},
            'selection_context':{'specs':specs,'options':{},'services':services,'quantity':'2','production':'1'},'generic_price_context':generic}


def price_body(value): return {'properties':deepcopy(value['generic_price_context']['properties'])}
def turn_body(value):
    g=value['generic_price_context']['turnaround']
    return {'quantity':value['selection_context']['quantity'],'specifications':{'printProduct':{**g['printProduct'],'turnarounds':[1,3]},'other':g['other']},'productOptions':g['options'],'itemType':5}
def future_response():
    # Deliberately synthetic positive row in the actually observed API schema.
    return {'items':[{'quantity':2,'productPrice':13.43,'subtotal':13.43,'available':True,'itemOptionsPrice':{}}]}
def observe(gate,value=None,body=None,data=None,kind='price',**kwargs):
    value=value or raw();gate.observe(kwargs.pop('url',PRICE_URL if kind=='price' else TURN_URL),kwargs.pop('status',200),data or (future_response() if kind=='price' else {'items':[{'id':1,'range':{'min':3,'max':4}}]}),
        kwargs.pop('before',deepcopy(value)),kwargs.pop('after',deepcopy(value)),kwargs.pop('evidence_id','price1' if kind=='price' else 'turn1'),
        1,kwargs.pop('generation',1),7,kwargs.pop('epoch',7),kwargs.pop('method','POST'),body if isinstance(body,str) else json.dumps(body or (price_body(value) if kind=='price' else turn_body(value))))
    assert not kwargs
def verified(gate,value=None,display='13.43'):
    return gate.verify(value or raw(),{'value':'1','selected_label':'Regular 3–4 Business Days'},displayed_subtotal=display)


def test_future_native_source_shaped_price_and_production_join_without_fee_formula():
    gate=m.FreshQuoteGate();observe(gate);observe(gate,kind='production');out=verified(gate)
    assert out['native_price_schema']=='generic-product-price/v1' and out['api_subtotal_reference_only'] and out['source_api_available_flag'] is True and out['stock_availability_verified'] is False
    assert not out['component_arithmetic_verified'] and not out['source_data_finalized']


def test_synthetic_source_shaped_150_unavailable_rows_remain_unavailable():
    data=json.loads((FIXTURES/'native-unavailable-price-response.json').read_text())
    assert data['fixture_provenance']['synthetic'] is True
    assert data['fixture_provenance']['source_price_evidence'] is False
    rows=m._generic_prices(data)
    assert len(rows)==150 and all(row['available'] is False for row in rows.values())
    gate=m.FreshQuoteGate();observe(gate,data=data);observe(gate,kind='production')
    with pytest.raises(m.ModernHeld,match='quantity_unavailable'):verified(gate)


@pytest.mark.parametrize('change',['sku','size','paper','color','service','width','height','quantity','unknown','duplicate','method','query','private_query','fragment','old_epoch','old_generation','status','before_drift','after_dims','after_service'])
def test_native_price_request_must_match_full_current_source_projection(change):
    value=raw();body=price_body(value);kw={}
    if change=='sku':body['properties']['printProductType']='47002'
    if change=='size':body['properties']['size']='12'
    if change in {'paper','color'}:body['properties']['other'][change]='99'
    if change=='service':body['properties']['other']['designServiceId']='42'
    if change in {'width','height'}:body['properties']['other'][change+'Id']='48'
    if change=='quantity':body['properties']['quantity']='1-300'
    if change=='unknown':body['properties']['other']['account']='SYNTHETIC-PRIVATE'
    if change=='duplicate':body=json.dumps(body)[:-1]+',"properties":{}}'
    if change=='method':kw['method']='GET'
    if change=='query':kw['url']=PRICE_URL.replace('itemId=null&cid=','cid=&itemId=null')
    if change=='private_query':kw['url']=PRICE_URL+'private'
    if change=='fragment':kw['url']=PRICE_URL+'#fragment'
    if change=='old_epoch':kw['epoch']=8
    if change=='old_generation':kw['generation']=2
    if change=='status':kw['status']=503
    if change=='before_drift':kw['before']=raw();kw['before']['base_context']['product_id']='900013'
    if change=='after_dims':kw['after']=raw();kw['after']['generic_price_context']['dimensions']['width']='48'
    if change=='after_service':kw['after']=raw();kw['after']['selection_context']['services']['manualProofType']='1'
    gate=m.FreshQuoteGate();observe(gate,value,body=body,**kw);observe(gate,value,kind='production')
    with pytest.raises(m.ModernHeld):verified(gate,value)


@pytest.mark.parametrize('change',['quantity','product','dimension','human_label','service','itemType','missing_other','unknown_other','turnaround_ids','duplicate'])
def test_generic_turnaround_uses_full_source_other_and_current_services(change):
    body=turn_body(raw())
    if change=='quantity':body['quantity']=10
    if change=='product':body['specifications']['printProduct']['productType']=47002
    if change=='dimension':body['specifications']['other']['hidWidthId']='48'
    if change=='human_label':body['specifications']['other']['Material']='Different stock'
    if change=='service':body['productOptions']=['1']
    if change=='itemType':body['itemType']=6
    if change=='missing_other':body['specifications']['other']={}
    if change=='unknown_other':body['specifications']['other']['private']='SYNTHETIC-PRIVATE'
    if change=='turnaround_ids':body['specifications']['printProduct']['turnarounds']=[1]
    if change=='duplicate':body=json.dumps(body)[:-1]+',"quantity":2}'
    gate=m.FreshQuoteGate();observe(gate);observe(gate,kind='production',body=body)
    with pytest.raises(m.ModernHeld):verified(gate)


@pytest.mark.parametrize('change',['display','controller','applied','availability','dimensions','service','missing_context'])
def test_final_current_native_amount_availability_and_identity_are_rechecked(change):
    gate=m.FreshQuoteGate();observe(gate);observe(gate,kind='production');value=raw();display='13.43'
    if change=='display':display='13.44'
    if change=='controller':value['native_total']['controller_total']='13.44'
    if change=='applied':value['native_total']['applied_product_price']='13.44'
    if change=='availability':observe(gate,data={'items':[{'quantity':2,'productPrice':13.43,'subtotal':13.43,'available':False}]})
    if change=='dimensions':value['generic_price_context']['dimensions']['width']='48';value['generic_price_context']['properties']['other']['widthId']='48'
    if change=='service':value['selection_context']['services']['manualProofType']='1'
    if change=='missing_context':value.pop('generic_price_context')
    with pytest.raises(m.ModernHeld):verified(gate,value,display)


@pytest.mark.parametrize('row',[{}, {'quantity':True,'productPrice':1,'subtotal':1,'available':True}, {'quantity':2,'productPrice':True,'subtotal':1,'available':True}, {'quantity':2,'productPrice':'NaN','subtotal':1,'available':True}, {'quantity':2,'productPrice':1,'subtotal':1,'available':'true'}])
def test_generic_row_schema_never_defaults_availability_or_money(row):
    with pytest.raises(m.ModernHeld):m._generic_prices({'items':[row]})


def test_quantity_only_reuse_requires_current_production_and_full_dimension_identity():
    gate=m.FreshQuoteGate();value=raw();data={'items':[future_response()['items'][0],{'quantity':10,'productPrice':25,'subtotal':25,'available':True}]}
    observe(gate,value,data=data);observe(gate,value,kind='production');verified(gate,value)
    value['selection_context']['quantity']='10';value['native_total'].update(controller_total='25',applied_product_price='25')
    with pytest.raises(m.ModernHeld,match='production_evidence_missing'):verified(gate,value,'25')
    observe(gate,value,kind='production');assert verified(gate,value,'25')['api_subtotal_reference_only']
    value['generic_price_context']['dimensions']['width']='48';value['generic_price_context']['properties']['other']['widthId']='48';value['generic_price_context']['turnaround']['other']['hidWidthId']='48'
    observe(gate,value,data=data)
    with pytest.raises(m.ModernHeld,match='production_evidence_missing'):verified(gate,value,'25')


@pytest.mark.parametrize('reference',[None,13.43,100])
def test_native_total_can_include_current_fee_without_using_api_subtotal(reference):
    value=raw();value['native_total']['controller_total']='15.43'
    data=future_response();data['items'][0]['subtotal']=reference
    gate=m.FreshQuoteGate();observe(gate,value,data=data);observe(gate,value,kind='production')
    result=verified(gate,value,'15.43')
    assert result['api_subtotal_reference_only'] and not result['component_arithmetic_verified']


def test_rendered_production_id_cannot_be_another_current_response_choice():
    gate=m.FreshQuoteGate();observe(gate);observe(gate,kind='production',data={'items':[{'id':1,'range':{'min':3,'max':4}},{'id':3,'range':{'min':1,'max':2}}]})
    with pytest.raises(m.ModernHeld,match='production_stale'):
        gate.verify(raw(),{'value':'3','selected_label':'Rush 1–2 Business Days'},displayed_subtotal='13.43')


def test_native_range_is_literal_even_when_schema_has_later_out_of_range_choice():
    value=raw();value['generic_price_context']['quantity_choices']=['1','2','200','150'];value['selection_context']['quantity']='200'
    gate=m.FreshQuoteGate();observe(gate,value,data={'items':[{'quantity':200,'productPrice':13.43,'subtotal':13.43,'available':True}]})
    with pytest.raises(m.ModernHeld):verified(gate,value)
    gate=m.FreshQuoteGate();data=future_response();data['items'].append({'quantity':200,'productPrice':13.43,'available':True})
    observe(gate,data=data)
    with pytest.raises(m.ModernHeld):verified(gate)


def html():
    value=raw();selected={**value['selection_context']['specs'],**value['model_services'],'quantity':'2','productId':'900012','productionTime':'1','paper':'','shoppingCartItemId':'','priceGroupId':'','centerSplit':'','standType':'','frame':''}
    fields={'specs':list(value['selection_context']['specs']),'options':[],'other':{**HID,**LABELS}}
    schema={'size':{'list':[{'id':'11','width':24,'height':36,'label':'24 x 36'}]},'quantity':{'list':[{'quantity':q} for q in (1,2,10,150)]}}
    labels={'orientation':('1','Horizontal'),'thickness':('2','3/16 inch'),'material':('1','Rigid stock'),'materialColor':('1','White'),'coreColor':('1','White')}
    schema.update({k:{'list':[{'id':v,'label':label}]} for k,(v,label) in labels.items()})
    others={'isAdmin':False,'waiveFees':False,'itemId':'null','editOrderItem':{},'blankProducts':[],'getGenericProductsPriceFromApiList':[47001],'subItemProducts':[],'embroideryProducts':[],'genericOrientation':[],'redirectFromMediaClip':False,'defaultSizeOptionCategories':{'Tall':[]},'apparelSizeStyle1':[]}
    radios=''.join(f'<input type=radio id="{g["key"]}x" name="{g["key"]}" value="{g["choices"][0]["value"]}" checked><label for="{g["key"]}x">{g["choices"][0]["label"]}</label>' for g in value['groups'] if g['choices'])
    c={'printProductType':47001,'fields':{'47001':fields},'schema':schema,'optionsHavingPrices':[],'jsonObj':{'userSelectedOptions':selected,'others':others,'appliedPrices':{'productPrice':'13.43','holeDrillingPrice':0,'standType':{},'frame':{}}}}
    return '<!doctype html><div id=ajaxLoader></div><div id=order-form data-printproducttype=47001 data-currentstoretype=broker><button type=button aria-controls=collapseOne>Process</button><div id=collapseOne class=show>'+radios+'</div></div><script>window.controller='+json.dumps(c)+';controller.totalPrice="13.43";document.querySelector("#order-form")._vnode={component:{proxy:{$refs:{childComponents:{_:{subTree:{component:{refs:{orderFormInstance:controller}}}}}}}}};</script>'


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['plain','orientation','color','dimension','hole_fee'])
async def test_readonly_projection_matches_exact_saved_native_builders_in_chromium(change):
    from playwright.async_api import async_playwright
    async with async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file():pytest.skip('local Chromium absent')
        browser=await runtime.chromium.launch(headless=True);context=await browser.new_context(service_workers='block');page=await context.new_page();requests=[]
        async def route(r):
            requests.append(r.request.url)
            if r.request.url==URL:await r.fulfill(status=200,content_type='text/html',body=html())
            else:await r.abort()
        await context.route('**/*',route)
        try:
            await page.goto(URL)
            scripts={'plain':'0','orientation':"controller.jsonObj.userSelectedOptions.orientation='2'",'color':"controller.jsonObj.userSelectedOptions.color='3'",'dimension':"controller.schema.size.list[0].width=48",'hole_fee':"controller.jsonObj.userSelectedOptions.materialHoleDrilling='79';controller.jsonObj.appliedPrices.holeDrillingPrice=2"}
            await page.evaluate(scripts[change]);observed=await page.evaluate(m.SERVICE_READ_JS,{'source_url':URL})
            assert observed['observed'] and observed['generic_price_context']['observed']
            snippets={name:(FIXTURES/file).read_text().split(':',1)[1] for name,file in [('parameters','generic-parameters.js'),('price','generic-price-builder.js'),('turn','native-turnaround-builder.js'),('options','native-options.js')]}
            native=await page.evaluate('''snippets=>{const parameters=eval('('+snippets.parameters+')'),options=eval('('+snippets.options+')');
                const o={A:{getPrintProductType:(e,t)=>t}},s={};s.parametersOnAddToCartStateless=(e,t,i,fields)=>JSON.stringify(options(e,t,fields,parameters(e,t,i,fields,{itemDetail:{type:e.userSelectedOptions.fileDelivery,productOptions:[],specifications:{printProduct:{},other:{}}}})));
                const X={A:{parametersOnAddToCart:s.parametersOnAddToCartStateless}},w={A:{getUrlParameter:()=>''}},R={A:{getGenericProductsPrice:(url,body)=>{window.captured={url,body};return {then:()=>({catch:()=>{}})}}}};
                const owner={validParams:()=>true,storedGenericPrice:{}};eval('('+snippets.price+')').call(owner,controller.jsonObj,controller.printProductType,controller.schema,controller.fields);
                const turn=eval('('+snippets.turn+')')(controller.jsonObj,controller.printProductType,controller.schema,controller.fields);
                return {price:window.captured,turn:JSON.parse(turn)};}''',snippets)
            assert m._scalar_tree(native['price']['body'])==m._scalar_tree({'properties':observed['generic_price_context']['properties']})
            m._generic_price_request(json.dumps(native['price']['body']),observed)
            m._turnaround_request(json.dumps(native['turn']),observed)
            assert requests==[URL]
        finally:await context.close();await browser.close()


@pytest.mark.asyncio
async def test_exact_public_total_builder_keeps_pdf_fee_separate_from_base_table():
    from playwright.async_api import async_playwright
    async with async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file():pytest.skip('local Chromium absent')
        browser=await runtime.chromium.launch(headless=True);page=await browser.new_page()
        try:
            # Exact saved function in a blank local document, not a source call.
            fn=(FIXTURES/'native-total.js').read_text().split(':',1)[1]
            result=await page.evaluate('''fn=>{const o={A:{isGeneric:()=>true}},total=eval('('+fn+')');
                const e={userSelectedOptions:{proofType:100,manualProofType:1},appliedPrices:{productPrice:13.43,proofType:{1:2},holeDrillingPrice:0}};
                return total(e,47001,{47001:{options:[]}});}''',fn)
            assert result=='15.43'
            # The price API's reported subtotal cannot prove that separate fee.
            assert future_response()['items'][0]['subtotal']==13.43
        finally:await browser.close()


def test_public_source_excerpt_fixtures_remain_exact_and_self_contained():
    import hashlib
    manifest=json.loads((FIXTURES/'manifest.json').read_text())
    for name,digest in manifest['files'].items():
        assert hashlib.sha256((FIXTURES/name).read_bytes()).hexdigest()==digest
    assert manifest['actual_request_body_retained'] is False
    assert manifest['historical_unavailable_row_count']==150
    assert manifest['price_rows_are_synthetic'] is True
