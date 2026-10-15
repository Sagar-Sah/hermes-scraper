"""Durable, rate-limited exact DPS price observations for diagnosed products.

The supplier route returns a price even for some explicitly invalid sizes, so
these observations never assert availability or product completion.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from weakref import WeakValueDictionary
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .dps_pilot import sanitize_price
from .dps_quote_contract import scoped_request
from .pacing import retry_after_seconds
from .tls import tls_context
from .transport import RateLimiter, validate_url

ORIGIN = "https://api.digital-print-solutions.com"
CACHE_SECONDS = 300


class DPSExactQuoteManager:
    def __init__(self, store):
        self.store = store
        self.root = Path(store.root) / "dps-exact-quotes"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.limiter = RateLimiter(store)
        # Independent selections can quote concurrently under the shared
        # supplier limiter; identical selections share one durable response.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def quote(self, pilot: dict, *, width, height, quantity, selected_options):
        request = scoped_request(pilot, width=width, height=height, quantity=quantity,
                                 selected_options=selected_options)
        product_id = pilot["observations"][0]["context"]["product_id"]
        url = f"{ORIGIN}/pricing/{product_id}"
        await validate_url(url)
        canonical = json.dumps({"pilot_id": pilot["id"], "request": request},
                               sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(canonical.encode()).hexdigest()
        path = self.root / f"{key}.json"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if path.exists():
                saved = json.loads(path.read_text())
                if (saved.get("pilot_id") == pilot["id"] and saved.get("request") == request
                        and time.time() - saved.get("observed_unix", 0) <= CACHE_SECONDS):
                    return {**saved, "cache_hit": True}
            async def active():
                return None
            token = await self.limiter.acquire(ORIGIN, 5, 4, active)
            started = time.monotonic()
            status = 0
            retry_after = 0
            try:
                async with httpx.AsyncClient(verify=tls_context(), timeout=20,
                                             follow_redirects=False) as client:
                    response = await client.get(url, params=request,
                                                headers={"Accept": "application/json"})
                status = response.status_code
                retry_after = retry_after_seconds(response.headers.get("Retry-After")) or 0
                if status != 200 or len(response.content) > 200000:
                    raise ValueError("source_exact_price_unavailable")
                price = sanitize_price(response.json())
                record = {
                    "schema": 1, "pilot_id": pilot["id"], "source_url": pilot["url"],
                    "supplier_product_id": product_id, "request": request, "price": price,
                    "response_sha256": hashlib.sha256(response.content).hexdigest(),
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "observed_unix": time.time(), "price_scope": "exact_selection",
                    "availability": "unverified", "production_availability": "unverified",
                    "cache_hit": False,
                }
                temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                with temp.open("x") as stream:
                    os.chmod(temp, 0o600)
                    json.dump(record, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
                return record
            finally:
                await asyncio.to_thread(self.limiter.release, ORIGIN, token,
                                        time.monotonic() - started, status, retry_after, 5)
