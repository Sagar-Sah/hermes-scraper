"""Native DPS calculator evidence for one current configuration, without orders.

The pinned public Frontend.Product.js supplies the route/DOM contract. Only
browser-originated GETs are observed; this module never copies session headers
or replays a pricing API. Quantity domains are not claimed to be price breaks.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

from .transport import validate_url

HOSTS = {"digital-print-solutions.com", "www.digital-print-solutions.com"}
ASSET_HOSTS = HOSTS | {"cdnjs.cloudflare.com", "lag.azureedge.net"}
PRODUCT_PATHS = {"/low-tack-vinyl", "/vinyl-decals/3-mil-high-tack-vinyl-material",
                 "/vinyl-decals/crack-and-peel-stickers", "/vinyl-decals/clear-decals",
                 "/vinyl-decals/white-reflective", "/vinyl-decals/calendered-vinyl",
                 "/vinyl-decals/hi-performance"}
SCRIPT_PATH = "/Scripts/Frontend.Product.js"
SCRIPT_SHA256 = "66305dcc6a737f1c7006e15b13042ec6c7666ef68dd327b1bd90b7f7d93757b3"


class DPSError(ValueError):
    """Static safe reason; upstream text must never be used as the message."""


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def number(value, *, integer=False, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise DPSError("numeric_value_unresolved")
    raw = str(value)
    if len(raw) > 32 or not re.fullmatch(r"\d+(?:\.\d+)?", raw):
        raise DPSError("numeric_value_unresolved")
    result = Decimal(raw)
    if result > 1000000000 or (positive and result <= 0) or (integer and result != result.to_integral_value()):
        raise DPSError("numeric_value_outside_supported_bound")
    return format(result.normalize(), "f")


def product_identifier(value):
    # These fields are public product route identifiers, never user/session IDs.
    if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", value):
        return value
    return number(value, integer=True, positive=True)


def source_schema(body):
    """Only known price fields/types and aggregate shape, never unknown text."""
    return {"body_type": type(body).__name__, "known_fields": {
        key: type(body[key]).__name__ for key in ("price", "uploadPrice", "designPrice")
        if isinstance(body, dict) and key in body},
        "unknown_field_count": len(set(body) - {"price", "uploadPrice", "designPrice"}) if isinstance(body, dict) else None}


def sanitize_price(body):
    if not isinstance(body, dict) or "price" not in body:
        raise DPSError("pricing_response_schema_unresolved")
    result = {"price": number(body["price"])}
    for key in ("uploadPrice", "designPrice"):
        if key in body and body[key] is not None:
            result[key] = number(body[key])
    return result


def sanitize_quantities(body):
    if not isinstance(body, dict):
        raise DPSError("quantity_domain_schema_unresolved")
    static = body.get("staticQuantities")
    if isinstance(static, list) and static:
        if len(static) > 10000:
            raise DPSError("quantity_domain_too_large")
        choices = [number(item.get("quantity"), integer=True, positive=True) if isinstance(item, dict)
                   else number(None) for item in static]
        if len(choices) != len(set(Decimal(v) for v in choices)):
            raise DPSError("quantity_domain_ambiguous")
        result = {"kind": "finite", "values": choices}
    else:
        low = number(body.get("minimumQuantity"), integer=True, positive=True)
        high = number(body.get("maximumQuantity"), integer=True, positive=True)
        if Decimal(low) > Decimal(high):
            raise DPSError("quantity_domain_invalid")
        result = {"kind": "numeric_range", "minimum": low, "maximum": high,
                  "enumerated": False}
    if body.get("startingQuantity") is not None:
        result["starting_quantity"] = number(body["startingQuantity"], integer=True, positive=True)
    return result


def sanitize_options(body, depth=0):
    if not isinstance(body, list) or len(body) > 500 or depth > 8:
        raise DPSError("options_schema_unresolved")
    output = []
    for group in body:
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            raise DPSError("options_schema_unresolved")
        choices = group.get("productOptionItemValues")
        if not isinstance(choices, list) or len(choices) > 2000:
            raise DPSError("options_schema_unresolved")
        projected = {"id": number(group.get("id"), integer=True, positive=True),
                     "label": re.sub(r"<[^>]*>", "", group["name"])[:500], "choices": []}
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get("name"), str):
                raise DPSError("options_schema_unresolved")
            item = {"id": number(choice.get("id"), integer=True, positive=True),
                    "label": re.sub(r"<[^>]*>", "", choice["name"])[:500],
                    "redirects": choice.get("specialType") == 1,
                    "children": sanitize_options(choice.get("productOptionItems") or [], depth + 1)}
            projected["choices"].append(item)
        output.append(projected)
    return output


DPS_DOM_JS = r"""() => {
 const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
 const clean=s=>String(s||'').replace(/\s+/g,' ').trim();
 const val=id=>{const a=document.querySelectorAll('#'+id);return a.length===1?a[0].value:null};
 const control=id=>{const a=[...document.querySelectorAll('#'+id)].filter(visible);if(a.length!==1)return null;const e=a[0];
  return {id,kind:e.tagName==='SELECT'?'select':e.type==='number'?'number':'unsupported',value:e.value,
   disabled:e.disabled,minimum:e.getAttribute('min'),maximum:e.getAttribute('max'),step:e.getAttribute('step'),
   choices:e.tagName==='SELECT'?[...e.options].map(o=>({value:o.value,label:clean(o.textContent),disabled:o.disabled})):[]};};
 const options=[...document.querySelectorAll('#ProductOptionsHolder select[data-option-id]')].filter(visible).map(e=>({
  id:e.getAttribute('data-option-id'),value:e.value,disabled:e.disabled,
  label:clean(e.closest('.form-group')?.querySelector('label')?.textContent),
  choices:[...e.options].map(o=>({value:o.value,label:clean(o.textContent),disabled:o.disabled,redirects:o.hasAttribute('redirectUrl')}))}));
 const turnaround=[...document.querySelectorAll('#TurnaroundOption,.turnaround-option')].filter(visible);
 const legacyPrices=[...document.querySelectorAll('#CustProductPriceHolder')].filter(visible);
 const uploadPrices=[...document.querySelectorAll('#custUploadPrice')].filter(visible);
 const prices=legacyPrices.length===1?legacyPrices:legacyPrices.length===0&&uploadPrices.length===1?uploadPrices:[];
 const price_selector=legacyPrices.length===1?'#CustProductPriceHolder':
  legacyPrices.length===0&&uploadPrices.length===1?'#custUploadPrice':null;
 const rawPrice=prices.length===1?clean(prices[0].textContent):'';
 const amount=/^\$\s*(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}$/.test(rawPrice)?rawPrice:null;
 const priceCandidates=[...document.querySelectorAll('[id*="Price"],[id*="price"],[class*="Price"],[class*="price"]')]
  .filter(visible).map(e=>({tag:e.tagName.toLowerCase(),id:clean(e.id).slice(0,80),text:clean(e.textContent).slice(0,100)}))
  .filter(e=>/\$\s*\d/.test(e.text)).slice(0,20);
 const hasLoginPrompt=[...document.querySelectorAll('.loginRequired,[id^="loginRequired"]')]
  .some(e=>visible(e)&&/please\s+log\s*in|log\s*in\s+to/i.test(e.textContent)) ||
  prices.some(e=>/log\s*in|sign\s*in/i.test(e.textContent));
 const stockNodes=[...document.querySelectorAll('[class*="stock"],[class*="Stock"],[class*="availability"],[class*="Availability"]')]
  .filter(visible);
 const explicitUnavailable=stockNodes.some(e=>/\b(?:temporarily\s+out\s+of\s+stock|out\s+of\s+stock|currently\s+unavailable)\b/i.test(clean(e.textContent)));
 const actions=[...document.querySelectorAll('button,input[type="submit"],a[role="button"]')]
  .filter(visible).map(e=>({text:clean(e.innerText||e.value),disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true'}))
  .filter(e=>/^(?:add\s+to\s+cart|upload\s+a\s+file|order\s+assist|continue\s+to\s+checkout)$/i.test(e.text));
 return {page_url:location.href,title:clean(document.querySelector('h1')?.textContent).slice(0,500),
  api_base:val('PrimaryApiPath'),product_id:val('ProductId'),options_product_id:val('ProductIdField'),
  quantity_product_id:val('CustProductIdField'),quantity:control('CustQuantityList'),
  width:control('CustProductWidthHolder'),height:control('CustProductHeightHolder'),options,
  options_holder_present:!!document.querySelector('#ProductOptionsHolder'),
  turnaround_count:turnaround.length,turnaround:turnaround.length===1?turnaround[0].value:turnaround.length===0?'0':null,
  amount,price_count:prices.length,price_selector,price_text_present:!!rawPrice,price_candidates:priceCandidates,login_prompt:hasLoginPrompt,
  explicit_unavailable:explicitUnavailable,purchase_actions:actions,
  password_visible:[...document.querySelectorAll('input[type=password]')].some(visible),
  busy:typeof window.jQuery==='function'&&Number(window.jQuery.active)>0};
}"""


def bind_context(snapshot):
    result = {"product_id": product_identifier(snapshot.get("product_id"))}
    for key in ("width", "height", "quantity"):
        field = snapshot.get(key)
        if not isinstance(field, dict) or field.get("disabled") or field.get("kind") not in ("select", "number"):
            raise DPSError("calculator_controls_unresolved")
        result[key] = number(field.get("value"), integer=key == "quantity", positive=True)
        if field["kind"] == "select":
            matches = [c for c in field.get("choices", []) if c.get("value") == field["value"] and not c.get("disabled")]
            if len(matches) != 1:
                raise DPSError("selected_domain_unresolved")
    result["turnaroundId"] = number(snapshot.get("turnaround"), integer=True)
    pairs = []
    for option in snapshot.get("options", []):
        if option.get("disabled"):
            raise DPSError("selected_option_disabled")
        key = number(option.get("id"), integer=True, positive=True)
        value = number(option.get("value"), integer=True, positive=True)
        matches = [c for c in option.get("choices", []) if c.get("value") == option["value"] and not c.get("disabled")]
        if len(matches) != 1 or matches[0].get("redirects"):
            raise DPSError("selected_option_unresolved_or_redirecting")
        pairs.append((key, value))
    if len({key for key, _ in pairs}) != len(pairs):
        raise DPSError("selected_option_duplicate")
    result["options"] = "C".join(key + "E" + value for key, value in pairs)
    return result


def bind_pricing_url(url, api_base, product_id):
    parts, base = urlsplit(url), urlsplit(api_base)
    expected = base.path.rstrip("/") + "/pricing/" + product_id
    if ((parts.scheme, parts.netloc, parts.path) != (base.scheme, base.netloc, expected)
            or parts.fragment or parts.username or parts.password):
        raise DPSError("pricing_route_unbound")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if len(pairs) != 5 or set(k for k, _ in pairs) != {"width", "height", "quantity", "turnaroundId", "options"}:
        raise DPSError("pricing_query_unbound")
    params = dict(pairs)
    for key in ("width", "height", "quantity", "turnaroundId"):
        params[key] = number(params[key], integer=key in ("quantity", "turnaroundId"), positive=key != "turnaroundId")
    if not re.fullmatch(r"(?:[1-9]\d*E[1-9]\d*(?:C[1-9]\d*E[1-9]\d*)*)?", params["options"]):
        raise DPSError("pricing_options_unbound")
    return {"product_id": product_id, **params}


def money_matches(raw, price):
    if not isinstance(raw, str) or not re.fullmatch(r"\$\s*(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}", raw):
        return False
    return Decimal(raw.replace("$", "").replace(",", "").strip()) == Decimal(price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def option_pairs(encoded):
    """Decode only the already-validated, ordered source option path."""
    if not isinstance(encoded, str) or not re.fullmatch(r"[1-9]\d*E[1-9]\d*(?:C[1-9]\d*E[1-9]\d*)*", encoded):
        raise DPSError("requested_option_path_unresolved")
    pairs = [tuple(part.split("E")) for part in encoded.split("C")]
    if len({key for key, _ in pairs}) != len(pairs):
        raise DPSError("requested_option_path_duplicate")
    return pairs


def selected_source_value(control, requested, *, integer=False):
    """Map a normalized number to one enabled rendered option."""
    value = Decimal(number(requested, integer=integer, positive=True))
    if not isinstance(control, dict) or control.get("disabled"):
        raise DPSError("requested_control_unavailable")
    if control.get("kind") == "select":
        matches = []
        for choice in control.get("choices", []):
            if choice.get("disabled"):
                continue
            try:
                if Decimal(number(choice.get("value"), integer=integer, positive=True)) == value:
                    matches.append(choice["value"])
            except DPSError:
                continue
        if len(matches) != 1:
            raise DPSError("requested_value_not_in_rendered_dropdown")
        return matches[0]
    if control.get("kind") == "number" and integer:
        low = Decimal(number(control.get("minimum"), integer=True, positive=True))
        high = Decimal(number(control.get("maximum"), integer=True, positive=True))
        step = Decimal(number(control.get("step") or "1", integer=True, positive=True))
        if low <= value <= high and (value - low) % step == 0:
            return str(value)
    raise DPSError("requested_value_outside_rendered_control")


class DPSPilotManager:
    def __init__(self, store, browser_pool, *, timeout_seconds=45):
        self.root = Path(store.root) / "dps-pricing-pilots"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.pool, self.timeout_seconds = browser_pool, timeout_seconds
        self._tasks, self._lock, self._closed = {}, asyncio.Lock(), False
        self.script_sha256 = SCRIPT_SHA256

    def _write(self, report):
        report["updated_at"] = now()
        path = self.root / (report["id"] + ".json")
        temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with temp.open("x") as stream:
            os.chmod(temp, 0o600)
            json.dump(report, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)

    def get(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise KeyError("pilot_not_found")
        try:
            data = json.loads((self.root / (job_id + ".json")).read_text())
        except FileNotFoundError:
            raise KeyError("pilot_not_found") from None
        if data["status"] in ("opening", "running") and job_id not in self._tasks:
            data["status"] = "attention"
            data["warnings"] = sorted(set(data["warnings"] + ["pilot_interrupted"]))
        return data

    def list(self):
        return [self.get(p.stem) for p in sorted(self.root.glob("*.json"), key=lambda p:p.stat().st_mtime, reverse=True)[:100]]

    async def start(self, url, quantities=None, *, expected_request=None):
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.port not in (None, 443)
                or parts.path not in PRODUCT_PATHS or parts.query or parts.fragment or parts.username or parts.password):
            raise DPSError("unsupported_product_url")
        quantities = [] if quantities is None else quantities
        if (not isinstance(quantities, list) or len(quantities) > 4 or
                any(type(q) is not int or not 0 < q <= 1000000 for q in quantities) or len(set(quantities)) != len(quantities)):
            raise DPSError("invalid_pilot_quantities")
        if expected_request is not None:
            if quantities or not isinstance(expected_request, dict) or set(expected_request) != {
                    "width", "height", "quantity", "turnaroundId", "options"}:
                raise DPSError("invalid_exact_selection")
            number(expected_request["width"], positive=True)
            number(expected_request["height"], positive=True)
            number(expected_request["quantity"], integer=True, positive=True)
            number(expected_request["turnaroundId"], integer=True)
            option_pairs(expected_request["options"])
        await validate_url(url)
        async with self._lock:
            if self._closed or any(not task.done() for task in self._tasks.values()):
                raise DPSError("pilot_manager_closed_or_busy")
            report = {"schema": 1, "id": uuid.uuid4().hex, "url": url, "status": "opening",
                      "created_at": now(), "requested_quantities": quantities, "observations": [], "responses": [],
                      "native_requests": 0, "quote_requests": 0, "request_limit": 20 + 4 * len(quantities),
                      "warnings": [], "source_script_sha256": self.script_sha256, "source_contract_verified": False,
                      "coverage": "current_configuration_and_explicit_quantities_only", "catalog_complete": False,
                      "final_eligible": False, "shipping": {"status": "held", "reason": "not_quoted"},
                      "price_basis": "source_calculator_display_price", "currency": "USD",
                      "price_breaks": {"status": "unresolved", "reason": "quantity_domain_is_not_a_price_break_table"}}
            if expected_request is not None:
                report["expected_request"] = expected_request
                report["coverage"] = "one_requested_rendered_selection_only"
                report["availability"] = "unverified"
                report["production_availability"] = "unverified"
            self._write(report)
            self._tasks[report["id"]] = asyncio.create_task(self._run(report))
            return self.get(report["id"])

    async def close(self):
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, report):
        pending, requests = set(), {}
        failure = None
        native_gate = asyncio.Lock()
        next_request_at = 0.0
        validated = set()

        async def check_public(url):
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.username or parts.password:
                raise DPSError("nonpublic_source_url")
            authority = parts.scheme + "://" + parts.netloc
            if authority not in validated:
                await validate_url(url)
                validated.add(authority)

        try:
            async with self.pool.page("https://digital-print-solutions.com", headless=False) as page:
                async def route(route, request):
                    nonlocal failure, next_request_at
                    try:
                        parts = urlsplit(request.url)
                        if request.method not in ("GET", "HEAD"):
                            await route.abort(); return
                        await check_public(request.url)
                        if request.is_navigation_request():
                            if (parts.hostname not in HOSTS or parts.path != urlsplit(report["url"]).path or parts.query):
                                failure = "source_navigation_outside_product"
                                await route.abort(); return
                        elif request.resource_type in ("script", "stylesheet", "image", "font"):
                            if parts.hostname not in ASSET_HOSTS or re.search(r"/(?:cart|account|orders?|checkout|payment|uploads?)(?:/|$)", parts.path, re.I):
                                await route.abort(); return
                        elif request.resource_type in ("xhr", "fetch"):
                            snapshot = await page.evaluate(DPS_DOM_JS)
                            api_base = urljoin(snapshot["page_url"], snapshot.get("api_base") or "")
                            base = urlsplit(api_base)
                            if (not snapshot.get("api_base") or base.scheme != "https" or base.query or base.fragment or base.username or base.password
                                    or (parts.scheme, parts.netloc) != (base.scheme, base.netloc)):
                                await route.abort(); return
                            prefix = base.path.rstrip("/")
                            product = product_identifier(snapshot.get("product_id"))
                            kind = None
                            for name, field, suffix in (("options", "options_product_id", "options"), ("quantities", "quantity_product_id", "customizable")):
                                value = snapshot.get(field)
                                if value and parts.path == prefix + "/products/" + product_identifier(value) + "/" + suffix and not parts.query:
                                    kind = name
                            context = None
                            if parts.path == prefix + "/pricing/" + product:
                                kind = "price"
                                context = bind_pricing_url(request.url, api_base, product)
                                try:
                                    bound = context == bind_context(snapshot)
                                except DPSError:
                                    bound = False
                            if kind is None or parts.fragment:
                                await route.abort(); return
                            async with native_gate:
                                if report["native_requests"] >= report["request_limit"]:
                                    failure = "native_request_budget_exhausted"
                                    await route.abort(); return
                                await asyncio.sleep(max(0, next_request_at - time.monotonic()))
                                next_request_at = time.monotonic() + 1
                                report["native_requests"] += 1
                                if kind == "price":
                                    report["quote_requests"] += 1
                                requests[request] = {"kind": kind, "context": context,
                                                     "configuration_bound_at_request": bound if kind == "price" else None,
                                                     "path": parts.path, "id": uuid.uuid4().hex, "requested_at": now()}
                        else:
                            await route.abort(); return
                        await route.continue_()
                    except DPSError as error:
                        report["last_guard_reason"] = str(error)
                        await route.abort()
                    except Exception:
                        report["last_guard_reason"] = "native_route_unresolved"
                        await route.abort()

                async def capture(response):
                    nonlocal failure
                    request = response.request
                    item = requests.get(request)
                    try:
                        parts = urlsplit(response.url)
                        if parts.hostname in HOSTS and parts.path == SCRIPT_PATH:
                            body = await response.body()
                            report["source_contract_verified"] = response.status == 200 and hashlib.sha256(body).hexdigest() == self.script_sha256
                            if not report["source_contract_verified"]:
                                failure = "source_script_contract_changed"
                        if item is None:
                            return
                        item["status"] = response.status
                        if response.status != 200:
                            item["reason"] = "source_response_not_successful"
                        else:
                            body = await response.body()
                            item["body_sha256"] = hashlib.sha256(body).hexdigest()
                            item["body_bytes"] = len(body)
                            if len(body) > 2000000:
                                raise DPSError("source_response_too_large")
                            data = json.loads(body)
                            if item["kind"] == "price":
                                item["schema"] = source_schema(data)
                                item["price"] = sanitize_price(data)
                            elif item["kind"] == "quantities":
                                item["quantity_domain"] = sanitize_quantities(data)
                            else:
                                item["option_domain"] = sanitize_options(data)
                    except DPSError as error:
                        if item is not None:
                            item["reason"] = str(error)
                    except Exception:
                        if item is not None:
                            item["reason"] = "source_response_unreadable"
                    finally:
                        requests.pop(request, None)
                        if item is not None:
                            report["responses"].append(item)

                def response_received(response):
                    task = asyncio.create_task(capture(response))
                    pending.add(task)
                    task.add_done_callback(pending.discard)

                def request_failed(request):
                    item = requests.pop(request, None)
                    if item:
                        item["reason"] = "native_request_failed"
                        report["responses"].append(item)

                await page.route("**/*", route)
                page.on("response", response_received)
                page.on("requestfailed", request_failed)
                await page.goto(report["url"], wait_until="domcontentloaded", timeout=45000)
                report["status"] = "running"
                self._write(report)

                async def observe(target=None, after_ids=None, expected_other_context=None, expected_context=None):
                    deadline, stable_key, stable_since = time.monotonic() + self.timeout_seconds, None, 0
                    last_reason = "native_calculator_not_ready"
                    while time.monotonic() < deadline:
                        if failure:
                            raise DPSError(failure)
                        snapshot = await page.evaluate(DPS_DOM_JS)
                        report["last_readiness"] = {"price_count": snapshot["price_count"], "amount": snapshot["amount"],
                            "price_selector": snapshot["price_selector"],
                            "price_candidates": snapshot["price_candidates"],
                            "quantity_present": snapshot["quantity"] is not None, "busy": snapshot["busy"],
                            "pending_responses": len(pending), "outstanding_requests": len(requests),
                            "source_contract_verified": report["source_contract_verified"], "login_prompt": snapshot["login_prompt"],
                            "password_visible": snapshot["password_visible"],
                            "public_metadata_present": {k: bool(snapshot.get(k)) for k in ("api_base", "product_id", "options_product_id", "quantity_product_id")}}
                        if snapshot["login_prompt"] or snapshot["password_visible"]:
                            raise DPSError("login_required")
                        if snapshot.get("explicit_unavailable"):
                            raise DPSError("rendered_supplier_unavailable")
                        try:
                            context = bind_context(snapshot)
                            if expected_context is not None and {k: v for k, v in context.items() if k != "product_id"} != expected_context:
                                raise DPSError("requested_selection_not_retained")
                            if expected_other_context is not None and {k:v for k,v in context.items() if k != "quantity"} != expected_other_context:
                                raise DPSError("nonquantity_configuration_changed")
                            candidates = [r for r in report["responses"] if r.get("kind") == "price" and r.get("price")
                                and r.get("configuration_bound_at_request") and r["context"] == context
                                and (not after_ids or r["id"] not in after_ids)]
                            if target is not None and Decimal(context["quantity"]) != target:
                                raise DPSError("quantity_selection_not_settled")
                            if not candidates:
                                raise DPSError("fresh_bound_native_quote_missing")
                            quote = candidates[-1]
                            if not money_matches(snapshot["amount"], quote["price"]["price"]):
                                raise DPSError("dom_price_mismatch_or_unresolved")
                            if not report["source_contract_verified"]:
                                raise DPSError("source_contract_capture_missing")
                            if pending or requests or snapshot["busy"]:
                                raise DPSError("native_requests_not_settled")
                            key = digest([context, snapshot["amount"], quote["id"]])
                            if key != stable_key:
                                stable_key, stable_since = key, time.monotonic()
                            elif time.monotonic() - stable_since >= .4:
                                report["title"] = snapshot["title"]
                                report["observations"].append({"context": context, "price": quote["price"],
                                    "dom_amount": snapshot["amount"], "evidence_id": quote["id"],
                                    "observed_at": now(), "verification": "fresh_native_quote_matches_scoped_display",
                                    "availability_signals": {"explicit_unavailable": False,
                                                             "purchase_actions": snapshot.get("purchase_actions", [])},
                                    "controls": {k: snapshot[k] for k in ("width", "height", "quantity", "options", "turnaround")}})
                                self._write(report)
                                return snapshot
                        except DPSError as error:
                            last_reason, stable_key = str(error), None
                        await asyncio.sleep(.15)
                    raise DPSError(last_reason)

                current = await observe()
                requested = report.get("expected_request")
                if requested is not None:
                    report["selection_phase"] = "source_preflight"
                    from .dps_quote_contract import scoped_request
                    # Recheck the source choice tree and published limits against
                    # the current browser-origin controls before changing the page.
                    pilot_view = {**report, "status": "completed", "source_contract_verified": True,
                                  "source_script_sha256": SCRIPT_SHA256}
                    selected = dict(option_pairs(requested["options"]))
                    if scoped_request(pilot_view, width=requested["width"], height=requested["height"],
                                      quantity=requested["quantity"], selected_options=selected) != requested:
                        raise DPSError("requested_selection_not_source_supported")
                    before = {r["id"] for r in report["responses"]}
                    for group_id, choice_id in option_pairs(requested["options"]):
                        report["selection_phase"] = "option_group_" + group_id
                        snapshot = await page.evaluate(DPS_DOM_JS)
                        same = [option for option in snapshot["options"]
                                if option["id"] == group_id and not option["disabled"]
                                and option["value"] == choice_id]
                        if len(same) == 1:
                            continue
                        selector = f'#ProductOptionsHolder select[data-option-id="{group_id}"]'
                        control = page.locator(selector)
                        await control.wait_for(state="visible", timeout=10000)
                        await control.select_option(choice_id)
                    for axis, selector in (("width", "#CustProductWidthHolder"),
                                           ("height", "#CustProductHeightHolder")):
                        report["selection_phase"] = axis
                        snapshot = await page.evaluate(DPS_DOM_JS)
                        chosen = selected_source_value(snapshot[axis], requested[axis])
                        control = page.locator(selector)
                        if await control.input_value() != chosen:
                            await control.select_option(chosen)
                    report["selection_phase"] = "quantity"
                    snapshot = await page.evaluate(DPS_DOM_JS)
                    chosen = selected_source_value(snapshot["quantity"], requested["quantity"], integer=True)
                    field = page.locator("#CustQuantityList")
                    if await field.input_value() != chosen:
                        if snapshot["quantity"]["kind"] == "select":
                            await field.select_option(chosen)
                        else:
                            await field.fill(chosen)
                            await field.dispatch_event("change")
                    if {k: v for k, v in bind_context(current).items() if k != "product_id"} == requested:
                        # A same-selection request has only the default page
                        # observation; retain it without implying fresh work.
                        report["selection_verified"] = True
                    else:
                        report["selection_phase"] = "bound_price_observation"
                        await observe(Decimal(requested["quantity"]), before,
                                      expected_context=requested)
                        report["selection_verified"] = True
                    actions = report["observations"][-1].get("availability_signals", {}).get("purchase_actions", [])
                    if not any(not action.get("disabled") for action in actions):
                        raise DPSError("rendered_purchase_step_unavailable")
                    report["availability"] = "rendered_purchase_step_available_cart_unverified"
                    report["production_availability"] = "selected_visible_choice_with_bound_quote"
                    report["selection_phase"] = "completed"
                baseline_context = {k:v for k,v in bind_context(current).items() if k != "quantity"}
                for quantity in report["requested_quantities"] if requested is None else []:
                    if Decimal(current["quantity"]["value"]) == quantity:
                        continue
                    field = current["quantity"]
                    before = {r["id"] for r in report["responses"]}
                    if field["kind"] == "select":
                        matches = [c["value"] for c in field["choices"] if not c["disabled"] and Decimal(number(c["value"], integer=True, positive=True)) == quantity]
                        if len(matches) != 1:
                            raise DPSError("requested_quantity_outside_observed_domain")
                        await page.locator("#CustQuantityList").select_option(matches[0])
                    else:
                        low = Decimal(number(field["minimum"], integer=True, positive=True))
                        high = Decimal(number(field["maximum"], integer=True, positive=True))
                        step = Decimal(number(field["step"] or "1", integer=True, positive=True))
                        if not low <= quantity <= high or (Decimal(quantity) - low) % step:
                            raise DPSError("requested_quantity_outside_observed_domain")
                        await page.locator("#CustQuantityList").fill(str(quantity))
                        await page.locator("#CustQuantityList").dispatch_event("change")
                    current = await observe(quantity, before, baseline_context)
                report["status"] = "completed"
        except asyncio.CancelledError:
            report["status"] = "attention"
            report["warnings"].append("pilot_interrupted")
        except DPSError as error:
            report["status"] = "login_required" if str(error) == "login_required" else "attention"
            report["warnings"].append(str(error))
        except Exception as error:
            report["status"] = "attention"
            report["warnings"].append("pilot_internal_" + type(error).__name__)
        finally:
            for task in list(pending):
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            report["completed_count"] = len(report["observations"])
            report["ended_at"] = now()
            self._write(report)
            self._tasks.pop(report["id"], None)
