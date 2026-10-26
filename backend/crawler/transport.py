import asyncio
import ipaddress
import json as jsonlib
import socket
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urljoin

import httpx
from .tls import tls_context
from .pacing import source_pacing, retry_after_seconds
from sqlalchemy import select, update, delete, func
from sqlalchemy.exc import IntegrityError

from .store import (
    Origin,
    Permit,
    Product,
    Response,
    RequestLease,
    TrafficWindow,
    canonical,
    digest,
    uid,
    safe_public_context,
)


class ExtractionError(Exception):
    def __init__(self, message, code="source_error"):
        super().__init__(message)
        self.code = code


class RunInterrupted(ExtractionError):
    pass


@dataclass
class FetchResult:
    data: object
    response_id: str
    cached: bool
    status: int


_CAPTURE_IDENTITY_FIELDS = frozenset({
    "navigation_generation", "navigation", "selection_epoch", "generation",
    "probe_job", "event_id", "request_id",
})


def observation_signature(url, context=None, **request_fields):
    """Group telemetry by request meaning, without changing cache/evidence keys.

    Only known top-level capture bookkeeping is omitted. Nested or unknown
    dimensions may be supplier/account state, so they remain significant.
    """
    public_context = safe_public_context(context or {})
    if isinstance(public_context, dict):
        public_context = {
            key: value for key, value in public_context.items()
            if key not in _CAPTURE_IDENTITY_FIELDS
        }
    return digest({"url": url, "context": public_context, **request_fields})


async def validate_url(url: str):
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ExtractionError(
            "Use an HTTP(S) URL without embedded credentials", "invalid_url"
        )
    if parsed.port not in (None, 80, 443):
        raise ExtractionError(
            "Custom network ports are disabled for external sources", "invalid_url"
        )
    try:
        ips = await asyncio.get_running_loop().run_in_executor(
            None,
            socket.getaddrinfo,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except socket.gaierror as e:
        raise ExtractionError(
            "Source hostname could not be resolved", "network_error"
        ) from e
    for ip in {item[4][0] for item in ips}:
        if not ipaddress.ip_address(ip).is_global:
            raise ExtractionError(
                "Private, local, and metadata network addresses are not allowed",
                "invalid_url",
            )
    return parsed


class RateLimiter:
    PERMIT_SECONDS = 45

    def __init__(self, store):
        self.store = store

    def _grant(self, origin, rps, concurrency):
        try:
            with self.store.session(immediate=True) as s:
                now = self.store.now(s)
                row = s.get(Origin, origin, with_for_update=not self.store.sqlite)
                if not row:
                    row = Origin(
                        id=origin,
                        rps=rps,
                        next_at=0,
                        backoff_until=0,
                        latency=0,
                        healthy=0,
                    )
                    s.add(row)
                    s.flush()
                s.execute(delete(Permit).where(Permit.expires < now))
                count = s.scalar(
                    select(func.count())
                    .select_from(Permit)
                    .where(Permit.origin == origin)
                )
                wait = max(row.next_at - now, row.backoff_until - now, 0)
                if count >= concurrency:
                    return None, max(wait, 0.1)
                if wait > 0:
                    return None, min(wait, 2)
                token = uid()
                s.add(Permit(id=token, origin=origin, expires=now + self.PERMIT_SECONDS))
                row.next_at = now + 1 / max(0.1, min(row.rps, rps))
                return token, 0
        except IntegrityError:
            return None, 0.05

    async def acquire(self, origin, rps, concurrency, check):
        while True:
            await check()
            token, wait = await asyncio.to_thread(self._grant, origin, rps, concurrency)
            if token:
                return token
            await asyncio.sleep(wait)

    def release(self, origin, token, elapsed, status, retry_after, cap):
        with self.store.session(immediate=True) as s:
            permit = s.get(Permit, token)
            s.execute(delete(Permit).where(Permit.id == token, Permit.origin == origin))
            row = s.get(Origin, origin, with_for_update=not self.store.sqlite)
            if not row:
                return
            # Callers may start timing before acquire. Supplier latency cannot
            # exceed the duration of a verified permit, and a no-request release
            # must not change adaptive latency or health.
            held = (self.store.now(s) - (permit.expires - self.PERMIT_SECONDS)
                    if permit is not None and permit.origin == origin else 0)
            elapsed = min(elapsed, held) if elapsed > 0 and held > 0 else 0
            if status in {429, 503}:
                row.rps = max(0.1, row.rps / 2)
                row.backoff_until = max(
                    row.backoff_until, self.store.now(s) + retry_after
                )
                row.healthy = 0
            elif elapsed > max(2, row.latency * 2) and row.latency:
                row.rps = max(0.1, row.rps * 0.8)
                row.healthy = 0
            elif elapsed and 200 <= status < 400:
                row.healthy += 1
                if row.healthy >= 30:
                    row.rps = min(cap, row.rps + 0.1)
                    row.healthy = 0
            if elapsed:
                row.latency = (
                    elapsed if not row.latency else 0.9 * row.latency + 0.1 * elapsed
                )


class Transport:
    def __init__(
        self, store, run_id, product_id, client=None, refresh=False, max_requests=None
    ):
        self.store, self.run_id, self.product_id = store, run_id, product_id
        self.client = client or httpx.AsyncClient(
            verify=tls_context(),
            timeout=httpx.Timeout(30),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        )
        self.owns_client = client is None
        self.limiter = RateLimiter(store)
        self.refresh, self.max_requests, self.calls = refresh, max_requests, 0
        self.epoch = None
        self.created_at = time.time()

    def _request_lease(self, key, owner, renew=False):
        try:
            with self.store.session(immediate=True) as s:
                now = self.store.now(s)
                lease = s.get(RequestLease, key, with_for_update=not self.store.sqlite)
                if lease is None:
                    if renew:
                        return False
                    s.add(RequestLease(id=key, owner=owner, expires=now + 60))
                elif lease.owner == owner or (not renew and lease.expires < now):
                    lease.owner, lease.expires = owner, now + 60
                else:
                    return False
                return True
        except IntegrityError:
            return False

    @asynccontextmanager
    async def request_guard(self, key):
        owner = uid()
        while not await asyncio.to_thread(self._request_lease, key, owner):
            await self.check()
            await asyncio.sleep(0.1)

        async def renew():
            while True:
                await asyncio.sleep(15)
                if not await asyncio.to_thread(self._request_lease, key, owner, True):
                    return

        task = asyncio.create_task(renew())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with self.store.session() as s:
                s.execute(
                    delete(RequestLease).where(
                        RequestLease.id == key, RequestLease.owner == owner
                    )
                )

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def record_observation(
        self, url, status, headers, body, context=None, count_request=True
    ):
        """Store a response produced by normal browser operation, without auth headers."""
        if isinstance(body, str):
            body = body.encode()
        if len(body) > self.store.config.max_response_bytes:
            raise ExtractionError(
                "Observed response exceeds size limit", "response_too_large"
            )
        content_type = headers.get("content-type", "application/json")
        key = digest({"browser_url": url, "context": context, "run_id": self.run_id})
        object_path = await asyncio.to_thread(self.store.put_object, body, "response")
        try:
            data = jsonlib.loads(body)
        except ValueError:
            data = body.decode(errors="replace")
        record = Response(
            id=uid(),
            request_key=key,
            status=status,
            url=url,
            object_path=object_path,
            raw_hash=digest(body.hex()),
            semantic_hash=digest(data),
            etag=headers.get("etag"),
            modified=headers.get("last-modified"),
            content_type=content_type,
        )
        with self.store.session() as s:
            s.add(record)
        await asyncio.to_thread(self.store.link_response, self.run_id, self.product_id,
            record.id, "source" if (context or {}).get("lane") in {"browser_source", "http_source"} else "dom",
            observation_signature(url, context), context)
        if count_request:
            await asyncio.to_thread(self._count, False)
        return FetchResult(data, record.id, False, status)

    async def check(self):
        if self.epoch is not None:
            with self.store.session() as s:
                p = s.get(Product, self.product_id)
                if not p or p.epoch != self.epoch or p.status != "running":
                    raise RunInterrupted("Worker lease superseded", "interrupted")
        if await asyncio.to_thread(self.store.status, self.run_id) not in {
            "running",
            "queued",
            "exporting",
        }:
            raise RunInterrupted("Run paused or cancelled", "interrupted")

    def _latest(self, key):
        with self.store.session() as s:
            return s.scalar(
                select(Response)
                .where(Response.request_key == key, Response.status.between(200, 299))
                .order_by(Response.created_at.desc())
                .limit(1)
            )

    def _count(self, cached):
        with self.store.session(immediate=True) as s:
            p = s.get(Product, self.product_id)
            if p is not None and s.get(TrafficWindow, p.id) is None:
                s.add(TrafficWindow(product_id=p.id, run_id=self.run_id,
                    requests_start=p.request_count, cache_hits_start=p.cache_hits,
                    rows_start=p.row_count))
            field = Product.cache_hits if cached else Product.request_count
            s.execute(
                update(Product)
                .where(Product.id == self.product_id)
                .values({field.key: field + 1})
            )

    def _decode(self, response):
        content = self.store.get_object(response.object_path)
        if "json" in response.content_type:
            try:
                return jsonlib.loads(content)
            except ValueError:
                raise ExtractionError("Invalid source JSON", "schema_changed")
        if response.content_type.startswith("image/"):
            return content
        value = content.decode("utf-8", errors="replace")
        if "text/html" in response.content_type and any(
            x in value[:3000].lower()
            for x in [
                "<title>authenticating",
                "verify you are human",
                "just a moment",
                "captcha",
            ]
        ):
            raise ExtractionError(
                "Source requires authorized browser access", "access_required"
            )
        return value

    async def fetch(
        self, url, method="GET", params=None, json=None, context=None, data=None
    ):
        parsed = await validate_url(url)
        profile = next(
            (
                p
                for p in self.store.profiles("source", secrets=True)
                if p.get("host", "").lower() == parsed.hostname.lower()
            ),
            {},
        )
        credentials = profile.get("credentials", {})
        headers = {
            "User-Agent": "HermesProductCrawler/0.1",
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        }
        forbidden = {"host", "connection", "content-length", "transfer-encoding"}
        headers.update(
            {
                k: str(v)
                for k, v in credentials.get("headers", {}).items()
                if k.lower() not in forbidden
            }
        )
        key = digest(
            {
                "method": method.upper(),
                "url": url,
                "params": params,
                "json": json,
                "data": data,
                "context": context,
                "profile": profile.get("id", "public"),
                "credential_generation": digest(credentials),
                "adapter_version": "1",
            }
        )
        signature = observation_signature(
            url, context, method=method.upper(), params=params, json=json, data=data,
            profile=profile.get("id", "public"),
            credential_generation=digest(credentials), adapter_version="1",
        )
        async with self.request_guard(key):
            await self.check()
            previous = await asyncio.to_thread(self._latest, key)
            if (
                previous
                and (not self.refresh or previous.created_at >= self.created_at)
                and time.time() - previous.created_at < self.store.config.cache_seconds
            ):
                await asyncio.to_thread(self._count, True)
                await asyncio.to_thread(self.store.link_response, self.run_id, self.product_id,
                    previous.id, "cache", signature, context)
                return FetchResult(
                    await asyncio.to_thread(self._decode, previous),
                    previous.id,
                    True,
                    previous.status,
                )
            if previous:
                if previous.etag:
                    headers["If-None-Match"] = previous.etag
                if previous.modified:
                    headers["If-Modified-Since"] = previous.modified
            try:
                rps, concurrency = source_pacing(profile, self.store.config)
            except ValueError as exc:
                raise ExtractionError(str(exc), "invalid_source_pacing") from exc
            origin = f"{parsed.scheme}://{parsed.netloc}"
            current_url = url
            for attempt in range(4):
                await self.check()
                if self.max_requests and self.calls >= self.max_requests:
                    raise ExtractionError(
                        "Configured request budget reached; resume with a larger budget",
                        "request_limit",
                    )
                token = await self.limiter.acquire(origin, rps, concurrency, self.check)
                started, status, backoff = time.monotonic(), 0, min(2**attempt, 30)
                try:
                    self.calls += 1
                    await asyncio.to_thread(self._count, False)
                    async with asyncio.timeout(35), self.client.stream(
                        method,
                        current_url,
                        params=params,
                        json=json,
                        data=data,
                        headers=headers,
                    ) as response:
                        status = response.status_code
                        observed_url = str(response.url)
                        retry = response.headers.get("retry-after")
                        if retry:
                            backoff = retry_after_seconds(retry, default=backoff)
                        buffer = bytearray()
                        async for chunk in response.aiter_bytes():
                            buffer.extend(chunk)
                            if len(buffer) > self.store.config.max_response_bytes:
                                raise ExtractionError(
                                    "Source response exceeds configured size limit",
                                    "response_too_large",
                                )
                        response_headers = response.headers
                    if status in {301, 302, 303, 307, 308}:
                        new_url = urljoin(
                            current_url, response_headers.get("location", "")
                        )
                        target = await validate_url(new_url)
                        if target.netloc != parsed.netloc:
                            raise ExtractionError(
                                "Cross-origin redirect requires its own source profile",
                                "redirect_required",
                            )
                        current_url, params = new_url, None
                        continue
                    if status == 304 and previous:
                        with self.store.session() as s:
                            copy = Response(
                                id=uid(),
                                request_key=key,
                                status=200,
                                url=observed_url,
                                object_path=previous.object_path,
                                raw_hash=previous.raw_hash,
                                semantic_hash=previous.semantic_hash,
                                etag=previous.etag,
                                modified=previous.modified,
                                content_type=previous.content_type,
                            )
                            s.add(copy)
                        await asyncio.to_thread(self.store.link_response, self.run_id, self.product_id,
                            copy.id, "source", signature, context)
                        return FetchResult(
                            await asyncio.to_thread(self._decode, copy),
                            copy.id,
                            False,
                            304,
                        )
                    content_type = response_headers.get(
                        "content-type", "application/octet-stream"
                    )
                    object_path = await asyncio.to_thread(
                        self.store.put_object, bytes(buffer), "response"
                    )
                    try:
                        parsed_data = (
                            jsonlib.loads(buffer) if "json" in content_type else None
                        )
                    except ValueError:
                        parsed_data = None
                    record = Response(
                        id=uid(),
                        request_key=key,
                        status=status,
                        url=observed_url,
                        object_path=object_path,
                        raw_hash=digest(bytes(buffer).hex()),
                        semantic_hash=(
                            digest(parsed_data)
                            if parsed_data is not None
                            else digest(bytes(buffer).hex())
                        ),
                        etag=response_headers.get("etag"),
                        modified=response_headers.get("last-modified"),
                        content_type=content_type,
                    )
                    with self.store.session() as s:
                        s.add(record)
                    await asyncio.to_thread(self.store.link_response, self.run_id, self.product_id,
                        record.id, "source", signature, context)
                    text_head = bytes(buffer[:3000]).decode(errors="ignore").lower()
                    if status in {401, 403} or (
                        "text/html" in content_type
                        and any(
                            x in text_head
                            for x in [
                                "<title>authenticating",
                                "verify you are human",
                                "just a moment",
                                "captcha",
                            ]
                        )
                    ):
                        raise ExtractionError(
                            "Source requires authorized access. Configure its API headers or use an authorized browser session.",
                            "access_required",
                        )
                    if status in {429, 500, 502, 503, 504}:
                        if attempt < 3:
                            raise httpx.HTTPError(f"Retryable HTTP {status}")
                        raise ExtractionError(
                            f"Supplier returned HTTP {status}; request remains recoverable",
                            "retry_exhausted",
                        )
                    if status >= 400:
                        raise ExtractionError(
                            f"Supplier returned HTTP {status}", "http_error"
                        )
                    return FetchResult(
                        (
                            parsed_data
                            if parsed_data is not None
                            else (
                                bytes(buffer)
                                if content_type.startswith("image/")
                                else bytes(buffer).decode(errors="replace")
                            )
                        ),
                        record.id,
                        False,
                        status,
                    )
                except (httpx.HTTPError, TimeoutError) as exc:
                    if attempt == 3:
                        raise ExtractionError(
                            "Source network request failed after retries",
                            "network_error",
                        ) from exc
                finally:
                    await asyncio.to_thread(
                        self.limiter.release,
                        origin,
                        token,
                        time.monotonic() - started,
                        status,
                        backoff,
                        rps,
                    )
                await asyncio.sleep(min(backoff, 30))
            raise ExtractionError("Redirect or retry limit reached", "retry_exhausted")
