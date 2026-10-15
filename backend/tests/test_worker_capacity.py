"""Loaded browser worker limits must be explicit without raising source pacing."""

import httpx
import pytest

from crawler.config import Settings


def test_new_install_runs_multiple_browser_products_without_enabling_99_tabs(tmp_path, monkeypatch):
    monkeypatch.delenv("ATLAS_BROWSER_PRODUCT_CONCURRENCY", raising=False)
    monkeypatch.delenv("ATLAS_PRODUCT_CONCURRENCY", raising=False)
    config = Settings(data_dir=tmp_path)
    assert config.product_concurrency == 100
    assert config.browser_product_concurrency == 4
    assert config.execution_capacity()["effective_browser_page_limit"] == 4
    assert (config.requests_per_second, config.source_concurrency) == (2, 2)


@pytest.mark.parametrize("configured,expected", [("99", 99), ("256", 256), ("999", 256), ("0", 1)])
def test_environment_configures_bounded_browser_worker_count(tmp_path, monkeypatch, configured, expected):
    monkeypatch.setenv("ATLAS_BROWSER_PRODUCT_CONCURRENCY", configured)
    config = Settings(data_dir=tmp_path)
    assert config.browser_product_concurrency == expected
    assert config.execution_capacity()["browser_page_limit"] == expected


def test_effective_browser_limit_respects_product_worker_limit(tmp_path):
    config = Settings(data_dir=tmp_path, product_concurrency=6, browser_product_concurrency=99)
    assert config.execution_capacity() == {
        "product_task_limit": 6, "browser_page_limit": 99,
        "effective_browser_page_limit": 6, "maximum_configured_limit": 256,
        "browser_setting": "ATLAS_BROWSER_PRODUCT_CONCURRENCY",
        "restart_required": True, "scope": "per_instance",
    }


@pytest.mark.asyncio
async def test_health_reports_running_capacity_without_exposing_configuration_secrets(tmp_path, monkeypatch):
    import crawler.app as api

    config = Settings(data_dir=tmp_path, api_token="private-fixture-token", product_concurrency=100,
                      browser_product_concurrency=99)
    monkeypatch.setattr(api, "settings", config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver") as client:
        response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["execution_capacity"] == {**config.execution_capacity(), **api.browser_workload.snapshot()}
    assert "private-fixture-token" not in response.text


@pytest.mark.asyncio
async def test_browser_activity_counts_queue_active_release_and_cancel():
    import asyncio
    from crawler.browser_capacity import BrowserWorkload

    workload = BrowserWorkload()
    semaphore = asyncio.Semaphore(1)
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def check():
        pass

    async def first():
        async with workload.slot(semaphore, check):
            first_started.set()
            await release.wait()

    async def queued():
        async with workload.slot(semaphore, check):
            raise AssertionError("Cancelled waiter must not acquire a slot")

    task = asyncio.create_task(first())
    await first_started.wait()
    waiter = asyncio.create_task(queued())
    try:
        await asyncio.sleep(0)
        assert workload.snapshot() == {"active_browser_workers": 1, "waiting_for_browser_workers": 1}
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert workload.snapshot() == {"active_browser_workers": 1, "waiting_for_browser_workers": 0}
    finally:
        release.set()
        await task
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}
    assert not semaphore.locked()
    with pytest.raises(RuntimeError):
        async with workload.slot(semaphore, check):
            raise RuntimeError("Failure during extraction")
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}
    assert not semaphore.locked()


@pytest.mark.asyncio
async def test_browser_activity_releases_waiter_when_queue_check_stops_work():
    import asyncio
    from crawler.browser_capacity import BrowserWorkload

    workload = BrowserWorkload()
    semaphore = asyncio.Semaphore(0)

    async def interrupted():
        raise RuntimeError("Paused while waiting")

    with pytest.raises(RuntimeError, match="Paused while waiting"):
        async with workload.slot(semaphore, interrupted):
            raise AssertionError("No browser work should start")
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}


@pytest.mark.asyncio
async def test_99_queued_products_obey_browser_capacity_and_report_waiting():
    import asyncio
    from crawler.browser_capacity import BrowserWorkload

    workload = BrowserWorkload()
    semaphore = asyncio.Semaphore(4)
    release = asyncio.Event()
    first_wave = asyncio.Event()
    started = 0
    maximum = 0

    async def check():
        pass

    async def product():
        nonlocal started, maximum
        async with workload.slot(semaphore, check):
            started += 1
            maximum = max(maximum, workload.snapshot()["active_browser_workers"])
            if started == 4:
                first_wave.set()
            await release.wait()

    tasks = [asyncio.create_task(product()) for _ in range(99)]
    try:
        await asyncio.wait_for(first_wave.wait(), timeout=2)
        assert workload.snapshot() == {"active_browser_workers": 4, "waiting_for_browser_workers": 95}
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert started == 99 and maximum == 4
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}


@pytest.mark.asyncio
async def test_99_products_share_source_page_caps_without_starving_second_origin():
    import asyncio
    from collections import Counter
    from crawler.browser_capacity import BrowserWorkload

    workload = BrowserWorkload()
    semaphore = asyncio.Semaphore(4)
    release = asyncio.Event()
    both_sources_active = asyncio.Event()
    active_by_source = Counter()
    completed = Counter()
    max_by_source = Counter()
    maximum = 0

    async def check():
        pass

    async def product(origin):
        nonlocal maximum
        async with workload.slot(semaphore, check, source_origin=origin, per_source_limit=2):
            active_by_source[origin] += 1
            max_by_source[origin] = max(max_by_source[origin], active_by_source[origin])
            maximum = max(maximum, workload.snapshot()["active_browser_workers"])
            if active_by_source == {"source-a": 2, "source-b": 2}:
                both_sources_active.set()
            try:
                await release.wait()
                completed[origin] += 1
            finally:
                active_by_source[origin] -= 1

    tasks = [asyncio.create_task(product("source-a")) for _ in range(97)]
    tasks.extend(asyncio.create_task(product("source-b")) for _ in range(2))
    try:
        await asyncio.wait_for(both_sources_active.wait(), timeout=2)
        assert workload.snapshot() == {"active_browser_workers": 4, "waiting_for_browser_workers": 95}
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert completed == {"source-a": 97, "source-b": 2}
    assert max_by_source == {"source-a": 2, "source-b": 2}
    assert maximum == 4
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}
    assert not workload._source_active
    async with workload.slot(semaphore, check, source_origin="source-a", per_source_limit=2):
        assert workload.snapshot()["active_browser_workers"] == 1


@pytest.mark.asyncio
async def test_source_admission_releases_on_cancellation_before_global_slot():
    import asyncio
    from crawler.browser_capacity import BrowserWorkload

    workload = BrowserWorkload()
    semaphore = asyncio.Semaphore(0)
    checked = asyncio.Event()

    async def check():
        checked.set()

    async def waiting():
        async with workload.slot(semaphore, check, source_origin="fixture", per_source_limit=1):
            raise AssertionError("Global browser cap is full")

    task = asyncio.create_task(waiting())
    await checked.wait()
    await asyncio.sleep(0)
    assert workload._source_active == {"fixture": 1}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not workload._source_active
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}
    semaphore.release()
    async with workload.slot(semaphore, check, source_origin="fixture", per_source_limit=1):
        assert workload.snapshot()["active_browser_workers"] == 1


@pytest.mark.asyncio
async def test_cancelling_source_waiter_preserves_owner_and_other_origin_progress():
    import asyncio
    from crawler.browser_capacity import BrowserWorkload

    workload = BrowserWorkload()
    semaphore = asyncio.Semaphore(2)
    release = asyncio.Event()
    owner_active = asyncio.Event()

    async def check():
        pass

    async def owner():
        async with workload.slot(semaphore, check, source_origin="first", per_source_limit=1):
            owner_active.set()
            await release.wait()

    async def queued():
        async with workload.slot(semaphore, check, source_origin="first", per_source_limit=1):
            raise AssertionError("Source remains occupied")

    task = asyncio.create_task(owner())
    await owner_active.wait()
    waiter = asyncio.create_task(queued())
    try:
        await asyncio.sleep(0)
        assert workload.snapshot() == {"active_browser_workers": 1, "waiting_for_browser_workers": 1}
        async with workload.slot(semaphore, check, source_origin="second", per_source_limit=1):
            assert workload.snapshot()["active_browser_workers"] == 2
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert workload._source_active == {"first": 1}
    finally:
        release.set()
        await task
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}
    assert not workload._source_active and not workload._source_changed


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 0, -1, 1.5, float("nan"), True])
async def test_source_browser_admission_rejects_invalid_limits(limit):
    import asyncio
    from crawler.browser_capacity import BrowserWorkload

    workload = BrowserWorkload()
    async def check():
        pass
    with pytest.raises(ValueError, match="positive limit"):
        async with workload.slot(asyncio.Semaphore(1), check, source_origin="fixture", per_source_limit=limit):
            raise AssertionError("No invalid source limit may acquire a worker")
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}
