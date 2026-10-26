"""Existing owned-session metadata only: synthetic host route, no supplier traffic."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from test_printograph_passive_manager import browser_page
from crawler.product_diagnosis import ProductDiagnosisManager
from crawler.dps_workflow_references import (DPS_SUPPLIER_ID as KEY, DPS_ACCOUNT_URL as URL,
    DPS_REGISTRY_ORIGIN as ORIGIN, WORKFLOW_REFERENCES_JS, PUBLIC_PATHS, project_workflow_references)

ITEM={'id':KEY,'custom':True,'origin':ORIGIN,'entry_url':'https://www.digital-print-solutions.com/',
      'website_url':'https://www.digital-print-solutions.com/','name':'DPS'}
HTML='''<!doctype html><h1>My account</h1>
<a href="/vinyl-decals/hi-performance">Hi-Performance</a>
<a href="/cart">Cart</a><a href="/shipping-estimate">Estimate Shipping</a>
<a href="/vinyl-decals/clear-decals">PRIVATE-CUSTOMER-NAME</a>
<a href="/my-account/orders/PRIVATE-ORDER-ID">PRIVATE-ORDER-NAME</a>
<a href="/cart?token=PRIVATE-TOKEN">PRIVATE-CART-TOKEN</a>
<a href="https://elsewhere.test/cart">Cart</a>
<a href="//user:PRIVATE-PASSWORD@www.digital-print-solutions.com/cart">Cart</a>
<a href="/shipping/options">Shipping</a><a href="/shipping/apply">Shipping</a>
<a href="/checkout">Checkout</a><a href="/upload">Upload</a>
<a hidden href="/products">Products</a>
<form action="/shipping-estimate" method="get">
<label for="txtShippingZip">ZIP</label><input id="txtShippingZip" name="postalCode" value="PRIVATE-ZIP">
<select id="CountryList" name="country"><option value="PRIVATE-COUNTRY">PRIVATE-COUNTRY</option></select>
<button id="estimateShipping">Estimate Shipping</button>
<input name="addressLine1" value="PRIVATE-ADDRESS"><input type="password" name="password" value="PRIVATE-PASSWORD">
<input type="hidden" name="token" value="PRIVATE-TOKEN"><input type="file" name="artwork">
</form><form action="/my-account" method="post"><input name="email" value="PRIVATE-EMAIL"></form>
<script>
window.probes={network:0,mutations:0,sourceFunctions:0,privateValues:0};
const forbidden=()=>{probes.sourceFunctions++;throw Error('PRIVATE-MUST-NOT-CALL')};
window.runShippingService=forbidden;window.applyShipping=forbidden;window.getCookieValue=forbidden;
Object.defineProperty(window,'cart',{get(){probes.privateValues++;throw Error('PRIVATE-CART')}});
window.fetch=()=>{probes.network++;throw Error('PRIVATE-NETWORK')};
XMLHttpRequest.prototype.open=()=>{probes.network++;throw Error('PRIVATE-NETWORK')};
for(const el of document.querySelectorAll('input,select'))
 Object.defineProperty(el,'value',{get(){probes.privateValues++;throw Error('PRIVATE-VALUE')}});
new MutationObserver(list=>probes.mutations+=list.length).observe(document,{attributes:true,childList:true,characterData:true,subtree:true});
</script>'''


@pytest_asyncio.fixture
async def owned(browser_page,tmp_path):
    page,server=browser_page
    # Existing fixture maps all real hosts to NOTFOUND. DPS account is fulfilled
    # from this literal fixture before network; no persisted supplier profile.
    async def fixture(route):await route.fulfill(status=200,content_type='text/html',body=HTML)
    await page.route(URL,fixture)
    await page.goto(URL,wait_until='load')
    value=ProductDiagnosisManager(SimpleNamespace(root=tmp_path/'state'),SimpleNamespace())
    value.registry=SimpleNamespace(get=lambda key:copy.deepcopy(ITEM),supplier_for=lambda url:KEY if url==URL else None)
    value._pages['supplier:'+KEY]=page
    value._supplier_sessions[KEY]={'session_status':'signed_in'}
    try:yield value,page,server
    finally:value._pages.clear();await value.close()


@pytest.mark.asyncio
async def test_existing_owned_page_read_has_zero_network_mutation_private_or_function_reads(owned):
    value,page,server=owned;before=list(server.seen);session=copy.deepcopy(value._supplier_sessions)
    result=await value.read_supplier_workflow_references(KEY,URL)
    assert result['ownership']=='existing_supplier_session_page'
    assert result['navigation_performed'] is False and result['source_requests_initiated']==0
    assert result['price_or_shipping_quote_collected'] is False
    assert result['links']==[
      {'path':'/vinyl-decals/hi-performance','label':'Hi-Performance','kind':'product','cart_isolation_required':False},
      {'path':'/cart','label':'Cart','kind':'cart','cart_isolation_required':True},
      {'path':'/shipping-estimate','label':'Estimate Shipping','kind':'shipping','cart_isolation_required':True},
      {'path':'/vinyl-decals/clear-decals','label':None,'kind':'product','cart_isolation_required':False}]
    assert len(result['forms'])==1 and len(result['forms'][0]['controls'])==3
    assert 'PRIVATE' not in json.dumps(result)
    assert await page.evaluate('probes')=={'network':0,'mutations':0,'sourceFunctions':0,'privateValues':0}
    assert server.seen==before and value._supplier_sessions==session


@pytest.mark.asyncio
@pytest.mark.parametrize('key,url',[(KEY,URL+'/'),(KEY,URL+'?token=PRIVATE'),(KEY,'https://www.digital-print-solutions.com/cart'),('fourover',URL),(KEY,True)])
async def test_wrong_supplier_or_url_rejected_before_evaluate(owned,key,url):
    value,page,server=owned;page.evaluate=AsyncMock(side_effect=AssertionError('must not evaluate'))
    with pytest.raises(ValueError,match='scope_unsupported'):await value.read_supplier_workflow_references(key,url)
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('condition',['missing','closed','unsigned','different_page_url','wrong_registry_origin'])
async def test_missing_ownership_or_session_never_opens_or_navigates(owned,condition):
    value,page,server=owned
    if condition=='missing':value._pages.clear()
    if condition=='closed':value._pages['supplier:'+KEY]=SimpleNamespace(is_closed=lambda:True)
    if condition=='unsigned':value._supplier_sessions[KEY]['session_status']='unknown'
    if condition=='different_page_url':await page.evaluate("history.replaceState({},'','/cart')")
    if condition=='wrong_registry_origin':value.registry.get=lambda key:{**ITEM,'origin':'https://unrelated.test'}
    page.goto=AsyncMock(side_effect=AssertionError('no navigation'))
    page.evaluate=AsyncMock(side_effect=AssertionError('no evaluation'))
    with pytest.raises(ValueError):await value.read_supplier_workflow_references(KEY,URL)
    page.goto.assert_not_awaited();page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_url_and_owned_page_identity_drift_after_projection_are_rejected(owned):
    value,page,server=owned;original=page.evaluate
    async def drift(script,args):
        result=await original(script,args)
        await original("history.replaceState({},'','/my-account?PRIVATE-TOKEN')")
        return result
    page.evaluate=drift
    with pytest.raises(ValueError,match='url_changed'):await value.read_supplier_workflow_references(KEY,URL)


@pytest.mark.asyncio
async def test_projection_errors_do_not_export_private_exception_text(owned):
    value,page,server=owned;page.evaluate=AsyncMock(side_effect=RuntimeError('PRIVATE-PASSWORD'))
    with pytest.raises(ValueError,match='^supplier_workflow_projection_failed$'):
        await value.read_supplier_workflow_references(KEY,URL)


@pytest.mark.asyncio
@pytest.mark.parametrize('html',[
 '<a href="/cart">Cart</a>'*513,
 '<form action="/shipping"></form>'*9,
 '<form action="/shipping">'+'<input id="txtShippingZip">'*17+'</form>',
 '<form action="/shipping">'+'<input name="private">'*65+'</form>',
])
async def test_dom_limits_fail_closed_without_partial_results(owned,html):
    value,page,server=owned
    await page.evaluate('(html)=>{document.body.innerHTML=html}',html)
    with pytest.raises(ValueError,match='metadata_limit'):await value.read_supplier_workflow_references(KEY,URL)


@pytest.mark.parametrize('raw',[
 None,[],{'observed':True,'links':[],'forms':[],'account':'PRIVATE'},
 {'observed':True,'links':[{'path':'/cart?token=PRIVATE','label':'Cart','kind':'cart','cart_isolation_required':True}],'forms':[]},
 {'observed':True,'links':[{'path':'/cart','label':'PRIVATE-NAME','kind':'cart','cart_isolation_required':True}],'forms':[]},
 {'observed':True,'links':[{'path':'/cart','label':'Cart','kind':'product','cart_isolation_required':False}],'forms':[]},
 {'observed':True,'links':[],'forms':[{'action_path':'/shipping','method':'get','kind':'shipping','cart_isolation_required':True,'controls':[{'kind':'text','id':'txtShippingZip','name':None,'required':False,'disabled':False,'value':'PRIVATE'}]}]},
 {'observed':True,'links':[{'path':'/cart','label':'Cart','kind':'cart','cart_isolation_required':True}]*25,'forms':[]},
])
def test_python_projection_rejects_unknown_shapes_private_fields_and_bounds(raw):
    with pytest.raises(ValueError):project_workflow_references(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize('target',[URL+'/',URL+'?token=PRIVATE','https://www.digital-print-solutions.com/cart',True])
async def test_explicit_account_open_rejects_invalid_target_before_any_navigation(owned,target):
    value,page,server=owned;page.goto=AsyncMock();value._validated_target=AsyncMock()
    with pytest.raises(ValueError,match='scope_unsupported'):await value.open_supplier(KEY,target_url=target)
    page.goto.assert_not_awaited();value._validated_target.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_restore_only_known_account_get_and_old_open_unchanged(owned):
    value,page,server=owned
    await page.evaluate("history.replaceState({},'','/')")
    value._validated_target=AsyncMock(return_value=(ORIGIN,{'www.digital-print-solutions.com','digital-print-solutions.com'},KEY))
    value.check_supplier=AsyncMock(return_value={'session_status':'signed_in'})
    page.goto=AsyncMock();page.bring_to_front=AsyncMock()
    assert await value.open_supplier(KEY,target_url=URL)=={'session_status':'signed_in'}
    page.goto.assert_awaited_once_with(URL,wait_until='domcontentloaded',timeout=45000)
    value._validated_target.assert_awaited_once_with(URL,login=True)
    page.goto.reset_mock();value._validated_target.reset_mock()
    await value.open_supplier(KEY)
    page.goto.assert_not_awaited();value._validated_target.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_supplier_explicit_account_target_rejected_before_navigation(owned):
    value,page,server=owned;page.goto=AsyncMock();value._validated_target=AsyncMock()
    with pytest.raises(ValueError,match='scope_unsupported'):
        await value.open_supplier('fourover',target_url=URL)
    page.goto.assert_not_awaited();value._validated_target.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_new_owned_page_uses_exact_target_and_existing_page_factory(owned):
    value,page,server=owned;value._pages.clear()
    value._release=AsyncMock();value._new_page=AsyncMock(return_value=page)
    value._validated_target=AsyncMock(return_value=(ORIGIN,{'www.digital-print-solutions.com','digital-print-solutions.com'},KEY))
    value.check_supplier=AsyncMock(return_value={'session_status':'signed_in'})
    page.goto=AsyncMock();page.bring_to_front=AsyncMock()
    await value.open_supplier(KEY,target_url=URL)
    value._validated_target.assert_awaited_once_with(URL,login=True)
    value._new_page.assert_awaited_once_with('supplier:'+KEY,ORIGIN,{'www.digital-print-solutions.com','digital-print-solutions.com'})
    page.goto.assert_awaited_once_with(URL,wait_until='domcontentloaded',timeout=45000)
    value.check_supplier.assert_awaited_once_with(KEY)


@pytest.mark.asyncio
async def test_more_than_24_unique_link_rows_fail_closed(owned):
    from crawler.dps_workflow_references import PUBLIC_LABELS
    value,page,server=owned
    html=''.join('<a href="'+path+'">'+label+'</a>' for path in ('/cart','/shipping') for label in PUBLIC_LABELS)
    await page.evaluate('(html)=>{document.body.innerHTML=html}',html)
    with pytest.raises(ValueError,match='metadata_limit'):
        await value.read_supplier_workflow_references(KEY,URL)


@pytest.mark.asyncio
async def test_total_control_bound_and_unknown_source_functions_remain_unread(owned):
    value,page,server=owned
    html=('<form action="/shipping">'+'<input id="txtShippingZip">'*16+'</form>')*5
    await page.evaluate('(html)=>{document.body.innerHTML=html}',html)
    with pytest.raises(ValueError,match='metadata_limit'):
        await value.read_supplier_workflow_references(KEY,URL)
