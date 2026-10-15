"""In-memory ordinary browser authentication for explicitly started domain jobs.

Initialization observes the next request from an already open product tab. It
does not navigate, click, launch a browser, issue a supplier request, export
cookies, or verify price composition. A blank duplicate owns only a tab lifetime.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .price_contract import BASE_FIELDS, OPTION_FIELDS, CONTROLLER_PRICE_STATE_JS, SOURCE_CONTRACT, calculate_source_total
from .probe_jobs import BrowserHTTPClient, ORIGIN, READ_PATHS, session_headers
from .store import Product, Run, digest, uid
from .tls import tls_context
from .transport import ExtractionError, Transport

# The existing verified capture is deliberately called with source_contract={}.
# Its refusal remains visible. The separate branch below reads only the same
# public selected-value whitelist, and cannot become a pricing-contract witness.
DOMAIN_CONTROLLER_STATE_JS = r"""async () => {
 const contractCheck = (__VERIFIED_READER__)({source_contract:{},settled:false});
 if (!['www.printograph.com','printograph.com'].includes(location.hostname) || location.protocol !== 'https:')
   return {observed:false,reason:'unsupported_source_origin'};
 const element=document.querySelector('#order-form');
 if(!element) return {observed:false,reason:'controller_root_missing'};
 const roots=[element._vnode?.component?.proxy,element.__vue_app__?._instance?.proxy].filter(Boolean);
 const controllers=[...new Set(roots.map(root=>root.$refs?.childComponents?._?.subTree?.component?.refs?.orderFormInstance).filter(Boolean))];
 if(controllers.length!==1) return {observed:false,reason:'controller_missing_or_ambiguous'};
 const c=controllers[0], data=c.jsonObj, type=String(c.printProductType);
 if(!data?.userSelectedOptions || !data?.others || type!==element.getAttribute('data-printproducttype') || !['1','27','36'].includes(type))
   return {observed:false,reason:'controller_state_missing_or_mismatched'};
 const selected={}, scalar=v=>v===null || typeof v==='boolean' || (typeof v==='number'&&Number.isFinite(v)) || (typeof v==='string'&&v.length<=100);
 for(const key of [...__BASE_FIELDS__[type],...__OPTION_FIELDS__[type],'proofType','manualProofType','designServices','fileDelivery','quantity','productId','productionTime'])
   if(Object.hasOwn(data.userSelectedOptions,key)&&scalar(data.userSelectedOptions[key])) selected[key]=data.userSelectedOptions[key];
 const flags={}, present=v=>v!==''&&v!==null&&v!==undefined;
 for(const [out,key] of [['is_admin','isAdmin'],['waive_fees','waiveFees']])
   if(typeof data.others[key]==='boolean') flags[out]=data.others[key];
 if(Object.hasOwn(data.others,'itemId')) flags.has_item_id=present(data.others.itemId)&&data.others.itemId!=='null';
 if(Object.hasOwn(data.others,'editOrderItem')&&data.others.editOrderItem&&typeof data.others.editOrderItem==='object') flags.has_edit_order_item=Object.keys(data.others.editOrderItem).length>0;
 for(const [out,key] of [['has_shopping_cart_item','shoppingCartItemId'],['has_price_group','priceGroupId']])
   if(Object.hasOwn(data.userSelectedOptions,key)) flags[out]=present(data.userSelectedOptions[key]);
 return {observed:true,capture_kind:'domain_controller_diagnostic',source_contract:{},source_contract_verified:false,
   contract_check:contractCheck.reason||'not_verified',settled:false,composition_verified:false,
   product_type:type,store_type:element.getAttribute('data-currentstoretype'),currency:'USD',
   userSelectedOptions:selected,other_context:flags,
   field_options:Array.isArray(c.fields?.[type]?.options)?c.fields[type].options.filter(v=>typeof v==='string'&&v.length<=100):null};
}""".replace("__VERIFIED_READER__", CONTROLLER_PRICE_STATE_JS).replace(
    "__BASE_FIELDS__", json.dumps(BASE_FIELDS)).replace("__OPTION_FIELDS__", json.dumps(OPTION_FIELDS))


def public_controller_state(value):
    """Validate again in Python; mocks or changed source cannot export extra data."""
    if not isinstance(value, dict) or value.get("observed") is not True:
        raise ExtractionError("Public product controller is unavailable; session setup needs attention", "domain_controller_unavailable")
    product_type = str(value.get("product_type", ""))
    if product_type not in BASE_FIELDS or value.get("store_type") not in {"broker", "retail"} or value.get("currency") != "USD":
        raise ExtractionError("Public product/store/currency context is unresolved", "domain_controller_unavailable")
    allowed = set(BASE_FIELDS[product_type]) | set(OPTION_FIELDS[product_type]) | {
        "proofType", "manualProofType", "designServices", "fileDelivery", "quantity", "productId", "productionTime"}
    selected = value.get("userSelectedOptions")
    if not isinstance(selected, dict) or any(key not in selected or selected[key] is None or selected[key] == "" for key in ("proofType", "fileDelivery")):
        raise ExtractionError("Proof and file-delivery values must be explicitly observed for domain reads", "domain_services_unresolved")
    def scalar(item):
        return item is None or isinstance(item, (bool, int)) or (isinstance(item, str) and len(item) <= 100)
    selected = {key: deepcopy(item) for key, item in selected.items() if key in allowed and scalar(item)}
    if not {"proofType", "fileDelivery"}.issubset(selected):
        raise ExtractionError("Unsupported service value shape", "domain_services_unresolved")
    flag_keys = {"is_admin", "waive_fees", "has_item_id", "has_edit_order_item", "has_shopping_cart_item", "has_price_group"}
    raw_flags = value.get("other_context")
    flags = {key: item for key, item in (raw_flags if isinstance(raw_flags, dict) else {}).items() if key in flag_keys and isinstance(item, bool)}
    options = value.get("field_options")
    options = [item for item in options if isinstance(item, str) and item in allowed] if isinstance(options, list) else None
    return {"observed": True, "capture_kind": "domain_controller_diagnostic", "source_contract": {},
            "source_contract_verified": False, "settled": False, "composition_verified": False,
            "product_type": product_type, "store_type": value["store_type"], "currency": "USD",
            "userSelectedOptions": selected, "other_context": flags, "field_options": options}


class _DomainHTTPClient(BrowserHTTPClient):
    async def aclose(self):
        # The shared connection pool and its jar belong to the session manager.
        # Native browser cookies remain owned by Chromium, never exported.
        self._headers.clear()


@dataclass
class _Session:
    id: str
    run_id: str
    context: object
    page: object
    client: object
    headers: dict = field(repr=False)
    public_context: dict
    public_controller: dict
    active: bool = True
    close_listener: object = field(default=None, repr=False)


class SourceDomainSessions:
    def __init__(self, store, browser_pool, *, client_factory=None):
        self.store, self.pool = store, browser_pool
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(
            verify=tls_context(), timeout=30, follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4)))
        self.sessions, self._initializing = {}, set()
        self._closed = False

    async def retain_probe_context(self, run_id, product_url, page, observed_headers, capture, loaded_contract):
        """Retain an already-budgeted probe's native context; issue no requests.

        Every handoff creates a new public scope. It never adopts response IDs
        or a session identifier from historical discovery/price evidence.
        """
        if self._closed or page.is_closed() or page.context not in set(getattr(self.pool, "_contexts", {}).values()):
            raise ExtractionError("The probe browser context is no longer owned", "domain_session_unavailable")
        allowed = {ORIGIN + "/products/" + slug + "/order" for slug in ("business-cards", "club-flyers", "roll-labels")}
        if product_url not in allowed or page.url.split("?")[0] != product_url:
            raise ExtractionError("The probe page does not match its source product", "domain_context_changed")
        with self.store.session() as s:
            run = s.get(Run, run_id)
            products = list(s.scalars(select(Product).where(Product.run_id == run_id, Product.url == product_url)))
            if not run or len(products) != 1:
                raise LookupError("Exactly one existing run product is required")
            if run.status in {"paused", "cancelled"}:
                raise ExtractionError("Run paused during probe handoff", "interrupted")
        formula = calculate_source_total(capture)
        if (loaded_contract != SOURCE_CONTRACT or capture.get("capture_kind") != "controller"
                or formula.get("status") != "complete" or formula.get("comparison") != "matched"):
            raise ExtractionError("A source-pinned matched controller baseline is required", "domain_probe_unresolved")
        before = public_controller_state(capture)
        after = public_controller_state(await page.evaluate(DOMAIN_CONTROLLER_STATE_JS))
        if before != after:
            raise ExtractionError("Controller context changed before session handoff", "domain_context_changed")
        headers = {key: value for key, value in session_headers(observed_headers).items() if value}
        if not headers or not any(key in {"authorization", "api-vendor-token", "x-csrf-token", "x-xsrf-token"} or key.startswith("api-") for key in headers):
            raise ExtractionError("No actual source session headers were observed", "session_bootstrap_required")
        duplicate = client = None
        try:
            duplicate = await page.context.new_page()
            # Recheck after the awaited allocation; never bind a superseded page.
            if page.is_closed() or page.url.split("?")[0] != product_url or public_controller_state(await page.evaluate(DOMAIN_CONTROLLER_STATE_JS)) != after:
                raise ExtractionError("Controller changed during session handoff", "domain_context_changed")
            session_id, epoch = uid(), uid()
            services = {"selected": {k: after["userSelectedOptions"].get(k) for k in
                        ("proofType", "manualProofType", "designServices", "fileDelivery")},
                        "other_context": after["other_context"], "store_type": after["store_type"], "currency": after["currency"]}
            context = {"origin": ORIGIN, "session_scope": session_id, "source_epoch": epoch,
                       "store_type": after["store_type"], "currency": after["currency"],
                       "service_signature": digest(services), "domain_controller_state": after,
                       "bootstrap": {"method": "matched_initialized_probe", "product_url": product_url,
                                     "source_contract": dict(loaded_contract), "controller_context_hash": formula["context_hash"]}}
            client = self.client_factory()
            session = _Session(session_id, run_id, page.context, duplicate, client, dict(headers), context, after)
            def context_closed():
                session.active = False
            session.close_listener = context_closed
            page.context.on("close", context_closed)
            self.sessions[session_id] = session
            duplicate = client = None
            return self.status(session_id)
        finally:
            headers.clear()
            if duplicate is not None and not duplicate.is_closed():
                await duplicate.close()
            if client is not None:
                await client.aclose()

    async def initialize(self, run_id, product_id, *, wait_seconds=20):
        if self._closed:
            raise ExtractionError("Session manager is closed", "domain_session_unavailable")
        if not 0 < wait_seconds <= 20:
            raise ValueError("Passive session observation must wait at most 20 seconds")
        with self.store.session() as s:
            run, product = s.get(Run, run_id), s.get(Product, product_id)
            if not run or not product or product.run_id != run_id:
                raise LookupError("Run/product not found")
            if run.status in {"paused", "cancelled"}:
                raise ExtractionError("Run is paused or cancelled", "interrupted")
            product_url = product.url
        if not product_url.startswith(ORIGIN + "/products/"):
            raise ValueError("Session initialization requires an existing Printograph product")
        # Use already-owned contexts only. Calling pool._context would launch a
        # browser on a cold process, which this passive setup explicitly avoids.
        pages = [(context, page) for context in set(getattr(self.pool, "_contexts", {}).values())
                 for page in context.pages if not page.is_closed() and page.url.split("?")[0] == product_url.split("?")[0]]
        if len(pages) != 1:
            raise ExtractionError("Exactly one existing product tab is needed; open/retain its ordinary browser session", "session_bootstrap_required")
        browser_context, observed_page = pages[0]
        key = (run_id, id(browser_context))
        if key in self._initializing:
            raise ExtractionError("Session initialization is already in progress", "domain_session_busy")
        self._initializing.add(key)
        observed, pending, headers = asyncio.Event(), set(), {}
        duplicate = client = None
        async def observe(request):
            try:
                parsed = urlsplit(request.url)
                if (f"{parsed.scheme}://{parsed.netloc}" != ORIGIN or not READ_PATHS.fullmatch(parsed.path)
                        or request.frame.page is not observed_page or request.method != "GET"):
                    return
                private = await request.all_headers()
                headers.update(session_headers(private))
                private.clear()
                observed.set()
            except Exception:
                # An ordinary page navigation can detach its request frame.
                # Wait for the next usable request inside the same fixed bound.
                pass
        def listener(request):
            task = asyncio.create_task(observe(request))
            pending.add(task)
            task.add_done_callback(pending.discard)
        browser_context.on("request", listener)
        try:
            before = public_controller_state(await observed_page.evaluate(DOMAIN_CONTROLLER_STATE_JS))
            try:
                await asyncio.wait_for(observed.wait(), timeout=wait_seconds)
            except TimeoutError:
                raise ExtractionError("No ordinary product request was observed; session setup needs attention", "session_bootstrap_required") from None
            after = public_controller_state(await observed_page.evaluate(DOMAIN_CONTROLLER_STATE_JS))
            services = lambda value: {"selected": {k: value["userSelectedOptions"].get(k) for k in
                ("proofType", "manualProofType", "designServices", "fileDelivery")}, "other_context": value["other_context"],
                "store_type": value["store_type"], "currency": value["currency"]}
            if services(before) != services(after) or observed_page.is_closed() or observed_page.url.split("?")[0] != product_url.split("?")[0]:
                raise ExtractionError("Source service context changed during passive session setup", "domain_context_changed")
            with self.store.session() as s:
                if s.get(Run, run_id).status in {"paused", "cancelled"}:
                    raise ExtractionError("Run paused during session setup", "interrupted")
            # A blank duplicate shares native login without loading a source URL.
            duplicate = await browser_context.new_page()
            session_id, epoch = uid(), uid()
            public_context = {"origin": ORIGIN, "session_scope": session_id, "source_epoch": epoch,
                              "store_type": after["store_type"], "currency": after["currency"],
                              "service_signature": digest(services(after)), "domain_controller_state": after}
            client = self.client_factory()
            session = _Session(session_id, run_id, browser_context, duplicate, client,
                               dict(headers), public_context, after)
            # The duplicate is only an owned UI resource. Closing a tab does
            # not close Chromium's shared authenticated context or cookie jar.
            def context_closed():
                session.active = False
            session.close_listener = context_closed
            browser_context.on("close", context_closed)
            self.sessions[session_id] = session
            duplicate = client = None
            return self.status(session_id)
        finally:
            browser_context.remove_listener("request", listener)
            for task in list(pending):
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            headers.clear()
            self._initializing.discard(key)
            if duplicate is not None and not duplicate.is_closed():
                await duplicate.close()
            if client is not None:
                await client.aclose()

    def status(self, session_id):
        session = self.sessions.get(session_id)
        if not session:
            raise LookupError("Domain session not found; initialize again after application restart")
        return {"session_scope": session.id, "source_epoch": session.public_context["source_epoch"],
                "run_id": session.run_id, "status": "ready" if session.active else "unavailable",
                "context": deepcopy(session.public_context), "controller_state": deepcopy(session.public_controller),
                "composition_verified": False, "source_requests_issued": 0}

    def validate_context(self, claim):
        request_context = claim["request"]["context"]
        session = self.sessions.get(request_context.get("session_scope"))
        if not session or not session.active or session.run_id != claim["run_id"]:
            raise ExtractionError("Initialize the ordinary browser session before starting domain reads", "domain_session_unavailable")
        if any(request_context.get(key) != value for key, value in session.public_context.items()):
            raise ExtractionError("Domain request is not bound to the initialized session and public service context", "domain_context_changed")
        return session

    @asynccontextmanager
    async def factory(self, claim, budget):
        session = self.validate_context(claim)
        await budget.check()
        client = _DomainHTTPClient(session.context, session.headers, budget, client=session.client)
        transport = Transport(self.store, claim["run_id"], claim["product_id"], client=client, refresh=True)
        transport.check = budget.check
        try:
            yield transport
        finally:
            await client.aclose()

    async def close(self):
        self._closed = True
        for session in list(self.sessions.values()):
            session.active = False
            if session.close_listener is not None:
                session.context.remove_listener("close", session.close_listener)
            session.headers.clear()
            session.client.cookies.clear()
            await session.client.aclose()
            if not session.page.is_closed():
                await session.page.close()
        self.sessions.clear()
