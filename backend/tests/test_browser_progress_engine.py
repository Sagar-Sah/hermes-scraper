"""Engine durability/exports contract for completed browser branch journals."""

from collections import Counter

import pytest
from sqlalchemy import select

import crawler.adapters.browser as browser_module
from crawler.engine import Engine
from crawler.store import BrowserBranch, Event, Product, Run
from crawler.transport import ExtractionError
from test_engine import store, observed_row, state


def browser_job(store):
    with store.session() as session:
        run = Run(
            name="Browser journal engine fixture",
            spec={"adapter": "browser", "image_mode": "download"},
        )
        session.add(run)
        session.flush()
        product = Product(run_id=run.id, url="https://fixture.invalid/product")
        session.add(product)
        session.flush()
        return run.id, product.id


def resume(store, run_id, product_id):
    with store.session() as session:
        session.get(Run, run_id).status = "queued"
        session.get(Product, product_id).status = "queued"
        session.get(Product, product_id).lease_until = 0


@pytest.mark.asyncio
@pytest.mark.parametrize("disappear", [False, True])
async def test_engine_resume_uses_only_skipped_membership_and_retains_images(
    store, monkeypatch, disappear
):
    rid, pid = browser_job(store)
    attempts, quoted, downloaded = 0, Counter(), []

    class BrowserAdapter:
        async def extract(self, spec, transport, emit):
            nonlocal attempts
            attempts += 1
            journal = transport.browser_progress
            for index in ([1] if disappear and attempts == 2 else [0, 1]):
                prefix = {"variant": str(index)}
                if journal.skip(prefix) is not None:
                    continue
                frame = journal.begin(prefix)
                try:
                    quoted[index] += 1
                    row = observed_row(spec, index)
                    row["images"] = [f"https://fixture.invalid/image-{index}.jpg"]
                    yield row
                    await journal.complete(frame, set())
                finally:
                    journal.end(frame)
                if attempts == 1:
                    raise ExtractionError("Local crash after completed first branch")
            await emit("coverage", {"status": "visible_domain_exhausted"})

    async def download_images(urls, *args):
        downloaded.extend(sorted(urls))
        return []

    monkeypatch.setattr(browser_module, "BrowserAdapter", BrowserAdapter)
    monkeypatch.setattr("crawler.assets.download_images", download_images)
    first = Engine(store)
    try:
        await first.process(store.claim(first.owner))
    finally:
        await first.client.aclose()
    assert state(store, rid, pid)[2] == 1
    resume(store, rid, pid)
    second = Engine(store)
    try:
        await second.process(store.claim(second.owner))
    finally:
        await second.client.aclose()
    assert quoted == {0: 1, 1: 1}
    if disappear:
        assert state(store, rid, pid)[1] == "needs_attention"
        with store.session() as session:
            errors = list(
                session.scalars(select(Event).where(Event.type == "product_error"))
            )
        assert errors[-1].payload["code"] == "snapshot_changed"
        assert not downloaded
    else:
        assert state(store, rid, pid)[2] == 2
        assert downloaded == [
            "https://fixture.invalid/image-0.jpg",
            "https://fixture.invalid/image-1.jpg",
        ]


@pytest.mark.asyncio
async def test_crash_after_row_commit_before_marker_replays_safely(store, monkeypatch):
    rid, pid = browser_job(store)
    calls = Counter()

    class BrowserAdapter:
        async def extract(self, spec, transport, emit):
            journal = transport.browser_progress
            for index in [0, 1]:
                prefix = {"variant": str(index)}
                if journal.skip(prefix) is not None:
                    continue
                frame = journal.begin(prefix)
                try:
                    calls[index] += 1
                    yield observed_row(spec, index)
                    await journal.complete(frame, set())
                finally:
                    journal.end(frame)
            await emit("coverage", {"status": "visible_domain_exhausted"})

    original = store.complete_browser_branch
    failed = False

    def fail_marker_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("Local crash between durable row and branch marker")
        return original(*args, **kwargs)

    monkeypatch.setattr(browser_module, "BrowserAdapter", BrowserAdapter)
    monkeypatch.setattr(store, "complete_browser_branch", fail_marker_once)
    engine = Engine(store)
    try:
        await engine.process(store.claim(engine.owner))
        assert state(store, rid, pid)[2] == 1
        with store.session() as session:
            assert not list(session.scalars(select(BrowserBranch)))
        resume(store, rid, pid)
        await engine.process(store.claim(engine.owner))
    finally:
        await engine.client.aclose()
    assert state(store, rid, pid)[2] == 2
    assert calls == {0: 2, 1: 1}
    assert len(list(store.iter_rows(product_id=pid))) == 2
