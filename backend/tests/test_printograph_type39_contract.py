"""Synthetic subtotal/capture scope tests; no source-price requests or domain claims."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from crawler.price_contract import (
    BASE_FIELDS, CONTROLLER_PRICE_STATE_JS, OPTION_FIELDS, SOURCE_CONTRACT,
    SPECIAL_SHAPES_SOURCE_CONTRACT, SPECIAL_SHAPES_SOURCE_URL,
    calculate_source_total, normalize_price_context,
)
from test_price_contract import captured
from test_product_diagnosis import PRODUCT, completed, manager

# Generated offline from untouched historical price_contract.py SHA779b6aeef...
# before the candidate was applied anywhere. Compact canonical context/result
# hashes and the historical context identity make this portable and non-self-referential.
HISTORICAL_GOLDENS = {
    '1': ('39dc3a19ea61b963c6667f958c8f6e35744a04feebc39ea9b334570871fb9c69',
          '3a1a666ae2377b1e7d99705529c943e29603acf370f0d676fc7cb3c610ef45ba',
          '39dc3a19ea61b963c6667f958c8f6e35744a04feebc39ea9b334570871fb9c69'),
    '2': ('e95832a15b5af8898c79b225dc00454648676a00f2913f5fa467ed9a0932e11c',
          '550641b685da1bbc6ad51df9887425f359004ebcd1d05ffa425701bb83f2e5ea',
          'e95832a15b5af8898c79b225dc00454648676a00f2913f5fa467ed9a0932e11c'),
    '27': ('f0f8eece3b7cf80483e2f545af056d21f20e1c308693a1db6074443f6c58c638',
           'd1e7075e3de9352f1f73ffdefc82a002ca5d7af8c29677cba195a7adc33d67e0',
           'f0f8eece3b7cf80483e2f545af056d21f20e1c308693a1db6074443f6c58c638'),
    '36': ('c456cd8e1067439eded79a13bf91608ac7ca928115c062535c59ad86ee37558d',
           'df60fde2b558ab3475c3474d24fca55c94be70833585d026ca7701ffcc04ba9c',
           'c456cd8e1067439eded79a13bf91608ac7ca928115c062535c59ad86ee37558d'),
}


def special_shapes():
    value = captured(product_type='39', base='10.00')
    value.update(source_contract=dict(SPECIAL_SHAPES_SOURCE_CONTRACT), source_url=SPECIAL_SHAPES_SOURCE_URL)
    value['field_specs'] = list(BASE_FIELDS['39'])
    value['userSelectedOptions'].update(shape='15', size='101', orientation='1', paper='1', color='3',
                                        coating='5', spotCoating='', paperFinish='1')
    return value


def test_current_type39_formula_only_keeps_actual_contract_and_source_provenance():
    value = special_shapes()
    result = calculate_source_total(value)
    assert result['status'] == 'complete' and result['subtotal'] == '10.00'
    assert result['context']['source_contract'] == SPECIAL_SHAPES_SOURCE_CONTRACT
    assert result['context']['source_url'] == SPECIAL_SHAPES_SOURCE_URL
    assert result['context']['field_options'] == ['coating','spotCoating','paperFinish']
    assert result['context']['selections']['shape'] == '15'
    assert result['fresh_source_verified'] is False and result['cart_verified'] is False


def test_type39_manual_proof_and_spot_component_are_explicit_not_inferred():
    value = special_shapes()
    value['userSelectedOptions'].update(proofType='100',manualProofType='1',fileDelivery='5',spotCoating='3')
    value['appliedPrices'].update(proofType={'1':'2.00'},spotCoating={'3':'3.00'})
    value['displayed_total'] = '15.00'
    result = calculate_source_total(value)
    assert result['subtotal'] == '15.00' and result['comparison'] == 'matched'
    assert [row['field'] for row in result['components']] == ['productPrice','proofType','spotCoating']
    del value['appliedPrices']['proofType']
    assert calculate_source_total(value)['subtotal'] is None


@pytest.mark.parametrize('role', ['order_form_sha256','common_sha256'])
def test_cross_mixed_bundle_pairs_never_borrow_reviewed_identity(role):
    value = special_shapes(); value['source_contract'][role] = SOURCE_CONTRACT[role]
    result = calculate_source_total(value)
    assert result['status'] == 'unresolved' and 'unreviewed_source_contract' in result['reasons']
    assert result['context']['source_contract'] == {}
    assert result['subtotal'] is None


@pytest.mark.parametrize('kind', ['extra','missing','historical_type39','current_type1','url','store'])
def test_exact_tuple_product_url_and_broker_scope_fail_closed(kind):
    value = special_shapes()
    if kind == 'extra': value['source_contract']['unreviewed'] = '0' * 64
    elif kind == 'missing': del value['source_contract']['common_sha256']
    elif kind == 'historical_type39': value['source_contract'] = dict(SOURCE_CONTRACT)
    elif kind == 'current_type1':
        value = captured(); value.update(source_contract=dict(SPECIAL_SHAPES_SOURCE_CONTRACT),source_url=SPECIAL_SHAPES_SOURCE_URL)
    elif kind == 'url': value['source_url'] = 'https://www.printograph.com/products/catalogs/order'
    elif kind == 'store': value['store_type'] = 'retail'
    result = calculate_source_total(value)
    assert result['status'] == 'unresolved' and result['subtotal'] is None


@pytest.mark.parametrize('field,value', [('orientation','3'),('color','4'),('coating','7'),('spotCoating','2'),
                                       ('paperFinish','2'),('productionTime','2')])
def test_type39_static_source_value_domains_are_guarded(field,value):
    capture = special_shapes(); capture['userSelectedOptions'][field] = value
    assert 'unsupported_selected_option:'+field in calculate_source_total(capture)['reasons']


def test_missing_type39_shape_services_and_field_order_cannot_reconcile():
    for field in ('shape','proofType','manualProofType','designServices','fileDelivery'):
        value = special_shapes(); del value['userSelectedOptions'][field]
        assert calculate_source_total(value)['status'] == 'unresolved'
    value = special_shapes(); value['field_options'].reverse()
    assert 'unreviewed_option_field_order' in calculate_source_total(value)['reasons']


@pytest.mark.parametrize('kind',['missing','reordered','extra'])
def test_type39_declared_base_specs_are_required_in_python(kind):
    value=special_shapes()
    if kind=='missing': del value['field_specs']
    elif kind=='reordered': value['field_specs'].reverse()
    else: value['field_specs'].append('unreviewed')
    assert 'unreviewed_base_field_order' in calculate_source_total(value)['reasons']


@pytest.mark.parametrize('kind', ['1','2','27','36'])
def test_historical_capture_context_hash_and_result_preserved_exactly(kind):
    value = captured(product_type=kind)
    normalized = normalize_price_context(value)
    canonical_hash = lambda item: hashlib.sha256(json.dumps(item,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    assert canonical_hash(normalized['context']) == HISTORICAL_GOLDENS[kind][0]
    assert canonical_hash(calculate_source_total(value)) == HISTORICAL_GOLDENS[kind][1]
    assert normalized['context_hash'] == HISTORICAL_GOLDENS[kind][2]


INSTALL_CONTROLLER = '''state => {
 const element=document.querySelector('#order-form');
 element.setAttribute('data-printproducttype',state.product_type);
 element.setAttribute('data-currentstoretype',state.store_type);
 const selected={...state.userSelectedOptions,shoppingCartItemId:'',priceGroupId:'',accountEmail:'PRIVATE_SENTINEL'};
 const controller={printProductType:Number(state.product_type),fields:{[state.product_type]:{options:state.field_options,specs:state.field_specs}},
   jsonObj:{userSelectedOptions:selected,appliedPrices:state.appliedPrices,
     others:{isAdmin:false,waiveFees:false,itemId:'',editOrderItem:{},oauth:'PRIVATE_SENTINEL'}},
   couponData:{coupon:{},isCouponSpecChanged:false},
   get totalPrice(){if(window.priceForbidden)throw Error('Unreviewed price getter');return state.displayed_total}};
 const child={_: {subTree:{component:{refs:{orderFormInstance:controller}}}}};
 element._vnode={component:{proxy:{$refs:{childComponents:child}}}};
}'''


async def test_current_controller_js_exact_tuple_scope_and_private_projection():
    playwright = pytest.importorskip('playwright.async_api')
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists(): pytest.skip('Playwright Chromium not installed')
        browser = await runtime.chromium.launch()
        try:
            page = await browser.new_page(); requests=[]
            async def route(request):
                requests.append(request.request.url)
                await request.fulfill(body='<div id="order-form"></div>',content_type='text/html')
            await page.route('**/*',route)
            await page.goto(SPECIAL_SHAPES_SOURCE_URL)
            await page.evaluate(INSTALL_CONTROLLER,special_shapes())
            captured_state = await page.evaluate(CONTROLLER_PRICE_STATE_JS,{'source_contract':SPECIAL_SHAPES_SOURCE_CONTRACT,'settled':True})
            assert captured_state['observed'] is True
            assert captured_state['source_contract'] == SPECIAL_SHAPES_SOURCE_CONTRACT
            assert captured_state['source_url'] == SPECIAL_SHAPES_SOURCE_URL
            assert 'PRIVATE_SENTINEL' not in json.dumps(captured_state)
            assert calculate_source_total(captured_state)['subtotal'] == '10.00'
            await page.evaluate('window.priceForbidden=true')
            for contract in ({}, SOURCE_CONTRACT,
                             {**SPECIAL_SHAPES_SOURCE_CONTRACT,'common_sha256':SOURCE_CONTRACT['common_sha256']},
                             {**SPECIAL_SHAPES_SOURCE_CONTRACT,'extra':'0'*64}):
                result = await page.evaluate(CONTROLLER_PRICE_STATE_JS,{'source_contract':contract,'settled':True})
                assert result['observed'] is False and 'PRIVATE_SENTINEL' not in json.dumps(result)
            await page.evaluate("document.querySelector('#order-form').setAttribute('data-currentstoretype','retail')")
            result=await page.evaluate(CONTROLLER_PRICE_STATE_JS,{'source_contract':SPECIAL_SHAPES_SOURCE_CONTRACT,'settled':True})
            assert result == {'observed':False,'reason':'unsupported_source_store'}
            await page.evaluate("document.querySelector('#order-form').setAttribute('data-currentstoretype','broker')")
            await page.evaluate("document.querySelector('#order-form')._vnode.component.proxy.$refs.childComponents._.subTree.component.refs.orderFormInstance.fields[39].options.reverse()")
            result=await page.evaluate(CONTROLLER_PRICE_STATE_JS,{'source_contract':SPECIAL_SHAPES_SOURCE_CONTRACT,'settled':True})
            assert result == {'observed':False,'reason':'controller_schema_mismatch'}
            for kind in ('missing','reordered','extra'):
                await page.evaluate(INSTALL_CONTROLLER,special_shapes())
                await page.evaluate('''kind => {
                  const fields=document.querySelector('#order-form')._vnode.component.proxy.$refs.childComponents._.subTree.component.refs.orderFormInstance.fields[39];
                  if(kind==='missing')delete fields.specs;
                  else if(kind==='reordered')fields.specs.reverse();else fields.specs.push('unreviewed');
                }''',kind)
                result=await page.evaluate(CONTROLLER_PRICE_STATE_JS,{'source_contract':SPECIAL_SHAPES_SOURCE_CONTRACT,'settled':True})
                assert result == {'observed':False,'reason':'controller_base_schema_mismatch'}
            await page.evaluate(INSTALL_CONTROLLER,special_shapes())
            await page.evaluate("document.querySelector('#order-form')._vnode.component.proxy.$refs.childComponents._.subTree.component.refs.orderFormInstance.jsonObj.userSelectedOptions.coating='7'")
            result=await page.evaluate(CONTROLLER_PRICE_STATE_JS,{'source_contract':SPECIAL_SHAPES_SOURCE_CONTRACT,'settled':True})
            assert result == {'observed':False,'reason':'unsupported_selected_option:coating'}
            await page.goto('https://www.printograph.com/products/catalogs/order')
            await page.evaluate(INSTALL_CONTROLLER,special_shapes())
            result=await page.evaluate(CONTROLLER_PRICE_STATE_JS,{'source_contract':SPECIAL_SHAPES_SOURCE_CONTRACT,'settled':True})
            assert result == {'observed':False,'reason':'unreviewed_source_url'}
            assert len(requests)==2  # Two intercepted fixture documents only.
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_manager_passes_actual_loaded_pair_and_never_injects_old_pins(manager,monkeypatch):
    m,pool=manager
    pool.html[SPECIAL_SHAPES_SOURCE_URL.split('.com',1)[1]]=PRODUCT
    record=await completed(m,SPECIAL_SHAPES_SOURCE_URL)
    page=m._pages[record['id']]
    await page.evaluate(INSTALL_CONTROLLER,special_shapes())
    monkeypatch.setattr(m,'_settle_initial',AsyncMock(return_value=True))
    m._bundle_hashes[record['id']]={key:{value} for key,value in SPECIAL_SHAPES_SOURCE_CONTRACT.items()}
    before=list(pool.requests)
    result=await m.read_scoped_price(record['id'],record['revision'])
    assert result['status']=='observed' and result['source_bundle_verified'] is True
    assert result['loaded_contract']==SPECIAL_SHAPES_SOURCE_CONTRACT
    assert result['capture']['source_contract']==SPECIAL_SHAPES_SOURCE_CONTRACT
    assert result['reconciliation']['subtotal']=='10.00'
    assert result['reconciliation']['fresh_source_verified'] is False and result['reconciliation']['cart_verified'] is False
    assert pool.requests==before and 'PRIVATE_SENTINEL' not in json.dumps(result)
    await page.evaluate('window.priceForbidden=true')
    m._bundle_hashes[record['id']]['common_sha256']={SOURCE_CONTRACT['common_sha256']}
    mixed=await m.read_scoped_price(record['id'],record['revision'])
    assert mixed['status']=='unavailable' and mixed['reason']=='unreviewed_source_contract'
    assert mixed['source_bundle_verified'] is False and 'capture' not in mixed
    assert pool.requests==before
