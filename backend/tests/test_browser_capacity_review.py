"""Source admission must release reservations through every queue exit."""

import asyncio

import pytest

from crawler.browser_capacity import BrowserWorkload


@pytest.mark.asyncio
async def test_cancel_source_waiter_does_not_disturb_active_page_or_leak_event():
    workload, semaphore = BrowserWorkload(), asyncio.Semaphore(2)
    holder_started, release_holder, waiter_checked = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def check():
        pass

    async def holder():
        async with workload.slot(semaphore, check, source_origin="fixture", per_source_limit=1):
            holder_started.set()
            await release_holder.wait()

    async def waiting_check():
        waiter_checked.set()

    async def waiter():
        async with workload.slot(semaphore, waiting_check, source_origin="fixture", per_source_limit=1):
            raise AssertionError("Source is still occupied")

    active = asyncio.create_task(holder())
    await holder_started.wait()
    waiting = asyncio.create_task(waiter())
    try:
        await waiter_checked.wait()
        assert workload.snapshot() == {"active_browser_workers": 1, "waiting_for_browser_workers": 1}
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert workload._source_active == {"fixture": 1}
        assert workload.snapshot() == {"active_browser_workers": 1, "waiting_for_browser_workers": 0}
    finally:
        release_holder.set()
        await active
    assert not workload._source_active and not workload._source_changed
    assert semaphore._value == 2


@pytest.mark.asyncio
async def test_check_failure_after_source_reservation_releases_it():
    workload, semaphore = BrowserWorkload(), asyncio.Semaphore(0)
    checks = 0

    async def check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("Product stopped before global browser admission")

    with pytest.raises(RuntimeError, match="Product stopped"):
        async with workload.slot(semaphore, check, source_origin="fixture", per_source_limit=1):
            raise AssertionError("No global capacity")
    assert not workload._source_active and not workload._source_changed
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}
    assert semaphore._value == 0


@pytest.mark.asyncio
async def test_mixed_99_task_cancellation_releases_global_and_source_capacity():
    workload, semaphore = BrowserWorkload(), asyncio.Semaphore(4)
    full, release = asyncio.Event(), asyncio.Event()

    async def check():
        pass

    async def product(index):
        async with workload.slot(semaphore, check, source_origin=f"fixture-{index % 3}", per_source_limit=2):
            if workload.snapshot()["active_browser_workers"] == 4:
                full.set()
            await release.wait()

    tasks = [asyncio.create_task(product(index)) for index in range(99)]
    try:
        await asyncio.wait_for(full.wait(), 2)
        assert sum(workload.snapshot().values()) == 99
        cancelled = tasks[::2]
        for task in cancelled:
            task.cancel()
        await asyncio.gather(*cancelled, return_exceptions=True)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not workload._source_active and not workload._source_changed
    assert workload.snapshot() == {"active_browser_workers": 0, "waiting_for_browser_workers": 0}
    assert semaphore._value == 4
