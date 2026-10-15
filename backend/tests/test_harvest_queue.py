"""Durable request checkpointing with global dedup and fenced product delivery."""

import asyncio
import json
from types import SimpleNamespace
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from urllib.parse import urlencode

import pytest
from sqlalchemy import delete, func, select

from crawler.config import Settings
from crawler.harvest_queue import HarvestConsumer, HarvestQueue, HarvestScope, HarvestTask, QueueError, execute_worker
from crawler.route_dependencies import compare_probe_observations, request_key
from crawler.store import Product, Response, ResponseObservation, Run, Store, digest


@pytest.fixture
def setup(tmp_path):
    store = Store(Settings(data_dir=tmp_path, s3_bucket=""))
    queue = HarvestQueue(store)
    with store.session() as session:
        run = Run(id="run", name="Queue fixture", status="running")
        session.add(run)
        session.add_all([Product(id=f"product-{index}", run_id="run", url=f"https://example.test/product{index}",
                                 status="running", epoch=1) for index in range(3)])
    yield store, queue
    store.engine.dispose()


def request(base="123", quantity=50):
    return {"method": "GET", "path": f"/service/rest/v1/products/{base}/prices", "params": {},
            "context": {"origin": "https://example.test", "session_scope": "opaque-session",
                        "source_epoch": "epoch-1", "store_type": "broker", "currency": "USD",
                        "service_signature": "service-scope", "selection": {"quantity": quantity}}}


def manifest(*requests):
    return {"phase": "QUOTE", "discovery_complete": True,
            "requests": [{"phase": "QUOTE", "request": item, "lookup_evidence_id": "lookup-evidence"} for item in requests]}


def response(store, claim, suffix="", *, data=None, path=None, bind=True):
    data = data or {"items": [{"quantity": 50, "markupPrice": "4.20"}]}
    req = claim["request"]
    url = req["context"]["origin"] + (path or req["path"])
    if req["params"]:
        url += "?" + urlencode(req["params"], doseq=True)
    response_id = claim["task_id"] + suffix
    body = json.dumps(data).encode()
    object_path = store.put_object(body, "response")
    with store.session() as session:
        session.add(Response(id=response_id, status=200, url=url, object_path=object_path,
                             semantic_hash=digest(data), request_key=claim["request_key"]))
    if bind:
        store.link_response("run", claim.get("product_id", "product-0"), response_id, "harvest", request_key(req), req["context"])
    return response_id


def expire(store, task_id):
    with store.session() as session:
        session.get(HarvestTask, task_id).lease_until = time.time() - 1


def test_global_manifest_dedup_preflight_and_restart_reuse(setup):
    store, queue = setup
    assert queue.install_manifest("run", "product-0", "scope", manifest(request("1"), request("2")))["new_global_tasks"] == 2
    assert queue.install_manifest("run", "product-1", "scope", manifest(request("2"), request("3")))["new_global_tasks"] == 1
    assert queue.install_manifest("run", "product-0", "scope", manifest(request("1"), request("2")))["new_global_tasks"] == 0
    initial = queue.preflight("run", 2)
    assert initial["active_unique_tasks"] == initial["unique_unfinished_requests"] == 3
    assert initial["remaining_request_seconds_at_measured_rate"] == 1.5
    assert not initial["is_end_to_end_eta"]
    first = queue.claim("run", "worker")
    response_id = response(store, first)
    assert queue.complete(first["task_id"], "worker", first["epoch"], response_id)
    reopened = HarvestQueue(store)
    assert reopened.preflight("run", 2)["succeeded"] == 1
    later = [reopened.claim("run", f"worker-{n}") for n in range(3)]
    assert sum(item is not None for item in later) == 2
    assert first["task_id"] not in {item["task_id"] for item in later if item}
    assert reopened.preflight("run", 2)["unique_unfinished_requests"] == 2
    with pytest.raises(QueueError, match="Immutable scope"):
        queue.install_manifest("run", "product-0", "scope", manifest(request("1")))


def test_parallel_claims_are_unique_and_expired_epoch_fences_old_worker(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(*(request(str(n)) for n in range(12))))
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n: queue.claim("run", f"worker-{n}"), range(20)))
    claims = [item for item in claims if item]
    assert len(claims) == len({item["task_id"] for item in claims}) == 12
    old = claims[0]
    response_id = response(store, old)
    expire(store, old["task_id"])
    replacement = queue.claim("run", "replacement")
    assert replacement["task_id"] == old["task_id"] and replacement["epoch"] > old["epoch"]
    assert not queue.complete(old["task_id"], old["owner"], old["epoch"], response_id)
    assert not queue.heartbeat(old["task_id"], old["owner"], old["epoch"])
    assert queue.complete(replacement["task_id"], "replacement", replacement["epoch"], response_id)


def test_retry_reuses_manifest_and_enforces_delay_and_attempt_budget(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request()), max_attempts=2)
    first = queue.claim("run", "one")
    assert queue.retry(first["task_id"], "one", first["epoch"], "503", delay=100)
    assert queue.claim("run", "early") is None
    with store.session() as session:
        session.get(HarvestTask, first["task_id"]).not_before = 0
    second = queue.claim("run", "two")
    assert second["task_id"] == first["task_id"] and second["attempt"] == 2
    assert queue.retry(second["task_id"], "two", second["epoch"], "503")
    assert queue.claim("run", "three") is None
    result = queue.preflight("run", 2)
    assert result["active_unique_tasks"] == result["failed"] == result["blocked_tasks"] == 1
    assert not result["requests_complete"]
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(HarvestTask)) == 1


def test_expired_leases_cannot_retry_forever(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request()), max_attempts=1)
    first = queue.claim("run", "one")
    expire(store, first["task_id"])
    assert queue.claim("run", "two") is None
    assert queue.preflight("run", 2)["failed"] == 1


def test_pause_stops_claim_delivery_and_ack_but_preserves_completed_response(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request()))
    claim = queue.claim("run", "worker")
    response_id = response(store, claim)
    with store.session() as session:
        session.get(Product, "product-0").status = "paused"
    assert not queue.heartbeat(claim["task_id"], "worker", claim["epoch"])
    assert queue.complete(claim["task_id"], "worker", claim["epoch"], response_id)
    assert queue.ready_responses("run", "product-0", "scope", product_epoch=1) == []
    with store.session() as session:
        product = session.get(Product, "product-0")
        product.status, product.epoch = "running", 2
    assert queue.ready_responses("run", "product-0", "scope", product_epoch=1) == []
    receipt = queue.ready_responses("run", "product-0", "scope", product_epoch=2)[0]
    assert receipt["response_id"] == response_id and receipt["lookup_evidence_ids"] == ["lookup-evidence"]
    with store.session() as session:
        session.get(Run, "run").status = "paused"
    assert queue.claim("run", "worker") is None
    paused = queue.preflight("run", 2)
    assert paused["manifest_unique_tasks"] == paused["suspended_unique_tasks"] == 1
    assert not paused["requests_complete"]
    assert not queue.acknowledge(receipt, result_reference="durable-batch")
    assert queue.ready_responses("run", "product-0", "scope", product_epoch=2) == []
    with store.session() as session:
        session.get(Run, "run").status = "running"
    assert queue.acknowledge(receipt, result_reference="durable-batch")
    assert queue.acknowledge(receipt, result_reference="durable-batch")
    assert not queue.acknowledge(receipt, result_reference="different-batch")
    assert queue.ready_responses("run", "product-0", "scope", product_epoch=2) == []


def test_old_scope_receipt_is_fenced_and_only_current_scope_claims(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "old", manifest(request()))
    claim = queue.claim("run", "worker")
    response_id = response(store, claim)
    assert queue.complete(claim["task_id"], "worker", claim["epoch"], response_id)
    old_receipt = queue.ready_responses("run", "product-0", "old", product_epoch=1)[0]
    queue.install_manifest("run", "product-0", "new", manifest(request("456")))
    assert queue.ready_responses("run", "product-0", "old", product_epoch=1) == []
    assert not queue.acknowledge(old_receipt, result_reference="stale-batch")
    assert queue.preflight("run", 2)["active_unique_tasks"] == 1
    queue.set_scope_active("run", "product-0", "old", True)
    assert not queue.acknowledge(old_receipt, result_reference="stale-batch")
    assert queue.preflight("run", 2)["succeeded"] == 1
    assert queue.claim("run", "worker") is None


def test_certificate_dedup_is_finite_and_changed_content_cannot_publish(setup):
    store, queue = setup
    data = {"items": [{"quantity": 50, "markupPrice": "4.20"}]}
    observations = [{"request": request(quantity=n), "status": 200, "complete": True,
                     "response_id": str(n), "data": data} for n in (50, 100)]
    certificate = compare_probe_observations(observations, "context.selection.quantity")["certificate"]
    result = queue.install_manifest("run", "product-0", "scope", manifest(request(quantity=50), request(quantity=100), request(quantity=250)), certificates=[certificate])
    assert result["unique_manifest_tasks"] == 2
    with store.session() as session:
        task = session.scalar(select(HarvestTask).where(HarvestTask.expected_semantic_hash.is_not(None)))
        task_id = task.id
    claims = [queue.claim("run", "worker") for _ in range(2)]
    claim = next(item for item in claims if item["task_id"] == task_id)
    changed_id = response(store, claim, data={"items": [{"quantity": 50, "markupPrice": "99.00"}]})
    with pytest.raises(QueueError, match="certificate"):
        queue.complete(task_id, "worker", claim["epoch"], changed_id)
    valid_id = response(store, claim, "valid", data=data)
    assert queue.complete(task_id, "worker", claim["epoch"], valid_id)
    receipt = queue.ready_responses("run", "product-0", "scope", product_epoch=1)[0]
    assert {m["request"]["context"]["selection"]["quantity"] for m in receipt["requested_members"]} == {50, 100}
    with pytest.raises(QueueError, match="Immutable scope"):
        queue.install_manifest("run", "product-0", "scope", manifest(request(quantity=50), request(quantity=250)), certificates=[certificate])


def test_unproven_context_difference_stays_separate(setup):
    _, queue = setup
    first = request()
    second = deepcopy(first)
    second["context"]["unproven_pricing_class"] = "class2"
    queue.install_manifest("run", "product-0", "scope", manifest(first, second))
    assert queue.preflight("run", 2)["active_unique_tasks"] == 2


def test_completion_requires_correct_immutable_response_and_is_idempotent(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request()))
    claim = queue.claim("run", "worker")
    with pytest.raises(QueueError, match="persisted"):
        queue.complete(claim["task_id"], "worker", claim["epoch"], "missing")
    wrong = response(store, claim, "wrong", path="/service/rest/v1/products/999/prices")
    with pytest.raises(QueueError, match="URL"):
        queue.complete(claim["task_id"], "worker", claim["epoch"], wrong)
    good = response(store, claim, "good")
    assert queue.complete(claim["task_id"], "worker", claim["epoch"], good)
    assert queue.complete(claim["task_id"], "worker", claim["epoch"], good)
    assert not queue.complete(claim["task_id"], "worker", claim["epoch"], wrong)
    # Consumer crash before acknowledgment rereads the saved response, not the source.
    assert queue.ready_responses("run", "product-0", "scope", product_epoch=1)[0]["response_id"] == good
    assert queue.claim("run", "worker") is None


def test_discovery_and_lookup_gates_are_mandatory(setup):
    _, queue = setup
    incomplete = manifest(request())
    incomplete["discovery_complete"] = False
    with pytest.raises(QueueError, match="complete discovery"):
        queue.install_manifest("run", "product-0", "scope", incomplete)
    bad = manifest(request())
    bad["requests"][0].pop("lookup_evidence_id")
    with pytest.raises(QueueError, match="lookup"):
        queue.install_manifest("run", "product-0", "scope", bad)
    for rate in (0, -1, float("nan"), float("inf")):
        with pytest.raises(QueueError):
            queue.preflight("run", rate)


def test_url_only_response_cannot_be_injected_without_context_binding(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request()))
    claim = queue.claim("run", "worker")
    unbound = response(store, claim, bind=False)
    with pytest.raises(QueueError, match="full-context"):
        queue.complete(claim["task_id"], "worker", claim["epoch"], unbound)
    other = deepcopy(claim["request"])
    other["context"]["service_signature"] = "other-pricing"
    store.link_response("run", "product-0", unbound, "harvest", request_key(other), other["context"])
    with pytest.raises(QueueError, match="full-context"):
        queue.complete(claim["task_id"], "worker", claim["epoch"], unbound)


@pytest.mark.asyncio
async def test_bounded_worker_fetches_full_context_checkpoints_and_closes(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request("1"), request("2")))
    calls, closed = [], []
    class Transport:
        def __init__(self, claim):
            self.claim = claim
        async def fetch(self, url, **kwargs):
            calls.append((url, kwargs))
            rid = response(store, self.claim, bind=False)
            return SimpleNamespace(response_id=rid, data={}, status=200)
        async def close(self):
            closed.append(self.claim["task_id"])
    first = await execute_worker(queue, "run", "worker", Transport, max_tasks=1)
    assert first["succeeded"] == first["transport_invocations"] == 1
    assert not first["catalog_complete"] and len(calls) == len(closed) == 1
    assert calls[0][1]["context"] == request()["context"]
    assert queue.preflight("run", 2)["pending"] == 1
    second = await execute_worker(HarvestQueue(store), "run", "new-worker", Transport, max_tasks=10)
    assert second["succeeded"] == 1 and len(calls) == 2
    third = await execute_worker(queue, "run", "again", Transport, max_tasks=10)
    assert third["claimed"] == 0 and len(calls) == 2
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(ResponseObservation).where(ResponseObservation.kind == "harvest")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [("source_unavailable", "retried"), ("access_required", "failed")])
async def test_worker_retry_codes_do_not_expand_manifest(setup, code, expected):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request()))
    class SourceError(Exception):
        pass
    class Transport:
        async def fetch(self, *args, **kwargs):
            error = SourceError("Source failure")
            error.code = code
            raise error
    result = await execute_worker(queue, "run", "worker", lambda claim: Transport(), max_tasks=3)
    assert result[expected] == result["claimed"] == 1
    with store.session() as session:
        assert session.scalar(select(func.count()).select_from(HarvestTask)) == 1
        task = session.scalar(select(HarvestTask))
        assert task.attempts == 1
        assert task.status == ("pending" if expected == "retried" else "failed")


@pytest.mark.asyncio
async def test_worker_renews_lease_during_slow_fetch(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request()))
    class Transport:
        def __init__(self, claim):
            self.claim = claim
        async def fetch(self, *args, **kwargs):
            await asyncio.sleep(0.18)
            return SimpleNamespace(response_id=response(store, self.claim, bind=False))
    result = await execute_worker(queue, "run", "worker", Transport, lease_seconds=0.12)
    assert result["succeeded"] == 1


@pytest.mark.asyncio
async def test_worker_cancels_fetch_when_consuming_product_pauses(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request()))
    cancelled = asyncio.Event()
    class Transport:
        async def fetch(self, *args, **kwargs):
            with store.session() as session:
                session.get(Product, "product-0").status = "paused"
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
    result = await execute_worker(queue, "run", "worker", lambda claim: Transport(), lease_seconds=0.3)
    assert cancelled.is_set() and result["succeeded"] == 0
    with store.session() as session:
        assert session.scalar(select(HarvestTask)).response_id is None
        assert session.scalar(select(func.count()).select_from(Response)) == 0


@pytest.mark.asyncio
async def test_explicit_worker_cancel_leaves_no_false_completion(setup):
    store, queue = setup
    queue.install_manifest("run", "product-0", "scope", manifest(request()))
    started, stopped = asyncio.Event(), asyncio.Event()
    class Transport:
        async def fetch(self, *args, **kwargs):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()
    task = asyncio.create_task(execute_worker(queue, "run", "worker", lambda claim: Transport()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    with store.session() as session:
        row = session.scalar(select(HarvestTask))
        assert row.status == "pending" and row.response_id is None
