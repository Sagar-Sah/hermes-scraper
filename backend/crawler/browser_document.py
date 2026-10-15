"""Capture one ordinary product document through the retained browser session.

This is a bounded document reader, not a calculator walk. All subresources and
non-GET requests are blocked; source pacing, request budgets and worker fencing
also apply to the single navigation. Authentication challenges are not solved.
"""
from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlparse

from .browser_session import browser_page
from .pacing import retry_after_seconds, source_pacing
from .transport import ExtractionError, validate_url


async def capture_product_document(transport, url):
    parsed = await validate_url(url)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    profile = next((p for p in transport.store.profiles('source')
                    if p.get('host', '').lower() == parsed.hostname.lower()), {})
    rate, concurrency = source_pacing(profile, transport.store.config)
    await transport.check()
    if transport.max_requests and transport.calls >= transport.max_requests:
        raise ExtractionError('Configured request budget reached', 'request_limit')
    token = await transport.limiter.acquire(origin, rate, concurrency, transport.check)
    started, status, backoff = time.monotonic(), 0, 30
    navigation_sent = False
    try:
        async with browser_page(transport, origin,
                                headless=os.getenv('ATLAS_BROWSER_HEADLESS', 'true').lower() != 'false') as page:
            # page.route sees the first URL of an HTTP redirect chain only.
            # Chromium's request interception sees each hop before dispatch,
            # while retaining the browser's normal TLS and cookie handling.
            cdp = await page.context.new_cdp_session(page)
            frame = (await cdp.send('Page.getFrameTree'))['frameTree']['frame']['id']
            active, failures = set(), []
            async def route_request(event):
                nonlocal navigation_sent
                request = event['request']
                main_document = event.get('resourceType') == 'Document' and event.get('frameId') == frame
                try:
                    if (request['method'] != 'GET' or not main_document
                            or request['url'] != url or navigation_sent):
                        if main_document:
                            failures.append(ExtractionError('Redirected or repeated product navigation is not permitted', 'source_error'))
                        await cdp.send('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'BlockedByClient'})
                        return
                    await transport.check()
                    if transport.max_requests and transport.calls >= transport.max_requests:
                        raise ExtractionError('Configured request budget reached', 'request_limit')
                    navigation_sent = True
                    transport.calls += 1
                    await asyncio.to_thread(transport._count, False)
                    await cdp.send('Fetch.continueRequest', {'requestId': event['requestId']})
                except Exception as exc:
                    failures.append(exc)
                    try:
                        await cdp.send('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'BlockedByClient'})
                    except Exception:
                        pass
            def intercepted(event):
                task = asyncio.create_task(route_request(event))
                active.add(task)
                task.add_done_callback(active.discard)
            cdp.on('Fetch.requestPaused', intercepted)
            try:
                # No script execution is needed to read the response body. This
                # also prevents inline code from opening unowned popup tabs.
                await cdp.send('Emulation.setScriptExecutionDisabled', {'value': True})
                await cdp.send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]})
                try:
                    response = await page.goto(url, wait_until='domcontentloaded', timeout=45000)
                except Exception:
                    if failures:
                        raise failures[0]
                    raise
                await transport.check()
                if failures:
                    raise failures[0]
                if response is None or response.url != url:
                    raise ExtractionError('Product navigation did not return the requested document', 'source_error')
                status = response.status
                headers = await response.all_headers()
                backoff = retry_after_seconds(headers.get('retry-after'))
                length = headers.get('content-length', '')
                if length.isdigit() and int(length) > transport.store.config.max_response_bytes:
                    raise ExtractionError('Product document exceeds configured size limit', 'response_too_large')
                body = await response.body()
                if len(body) > transport.store.config.max_response_bytes:
                    raise ExtractionError('Product document exceeds configured size limit', 'response_too_large')
                await transport.check()
                result = await transport.record_observation(url, status, headers, body,
                    context={'adapter':'printitprints', 'lane':'browser_document',
                             'capture':'exact_product_document', 'subresources':'blocked'}, count_request=False)
                if status in {401,403} or any(x in body[:10000].lower() for x in
                        [b'<title>just a moment', b'<title>authenticating', b'verify you are human']):
                    raise ExtractionError('Product document requires authorized browser access', 'access_required')
                if status >= 400:
                    raise ExtractionError(f'Product document returned HTTP {status}', 'source_error')
                return result
            finally:
                # Keep interception enabled until the owned tab is gone. Turning
                # it off first could release a delayed asset/write request.
                try:
                    await page.close()
                finally:
                    pending = list(active)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    try:
                        await cdp.detach()
                    except Exception:
                        pass
    finally:
        await asyncio.to_thread(transport.limiter.release, origin, token,
                                time.monotonic()-started, status, backoff, rate)
