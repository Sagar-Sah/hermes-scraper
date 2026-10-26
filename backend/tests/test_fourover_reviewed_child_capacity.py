"""Reviewed large declarations with fake transport only; never supplier requests."""
import json

import pytest

from crawler.fourover_bulk import (
    ChildConfigurationError, child_configuration_limit, inspect_child_configurations,
)
from crawler.fourover_options import inspect_option_contract
from crawler.fourover_child_prices import FourOverChildPriceCollector
import test_fourover_child_prices as transport


collector = transport.collector
SCOPE = transport.SCOPE
SNAPSHOT = transport.SNAPSHOT
URLS = ("https://4over.com/16pt-postcards", "https://4over.com/specialty-folds-brochures")


def large_document(count):
    ids = [str(n) for n in range(10001, 10001 + count)]
    config = {
        "attributes": {
            "211": {"id": "211", "code": "size", "label": "Size", "options": [
                {"id": str(index + 1), "label": "Source size " + child, "products": [child]}
                for index, child in enumerate(ids)]},
            "197": {"id": "197", "code": "stock", "label": "Stock", "options": [
                {"id": "1", "label": "Source stock", "products": ids}]},
        },
        "index": {child: {"211": str(index + 1), "197": "1"}
                  for index, child in enumerate(ids)},
        "accountToken": "PRIVATE-NEVER-PERSIST",
    }
    options = ('<script type="text/x-magento-init">' + json.dumps({
        "#product_addtocart_form": {"configurable": {"spConfig": config}}}) + '</script>').encode()
    return ids, transport.document(ids) + options


@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize("count", [2314, 2901, 3000])
def test_reviewed_declarations_and_reverse_membership_are_complete_at_bounded_capacity(url, count):
    ids, html = large_document(count)
    children, diagnostic = inspect_child_configurations(html.decode(), source_url=url)
    assert len(children) == count and diagnostic["maximum_children"] == 3000
    report = inspect_option_contract(html.decode(), children, source_url=url)
    assert report["binding_complete"] and len(report["rows"]) == count
    assert {row["child_id"] for row in report["rows"]} == set(ids)
    assert len(report["attributes"][0]["options"]) == count
    assert len(report["attributes"][1]["options"][0]["products"]) == count
    assert not any(report[key] for key in ("source_data_complete", "catalog_complete", "final_eligible"))
    assert "PRIVATE-NEVER-PERSIST" not in json.dumps(report)


@pytest.mark.parametrize("url", [None, "https://4over.com/all-inclusive-postcards",
    "https://4over.com/z-fold-brochures", "https://other.test/16pt-postcards",
    "http://4over.com/16pt-postcards", "https://4over.com.evil.test/16pt-postcards",
    "https://4over.com/16pt-postcards?override=1", "https://4over.com/16pt-postcards#x",
    "https://u:p@4over.com/16pt-postcards", "https://4over.com/16pt-postcards//",
    "https://@4over.com/16pt-postcards", "https://:@4over.com/16pt-postcards",
    "https://4over.com/16pt-\npostcards", "https://4over.com/16pt-\rpostcards",
    "https://4over.com/16pt-\tpostcards", "https://4over.com/16pt-\x00postcards",
    "https://4over.com/16pt-\x7fpostcards",
    "https://4over.com/16pt-postcards/other", "https://4over.com/16pt-postcards:bad",
    "https://4over.com:bad/16pt-postcards", " https://4over.com/16pt-postcards"])
def test_unreviewed_sources_cannot_raise_default_limit(url):
    _, html = large_document(2001)
    assert child_configuration_limit(url) == 2000
    with pytest.raises(ChildConfigurationError) as error:
        inspect_child_configurations(html.decode(), source_url=url)
    assert error.value.code == "child_declaration_limit"
    assert error.value.diagnostic["maximum_children"] == 2000
    children = [{"child_id": str(n)} for n in range(1, 2002)]
    assert inspect_option_contract(html.decode(), children, source_url=url)["binding_complete"] is False


@pytest.mark.parametrize("url", URLS)
def test_large_reviewed_sources_still_reject_3001_and_undeclared_membership(url):
    _, html = large_document(3001)
    with pytest.raises(ChildConfigurationError) as error:
        inspect_child_configurations(html.decode(), source_url=url)
    assert error.value.code == "child_declaration_limit"
    assert error.value.diagnostic["declared_child_count"] == 3001
    ids, html = large_document(2314)
    children, _ = inspect_child_configurations(html.decode(), source_url=url)
    tampered = html.decode().replace('"products": ["10001"]', '"products": ["999999"]', 1)
    report = inspect_option_contract(tampered, children, source_url=url)
    assert report["reason_code"] == "duplicate_or_unknown_option_binding"
    assert report["rows"] == [] and not report["binding_complete"]


def use_reviewed_url(monkeypatch, url):
    # Existing fake transport only. No real DNS, browser or supplier is touched.
    monkeypatch.setattr(transport, "URL", url)
    monkeypatch.setattr(transport.Response, "url", url)


@pytest.mark.asyncio
@pytest.mark.parametrize("url,count", tuple(zip(URLS, (2314, 2901))))
async def test_recovery_from_old_limit_retains_checkpoint_and_successful_price_cache(collector, monkeypatch, url, count):
    import crawler.fourover_child_prices as module
    service, state = collector
    use_reviewed_url(monkeypatch, url)
    ids, state["document_body"] = large_document(count)
    with monkeypatch.context() as old_runtime:
        old_runtime.setattr(module, "inspect_child_configurations",
            lambda html, **kwargs: inspect_child_configurations(html))
        blocked = await service.collect(SNAPSHOT, url, [], request_budget=1, scope=SCOPE)
    assert blocked["error"]["code"] == "child_declaration_limit"
    assert state["documents"] == 1 and state["children_sent"] == []
    recovered = await service.collect(SNAPSHOT, url, ids[:2], request_budget=3, scope=SCOPE)
    assert recovered["requests_attempted"] == 4
    assert recovered["option_contract"]["binding_complete"] is True
    assert len(recovered["declared_children"]) == count
    assert state["children_sent"] == ids[:2]
    assert len(service.limiter.acquired) == len(service.limiter.released) == 4
    assert all(item == ("https://4over.com", 0.5, 1) for item in service.limiter.acquired)
    restarted = FourOverChildPriceCollector(service.store, transport.Pool(state))
    restarted.limiter = transport.Limiter()
    before = (state["documents"], list(state["children_sent"]))
    cached = await restarted.collect(SNAPSHOT, url, ids[:2], request_budget=1, scope=SCOPE)
    assert cached["requests_this_call"] == 0 and cached["cache_hits_this_call"] == 2
    assert before == (state["documents"], state["children_sent"])
    assert restarted.limiter.acquired == [] and restarted.limiter.released == []
    result = await restarted.collect(SNAPSHOT, url, ids[:3], request_budget=2, scope=SCOPE)
    assert result["requests_this_call"] == 2 and result["cache_hits_this_call"] == 2
    assert state["children_sent"] == ids[:3]
    assert len(restarted.limiter.acquired) == len(restarted.limiter.released) == 2
    assert not any(result[key] for key in ("catalog_complete", "final_eligible", "services_verified", "supplier_total_verified"))
    assert "PRIVATE-NEVER-PERSIST" not in json.dumps(result)


@pytest.mark.asyncio
async def test_reviewed_capacity_does_not_bypass_request_membership_or_response_bounds(collector, monkeypatch):
    service, state = collector
    url = URLS[0]
    use_reviewed_url(monkeypatch, url)
    _, state["document_body"] = large_document(2314)
    rejected = await service.collect(SNAPSHOT, url, ["999999"], request_budget=2, scope=SCOPE)
    assert rejected["error"]["code"] == "undeclared_child" and state["children_sent"] == []
    state["document_body"] += b" " * (2 * 1024 * 1024)
    oversized = await service.collect("b" * 32, url, [], request_budget=1, scope=SCOPE)
    assert oversized["error"]["code"] == "response_too_large"
    assert len(service.limiter.acquired) == len(service.limiter.released) == 2
