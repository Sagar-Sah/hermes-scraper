"""Safe reference projection only: intercepted local Chromium, no supplier."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from crawler.price_contract import SOURCE_CONTRACT
from crawler.printograph_source_references import (
    SOURCE_URLS, navigation_url, project_references, script_url,
)
from test_product_diagnosis import PRODUCT, completed, manager

ORDER = "https://static.printograph.com/tl/cli-app/dist/production/js/orderForm.js?v=0109_20261022_123456-101"
COMMON = ORDER.replace("orderForm.js", "chunk-common.js")
SOURCE = "https://www.printograph.com/products/special-shapes/order"


@pytest.mark.parametrize("suffix", ["", "?v=1", "?ver=1.2.3", "?version=0109_20261022_123456-101"])
def test_safe_observed_version_is_preserved(suffix):
    url = ORDER.split("?")[0] + suffix
    assert script_url(url) == ("order_form", url)
    assert script_url(url.replace("orderForm.js", "common.js"))[0] == "common"


@pytest.mark.parametrize("url", [
    ORDER.replace("https:", "http:"), ORDER.replace("static.printograph.com", "evil.invalid"),
    ORDER.replace("static.printograph.com", "static.printograph.com.evil.invalid"),
    ORDER.replace("static.printograph.com", "user:private@static.printograph.com"),
    ORDER.replace("static.printograph.com", "@static.printograph.com"),
    ORDER.replace("static.printograph.com", "static.printograph.com:443"),
    ORDER.replace("orderForm.js", "other.js"), ORDER + "&token=PRIVATE",
    ORDER + "&v=123", ORDER.replace("?v=", "?%76="), ORDER.replace("?v=", "?auth="),
    ORDER + "#PRIVATE", ORDER.replace("/tl/", "/../tl/"),
    ORDER.replace("/tl/", "/%2e%2e/tl/"), ORDER.replace("/tl/", "/tl\\"),
    ORDER + "\n", ORDER + "\t", ORDER + " ",
    ORDER + "\x00", ORDER + "\x7f", ORDER + "é", ORDER.replace("?v=", "?v=PRIVATE-"),
    ORDER.replace("?v=", "?v=") + "x" * 70,
])
def test_script_rejects_credentials_private_queries_and_normalization(url):
    with pytest.raises(ValueError):
        script_url(url)


@pytest.mark.parametrize('filename,role', [('orderForm.js','order_form'),('chunk-common.js','common')])
def test_exact_static_protocol_relative_src_normalizes_to_https(filename, role):
    raw = '//static.printograph.com/tl/cli-app/dist/production/js/' + filename + '?v=0109_20261007_202205-101'
    assert script_url(raw) == (role, 'https:' + raw)


@pytest.mark.parametrize('raw', [
    '//evil.invalid/js/orderForm.js?v=1', '//static.printograph.com.evil.invalid/js/orderForm.js?v=1',
    '//user@static.printograph.com/js/orderForm.js?v=1', '//static.printograph.com:443/js/orderForm.js?v=1',
    '///static.printograph.com/js/orderForm.js?v=1', '//static.printograph.com/js/../orderForm.js?v=1',
    '//static.printograph.com/js/orderForm.js?token=private', '//static.printograph.com/js/orderForm.js?v=1#private',
    '//static.printograph.com/js/orderForm.js?v=1\n', '//static.printograph.com\\js/orderForm.js?v=1',
])
def test_protocol_relative_static_normalization_keeps_existing_guards(raw):
    with pytest.raises(ValueError):
        script_url(raw)


def test_protocol_relative_navigation_is_still_rejected():
    with pytest.raises(ValueError):
        navigation_url('//www.printograph.com/products/acrylic-boards/order')


@pytest.mark.parametrize("slug,family", [
    ("acrylic-signs", "acrylic"), ("cardboard-signs", "cardboards"),
    ("corrugated-boards", "corrugated"), ("staggered-cut-flyers", "staggered"),
    ("presentation-folders", "folders"), ("catalogs", "catalogs"), ("booklets", "catalogs"),
])
def test_only_relevant_official_product_navigation(slug, family):
    url, found = navigation_url("/products/" + slug + "/order")
    assert url == "https://www.printograph.com/products/" + slug + "/order"
    assert family in found


@pytest.mark.parametrize("url", [
    "/account/catalogs", "/products/catalogs/account", "/products/catalogs/checkout/order",
    "/products/catalogs/order?token=PRIVATE", "/products/catalogs/order?utm_source=tracking",
    "/products/catalogs/order#PRIVATE", "https://evil.invalid/products/catalogs/order",
    "https://user:PRIVATE@www.printograph.com/products/catalogs/order",
    "https://@www.printograph.com/products/catalogs/order", "http://www.printograph.com/products/catalogs/order",
    "//www.printograph.com/products/catalogs/order", "/products/postcards/order",
    "/products/catalogs/../order", "/products/catalogs/../../products/acrylic-signs/order",
    "/products/catalogs/%2e%2e/order", "https://www.printograph.com:443/products/catalogs/order",
    "/products\\catalogs/order", "/products/catalogs/order\x00",
])
def test_navigation_rejects_account_queries_and_path_laundering(url):
    with pytest.raises(ValueError):
        navigation_url(url)


def raw(scripts=None, navigation=None, **kwargs):
    return dict(source_url_match=True, scripts=scripts or [], navigation=navigation or [],
                scripts_truncated=False, navigation_truncated=False, **kwargs)


def test_duplicate_identical_scripts_are_deduplicated_conflicting_versions_rejected():
    result = project_references(raw([ORDER, ORDER, COMMON]))
    assert len(result["scripts"]) == 2
    assert result["script_roles_missing"] == []
    with pytest.raises(ValueError, match="source_script_version_ambiguous"):
        project_references(raw([ORDER, ORDER.replace("123456", "999999"), COMMON]))
    with pytest.raises(ValueError, match="source_script_version_ambiguous"):
        project_references(raw([COMMON, COMMON.replace("chunk-common.js", "common.js")]))


@pytest.mark.parametrize("update", [
    {"scripts": [ORDER] * 257}, {"navigation": ["/products/catalogs/order"] * 65},
    {"source_url_match": False}, {"scripts_truncated": True}, {"scripts_truncated": None},
    {"scripts": "PRIVATE"}, {"navigation": {"account": "PRIVATE"}},
])
def test_raw_projection_bounds_fail_closed(update):
    value = raw(); value.update(update)
    with pytest.raises(ValueError):
        project_references(value)


async def setup_page(m, pool, url=SOURCE):
    pool.html[url.split(".com", 1)[1]] = PRODUCT
    record = await completed(m, url)
    page = m._pages[record["id"]]
    await page.evaluate("""({order,common})=>{
      const add=(tag,key,value)=>{const e=document.createElement(tag);if(tag==='script')e.type='application/json';e.setAttribute(key,value);document.body.append(e);return e;};
      add('script','src',order);add('script','src',common);add('script','src',order);
      add('script','src',order+'&token=PRIVATE_TOKEN');
      add('script','src','https://evil.invalid/orderForm.js');
      const inline=document.createElement('script');inline.type='application/json';inline.textContent='PRIVATE_INLINE_ACCOUNT';document.body.append(inline);
      const input=document.createElement('input');input.value='PRIVATE_PASSWORD';document.body.append(input);
      for(const slug of ['acrylic-signs','cardboards','corrugated-boards','staggered-cut-flyers','folders','catalogs']){const e=add('a','href','/products/'+slug+'/order');e.textContent='PRIVATE_LABEL';}
      add('a','href','/products/catalogs/order?nonce=PRIVATE_NONCE');
      add('a','href','/products\\\\catalogs-PRIVATE_LAUNDER/order');
    }""", {"order": ORDER, "common": COMMON})
    return record, page


@pytest.mark.asyncio
async def test_owned_dom_protocol_relative_static_refs_only_no_requests_or_state_change(manager):
    m, pool = manager
    pool.html[SOURCE.split('.com', 1)[1]] = PRODUCT
    record = await completed(m, SOURCE)
    page = m._pages[record['id']]
    await page.evaluate('''() => {
      for(const filename of ['chunk-common.js','orderForm.js']) {
        const e=document.createElement('script');e.type='application/json';
        e.setAttribute('src','//static.printograph.com/tl/cli-app/dist/production/js/'+filename+'?v=0109_20261007_202205-101');document.body.append(e);
      }
      for(const href of ['//www.printograph.com/products/folders/order','/products/cardboards/order']) {
        const e=document.createElement('a');e.setAttribute('href',href);document.body.append(e);
      }
    }''')
    m._bundle_hashes[record['id']] = {key: {value} for key,value in SOURCE_CONTRACT.items()}
    before_requests, before_record = list(pool.requests), m.get(record['id'])
    result = await m.read_source_references(record['id'], record['revision'], SOURCE)
    assert result['scripts'] == [
        {'role':'common','url':'https://static.printograph.com/tl/cli-app/dist/production/js/chunk-common.js?v=0109_20261007_202205-101'},
        {'role':'order_form','url':'https://static.printograph.com/tl/cli-app/dist/production/js/orderForm.js?v=0109_20261007_202205-101'},
    ]
    assert result['navigation'] == [{'url':'https://www.printograph.com/products/cardboards/order','families':['cardboards']}]
    assert result['static_asset_hash_comparison_ready'] is True
    assert result['source_fetches_initiated'] == 0 and result['price_verified'] is False
    assert pool.requests == before_requests and m.get(record['id']) == before_record


@pytest.mark.asyncio
@pytest.mark.parametrize("url", sorted(SOURCE_URLS))
async def test_owned_current_page_projection_has_no_network_state_or_private_reads(manager, url):
    m, pool = manager
    record, _ = await setup_page(m, pool, url)
    m._bundle_hashes[record["id"]] = {k: {v} for k, v in SOURCE_CONTRACT.items()}
    before = list(pool.requests); saved = m.get(record["id"])
    result = await m.read_source_references(record["id"], record["revision"], url)
    assert result["status"] == "observed" and result["revision"] == record["revision"]
    assert result["source_bundle_verified"] is True
    assert result["static_asset_hash_comparison_ready"] is True
    assert len(result["scripts"]) == 2 and len(result["navigation"]) == 6
    assert all(not result[k] for k in ["account_or_input_fields_emitted", "price_verified", "page_contents_emitted",
               "asset_bodies_returned", "static_asset_fetch_authorized", "diagnosis_or_selection_changed"])
    assert result["source_fetches_initiated"] == 0
    assert "PRIVATE" not in json.dumps(result)
    assert pool.requests == before and m.get(record["id"]) == saved


@pytest.mark.asyncio
async def test_wrong_revision_and_source_rejected_before_dom_evaluation(manager):
    m, pool = manager; record, page = await setup_page(m, pool)
    original = page.evaluate; page.evaluate = AsyncMock(side_effect=RuntimeError("MUST_NOT_EVALUATE"))
    try:
        for revision, url, error in [
            (False, SOURCE, "revision"), (record["revision"] - 1, SOURCE, "revision"),
            (record["revision"], "https://evil.invalid/", "source_url_unsupported"),
            (record["revision"], SOURCE + "?token=PRIVATE", "source_url_unsupported"),
            (record["revision"], SOURCE.replace("special-shapes", "catalogs"), "source_url_mismatch"),
        ]:
            with pytest.raises(ValueError, match=error):
                await m.read_source_references(record["id"], revision, url)
        page.evaluate.assert_not_called()
    finally:
        page.evaluate = original


@pytest.mark.asyncio
async def test_conflicting_current_dom_versions_and_redirected_page_rejected(manager):
    m, pool = manager; record, page = await setup_page(m, pool)
    await page.evaluate("""url=>{const e=document.createElement('script');e.type='application/json';e.src=url;document.body.append(e)}""", ORDER.replace("123456", "999999"))
    before = list(pool.requests)
    with pytest.raises(ValueError, match="source_script_version_ambiguous"):
        await m.read_source_references(record["id"], record["revision"], SOURCE)
    assert pool.requests == before
    await page.goto("https://www.printograph.com/user/login")
    with pytest.raises(ValueError, match="source_url_mismatch"):
        await m.read_source_references(record["id"], record["revision"], SOURCE)


@pytest.mark.asyncio
async def test_conflicting_observed_body_hashes_are_never_silently_dropped(manager):
    m, pool = manager; record, _ = await setup_page(m, pool)
    m._bundle_hashes[record["id"]] = {"common_sha256": {"a" * 64, "b" * 64}}
    before = list(pool.requests)
    with pytest.raises(ValueError, match="source_script_hash_ambiguous"):
        await m.read_source_references(record["id"], record["revision"], SOURCE)
    assert pool.requests == before


@pytest.mark.asyncio
async def test_diagnosis_background_default_explicit_focus_and_supplier_login_unchanged(manager):
    m, pool = manager; original = m._new_page; pages = {}
    async def tracked(owner, origin, hosts):
        page = await original(owner, origin, hosts)
        page.bring_to_front = AsyncMock()
        pages[owner] = page
        return page
    m._new_page = tracked
    quiet = await m.start(SOURCE)
    await m._tasks[quiet["id"]]
    pages[quiet["id"]].bring_to_front.assert_not_called()
    foreground = await m.start(SOURCE, focus=True)
    await m._tasks[foreground["id"]]
    pages[foreground["id"]].bring_to_front.assert_awaited_once()
    await m.open_supplier("printograph")
    pages["supplier:printograph"].bring_to_front.assert_awaited_once()
    before = list(pool.requests)
    for value in [1, 0, "false", None]:
        with pytest.raises(ValueError, match="invalid_diagnosis_focus"):
            await m.start(SOURCE, focus=value)
    assert pool.requests == before


@pytest.mark.asyncio
async def test_revision_change_during_projection_cannot_return_stale_references(manager):
    m, pool = manager; record, page = await setup_page(m, pool)
    original = page.evaluate
    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        m._records[record["id"]]["revision"] += 1
        return result
    page.evaluate = changed
    with pytest.raises(ValueError, match="revision"):
        await m.read_source_references(record["id"], record["revision"], SOURCE)


@pytest.mark.asyncio
async def test_pending_observed_hashes_awaited_changed_hashes_not_verified(manager):
    m, pool = manager; record, _ = await setup_page(m, pool)
    async def observed_response():
        await asyncio.sleep(.01)
        m._bundle_hashes[record["id"]] = {"common_sha256": {"b" * 64}, "order_form_sha256": {"a" * 64}}
    task = asyncio.create_task(observed_response()); m._bundle_tasks[record["id"]] = {task}
    before = list(pool.requests)
    result = await m.read_source_references(record["id"], record["revision"], SOURCE)
    assert result["source_bundle_verified"] is False and task.done()
    assert result["loaded_contract"] == {"common_sha256": "b" * 64, "order_form_sha256": "a" * 64}
    assert pool.requests == before
