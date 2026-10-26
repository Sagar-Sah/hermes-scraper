"""Bounded, resumable 4over child table reads in the retained browser session.

Tables are scoped historical supplier evidence, never checkout quotes or full
catalog certification. Raw response bytes are hashed; only public child IDs,
SKUs and parsed prices are persisted. Account envelopes/headers are not saved.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .adapters.browser import browser_slot
from .fourover_bulk import ChildConfigurationError, inspect_child_configurations
from .fourover_options import inspect_option_contract
from .pacing import retry_after_seconds, source_pacing
from .transport import ExtractionError, RateLimiter, validate_url

_SCOPE = {"sets": 1, "job_sample": False, "digital_proof": False}
_SCHEMA = "hermes-fourover-child-price-collection/v1"
_SUFFIX = "fourover-child-price.json"
_LIMIT = 2 * 1024 * 1024

_FETCH_JS = r"""async ({url,limit}) => {
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
 try {
   const response=await fetch(url,{method:'GET',credentials:'same-origin',redirect:'error',
     signal:controller.signal,headers:{'x-requested-with':'XMLHttpRequest','accept':'application/json','content-type':'application/json'}});
   const retry_after=response.headers.get('retry-after');
   if(response.status!==200){await response.body?.cancel();return {status:response.status,retry_after,body_b64:''};}
   if(Number(response.headers.get('content-length')||0)>limit){await response.body?.cancel();return {status:response.status,retry_after,too_large:true};}
   const reader=response.body?.getReader(),chunks=[];let length=0;
   if(!reader)return {status:response.status,retry_after,body_b64:''};
   try {
     while(true){const next=await reader.read();if(next.done)break;length+=next.value.length;
       if(length>limit){await reader.cancel();return {status:response.status,retry_after,too_large:true};}
       chunks.push(next.value);
     }
   } finally {reader.releaseLock();}
   const bytes=new Uint8Array(length);let offset=0;
   for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
   let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
   return {status:response.status,retry_after,body_b64:btoa(binary)};
 } finally {clearTimeout(timer)}
}"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _url(value):
    if not isinstance(value, str) or value != value.strip() or len(value) > 4096:
        raise ValueError("Exact public 4over product URL required")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {"4over.com", "www.4over.com"}
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or "\\" in value
            or not re.fullmatch(r"/[a-zA-Z0-9][a-zA-Z0-9_-]*/?", parsed.path)
            or parsed.path.strip("/").lower() in {"customer", "account", "cart", "checkout", "admin", "rest", "login", "logout"}):
        raise ValueError("Exact public 4over product URL required")
    return parsed


def _scope(value):
    if (not isinstance(value, dict) or set(value) != set(_SCOPE)
            or type(value["sets"]) is not int or value["sets"] != 1
            or value["job_sample"] is not False or value["digital_proof"] is not False):
        raise ValueError("Explicit one-set, sample-off, proof-off scope required")
    return dict(_SCOPE)


def parse_positive_prices(raw):
    """Keep exact decimal source numbers; never expand/interpolate tier domains."""
    def invalid_constant(_):
        raise ValueError("Nonfinite source price")
    try:
        value = json.loads(raw, parse_float=Decimal, parse_constant=invalid_constant)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise ValueError()
        result = {}
        for source_key, target_key in (("packagePrices", "package_prices"), ("tierPrices", "tier_prices")):
            rows = value[0].get(source_key, [])
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ValueError()
            parsed, seen = [], set()
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError()
                quantity, price = row.get("qty"), row.get("price")
                if type(quantity) not in (int, Decimal) or type(price) not in (int, Decimal):
                    raise ValueError()
                quantity, price = Decimal(quantity), Decimal(price)
                if (not quantity.is_finite() or quantity < 1 or quantity != quantity.to_integral_value()
                        or not price.is_finite() or price <= 0 or int(quantity) in seen):
                    raise ValueError()
                seen.add(int(quantity))
                parsed.append({"quantity": int(quantity), "price": format(price, "f")})
            result[target_key] = parsed
        if not any(result.values()):
            raise ValueError()
        return result
    except (ValueError, TypeError, UnicodeError, ArithmeticError):
        raise ExtractionError("Source child prices are missing, zero or malformed", "source_price_unavailable") from None


class FourOverChildPriceCollector:
    def __init__(self, store, browser_pool):
        self.store, self.pool = store, browser_pool
        self.root = Path(store.root) / "fourover-child-prices"
        self.limiter = RateLimiter(store)

    def _path(self, snapshot_id, source_url):
        _url(source_url)
        if not isinstance(snapshot_id, str) or not re.fullmatch(r"[a-f0-9]{32}", snapshot_id):
            raise ValueError("Bounded snapshot identifier required")
        return self.root / snapshot_id / (hashlib.sha256(source_url.encode()).hexdigest() + ".json")

    def _save(self, path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.with_suffix("." + os.urandom(12).hex() + ".tmp")
        try:
            with temp.open("xb") as stream:
                os.chmod(temp, 0o600)
                stream.write(_encoded(value)); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    @asynccontextmanager
    async def _locked(self, path, check):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            if os.name == "nt" and os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            while not locked:
                await check()
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, 0); msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    await asyncio.sleep(0.1)
            yield
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, 0); msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self, path, snapshot_id, source_url, scope):
        if not path.exists():
            return {"schema": _SCHEMA, "snapshot_id": snapshot_id, "source_url": source_url,
                    "scope": scope, "declared_children": [], "observations": {},
                    "requests_attempted": 0, "status": "not_started", "created_at": _now(), "updated_at": _now()}
        if path.is_symlink():
            raise ValueError("Invalid child price checkpoint")
        value = json.loads(path.read_text())
        if (value.get("schema") != _SCHEMA or value.get("snapshot_id") != snapshot_id
                or value.get("source_url") != source_url or value.get("scope") != scope):
            raise ValueError("Child price checkpoint context mismatch")
        for child, record in value.get("observations", {}).items():
            if record.get("status") == "collected":
                self._evidence(value, child, record)
        return value

    def _evidence(self, job, child, record):
        sha, relative = record.get("sha256"), record.get("object_path", "")
        if (not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)
                or relative != f"objects/{sha[:2]}/{sha}.{_SUFFIX}"):
            raise ValueError("Invalid child price evidence")
        raw = self.store.get_object(relative)
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError("Child price evidence changed")
        value = json.loads(raw)
        if (value.get("child_id") != child or value.get("source_url") != job["source_url"]
                or value.get("scope") != job["scope"]):
            raise ValueError("Child price evidence context mismatch")
        return value

    def get(self, snapshot_id, source_url):
        job = self._load(self._path(snapshot_id, source_url), snapshot_id, source_url, dict(_SCOPE))
        return self._result(job, list(job["observations"]), 0, 0)

    def _result(self, job, requested, sent, hits):
        records = []
        for child in requested:
            record = job["observations"].get(child)
            if record and record.get("status") == "collected":
                records.append({**record, **self._evidence(job, child, record)})
        result = {**job, "observations": records, "requested_child_ids": requested,
                "requests_this_call": sent, "cache_hits_this_call": hits,
                "scope_semantics": "Source child tables only; requested one-set services-off scope is not a verified payable quote",
                "supplier_total_verified": False, "services_verified": False,
                "catalog_complete": False, "final_eligible": False,
                "use": "historical_source_evidence_only_require_fresh_snapshot_for_refresh"}
        if (requested and len(records) == len(requested)) or (not requested and job["declared_children"]):
            result["status"] = "completed_requested_subset" if requested else "discovered_source_children"
            result.pop("error", None)
        return result

    async def collect(self, snapshot_id, source_url, child_ids, *, request_budget,
                      scope, check=None):
        """Read <=10 declared children; [] discovers children without price reads.

        The per-call budget includes the one product document GET.

        Successful child responses checkpoint before another await and are never
        re-requested within this snapshot. A fresh snapshot ID requests refresh.
        Request failures are not automatically retried. All-cache-hit calls make
        no browser/DNS/network calls. ``check`` may pause/cancel queued work.
        """
        async def noop():
            pass
        check = check or noop
        parsed, scope = _url(source_url), _scope(scope)
        if (not isinstance(child_ids, list) or not 0 <= len(child_ids) <= 10
                or any(not isinstance(child, str) or not re.fullmatch(r"[1-9][0-9]{0,12}", child) for child in child_ids)
                or len(set(child_ids)) != len(child_ids)):
            raise ValueError("Choose zero to ten unique source-declared child IDs")
        if type(request_budget) is not int or not 1 <= request_budget <= 11:
            raise ValueError("Request budget must be from one to eleven, including the document")
        path = self._path(snapshot_id, source_url)
        async with self._locked(path, check):
            job = self._load(path, snapshot_id, source_url, scope)
            hits = sum(job["observations"].get(c, {}).get("status") == "collected" for c in child_ids)
            missing = [c for c in child_ids if job["observations"].get(c, {}).get("status") != "collected"]
            if not missing and (child_ids or job["declared_children"]):
                return self._result(job, child_ids, 0, hits)
            await check()
            await validate_url(source_url)
            profile = next((p for p in self.store.profiles("source")
                            if str(p.get("host", p.get("domain", ""))).removeprefix("www.") == "4over.com"), {})
            rate, concurrency = source_pacing(profile, self.store.config)
            sent, allowed_fetch = 0, None
            navigation_sent = False

            async def read(kind, action, consume, child_id=None):
                nonlocal sent
                await check()
                if sent >= request_budget:
                    raise ExtractionError("Child collection request budget exhausted", "request_limit")
                token = await self.limiter.acquire("https://4over.com", rate, concurrency, check)
                started, status, backoff = time.monotonic(), 0, 0
                try:
                    await check()
                    sent += 1; job["requests_attempted"] += 1
                    job["last_attempt"] = {"kind": kind, "child_id": child_id, "status": "reserved", "at": _now()}
                    self._save(path, job)
                    status, raw, retry = await action()
                    backoff = retry_after_seconds(retry, default=30)
                    if status in {401, 403}:
                        raise ExtractionError("4over pricing session needs sign-in", "access_required")
                    if status != 200:
                        raise ExtractionError("4over source request failed", "source_http_error")
                    if len(raw) > min(_LIMIT, self.store.config.max_response_bytes):
                        raise ExtractionError("4over source response too large", "response_too_large")
                    consume(raw)  # Durable per-child success before any other await.
                    job["last_attempt"]["status"] = "captured"
                    job["updated_at"] = _now(); self._save(path, job)
                finally:
                    await asyncio.to_thread(self.limiter.release, "https://4over.com", token,
                                            time.monotonic()-started, status, backoff, rate)

            try:
                async with (
                    browser_slot(check, source_origin="https://4over.com", per_source_limit=concurrency),
                    self.pool.page("https://4over.com", headless=False) as page,
                ):
                    async def route(route):
                        nonlocal navigation_sent, allowed_fetch
                        request = route.request
                        document = (request.resource_type == "document" and request.frame == page.main_frame
                                    and request.url == source_url and not navigation_sent)
                        price = (request.resource_type == "fetch" and request.frame == page.main_frame
                                 and request.url == allowed_fetch)
                        if request.method == "GET" and (document or price):
                            if document:
                                navigation_sent = True
                            else:
                                allowed_fetch = None
                            await route.continue_()
                        else:
                            await route.abort()
                    await page.route("**/*", route)
                    cdp = await page.context.new_cdp_session(page)
                    try:
                        await cdp.send("Emulation.setScriptExecutionDisabled", {"value": True})
                        async def document():
                            response = await page.goto(source_url, wait_until="domcontentloaded", timeout=30000)
                            if response is None or response.url != source_url:
                                raise ExtractionError("4over product redirected", "access_required")
                            length = response.headers.get("content-length", "")
                            if length.isdigit() and int(length) > min(_LIMIT, self.store.config.max_response_bytes):
                                raise ExtractionError("4over source response too large", "response_too_large")
                            raw = await asyncio.wait_for(response.body(), timeout=20)
                            return response.status, raw, response.headers.get("retry-after")
                        def declaration(raw):
                            # Retain only hash/length before parsing. Failed HTML
                            # may contain account content and is never archived.
                            job["document_body_sha256"] = hashlib.sha256(raw).hexdigest()
                            job["document_body_bytes"] = len(raw)
                            self._save(path, job)
                            try:
                                children, diagnostic = inspect_child_configurations(
                                    raw.decode("utf-8"), source_url=source_url)
                            except ChildConfigurationError as exc:
                                job["document_declaration"] = exc.diagnostic
                                self._save(path, job)
                                raise
                            job["document_declaration"] = diagnostic
                            if job["declared_children"] and children != job["declared_children"]:
                                raise ExtractionError("Source child declaration changed; use a fresh snapshot", "source_declaration_changed")
                            job["option_contract"] = inspect_option_contract(
                                raw.decode("utf-8"), children, source_url=source_url)
                            job["option_contract_document_sha256"] = job["document_body_sha256"]
                            job["declared_children"] = children
                        await read("product_document", document, declaration)
                        declared = {child["child_id"] for child in job["declared_children"]}
                        if not set(child_ids) <= declared:
                            raise ExtractionError("Requested child not declared by this product", "undeclared_child")
                        await cdp.send("Emulation.setScriptExecutionDisabled", {"value": False})
                        for child in missing:
                            url = f"{parsed.scheme}://{parsed.netloc}/rest/default/V1/product/prices/{child}?_={os.urandom(12).hex()}"
                            allowed_fetch = url
                            async def price(url=url):
                                reply = await page.evaluate(_FETCH_JS, {"url": url, "limit": min(_LIMIT, self.store.config.max_response_bytes)})
                                if reply.get("too_large"):
                                    raise ExtractionError("4over source response too large", "response_too_large")
                                try:
                                    raw = base64.b64decode(reply.get("body_b64", ""), validate=True)
                                except (ValueError, TypeError):
                                    raise ExtractionError("4over source response unreadable", "source_price_unavailable") from None
                                return reply["status"], raw, reply.get("retry_after")
                            def save_price(raw, child=child):
                                projection = {"source_url": source_url, "child_id": child, "scope": scope,
                                              "captured_at": _now(), "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                                              **parse_positive_prices(raw)}
                                encoded = _encoded(projection)
                                relative = self.store.put_object(encoded, _SUFFIX)
                                job["observations"][child] = {"status": "collected", "child_id": child,
                                    "object_path": relative, "sha256": hashlib.sha256(encoded).hexdigest()}
                                self._save(path, job)
                            await read("child_price", price, save_price, child)
                    finally:
                        await cdp.detach()
                job["status"] = "completed_requested_subset" if child_ids else "discovered_source_children"
                job.pop("error", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = exc.code if isinstance(exc, (ExtractionError, ChildConfigurationError)) else "source_capture_failed"
                job["status"] = "access_required" if code == "access_required" else "partial"
                error_type = type(exc).__name__
                if error_type not in {"ExtractionError", "ChildConfigurationError", "ValueError", "TypeError", "AttributeError", "UnicodeDecodeError", "TimeoutError", "Error", "OSError"}:
                    error_type = "Exception"
                job["error"] = {"code": code, "exception_type": error_type,
                                "child_id": job.get("last_attempt", {}).get("child_id"), "at": _now()}
            job["updated_at"] = _now(); self._save(path, job)
            return self._result(job, child_ids, sent, hits)
