"""Durable per-run request manifest queue; no network or application side effects.

Response objects must be durable before completion. Consumers may create rows only
through the engine's existing product-epoch fence, then acknowledge delivery. A
crash between row commit and acknowledgement can repeat consumption, never the
successful source request; semantic row dedup remains the engine's responsibility.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from contextlib import AsyncExitStack
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import Boolean, Column, Float, Integer, JSON, String, Text, func, select

from .route_dependencies import finite_equivalence_key, request_identity, request_key
from .store import Base, Product, Response, ResponseObservation, Run, digest


class QueueError(ValueError):
    pass


class HarvestScope(Base):
    __tablename__ = "harvest_scopes"
    id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    product_id = Column(String, nullable=False, index=True)
    scope = Column(String, nullable=False)
    manifest_hash = Column(String, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    epoch = Column(Integer, nullable=False, default=1)


class HarvestTask(Base):
    __tablename__ = "harvest_tasks"
    id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    request_key = Column(String, nullable=False, index=True)
    request = Column(JSON, nullable=False)
    expected_semantic_hash = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending", index=True)
    owner = Column(String, nullable=False, default="")
    epoch = Column(Integer, nullable=False, default=0)
    lease_until = Column(Float, nullable=False, default=0)
    not_before = Column(Float, nullable=False, default=0)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    response_id = Column(String, nullable=True)
    error = Column(Text, nullable=True)


class HarvestConsumer(Base):
    __tablename__ = "harvest_consumers"
    id = Column(String, primary_key=True)
    task_id = Column(String, nullable=False, index=True)
    scope_id = Column(String, nullable=False, index=True)
    product_id = Column(String, nullable=False, index=True)
    lookup_evidence_ids = Column(JSON, nullable=False, default=list)
    requested_members = Column(JSON, nullable=False, default=list)
    delivered = Column(Boolean, nullable=False, default=False)
    result_reference = Column(String, nullable=True)


def ensure_tables(engine):
    Base.metadata.create_all(engine, tables=[HarvestScope.__table__, HarvestTask.__table__, HarvestConsumer.__table__])


class HarvestQueue:
    def __init__(self, store):
        self.store = store
        ensure_tables(store.engine)

    def _run(self, session, run_id):
        run = session.get(Run, run_id, with_for_update=not self.store.sqlite)
        if run is None:
            raise QueueError("Run does not exist")
        return run

    @staticmethod
    def _active_consumers(run_id):
        return (select(HarvestConsumer.task_id)
                .join(HarvestScope, HarvestScope.id == HarvestConsumer.scope_id)
                .join(Product, Product.id == HarvestScope.product_id)
                .join(Run, Run.id == HarvestScope.run_id)
                .where(HarvestScope.run_id == run_id, HarvestScope.active.is_(True),
                       Product.run_id == run_id, Product.status.in_(["queued", "running"]),
                       Run.status.in_(["queued", "running"])))

    def install_manifest(self, run_id, product_id, scope, manifest, *, certificates=(), max_attempts=3):
        if not scope or manifest.get("phase") != "QUOTE" or manifest.get("discovery_complete") is not True:
            raise QueueError("Only a scoped quote manifest after complete discovery may enter the queue")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise QueueError("max_attempts must be a positive integer")
        prepared = {}
        for entry in manifest.get("requests", []):
            if entry.get("phase") != "QUOTE" or not entry.get("lookup_evidence_id"):
                raise QueueError("Every quote task needs a validated lookup evidence reference")
            request = request_identity(entry["request"])
            original_key = request_key(request)
            matching = [certificate for certificate in certificates
                        if original_key in certificate.get("verified_members", ())]
            # A single exact certificate may alias a tested member; no composition.
            certificate = min(matching, key=lambda c: c["id"]) if matching else None
            key = finite_equivalence_key(request, certificate)
            expected = certificate.get("response_hash") if certificate else None
            if key in prepared and prepared[key]["expected"] != expected:
                raise QueueError("Conflicting finite dependency certificates")
            prior_evidence = prepared.get(key, {}).get("lookup_evidence_ids", [])
            members = prepared.get(key, {}).get("members", {})
            members[original_key] = {"request_key": original_key, "request": request,
                                     "lookup_evidence_id": entry["lookup_evidence_id"]}
            prepared[key] = {"request": request, "expected": expected, "members": members,
                             "lookup_evidence_ids": sorted(set(prior_evidence + [entry["lookup_evidence_id"]]))}
        manifest_hash = digest(sorted((key, sorted(value["members"])) for key, value in prepared.items()))
        scope_id = digest([run_id, product_id, scope])
        with self.store.session(immediate=True) as session:
            self._run(session, run_id)
            product = session.get(Product, product_id)
            if product is None or product.run_id != run_id:
                raise QueueError("Product does not belong to this run")
            existing = session.get(HarvestScope, scope_id)
            if existing and existing.manifest_hash != manifest_hash:
                raise QueueError("Immutable scope manifest changed; create a new scope")
            for prior in session.scalars(select(HarvestScope).where(HarvestScope.run_id == run_id, HarvestScope.product_id == product_id)):
                if prior.id != scope_id and prior.active:
                    prior.active, prior.epoch = False, prior.epoch + 1
            if existing is None:
                existing = HarvestScope(id=scope_id, run_id=run_id, product_id=product_id, scope=scope,
                                        manifest_hash=manifest_hash, active=True, epoch=1)
                session.add(existing)
            elif not existing.active:
                existing.active, existing.epoch = True, existing.epoch + 1
            created = 0
            for key, value in prepared.items():
                task_id = digest([run_id, key])
                task = session.get(HarvestTask, task_id)
                if task is None:
                    task = HarvestTask(id=task_id, run_id=run_id, request_key=key, request=value["request"],
                                       expected_semantic_hash=value["expected"], max_attempts=max_attempts)
                    session.add(task)
                    created += 1
                elif task.expected_semantic_hash != value["expected"]:
                    raise QueueError("Existing task uses a different equivalence certificate")
                consumer_id = digest([scope_id, task_id])
                if session.get(HarvestConsumer, consumer_id) is None:
                    session.add(HarvestConsumer(id=consumer_id, task_id=task_id, scope_id=scope_id, product_id=product_id,
                                                lookup_evidence_ids=value["lookup_evidence_ids"],
                                                requested_members=list(value["members"].values())))
            return {"scope_id": scope_id, "scope_epoch": existing.epoch, "unique_manifest_tasks": len(prepared),
                    "new_global_tasks": created}

    def set_scope_active(self, run_id, product_id, scope, active):
        with self.store.session(immediate=True) as session:
            self._run(session, run_id)
            row = session.get(HarvestScope, digest([run_id, product_id, scope]))
            if row is None:
                raise QueueError("Scope does not exist")
            if active:
                for prior in session.scalars(select(HarvestScope).where(HarvestScope.run_id == run_id, HarvestScope.product_id == product_id)):
                    if prior.id != row.id and prior.active:
                        prior.active, prior.epoch = False, prior.epoch + 1
            if row.active != bool(active):
                row.active, row.epoch = bool(active), row.epoch + 1
            return row.epoch

    def preflight(self, run_id, measured_rps):
        if not isinstance(measured_rps, (int, float)) or not math.isfinite(measured_rps) or measured_rps <= 0:
            raise QueueError("A positive finite measured request rate is required")
        with self.store.session() as session:
            active_scope_count = session.scalar(select(func.count(HarvestScope.id))
                .join(Product, Product.id == HarvestScope.product_id).join(Run, Run.id == HarvestScope.run_id)
                .where(HarvestScope.run_id == run_id, HarvestScope.active.is_(True),
                       Product.status.in_(["queued", "running"]), Run.status.in_(["queued", "running"])))
            manifest_tasks = list(session.scalars(select(HarvestTask).where(HarvestTask.run_id == run_id,
                HarvestTask.id.in_(select(HarvestConsumer.task_id).join(HarvestScope, HarvestScope.id == HarvestConsumer.scope_id)
                    .where(HarvestScope.run_id == run_id, HarvestScope.active.is_(True))))))
            tasks = list(session.scalars(select(HarvestTask).where(HarvestTask.run_id == run_id,
                                         HarvestTask.id.in_(self._active_consumers(run_id)))))
            counts = {status: sum(t.status == status for t in tasks) for status in ("pending", "leased", "succeeded", "failed")}
            remaining = counts["pending"] + counts["leased"]
            manifest_remaining = sum(t.status in {"pending", "leased"} for t in manifest_tasks)
            return {"active_unique_tasks": len(tasks), "active_scopes": active_scope_count,
                    "manifest_unique_tasks": len(manifest_tasks),
                    "manifest_unique_unfinished_requests": manifest_remaining,
                    "manifest_request_seconds_at_measured_rate": manifest_remaining / measured_rps,
                    "suspended_unique_tasks": len(manifest_tasks) - len(tasks),
                    **counts, "unique_unfinished_requests": remaining,
                    "remaining_request_seconds_at_measured_rate": remaining / measured_rps,
                    "measured_requests_per_second": measured_rps,
                    "is_end_to_end_eta": False, "blocked_tasks": counts["failed"],
                    "requests_complete": active_scope_count > 0 and remaining == 0 and counts["failed"] == 0,
                    "eta_limits": "Excludes future retries, retry waits, processing, export and undiscovered work; in-flight work counted once"}

    def claim(self, run_id, owner, *, lease_seconds=60):
        if not owner or lease_seconds <= 0 or not math.isfinite(lease_seconds):
            raise QueueError("A worker owner and positive finite lease are required")
        with self.store.session(immediate=True) as session:
            run = self._run(session, run_id)
            if run.status not in {"queued", "running"}:
                return None
            now = self.store.now(session)
            eligible = (select(HarvestTask).where(HarvestTask.run_id == run_id,
                        HarvestTask.id.in_(self._active_consumers(run_id)),
                        ((HarvestTask.status == "pending") & (HarvestTask.not_before <= now)) |
                        ((HarvestTask.status == "leased") & (HarvestTask.lease_until <= now)))
                        .order_by(HarvestTask.id))
            for task in session.scalars(eligible):
                if task.attempts >= task.max_attempts:
                    task.status, task.error = "failed", "Request attempt limit reached"
                    task.epoch += 1
                    continue
                task.status, task.owner = "leased", owner
                task.epoch += 1
                task.attempts += 1
                task.lease_until = now + lease_seconds
                attribution = session.scalar(select(HarvestConsumer.product_id)
                    .join(HarvestScope, HarvestScope.id == HarvestConsumer.scope_id)
                    .join(Product, Product.id == HarvestConsumer.product_id)
                    .where(HarvestConsumer.task_id == task.id, HarvestScope.active.is_(True),
                           Product.status.in_(["queued", "running"]))
                    .order_by(HarvestConsumer.product_id).limit(1))
                return {"task_id": task.id, "owner": owner, "epoch": task.epoch,
                        "run_id": run_id, "product_id": attribution, "max_attempts": task.max_attempts,
                        "request": task.request, "request_key": task.request_key,
                        "attempt": task.attempts, "lease_until": task.lease_until}
            return None

    def _leased(self, session, task_id, owner, epoch):
        initial = session.get(HarvestTask, task_id)
        if initial is None:
            return None
        self._run(session, initial.run_id)
        task = session.get(HarvestTask, task_id, populate_existing=True)
        if task.status != "leased" or task.owner != owner or task.epoch != epoch or task.lease_until <= self.store.now(session):
            return None
        return task

    def heartbeat(self, task_id, owner, epoch, *, lease_seconds=60):
        if lease_seconds <= 0 or not math.isfinite(lease_seconds):
            raise QueueError("Positive finite lease required")
        with self.store.session(immediate=True) as session:
            task = self._leased(session, task_id, owner, epoch)
            if task is None:
                return False
            if not session.scalar(select(HarvestTask.id).where(HarvestTask.id == task.id,
                                   HarvestTask.id.in_(self._active_consumers(task.run_id)))):
                return False
            task.lease_until = self.store.now(session) + lease_seconds
            return True

    def complete(self, task_id, owner, epoch, response_id):
        with self.store.session(immediate=True) as session:
            initial = session.get(HarvestTask, task_id)
            if initial and initial.status == "succeeded":
                return initial.owner == owner and initial.epoch == epoch and initial.response_id == response_id
            task = self._leased(session, task_id, owner, epoch)
            if task is None:
                return False
            response = session.get(Response, response_id)
            if response is None or response.status != 200:
                raise QueueError("Successful immutable response must be persisted before task completion")
            binding = session.scalar(select(ResponseObservation.id).where(
                ResponseObservation.run_id == task.run_id, ResponseObservation.response_id == response_id,
                ResponseObservation.kind == "harvest",
                ResponseObservation.request_signature == request_key(task.request)).limit(1))
            if binding is None:
                raise QueueError("Response lacks durable full-context harvest request binding")
            url = urlsplit(response.url or "")
            expected_query = {str(k): [str(item) for item in (v if isinstance(v, list) else [v])]
                              for k, v in task.request["params"].items()}
            origin = task.request["context"]["origin"].rstrip("/")
            if (f"{url.scheme}://{url.netloc}" != origin or url.path != task.request["path"]
                    or parse_qs(url.query, keep_blank_values=True) != expected_query):
                raise QueueError("Response URL is not the leased request")
            if task.expected_semantic_hash and response.semantic_hash != task.expected_semantic_hash:
                raise QueueError("Response changed from the finite dependency certificate; rediscovery required")
            task.status, task.response_id, task.lease_until, task.error = "succeeded", response_id, 0, None
            return True

    def retry(self, task_id, owner, epoch, error, *, delay=0, retryable=True):
        if delay < 0 or not math.isfinite(delay):
            raise QueueError("Retry delay must be finite and nonnegative")
        with self.store.session(immediate=True) as session:
            task = self._leased(session, task_id, owner, epoch)
            if task is None:
                return False
            task.status = "pending" if retryable and task.attempts < task.max_attempts else "failed"
            task.error, task.not_before, task.lease_until = str(error), self.store.now(session) + delay, 0
            task.owner = ""
            task.epoch += 1
            return True

    def ready_responses(self, run_id, product_id, scope, *, product_epoch, limit=100):
        with self.store.session() as session:
            product = session.get(Product, product_id)
            run = session.get(Run, run_id)
            active = session.get(HarvestScope, digest([run_id, product_id, scope]))
            if not (run and run.status in {"queued", "running"} and product and product.run_id == run_id
                    and product.status == "running" and product.epoch == product_epoch and active and active.active):
                return []
            rows = session.execute(select(HarvestConsumer, HarvestTask).join(HarvestTask, HarvestTask.id == HarvestConsumer.task_id)
                                   .where(HarvestConsumer.scope_id == active.id, HarvestConsumer.delivered.is_(False),
                                          HarvestTask.status == "succeeded").order_by(HarvestTask.id).limit(limit))
            return [{"consumer_id": consumer.id, "task_id": task.id, "response_id": task.response_id,
                     "scope_id": active.id, "scope_epoch": active.epoch, "product_epoch": product_epoch,
                     "request": task.request, "lookup_evidence_ids": consumer.lookup_evidence_ids,
                     "requested_members": consumer.requested_members} for consumer, task in rows]

    def acknowledge(self, receipt, *, result_reference):
        """Acknowledge only after durable downstream output; never use as a row-write bypass."""
        if not result_reference:
            raise QueueError("Durable downstream result reference required")
        with self.store.session(immediate=True) as session:
            consumer = session.get(HarvestConsumer, receipt["consumer_id"])
            if consumer is None:
                return False
            scope = session.get(HarvestScope, consumer.scope_id)
            run = self._run(session, scope.run_id)
            product = session.get(Product, consumer.product_id, with_for_update=not self.store.sqlite)
            task = session.get(HarvestTask, consumer.task_id)
            if not (run.status in {"queued", "running"} and product and product.status == "running"
                    and product.epoch == receipt["product_epoch"] and scope.active
                    and scope.id == receipt["scope_id"] and scope.epoch == receipt["scope_epoch"]
                    and task.id == receipt["task_id"] and task.status == "succeeded"
                    and task.response_id == receipt["response_id"]):
                return False
            if consumer.delivered:
                return consumer.result_reference == result_reference
            consumer.delivered, consumer.result_reference = True, result_reference
            return True


class _LeaseLost(Exception):
    code = "interrupted"


async def execute_worker(queue, run_id, owner, transport_factory, *, max_tasks=1, lease_seconds=60):
    """Execute at most max_tasks leased Transport.fetch invocations, then return.

    transport_factory(claim) returns a transport, awaitable transport, or async
    context manager. Bare transports are closed after each invocation; a context
    manager controls its own lifetime. The existing transport owns pacing, HTTP
    retries, authentication, validation and immutable response persistence. Queue
    attempts count these logical invocations, not lower-level network attempts.
    This helper fetches tables only; it never creates rows or declares catalog
    completeness. Consumer output remains behind the engine product epoch fence.
    """
    if not isinstance(max_tasks, int) or isinstance(max_tasks, bool) or max_tasks < 1:
        raise QueueError("max_tasks must be a positive bounded integer")
    result = {"claimed": 0, "succeeded": 0, "retried": 0, "failed": 0, "fenced": 0,
              "transport_invocations": 0, "response_ids": [], "catalog_complete": False}
    retryable_codes = {"browser_network_error", "network_error", "retry_exhausted",
                       "rate_limited", "source_unavailable"}
    for _ in range(max_tasks):
        claim = await asyncio.to_thread(queue.claim, run_id, owner, lease_seconds=lease_seconds)
        if claim is None:
            break
        result["claimed"] += 1

        async def fetch_one():
            resource = transport_factory(claim)
            if inspect.isawaitable(resource):
                resource = await resource
            async with AsyncExitStack() as stack:
                if hasattr(resource, "__aenter__"):
                    transport = await stack.enter_async_context(resource)
                else:
                    transport = resource
                    if hasattr(transport, "close"):
                        async def close():
                            closed = transport.close()
                            if inspect.isawaitable(closed):
                                await closed
                        stack.push_async_callback(close)
                request = claim["request"]
                result["transport_invocations"] += 1
                return await transport.fetch(request["context"]["origin"].rstrip("/") + request["path"],
                    method=request["method"], params=request["params"], json=request["body"],
                    context=request["context"])

        async def renew():
            while True:
                await asyncio.sleep(min(15, lease_seconds / 3))
                valid = await asyncio.to_thread(queue.heartbeat, claim["task_id"], owner, claim["epoch"],
                                                lease_seconds=lease_seconds)
                if not valid:
                    raise _LeaseLost("Request lease or active consumer scope changed")

        fetch_task, renew_task = asyncio.create_task(fetch_one()), asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait((fetch_task, renew_task), return_when=asyncio.FIRST_COMPLETED)
            if renew_task in done:
                await renew_task
            fetched = await fetch_task
            await asyncio.to_thread(queue.store.link_response, run_id, claim["product_id"], fetched.response_id,
                                    "harvest", request_key(claim["request"]), claim["request"]["context"])
            completed = await asyncio.to_thread(queue.complete, claim["task_id"], owner, claim["epoch"], fetched.response_id)
            if completed:
                result["succeeded"] += 1
                result["response_ids"].append(fetched.response_id)
            else:
                result["fenced"] += 1
        except asyncio.CancelledError:
            fetch_task.cancel()
            await asyncio.gather(fetch_task, return_exceptions=True)
            await asyncio.to_thread(queue.retry, claim["task_id"], owner, claim["epoch"],
                                    "interrupted: worker cancelled", delay=0, retryable=True)
            raise
        except Exception as exc:
            if not fetch_task.done():
                fetch_task.cancel()
                await asyncio.gather(fetch_task, return_exceptions=True)
            code = getattr(exc, "code", "queue_integrity_error" if isinstance(exc, QueueError) else "worker_error")
            can_retry = code in retryable_codes or code == "interrupted"
            delay = 0 if code == "interrupted" else min(60, 5 * (2 ** (claim["attempt"] - 1)))
            saved = await asyncio.to_thread(queue.retry, claim["task_id"], owner, claim["epoch"],
                                           f"{code}: {type(exc).__name__}", delay=delay, retryable=can_retry)
            result["fenced" if not saved else "retried" if can_retry and claim["attempt"] < claim["max_attempts"] else "failed"] += 1
        finally:
            for task in (fetch_task, renew_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(fetch_task, renew_task, return_exceptions=True)
    return result
