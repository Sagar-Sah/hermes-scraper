import asyncio
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import pytest
from sqlalchemy import select

from crawler.campaign_sources import SourceCollector, parse_public_page
from crawler.config import Settings
from crawler.store import Permit, Store
from crawler.transport import ExtractionError


B2 = "https://www.b2sign.com/pole-banner-set"
SOAR = "https://soardistusa.com/products/example"


def b2body(*, title="Pole Banner Set", props=None):
    props = props or {"item": {"id": 31, "title": title, "url": "/pole-banner-set", "price": "17.76",
                                "attrs": [{"id": "size", "values": ["18x24", "24x36"]}], "customer": "PRIVATE_ITEM"},
                      "menus": [{"title": "Hardware", "items": [{"title": "Case", "type": "item", "url": "/hard-case-only", "debug": "PRIVATE_MENU"}]}],
                      "customer": {"email": "PRIVATE_EMAIL"}, "debug": "PRIVATE_DEBUG",
                      "defaultShippingAddress": {"name": "PRIVATE_ADDRESS"}}
    encoded = base64.b64encode(quote(json.dumps({"props": props, "component": "PRIVATE_COMPONENT"})).encode()).decode()
    return f'<html><title>{title}</title><div data-page="{encoded}"></div></html>'.encode()


class Limiter:
    def __init__(self):
        self.grants, self.releases, self.active, self.maximum = [], [], 0, 0
        self.lock = asyncio.Lock()

    async def acquire(self, origin, rps, concurrency, check):
        assert rps == 1 and concurrency == 1
        await self.lock.acquire()
        await check()
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.grants.append(origin)
        return str(len(self.grants))

    def release(self, origin, token, elapsed, status, retry_after, cap):
        self.active -= 1
        self.releases.append((origin, status, retry_after, cap))
        self.lock.release()


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    validations = []
    async def validate(url):
        validations.append(url)
        return urlsplit(url)
    monkeypatch.setattr("crawler.campaign_sources.validate_url", validate)
    yield store, validations
    store.engine.dispose()


async def finish(collector, job_id):
    task = collector._tasks.get(job_id)
    if task:
        await task
    return collector.get(job_id)


def test_b2_projection_decodes_actual_encoding_and_excludes_private_envelope():
    result = parse_public_page(b2body(), B2)
    serialized = json.dumps(result)
    assert "PRIVATE" not in serialized
    assert result["item"]["id"] == 31
    assert result["item"]["attrs"][0]["values"] == ["18x24", "24x36"]
    assert result["menus"] == [{"title": "Hardware", "children": [{"title": "Case", "url": "/hard-case-only", "type": "item"}]}]
    assert any(x["url"] == "https://www.b2sign.com/hard-case-only" for x in result["observed_links"])
    assert all(x["mapping_verified"] is False for x in result["observed_links"])


def test_b2_category_children_are_projected_without_global_account_fields():
    body = b2body(props={"coll": {"id": 4, "title": "Hardware", "children": [
        {"id": 9, "title": "Stand", "url": "/stand", "debug": "PRIVATE"}]}, "customer": "PRIVATE"})
    d = parse_public_page(body, B2)
    assert d["coll"]["children"] == [{"id": 9, "title": "Stand", "url": "/stand"}]
    assert d["observed_links"][0]["relation"] == "collection_child"


def test_soardist_only_unambiguous_product_json_and_no_form_values():
    body = b'''<title>Stand</title><form action="/cart?token=PRIVATE"><input name="csrf_token" value="PRIVATE"><input name="quantity" value="1"></form>
    <script src="https://cdn.example/script.js?token=PRIVATE"></script>
    <script type="application/ld+json">{"@type":"Product","name":"Stand","sku":"S1","offers":{"price":"120","priceCurrency":"USD"},"customer":"PRIVATE"}</script>
    <script type="application/json">{"customer":"PRIVATE","product":{"title":"Not proof"}}</script>'''
    result = parse_public_page(body, SOAR)
    assert result["product_json"]["sku"] == "S1"
    assert "PRIVATE" not in json.dumps(result)
    assert result["forms"][0]["action"] == "https://soardistusa.com/cart"
    ambiguous = body + b'<script type="application/ld+json">{"@type":"Product","name":"Other"}</script>'
    assert parse_public_page(ambiguous, SOAR)["product_json"] is None


def test_nonproduct_json_not_promoted_and_login_or_challenge_rejected():
    assert parse_public_page(b'<title>Collection</title><script type="application/json">{"id":1,"title":"Unknown","variants":[]}</script>', SOAR)["product_json"] is None
    for body in [b'<form><input type="password"></form>', b'<title>Just a moment...</title>']:
        with pytest.raises(ExtractionError) as caught:
            parse_public_page(body, B2)
        assert caught.value.code == "access_required"


@pytest.mark.asyncio
async def test_durable_completion_records_exact_pages_without_following_observed_links(env):
    store, validations = env
    calls = []
    async def handler(request):
        calls.append(str(request.url))
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, content=b2body(), headers={"content-type": "text/html", "set-cookie": "session=PRIVATE"})
    collector = SourceCollector(store, http_transport=httpx.MockTransport(handler))
    collector.limiter = Limiter()
    job = await collector.start([B2, B2])
    result = await finish(collector, job["id"])
    assert result["status"] == "completed" and result["completed_pages"] == 1
    assert result["input_count"] == 2 and result["http_attempts"] == 1
    assert calls == [B2] and validations == [B2, B2]
    assert "public_data" not in result["pages"][0]["evidence"]
    evidence = result["pages"][0]["evidence"]
    stored = store.get_object(evidence["object_path"])
    assert hashlib.sha256(stored).hexdigest() == evidence["sha256"]
    assert b"PRIVATE" not in stored
    assert collector.get(job["id"], include_data=True)["pages"][0]["public_data"]["item"]["id"] == 31
    assert collector.limiter.active == 0
    assert collector._path(job["id"]).stat().st_mode & 0o777 == 0o600
    assert (Path(store.root) / evidence["object_path"]).stat().st_mode & 0o777 == 0o600
    await collector.close()
    reloaded = SourceCollector(store, http_transport=httpx.MockTransport(handler))
    assert reloaded.get(job["id"])["status"] == "completed"
    await reloaded.resume(job["id"])
    assert calls == [B2]
    assert reloaded.list()[0]["id"] == job["id"]
    await reloaded.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://www.b2sign.com/a", "https://b2sign.com.evil.test/a", "https://user:pass@b2sign.com/a", "https://b2sign.com:444/a", "https://b2sign.com/login", "https://soardistusa.com/products.json", "https://b2sign.com/a?session=secret", "https://b2sign.com/a#fragment"])
async def test_invalid_urls_rejected_before_any_dns_or_io(env, url):
    store, validations = env
    collector = SourceCollector(store)
    with pytest.raises(ValueError):
        await collector.start([url])
    assert validations == [] and collector.list() == []
    await collector.close()


@pytest.mark.asyncio
async def test_redirect_is_validated_and_cookies_are_never_replayed(env):
    store, validations = env
    calls = []
    async def handler(request):
        assert "cookie" not in request.headers
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://b2sign.com/pole-banner-set", "set-cookie": "x=PRIVATE"})
        return httpx.Response(200, content=b2body(), headers={"content-type": "text/html"})
    collector = SourceCollector(store, http_transport=httpx.MockTransport(handler))
    collector.limiter = Limiter()
    job = await collector.start([B2]); result = await finish(collector, job["id"])
    assert result["http_attempts"] == 2 and result["status"] == "completed"
    assert validations[-1] == "https://b2sign.com/pole-banner-set"
    assert collector.limiter.grants == ["https://www.b2sign.com"] * 2
    await collector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["http://b2sign.com/a", "https://127.0.0.1/a", "https://soardistusa.com/products/a", "https://b2sign.com/login"])
async def test_unsafe_redirect_stops_without_fetching_target(env, target):
    store, validations = env
    calls = []
    def handler(request):
        calls.append(str(request.url)); return httpx.Response(302, headers={"location": target})
    collector = SourceCollector(store, http_transport=httpx.MockTransport(handler)); collector.limiter = Limiter()
    job = await collector.start([B2]); result = await finish(collector, job["id"])
    assert calls == [B2] and result["error"]["code"] == "invalid_redirect"
    assert collector.limiter.active == 0
    await collector.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(401,"access_required"),(403,"access_required"),(429,"rate_limited"),(500,"source_unavailable"),(502,"source_unavailable"),(503,"source_unavailable"),(504,"source_unavailable")])
async def test_supplier_error_stops_whole_job_no_retry_or_next_url(env, status, code):
    store, _ = env
    calls = []
    def handler(request):
        calls.append(str(request.url)); return httpx.Response(status, headers={"retry-after":"45"})
    collector = SourceCollector(store, http_transport=httpx.MockTransport(handler)); collector.limiter = Limiter()
    job = await collector.start([B2,B2+'-2']); result = await finish(collector,job['id'])
    assert len(calls) == 1 and result['status'] == 'attention' and result['error']['code'] == code
    assert result['pages'][0]['last_http_status'] == status and result['pages'][1]['status'] == 'pending'
    assert collector.limiter.active == 0 and collector.limiter.releases[0][2] == 45
    await collector.close()


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'12345678'; yield b'12345678'


@pytest.mark.asyncio
@pytest.mark.parametrize('length_hint',[False,True])
async def test_response_limit_enforced_before_or_during_stream(env,monkeypatch,length_hint):
    store,_=env
    monkeypatch.setattr('crawler.campaign_sources.MAX_RESPONSE_BYTES',10)
    def handler(request):
        return httpx.Response(200,stream=Chunks(),headers={'content-type':'text/html',**({'content-length':'16'} if length_hint else {})})
    collector=SourceCollector(store,http_transport=httpx.MockTransport(handler));collector.limiter=Limiter()
    job=await collector.start([B2]);result=await finish(collector,job['id'])
    assert result['error']['code']=='response_too_large' and not result['pages'][0]['evidence']
    assert collector.limiter.active==0
    await collector.close()


@pytest.mark.asyncio
async def test_pause_restart_skips_completed_and_releases_actual_shared_permit(env):
    store,_=env
    second_entered=asyncio.Event(); calls=[]
    async def handler(request):
        calls.append(str(request.url))
        if len(calls)==2:
            second_entered.set();await asyncio.Event().wait()
        return httpx.Response(200,content=b2body(),headers={'content-type':'text/html'})
    collector=SourceCollector(store,http_transport=httpx.MockTransport(handler))
    job=await collector.start([B2,B2+'-2'])
    await asyncio.wait_for(second_entered.wait(),4)
    await collector.pause(job['id'])
    assert collector.get(job['id'])['completed_pages']==1
    with store.session() as s:assert list(s.scalars(select(Permit)))==[]
    await collector.close()
    reloaded=SourceCollector(store,http_transport=httpx.MockTransport(handler));reloaded.limiter=Limiter()
    await reloaded.resume(job['id']);result=await finish(reloaded,job['id'])
    assert result['status']=='completed' and calls==[B2,B2+'-2',B2+'-2']
    assert result['http_attempts']==3
    await reloaded.close()


@pytest.mark.asyncio
async def test_two_collectors_cannot_execute_same_job_twice(env):
    store,_=env
    entered=asyncio.Event();release=asyncio.Event();calls=[]
    async def handler(request):
        calls.append(str(request.url));entered.set();await release.wait()
        return httpx.Response(200,content=b2body(),headers={'content-type':'text/html'})
    first=SourceCollector(store,http_transport=httpx.MockTransport(handler));first.limiter=Limiter()
    second=SourceCollector(store,http_transport=httpx.MockTransport(handler));second.limiter=Limiter()
    job=await first.start([B2]);await entered.wait()
    await second.resume(job['id']);await finish(second,job['id']);release.set();await finish(first,job['id'])
    assert calls==[B2]
    await first.close();await second.close()


@pytest.mark.asyncio
async def test_restart_interruption_is_visible_without_auto_requests(env):
    store,_=env
    collector=SourceCollector(store)
    job={'id':'a'*32,'status':'running','pages':[{'url':B2,'status':'fetching','attempts':1,'evidence':None}]}
    collector._save(job)
    report=collector.get(job['id'])
    assert report['status']=='interrupted' and report['worker_active'] is False and report['resumable'] is True
    assert collector._tasks=={}
    await collector.close()


@pytest.mark.asyncio
async def test_other_process_cannot_rewrite_active_job_as_paused(env):
    store,_=env
    entered=asyncio.Event();release=asyncio.Event()
    async def handler(request):
        entered.set();await release.wait()
        return httpx.Response(200,content=b2body(),headers={'content-type':'text/html'})
    first=SourceCollector(store,http_transport=httpx.MockTransport(handler));first.limiter=Limiter()
    second=SourceCollector(store,http_transport=httpx.MockTransport(handler))
    job=await first.start([B2]);await entered.wait()
    with pytest.raises(ValueError,match='another running worker'):
        await second.pause(job['id'])
    assert second.get(job['id'])['status']=='running'
    release.set();await finish(first,job['id'])
    await first.close();await second.close()


def test_four_over_public_product_specs_are_metadata_not_source_cost():
    body=b'''<title>Brown Kraft Business Cards</title><script type="application/ld+json">
    {"@type":"Product","name":"Brown Kraft Business Cards","sku":"BKBC","offers":{"price":"12.00","priceCurrency":"USD"}}</script>
    <table><tr><th>Stock</th><td>18PT Brown Kraft</td></tr><tr><th>Colorspec</th><td>4/4</td><td>4/0</td></tr>
    <tr><td>Account</td><td>PRIVATE</td></tr></table><dl><dt>Turnaround Time</dt><dd>4 Business Days</dd></dl>
    <a href="/brown-kraft-postcards">Brown Kraft Postcards</a>'''
    d=parse_public_page(body,'https://4over.com/brown-kraft-business-cards')
    assert d['supplier']=='4over' and d['product_json']['sku']=='BKBC'
    assert d['observed_specifications']==[{'label':'Stock','values':['18PT Brown Kraft']},{'label':'Colorspec','values':['4/4','4/0']},{'label':'Turnaround Time','values':['4 Business Days']}]
    assert d['prices_verified'] is False and d['public_price_basis']=='public_listing_not_verified_authenticated_supplier_cost'
    assert 'PRIVATE' not in json.dumps(d)
    assert d['observed_links'][0]['url']=='https://4over.com/brown-kraft-postcards'


def test_soardist_explicit_shopify_product_retains_unavailable_variants():
    body=b'''<title>Stand</title><script id="ProductJson-main" type="application/json">
    {"id":123,"title":"Stand","options":["Size"],"variants":[
    {"id":1,"title":"Small","available":true,"price":12000},
    {"id":2,"title":"Large","available":false,"price":19000}],"customer":"PRIVATE"}</script>'''
    d=parse_public_page(body,SOAR)
    assert len(d['product_json']['variants'])==2
    assert d['product_json']['variants'][1]['available'] is False
    assert d['product_json']['variants'][1]['price']==19000
    assert d['prices_verified'] is False
    assert 'PRIVATE' not in json.dumps(d)


@pytest.mark.asyncio
async def test_four_over_exact_supplied_url_collected_with_same_safe_limiter(env):
    store,validations=env;calls=[]
    url='https://4over.com/brown-kraft-business-cards'
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200,content=b'<title>Brown Kraft Business Cards</title>',headers={'content-type':'text/html'})
    collector=SourceCollector(store,http_transport=httpx.MockTransport(handler));collector.limiter=Limiter()
    job=await collector.start([url]);result=await finish(collector,job['id'])
    assert calls==[url] and result['status']=='completed' and result['catalog_complete'] is False
    assert collector.limiter.grants==['https://4over.com']
    assert collector.get(job['id'],include_data=True)['pages'][0]['public_data']['product_json'] is None
    await collector.close()


def test_document_title_does_not_absorb_later_svg_titles():
    d=parse_public_page(b'<html><head><title>Actual Product</title></head><body><svg><title>icon-cart</title></svg><svg><title>icon-account</title></svg></body></html>',SOAR)
    assert d['title']=='Actual Product'


def test_b2_public_layout_links_retain_observed_paths_not_private_envelope():
    body=b2body(props={'page':{'id':29,'title':'Hardware Only','url':'/hardware-only','body':{'mode':'grid','layout':[
        {'type':'collection','items':[{'id':41,'title':'Hard Case Only','url':'/hard-case-only'}]},
        {'html':'<a href="/poster-stand-hardware-only">Poster Stand</a>'}]},'customer':'PRIVATE'},'customer':'PRIVATE'})
    d=parse_public_page(body,'https://www.b2sign.com/hardware-only')
    assert d['page']['id']==29 and 'PRIVATE' not in json.dumps(d)
    links={x['url']:x for x in d['observed_links']}
    assert links['https://www.b2sign.com/hard-case-only']['relation']=='public_page_block'
    assert 'https://www.b2sign.com/poster-stand-hardware-only' in links


def test_soardist_canonical_bound_duplicate_product_representations_and_variants():
    body=b'''<title>Example</title><link rel="canonical" href="https://soardistusa.com/products/example">
    <script type="application/ld+json">[
    {"@type":"Product","name":"Example","url":"https://soardistusa.com/products/example","productId":"123","offers":[{"sku":"206001","url":"https://soardistusa.com/products/example?variant=22","availability":"InStock"}]},
    {"@type":"Product","name":"Related","url":"https://soardistusa.com/products/other","productId":"999"}]</script>
    <script type="application/ld+json">{"@type":"Product","name":"Example","url":"https://soardistusa.com/products/example","productId":"123","sku":"206001"}</script>
    <script>var meta = {"product":{"id":123,"variants":[{"id":22,"sku":"206001","available":false}]},"customer":{"email":"PRIVATE"}};</script>
    <a href="/account/login">Login to view price</a>'''
    d=parse_public_page(body,SOAR)
    assert d['product_json']['productId']=='123'
    assert len(d['product_representations'])==3
    assert d['public_variant_metadata'][0]['variants'][0]['available'] is False
    assert 'PRIVATE' not in json.dumps(d) and 'Related' not in json.dumps(d['product_representations'])
    assert d['pricing_access']=='login_requested' and d['prices_verified'] is False
    assert 'price' not in d['product_json']['offers'][0]


def test_matching_url_with_conflicting_explicit_product_ids_is_unresolved():
    body=('<script type="application/ld+json">'+json.dumps([
        {'@type':'Product','url':SOAR,'productId':'1'},
        {'@type':'Product','url':SOAR,'productId':'2'}])+'</script>').encode()
    d=parse_public_page(body,SOAR)
    assert d['product_json'] is None and 'ambiguous_product_json_not_retained' in d['warnings']


def test_native_public_variant_options_are_retained_but_hidden_values_are_not():
    body=b'''<title>Product</title><form action="/cart/add"><select name="id"><option value="1">Small</option><option value="2" disabled>Large</option></select><input type="hidden" name="customer_id" value="PRIVATE"></form>'''
    d=parse_public_page(body,SOAR)
    assert d['forms'][0]['controls'][0]['options'][1]=={'value':'2','label':'Large','disabled':True,'selected':False}
    assert 'PRIVATE' not in json.dumps(d)


def test_fourover_public_spec_list_is_not_confused_with_nav_or_price_availability():
    body=b'''<title>Brown Kraft Business Cards</title><nav><p>PRIVATE</p></nav>
    <div class="page-title-wrapper product"><h1 class="page-title">Brown Kraft Business Cards</h1></div>
    <div class="product attribute description"><style>.x{color:red}</style><p>Natural fibers and optional white ink.</p></div>
    <ul id="product-attribute-specs-table"><li><span class="label">Stock</span><ul class="data"><li>18PT Uncoated Kraft</li></ul></li></ul>
    <div class="product media"><img src="/media/catalog/kraft.jpg" alt="Kraft cards"></div>'''
    d=parse_public_page(body,'https://4over.com/brown-kraft-business-cards')
    c=d['product_content']
    assert c['title']=='Brown Kraft Business Cards'
    assert c['description_paragraphs']==['Natural fibers and optional white ink.']
    assert c['specifications'][0]['values']==['18PT Uncoated Kraft']
    assert c['images'][0]['url']=='https://4over.com/media/catalog/kraft.jpg'
    assert c['option_combinations_verified'] is False
    assert c['pricing_verified'] is False and 'PRIVATE' not in json.dumps(c)

DPS = 'https://www.digital-print-solutions.com/low-tack-vinyl'


def dps_body(*, url=DPS, extra_product=False, login=True):
    product = {'@type': 'Product', 'name': 'Low Tack Vinyl', 'image': 'https://lag.azureedge.net/product-images/decal.jpg',
               'offers': [{'@type': 'Offer', 'price': '0.4100', 'priceCurrency': 'USD'}]}
    return (f'<title>Low Tack Vinyl</title><link rel="canonical" href="{url}">'
            f'<script type="application/ld+json">{json.dumps(product)}</script>'
            + ('<script type="application/ld+json">{"@type":"Product","name":"Other"}</script>' if extra_product else '')
            + '''<form method="post"><input type="hidden" name="__VIEWSTATE" value="PRIVATE_VIEWSTATE">
            <input type="hidden" id="csessid" value="PRIVATE_SESSION"><input name="UserCartId" value="PRIVATE_CART">
            <input type="hidden" id="EncodedProductOptions" value="PRIVATE_HIDDEN">
            <select id="CustProductHeightHolder"><option value="2.0">2 inches</option><option selected value="3.0">3 inches</option></select>
            <select id="CustProductWidthHolder"><option value="2.0">2 inches</option><option disabled value="120">120 inches</option></select>
            <div id="ProductOptionsHolder">Loading product options...</div></form>'''
            + ('<div id="disabledOverlayLoginText">Login Required</div><strong class="loginRequired">Please Login</strong>' if login else '')).encode()


def test_dps_public_sizes_retained_but_login_seo_offer_never_becomes_quote():
    result = parse_public_page(dps_body(), DPS)
    assert result['supplier'] == 'dps'
    assert result['pricing_access'] == 'login_requested'
    assert result['prices_verified'] is False
    assert result['product_json']['offers'][0]['price'] == '0.4100'
    content = result['product_content']
    assert content['quote_rows'] == 0 and content['pricing_verified'] is False
    assert content['option_combinations_verified'] is False and content['quantity_domain_complete'] is False
    assert [c['key'] for c in content['controls']] == ['height', 'width']
    assert content['controls'][0]['choices'][1] == {'value': '3.0', 'label': '3 inches', 'selected': True, 'disabled': False}
    assert content['controls'][1]['choices'][1]['disabled'] is True
    assert content['images'] == [{'url': 'https://lag.azureedge.net/product-images/decal.jpg'}]
    assert 'public_seo_offer_has_no_bound_configuration' in result['warnings']
    assert 'quantity_domain_not_observed' in result['warnings']
    assert 'product_addons_loaded_dynamically_not_observed' in result['warnings']
    assert 'PRIVATE' not in json.dumps(result) and result['forms'] == []


@pytest.mark.parametrize('body', [dps_body(url='https://www.digital-print-solutions.com/vinyl-decals/clear-decals'),
                                      dps_body(extra_product=True), b'<title>Missing product</title>'])
def test_dps_identity_must_bind_canonical_and_unique_product(body):
    with pytest.raises(ExtractionError) as error:
        parse_public_page(body, DPS)
    assert error.value.code == 'source_schema_unresolved'


def test_dps_numeric_bounds_and_actual_addons_preserve_empty_none_without_inventing_domain():
    body = dps_body(login=False).replace(b'<div id="ProductOptionsHolder">Loading product options...</div>', b'''
        <input id="CustQuantityList" type="number" min="1" max="10000" step="1" value="PRIVATE_CURRENT_VALUE">
        <div id="ProductOptionsHolder"><div class="option-group"><label>Lamination</label>
          <select><option value="" selected>None</option><option value="g">Gloss</option></select>
        </div></div>''')
    result = parse_public_page(body, DPS)
    assert result['pricing_access'] == 'not_established'
    controls = result['product_content']['controls']
    quantity = next(c for c in controls if c['key'] == 'quantity')
    assert (quantity['min'], quantity['max'], quantity['step']) == ('1', '10000', '1')
    assert 'value' not in quantity and 'PRIVATE' not in json.dumps(result)
    assert controls[-1]['label'] == 'Lamination' and controls[-1]['choices'][0]['value'] == ''
    assert result['product_content']['quantity_domain_complete'] is False
    assert 'product_addons_loaded_dynamically_not_observed' not in result['warnings']


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://www.digital-print-solutions.com/customer-login',
    'https://www.digital-print-solutions.com/pricing/123', 'https://www.digital-print-solutions.com/products/123/options',
    DPS + '?quantity=100', 'https://www.digital-print-solutions.com/Scripts/Frontend.Product.js'])
async def test_dps_collector_allows_only_reviewed_product_documents_no_api_or_login(env, url):
    store, validations = env
    collector = SourceCollector(store)
    with pytest.raises(ValueError):
        await collector.start([url])
    assert validations == [] and collector.list() == []
    await collector.close()


@pytest.mark.asyncio
async def test_dps_collection_stores_public_projection_without_following_login_or_price_routes(env):
    store, _ = env
    calls = []
    def handler(request):
        calls.append(str(request.url))
        assert request.method == 'GET' and 'cookie' not in request.headers and 'authorization' not in request.headers
        return httpx.Response(200, content=dps_body() + b'<a href="/pricing/123">Price API</a><a href="/customer-login">Login</a>',
                              headers={'content-type': 'text/html', 'set-cookie': 'session=PRIVATE'})
    collector = SourceCollector(store, http_transport=httpx.MockTransport(handler)); collector.limiter = Limiter()
    job = await collector.start([DPS]); result = await finish(collector, job['id'])
    assert result['status'] == 'completed' and result['http_attempts'] == 1 and calls == [DPS]
    assert collector.limiter.grants == ['https://www.digital-print-solutions.com']
    data = collector.get(job['id'], include_data=True)['pages'][0]['public_data']
    assert data['pricing_access'] == 'login_requested' and data['observed_links'] == []
    assert data['product_content']['quote_rows'] == 0
    assert b'PRIVATE' not in store.get_object(result['pages'][0]['evidence']['object_path'])
    await collector.close()
