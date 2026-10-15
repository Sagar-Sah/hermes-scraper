#!/usr/bin/env python3
"""Offline full-browser traversal benchmark; never contacts a supplier.

Run from the repository root with PYTHONPATH=backend and the project Python:
  python scripts/bench_browser_traversal.py --reviewed-source-fixture --output /tmp/browser-benchmark.json
  python scripts/bench_browser_traversal.py --browser-ref a01ac7d --output /tmp/baseline.json

The loopback fixture uses eight exact size/paper/quantity configurations, a real
Chromium page, source-triggered local JSON quotes, and an in-memory journal. It
measures traversal/settling, not durable storage or supplier/network capacity.
--reviewed-source-fixture explicitly opts this immediate-response fixture into
the reviewed-source shortcut locally; production eligibility is not changed.
URL validation is bypassed only for the known loopback fixture URL. The no-op
limiter records every acquisition; this is not a supplier rate-limit benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


HTML = b'''<!doctype html><title>Offline eight-row benchmark</title>
<h1>Local traversal fixture</h1><main><div id="order-form">
<label for="size">Size</label><select id="size"><option value="small">Small</option><option value="large">Large</option></select>
<label for="paper">Paper</label><select id="paper"><option value="gloss">Gloss</option><option value="matte">Matte</option></select>
<label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option><option value="250">250</option></select>
</div></main><div id="subtotal">$0.00</div><div id="ajaxLoader" style="display:none">Loading</div>
<script>
async function quote(){
 document.querySelector('#ajaxLoader').style.display='block';
 const query=new URLSearchParams([...document.querySelectorAll('select')].map(e=>[e.id,e.value]));
 const response=await fetch('/service/rest/v1/products/prices?'+query);
 const result=await response.json();
 document.querySelector('#subtotal').textContent='$'+result.subtotal;
 document.querySelector('#ajaxLoader').style.display='none';
}
document.querySelectorAll('select').forEach(e=>e.addEventListener('change',quote));quote();
</script>'''


class MemoryJournal:
    """Exercise the normal journal branch path without measuring disk writes."""
    def __init__(self):
        self.completed = 0

    def skip(self, prefix, services):
        return None

    def contains(self, prefix):
        return False

    def begin(self, prefix, services):
        return SimpleNamespace(prefix=prefix)

    async def complete(self, frame, notes):
        self.completed += 1

    def abandon(self, frame):
        pass

    def end(self, frame):
        pass


class AsyncioMeasurements:
    def __init__(self):
        self.sleeps = Counter()

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, seconds, *args, **kwargs):
        self.sleeps[str(seconds)] += 1
        return await asyncio.sleep(seconds, *args, **kwargs)


def load_adapter(reference, directory):
    if not reference:
        from crawler.adapters import browser
        return browser
    source = subprocess.check_output(
        ['git', 'show', f'{reference}:backend/crawler/adapters/browser.py'], text=True
    )
    target = Path(directory) / 'benchmark_browser.py'
    target.write_text(source)
    spec = importlib.util.spec_from_file_location('crawler.adapters._benchmark_browser', target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def measure(adapter, *, reviewed_fixture=False):
    from crawler.browser_session import BrowserSessionPool
    from crawler.transport import validate_url as real_validate_url

    counts = Counter()
    request_paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            request_paths.append(parsed.path)
            if parsed.path == '/product':
                body, kind = HTML, 'text/html'
            elif parsed.path == '/service/rest/v1/products/prices':
                query = parse_qs(parsed.query)
                size, paper, quantity = (query[key][0] for key in ('size', 'paper', 'quantity'))
                amount = (Decimal(10 if size == 'small' else 20)
                          + Decimal(1 if paper == 'gloss' else 2) + Decimal(quantity) / 10)
                body, kind = json.dumps({'subtotal': f'{amount:.2f}'}).encode(), 'application/json'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    source_url = f'http://127.0.0.1:{server.server_port}/product'
    source_origin = f'http://127.0.0.1:{server.server_port}'

    async def validate_fixture(url):
        if urlsplit(url).netloc == urlsplit(source_url).netloc:
            return urlsplit(url)
        return await real_validate_url(url)

    class Limiter:
        async def acquire(self, origin, rps, concurrency, check):
            assert origin == source_origin, 'Fixture must not contact another origin'
            counts['limiter_acquisitions'] += 1
            await check()
            return 'fixture-permit'

        def release(self, *args):
            counts['limiter_releases'] += 1

    class Transport:
        def __init__(self):
            self.store = SimpleNamespace(config=SimpleNamespace(requests_per_second=2, source_concurrency=2))
            self.limiter = Limiter()
            self.browser_progress = MemoryJournal()

        async def check(self):
            counts['transport_checks'] += 1

        async def record_observation(self, url, status, headers, body, context=None, count_request=True):
            counts['observations'] += 1
            return SimpleNamespace(data=json.loads(body), response_id=f'fixture-{counts["observations"]}', status=status)

    class PageMeasurements:
        def __init__(self, page):
            self.page = page

        def __getattr__(self, name):
            return getattr(self.page, name)

        async def evaluate(self, expression, *args, **kwargs):
            counts['page_evaluations'] += 1
            if expression == adapter.SNAPSHOT_JS:
                counts['snapshot_evaluations'] += 1
            return await self.page.evaluate(expression, *args, **kwargs)

    pool = BrowserSessionPool()

    @asynccontextmanager
    async def fixture_page(transport, origin, *, headless=True):
        assert origin == source_origin
        async with pool.page(origin, headless=True) as page:
            yield PageMeasurements(page)

    transport = Transport()
    events, rows = [], []
    measurements = AsyncioMeasurements()

    async def emit(kind, payload):
        events.append((kind, deepcopy(payload)))

    try:
        with patch('crawler.transport.validate_url', validate_fixture), \
             patch('crawler.browser_session.browser_page', fixture_page), \
             patch.object(adapter, 'asyncio', measurements), \
             patch.object(adapter, 'supports_stable_reuse',
                          lambda url, state: reviewed_fixture and url == source_url,
                          create=True):
            started = time.perf_counter()
            async for row in adapter.BrowserAdapter().extract({
                'url': source_url, 'selection_filters': {},
                'source_metadata': {'currency': 'USD', 'price_selector': '#subtotal'},
            }, transport, emit):
                rows.append(row)
            elapsed = time.perf_counter() - started
        actual = sorted((row['configuration']['size']['value'], row['configuration']['paper']['value'],
                         row['quantity'], row['subtotal']) for row in rows)
        expected = sorted((size, paper, quantity,
                           f'{Decimal(10 if size == "small" else 20) + Decimal(1 if paper == "gloss" else 2) + Decimal(quantity) / 10:.2f}')
                          for size in ('small', 'large') for paper in ('gloss', 'matte') for quantity in (100, 250))
        assert actual == expected, (actual, expected)
        report = next(payload for kind, payload in events if kind == 'coverage')
        assert report['count_is_exact'] is True, report
        assert report['status'] == 'visible_domain_exhausted', report
        assert counts['limiter_acquisitions'] == counts['limiter_releases']
        assert Counter(request_paths) == {
            '/product': 1, '/service/rest/v1/products/prices': 12,
        }, request_paths  # Optimization must not add supplier-facing requests.
        return {'elapsed_seconds': round(elapsed, 4), 'rows': len(rows),
                'request_counts': dict(Counter(request_paths)), 'metrics': dict(counts),
                'adapter_sleep_counts': dict(measurements.sleeps),
                'requested_adapter_sleep_seconds': round(sum(float(delay) * count for delay, count in measurements.sleeps.items()), 4),
                'completed_journal_branches': transport.browser_progress.completed,
                'exact_rows': [list(row) for row in actual], 'coverage': report}
    finally:
        await pool.close()
        await asyncio.to_thread(server.shutdown)
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-ref', help='Load browser adapter source from a local Git ref')
    parser.add_argument('--reviewed-source-fixture', action='store_true',
                        help='Opt this local immediate-response fixture into reviewed-source stability reuse')
    parser.add_argument('--iterations', type=int, default=1)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    with tempfile.TemporaryDirectory(prefix='hermes-browser-benchmark-') as temporary:
        adapter = load_adapter(args.browser_ref, temporary)
        runs = [asyncio.run(measure(adapter, reviewed_fixture=args.reviewed_source_fixture))
                for _ in range(args.iterations)]
    result = {'benchmark': 'eight-row local Chromium traversal with in-memory journal',
              'measured_at_utc': datetime.now(timezone.utc).isoformat(),
              'adapter_ref': args.browser_ref or 'working-tree',
              'reviewed_source_fixture': args.reviewed_source_fixture,
              'readiness_contract': 'Each choice triggers an exact immediate local JSON quote; reviewed-source eligibility is patched for this fixture only.' if args.reviewed_source_fixture else 'Generic source timing; reviewed-source reuse is disabled locally.',
              'python': platform.python_version(), 'platform': platform.platform(),
              'scope': 'Real local browser; no supplier requests, persisted product rows/exports or source rate-limit timing. Chromium uses temporary runtime files.',
              'runs': runs}
    encoded = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end='')


if __name__ == '__main__':
    main()
