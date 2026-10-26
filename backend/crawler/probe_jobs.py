"""Bounded, durable, read-only source probes using the native login session.

A probe is not a catalog harvest or a completed product. Every outgoing supplier
attempt, including scripts during bootstrap and retries, reserves budget first.
Headers remain in memory and are scoped to the exact supplier origin.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import re
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from .store import ProbeJob, ProbeAttempt, Product, Run, canonical, uid, safe_public_context
from .transport import Transport, ExtractionError, RateLimiter, validate_url
from .pacing import source_pacing, retry_after_seconds
from .tls import tls_context

ORIGIN = "https://www.printograph.com"
ASSET_ORIGIN = "https://static.printograph.com"
API = "/service/rest/v1/"
READ_PATHS = re.compile(r"^/service/rest/v1/(?:products(?:/\d+(?:/options(?:/\d+/prices)?|/prices)?|/quantities|/options/quantities)?|settings/product/specifications)$")
# Public quote routes only. Cart/order/payment/upload/customer mutations are absent.
QUOTE_POSTS = {API + "rules/turnarounds", API + "product/prices"}
AUTH_HEADERS = {"api-vendor-token", "authorization", "x-csrf-token", "x-xsrf-token"}

# Fixed Chromium identifiers only: never persist raw exception/request text.
_PUBLIC_NETWORK_ERRORS = frozenset({
    "ERR_ABORTED", "ERR_FAILED", "ERR_BLOCKED_BY_CLIENT", "ERR_BLOCKED_BY_RESPONSE",
    "ERR_CERT_AUTHORITY_INVALID", "ERR_CERT_DATE_INVALID", "ERR_CERT_COMMON_NAME_INVALID",
    "ERR_CERT_INVALID", "ERR_SSL_PROTOCOL_ERROR", "ERR_SSL_VERSION_OR_CIPHER_MISMATCH",
    "ERR_CONNECTION_RESET", "ERR_CONNECTION_REFUSED", "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_TIMED_OUT", "ERR_NAME_NOT_RESOLVED", "ERR_TIMED_OUT",
    "ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED", "ERR_NETWORK_CHANGED",
    "ERR_INTERNET_DISCONNECTED", "ERR_HTTP_RESPONSE_CODE_FAILURE", "ERR_TOO_MANY_REDIRECTS",
    "ERR_EMPTY_RESPONSE", "ERR_HTTP2_PROTOCOL_ERROR",
})
_PROBE_STAGES = frozenset({"initializing", "budget_check", "browser_session", "bootstrap_navigation",
                           "bootstrap_transport", "source_requests", "browser_probe"})


def probe_failure_diagnostic(exc, stage, request_failure=None):
    """A bounded public error category, with no URL, credentials or raw text."""
    message = str(exc)
    tokens = re.findall(r"(?<![A-Z0-9_])(ERR_[A-Z0-9_]+)(?![A-Z0-9_])", message)
    tokens += re.findall(r"(?<![A-Z0-9_])(ERR_[A-Z0-9_]+)(?![A-Z0-9_])", str(request_failure or ""))
    network_error = next((token for token in tokens if token in _PUBLIC_NETWORK_ERRORS), None)
    lower = message.lower()
    category = "browser_error"
    if network_error and network_error.startswith(("ERR_CERT_", "ERR_SSL_")):
        category = "tls_error"
    elif network_error == "ERR_ABORTED" or "interrupted by another navigation" in lower:
        category = "navigation_interrupted"
    elif network_error:
        category = "network_error"
    elif "target closed" in lower or "target page, context or browser has been closed" in lower:
        category = "target_closed"
    elif type(exc).__name__ == "TimeoutError":
        category = "timeout"
    name = type(exc).__name__
    return {"stage": stage if stage in _PROBE_STAGES else "unknown",
            "exception_type": name if name in {"Error", "TimeoutError", "ExtractionError", "OSError", "RuntimeError", "ValueError"} else "Exception",
            "category": category, "network_error": network_error}


def session_headers(incoming):
    """Only same-origin custom transport headers; native cookies remain native."""
    return {k.lower(): v for k,v in incoming.items()
            if k.lower() in AUTH_HEADERS or k.lower().startswith(("api-", "x-"))}


def bootstrap_transport_witness(method, url, incoming):
    """Use an ordinary request's actual headers; this is not a login check.

    Startup settings requests precede product requests. Bootstrap aborts their
    responses, so waiting exclusively for a later product request can deadlock.
    """
    from .browser_dependency_probe import STARTUP_GETS
    parsed = urlparse(url)
    if (method != "GET" or f"{parsed.scheme}://{parsed.netloc}" != ORIGIN
            or parsed.username or parsed.password
            or not (READ_PATHS.fullmatch(parsed.path) or parsed.path in STARTUP_GETS)):
        return None
    headers = {key: value for key, value in session_headers(incoming).items() if value}
    # Mere X-Requested-With does not establish the session transport needed by
    # the source client. Cookie state stays in Chromium and is never exported.
    if not any(key in AUTH_HEADERS or key.startswith("api-") for key in headers):
        return None
    return headers
PUBLIC_QUERY = {"productType", "requestValues", "turnaround", "size", "orientation", "paper", "color", "coating", "shape", "paperFinish", "finish", "quantity", "quantities", "optionIds", "optionId", "includePrices", "includeTurnaround", "priceGroup", "count", "offset", "limit", "page", "eddm", "itemId", "cid"}


def public_url(url):
    from urllib.parse import parse_qsl, urlencode, urlunsplit
    p = urlparse(url)
    query = [(k, v if k in PUBLIC_QUERY else "[redacted]") for k, v in parse_qsl(p.query, keep_blank_values=True)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))


def validate_probe(request):
    if set(request) - {"id", "method", "path", "params", "json", "context"}:
        raise ValueError("Unknown probe fields are not stored")
    if safe_public_context(request) != request:
        raise ValueError("Probe payload must not contain credential fields")
    method = request.get("method", "GET").upper()
    path = request.get("path", "")
    p = urlparse(path)
    if p.scheme or p.netloc or p.query or p.fragment or ".." in path:
        raise ValueError("Probe path must be an exact supplier API path")
    if not ((method == "GET" and READ_PATHS.fullmatch(path)) or (method == "POST" and path in QUOTE_POSTS)):
        raise ValueError("Only captured product discovery and calculation routes are allowed")
    params = request.get("params", {})
    if not isinstance(params, dict) or set(params) - PUBLIC_QUERY:
        raise ValueError("Probe contains an unknown or private query parameter")
    if any(not isinstance(v, (str, int, bool)) or len(str(v)) > 1000 for v in params.values()):
        raise ValueError("Probe query values must be bounded scalars")
    if params.get("cid", "") != "" or params.get("itemId", "null") != "null":
        raise ValueError("Probes cannot target customer or cart item identities")
    if len(canonical(request)) > 50000:
        raise ValueError("Probe specification exceeds size limit")
    context = request.get("context", {})
    if safe_public_context(context) != context:
        raise ValueError("Probe context must not contain credentials")
    if method == "POST" and path == API + "product/prices":
        body = request.get("json", {})
        if not isinstance(body, dict) or set(body) != {"properties"} or not isinstance(body["properties"], dict):
            raise ValueError("Only specification-only calculation payloads are allowed")
        props = body["properties"]
        if set(props) - {"size", "printProductType", "turnaround", "quantity", "other"}:
            raise ValueError("Unknown quote properties")
        other = props.get("other", {})
        if not isinstance(other, dict) or set(other) - {"paper", "color", "orientation", "shape", "paperFinish", "finish", "coating"}:
            raise ValueError("Unknown quote specification")
        if any(not isinstance(v, (str, int, bool)) for k,v in props.items() if k != "other") or any(not isinstance(v, (str, int, bool)) for v in other.values()):
            raise ValueError("Quote specifications must be scalar source values")
    if method == "POST" and path == API + "rules/turnarounds":
        body = request.get("json", {})
        if not isinstance(body, dict) or set(body) - {"quantity", "specifications", "productOptions", "itemType"}:
            raise ValueError("Unknown turnaround quote fields")
    return {**request, "method": method}


class ProbeBudget:
    def __init__(self, store, job_id, owner):
        self.store, self.job_id, self.owner = store, job_id, owner

    async def check(self):
        with self.store.session() as s:
            job = s.get(ProbeJob, self.job_id)
            run = s.get(Run, job.run_id) if job else None
            if not job or job.owner != self.owner or job.status != "running":
                raise ExtractionError("Probe lease ended", "probe_interrupted")
            if not run or run.status in {"cancelled", "paused"}:
                raise ExtractionError("Run paused or cancelled", "probe_interrupted")

    def reserve(self, phase, method, url):
        with self.store.session(immediate=True) as s:
            job = s.get(ProbeJob, self.job_id, with_for_update=not self.store.sqlite)
            if not job or job.status != "running" or job.owner != self.owner:
                raise ExtractionError("Probe lease ended", "probe_interrupted")
            run = s.get(Run, job.run_id)
            if not run or run.status in {"paused", "cancelled"}:
                raise ExtractionError("Run paused or cancelled", "probe_interrupted")
            if job.used >= job.budget:
                raise ExtractionError("Durable supplier probe budget exhausted", "probe_budget_exhausted")
            job.used += 1
            job.updated_at = time.time()
            attempt = ProbeAttempt(job_id=job.id, sequence=job.used, phase=phase, method=method,
                                   public_url=public_url(url))
            s.add(attempt)
            s.flush()
            return attempt.id

    def finish(self, attempt_id, status, elapsed):
        with self.store.session(immediate=True) as s:
            row = s.get(ProbeAttempt, attempt_id)
            if row and row.job_id == self.job_id:
                row.status, row.elapsed = status, elapsed


class BrowserHTTPClient:
    """OS-trusted HTTP using the user's native browser login in memory only.

    Chromium and Playwright's Node HTTP client can have different trust stores.
    Use Hermes' OS-trusted HTTPX pool; transfer only cookies valid for this exact
    origin. Never serialize the jar, headers, or cookie-bearing request objects.
    """
    def __init__(self, context, headers, budget, client=None):
        self.context, self._headers, self.budget = context, dict(headers), budget
        self.client = client or httpx.AsyncClient(verify=tls_context(), timeout=30,
            follow_redirects=False, limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
        self.owns_client = client is None

    async def aclose(self):
        self._headers.clear()
        self.client.cookies.clear()
        if self.owns_client:
            await self.client.aclose()

    @asynccontextmanager
    async def stream(self, method, url, *, params=None, json=None, data=None, headers=None):
        from urllib.parse import parse_qsl
        from http.cookies import SimpleCookie
        from email.utils import parsedate_to_datetime
        p = urlparse(url)
        if f"{p.scheme}://{p.netloc}" != ORIGIN:
            raise ExtractionError("Session transport cannot leave its supplier origin", "invalid_url")
        query = parse_qsl(p.query, keep_blank_values=True)
        if len(dict(query)) != len(query) or set(dict(query)) & set(params or {}):
            raise ExtractionError("Ambiguous supplier query", "invalid_url")
        validate_probe({"method": method, "path": p.path,
                        "params": {**dict(query), **(params or {})}, "json": json, "context": {}})
        await self.budget.check()
        # Chromium filters by URL, including secure/domain/path/expiry rules.
        cookies = await self.context.cookies([url])
        safe = {k: v for k, v in (headers or {}).items()
                if k.lower() in {"accept", "if-none-match", "if-modified-since"}}
        safe.update(self._headers)
        if cookies:
            safe["Cookie"] = "; ".join(c["name"] + "=" + c["value"] for c in cookies)
        cookies.clear()
        attempt = await asyncio.to_thread(self.budget.reserve, "probe", method, url)
        started, status = time.monotonic(), 0
        try:
            async with self.client.stream(method, url, params=params, json=json,
                                          data=data, headers=safe) as result:
                status = result.status_code
                # Apply only server-issued cookies for this supplier back to the
                # same native context, so normal session rotation stays coherent.
                updates = []
                for raw in result.headers.get_list("set-cookie"):
                    jar = SimpleCookie()
                    try:
                        jar.load(raw)
                    except Exception:
                        continue
                    for name, morsel in jar.items():
                        domain = morsel["domain"] or p.hostname
                        if domain.lstrip(".") not in {"printograph.com", "www.printograph.com"}:
                            continue
                        cookie = {"name": name, "value": morsel.value, "domain": domain,
                                  "path": morsel["path"] or "/", "secure": bool(morsel["secure"]),
                                  "httpOnly": bool(morsel["httponly"])}
                        if morsel["samesite"].lower() in {"lax", "strict", "none"}:
                            cookie["sameSite"] = morsel["samesite"].capitalize()
                        try:
                            if morsel["max-age"]:
                                cookie["expires"] = max(1, time.time() + int(morsel["max-age"]))
                            elif morsel["expires"]:
                                cookie["expires"] = max(1, parsedate_to_datetime(morsel["expires"]).timestamp())
                        except (ValueError, TypeError, OverflowError):
                            pass
                        updates.append(cookie)
                if updates:
                    await self.context.add_cookies(updates)
                updates.clear()
                yield result
        finally:
            safe.clear()
            await asyncio.to_thread(self.budget.finish, attempt, status, time.monotonic() - started)


class ProbeTransport(Transport):
    async def check(self):
        await self.probe_budget.check()


def completed_probe_ids(results):
    """Resuming an append-only experiment must not repeat HTTP outcomes.

    Transport has already applied its HTTP retry policy. A deliberate retest
    gets a new manifest ID; interrupted/auth/transport failures remain resumable.
    """
    latest = {item["id"]: item for item in results}
    return {ident for ident, item in latest.items()
            if item.get("status") == "observed"
            or (item.get("status") == "failed" and item.get("code") == "http_error")}


class ProbeRunner:
    def __init__(self, store, browser_pool, *, on_stage_finished=None, domain_sessions=None):
        self.store, self.pool = store, browser_pool
        self.tasks = {}
        self.on_stage_finished = on_stage_finished
        self.domain_sessions = domain_sessions

    def start(self, run_id, manifest, *, job_id=None, budget=400, resume_run=False):
        if set(manifest) - {"product_url", "purpose", "prior_public_contract_requests", "requests", "browser_probes"}:
            raise ValueError("Unknown probe manifest fields")
        requests = [validate_probe(r) for r in manifest.get("requests", [])]
        browser_probes = manifest.get("browser_probes", [])
        if not isinstance(browser_probes, list) or len(browser_probes) > 12:
            raise ValueError("At most twelve controlled probe attempts across the three selected products")
        allowed_urls = {ORIGIN + "/products/" + slug + "/order" for slug in ("business-cards", "club-flyers", "roll-labels")}
        for probe in browser_probes:
            if not isinstance(probe, dict) or set(probe) - {"id", "product_url", "phases", "initialization", "canary_selections", "retain_domain_session"}:
                raise ValueError("Invalid controlled browser probe")
            if "retain_domain_session" in probe and (type(probe["retain_domain_session"]) is not bool
                    or probe["retain_domain_session"] and (self.domain_sessions is None or not probe.get("initialization") or probe.get("phases") != [] or probe.get("canary_selections"))):
                raise ValueError("Retained session requires an initialized baseline-only probe and session manager")
            if probe.get("product_url") not in allowed_urls or not isinstance(probe.get("id"), str):
                raise ValueError("Controlled probes require a selected product and ID")
            phases = probe.get("phases", [])
            if not isinstance(phases, list) or set(phases) - {"manual_proof_1", "manual_proof_36", "delivery", "rush"}:
                raise ValueError("Unknown controlled probe phase")
            if "initialization" in probe:
                from .browser_dependency_probe import initialized_product_url
                initialized_product_url(probe["product_url"], probe["initialization"])
            if "canary_selections" in probe:
                from .browser_dependency_probe import validated_canary_selections
                validated_canary_selections(probe["product_url"], probe.get("initialization"),
                                             phases, probe["canary_selections"])
        if not (requests or browser_probes) or len(requests) > 300 or not 1 <= budget <= 400:
            raise ValueError("Supply 1–300 read-only probes within a 400-attempt ceiling")
        url = manifest.get("product_url", "")
        if url not in allowed_urls:
            raise ValueError("Bootstrap must use one of the three selected product pages")
        ids = [r.get("id", str(i)) for i,r in enumerate(requests)] + [r["id"] for r in browser_probes]
        if any(not isinstance(i,str) or len(i)>100 for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Probe IDs must be unique bounded strings")
        job_id, owner = job_id or uid(), uid()
        manifest = {**manifest, "requests": requests}
        with self.store.session(immediate=True) as s:
            run = s.get(Run, run_id)
            if not run:
                raise LookupError("Run not found")
            if resume_run:
                if run.status not in {"paused", "queued", "running"}:
                    raise ValueError("Only a paused or active run can start a canary stage")
                # This resumes only the finite stage. Individually paused
                # product walkers keep their state, epochs and saved rows.
                run.status, run.updated_at = "running", time.time()
            previous = s.get(ProbeJob, job_id)
            if previous:
                old = previous.manifest
                same_setup = {k:v for k,v in old.items() if k not in {"requests", "browser_probes"}} == {k:v for k,v in manifest.items() if k not in {"requests", "browser_probes"}}
                extends = manifest["requests"][:len(old["requests"])] == old["requests"]
                old_browser = old.get("browser_probes", [])
                extends = extends and browser_probes[:len(old_browser)] == old_browser
                if previous.run_id != run_id or not same_setup or not extends or previous.budget != budget:
                    raise ValueError("An existing probe budget or completed request cannot be replaced; append requests only")
                if previous.status == "running" and job_id in self.tasks:
                    if manifest != old:
                        raise ValueError("Wait for the current probe batch before appending")
                    return job_id
                previous.owner, previous.status, previous.error = owner, "running", None
                previous.manifest = manifest
            else:
                s.add(ProbeJob(id=job_id, run_id=run_id, budget=budget, used=0, owner=owner,
                               status="running", manifest=manifest, results=[]))
        self.tasks[job_id] = asyncio.create_task(self.run(job_id, owner))
        return job_id

    def has_active_scope(self, run_id, session):
        live = [key for key, task in tuple(self.tasks.items()) if not task.done()]
        return bool(live and session.scalar(select(ProbeJob.id).where(
            ProbeJob.run_id == run_id, ProbeJob.id.in_(live), ProbeJob.status == 'running').limit(1)))

    def status(self, job_id):
        with self.store.session() as s:
            job = s.get(ProbeJob, job_id)
            if not job:
                raise LookupError("Probe not found")
            return {k: getattr(job, k) for k in ("id", "run_id", "status", "budget", "used", "created_at", "updated_at", "manifest", "results", "error")}

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def run(self, job_id, owner):
        budget = ProbeBudget(self.store, job_id, owner)
        state = self.status(job_id)
        manifest, results = state["manifest"], list(state["results"])
        limiter = RateLimiter(self.store)
        profile = next((p for p in self.store.profiles("source") if p.get("host") == "www.printograph.com"), {})
        auth, auth_ready, pending = {}, asyncio.Event(), set()
        auth_lock = asyncio.Lock()
        failure, listening_context, listener, client = None, None, None, None
        stage, bootstrap_request_failure = "initializing", None
        try:
            rps, concurrency = source_pacing(profile, self.store.config)
            stage = "budget_check"
            await budget.check()
            await validate_url(manifest["product_url"])
            stage = "browser_session"
            async with self.pool.page(ORIGIN, headless=os.getenv("ATLAS_BROWSER_HEADLESS", "true").lower() != "false") as page:
                def bootstrap_failed(request):
                    nonlocal bootstrap_request_failure
                    if request.is_navigation_request():
                        # Keep only a known code even in transient diagnostic state.
                        bootstrap_request_failure = probe_failure_diagnostic(
                            RuntimeError(""), "bootstrap_navigation", request.failure)["network_error"]
                page.on("requestfailed", bootstrap_failed)
                async def capture_transport(request, incoming=None):
                    incoming = incoming if incoming is not None else await request.all_headers()
                    headers = bootstrap_transport_witness(request.method, request.url, incoming)
                    if headers is None:
                        return
                    async with auth_lock:
                        auth.update(headers)
                        if not auth_ready.is_set():
                            await asyncio.to_thread(self.store.event, state["run_id"], "probe_session_transport", {
                                "probe_id": job_id, "path": urlparse(request.url).path,
                                "header_names": sorted(headers), "native_cookie_jar": True,
                                "authentication_validated": False})
                            auth_ready.set()
                async def observe(request):
                    task = asyncio.current_task()
                    try:
                        p = urlparse(request.url)
                        if f"{p.scheme}://{p.netloc}" != ORIGIN or not p.path.startswith(API):
                            return
                        await capture_transport(request)
                    finally:
                        pending.discard(task)
                def on_request(request):
                    task = asyncio.create_task(observe(request))
                    pending.add(task)
                listening_context, listener = page.context, on_request
                listening_context.on("request", listener)
                async def route(route):
                    task = asyncio.current_task()
                    pending.add(task)
                    request, token, attempt = route.request, None, None
                    began, elapsed, code, delay = None, None, 0, 0
                    try:
                        p = urlparse(request.url)
                        origin = f"{p.scheme}://{p.netloc}"
                        if origin not in {ORIGIN, ASSET_ORIGIN} or request.resource_type in {"image", "font", "media"}:
                            await route.abort()
                            return
                        if origin == ORIGIN and p.path.startswith(API):
                            incoming = await request.all_headers()
                            auth.update(session_headers(incoming))
                            await capture_transport(request, incoming)
                            # Bootstrap extracts ordinary session transport only. No
                            # calculator API calls run before the explicit probe list.
                            await route.abort()
                            return
                        if request.method != "GET" or request.resource_type not in {"document", "script", "stylesheet"}:
                            await route.abort()
                            return
                        token = await limiter.acquire(origin, rps, concurrency, budget.check)
                        attempt = await asyncio.to_thread(budget.reserve, "bootstrap", request.method, request.url)
                        began = time.monotonic()
                        await route.continue_()
                        response = await request.response()
                        if response:
                            code = response.status
                            delay = retry_after_seconds(response.headers.get("retry-after"), default=0)
                            await response.finished()
                        elapsed = time.monotonic() - began
                    except (ExtractionError, asyncio.CancelledError):
                        elapsed = time.monotonic() - began if began is not None else 0
                        try:
                            await route.abort()
                        except Exception:
                            pass
                    except Exception:
                        elapsed = time.monotonic() - began if began is not None else 0
                        try:
                            await route.abort()
                        except Exception:
                            pass
                    finally:
                        if elapsed is None:
                            elapsed = time.monotonic() - began if began is not None else 0
                        try:
                            if token:
                                await asyncio.to_thread(limiter.release, origin, token, elapsed, code, delay, rps)
                        finally:
                            try:
                                if attempt:
                                    await asyncio.to_thread(budget.finish, attempt, code, elapsed)
                            finally:
                                pending.discard(task)
                await page.route("**/*", route)
                # A sibling scraper tab already has the ordinary supplier transport.
                # Observe its next normal product request without issuing another one.
                if any(p != page and p.url.startswith(ORIGIN + "/products/") for p in page.context.pages):
                    try:
                        await asyncio.wait_for(auth_ready.wait(), timeout=20)
                    except TimeoutError:
                        pass
                if not auth_ready.is_set():
                    stage = "bootstrap_navigation"
                    await page.goto(manifest["product_url"], wait_until="domcontentloaded", timeout=60000)
                stage = "bootstrap_transport"
                try:
                    await asyncio.wait_for(auth_ready.wait(), timeout=25)
                except TimeoutError:
                    raise ExtractionError("Normal source transport did not initialize; login/bootstrap needs attention", "session_bootstrap_required")
                listening_context.remove_listener("request", listener)
                listening_context, listener = None, None
                client = BrowserHTTPClient(page.context, auth, budget)
                transport = ProbeTransport(self.store, state["run_id"], "", client=client, refresh=True)
                transport.probe_budget = budget
                transport.created_at = state["created_at"]
                done = completed_probe_ids(results)
                stage = "source_requests"
                for index, request in enumerate(manifest["requests"]):
                    ident = request.get("id", str(index))
                    if ident in done:
                        continue
                    await budget.check()
                    context = {"lane": "http_source", "probe_job": job_id, "scenario": request.get("context", {}),
                               "session_generation": owner}
                    try:
                        fetched = await transport.fetch(ORIGIN + request["path"], method=request["method"],
                            params=request.get("params"), json=request.get("json"), context=context)
                        result = {"id": ident, "status": "observed", "http_status": fetched.status,
                                  "response_id": fetched.response_id, "cached": fetched.cached}
                    except ExtractionError as exc:
                        result = {"id": ident, "status": "failed", "code": exc.code}
                        fatal = exc.code in {"probe_budget_exhausted", "probe_interrupted", "access_required", "network_error", "tls_trust_error"}
                    else:
                        fatal = False
                    results.append(result)
                    with self.store.session(immediate=True) as s:
                        job = s.get(ProbeJob, job_id)
                        if job.owner != owner:
                            raise ExtractionError("Probe lease ended", "probe_interrupted")
                        job.results, job.updated_at = list(results), time.time()
                    if fatal:
                        raise ExtractionError("Probe stopped; inspect per-request result", result["code"])
                for probe in manifest.get("browser_probes", []):
                    if probe["id"] in done:
                        continue
                    from .browser_dependency_probe import probe_product
                    stage = "browser_probe"
                    async def retain_baseline(page, headers, capture, loaded_contract):
                        return await self.domain_sessions.retain_probe_context(
                            state["run_id"], probe["product_url"], page, headers, capture, loaded_contract)
                    report = await probe_product(pool=self.pool, transport=transport,
                        budget=budget, product_url=probe["product_url"],
                        headless=os.getenv("ATLAS_BROWSER_HEADLESS", "true").lower() != "false",
                        phases=tuple(probe["phases"]), initialization=probe.get("initialization"),
                        canary_selections=probe.get("canary_selections"),
                        **({"on_baseline_ready": retain_baseline} if probe.get("retain_domain_session") else {}))
                    results.append({"id": probe["id"], "status": "observed", "kind": "browser_dependency_probe", "report": report})
                    with self.store.session(immediate=True) as s:
                        job = s.get(ProbeJob, job_id)
                        if job.owner != owner:
                            raise ExtractionError("Probe lease ended", "probe_interrupted")
                        job.results, job.updated_at = list(results), time.time()
                await page.unroute("**/*", route)
                for task in list(pending):
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            failure = "probe_interrupted"
        except ExtractionError as exc:
            failure = exc.code
        except Exception as exc:
            diagnostic = probe_failure_diagnostic(exc, stage, bootstrap_request_failure)
            failure = "probe_internal_error:" + diagnostic["exception_type"] + ":" + diagnostic["stage"] + ":" + (diagnostic["network_error"] or diagnostic["category"])
            await asyncio.to_thread(self.store.event, state["run_id"], "probe_failure_diagnostic",
                                    {"probe_id": job_id, **diagnostic})
        finally:
            if listening_context is not None:
                listening_context.remove_listener("request", listener)
            for task in list(pending):
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            auth.clear()
            if client is not None:
                await client.aclose()
            with self.store.session(immediate=True) as s:
                job = s.get(ProbeJob, job_id)
                if job and job.owner == owner:
                    latest = {r["id"]: r for r in results}
                    unresolved = any(r["status"] != "observed" or r.get("report", {}).get("status", "finished") != "finished" for r in latest.values())
                    job.status = "needs_attention" if failure else ("completed_with_errors" if unresolved else "completed")
                    job.error, job.updated_at = failure, time.time()
            self.tasks.pop(job_id, None)
            if self.on_stage_finished is not None:
                await asyncio.to_thread(self.on_stage_finished, state['run_id'])
