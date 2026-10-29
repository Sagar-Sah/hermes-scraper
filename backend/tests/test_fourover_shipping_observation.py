"""Synthetic monetary fixtures; no current supplier prices or account records."""
import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from crawler.fourover_shipping_observation import (SHIPPING_CONTROL_OBSERVATION_JS, observation_labels,
    passive_pilot_projection, wait_for_shipping_hydration, select_existing_benchmark_destination,
    forbidden_observation_write)


def test_passive_dom_projection_redacts_customer_and_address_book_values_without_actions():
    node=shutil.which('node')
    if not node:pytest.skip('Node runtime absent')
    # A VM DOM fixture exercises the observer with public labels and malicious
    # private values; no browser, network, source writes or address save occurs.
    harness=r'''
const vm=require('vm');const observer=OBSERVER;
function element({tag='input',id='',name='',type='',text='',value='',label='',attrs={},className=''}={}){
 return {tagName:tag.toUpperCase(),id,name,type,textContent:text,value,className,disabled:false,checked:false,
  options:[],src:'',getClientRects:()=>[{}],getAttribute:key=>attrs[key]||null,
  closest:selector=>selector==='label'&&label?{textContent:label}:null,
  click:()=>{throw Error('Observer attempted mutation');}};
}
const controls=[element({id:'zip-code',name:'postcode',value:'85746',label:'ZIP Code'}),
 element({id:'region',name:'state',value:'AZ',label:'State'}),
 element({id:'address-42',name:'address_id',tag:'select',value:'778899',label:'Address Book'}),
 element({id:'firstname',value:'TonyJones',label:'First Name'}),
 element({name:'email',value:'alice@example.org'}),element({name:'phone',value:'5551234567'}),
 element({name:'street',value:'123 Elm Lane'}),
 element({tag:'input',type:'checkbox',name:'save_in_address_book',label:'Save this address for future use'}),
 element({tag:'button',text:'Estimate Shipping',attrs:{'data-bind':"click: function(){ selectAddress('TonyJones','778899') }"}})];
const rate=element({tag:'label',id:'shipping-rate-123e4567-e89b-12d3-a456-426614174000',
 text:'Ground Shipping $19.95 TonyJones 123 Elm Lane',attrs:{'data-bind':'click: selectShippingMethod'}});
const logo=element({tag:'img',attrs:{alt:'UPS logo'}});logo.alt='UPS logo';
const facility=element({tag:'dt',text:'Production Facility'});facility.nextElementSibling=element({tag:'dd',text:'Moonachie, NJ Production Facility The 4over facility that will produce the project. The facility is chosen based on the closest facility to the shipping address that is able to produce the project.'});
const root={querySelector:()=>null,querySelectorAll:selector=>{
 if(selector.startsWith('input,'))return controls;
 if(selector.startsWith('a,'))return controls.filter(e=>e.tagName==='BUTTON');
 if(selector.startsWith('h1,'))return [element({tag:'h2',text:'Shipping'}),element({tag:'h2',text:'Ship to TonyJones'})];
 if(selector.startsWith('button,label'))return [rate];
 if(selector==='img[alt]')return [logo];
 if(selector==='span,strong,b,dt,dd,div,p,li')return [facility];
 return [];
}};
const script={src:'https://4over.com/static/version1792805087/frontend/FourOver/default/en_US/FourOver_Project/js/view/project/shipment.js?token=privateToken'};
const document={querySelector:()=>root,querySelectorAll:()=>[script]};
Object.defineProperty(document,'cookie',{get:()=>{throw Error('Observer read cookies');}});
const context={document,location:{href:'https://4over.com/real-estate-post'},URL,CSS:{escape:String},
 performance:{getEntriesByType:()=>[{name:'https://4over.com/customer/address/778899'}]}};
const result=vm.runInNewContext('('+observer+')',context)(['Ground Shipping','2nd Day Air A.M.','TonyJones Ground']);
process.stdout.write(JSON.stringify(result));
'''.replace('OBSERVER',json.dumps(SHIPPING_CONTROL_OBSERVATION_JS))
    result=json.loads(subprocess.check_output([node,'-e',harness],text=True))
    raw=json.dumps(result)
    for private in ['TonyJones','alice@example.org','5551234567','123 Elm Lane','778899','privateToken','123e4567-e89b-12d3-a456-426614174000']:
        assert private not in raw
    assert result['controls'][0]['value']=='85746'
    assert result['controls'][1]['value']=='AZ'
    assert result['controls'][2]['id']=='address-[number]' and 'value' not in result['controls'][2]
    assert any(item['label']=='Save this address for future use' for item in result['controls'])
    assert result['rate_cards'][0]['service_labels']==['Ground Shipping']
    assert result['rate_cards'][0]['amounts']==['$19.95']
    assert result['carrier_logos'][0]['alt']=='UPS logo'
    assert result['production_facilities']==[{'label':'Production Facility','city':'Moonachie','state':'NJ'}]
    assert len(result['public_js_assets'])==1 and '?' not in result['public_js_assets'][0]
    assert result['limits']['mutations_performed'] is False


def test_shipping_wait_reconciles_hydrated_options_before_view_capture():
    responses=[{'status':200,'rates':[{'method_code':'03','amount':19.95,'available':True},
                                     {'method_code':'12','amount':29.95,'available':True}]}]
    class Page:
        calls=0
        async def evaluate(self,_):
            self.calls+=1
            return [{'visible':False,'choices':[] if self.calls==1 else [
                {'method_code':'03','cents':1995},{'method_code':'12','cents':2995}]}]
    page=Page();snapshot=asyncio.run(wait_for_shipping_hydration(page,responses,timeout=.3))
    assert page.calls==2 and snapshot['status']=='matched'
    assert snapshot['native_count']==2 and snapshot['visible_selector_count']==0


def test_shipping_wait_does_not_accept_stale_rendered_money():
    class Page:
        async def evaluate(self,_):return [{'visible':True,'choices':[{'method_code':'03','cents':1500}]}]
    response=[{'status':200,'rates':[{'method_code':'03','amount':19.95,'available':True}]}]
    snapshot=asyncio.run(wait_for_shipping_hydration(Page(),response,timeout=.01))
    assert snapshot['status']=='not_hydrated_before_deadline'
    assert snapshot['native_count']==1


def test_passive_pilot_projection_keeps_service_rates_but_not_legacy_address_inputs():
    pilot={'product_id':'6669','source_content':{},'shipping_view':{'money':['Total $100.00'],
        'fields':[{'name':'address_id','value':'private-address-id'}]},
        'shipping_response_shapes':[{'status':200,'rates':[{'method_code':'03','method_title':'Ground Shipping',
            'amount':19.95,'available':True,'carrier_title':'Observed Carrier','private_address':'private-address'}]}],
        'shipping_control_observation':{'schema':'fourover-shipping-controls/v1','controls':[]}}
    projected=passive_pilot_projection(pilot)
    assert observation_labels(pilot['shipping_response_shapes'])==['Ground Shipping']
    assert 'private-address' not in json.dumps(projected)
    assert projected['shipping_rates'][0]['rates'][0]['carrier_title']=='Observed Carrier'
    assert 'carrier_name' not in projected['shipping_rates'][0]['rates'][0]


def test_inspection_request_is_strict_and_cached_inspection_fails_without_source_call(monkeypatch):
    import crawler.app as module
    Request=module.FourOverRealEstatePostExactQuoteRequest
    body={'size':'18x24','stock':'pvc_3mm','rounded_corners':False,'sets':1}
    assert Request(**body).observe_shipping_controls is False
    with pytest.raises(ValidationError):Request(**body,observe_shipping_controls='true')
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.fourover_real_estate_post_exact_quote('unused','6669',
            Request(**body,require_fresh=False,observe_shipping_controls=True)))
    assert error.value.status_code==422


def test_direct_pilot_cannot_bypass_guarded_inspection_entry(monkeypatch):
    import crawler.app as module
    monkeypatch.setattr(module.app.state,'campaigns',SimpleNamespace(get=lambda _: {
        'products':[{'id':'6669','supplier':'fourover','supplier_url':'https://4over.com/real-estate-post'}]}),raising=False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.fourover_shipping_pilot('campaign','6669',observe_shipping_controls=True))
    assert error.value.status_code==422 and 'guarded' in error.value.detail


def test_benchmark_request_accepts_any_valid_zip_but_requires_guarded_pair():
    from crawler.app import FourOverRealEstatePostExactQuoteRequest as Request
    body={'size':'18x24','stock':'pvc_3mm','rounded_corners':False,'sets':1}
    valid=Request(**body,observe_shipping_controls=True,benchmark_destination_zip='12847',benchmark_destination_state='NY')
    assert valid.benchmark_destination_zip=='12847'
    for extra in [{'benchmark_destination_zip':'10001'},
                  {'observe_shipping_controls':True,'benchmark_destination_zip':'10001','benchmark_destination_state':'ny'},
                  {'benchmark_destination_zip':'10001','benchmark_destination_state':'NY'},
                  {'observe_shipping_controls':True,'benchmark_destination_zip':'10001','benchmark_destination_state':'NY','require_fresh':False}]:
        with pytest.raises(ValidationError):Request(**body,**extra)


def test_existing_benchmark_selection_requires_apply_and_never_creates_address():
    class Locator:
        def __init__(self,page,kind,rows=None,pattern=None):self.page=page;self.kind=kind;self.rows=rows;self.pattern=pattern
        @property
        def first(self):return self
        def filter(self,has_text=None,has=None):
            if self.kind=='cards':
                rows=self.rows
                if has_text:rows=[row for row in rows if has_text.search(row['label'])]
                if has:rows=[row for row in rows if has.pattern.search(row['postcode'])]
                return Locator(self.page,self.kind,rows)
            if self.kind=='postal':return Locator(self.page,self.kind,pattern=has_text)
            return self
        async def count(self):return len(self.rows) if self.rows is not None else 1
        async def wait_for(self,**_):
            if self.rows==[]:raise TimeoutError()
        def locator(self,_):return Locator(self.page,'select_existing',self.rows)
        def get_by_role(self,role,name):
            assert role=='button' and name.search('SHIP HERE')
            return Locator(self.page,'apply_existing')
        async def get_attribute(self,_):return ''
        async def click(self,**_):
            self.page.actions.append(self.kind)
            if self.kind=='select_existing':self.page.selected=self.rows[0]
            if self.kind=='apply_existing':self.page.destination={'status':'bound_postal_and_state',
                'postcode':self.page.selected['postcode'],'state':self.page.selected['state']}
    class Page:
        def __init__(self,rows):
            self.rows=rows;self.actions=[];self.destination={'status':'bound_postal_and_state','postcode':'85296-5045','state':'AZ'}
        async def evaluate(self,_):return self.destination
        def locator(self,selector):
            if selector.startswith('.ship-from-address a.'):return Locator(self,'lookup')
            if selector.startswith('[data-bind*="postcode"]'):return Locator(self,'postal')
            if selector=='li.project-address-item:visible':return Locator(self,'cards',self.rows)
            if selector.startswith('.modal-popup'):return Locator(self,'dialog')
            if selector=='li.project-address-item':return Locator(self,'all_cards')
            raise AssertionError(selector)
    rows=[{'label':'Private Customer','postcode':'10001-7800','state':'NY'},
          {'label':'Hermes Shipping Test','postcode':'10001-7800','state':'NY'}]
    page=Page(rows)
    result=asyncio.run(select_existing_benchmark_destination(page,'10001','NY'))
    assert page.actions==['lookup','select_existing','apply_existing']
    assert result['postcode']=='10001-7800' and result['state']=='NY'
    assert result['address_record_written'] is False
    assert 'Private Customer' not in json.dumps(result)
    page=Page(rows[:1])
    with pytest.raises(ValueError,match='benchmark_destination_not_retained'):
        asyncio.run(select_existing_benchmark_destination(page,'10001','NY',timeout=.1))
    assert page.actions==['lookup']
    page=Page(rows)
    result=asyncio.run(select_existing_benchmark_destination(page,'85296','AZ'))
    assert result['selection']=='current_destination_retained' and page.actions==[]


def test_observation_guard_blocks_account_address_cart_and_order_writes_only():
    for path in ['/rest/default/V1/project/mine/address','/customer/address/formPost/',
                 '/checkout/cart/add/','/sales/order/create/']:
        assert forbidden_observation_write('POST',path) is True
    assert forbidden_observation_write('POST','/rest/default/V1/project/mine/addresses') is False
    assert forbidden_observation_write('POST','/print/shipment/shippingRates/') is False
    assert forbidden_observation_write('POST','/print/project/summary/') is False


def test_shipping_export_uses_final_response_and_rejects_stale_hydration_binding():
    from crawler.fourover_shipping_observation import current_shipping_rates
    pilot={'shipping_response_shapes':[
        {'status':200,'rates':[{'available':True,'method_code':'03','amount':15.00}]},
        {'status':200,'rates':[{'available':True,'method_code':'03','amount':20.00}]}],
        'shipping_control_observation':{
            'rate_hydration':{'status':'matched','choices':[{'method_code':'03','cents':2000}]},
            'current_destination':{'status':'bound_postal_and_state','postcode':'33131-2100','state':'FL'},
            'benchmark_destination':{'status':'bound_postal_and_state','postcode':'33131-2100','state':'FL'}}}
    result=current_shipping_rates(pilot)
    assert len(result)==1 and result[0]['rates'][0]['amount']==20.00
    assert result[0]['binding']['status']=='native_choices_and_destination_retained'
    pilot['shipping_control_observation']['rate_hydration']['choices'][0]['cents']=1500
    assert current_shipping_rates(pilot)[0]['binding']['status']=='unknown_not_reconciled'
    pilot['shipping_control_observation']['rate_hydration']['choices'][0]['cents']=2000
    pilot['shipping_control_observation']['current_destination']['state']='AZ'
    assert current_shipping_rates(pilot)[0]['binding']['status']=='unknown_not_reconciled'
    pilot['shipping_response_shapes'].append({'status':503,'rates':[]})
    result=current_shipping_rates(pilot)
    assert result[0]['rates']==[] and result[0]['binding']['status']=='unknown_not_reconciled'


def test_native_ground_selection_uses_dom_option_and_requires_bound_summary_shipping():
    from crawler.fourover_shipping_observation import (select_native_ground_shipping,CURRENT_DESTINATION_JS,
        NATIVE_GROUND_OPTIONS_JS,SHIPPING_RATE_CHOICES_JS)
    from crawler.fourover_real_estate import selection,requested_project_context
    chosen=selection('18x24','pvc_3mm',False,1)
    class Select:
        def __init__(self,page):self.page=page
        async def select_option(self,value,**_):
            assert value=='mapi_03';self.page.actions.append(value);self.page.value=value
            if self.page.refresh:
                self.page.summaries.append({'status':200,'selection_context':requested_project_context(chosen),
                    'totals':{'subtotal':100.00,'shipping':20.00,'tax':12.34,'grand_total':221.29}})
    class Page:
        def __init__(self,refresh=True,changed=False):
            self.value='';self.actions=[];self.refresh=refresh;self.changed=changed
            self.summaries=[{'status':200,'selection_context':requested_project_context(chosen),
                             'totals':{'subtotal':100.00,'tax':7.00,'grand_total':107.00}}]
        async def evaluate(self,script):
            if script==CURRENT_DESTINATION_JS:
                return {'status':'bound_postal_and_state','postcode':'10001' if self.changed and self.value else '33131-2100','state':'FL'}
            if script==NATIVE_GROUND_OPTIONS_JS:
                return [{'visible':True,'value':self.value,'choices':[{'value':'mapi_03','label':'Ground Shipping $20.00'},
                    {'value':'mapi_12','label':'3 Day Shipping Service $86.00'}]}]
            if script==SHIPPING_RATE_CHOICES_JS:
                return [{'visible':True,'choices':[{'method_code':'03','cents':2000},{'method_code':'12','cents':8600}]}]
            return ['Total $221.29' if self.value and self.refresh else 'Total $107.00']
        def locator(self,selector):
            assert selector=='select[data-bind*="shipment.shippingMethod"]';return Select(self)
    responses=[{'status':200,'rates':[{'available':True,'method_code':'03','amount':20.00},
                                    {'available':True,'method_code':'12','amount':86.00}]}]
    page=Page();result=asyncio.run(select_native_ground_shipping(page,responses,page.summaries,chosen,timeout=.1))
    assert page.actions==['mapi_03'] and result['amount']=='20.00'
    assert result['status']=='native_ground_selected_summary_and_total_matched'
    assert result['postcode']=='33131-2100' and result['address_record_written'] is False
    assert result['carrier_identity']=='source_fields_only_not_inferred_from_method_code'
    page=Page(refresh=False)
    with pytest.raises(ValueError,match='summary_not_ready'):
        asyncio.run(select_native_ground_shipping(page,responses,page.summaries,chosen,timeout=.01))
    page=Page(changed=True)
    with pytest.raises(ValueError,match='destination_changed'):
        asyncio.run(select_native_ground_shipping(page,responses,page.summaries,chosen,timeout=.1))


def test_ground_request_requires_strict_fresh_guarded_observation():
    from crawler.app import FourOverRealEstatePostExactQuoteRequest as Request
    body={'size':'18x24','stock':'pvc_3mm','rounded_corners':False,'sets':1}
    assert Request(**body,observe_shipping_controls=True,select_ground_shipping=True).select_ground_shipping is True
    for changes in [{'select_ground_shipping':True},
                    {'select_ground_shipping':'true','observe_shipping_controls':True},
                    {'select_ground_shipping':True,'observe_shipping_controls':True,'require_fresh':False}]:
        with pytest.raises(ValidationError):Request(**body,**changes)
