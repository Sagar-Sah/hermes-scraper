"""Bounded anonymous public-display sampling; no cart or supplier operations."""
from __future__ import annotations

import asyncio
import hashlib
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from .product_companion_area_pricing import (
    SCRIPT_SHA256, breakpoint_coverage, parse_public_rule,
    retail_prediction, validate_selection,
)
from .product_companion_fields import CAPTURE_RENDERED_FIELDS_JS, normalize_field_inventory


STOREFRONT_URL = "https://printitprints.com/shop/grand-format/signs-banners/signs/rigid-signs/4mm-coroplast-signs/"
MAX_CASES = 16


def default_cases() -> list[dict]:
    base = {"product_id": "6383", "width": 24, "height": 18, "colorspec": "4/0",
            "quantity": 1, "flute_direction": "vertical", "stakes_per_sign": 0}
    cases = [{"case_id": f"front-q{q}", "selection": {**base, "quantity": q}, "expected": "price"}
             for q in [1, 5, 6, 25, 26, 50, 51, 75, 76, 499]]
    cases.extend([
        {"case_id": "both-q6-one-stake", "selection": {**base, "colorspec": "4/4", "quantity": 6, "stakes_per_sign": 1}, "expected": "price"},
        {"case_id": "both-q6-two-stakes", "selection": {**base, "colorspec": "4/4", "quantity": 6, "stakes_per_sign": 2}, "expected": "price"},
        {"case_id": "height36", "selection": {**base, "colorspec": "4/4", "width": 36, "height": 36}, "expected": "price"},
        {"case_id": "height46", "selection": {**base, "colorspec": "4/4", "width": 36, "height": 46}, "expected": "price"},
        {"case_id": "portrait-stake-blocked", "selection": {**base, "width": 18, "height": 24, "stakes_per_sign": 1}, "expected": "blocked_orientation"},
        {"case_id": "horizontal-stake-blocked", "selection": {**base, "flute_direction": "horizontal", "stakes_per_sign": 1}, "expected": "blocked_orientation"},
    ])
    return cases


def _plan(cases) -> list[dict]:
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError("area_sample_plan_out_of_bounds")
    identifiers = set()
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {"case_id", "selection", "expected"}
                or not isinstance(case["case_id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", case["case_id"])
                or case["case_id"] in identifiers or not isinstance(case["selection"], dict)
                or case["expected"] not in {"price", "blocked_orientation"}):
            raise ValueError("area_sample_plan_invalid")
        identifiers.add(case["case_id"])
    return cases


def _request_allowed(method: str, url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if (method not in {"GET", "HEAD"} or parsed.scheme != "https"
                or parsed.hostname not in {"printitprints.com", "www.printitprints.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            return False
        if set(parse_qs(parsed.query)) & {"add-to-cart", "remove_item", "undo_item", "wc-ajax", "wc-api", "action"}:
            return False
        return not re.search(r"/(?:cart|checkout|my-account|wp-admin|wp-login\.php|wc-api)(?:/|$)|/wp-json/wc/(?:store/v\d+/cart|v\d+/orders)(?:/|$)", parsed.path)
    except ValueError:
        return False


CASE_READY_JS = r"""wanted => {
 const read=n=>document.querySelector('form.cart [name="'+n+'"]')?.value;
 return ['width','height','run_size','flute','stakes_per_sign'].every(k=>String(read('pigf_'+k))===String(wanted[k]))
  &&String(read('pigf_colorspec')||'').includes(wanted.colorspec)
  &&!!document.querySelector('[data-pigf-total]')?.textContent?.trim();
}"""

OBSERVE_CASE_JS = r"""() => {
 const read=n=>String(document.querySelector('form.cart [name="'+n+'"]')?.value||'');
 const node=document.querySelector('[data-pigf-total]'),publicNode=document.querySelector('[data-pigf-public-price]');
 const visible=n=>{if(!n)return false;for(let p=n;p;p=p.parentElement){const s=getComputedStyle(p);if(p.hidden||s.display==='none'||s.visibility==='hidden'||p.getAttribute('aria-hidden')==='true')return false;}return !!n.getClientRects().length;};
 const warning=document.querySelector('[data-pigf-validation]'),button=document.querySelector('form.cart #add_to_cart_button,form.cart .single_add_to_cart_button');
 return {total_text:String(node?.textContent||'').trim(),total_visible:visible(node),public_price_text:String(publicNode?.textContent||'').trim(),public_price_visible:visible(publicNode),
  button_disabled:button?!!button.disabled:null,warning_visible:visible(warning),warning_text:visible(warning)?String(warning.textContent||'').trim().slice(0,300):'',
  retained:{width:read('pigf_width'),height:read('pigf_height'),colorspec:read('pigf_colorspec'),quantity:read('pigf_run_size'),flute_direction:read('pigf_flute'),stakes_per_sign:read('pigf_stakes_per_sign')}};
}"""


def _display_money(value: str) -> str | None:
    match = re.fullmatch(r"\$\s*((?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)\.[0-9]{2})", value.strip())
    return match[1].replace(",", "") if match else None


def _result(reason: str, **values) -> dict:
    return {"status": "unknown", "reason": reason, "scope": "bounded_public_display_only",
            "overall_product_status": "unknown", "backend_status": "unknown",
            "current_supplier_prices": "unknown", "wordpress_writes": 0,
            "cart_order_or_upload_actions": 0, "supplier_requests": 0,
            "samples": [], "field_inventory": None, "area_rule": None,
            "coverage": None, "observations": {}, **values}


async def _select(page, selector: str, value: str) -> None:
    await page.locator(selector).first.select_option(value, timeout=4000)


async def _sample_page(page, pair, cases, responses, result):
    response = await page.goto(pair["storefront_url"], wait_until="domcontentloaded", timeout=30000)
    if response is None or response.status != 200 or page.url != STOREFRONT_URL:
        raise ValueError("area_rendered_storefront_unavailable")
    await page.wait_for_selector("[data-pigf-coroplast] [data-pigf-stakes]", state="attached", timeout=10000)
    inventory = normalize_field_inventory(await page.evaluate(CAPTURE_RENDERED_FIELDS_JS))
    if inventory["product_id"] != "6383" or inventory["url"] != STOREFRONT_URL:
        raise ValueError("area_rendered_identity_changed")
    result["field_inventory"] = inventory
    script_url = urljoin(page.url, await page.locator("#print-it-grand-format-configurator-js").get_attribute("src") or "")
    if not _request_allowed("GET", script_url) or script_url not in responses:
        raise ValueError("area_loaded_script_evidence_missing")
    script = await asyncio.wait_for(responses[script_url].body(), timeout=10)
    inline = (await page.locator("#print-it-grand-format-configurator-js-extra").text_content() or "").encode()
    rule = parse_public_rule(script, inline, expected_script_sha256=SCRIPT_SHA256,
                             expected_config_sha256=hashlib.sha256(inline).hexdigest())
    result["area_rule"] = rule
    result["observations"]["loaded_public_script"] = {"url": script_url, "sha256": hashlib.sha256(script).hexdigest(), "bytes": len(script)}
    result["observations"]["loaded_inline_config_sha256"] = hashlib.sha256(inline).hexdigest()
    if rule.get("public_rule_status") != "verified":
        raise ValueError("area_public_rule_not_verified")
    for case in cases:
        validity = validate_selection(rule, case["selection"])
        if case["expected"] == "price" and not validity.get("public_selection_valid"):
            raise ValueError("area_sample_selection_unverified")
        if case["expected"] == "blocked_orientation" and validity.get("selection_error") != "stake_orientation":
            raise ValueError("area_sample_block_expectation_unverified")
    for case in cases:
        selected = case["selection"]
        await _select(page, 'form.cart select[name="print_option_stock"],form.cart select[name="print_option_paper_stock"]', "4mm White Coroplast")
        await _select(page, 'form.cart select[name="print_option_colorspec"],form.cart select[name="print_option_printing"]',
                      "4/0 (4 color front)" if selected["colorspec"] == "4/0" else "4/4 (4 color both sides)")
        await _select(page, 'form.cart select[name="print_option_width"]', str(selected["width"]))
        await _select(page, 'form.cart select[name="print_option_height"]', str(selected["height"]))
        await _select(page, "[data-pigf-flute]", selected["flute_direction"])
        await _select(page, "[data-pigf-stakes]", str(selected["stakes_per_sign"]))
        await page.locator("[data-pigf-coroplast-quantity]").fill(str(selected["quantity"]), timeout=4000)
        await page.locator("[data-pigf-coroplast-quantity]").dispatch_event("change")
        await page.wait_for_function(CASE_READY_JS, arg={"width": selected["width"], "height": selected["height"],
            "colorspec": selected["colorspec"], "run_size": selected["quantity"],
            "flute": selected["flute_direction"], "stakes_per_sign": selected["stakes_per_sign"]}, timeout=5000)
        observed = await page.evaluate(OBSERVE_CASE_JS)
        sample = {"case_id": case["case_id"], "selection": selected, "expected": case["expected"],
                  "observed": observed, "backend_acceptance": "unknown", "current_supplier_prices": "unknown"}
        if case["expected"] == "price":
            prediction = retail_prediction(rule, selected)
            actual, top = _display_money(observed["total_text"]), _display_money(observed["public_price_text"])
            sample.update(expected_retail=prediction["predicted_public_js_total"], displayed_retail=actual,
                          displayed_top_retail=top, historical_backend_height_conflict=prediction["historical_height_conflict"],
                          purchase_button_status="observed_only_other_requirements_unchecked")
            sample["match"] = (observed["total_visible"] and observed["public_price_visible"]
                and actual == sample["expected_retail"] and top == actual)
        else:
            sample["match"] = observed["button_disabled"] is True and observed["warning_visible"] and bool(observed["warning_text"])
        result["samples"].append(sample)
    final_inventory = normalize_field_inventory(await page.evaluate(CAPTURE_RENDERED_FIELDS_JS))
    if final_inventory["product_id"] != "6383" or final_inventory["url"] != STOREFRONT_URL:
        raise ValueError("area_final_field_identity_changed")
    result["observations"]["final_field_inventory"] = final_inventory
    result["coverage"] = breakpoint_coverage(rule, [case["selection"] for case in cases])
    result.update(status="match" if all(sample["match"] for sample in result["samples"]) else "mismatch",
                  reason="declared_public_display_cases_only", declared_case_count=len(cases))


async def sample_coroplast_public_storefront(pair: dict, *, cases=None, playwright_factory=None) -> dict:
    """Reuse one anonymous page for at most16 declared public-display cases.

    No CDP, saved profile, authentication, form submission, Add to Cart, upload,
    or supplier navigation is supported. Routing blocks mutating requests and
    non-PrintItPrints origins as an additional read-only boundary.
    """
    result = _result("public_render_not_completed")
    if (not isinstance(pair, dict) or str(pair.get("product_id")) != "6383"
            or pair.get("storefront_url") != STOREFRONT_URL):
        return _result("area_render_pair_scope_unverified")
    try:
        declared = _plan(default_cases() if cases is None else cases)
    except ValueError:
        return _result("area_sample_plan_invalid")
    if playwright_factory is None:
        from playwright.async_api import async_playwright
        playwright_factory = async_playwright
    blocked = {"requests": 0, "mutating_requests": 0, "external_origin_requests": 0}
    async def route_request(route):
        request = route.request
        if _request_allowed(request.method, request.url):
            await route.continue_()
        else:
            blocked["requests"] += 1
            blocked["mutating_requests"] += request.method not in {"GET", "HEAD"}
            blocked["external_origin_requests"] += urlsplit(request.url).hostname not in {"printitprints.com", "www.printitprints.com"}
            await route.abort()
    try:
        async with playwright_factory() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers="block", ignore_https_errors=False)
                await context.route("**/*", route_request)
                page = await context.new_page()
                responses = {}
                def observed_response(response):
                    if response.request.resource_type == "script" and "print-it-grand-format-configurator" in response.url and len(responses) < 4:
                        responses[response.url] = response
                page.on("response", observed_response)
                await asyncio.wait_for(_sample_page(page, pair, declared, responses, result), timeout=90)
            finally:
                await browser.close()
    except Exception as exc:
        # Only exception class/fixed reason; no page/account/provider bodies.
        result.update(status="unknown", reason=str(exc) if isinstance(exc, ValueError) and str(exc).startswith("area_") else type(exc).__name__)
    result["observations"]["blocked_requests"] = blocked
    result["observations"]["navigation_count_limit"] = 1
    result["observations"]["anonymous_context"] = True
    return result
