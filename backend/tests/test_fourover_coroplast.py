import asyncio
import copy
import json
from pathlib import Path

import pytest

from crawler.fourover_coroplast import (SOURCE, OBSERVE_JS, selection_from_diagnosis,
    retained_selection, observation_receipt, configure_and_observe, forbidden_write)


def diagnosis():
    specs = [('dynamic_size[width]','',['6','24','36']), ('dynamic_size[height]','',['6','18','46']),
        ('super_attribute[197]','attribute197',['645']),('super_attribute[204]','attribute204',['18','97']),
        ('options[489]','select_489',['595','596']),('options[490]','select_490',['597','598'])]
    return {'id':'a'*32,'revision':1,'status':'ready','supplier_id':'fourover','url':SOURCE,
            'fields':[{'source_name':name,'source_id':identity,'label':name,'kind':'select','disabled':False,
                       'choices':[{'value':value,'label':value,'disabled':False} for value in values]}
                      for name,identity,values in specs]}


SELECTION = {'dynamic_size[width]':'24','dynamic_size[height]':'18','super_attribute[197]':'645',
             'super_attribute[204]':'97','options[489]':'596','options[490]':'598'}


def observation():
    return {'status':'observed','source_url':SOURCE,'selects':[{'name':f['source_name'],'id':f['source_id'],
        'selected':SELECTION[f['source_name']],'disabled':False,'valid':True,'choices':f['choices']}
        for f in diagnosis()['fields']], 'truncation':{'choices':False,'quantity_rows':False}}


@pytest.mark.parametrize('update',[
    {'url':'https://4over.com/gator-board-signs'},{'supplier_id':'printograph'},{'status':'closed'},
])
def test_other_products_and_closed_diagnoses_cannot_configure(update):
    value=diagnosis();value.update(update)
    with pytest.raises(ValueError,match='scope_invalid'):selection_from_diagnosis(value,SELECTION)


@pytest.mark.parametrize('selections',[
    {}, {'input[type=password]':'secret'}, {'project_name':'Customer name'},
    {'options[9999]':'1'}, {'options[490]':'999'}, {'dynamic_size[width]':24},
    {'dynamic_size[width]':''}, {'dynamic_size[width]':'4'},
])
def test_only_observed_public_selects_and_values_are_allowed(selections):
    with pytest.raises(ValueError):selection_from_diagnosis(diagnosis(),selections)


def test_duplicate_diagnosed_fields_and_disabled_choices_are_rejected():
    value=diagnosis();value['fields'].append(copy.deepcopy(value['fields'][0]))
    with pytest.raises(ValueError,match='not_observed'):selection_from_diagnosis(value,SELECTION)
    value=diagnosis();value['fields'][0]['choices'][1]['disabled']=True
    with pytest.raises(ValueError,match='not_observed'):selection_from_diagnosis(value,SELECTION)


def test_receipt_is_branch_evidence_never_green_or_interpolated_price():
    selected=selection_from_diagnosis(diagnosis(),SELECTION)
    result=observation_receipt(observation(),selected,diagnosis_id='a'*32,revision=1)
    assert result['coverage']['supplier_prices']=='unknown'
    assert result['coverage']['other_branches']=='unknown'
    assert result['source_data_finalized'] is False and result['can_trigger_auto_correction'] is False
    assert len(result['observation_sha256'])==64
    assert 'supplier_total' not in result


@pytest.mark.parametrize('modify',[
    lambda x:x['selects'][0].update(selected='6'),
    lambda x:x['selects'][0].update(disabled=True),
    lambda x:x['selects'][0].update(valid=False),
    lambda x:x['selects'][2].update(id='replacement-control'),
    lambda x:x['selects'].append(copy.deepcopy(x['selects'][0])),
    lambda x:x['truncation'].update(choices=True),
    lambda x:x.update(source_url=SOURCE+'?other=1'),
])
def test_reset_duplicate_or_truncated_native_fields_fail_closed(modify):
    value=observation();modify(value)
    with pytest.raises(ValueError):retained_selection(value,selection_from_diagnosis(diagnosis(),SELECTION))


def native_page():
    controls=[]
    for field in diagnosis()['fields']:
        controls.append('<label>'+field['label']+'<select name="'+field['source_name']+'" id="'+field['source_id']+'">'
            +'<option value="">Choose</option>'+''.join('<option value="'+c['value']+'">'+c['label']+'</option>' for c in field['choices'])
            +'</select></label>')
    return '<main class="column main">'+''.join(controls)+'''<div id="nested"></div>
        <input name="customer_email" value="never-capture@example.org"><input name="project_name" value="Private order">
        <script>document.querySelector('[name="options[490]"]').addEventListener('change',()=>{
          setTimeout(()=>document.querySelector('#nested').innerHTML='<label>H-Stakes<select name="options[491]" id="select_491"><option value="">Choose</option><option value="600">None</option><option value="601">One stake</option></select></label><label>Quantity<input id="qty-box-qty" name="qty" type="number" min="1" max="499" value="1"></label>',90)
        });</script></main>'''


def test_real_browser_native_changes_discover_nested_controls_without_actions_or_private_values():
    playwright=pytest.importorskip('playwright.async_api')
    async def run():
        async with playwright.async_playwright() as runtime:
            if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.route(SOURCE,lambda route:route.fulfill(content_type='text/html',body=native_page()))
                await page.goto(SOURCE)
                await page.evaluate('''() => {window.choiceTimes=[];document.querySelector('.column.main').addEventListener('change',e=>{if(e.target.matches('select'))window.choiceTimes.push(performance.now())})}''')
                # An unrelated outside clone must never become the owner.
                await page.evaluate('''() => {const e=document.createElement('select');e.name='options[490]';document.body.append(e)}''')
                checks=[]
                async def scope():checks.append(True)
                result=await configure_and_observe(page,diagnosis(),SELECTION,scope,timeout=8)
                observed=result['observation']
                nested=[f for f in observed['selects'] if f['name']=='options[491]']
                assert len(nested)==1 and nested[0]['selected']==''
                assert observed['numbers'][0]['max']=='499'
                assert result['coverage']['quantity_expansion']=='unknown'
                assert len(checks)>6
                times=await page.evaluate('window.choiceTimes')
                assert len(times)==6 and all(right-left>=950 for left,right in zip(times,times[1:]))
                raw=json.dumps(result)
                assert 'never-capture' not in raw and 'Private order' not in raw
                # New controls need a new diagnosis. They are not guessed.
                with pytest.raises(ValueError,match='not_observed'):
                    await configure_and_observe(page,diagnosis(),{'options[491]':'601'},scope)
                await page.evaluate("document.querySelector('.column.main').append(document.querySelector('[name=\"options[490]\"]').cloneNode(true))")
                with pytest.raises(ValueError,match='control_unavailable'):
                    await configure_and_observe(page,diagnosis(),{'options[490]':'598'},scope)
            finally:await browser.close()
    asyncio.run(run())


def test_api_request_is_strict_and_scoped():
    from pydantic import ValidationError
    from crawler.app import CoroplastObservedBranchRequest
    assert CoroplastObservedBranchRequest(revision=1,selections=SELECTION).revision==1
    for value in ({'revision':True,'selections':SELECTION},{'revision':1,'selections':SELECTION,'price':'1.00'},
                  {'revision':1,'selections':{'dynamic_size[width]':24}}):
        with pytest.raises(ValidationError):CoroplastObservedBranchRequest(**value)


@pytest.mark.parametrize('method,path,blocked',[
    ('POST','/rest/default/V1/project/mine/address',True),
    ('POST','/customer/address/formPost',True),('POST','/checkout/cart/add',True),
    ('POST','/rest/default/V1/carts/mine/items',True),('POST','/sales/order/create',True),
    ('POST','/rest/default/V1/project/mine/summary',False),
    ('GET','/customer/address/',False),('GET','/4coro-coroplast-signs',False),
])
def test_observation_blocks_transactional_and_address_writes(method,path,blocked):
    assert forbidden_write(method,'https://4over.com'+path) is blocked
