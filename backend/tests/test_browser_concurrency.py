"""Browser page capacity is independent from the shared source request budget."""

import asyncio
import time

import pytest
from sqlalchemy import select

from crawler.config import Settings
from crawler.store import Permit, Store
from crawler.transport import RateLimiter


def test_browser_page_capacity_is_explicit_and_defaults_to_four(tmp_path, monkeypatch):
    monkeypatch.delenv("ATLAS_BROWSER_PRODUCT_CONCURRENCY", raising=False)
    assert Settings(data_dir=tmp_path).browser_product_concurrency == 4
    monkeypatch.setenv("ATLAS_BROWSER_PRODUCT_CONCURRENCY", "3")
    assert Settings(data_dir=tmp_path).browser_product_concurrency == 3
    monkeypatch.setenv("ATLAS_BROWSER_PRODUCT_CONCURRENCY", "0")
    assert Settings(data_dir=tmp_path).browser_product_concurrency == 1


@pytest.mark.asyncio
async def test_three_product_limiters_share_two_rps_and_two_request_budget(tmp_path):
    store = Store(
        Settings(data_dir=tmp_path, s3_bucket="", browser_product_concurrency=3)
    )
    starts = []
    active = 0
    maximum_active = 0

    async def check():
        pass

    async def product():
        nonlocal active, maximum_active
        limiter = RateLimiter(store)
        for _ in range(2):
            token = await limiter.acquire("https://local-fixture.invalid", 2, 2, check)
            started = time.monotonic()
            starts.append(started)
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                # Work lasts long enough that spacing alone would allow three
                # concurrent requests; the database permit ceiling must apply.
                await asyncio.sleep(1.2)
            finally:
                active -= 1
                await asyncio.to_thread(
                    limiter.release,
                    "https://local-fixture.invalid",
                    token,
                    time.monotonic() - started,
                    200,
                    0,
                    2,
                )

    try:
        await asyncio.gather(*(product() for _ in range(3)))
        assert len(starts) == 6
        assert maximum_active == 2
        assert all(
            later - earlier >= 0.45 for earlier, later in zip(starts, starts[1:])
        )
        with store.session() as session:
            assert list(session.scalars(select(Permit))) == []
    finally:
        store.engine.dispose()
