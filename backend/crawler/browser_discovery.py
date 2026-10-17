"""Bounded title lookup through an ordinary browser's public catalog links."""

import asyncio
import os
import re
import time
from types import SimpleNamespace
from urllib.parse import urldefrag, urljoin, urlparse

from .adapters.browser import browser_slot
from .adapters.printograph import AdapterError
from .browser_session import browser_page
from .transport import validate_url


def match_titles(candidates, titles):
    normalized = lambda value: re.sub(r"\s+", " ", value).strip().casefold()
    matches, unresolved = [], []
    for title in titles:
        exact = [c for c in candidates if normalized(c["title"]) == normalized(title)]
        order = [
            c for c in exact if urlparse(c["url"]).path.rstrip("/").endswith("/order")
        ]
        if order:
            exact = order
        unique = {c["url"]: c for c in exact}
        if len(unique) == 1:
            matches.append(next(iter(unique.values())))
        else:
            choices = (
                list(unique.values())
                or [
                    c for c in candidates if normalized(title) in normalized(c["title"])
                ][:10]
            )
            unresolved.append({"title": title, "candidates": choices})
    return {"matches": matches, "unresolved": unresolved}


async def resolve_titles(
    source_url, titles, *, limiter=None, rps=1.0, concurrency=1, browser_pool=None
):
    """Two pages maximum; exact matches only. Optional shared database limiter.

    The app can pass RateLimiter(store) and its engine browser pool before a run exists. No source cookies,
    headers, authorization tokens, or private account data are read or exported.
    """
    parsed = await validate_url(source_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    urls = (
        [urljoin(base, "/products/info.html"), source_url]
        if parsed.hostname in {"www.printograph.com", "printograph.com"}
        else [source_url]
    )
    urls = list(dict.fromkeys(urls))[:2]
    if not titles or len(titles) > 100:
        raise AdapterError(
            "invalid_selection", "Resolve between one and 100 product titles at a time"
        )
    headless = os.getenv("ATLAS_BROWSER_HEADLESS", "true").lower() not in {
        "false",
        "0",
        "no",
    }
    candidates = {}
    errors = []
    source_requests = 0
    last_start = 0.0
    origin_locks = {}

    async def check():
        return None

    async with browser_slot(check, source_origin=base, per_source_limit=min(2, concurrency)):
        async with browser_page(
            SimpleNamespace(browser_pool=browser_pool), base, headless=headless
        ) as page:
            verified = {base}
            completions = {}
            route_tasks = set()
            closing = False

            def request_done(request):
                event = completions.get(request)
                if event:
                    event.set()

            page.on("requestfinished", request_done)
            page.on("requestfailed", request_done)

            async def route_request(route):
                nonlocal source_requests, last_start
                task = asyncio.current_task()
                route_tasks.add(task)
                request = route.request
                origin = (
                    f"{urlparse(request.url).scheme}://{urlparse(request.url).netloc}"
                )
                token, response, status = None, None, 0
                started = time.monotonic()
                try:
                    if closing:
                        await route.abort()
                        return
                    if request.resource_type in {"image", "media", "font"}:
                        await route.abort()
                        return
                    if origin not in verified:
                        await validate_url(request.url)
                        verified.add(origin)
                    if request.resource_type not in {"document", "xhr", "fetch"}:
                        await route.continue_()
                        return
                    source_requests += 1
                    if source_requests > 40:
                        raise AdapterError(
                            "request_limit",
                            "Browser title discovery reached its 40-request ceiling",
                        )
                    if request.method not in {"GET", "HEAD"} and re.search(
                        r"/(cart|orders|checkout)(/|$)", urlparse(request.url).path
                    ):
                        await route.abort()
                        return
                    if limiter:
                        token = await limiter.acquire(
                            origin, min(2.0, rps), min(2, concurrency), check
                        )
                    else:
                        lock = origin_locks.setdefault(origin, asyncio.Lock())
                        async with lock:
                            await asyncio.sleep(
                                max(
                                    0,
                                    1 / max(0.1, min(2.0, rps))
                                    - (time.monotonic() - last_start),
                                )
                            )
                            last_start = time.monotonic()
                    started = time.monotonic()
                    complete = completions[request] = asyncio.Event()
                    await route.continue_()
                    async with asyncio.timeout(30):
                        response = await request.response()
                        if response is None:
                            raise RuntimeError(
                                request.failure
                                or "Chromium request ended without a response"
                            )
                        status = response.status
                        await complete.wait()
                except Exception as exc:
                    reason = (
                        str(exc).split("\nCall log:", 1)[0].splitlines()
                        or [type(exc).__name__]
                    )[0]
                    code = getattr(exc, "code", "browser_discovery_failed")
                    if re.search(
                        r"certificate|cert_authority|unable to verify|self.signed",
                        reason,
                        re.I,
                    ):
                        code, reason = (
                            "tls_trust_error",
                            "Configure Chromium's trusted CA chain; TLS verification remains enabled",
                        )
                    errors.append({"code": code, "message": reason[:300]})
                    try:
                        await route.abort()
                    except Exception:
                        pass
                finally:
                    completions.pop(request, None)
                    try:
                        if token:
                            await asyncio.to_thread(
                                limiter.release,
                                origin,
                                token,
                                time.monotonic() - started,
                                status,
                                30 if status in {429, 503} else 0,
                                min(2.0, rps),
                            )
                    finally:
                        route_tasks.discard(task)

            await page.route("**/*", route_request)
            try:
                for url in urls:
                    try:
                        await page.goto(
                            url, wait_until="domcontentloaded", timeout=30_000
                        )
                    except Exception as exc:
                        if not errors:
                            errors.append(
                                {
                                    "code": "browser_discovery_failed",
                                    "message": str(exc).split("\nCall log:", 1)[0][
                                        :300
                                    ],
                                }
                            )
                        break
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            state = await page.evaluate(
                                """() => ({title:document.title,links:Array.from(document.querySelectorAll('a[href]')).map(a=>({href:a.href,labels:[a.getAttribute('aria-label'),a.getAttribute('title'),a.innerText,a.querySelector('img')?.alt].filter(Boolean)}))})"""
                            )
                        except Exception as exc:
                            if re.search(
                                r"execution context was destroyed|cannot find context|most likely because of a navigation",
                                str(exc),
                                re.I,
                            ):
                                await asyncio.sleep(0.2)
                                continue
                            raise
                        if (
                            not re.search(
                                r"authenticating|just a moment|checking your browser",
                                state["title"],
                                re.I,
                            )
                            and state["links"]
                        ):
                            break
                        await asyncio.sleep(0.5)
                    else:
                        errors.append(
                            {
                                "code": "access_required",
                                "message": "Catalog links were not available through ordinary browser access",
                            }
                        )
                        break
                    for link in state["links"]:
                        target = urldefrag(urljoin(url, link["href"]))[0]
                        if urlparse(target).netloc != parsed.netloc or urlparse(
                            target
                        ).scheme not in {"https", "http"}:
                            continue
                        for label in link["labels"]:
                            text = re.sub(r"\s+", " ", label).strip()
                            if text and len(text) <= 180:
                                candidates[(text.casefold(), target)] = {
                                    "title": text,
                                    "url": target,
                                    "discovered_from": page.url,
                                }
                    if not match_titles(list(candidates.values()), titles)[
                        "unresolved"
                    ]:
                        break
            finally:
                closing = True
                pending_routes = list(route_tasks)
                for task in pending_routes:
                    task.cancel()
                if pending_routes:
                    await asyncio.gather(*pending_routes, return_exceptions=True)
                page.remove_listener("requestfinished", request_done)
                page.remove_listener("requestfailed", request_done)
    result = match_titles(list(candidates.values()), titles)
    result.update(
        {
            "method": "ordinary_browser_links",
            "source_requests": source_requests,
            "errors": errors,
            "coverage": "bounded_catalog_pages",
        }
    )
    return result
