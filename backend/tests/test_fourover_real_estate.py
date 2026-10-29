"""Synthetic monetary fixtures; no current supplier prices or account records."""
import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from crawler.fourover_real_estate import (SOURCE, PRODUCT, SIZE_VALUES, STOCK_VALUES, selection,
                                         requested_project_context, project_context, summarize, RealEstateQuoteArchive,
                                         wait_for_rendered_quote, enter_set_count, QUOTE_DEADLINE)


def pilot(chosen=None, amount='100.00'):
    chosen=chosen or selection('18x24','pvc_3mm',False,1)
    expected=requested_project_context(chosen)
    fields=[]
    choices={'super_attribute[211]':('Size *','18" x 24"' if chosen['size']=='18x24' else '24" x 30"'),
             'super_attribute[197]':('Stock *','3mm PVC' if chosen['stock']=='pvc_3mm' else 'Aluminum Sandwich Board 3mm - Double-sided White'),
             'super_attribute[204]':('Colorspec *','4/4 (4 color both sides)'),
             'super_attribute[216]':('Drill Hole *','-' if chosen['stock']=='pvc_3mm' else '3/8" Drill hole Real Estate Post'),
             'super_attribute[229]':('Grommets *','Real Estate Post Grommets' if chosen['stock']=='pvc_3mm' else '-'),
             'options[892]':('Hardware','White 6ft Post (Gothic Cap) +$80.00'),
             'options[893]':('1” Round Corners','Yes' if chosen['rounded_corners'] else 'No')}
    for name,value in expected.items():
        if name.startswith('product_options[') and name!='product_options[turnaround_time]':
            source_name=name.replace('product_options[super_attribute]','super_attribute',1).replace('product_options[options]','options',1)
            fields.append({'name':source_name,'label':choices[source_name][0],'selected_label':choices[source_name][1],
                           'selected':value,'disabled':False})
    applied_options=[{'label':field['label'].replace('*','').strip(),'value':field['selected_label']} for field in fields]
    applied_options.append({'label':'Turnaround Time','value':'4 Business Days'})
    return {'product_id':PRODUCT,'selected_child_id':chosen['child_id'],'sets':chosen['sets'],'width':None,'height':None,
            'set_control':{'value':str(chosen['sets']),'min':'1','max':'200','valid':True},
            'selected_services':{'job_sample':False,'digital_proof':False},'source_selection':fields,
            'service_controls':{name:{'present':False,'ambiguous':False,'checked':False} for name in ('job_sample','digital_proof')},
            'turnaround_control':{'value':'4','label':'4 Business Days','retained':True},
            'project_response_shapes':[{'status':200,'selection_context':expected,
                                        'totals':{'subtotal':amount,'grand_total':amount},
                                        'product_options':applied_options}],
            'shipping_view':{'money':['Price Per Set: $0.00',f'Subtotal ${amount} Total: ${amount} Taxes may apply'],
                             'fields':[{'name':'private-address','value':'do-not-archive'}]},
            'shipping_response_shapes':[],
            'source_content':{'details_text':'Bleed: 0.5 inch','template_urls':['https://4over.com/media/asset/templates/post.pdf']}}


def test_exact_complete_package_preserves_source_total_without_shipping_gate():
    chosen=selection('18x24','pvc_3mm',False,1)
    result=summarize(pilot(chosen),chosen)
    assert result['supplier_total_excluding_shipping']=='100.00'
    assert result['hardware']=='white_6ft_gothic_post'
    assert result['displayed_per_set_status']=='missing_or_placeholder'
    assert result['production_detail_status']=='missing_at_supplier'
    assert result['source_content']['details_text']=='Bleed: 0.5 inch'


def test_rounded_200_sets_price_is_native_not_one_set_multiplication():
    chosen=selection('24x30','aluminum_sandwich_3mm',True,200)
    value=pilot(chosen,'28000.00')
    value['project_response_shapes'][0]['totals']={'subtotal':'27990.00','small_qty_fee':'10.00','grand_total':'28000.00'}
    result=summarize(value,chosen)
    assert result['rounded_corners'] is True and result['sets']==200
    assert result['supplier_total_excluding_shipping']=='28000.00'
    assert result['small_quantity_fee']=='10.00'
    assert requested_project_context(chosen)['product_options[super_attribute][216]']=='1030'
    assert requested_project_context(chosen)['product_options[super_attribute][229]']=='608'


def test_aluminum_rejects_pvc_finishing_context():
    chosen=selection('18x24','aluminum_sandwich_3mm',False,1);value=pilot(chosen,'110.00')
    value['project_response_shapes'][0]['selection_context']['product_options[super_attribute][216]']='230'
    with pytest.raises(ValueError,match='summary_not_bound'):
        summarize(value,chosen)


def test_unrelated_production_estimate_and_late_service_reset_are_not_accepted():
    chosen=selection('18x24','pvc_3mm',False,1);value=pilot(chosen)
    value['shipping_response_shapes']=[{'status':200,'selection_context':{},'rates':[{'production_estimate':'2099-01-01'}]}]
    result=summarize(value,chosen)
    assert result['production_estimates']==[] and result['production_detail_status']=='unknown_unbound'
    value['service_controls']['job_sample']={'present':True,'ambiguous':False,'checked':True}
    with pytest.raises(ValueError,match='service_state_unverified'):
        summarize(value,chosen)


@pytest.mark.parametrize('change',[
    lambda p:p['source_selection'][-1].update(selected='1080'),
    lambda p:p['source_selection'][-2].update(selected=''),
    lambda p:p['set_control'].update(value='200'),
    lambda p:p['set_control'].update(valid=False),
    lambda p:p['turnaround_control'].update(value='1'),
    lambda p:p['project_response_shapes'][0]['selection_context'].update(shipment_job_counts={'0':2}),
    lambda p:p['project_response_shapes'][0]['selection_context'].update(**{'product_options[options][892]':''}),
    lambda p:p['project_response_shapes'][0]['totals'].update(grand_total='13.25'),
    lambda p:p['project_response_shapes'][0]['product_options'][-3].update(value='No'),
    lambda p:p['shipping_view'].update(money=['Total: $13.25']),
    lambda p:p.update(selected_child_id='83539'),
])
def test_wrong_custom_options_stale_totals_and_set_retention_fail_closed(change):
    chosen=selection('18x24','pvc_3mm',False,1);value=pilot(chosen);change(value)
    with pytest.raises(ValueError):summarize(value,chosen)


def test_project_context_counts_sets_without_saving_uuid_or_address_values():
    chosen=selection('18x24','pvc_3mm',False,3)
    data={key:value for key,value in requested_project_context(chosen).items() if key not in ('shipment_job_counts','contiguous_job_slots')}
    for index in range(3):data[f'shipments[0][jobs][{index}][uuid]']='private-uuid-'+str(index)
    data['shipments[0][shipping_address][street][]']='private street'
    projected=project_context(data)
    assert projected==requested_project_context(chosen)
    assert 'private' not in json.dumps(projected)
    data.pop('shipments[0][jobs][1][uuid]')
    assert project_context(data)['contiguous_job_slots'] is False


def test_archive_keeps_exact_scope_and_conflict_without_private_fields(tmp_path):
    chosen=selection('18x24','pvc_3mm',False,1);value=pilot(chosen)
    archive=RealEstateQuoteArchive(tmp_path);quote=summarize(value,chosen)
    archive.save('campaign',chosen,quote,value)
    saved=archive.get('campaign',chosen)
    assert saved['quote']['supplier_total_excluding_shipping']=='100.00'
    assert 'do-not-archive' not in json.dumps(saved)
    assert archive.get('campaign',selection('18x24','pvc_3mm',True,1)) is None
    conflict_id=archive.save_conflict('campaign',chosen,value,'unverified')
    conflict=json.loads((Path(tmp_path)/'real-estate-post-exact-quotes/campaign/conflicts'/f'{conflict_id}.json').read_text())
    assert conflict['status']=='unverified_source_observation_no_price_completion'
    assert 'do-not-archive' not in json.dumps(conflict)


def test_actual_native_summary_omits_only_inapplicable_drill_and_uses_total_without_colon(tmp_path):
    record=json.loads((Path(__file__).parent/'fixtures/fourover_real_estate_native.json').read_text())
    quote=summarize(record['evidence'],record['selection'])
    assert quote['supplier_total_excluding_shipping']=='100.00'
    assert quote['rounded_corners'] is True
    assert quote['displayed_per_set_status']=='matched'
    assert quote['production_detail_status']=='unknown_not_captured'
    assert quote['source_content']['text_capture_status']=='legacy_unfiltered_dom_text_not_approved_copy'
    assert quote['source_content']['gallery_capture_status']=='dom_only_not_complete_gallery_evidence'
    archive=RealEstateQuoteArchive(tmp_path)
    folder=archive.path('campaign',record['selection']).parent/'conflicts'
    folder.mkdir()
    path=folder/(record['id']+'.json');path.write_text(json.dumps(record))
    promoted=archive.promote_conflict('campaign',record['id'])
    assert promoted['captured_at']==record['captured_at']
    assert promoted['promoted_from_conflict_id']==record['id']
    assert json.loads(path.read_text())==record
    # Hardware and actual finishes cannot inherit the no-action exception.
    for label in ('Hardware','Grommets'):
        invalid=copy.deepcopy(record['evidence'])
        for response in invalid['project_response_shapes']:
            response['product_options']=[item for item in response['product_options'] if item['label']!=label]
        with pytest.raises(ValueError,match='server_applied_options_missing'):
            summarize(invalid,record['selection'])


def test_aluminum_summary_may_omit_only_its_no_grommet_sentinel():
    chosen=selection('18x24','aluminum_sandwich_3mm',False,1)
    value=pilot(chosen,'110.00')
    value['project_response_shapes'][0]['product_options']=[item for item in value['project_response_shapes'][0]['product_options']
                                                           if item['label']!='Grommets']
    assert summarize(value,chosen)['supplier_total_excluding_shipping']=='110.00'
    value['project_response_shapes'][0]['product_options']=[item for item in value['project_response_shapes'][0]['product_options']
                                                           if item['label']!='Drill Hole']
    with pytest.raises(ValueError,match='server_applied_options_missing'):
        summarize(value,chosen)


def test_archive_preserves_only_safe_production_fields_and_rejects_naive_source_time(tmp_path):
    chosen=selection('18x24','pvc_3mm',False,1);value=pilot(chosen)
    value['shipping_response_shapes']=[{'status':200,'selection_context':project_context({'job_number':'private-job',
            'shipment_uuid':'private-shipment'}), 'rates':[{'production_estimate':'2026-11-01','address':'private-address'}]}]
    projected=RealEstateQuoteArchive.evidence(value)
    assert 'private' not in json.dumps(projected)
    result=summarize(projected,chosen)
    assert result['production_detail_status']=='unknown_unbound' and result['production_estimates']==[]
    with pytest.raises(ValueError,match='timestamp_must_have_timezone'):
        RealEstateQuoteArchive(tmp_path).save('campaign',chosen,result,captured_at='2026-10-29T05:42:57')


def test_native_total_wait_requires_bound_server_money_and_has_short_budget():
    chosen=selection('18x24','pvc_3mm',False,1)
    class Page:
        async def evaluate(self,_):return ['Subtotal $100.00 Total $100.00 Taxes may apply']
    responses=pilot(chosen)['project_response_shapes']
    assert asyncio.run(wait_for_rendered_quote(Page(),responses,chosen,timeout=.01)) is True
    responses[0]['selection_context']['shipment_job_counts']={'0':2}
    assert asyncio.run(wait_for_rendered_quote(Page(),responses,chosen,timeout=.01)) is False
    responses=pilot(chosen)['project_response_shapes'];responses[0]['totals']['grand_total']='13.25'
    assert asyncio.run(wait_for_rendered_quote(Page(),responses,chosen,timeout=.01)) is False


def test_saved_q2_observation_cannot_be_promoted_from_retained_dom_count(tmp_path):
    record=json.loads((Path(__file__).parent/'fixtures/fourover_real_estate_sets/failed-q2-observation.json').read_text())
    assert record['evidence']['set_control']['value']=='2'
    assert record['evidence']['project_response_shapes'][-1]['selection_context']['shipment_job_counts']=={'0':1}
    with pytest.raises(ValueError,match='summary_not_bound_to_requested_options_and_sets'):
        summarize(record['evidence'],record['selection'])
    archive=RealEstateQuoteArchive(tmp_path)
    folder=archive.path('campaign',record['selection']).parent/'conflicts';folder.mkdir()
    (folder/(record['id']+'.json')).write_text(json.dumps(record))
    with pytest.raises(ValueError,match='summary_not_bound'):
        archive.promote_conflict('campaign',record['id'])
    assert archive.get('campaign',record['selection']) is None


def test_readiness_uses_remaining_shared_deadline_and_leaves_cleanup_time():
    chosen=selection('18x24','pvc_3mm',False,2)
    class Page:
        async def evaluate(self,_):return ['Price Per Set: $100.00']
    async def bounded():
        token=QUOTE_DEADLINE.set(time.monotonic()+2.02)
        started=time.monotonic()
        try:
            assert await wait_for_rendered_quote(Page(),[],chosen) is False
            assert time.monotonic()-started<.2
        finally:QUOTE_DEADLINE.reset(token)
    asyncio.run(bounded())


def test_normal_keyboard_entry_reaches_keyup_bound_model_in_local_browser():
    playwright=pytest.importorskip('playwright.async_api')
    async def scenario():
        async with playwright.async_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                # Synthetic event regression, not a claim about the supplier's
                # still-unverified exact Sets component implementation.
                await page.set_content('<input type="number" min="1" max="200" value="1"><div id="total"></div>')
                await page.evaluate('''() => {
                  window.modelSets=1;const e=document.querySelector('input');
                  e.addEventListener('keyup',()=>{window.modelSets=Number(e.value);});
                }''')
                control=page.locator('input')
                await control.fill('2');await control.press('Tab')
                assert await control.input_value()=='2' and await page.evaluate('modelSets')==1
                await enter_set_count(control,3)
                assert await control.input_value()=='3' and await page.evaluate('modelSets')==3
            finally:await browser.close()
    asyncio.run(scenario())


def test_request_is_strict_and_product_specific():
    from crawler.app import FourOverRealEstatePostExactQuoteRequest as Request
    base={'size':'18x24','stock':'pvc_3mm','rounded_corners':False,'sets':1}
    assert Request(**base).require_fresh is True
    for change in [{'sets':0},{'sets':201},{'sets':True},{'sets':'1'},{'rounded_corners':'false'},
                   {'hardware':'sign_only'},{'turnaround':'rush'},{'size':'custom'},{'unexpected':1}]:
        with pytest.raises(ValidationError):Request(**(base|change))


def test_endpoint_uses_queued_mapping_and_complete_custom_scope(monkeypatch,tmp_path):
    import crawler.app as module
    from crawler.app import FourOverRealEstatePostExactQuoteRequest as Request
    chosen=selection('18x24','pvc_3mm',True,2)
    product={'id':'6669','supplier':'fourover','supplier_url':SOURCE}
    campaign={'products':[product],'active_group_product_ids':['6669']}
    campaigns=SimpleNamespace(get=lambda _:campaign,require_active_group=lambda *_:campaign)
    monkeypatch.setattr(module.app.state,'campaigns',campaigns,raising=False)
    config=SimpleNamespace()
    monkeypatch.setattr(module,'store',SimpleNamespace(root=tmp_path,profiles=lambda _:[],config=config))
    import crawler.pacing as pacing
    monkeypatch.setattr(pacing,'source_pacing',lambda *_:(1,1))
    import crawler.transport as transport
    class Limiter:
        def __init__(self,_):pass
        async def acquire(self,origin,rate,concurrency,check):await check();return 'token'
        def release(self,*_):pass
    monkeypatch.setattr(transport,'RateLimiter',Limiter)
    calls=[]
    async def fake_pilot(campaign_id,product_id,**kwargs):
        calls.append(kwargs)
        result=pilot(chosen,'250.00')
        if kwargs.get('observe_shipping_controls'):
            result['shipping_control_observation']={'schema':'fourover-shipping-controls/v1','controls':[]}
            result['shipping_rates']=[{'status':200,'rates':[{'method_code':'03','method_title':'Ground Shipping','amount':19.95}]}]
        return result
    monkeypatch.setattr(module,'fourover_shipping_pilot',fake_pilot)
    result=asyncio.run(module.fourover_real_estate_post_exact_quote('campaign','6669',Request(size='18x24',stock='pvc_3mm',rounded_corners=True,sets=2)))
    assert result['supplier_total_excluding_shipping']=='250.00' and result['cache']['price_fresh'] is True
    assert calls[0]['source_hardware_value']=='1078' and calls[0]['source_round_corners_value']=='1080'
    assert calls[0]['turnaround_time']==4 and calls[0]['sets']==2
    cached=asyncio.run(module.fourover_real_estate_post_exact_quote('campaign','6669',Request(size='18x24',stock='pvc_3mm',rounded_corners=True,sets=2,require_fresh=False)))
    assert cached['cache']['price_fresh'] is False and len(calls)==1
    inspected=asyncio.run(module.fourover_real_estate_post_exact_quote('campaign','6669',Request(size='18x24',stock='pvc_3mm',rounded_corners=True,sets=2,observe_shipping_controls=True)))
    assert calls[-1]['observe_shipping_controls'] is True
    assert inspected['shipping_control_observation']['schema']=='fourover-shipping-controls/v1'
    assert inspected['shipping_rates'][0]['rates'][0]['amount']==19.95
    assert 'do-not-archive' not in json.dumps(inspected)


def test_endpoint_releases_backoff_and_fails_closed_on_supplier_429(monkeypatch,tmp_path):
    import crawler.app as module
    import crawler.pacing as pacing
    import crawler.transport as transport
    from crawler.fourover_real_estate import QUOTE_HTTP_EVENTS
    from fastapi import HTTPException
    chosen=selection('18x24','pvc_3mm',False,1)
    campaign={'products':[{'id':'6669','supplier':'fourover','supplier_url':SOURCE}], 'active_group_product_ids':['6669']}
    monkeypatch.setattr(module.app.state,'campaigns',SimpleNamespace(get=lambda _:campaign),raising=False)
    monkeypatch.setattr(module,'store',SimpleNamespace(root=tmp_path,profiles=lambda _:[],config=SimpleNamespace()))
    monkeypatch.setattr(pacing,'source_pacing',lambda *_:(1,1))
    released=[]
    class Limiter:
        def __init__(self,_):pass
        async def acquire(self,origin,rate,concurrency,check):await check();return 'token'
        def release(self,*args):released.append(args)
    monkeypatch.setattr(transport,'RateLimiter',Limiter)
    async def limited(*_,**__):
        QUOTE_HTTP_EVENTS.get().append({'status':429,'retry_after':'120'})
        return pilot(chosen)
    monkeypatch.setattr(module,'fourover_shipping_pilot',limited)
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.fourover_real_estate_post_exact_quote('campaign','6669',module.FourOverRealEstatePostExactQuoteRequest(size='18x24',stock='pvc_3mm',rounded_corners=False,sets=1)))
    assert error.value.detail['code']=='real_estate_supplier_backoff_required'
    assert released[0][3:5]==(429,120)
    assert not list((tmp_path/'real-estate-post-exact-quotes/campaign').glob('*.json'))
    async def slow(*_,**__):await asyncio.sleep(.05)
    monkeypatch.setattr(module,'fourover_shipping_pilot',slow)
    monkeypatch.setattr(module,'REAL_ESTATE_QUOTE_TIMEOUT',.001)
    with pytest.raises(HTTPException) as timeout:
        asyncio.run(module.fourover_real_estate_post_exact_quote('campaign','6669',module.FourOverRealEstatePostExactQuoteRequest(size='18x24',stock='pvc_3mm',rounded_corners=False,sets=1)))
    assert timeout.value.detail['code']=='supplier_quote_time_budget_exhausted'


def test_synthetic_bound_florida_tax_is_reconciled_but_never_becomes_supplier_goods_cost(tmp_path):
    fixture=Path(__file__).parent/'fixtures/fourover_real_estate_shipping/fl-tax-bound-observation.json'
    record=json.loads(fixture.read_text())
    quote=summarize(record['evidence'],record['selection'])
    assert quote['supplier_total_excluding_shipping']=='100.00'
    assert quote['supplier_total_excluding_shipping_and_tax']=='100.00'
    assert quote['effective_total_per_set']=='100.00'
    assert quote['source_tax']=='7.00' and quote['source_grand_total']=='107.00'
    assert quote['source_shipping_and_processing']=='0.00'
    assert quote['shipping_rates'][0]['binding']['postcode']=='33131-2100'
    assert quote['shipping_rates'][0]['binding']['state']=='FL'
    assert quote['shipping_rates'][0]['binding']['status']=='native_choices_and_destination_retained'
    assert len(quote['shipping_rates'])==1
    assert quote['shipping_rates'][0]['rates'][0]['amount']==20.00
    assert '15.00' not in json.dumps(quote['shipping_rates'])
    assert quote['shipping_control_observation']['production_facilities']==[]
    archive=RealEstateQuoteArchive(tmp_path)
    folder=archive.path('campaign',record['selection']).parent/'conflicts';folder.mkdir()
    (folder/(record['id']+'.json')).write_text(json.dumps(record))
    promoted=archive.promote_conflict('campaign',record['id'])
    assert promoted['captured_at']==record['captured_at']
    assert promoted['quote']['supplier_total_excluding_shipping']=='100.00'
    assert promoted['quote']['shipping_rates'][0]['rates'][0]['amount']==20.00


def test_explicit_source_shipping_tax_and_handling_reconcile_without_goods_contamination():
    chosen=selection('18x24','pvc_3mm',False,1);value=pilot(chosen)
    value['project_response_shapes'][0]['totals'].update(
        shipping='20.00',local_delivery_fee='2.00',tax='12.34',grand_total='134.34')
    value['shipping_view']['money']=['Price Per Set: $100.00','Total $134.34']
    quote=summarize(value,chosen)
    assert quote['supplier_total_excluding_shipping']=='100.00'
    assert quote['source_shipping_and_processing']=='20.00'
    assert quote['source_handling_fee']=='2.00' and quote['source_tax']=='12.34'
    assert quote['source_grand_total']=='134.34'
    value['project_response_shapes'][0]['totals']['grand_total']='134.35'
    value['shipping_view']['money']=['Total $134.35']
    with pytest.raises(ValueError,match='components_disagree'):summarize(value,chosen)


@pytest.mark.parametrize('change',[
    {'unexpected_fee':'0.00'}, {'job_sample':'1.00'}, {'digital_proof':'1.00'},
    {'discount':'1.00'}, {'tax':'-1.00'}, {'tax':'NaN'}, {'shipping':'Infinity'},
])
def test_source_component_extension_remains_fail_closed(change):
    chosen=selection('18x24','pvc_3mm',False,1);value=pilot(chosen)
    value['project_response_shapes'][0]['totals'].update(change)
    with pytest.raises(ValueError):summarize(value,chosen)


def test_shipping_budget_has_matching_isolated_permit_and_archives_safe_partial_failure(monkeypatch,tmp_path):
    import crawler.app as module
    import crawler.pacing as pacing
    import crawler.transport as transport
    from crawler.fourover_real_estate import QUOTE_PARTIAL_EVIDENCE
    from fastapi import HTTPException
    chosen=selection('18x24','pvc_3mm',False,1)
    product={'id':'6669','supplier':'fourover','supplier_url':SOURCE}
    campaign={'products':[product],'active_group_product_ids':['6669']}
    monkeypatch.setattr(module.app.state,'campaigns',SimpleNamespace(get=lambda _:campaign,require_active_group=lambda *_:campaign),raising=False)
    monkeypatch.setattr(module,'store',SimpleNamespace(root=tmp_path,profiles=lambda _:[],config=SimpleNamespace()))
    monkeypatch.setattr(pacing,'source_pacing',lambda *_:(1,1))
    leases=[];budgets=[]
    class Limiter:
        PERMIT_SECONDS=45
        def __init__(self,_):pass
        async def acquire(self,*args):
            await args[-1]();leases.append(self.PERMIT_SECONDS);return 'token'
        def release(self,*_):pass
    monkeypatch.setattr(transport,'RateLimiter',Limiter)
    async def fake_pilot(*_,**kwargs):
        budgets.append(QUOTE_DEADLINE.get()-time.monotonic())
        partial=QUOTE_PARTIAL_EVIDENCE.get()
        partial.update(pilot(chosen))
        if kwargs.get('select_ground_shipping'):
            raise ValueError('native_ground_selected_summary_not_ready_before_deadline')
        return pilot(chosen)
    monkeypatch.setattr(module,'fourover_shipping_pilot',fake_pilot)
    Request=module.FourOverRealEstatePostExactQuoteRequest
    base={'size':'18x24','stock':'pvc_3mm','rounded_corners':False,'sets':1}
    asyncio.run(module.fourover_real_estate_post_exact_quote('campaign','6669',Request(**base)))
    with pytest.raises(HTTPException) as error:
        asyncio.run(module.fourover_real_estate_post_exact_quote('campaign','6669',Request(**base,
            observe_shipping_controls=True,select_ground_shipping=True)))
    assert leases==[45,95] and Limiter.PERMIT_SECONDS==45
    assert 39<budgets[0]<=40 and 89<budgets[1]<=90
    evidence_id=error.value.detail['evidence_id']
    assert evidence_id
    record=json.loads((tmp_path/'real-estate-post-exact-quotes/campaign/conflicts'/f'{evidence_id}.json').read_text())
    assert record['evidence']['set_control']['value']=='1'
    assert record['evidence']['source_selection']
    assert 'do-not-archive' not in json.dumps(record)
    assert QUOTE_PARTIAL_EVIDENCE.get() is None


def test_saved_selected_shipping_total_recovers_from_native_value_span_without_requote(tmp_path):
    record=json.loads((Path(__file__).parent/'fixtures/fourover_real_estate_shipping/ny-selected-ground-li-total-observation.json').read_text())
    assert record['evidence']['shipping_view']['money']==[
        'Price Per Set: $100.00','$100.00','$20.00','$9.00','$129.00 Taxes may apply','$129.00']
    archive=RealEstateQuoteArchive(tmp_path)
    folder=archive.path('campaign',record['selection']).parent/'conflicts';folder.mkdir()
    original=folder/(record['id']+'.json');original.write_text(json.dumps(record))
    promoted=archive.promote_conflict('campaign',record['id'])
    quote=promoted['quote']
    assert quote['supplier_total_excluding_shipping']=='100.00'
    assert quote['source_shipping_and_processing']=='20.00' and quote['source_tax']=='9.00'
    assert quote['source_grand_total']=='129.00'
    assert quote['shipping_control_observation']['production_facilities']==[]
    selected=quote['shipping_control_observation']['selected_ground_service']
    assert selected['status']=='recovered_from_exact_summary_and_unique_native_ground_rate'
    assert selected['final_dom_selection_captured'] is False
    assert selected['postcode']=='10001-7800' and selected['state']=='NY'
    assert promoted['captured_at']==record['captured_at'] and json.loads(original.read_text())==record
    # Same amount under an unrelated method is insufficient for this recovery.
    wrong=copy.deepcopy(record)
    wrong['evidence']['shipping_rates'][-1]['rates'][0]['method_title']='3 Day Shipping Service'
    original.write_text(json.dumps(wrong))
    with pytest.raises(ValueError,match='offline_recovery_ambiguous'):
        archive.promote_conflict('campaign',record['id'])


def test_native_total_value_signature_excludes_bare_prices_and_ambiguous_totals():
    from crawler.fourover_real_estate import visible_totals
    assert visible_totals(['$129.00 Taxes may apply'])=={__import__('decimal').Decimal('129.00')}
    assert visible_totals(['Price Per Set: $100.00','$100.00'])==set()
    assert len(visible_totals(['Total $129.00','$107.00 Taxes may apply']))==2


def test_turnaround_dependency_is_selected_before_shipping_in_real_local_dom():
    from crawler.fourover_real_estate import select_turnaround_before_shipping
    playwright=pytest.importorskip('playwright.async_api')
    async def scenario():
        async with playwright.async_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('<main class="column main"><div id="controls"></div></main>')
                await page.evaluate('''() => setTimeout(()=>{document.querySelector('#controls').innerHTML=
                    '<input type="radio" id="four" name="turnaround_time" value="4"><label for="four">4 Business Days</label>'},100)''')
                proof=await select_turnaround_before_shipping(page,timeout=1)
                assert proof=={'value':'4','label':'4 Business Days','retained':True,
                              'observation_phase':'before_proceed_to_shipping','selection_action':'native_associated_label_clicked'}
                # A source Step2 may remove the hidden Step1 controls. Its
                # already captured proof still requires final server binding.
                await page.evaluate("document.querySelector('#controls').remove()")
                assert await page.locator('input[name="turnaround_time"]').count()==0
                value=pilot();value['turnaround_control']=proof
                assert summarize(value,selection('18x24','pvc_3mm',False,1))['turnaround']['retained'] is True
                value['project_response_shapes'][0]['selection_context']['product_options[turnaround_time]']='2'
                with pytest.raises(ValueError,match='summary_not_bound'):summarize(value,selection('18x24','pvc_3mm',False,1))
                with pytest.raises(ValueError,match='turnaround_not_ready_before_shipping'):
                    await select_turnaround_before_shipping(page,timeout=.02)
            finally:await browser.close()
    asyncio.run(scenario())


def test_readiness_diagnostic_keeps_hidden_clone_counts_without_private_project_label(tmp_path):
    from crawler.fourover_real_estate import select_turnaround_before_shipping, QUOTE_PARTIAL_EVIDENCE
    playwright=pytest.importorskip('playwright.async_api')
    async def scenario():
        async with playwright.async_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('''<main class="column main">
                  <input name="project_name" value="Private customer purchase-order label" data-bind="value: name,valueUpdate: 'keyup'">
                  <input type="radio" name="turnaround_time" value="4" id="visible"><label for="visible">4 Business Days</label>
                  <input style="display:none" type="radio" name="turnaround_time" value="4" id="hidden">
                  <input name="run_size" value="1"><input name="customer_email" value="private@example.org">
                </main>''')
                partial={};token=QUOTE_PARTIAL_EVIDENCE.set(partial)
                try:
                    with pytest.raises(ValueError,match='turnaround_not_ready_before_shipping'):
                        await select_turnaround_before_shipping(page,timeout=.02)
                finally:QUOTE_PARTIAL_EVIDENCE.reset(token)
                for phase in ('before_wait','not_ready'):
                    observed=partial['turnaround_readiness'][phase]
                    assert observed['target_count']==2 and observed['visible_target_count']==1
                    assert observed['enabled_target_count']==2
                    assert observed['project_name_binding']=={'count':1,'key_up_binding':True,'text_input_binding':False,'empty':False}
                serialized=json.dumps(RealEstateQuoteArchive.evidence(partial))
                assert 'Private customer' not in serialized and 'purchase-order label' not in serialized
                assert 'private@example.org' not in serialized and 'customer_email' not in serialized
                assert 'run_size' in serialized and 'visible_target_count' in serialized
            finally:await browser.close()
    asyncio.run(scenario())



def test_turnaround_ownership_ignores_outside_clone_but_holds_inside_owned_duplicates():
    from crawler.fourover_real_estate import select_turnaround_before_shipping
    playwright=pytest.importorskip('playwright.async_api')
    async def scenario():
        async with playwright.async_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('''<div style="display:none"><input type="radio" name="turnaround_time" value="4" id="outside"><label for="outside">unrelated hidden clone</label></div>
                    <main class="column main"><input type="radio" name="turnaround_time" value="4" id="owned"><label for="owned">4 Business Days</label></main>''')
                assert await page.locator('input[name="turnaround_time"][value="4"]').count()==2
                proof=await select_turnaround_before_shipping(page,timeout=.3)
                assert proof['retained'] is True and proof['label']=='4 Business Days'
                await page.evaluate('''() => document.querySelector('main').insertAdjacentHTML('beforeend',
                    '<input style="display:none" type="radio" name="turnaround_time" value="4" id="owned-clone">')''')
                with pytest.raises(ValueError,match='turnaround_not_ready_before_shipping'):
                    await select_turnaround_before_shipping(page,timeout=.02)
            finally:await browser.close()
    asyncio.run(scenario())


def test_native_quantity_keyboard_blur_initializes_price_and_turnaround_dependency():
    from crawler.fourover_real_estate import commit_quantity_one_before_turnaround,select_turnaround_before_shipping,QUOTE_PARTIAL_EVIDENCE
    playwright=pytest.importorskip('playwright.async_api')
    async def scenario():
        async with playwright.async_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                html='''<main class="column main"><input type="hidden" name="qty" value="1">
                  <ul><li class="runsizes_row custom_runsize_row"><input type="number" id="qty-box-qty" min="1" value="1">
                  <span class="custom-run-size-unitprice">$0.00</span><span class="custom-run-size-totalprice">$0.00</span>
                  </li></ul><div id="production"></div></main>'''
                await page.set_content(html)
                await page.evaluate('''() => document.querySelector('#qty-box-qty').addEventListener('blur',()=>{
                  setTimeout(()=>{document.querySelector('.custom-run-size-unitprice').textContent='$21.00';
                    document.querySelector('.custom-run-size-totalprice').textContent='$21.00';
                    document.querySelector('#production').innerHTML='<input type="radio" name="turnaround_time" value="4" id="tat-4"><label for="tat-4">4 Business Days</label>'},100);
                })''')
                assert await page.locator('input[name="qty"]').input_value()=='1'
                assert await page.locator('input[name="turnaround_time"]').count()==0
                partial={};token=QUOTE_PARTIAL_EVIDENCE.set(partial)
                try:
                    receipt=await commit_quantity_one_before_turnaround(page,timeout=1)
                    turn=await select_turnaround_before_shipping(page,timeout=.3)
                finally:QUOTE_PARTIAL_EVIDENCE.reset(token)
                assert receipt['unit_price']=='21.0000' and receipt['row_total']=='21.00'
                assert receipt['control']['value']=='1' and receipt['control']['valid'] is True
                assert receipt['price_scope']=='native_print_row_only_not_complete_hardware_package'
                assert turn['value']=='4' and turn['retained'] is True
                assert partial['run_size_readiness']['before_commit']['unit_price_texts']==['$0.00']
                assert partial['run_size_readiness']['price_ready']['unit_price']=='21.0000'
                await page.set_content(html)
                with pytest.raises(ValueError,match='quantity_row_price_not_ready'):
                    await commit_quantity_one_before_turnaround(page,timeout=.05)
            finally:await browser.close()
    asyncio.run(scenario())


def test_actual_public_quantity_modules_prove_normalization_and_price_dependencies():
    import hashlib,shutil,subprocess
    node=shutil.which('node')
    if not node:pytest.skip('Node absent')
    folder=Path(__file__).parent/'fixtures/fourover_quantity_readiness'
    manifest=json.loads((folder/'manifest.json').read_text());sources={}
    for item in manifest['files']:
        raw=(folder/item['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==item['sha256']
        sources[item['url'].split('/en_US/',1)[1]]=raw.decode()
    code=r'''
const vm=require('vm'),assert=require('assert'),sources=SOURCES;
const ko={observable:value=>{const f=(...args)=>args.length?(value=args[0]):value;f.subscribe=()=>{};return f;},
 pureComputed:(f,ctx)=>()=>f.call(ctx)};
const _={filter:(rows,f)=>rows.filter(f),sortBy:(rows,k)=>rows.slice().sort((a,b)=>a[k]-b[k]),last:rows=>rows.at(-1),
 extend:Object.assign,isNaN:Number.isNaN};
const modules={'ko':ko,'underscore':_};
const load=path=>{let result;vm.runInNewContext(sources[path],{define:(deps,f)=>result=f(...deps.map(d=>modules[d]))});return result;};
const qty=load('FourOver_Project/js/model/selected-runsize.js');assert.strictEqual(qty(),1);
modules['FourOver_Project/js/model/selected-runsize']=qty;
modules['FourOver_Project/js/model/product-tier-prices']=ko.observable([]);
const unit=ko.observable(undefined);modules['FourOver_Project/js/model/product-unit-price']=unit;
const fixed=load('FourOver_ProductFixedSize/js/model/price-processor.js');
modules['FourOver_ProductFixedSize/js/model/price-processor']=fixed;
let calculator=null,registrations=0;
const processor={getUnitPrice:q=>calculator?calculator(q):0,getItemTotalPrice:q=>calculator?calculator(q)*q:0,
 setProductPriceCalculator:c=>{calculator=c;registrations++;}};
modules['FourOver_Project/js/model/product-price-processor']=processor;
modules.uiComponent={extend:value=>value};
modules['FourOver_ProductStepConfig/js/model/config']={isMarkupEnabled:()=>false};
modules.priceUtils={formatPrice:(value,options)=>Number(value).toFixed(options.requiredPrecision)};
const box=load('FourOver_ProductFixedSize/js/view/product/qty-box.js');
const instance=Object.assign({},box.defaults,box,{priceFormat:{},_super(){},observe(names){for(const key of names)this[key]=ko.observable(this[key]);}});
instance.initObservable();assert.strictEqual(instance.isVisible(),false);
load('FourOver_ProductFixedSize/js/view/register-price-processor.js');assert.strictEqual(registrations,1);
assert.strictEqual(instance.isVisible(),false); // registered calculator still needs supplier prices
qty('');instance.correctQty();assert.strictEqual(qty(),1);assert.strictEqual(instance.isVisible(),false);
qty('0');instance.correctQty();assert.strictEqual(qty(),1);
unit(21);assert.strictEqual(instance.isVisible(),true);assert.strictEqual(instance.getUnitPrice(),'21.0000');
assert.strictEqual(instance.getTotalPrice(),'21.00');
modules['FourOver_Project/js/model/product-tier-prices']([{qty:1,price:20.5},{qty:10,price:18}]);
assert.strictEqual(processor.getUnitPrice(1),20.5);assert.strictEqual(processor.getUnitPrice(10),18);
process.stdout.write('actual source dependency checks passed');
'''.replace('SOURCES',json.dumps(sources))
    assert subprocess.check_output([node,'-e',code],text=True)=='actual source dependency checks passed'


def test_preconfiguration_wait_prevents_changes_before_component_listener_mounts():
    from crawler.fourover_real_estate import wait_for_preconfiguration_readiness,commit_quantity_one_before_turnaround,QUOTE_PARTIAL_EVIDENCE
    playwright=pytest.importorskip('playwright.async_api')
    async def scenario():
        async with playwright.async_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.set_content('<main class="column main"><select id="size"><option>A</option><option>B</option></select><div id="mount"></div></main>')
                await page.evaluate('''() => {
                  window.ready=false;window.changes=0;window.require=Object.assign(()=>{}, {defined:()=>window.ready});
                  setTimeout(()=>{
                    document.querySelector('#mount').innerHTML='<ul><li style="display:none" class="runsizes_row custom_runsize_row"><input type="number" id="qty-box-qty" min="1" value="1"><span class="custom-run-size-unitprice">$0.0000</span><span class="custom-run-size-totalprice">$0.00</span></li></ul>';
                    window.ready=true;
                    document.querySelector('#size').addEventListener('change',()=>{window.changes++;
                      document.querySelector('li').style.display='block';
                      document.querySelector('.custom-run-size-unitprice').textContent='$21.1250';
                      document.querySelector('.custom-run-size-totalprice').textContent='$21.13';
                    });
                  },150);
                }''')
                await page.locator('#size').select_option('B')
                assert await page.evaluate('window.changes')==0
                partial={};token=QUOTE_PARTIAL_EVIDENCE.set(partial)
                try:
                    ready=await wait_for_preconfiguration_readiness(page,timeout=1)
                    assert ready['owned_qty_input_count']==1
                    # Attachment is sufficient here; its price-driven wrapper
                    # intentionally remains hidden until this ordinary change.
                    assert await page.locator('#qty-box-qty').is_visible() is False
                    await page.locator('#size').select_option('A')
                    receipt=await commit_quantity_one_before_turnaround(page,timeout=.4)
                finally:QUOTE_PARTIAL_EVIDENCE.reset(token)
                assert await page.evaluate('window.changes')==1
                assert receipt['unit_price']=='21.1250' and receipt['row_total']=='21.13'
                assert partial['preconfiguration_readiness']['ready'] is True
                await page.evaluate('window.ready=false')
                with pytest.raises(ValueError,match='pricing_component_not_ready_before_configuration'):
                    await wait_for_preconfiguration_readiness(page,timeout=.02)
            finally:await browser.close()
    asyncio.run(scenario())


def test_readiness_http_categories_omit_queries_and_account_or_source_ids():
    from crawler.fourover_real_estate import readiness_http_status
    record=readiness_http_status('/rest/default/V1/product/prices/83539?token=private',403)
    assert record=={'category':'product_pricing','path':'/product/prices/[source-product]','http_status':403}
    assert '83539' not in json.dumps(record) and 'private' not in json.dumps(record)
    assert readiness_http_status('/print/project/summary/',401)['http_status']==401
    assert readiness_http_status('/customer/778899/address/token-private',200) is None


def test_owned_styled_radio_uses_visible_label_and_does_not_reselect_checked_choice():
    from crawler.fourover_real_estate import select_turnaround_before_shipping
    playwright=pytest.importorskip('playwright.async_api')
    async def scenario():
        async with playwright.async_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                def markup(checked='',disabled='',label_style=''):
                    return f'''<main class="column main"><input style="visibility:hidden" type="radio" id="tat-4" name="turnaround_time" value="4" {checked} {disabled}>
                        <label style="{label_style}" for="tat-4">4 Business Days</label></main>'''
                await page.set_content(markup('checked'))
                await page.evaluate("window.labelClicks=0;document.querySelector('label').addEventListener('click',()=>window.labelClicks++)")
                assert await page.locator('input').is_visible() is False
                proof=await select_turnaround_before_shipping(page,timeout=.2)
                assert proof['retained'] is True and proof['selection_action']=='already_selected_read_only'
                assert await page.evaluate('window.labelClicks')==0
                await page.set_content(markup())
                proof=await select_turnaround_before_shipping(page,timeout=.2)
                assert proof['retained'] is True and proof['selection_action']=='native_associated_label_clicked'
                for html in [markup('checked',label_style='display:none'),markup('checked','disabled'),
                             markup('checked').replace('</main>','<input type="radio" name="turnaround_time" value="4"></main>')]:
                    await page.set_content(html)
                    with pytest.raises(ValueError,match='turnaround_not_ready_before_shipping'):
                        await select_turnaround_before_shipping(page,timeout=.02)
            finally:await browser.close()
    asyncio.run(scenario())
