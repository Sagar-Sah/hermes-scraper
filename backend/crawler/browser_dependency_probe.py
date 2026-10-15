"""Budgeted, read-only calculator experiments in a duplicate supplier tab.

This records observations and reconciliation, not cart verification or proof of
dependency independence. No browser credentials or general Vue state are read.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import traceback
from decimal import Decimal, InvalidOperation
from collections import OrderedDict
from urllib.parse import parse_qsl, urlencode, urlparse

from .pacing import retry_after_seconds, source_pacing
from .adapters.browser import SNAPSHOT_JS, normalize_snapshot, filter_choices
from .price_contract import (
    CONTROLLER_PRICE_STATE_JS, SOURCE_CONTRACT, calculate_source_total,
)
from .probe_jobs import ASSET_ORIGIN, ORIGIN, READ_PATHS, public_url
from .store import canonical, digest, safe_public_context, uid
from .transport import ExtractionError, RateLimiter

PRODUCT_URLS = {
    ORIGIN + f"/products/{slug}/order"
    for slug in ("business-cards", "club-flyers", "roll-labels")
}
STARTUP_GETS = {
    "/service/rest/v1/settings", "/service/rest/v1/users/self",
    "/service/rest/v1/users/self/settings", "/service/rest/v1/users/self/tier",
    "/service/rest/v1/store/countries", "/service/rest/v1/store/settings",
}
STARTUP_POSTS = {
    "/service/rest/v1/i18n/lookup/resources",
    "/service/rest/v1/rules/turnarounds",
}
FORBIDDEN_PATH = re.compile(r"/(?:cart|orders?|payments?|uploads?|quotes?|oauth)(?:/|$)", re.I)
PRIVATE_QUERY = re.compile(r"secret|token|cookie|password|auth|credential|session|customer|contact|address|email|cid|itemId", re.I)


def initialized_product_url(product_url, initialization=None):
    """Build only the ordinary public-spec initialization proven in the bundle.

    This never enables cart duplication, saved projects or account item IDs.
    Source session state may override these values; the caller must read back
    the effective controller state before recording a baseline.
    """
    if product_url not in PRODUCT_URLS:
        raise ValueError("Initialization requires an allowed product base URL")
    if initialization is None:
        return product_url
    if not isinstance(initialization, dict) or not initialization:
        raise ValueError("Initialization must be a nonempty public specification map")
    roll = product_url.endswith("/roll-labels/order")
    allowed = {"size", "paper", "color", "quantity", "turnaround", "proof_type", "file_delivery"}
    if roll:
        allowed |= {"shape", "finish", "windPosition"}
    if set(initialization) - allowed or ("shape" if roll else "size") not in initialization:
        raise ValueError("Initialization contains unsupported or missing source fields")
    values = {}
    for key, value in initialization.items():
        if type(value) not in {int, str} or not re.fullmatch(r"[1-9][0-9]{0,8}", str(value)):
            raise ValueError("Initialization values must be bounded positive numeric IDs")
        values[key] = str(value)
    for key, choices in {"turnaround": {"1", "3"}, "proof_type": {"1", "31", "36"},
                         "file_delivery": {"5", "22"}, "windPosition": {"32", "33", "34", "35", "54"}}.items():
        if key in values and values[key] not in choices:
            raise ValueError("Initialization uses an unsupported source selection")
    if ("proof_type" in values) != ("file_delivery" in values):
        raise ValueError("Proof and file delivery must be initialized together")
    return product_url + "?" + urlencode(sorted(values.items()))


def initialization_mismatches(initialization, capture):
    """Compare public query aliases against the actual selected controller IDs."""
    expected = {}
    for key, value in initialization.items():
        if key == "proof_type":
            expected["proofType"] = "31" if str(value) == "31" else "100"
            if str(value) != "31":
                expected["manualProofType"] = str(value)
        else:
            expected[{"turnaround": "productionTime", "file_delivery": "fileDelivery"}.get(key, key)] = str(value)
    selected = capture.get("userSelectedOptions", {})
    return {key: {"requested": value, "observed": selected.get(key)}
            for key, value in expected.items() if str(selected.get(key)) != value}


CANARY_VALUES = {
    "roundedCorners": {"", "2"}, "coating": {"", "5", "40"},
    "spotCoating": {"", "3", "39"}, "raisedPrint": {"", "135", "137", "138", "140"},
    "raisedFoilColor": {"141", "142", "143"},
}


def validated_canary_selections(product_url, initialization, phases, selections):
    """Source-supported fields only; actual offered values are checked in the UI."""
    if selections is None:
        return []
    if (product_url != ORIGIN + "/products/business-cards/order" or not initialization
            or phases is None or phases or not isinstance(selections, list) or not 1 <= len(selections) <= 6):
        raise ValueError("Canaries require one initialized Business Cards page, no service phases, and 1–6 dropdown changes")
    initialized_product_url(product_url, initialization)
    result, names = [], set()
    for item in selections:
        if not isinstance(item, dict) or set(item) != {"name", "key", "value"}:
            raise ValueError("Canary requires exactly name, key and value")
        name, key, value = item["name"], item["key"], item["value"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name) or name == "baseline" or name in names:
            raise ValueError("Canary names must be distinct bounded public names")
        if type(value) not in {str, int} or not isinstance(key, str):
            raise ValueError("Canary values must be explicit public scalars")
        value = str(value)
        if key == "quantity":
            if not re.fullmatch(r"[1-9][0-9]{0,8}", value):
                raise ValueError("Canary quantity must be a positive source value")
        elif key not in CANARY_VALUES or value not in CANARY_VALUES[key]:
            raise ValueError("Canary selection is not in the reviewed Business Cards field domain")
        names.add(name)
        result.append({"name": name, "key": key, "value": value})
    return result


def canary_capture_ready(report, phase):
    formula = phase.get("formula", {})
    return (report.get("loaded_contract") == SOURCE_CONTRACT
            and phase.get("status") == "observed"
            and formula.get("status") == "complete" and formula.get("comparison") == "matched")


PUBLIC_PRICE_METADATA_JS = r"""() => {
 const element=document.querySelector('#order-form');
 if(!element)return {status:'unresolved',reason:'product_root_missing'};
 const roots=[element._vnode?.component?.proxy,element.__vue_app__?._instance?.proxy].filter(Boolean);
 const controllers=[...new Set(roots.map(r=>r.$refs?.childComponents?._?.subTree?.component?.refs?.orderFormInstance).filter(Boolean))];
 const scalar=v=>(typeof v==='string'||typeof v==='number')&&/^[0-9]{1,9}$/.test(String(v))?String(v):null;
 const fromDom=scalar(element.getAttribute('data-currentstore'));
 const fromController=controllers.length===1?scalar(controllers[0].store):null;
 return {status:fromDom!==null&&fromController!==null?(fromDom===fromController?'observed':'conflicting'):'unresolved',
   controller_store:fromController,dom_currentstore:fromDom,
   controller_count:controllers.length};
}"""


class PublicAssetCache:
    """Short-lived public static bytes only; never HTML, API or session data."""
    def __init__(self, max_bytes=64 * 1024 * 1024, clock=time.monotonic):
        self.max_bytes, self.clock, self.size = max_bytes, clock, 0
        self.items = OrderedDict()

    @staticmethod
    def allowed(url):
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" == ASSET_ORIGIN and bool(re.search(r"\.(js|css)$", p.path, re.I))

    def get(self, url):
        item = self.items.get(url)
        if item is None:
            return None
        if item["expires"] <= self.clock():
            self.size -= len(self.items.pop(url)["body"])
            return None
        self.items.move_to_end(url)
        return item

    def put(self, url, status, headers, body):
        headers = {k.lower(): v for k, v in headers.items()}
        if status != 200 or not self.allowed(url) or len(body) > self.max_bytes or "set-cookie" in headers:
            return False
        directives = [part.strip().lower() for part in headers.get("cache-control", "").split(",")]
        if any(part.split("=", 1)[0] in {"private", "no-store", "no-cache"} for part in directives):
            return False
        vary = {part.strip().lower() for part in headers.get("vary", "").split(",")}
        if vary & {"*", "cookie", "authorization", "proxy-authorization"}:
            return False
        ages = [part.split("=", 1)[1].strip('"') for part in directives if part.startswith("max-age=")]
        if len(ages) != 1 or not ages[0].isdigit():
            return False
        age = headers.get("age", "0")
        if not str(age).isdigit():
            return False
        ttl = min(int(ages[0]) - int(age), 600)
        if ttl <= 0:
            return False
        if url in self.items:
            self.size -= len(self.items.pop(url)["body"])
        while self.items and self.size + len(body) > self.max_bytes:
            _, old = self.items.popitem(last=False)
            self.size -= len(old["body"])
        self.items[url] = {"body": body, "expires": self.clock() + ttl,
                           "headers": {"content-type": headers.get("content-type", "application/javascript")}}
        self.size += len(body)
        return True


def asset_contract_key(url):
    path = urlparse(url).path
    return "order_form_sha256" if path.endswith("/orderForm.js") else "common_sha256" if path.endswith("/chunk-common.js") else None


def request_policy(url, method, resource_type, product_url, body=None):
    """Return a narrow request lane, or None. POST is calculation/locale only."""
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    if p.username or p.password or origin not in {ORIGIN, ASSET_ORIGIN}:
        return None
    query = parse_qsl(p.query, keep_blank_values=True)
    if any((PRIVATE_QUERY.search(k) and not (k == "cid" and v == "" or k == "itemId" and v == "null"))
           or k.lower() == "action" for k, v in query):
        return None
    if method == "GET" and resource_type == "document":
        if url != product_url or p.fragment or len(query) != len(dict(query)):
            return None
        try:
            generated = initialized_product_url(p._replace(query="", fragment="").geturl(), dict(query) if query else None)
        except ValueError:
            return None
        return "document" if url == generated else None
    if method == "GET" and resource_type in {"script", "stylesheet"}:
        # Public presentation assets can have cart/order directory names.
        # This gate never admits an API route or a non-GET operation.
        return "asset" if re.search(r"\.(?:js|css)$", p.path, re.I) and not p.path.startswith("/service/rest/") else None
    if FORBIDDEN_PATH.search(p.path):
        return None
    if origin != ORIGIN or resource_type not in {"fetch", "xhr"}:
        return None
    if method == "GET" and (READ_PATHS.fullmatch(p.path) or p.path == "/service/rest/v1/product/search"):
        return "public_source"
    if method == "GET" and p.path in STARTUP_GETS:
        return "startup"
    if method == "POST" and p.path in STARTUP_POSTS:
        if not isinstance(body, dict) or safe_public_context(body) != body:
            return None
        if len(canonical(body)) > 50000:
            return None
        if p.path.endswith("/i18n/lookup/resources"):
            return "startup" if set(body) <= {"nonEnums", "enums", "country", "state"} and all(isinstance(v, str) for v in body.values()) else None
        if set(body) <= {"quantity", "specifications", "productOptions", "itemType"}:
            return "public_source"
    return None


CONTROL_STATE_JS = r"""() => {
 const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
 const clean=s=>String(s||'').replace(/\s+/g,' ').trim().slice(0,180);
 const path=e=>{if(e.id)return '#'+CSS.escape(e.id);const parts=[];for(let n=e;n&&n.nodeType===1;n=n.parentElement){if(n.id){parts.unshift('#'+CSS.escape(n.id));break;}let i=1;for(let s=n.previousElementSibling;s;s=s.previousElementSibling)if(s.tagName===n.tagName)i++;parts.unshift(n.tagName.toLowerCase()+':nth-of-type('+i+')');}return parts.join(' > ')};
 const names={proofType:'proofType',proofTypeForManual:'manualProofType',manualProofType:'manualProofType',fileDeliveryForIop:'fileDelivery',fileDeliveryForManual:'fileDelivery',fileDelivery:'fileDelivery',productionTime:'productionTime',quantity:'quantity'};
 const controls=[];
 for(const e of document.querySelectorAll('#order-form select,#collapseOne select,#order-form input[type=radio],#collapseOne input[type=radio]')){
   const key=names[e.name]||names[e.id];if(!key)continue;
   const label=Array.from(e.labels||[]).find(visible),shown=visible(e)||!!label;
   if(e.tagName==='SELECT'){
     if(!shown||e.disabled)continue;
     controls.push({key,kind:'select',selector:path(e),value:e.value,choices:Array.from(e.options).filter(o=>!o.disabled&&o.value).map(o=>({value:o.value,label:clean(o.textContent)}))});
   } else {
     if(!shown||e.disabled)continue;
     let group=controls.find(c=>c.key===key&&c.kind==='radio');
     if(!group){group={key,kind:'radio',value:null,choices:[]};controls.push(group);}
     if(e.checked)group.value=e.value;
     group.choices.push({value:e.value,label:clean(label?.textContent||e.value),selector:path(e),label_selector:label?path(label):null});
   }
 }
 const buttons=Array.from(document.querySelectorAll('#radioBtn a.turnAround[data-toggle="turnaround"][data-title]')).filter(visible);
 if(buttons.length)controls.push({key:'productionTime',kind:'button',value:buttons.find(e=>e.classList.contains('active'))?.getAttribute('data-title')||null,choices:buttons.filter(e=>e.getAttribute('aria-disabled')!=='true'&&!e.classList.contains('disabled')&&!/not\s+available/i.test(e.textContent)).map(e=>({value:e.getAttribute('data-title'),selector:path(e),label:clean(e.textContent)}))});
 const subtotal=Array.from(document.querySelectorAll('.totalSection')).filter(visible).find(e=>/subtotal/i.test(e.querySelector('.subTotal')?.textContent||''));
 const price=subtotal?clean(subtotal.querySelector('.col-4')?.textContent):null;
 const disclosure=Array.from(document.querySelectorAll('button,a,[role=button]')).filter(e=>visible(e)&&((e.getAttribute('href')==='#collapseOne')||(e.getAttribute('data-target')==='#collapseOne')||(e.getAttribute('aria-controls')==='collapseOne')||/^Process Type\s*[+−-]?$/i.test(clean(e.textContent))));
 return {controls,price,busy:Array.from(document.querySelectorAll('#ajaxLoader,[aria-busy=true]')).some(visible),
   disclosure:!visible(document.querySelector('#collapseOne'))&&disclosure.length===1?path(disclosure[0]):null,
   challenge:/authenticating|verify you are human|access denied|just a moment/i.test(document.title),login:Array.from(document.querySelectorAll('input[type=password]')).some(visible)};
}"""


PUBLIC_DOM_FLAGS_JS = r"""() => {
 const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
 const root=document.querySelector('#order-form');
 return {document_ready:document.readyState,product_root_present:!!root,
   gpfields_exists:'gpfields' in window,gpfields_type:typeof window.gpfields,
   gpfields_not_defined_observed:window.__atlasDependencyGpfieldsError===true,
   subtotal_present:Array.from(document.querySelectorAll('.totalSection')).some(visible),
   visible_control_count:root?Array.from(root.querySelectorAll('select,input[type=radio]')).filter(visible).length:0,
   visible_login_input:Array.from(document.querySelectorAll('input[type=password]')).some(visible),
   busy:Array.from(document.querySelectorAll('#ajaxLoader,[aria-busy=true]')).some(visible),
   challenge:/authenticating|verify you are human|access denied|just a moment/i.test(document.title)};
}"""


GPFIELDS_ERROR_SIGNAL_JS = r"""(() => {
 // The supplier's error handler prevents this exact event and reloads. Keep
 // only a boolean before that handler runs; never retain its error object.
 window.__atlasDependencyGpfieldsError=false;
 window.addEventListener('error',event=>{
   if(event.message==='gpfields is not defined'||event.message==='Uncaught ReferenceError: gpfields is not defined'){
     window.__atlasDependencyGpfieldsError=true;
     // Retain this one bit outside the document before the supplier reloads it.
     window.__atlasDependencyGpfieldsObserved().catch(()=>{});
   }
 },true);
})()"""


def page_error_diagnostic(error, phase, stage):
    """Only classify the source-proven missing global; no message is retained."""
    message = str(getattr(error, "message", error)).strip()
    code = "gpfields_not_defined" if message in {
        "gpfields is not defined", "ReferenceError: gpfields is not defined",
        "Uncaught ReferenceError: gpfields is not defined",
    } else "unclassified_page_error"
    return {"code": code, "phase": phase, "stage": stage}


def blocked_request_diagnostic(url, method, resource_type, body):
    """Expose known supplier paths and parameter names, never query values."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    known_origin = origin in {ORIGIN, ASSET_ORIGIN} and not parsed.username and not parsed.password
    public_path = known_origin and (
        origin == ORIGIN and parsed.path.startswith("/service/rest/v1/")
        or resource_type in {"script", "stylesheet"} and re.search(r"\.(?:js|css)$", parsed.path, re.I)
    )
    safe_path = parsed.path if public_path and re.fullmatch(r"[/A-Za-z0-9._-]{1,240}", parsed.path) else "[non-API resource]"
    safe_path = re.sub(r"[A-Za-z0-9_-]{32,}", "[redacted]", safe_path)
    return {"method": method, "resource": resource_type,
            "origin": origin if known_origin else "[other origin]", "path": safe_path,
            "query_names": sorted({k[:80] for k, _ in parse_qsl(parsed.query, keep_blank_values=True)}),
            "body_fields": {str(k)[:80]: type(v).__name__ for k, v in body.items()} if isinstance(body, dict) else type(body).__name__}


def exception_diagnostic(exc, stage):
    """Classify failures without copying exception messages, URLs or page data."""
    name = type(exc).__name__
    message = str(exc).lower()
    signals = {
        "execution_context_destroyed": "execution context was destroyed" in message or "cannot find context with specified id" in message,
        "target_closed": any(text in message for text in ("target page, context or browser has been closed", "target closed", "page has been closed")),
        "strict_mode_violation": "strict mode violation" in message,
        "invalid_selector": "invalid selector" in message or "while parsing css selector" in message,
        "navigation_interrupted": "interrupted by another navigation" in message or "net::err_aborted" in message,
    }
    if isinstance(exc, ExtractionError):
        code = exc.code
    elif name == "TimeoutError":
        code = "navigation_timeout" if stage == "navigation" else "browser_action_timeout"
    else:
        code = next((key for key, value in signals.items() if value), "browser_probe_failed")
    result = {"code": code, "exception_type": name, "stage": stage}
    if any(signals.values()):
        result["signals"] = signals
    frames = [{"file": "browser_dependency_probe.py", "function": frame.name, "line": frame.lineno}
              for frame in traceback.extract_tb(exc.__traceback__)
              if frame.filename.rsplit("/", 1)[-1] == "browser_dependency_probe.py"]
    if frames:
        result["trace"] = frames[-4:]
    return result


def changed_selections(baseline, current):
    before = baseline.get("userSelectedOptions", {})
    after = current.get("userSelectedOptions", {})
    return {key: {"before": before.get(key), "after": after.get(key)}
            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}


def quote_ready(capture):
    selected = capture.get("userSelectedOptions", {})
    try:
        for key in ("quantity", "productId"):
            value = str(selected.get(key, ""))
            if not value.isdigit() or int(value) <= 0:
                return False
        amount = Decimal(str(capture.get("appliedPrices", {}).get("productPrice")))
        return amount.is_finite() and amount > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


async def recheck_base_price(transport, budget, name, capture):
    """Fresh authenticated read; the supplied transport owns its durable budget."""
    if not quote_ready(capture):
        raise ExtractionError("Base recheck requires an initialized quote", "unconfigured_quote")
    source_url = ORIGIN + "/service/rest/v1/products/" + str(capture["userSelectedOptions"]["productId"]) + "/prices"
    fetched = await transport.fetch(source_url, params={"itemId": "null", "cid": ""},
        context={"lane": "http_source", "probe_job": budget.job_id, "event_id": uid(),
                 "dependency_phase": name, "controller_context": capture})
    return {"phase": name, "kind": "explicit_base_recheck", "evidence_id": fetched.response_id,
            "url": source_url, "semantic_hash": digest(fetched.data), "request_method": "GET",
            "controller_context": capture, "cached": fetched.cached}


async def probe_product(*, pool, transport, budget, product_url, headless=True,
                        settle_timeout=45, stability_seconds=1.0, phases=None, initialization=None,
                        canary_selections=None, on_baseline_ready=None):
    """Open one isolated tab in the shared login context and return its report.

    budget is an existing ProbeBudget; no new allowance is created. A failed
    baseline/reset stops subsequent experiments. Missing controls are explicit.
    """
    canaries = validated_canary_selections(product_url, initialization, phases, canary_selections)
    document_url = initialized_product_url(product_url, initialization)
    store = transport.store
    profile = next((p for p in store.profiles("source") if p.get("host") == "www.printograph.com"), {})
    rps, concurrency = source_pacing(profile, store.config)
    limiter = RateLimiter(store)
    if not hasattr(pool, "_dependency_public_asset_cache"):
        pool._dependency_public_asset_cache = PublicAssetCache()
    asset_cache = pool._dependency_public_asset_cache
    report = {"schema_version": 1, "product_url": product_url, "status": "unresolved",
              "source_contract": dict(SOURCE_CONTRACT), "loaded_contract": {},
              "phases": [], "captures": [], "blocked_requests": {}, "errors": [],
              "cart_verified": False, "dependency_independence_proven": False,
              "public_asset_cache_hits": 0, "navigation_count": 0, "actions": [], "page_errors": [],
              "source_error_signals": []}
    if initialization is not None:
        report["requested_initialization"] = {key: str(value) for key, value in initialization.items()}
    pending, active = set(), set()
    retained_headers = {}
    phase = "baseline"
    fatal = None
    stage = "navigation"

    async with pool.page(ORIGIN, headless=headless) as page:
        def record_page_error(error):
            if len(report["page_errors"]) < 20:
                report["page_errors"].append(page_error_diagnostic(error, phase, stage))

        def record_gpfields_error(_source):
            if len(report["source_error_signals"]) < 20:
                report["source_error_signals"].append({"code": "gpfields_not_defined", "phase": phase, "stage": stage})

        page.on("pageerror", record_page_error)
        await page.expose_binding("__atlasDependencyGpfieldsObserved", record_gpfields_error)
        await page.add_init_script(GPFIELDS_ERROR_SIGNAL_JS)

        async def controller(settled=False):
            capture = await page.evaluate(CONTROLLER_PRICE_STATE_JS,
                                          {"source_contract": SOURCE_CONTRACT, "settled": settled})
            if canaries:
                capture["public_price_metadata"] = await page.evaluate(PUBLIC_PRICE_METADATA_JS)
            return capture

        async def route_handler(route):
            nonlocal fatal
            task = asyncio.current_task()
            pending.add(task)
            request, token, attempt = route.request, None, None
            queued_at, sent_at, status, delay = time.monotonic(), None, 0, 0
            lane = None
            try:
                body = None
                if request.method == "POST":
                    try:
                        body = request.post_data_json
                    except Exception:
                        pass
                lane = request_policy(request.url, request.method, request.resource_type, document_url, body)
                if lane is None:
                    key = canonical(blocked_request_diagnostic(request.url, request.method, request.resource_type, body))
                    report["blocked_requests"][key] = report["blocked_requests"].get(key, 0) + 1
                    await route.abort()
                    return
                if on_baseline_ready is not None and lane == "public_source" and request.method == "GET":
                    from .probe_jobs import bootstrap_transport_witness
                    incoming = await request.all_headers()
                    witnessed = bootstrap_transport_witness(request.method, request.url, incoming)
                    if witnessed:
                        retained_headers.update(witnessed)
                    incoming.clear()
                if lane == "document" and request.frame == page.main_frame:
                    report["navigation_count"] += 1
                    if report["navigation_count"] > 1:
                        fatal = "source_navigation_during_initialization" if stage in {"navigation", "baseline_initialize"} else "source_navigation_during_phase"
                        report["navigation_interruption"] = {"phase": phase, "stage": stage,
                            "action_key": (report.get("pending_action") or {}).get("key")}
                        # A new document invalidates the controller witness. Stop
                        # before sending it instead of silently restarting phases.
                        await route.abort()
                        return
                active.add(task)
                await budget.check()
                cached = asset_cache.get(request.url) if lane == "asset" else None
                if cached is not None:
                    await route.fulfill(status=200, headers=cached["headers"], body=cached["body"])
                    report["public_asset_cache_hits"] += 1
                    contract_key = asset_contract_key(request.url)
                    if contract_key:
                        report["loaded_contract"][contract_key] = hashlib.sha256(cached["body"]).hexdigest()
                    return
                origin = f"{urlparse(request.url).scheme}://{urlparse(request.url).netloc}"
                token = await limiter.acquire(origin, rps, concurrency, budget.check)
                attempt = await asyncio.to_thread(budget.reserve, "browser_dependency:" + phase,
                                                  request.method, request.url)
                request_phase = phase
                try:
                    request_context = await controller(False) if lane == "public_source" else None
                except Exception:
                    request_context = {"observed": False, "reason": "document_not_ready"}
                # Queueing is local pacing, not supplier latency. Feeding that
                # wait into adaptive pacing causes a self-amplifying slowdown.
                sent_at = time.monotonic()
                await route.continue_()
                response = await request.response()
                if response is None:
                    raise ExtractionError("Allowed source request failed", "source_network_error")
                status = response.status
                delay = retry_after_seconds(response.headers.get("retry-after"), default=0)
                await response.finished()
                if status >= 400:
                    report["errors"].append({"phase": request_phase, "status": status,
                                             "path": urlparse(request.url).path})
                    if lane in {"document", "public_source"}:
                        raise ExtractionError("Supplier request failed", "source_http_error")
                contract_key = asset_contract_key(request.url)
                if lane in {"asset", "public_source"}:
                    content = await response.body()
                    if len(content) > store.config.max_response_bytes:
                        raise ExtractionError("Source response exceeds size limit", "response_too_large")
                    if lane == "asset":
                        asset_cache.put(request.url, status, response.headers, content)
                        if contract_key:
                            report["loaded_contract"][contract_key] = hashlib.sha256(content).hexdigest()
                    elif 200 <= status < 300:
                        data = json.loads(content)
                        result = await transport.record_observation(
                            request.url, status, {"content-type": "application/json"}, content,
                            context={"lane": "browser_source", "dependency_phase": request_phase,
                                     "request_method": request.method, "request_body": body,
                                     "controller_context": request_context}, count_request=False)
                        report["captures"].append({"phase": request_phase, "evidence_id": result.response_id,
                            "url": public_url(request.url), "semantic_hash": digest(data),
                            "request_method": request.method, "request_body": body,
                            "controller_context": request_context})
            except ExtractionError as exc:
                fatal = fatal or exc.code
                try:
                    await route.abort()
                except Exception:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                fatal = fatal or "source_capture_failed"
                report.setdefault("capture_failures", []).append(exception_diagnostic(exc, "source_capture"))
                try:
                    await route.abort()
                except Exception:
                    pass
            finally:
                elapsed = time.monotonic() - sent_at if sent_at is not None else 0
                if attempt:
                    await asyncio.to_thread(budget.finish, attempt, status, elapsed)
                active.discard(task)
                if token:
                    await asyncio.to_thread(limiter.release, origin, token, elapsed, status, delay, rps)
                report["max_queue_seconds"] = max(report.get("max_queue_seconds", 0), (sent_at or time.monotonic()) - queued_at)
                pending.discard(task)

        async def control_state():
            state = await page.evaluate(CONTROL_STATE_JS)
            visible = normalize_snapshot(await page.evaluate(SNAPSHOT_JS, {"priceSelector": None}))
            keys = {"shape", "size", "orientation", "paper", "color", "finish", "quantity"}
            if canaries:
                keys |= set(CANARY_VALUES)
            aliases = {"spotcoating": "spotCoating", "raisedprint": "raisedPrint", "raisedfoilcolor": "raisedFoilColor"}
            base_controls = [{**control, "key": aliases.get(control["key"], control["key"])}
                             for control in visible["controls"] if aliases.get(control["key"], control["key"]) in keys]
            state["controls"] = [c for c in state["controls"] if c["key"] not in keys] + base_controls
            return state

        async def settle(require_quote=True):
            deadline, previous, stable_since = time.monotonic() + settle_timeout, None, None
            while time.monotonic() < deadline:
                await budget.check()
                if fatal:
                    raise ExtractionError("Dependency probe source request failed", fatal)
                state = await control_state()
                capture = await controller(False)
                report["last_observation"] = {"phase": phase, "busy": state["busy"],
                    "challenge": state["challenge"], "visible_login_input": state["login"],
                    "control_count": len(state["controls"]), "subtotal_present": bool(state.get("price")),
                    "controller_observed": capture.get("observed") is True,
                    "controller_reason": capture.get("reason"), "active_requests": len(active),
                    "quote_ready": quote_ready(capture), "require_quote": require_quote}
                if state["challenge"] or state["login"]:
                    raise ExtractionError("Source authentication required", "access_required")
                signature = digest({"state": state, "capture": capture})
                ready = not active and not state["busy"] and capture.get("observed") and (not require_quote or (state.get("price") and quote_ready(capture)))
                if signature == previous and ready:
                    stable_since = stable_since or time.monotonic()
                    if time.monotonic() - stable_since >= stability_seconds:
                        capture["settled"] = True
                        capture["displayed_total"] = state["price"]
                        return state, capture
                else:
                    stable_since = None
                previous = signature
                await asyncio.sleep(0.2)
            raise ExtractionError("Calculator state did not settle", "source_timeout")

        async def select(key, value, *, require_quote=True, force=False, dropdown_only=False):
            action = {"key": key, "value": str(value)[:80], "kind": "unknown",
                      "phase": phase, "status": "locating"}
            report["pending_action"] = action
            report["actions"].append(action)
            report["actions"] = report["actions"][-20:]
            state, capture = await settle(require_quote=require_quote)
            groups = [c for c in state["controls"] if c["key"] == key]
            if len(groups) != 1:
                raise ExtractionError("Control missing or ambiguous", "control_unavailable")
            control = groups[0]
            if dropdown_only and control["kind"] != "select":
                raise ExtractionError("Canary target is not an observed dropdown", "canary_requires_dropdown")
            choices = [c for c in filter_choices(control) if str(c["value"]) == str(value)]
            if len(choices) != 1:
                raise ExtractionError("Requested value not currently offered", "choice_unavailable")
            choice = choices[0]
            action.update(kind=control["kind"], selector=str(control.get("selector") or choice.get("selector") or "")[:512],
                          label_selector=str(choice.get("label_selector") or "")[:512], status="pending")
            if force or str(control.get("value")) != str(value):
                if control["kind"] == "select":
                    await page.locator(control["selector"]).select_option(str(value), timeout=10000)
                else:
                    await page.locator(choice.get("label_selector") or choice["selector"]).click(timeout=10000)
                state, capture = await settle(require_quote=require_quote)
            if str(capture.get("userSelectedOptions", {}).get(key)) != str(value):
                raise ExtractionError("Controller did not retain requested value", "selection_not_applied")
            action.update(status="completed", effective_value=str(capture["userSelectedOptions"][key])[:80])
            return state, capture

        async def initialize_baseline():
            state, capture = await settle(require_quote=False)
            report["initial_selected_map"] = dict(capture.get("userSelectedOptions", {}))
            report["baseline_initialization"] = []
            if initialization is not None:
                report["initialization_mismatches"] = initialization_mismatches(initialization, capture)
                if report["initialization_mismatches"]:
                    raise ExtractionError("Source did not retain the requested public initialization", "initialization_not_applied")
                return await settle()
            if quote_ready(capture):
                return await settle()
            for key in ("shape", "orientation", "size", "paper", "finish", "color", "quantity"):
                state, capture = await settle(require_quote=False)
                controls = [c for c in state["controls"] if c["key"] == key]
                if len(controls) != 1:
                    if key in {"shape", "orientation", "finish"}:
                        continue
                    raise ExtractionError("Required baseline control missing", "baseline_control_unavailable")
                control = controls[0]
                choices = [choice for choice in filter_choices(control) if str(choice.get("value", "")).isdigit() and int(str(choice["value"])) > 0]
                if not choices:
                    raise ExtractionError("No offered baseline value", "baseline_choice_unavailable")
                current = str(capture.get("userSelectedOptions", {}).get(key, ""))
                offered = {str(choice["value"]) for choice in choices}
                force_size_dependency = key == "size" and not capture.get("userSelectedOptions", {}).get("paper")
                if current in offered and not force_size_dependency:
                    continue
                choice = next((c for c in choices if str(c["value"]) != current), choices[0]) if force_size_dependency else choices[0]
                state, capture = await select(key, choice["value"], require_quote=False, force=force_size_dependency)
                report["baseline_initialization"].append({"key": key, "value": str(choice["value"]), "label": choice.get("label", "")})
            state, capture = await settle()
            report["baseline_selected_map"] = dict(capture["userSelectedOptions"])
            return state, capture

        async def phase_record(name, capture, baseline=None, status="observed", reason=None):
            reconciliation = calculate_source_total(capture)
            if report["loaded_contract"] != SOURCE_CONTRACT:
                reconciliation.update(status="unresolved", comparison="unresolved", subtotal=None)
                reconciliation["reasons"] = sorted(set(reconciliation.get("reasons", [])) | {"loaded_source_contract_unverified"})
            record = {"name": name, "status": status, "capture": capture,
                      "formula": reconciliation, "evidence_ids": [c["evidence_id"] for c in report["captures"] if c["phase"] == name],
                      "changed_selections": changed_selections(baseline, capture) if baseline else {}}
            if reason:
                record["reason"] = reason
            report["phases"].append(record)
            if status == "observed" and reconciliation["status"] == "complete" and reconciliation.get("comparison") == "matched":
                try:
                    fresh = await recheck_base_price(transport, budget, name, capture)
                except ExtractionError as exc:
                    record["status"], record["reason"] = "unresolved", exc.code
                    raise
                report["captures"].append(fresh)
                record["evidence_ids"].append(fresh["evidence_id"])
                record["base_recheck"] = {k: fresh[k] for k in ("evidence_id", "semantic_hash", "cached")}

        async def restore(baseline):
            _, current = await settle()
            expected = baseline["userSelectedOptions"]
            if str(current["userSelectedOptions"].get("manualProofType")) != str(expected.get("manualProofType")):
                if str(current["userSelectedOptions"].get("proofType")) != "100":
                    _, current = await select("proofType", "100")
                _, current = await select("manualProofType", expected["manualProofType"])
            for key in ("productionTime", "proofType", "manualProofType", "fileDelivery", "quantity"):
                target = expected.get(key)
                if key == "manualProofType" and str(expected.get("proofType")) != "100":
                    continue
                if target is not None and str(current["userSelectedOptions"].get(key)) != str(target):
                    _, current = await select(key, target)
            if changed_selections(baseline, current):
                raise ExtractionError("Baseline selections could not be restored", "baseline_reset_failed")
            return current

        await page.route("**/*", route_handler)
        try:
            await budget.check()
            await page.goto(document_url, wait_until="domcontentloaded", timeout=60000)
            stage = "baseline_initialize"
            state, baseline = await initialize_baseline()
            report.setdefault("baseline_selected_map", dict(baseline["userSelectedOptions"]))
            if initialization is not None:
                report["initialization_mismatches"] = initialization_mismatches(initialization, baseline)
                if report["initialization_mismatches"]:
                    raise ExtractionError("Source changed the requested public initialization", "initialization_not_applied")
            await phase_record("baseline", baseline)
            if on_baseline_ready is not None:
                if not canary_capture_ready(report, report["phases"][-1]):
                    raise ExtractionError("Retained session requires a matched source-pinned baseline", "domain_probe_unresolved")
                report["retained_domain_session"] = await on_baseline_ready(
                    page, dict(retained_headers), baseline, dict(report["loaded_contract"]))
            if canaries:
                report["requested_canary_selections"] = canaries
                if not canary_capture_ready(report, report["phases"][-1]):
                    raise ExtractionError("Baseline formula/source evidence is unresolved", "canary_baseline_unresolved")
                previous = baseline
                for canary in canaries:
                    phase, stage = canary["name"], "canary:" + canary["name"]
                    if str(previous["userSelectedOptions"].get(canary["key"])) == canary["value"]:
                        raise ExtractionError("Canary must change an effective selection", "canary_selection_unchanged")
                    _, current = await select(canary["key"], canary["value"], dropdown_only=True)
                    changes = changed_selections(previous, current)
                    confounded = set(changes) != {canary["key"]}
                    await phase_record(phase, current, previous,
                        status="confounded" if confounded else "observed",
                        reason="canary_dependent_selection_changed" if confounded else None)
                    if confounded:
                        raise ExtractionError("Canary changed an additional selection", "canary_dependent_selection_changed")
                    if not canary_capture_ready(report, report["phases"][-1]):
                        raise ExtractionError("Canary formula/source evidence is unresolved", "canary_quote_unresolved")
                    previous = current
            definitions = phases if phases is not None else ("manual_proof_1", "manual_proof_36", "delivery", "rush")
            # The complete controller capture already includes hidden services.
            # Preserve that valid witness before optional UI controls can fail.
            if definitions and state.get("disclosure"):
                stage = "optional_disclosure"
                await page.locator(state["disclosure"]).click(timeout=10000)
                state, disclosed = await settle()
                if changed_selections(baseline, disclosed):
                    raise ExtractionError("Opening optional controls changed baseline selections", "disclosure_changed_baseline")
            for name in definitions:
                phase = name
                stage = "phase:" + name
                try:
                    current = await restore(baseline)
                    if name in {"manual_proof_1", "manual_proof_36"}:
                        await select("proofType", "100")
                        _, current = await select("manualProofType", name.rsplit("_", 1)[1])
                    elif name == "delivery":
                        state, _ = await settle()
                        choices = [v for c in state["controls"] if c["key"] == "fileDelivery"
                                   for v in c["choices"] if str(v["value"]) != str(baseline["userSelectedOptions"].get("fileDelivery"))]
                        if not choices:
                            raise ExtractionError("No alternate delivery choice offered", "choice_unavailable")
                        _, current = await select("fileDelivery", choices[0]["value"])
                    elif name == "rush":
                        _, current = await select("productionTime", "3")
                    else:
                        raise ValueError("Unknown dependency phase")
                    await phase_record(name, current, baseline)
                except ExtractionError as exc:
                    report["phases"].append({"name": name, "status": "unsupported" if exc.code in {"control_unavailable", "choice_unavailable"} else "unresolved", "reason": exc.code})
                    if exc.code not in {"control_unavailable", "choice_unavailable"}:
                        raise
                phase = "reset_after_" + name
                stage = "baseline_reset"
                await restore(baseline)
            report["status"] = "finished"
        except ExtractionError as exc:
            report["reason"] = fatal or exc.code
            report["failure"] = exception_diagnostic(exc, stage)
        except Exception as exc:
            report["failure"] = exception_diagnostic(exc, stage)
            report["reason"] = fatal or report["failure"]["code"]
        finally:
            retained_headers.clear()
            report["last_phase"], report["last_stage"] = phase, stage
            pending_action = report.get("pending_action")
            if pending_action and pending_action["status"] != "completed":
                pending_action.update(status="unresolved", reason=report.get("reason", "action_not_completed"))
            try:
                report["public_dom_flags"] = await asyncio.wait_for(page.evaluate(PUBLIC_DOM_FLAGS_JS), timeout=2)
            except Exception as exc:
                report["public_dom_flags"] = {"unavailable": True, "exception_type": type(exc).__name__}
            # Keep the gate installed until the duplicate tab is closed. Removing
            # it first would leave late page timers able to send unbudgeted I/O.
            await page.close()
            for task in list(pending):
                task.cancel()
            await asyncio.gather(*list(pending), return_exceptions=True)
    evidence = await transport.record_observation(
        product_url, 200, {"content-type": "application/json"}, canonical(report),
        context={"lane": "browser_dependency_report"}, count_request=False)
    report["report_evidence_id"] = evidence.response_id
    return report
