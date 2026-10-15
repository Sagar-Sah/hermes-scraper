"""Out-of-order async-generator cleanup cannot complete interrupted branches."""

import asyncio
import gc

import pytest
from sqlalchemy import select

from crawler.adapters.browser import BrowserAdapter
from crawler.store import BrowserBranch
from test_browser_progress import (
    browser_page_html,
    commit_rows,
    durable_store,
    journal_for,
)
from test_browser_traversal import local_page


@pytest.mark.asyncio
async def test_abandon_parent_closes_descendants_but_normal_order_remains_strict(durable_store):
    journal = await journal_for(durable_store)
    parent = journal.begin({"paper": "a"})
    child = journal.begin({"paper": "a", "quantity": "100"})
    with pytest.raises(RuntimeError, match="close in order"):
        journal.end(parent)
    journal.abandon(parent)
    assert parent.abandoned and child.abandoned and not journal.frames
    # Delayed child finalization and repeated interruption are harmless only
    # because an explicit abort already fenced this unfinished subtree.
    journal.end(parent)
    journal.abandon(child)
    journal.end(child)
    await journal.complete(child, set())
    store, _, _ = durable_store
    with store.session() as session:
        assert not list(session.scalars(select(BrowserBranch)))
    healthy = journal.begin({"paper": "b"})
    journal.end(healthy)
    with pytest.raises(RuntimeError, match="close in order"):
        journal.end(healthy)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["aclose", "cancel"])
async def test_browser_yield_cleanup_keeps_durable_row_without_false_completion(
    local_page, durable_store, monkeypatch, method
):
    journal = await journal_for(durable_store)
    original = BrowserAdapter.extract
    held_generators = []
    nested_frames = []

    async def stop_at_yield(adapter, *args, **kwargs):
        generator = original(adapter, *args, **kwargs)
        held_generators.append(generator)
        try:
            row = await anext(generator)
            nested_frames.extend(journal.frames)
            yield row  # local_page commits this row before requesting another.
            if method == "cancel":
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
        finally:
            await generator.aclose()

    monkeypatch.setattr(BrowserAdapter, "extract", stop_at_yield)
    errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: errors.append(context))
    try:
        operation = local_page(
            browser_page_html(), journal=journal,
            on_row=commit_rows(durable_store, journal),
            response=lambda _: (200, {"subtotal": "5.00"}),
        )
        if method == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            await operation
        held_generators.clear()
        for _ in range(20):
            gc.collect()
            await asyncio.sleep(0.05)
            if not journal.frames:
                break
        assert len(nested_frames) >= 3
        assert not journal.frames
        assert all(frame.abandoned for frame in nested_frames)
        assert not errors
        store, _, pid = durable_store
        assert len(list(store.iter_rows(product_id=pid))) == 1
        with store.session() as session:
            assert not list(session.scalars(select(BrowserBranch)))
        # Even a late completion attempt cannot turn the committed first row
        # into an exhaustion marker for unvisited quantity/product siblings.
        for frame in nested_frames:
            await journal.complete(frame, set())
        assert not journal.completed
    finally:
        loop.set_exception_handler(previous_handler)
