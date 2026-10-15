"""Independent native UI canaries against intercepted Chromium only."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from crawler.b2sign_canary import (B2SignCanaryManager, native_request_projection,
                                  intermediate_size_request,
                                   request_matches_case, _key)
from crawler.b2sign_catalog import enumerate_catalog, parse_public_product, PINNED_BUNDLE_SHA256, _TENT_WALL_ACTIONS
from crawler.b2sign_harvest import HarvestError, SharedQuoteGate, prepare_candidate
from crawler.b2sign_pilot import load_contract
from test_b2sign_catalog import catalog
from test_b2sign_harvest import HarvestPool
from test_b2sign_pilot import response as quote_response
from test_b2sign_diagnosis import B2_HTML


def test_native_tent_wall_action_key_is_reviewed_and_must_match_its_source_rule():
    cat = parse_public_product({'id': 35, 'title': 'Tent', 'url': '/event-tent-canopy-graphic',
        'attrs': {'attrs': [{'key': 'tent_full_wall', 'label': 'Full Wall', 'component': 'select',
                            'options': [{'key': '0', 'label': 'No'}, {'key': '1', 'label': 'Yes'}],
                            'action': _TENT_WALL_ACTIONS}],
                  'turnaround': [{'match': True, 'options': [{'key': 'v1', 'label': 'Next Day'}]}]}},
        source_url='https://www.b2sign.com/event-tent-canopy-graphic',
        source_bundle_sha256=PINNED_BUNDLE_SHA256)
    payload = {'id': 35, 'values': {'tent_full_wall': '0', 'tent_full_wall_velcro': 'nfw'},
               'jobs': json.dumps([{'subjobs': [{'name': '', 'quantity': '101'}], 'shipping': '',
                                    'shipping_service': '03', 'shipping_service_label': 'Ground',
                                    'turnaround': 'v1'}])}
    safe, quantity = native_request_projection(payload, cat)
    assert quantity == 101 and safe['configuration_bound'] is True
    payload['values']['tent_full_wall_velcro'] = 'cfw'
    stale, _ = native_request_projection(payload, cat)
    assert stale['configuration_bound'] is False
    payload['values']['tent_full_wall_velcro'] = 'unknown'
    with pytest.raises(HarvestError, match='native_quote_derived_invalid'):
        native_request_projection(payload, cat)


def test_native_hardware_quote_without_production_control_remains_bound():
    cat = parse_public_product({'id': 202, 'title': 'Hard Case Only', 'url': '/hard-case-only',
        'attrs': {'attrs': [], 'designproof': False}},
        source_url='https://www.b2sign.com/hard-case-only',
        source_bundle_sha256=PINNED_BUNDLE_SHA256)
    payload = {'id': 202, 'values': {'_qty': '10'},
        'jobs': json.dumps([{'subjobs': [{'name': '', 'quantity': '10'}],
                             'shipping': '', 'shipping_service': '03',
                             'shipping_service_label': 'Ground'}])}
    safe, quantity = native_request_projection(payload, cat)
    assert quantity == 10 and safe['configuration_bound'] is True
    assert safe['job'].get('turnaround') is None


class CanaryPool(HarvestPool):
    def html(self):
        item = {'id':31,'url':'/pole-banner-set','title':'Pole Banner Set','attrs':self.contract['attrs']}
        defaults = {attr['key']: ('18x24' if attr['component']=='size' else next((o['key'] for o in attr.get('options',[]) if o.get('default')), '')) for attr in self.contract['attrs']['attrs']}
        html = B2_HTML.replace('value="1" checked','value="v1" checked').replace('value="0"><span','value="v0"><span')
        width = '<div class="_attribute_fixture" id="attr-pole_width" style="display:none"><label>Width</label><button role="combobox" class="MuiSelect-button" aria-controls="width-list" aria-expanded="false">18&quot;</button></div>'
        html = html.replace('<div class="_attribute_fixture" id="attr-windslit">', width + '<div class="_attribute_fixture" id="attr-windslit">')
        script = r'''
const fixtureItem=ITEM, fixtureValues=VALUES;
const app=document.createElement('div');app.id='app';app.dataset.page=JSON.stringify({props:{item:fixtureItem,customer:{secret:'PRIVATE_USER'}}});document.body.appendChild(app);
const quantity=document.querySelector('div[class*="_quantity_"] input');
const subtotal=document.querySelector('div[class*="_subtotal_"]');
const turnaround=document.querySelector('div[class*="_turnaround_"] input:checked');
function production(){const q=Number(quantity.value);turnaround.value=q>200?'v5':q>100?'v3':'v1';if(turnaround.closest('label'))turnaround.closest('label').querySelector('span').textContent=q>200?'5 Business Days':q>100?'3 Business Days':'Next Day';}
async function nativeQuote(){
 production();const q=Number(quantity.value);
 const payload={id:31,values:{...fixtureValues},jobs:JSON.stringify([{subjobs:[{name:'',quantity:String(q)}],shipping:'',shipping_service:'03',shipping_service_label:'Ground',turnaround:turnaround.value}])};
 const r=await fetch('/item/quote',{method:'POST',headers:{'Content-Type':'application/json','X-Inertia':'true','X-XSRF-Token':'PRIVATE_CSRF_FIXTURE'},body:JSON.stringify(payload)});
 const body=await r.json();if(!body.props?.quoteResult?.quote?.[0])return;
 subtotal.textContent='$'+(body.props.quoteResult.quote[0].subtotal+MISMATCH).toFixed(2);
 if(fixtureValues.hardware==='h'){document.querySelector('div[class*="_turnaround_"]')?.remove();document.querySelector('.MuiCheckbox-root')?.remove();}
 // Inertia replaces bootstrap props after the first native response.
 app.dataset.page=JSON.stringify({props:{quoteResult:{quote:[]}}});
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.querySelector('#width-list')?.remove();document.querySelector('#attr-pole_width button').setAttribute('aria-expanded','false');}});
const widthButton=document.querySelector('#attr-pole_width button');widthButton.addEventListener('click',()=>open(widthButton,['18"','24"','30"'],'width'));
quantity.addEventListener('input',()=>{production();subtotal.textContent='$'+(56.26*Number(quantity.value)).toFixed(2)});
document.addEventListener('click',e=>{
 const option=e.target.closest('[role=option]');if(option){
  if(option.parentElement.id==='size-list'){fixtureValues._size=option.textContent.replaceAll('"','').replaceAll(' ','');fixtureValues.pole_width=fixtureValues._size.split('x')[0]}
  else if(option.parentElement.id==='wind-list')fixtureValues.windslit=option.textContent==='No Windslits'?'0':'1';
  else if(option.parentElement.id==='width-list')fixtureValues.pole_width=option.textContent.replaceAll('"','');else return;
  nativeQuote();
 }
 const button=e.target.closest('#attr-hardware div[class*="_item_"]');if(button){
  document.querySelectorAll('#attr-hardware div[class*="_item_"]').forEach(x=>x.classList.remove('_active_fixture'));button.classList.add('_active_fixture');
  fixtureValues.hardware={'Banner + Hardware':'p','Banner Only':'0','Hardware Only':'h'}[button.textContent];document.querySelector('#attr-pole_width').style.display=fixtureValues.hardware==='h'?'':'none';nativeQuote();
 }
});
setTimeout(nativeQuote,20);
fetch('/cart/add',{method:'POST',body:'PRIVATE_CART'}).catch(()=>{});
fetch('/account/addresses').catch(()=>{});
'''.replace('ITEM', json.dumps(item)).replace('VALUES', json.dumps(defaults)).replace('MISMATCH', '3' if self.mismatch else '0')
        if getattr(self, 'reset_wind', False):
            script = script.replace('// Inertia replaces', "if(fixtureValues.windslit==='1')document.querySelector('#wind-button').textContent='No Windslits';\n // Inertia replaces")
        return html.replace('</body>', '<script src="/build/assets/app-fixture.js"></script><script>'+script+'</script></body>')


def plan_for(cat, quantities=(1,), *, wind='0', expected=True):
    cases = []
    for qty in quantities:
        row = next(r for r in enumerate_catalog(cat, quantities=[qty]) if r['values']['hardware']=='p' and r['values']['_size']=='18x24' and r['values']['windslit']==wind)
        total = str(round(56.26*qty,2)) if expected else None
        cases.append({'id':_key(row['values'],qty,row['turnaround'],cat),'candidate':row,
                      'reference_status':'matching_saved_quote' if expected else 'no_matching_saved_quote',
                      'reference_quotes':[{'subtotal':total,'candidate_id':'saved','context_id':'old'}] if expected else [],
                      'expected_subtotal':total,'covers':['quantity:'+str(qty)]})
    return {'source_url':cat['url'],'source_product_id':cat['source_product_id'],'contract_sha256':cat['contract_sha256'],
            'cases':cases,'required_count':len(cases)+1,'uncovered_requirements':[{'kind':'production','value':'v0','reason':'source_clock_unresolved'}]}


@pytest_asyncio.fixture
async def runner(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium unavailable')
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(service_workers='block')
        pool = CanaryPool(context, load_contract())
        manager = B2SignCanaryManager(SimpleNamespace(root=tmp_path),pool,timeout_seconds=3)
        manager.gate = SharedQuoteGate(6)  # Local fixtures only; runtime remains1RPS.
        manager.bundle_url='https://www.b2sign.com/build/assets/app-fixture.js'
        manager.bundle_sha256=hashlib.sha256(pool.bundle.encode()).hexdigest()
        monkeypatch.setattr('crawler.b2sign_canary.validate_url',AsyncMock())
        def native_response(quantity):
            body=quote_response(quantity)
            if not getattr(pool,'omit_type',False):
                body['props']['quoteResult']['type']='hardware' if pool.posted[-1]['values'].get('hardware')=='h' else 'printable'
            return body
        monkeypatch.setattr('test_b2sign_harvest.response',native_response)
        try:
            yield manager,pool
        finally:
            await manager.close()
            await browser.close()


@pytest.mark.asyncio
async def test_native_UI_quantity201_forces_fresh_quote_without_initializer_or_secret(runner):
    manager,pool=runner
    cat=catalog()
    user=await pool.context.new_page();await user.set_content('<h1>User tab unchanged</h1>')
    report=await manager.start(cat,plan_for(cat,(1,201)))
    await manager._tasks[report['id']]
    done=manager.get(report['id'])
    assert done['status']=='completed',done
    assert done['completed_count']==done['matched_reference_count']==done['dom_server_matched_count']==2
    assert done['catalog_complete'] is False and done['prices_verified'] is False
    assert done['uncovered_requirements'][0]['value']=='v0'
    assert all('_qty' not in p['values'] for p in pool.posted)
    second=next(r for r in done['results'] if r['candidate']['quantity']==201)
    assert second['candidate']['turnaround']=='v5'
    assert 'fresh_quote_toggle:windslit' in second['actions']
    assert second['request']['quantity_representation']=='job_only'
    assert second['unit_times_quantity_match'] is True
    assert done['quote_requests']==4  # Two bootstraps + legitimate away/back choices.
    assert all('/cart/' not in url and '/account/' not in url for _,url in pool.requests)
    assert not user.is_closed() and all(p.is_closed() for p in pool.pages)
    for path in manager.root.rglob('*.json'):
        assert 'PRIVATE' not in path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_custom_dropdown_source_value_native_request_and_missing_reference_remains_explicit(runner):
    manager,pool=runner
    cat=catalog()
    report=await manager.start(cat,plan_for(cat,wind='1',expected=False))
    await manager._tasks[report['id']]
    done=manager.get(report['id'])
    assert done['status']=='completed',done
    row=done['results'][0]
    assert row['request']['values']['windslit']=='1'
    assert row['status']=='dom_server_matched_without_reference'
    assert done['matched_reference_count']==0 and done['dom_server_matched_count']==1
    assert row['reference_match'] is False and row['final_eligible'] is False


@pytest.mark.asyncio
async def test_mismatch_checkpoints_and_resume_does_not_overwrite_observation(runner):
    manager,pool=runner
    pool.mismatch=True
    cat=catalog()
    report=await manager.start(cat,plan_for(cat,(1,10)))
    await manager._tasks[report['id']]
    done=manager.get(report['id'])
    assert done['status']=='attention' and done['mismatch_count']==1,done
    assert done['completed_count']==1
    saved=next((manager._path(report['id'])/'results').glob('*.json'))
    before=saved.read_bytes()
    pool.mismatch=False
    await manager.resume(report['id']);await manager._tasks[report['id']]
    assert saved.read_bytes()==before
    assert manager.get(report['id'])['mismatch_count']==1
    assert manager.get(report['id'])['status']=='attention'


def test_native_projection_rejects_initializer_shadow_and_addresses():
    cat=catalog();c=prepare_candidate(cat,plan_for(cat)['cases'][0]['candidate'])
    payload={'id':31,'values':c['values'],'jobs':[{'subjobs':[{'name':'','quantity':'1'}],'shipping':'','turnaround':'v1'}]}
    safe,qty=native_request_projection(payload,cat)
    assert qty==1 and request_matches_case(safe,c)
    for mutate in [lambda p:p['values'].update(_qty='10'),lambda p:p['values'].update(_designproof='1'),lambda p:p['jobs'][0].update(shipping_to_id=4),lambda p:p['jobs'][0]['subjobs'][0].update(name='PRIVATE')]:
        altered=copy.deepcopy(payload);mutate(altered)
        with pytest.raises(HarvestError):native_request_projection(altered,cat)


def test_native_projection_keeps_open_size_intermediate_states_scoped():
    from test_b2sign_catalog import simple
    cat=catalog(simple([{'key':'_size','component':'size','options':
                         [{'key':'width','label':'Width'},{'key':'height','label':'Height'}]}]))
    payload={'id':999,'values':{'_size':'0x0'},
             'jobs':[{'subjobs':[{'name':'','quantity':'101'}],'shipping':'','turnaround':'regular'}]}
    safe,qty=native_request_projection(payload,cat)
    assert qty==101 and safe['values']['_size']=='0x0'
    for value in ['24x36','','height']:
        altered=copy.deepcopy(payload);altered['values']['_size']=value
        assert native_request_projection(altered,cat)[1]==101
    altered=copy.deepcopy(payload);altered['values']['_size']='PRIVATE_ADDRESS'
    with pytest.raises(HarvestError, match='native_quote_size_invalid'):
        native_request_projection(altered,cat)
    assert intermediate_size_request(cat, {'values': {'_size': 'height'}})
    assert intermediate_size_request(cat, {'values': {'_size': ''}})
    assert not intermediate_size_request(cat, {'values': {'_size': '24x36'}})


@pytest.mark.asyncio
async def test_automatic_control_reset_cannot_use_prior_exact_response(runner):
    manager,pool=runner
    pool.reset_wind=True
    cat=catalog()
    report=await manager.start(cat,plan_for(cat,wind='1'))
    await manager._tasks[report['id']]
    done=manager.get(report['id'])
    assert done['status']=='attention',done
    assert done['completed_count']==0
    assert done['last_readiness_diagnostic']['fresh_bound_quotes']>=1
    assert done['last_readiness_diagnostic']['attribute_controls_match'] is False


def hardware_plan(cat):
    plan=plan_for(cat)
    row=next(r for r in enumerate_catalog(cat,quantities=[1]) if r['values']['hardware']=='h' and r['values']['pole_width']=='24')
    plan['cases'][0]['candidate']=row
    plan['cases'][0]['id']=_key(row['values'],1,row['turnaround'],cat)
    plan['cases'][0]['covers'] += ['production:v1']
    return plan


@pytest.mark.asyncio
async def test_nonprintable_hardware_proves_price_but_holds_absent_production(runner):
    manager,pool=runner
    cat=catalog()
    report=await manager.start(cat,hardware_plan(cat))
    await manager._tasks[report['id']]
    done=manager.get(report['id'])
    assert done['status']=='completed',done
    row=done['results'][0]
    assert row['status']=='matched_reference' and row['request']['values']['hardware']=='h'
    assert row['request']['values']['pole_width']=='24'
    assert row['source_reported_printable'] is False
    assert row['production_verification']['status']=='source_nonprintable_control_absent'
    assert row['production_verification']['availability_verified'] is False
    assert row['production_verification']['request_value']=='v1'
    assert 'production:v1' not in row['covers']
    assert done['production_held_count']==1
    assert done['last_readiness_diagnostic']['production_absence_bound_to_nonprintable_quote'] is True
    assert row['proof_observation']=='observed_unchecked_then_source_nonprintable_control_absent'


@pytest.mark.asyncio
async def test_absent_production_without_source_type_evidence_is_unresolved_not_unavailable(runner):
    manager,pool=runner
    pool.omit_type=True
    cat=catalog()
    report=await manager.start(cat,hardware_plan(cat))
    await manager._tasks[report['id']]
    done=manager.get(report['id'])
    assert done['status']=='attention',done
    assert done['completed_count']==0
    assert done['warnings']==['canary_production_control_unresolved']
    assert done['last_production_diagnostic']['target_radio_count']==0
    assert done['last_production_diagnostic']['source_reports_nonprintable'] is False
    assert done['last_production_diagnostic']['source_availability_determined'] is False
