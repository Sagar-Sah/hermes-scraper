import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from crawler.config import Settings
from crawler.store import Batch, Product, Run, Store


@pytest.fixture
def store(tmp_path):
    result = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    yield result
    result.engine.dispose()


def create_run(store, count=1, status="queued"):
    with store.session() as session:
        run = Run(name="Recovery fixture", status=status, spec={"adapter": "fixture"})
        session.add(run)
        session.flush()
        products = [
            Product(run_id=run.id, url=f"fixture://product/{index}")
            for index in range(count)
        ]
        session.add_all(products)
        session.flush()
        return run.id, [p.id for p in products]


def row(key="row-1"):
    return {
        "id": key,
        "title": "Fixture",
        "configuration": {"stock": "matte"},
        "quantity": 100,
        "currency": "USD",
        "subtotal": "20.00",
    }


def test_parallel_claims_never_assign_one_product_twice(store):
    _, ids = create_run(store, 12)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda index: store.claim(f"worker-{index}"), range(20)))
    actual = [claim for claim in claims if claim]
    assert len(actual) == len(ids)
    assert {claim["id"] for claim in actual} == set(ids)
    assert all(claim["epoch"] == 1 for claim in actual)


def test_expired_lease_reclaim_fences_old_heartbeat_and_writes(store):
    run_id, ids = create_run(store)
    first = store.claim("old-worker")
    store.append_rows(ids[0], first["epoch"], [row()])
    with store.session() as session:
        product = session.get(Product, ids[0])
        product.lease_until = time.time() - 1
    second = store.claim("replacement-worker")
    assert second["epoch"] == first["epoch"] + 1
    assert not store.heartbeat(ids[0], first["epoch"])
    assert store.heartbeat(ids[0], second["epoch"])
    with pytest.raises(RuntimeError, match="superseded"):
        store.append_rows(ids[0], first["epoch"], [row("stale")])
    store.append_rows(ids[0], second["epoch"], [row("row-2")])
    assert [entry["id"] for entry in store.iter_rows(run_id=run_id)] == [
        "row-1",
        "row-2",
    ]
    with store.session() as session:
        assert session.get(Product, ids[0]).row_count == 2


def test_paused_and_cancelled_runs_are_never_claimed(store):
    paused, _ = create_run(store, status="paused")
    cancelled, _ = create_run(store, status="cancelled")
    assert store.claim("worker") is None
    with store.session() as session:
        session.get(Run, paused).status = "queued"
    claim = store.claim("worker")
    assert claim["run_id"] == paused
    assert store.status(cancelled) == "cancelled"


def test_restart_restores_batch_keys_order_and_fencing_epoch(store):
    run_id, ids = create_run(store)
    claim = store.claim("old-process")
    store.append_rows(ids[0], claim["epoch"], [row("first"), row("second")])
    with store.session() as session:
        session.get(Product, ids[0]).lease_until = 0
    restarted = Store(store.config)
    try:
        replacement = restarted.claim("new-process")
        assert replacement["epoch"] == claim["epoch"] + 1
        assert restarted.existing_keys(ids[0]) == {"first", "second"}
        assert [r["id"] for r in restarted.iter_rows(run_id=run_id)] == [
            "first",
            "second",
        ]
        with restarted.session() as session:
            batches = list(
                session.scalars(select(Batch).where(Batch.product_id == ids[0]))
            )
        assert len(batches) == 1 and batches[0].sequence == 0
    finally:
        restarted.engine.dispose()


def test_content_addressed_objects_reject_path_escape(store):
    first = store.put_object(b"fixture bytes")
    assert store.put_object(b"fixture bytes") == first
    assert store.get_object(first) == b"fixture bytes"
    with pytest.raises(ValueError, match="Invalid object path"):
        store.get_object("../outside")
