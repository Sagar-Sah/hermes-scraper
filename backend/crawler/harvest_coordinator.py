"""Explicitly started shared harvest workers with durable preflight gating.

Construction, discovery recording, manifest installation and status reads do not
call the source. The caller supplies the ordinary authenticated Transport factory;
its shared limiter remains authoritative. This coordinator fetches evidence, never
claims full catalog coverage or bypasses the engine's row-commit fences.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
import uuid

from sqlalchemy import Column, Float, JSON, String, select

from .harvest_queue import HarvestQueue, HarvestScope, HarvestTask, QueueError, execute_worker
from .route_dependencies import request_identity, request_key
from .store import Base, Event, Product, Run, digest


class HarvestPlanState(Base):
    __tablename__ = "harvest_plan_states"
    id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    product_id = Column(String, nullable=False, index=True)
    scope = Column(String, nullable=True)
    discovery = Column(JSON, nullable=False, default=dict)
    updated_at = Column(Float, nullable=False, default=time.time)


class HarvestCoordinatorState(Base):
    __tablename__ = "harvest_coordinator_states"
    run_id = Column(String, primary_key=True)
    status = Column(String, nullable=False, default="not_started")
    measured_rps = Column(Float, nullable=True)
    preflight = Column(JSON, nullable=False, default=dict)
    updated_at = Column(Float, nullable=False, default=time.time)


def ensure_tables(engine):
    Base.metadata.create_all(engine, tables=[HarvestPlanState.__table__, HarvestCoordinatorState.__table__])


def _discovery_summary(value, *, complete=None):
    summary = dict(value or {})
    aliases = {"candidate_lookup_count": "candidate_count", "unresolved_candidates": "unresolved_count"}
    for source, target in aliases.items():
        if source in summary and target not in summary:
            summary[target] = summary[source]
    if complete is not None:
        if summary.get("discovery_complete") is False and complete:
            raise QueueError("Unfinished discovery cannot install a quote manifest")
        summary["discovery_complete"] = bool(complete)
    else:
        summary["discovery_complete"] = summary.get("discovery_complete") is True
    for field in ("candidate_count", "resolved_count", "unresolved_count", "validated_base_count"):
        value = summary.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise QueueError(f"{field} must be a nonnegative integer or unknown")
    if summary["discovery_complete"] and summary.get("unresolved_count", 0):
        raise QueueError("Unresolved candidates cannot be declared discovery-complete")
    if summary["discovery_complete"]:
        summary["unresolved_count"] = 0
    if summary.get("candidate_count") is not None and summary.get("resolved_count") is not None:
        if summary["resolved_count"] > summary["candidate_count"]:
            raise QueueError("Resolved candidates exceed the declared candidate domain")
        if summary["discovery_complete"] and summary["resolved_count"] != summary["candidate_count"]:
            raise QueueError("Declared complete discovery has unresolved candidate counts")
    # Persist only bounded progress fields, not arbitrary credentials or large bodies.
    return {key: summary.get(key) for key in ("candidate_count", "resolved_count", "unresolved_count", "validated_base_count", "discovery_complete")}


def normalize_quote_manifest(value):
    """Normalize nested/flat quote operations without inferring lookup validity."""
    if not isinstance(value, dict) or value.get("phase") != "QUOTE" or value.get("discovery_complete") is not True:
        raise QueueError("An explicitly completed QUOTE manifest is required")
    if value.get("accepted") is False:
        raise QueueError("Manifest was not accepted")
    _discovery_summary(value, complete=True)
    entries = value.get("requests")
    if not isinstance(entries, list):
        raise QueueError("Quote manifest requests must be an explicit list")
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("phase", "QUOTE") != "QUOTE" or not entry.get("lookup_evidence_id"):
            raise QueueError("Every quote operation requires validated lookup evidence")
        request = request_identity(entry.get("request", entry))
        if request["method"] != "GET" or not re.fullmatch(r"/service/rest/v1/products/\d+(?:/options/\d+)?/prices", request["path"]):
            raise QueueError("Only explicit source price-table GET operations are supported")
        normalized.append({"phase": "QUOTE", "request": request, "id": request_key(request),
                           "lookup_evidence_id": entry["lookup_evidence_id"], "status": "unobserved"})
    return {"phase": "QUOTE", "discovery_complete": True, "requests": normalized}


def read_status(queue, run_id, *, measured_rps=None):
    """Read installed request progress alongside discovered/unresolved candidate counts."""
    store = queue.store
    with store.session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise QueueError("Run does not exist")
        state = session.get(HarvestCoordinatorState, run_id)
        plans = {row.product_id: row for row in session.scalars(select(HarvestPlanState).where(HarvestPlanState.run_id == run_id))}
        products = []
        for product in session.scalars(select(Product).where(Product.run_id == run_id).order_by(Product.id)):
            plan = plans.get(product.id)
            scope = session.get(HarvestScope, digest([run_id, product.id, plan.scope])) if plan and plan.scope else None
            products.append({"product_id": product.id, "product_status": product.status,
                             "scope": plan.scope if plan else None,
                             "manifest_installed": bool(scope and scope.active),
                             "discovery": plan.discovery if plan else _discovery_summary({})})
        rate = measured_rps if measured_rps is not None else state.measured_rps if state else None
        coordinator_status = state.status if state else "not_started"
        run_status = run.status
    progress = queue.preflight(run_id, rate if rate is not None else 1)
    if rate is None:
        progress["measured_requests_per_second"] = None
        progress["remaining_request_seconds_at_measured_rate"] = None
        progress["manifest_request_seconds_at_measured_rate"] = None
    unresolved = [p["product_id"] for p in products if not p["discovery"].get("discovery_complete")]
    missing = [p["product_id"] for p in products if not p["manifest_installed"]]
    discovery_counts = {}
    for field in ("candidate_count", "resolved_count", "unresolved_count", "validated_base_count"):
        discovery_counts[field] = {"known_total": sum(p["discovery"].get(field) or 0 for p in products),
                                   "unknown_product_ids": [p["product_id"] for p in products if p["discovery"].get(field) is None]}
    return {"run_id": run_id, "run_status": run_status, "coordinator_status": coordinator_status,
            "discovery_counts": discovery_counts,
            "products": products, "discovery_unresolved_product_ids": unresolved,
            "missing_manifest_product_ids": missing, "requests": progress,
            "catalog_complete": False, "composition_complete": False,
            "coverage_note": "Request completion alone does not establish valid configuration, service, shipping, composition or export completeness"}


class HarvestCoordinator:
    def __init__(self, store, transport_factory, *, workers=2, publish=None, poll_seconds=0.25, lease_seconds=60):
        if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
            raise QueueError("A positive shared worker count is required")
        if not math.isfinite(poll_seconds) or poll_seconds <= 0 or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise QueueError("Positive finite polling and lease intervals required")
        self.store, self.transport_factory, self.publish = store, transport_factory, publish
        self.queue = HarvestQueue(store)
        ensure_tables(store.engine)
        self.workers, self.poll_seconds, self.lease_seconds = workers, poll_seconds, lease_seconds
        self.owner = uuid.uuid4().hex
        self._enabled, self._tasks, self._inflight = set(), [], {}
        self._locks, self._idle_checks = {}, {}
        self._round_robin = 0
        self._wake = asyncio.Event()
        self._stopping = False

    def _lock(self, run_id):
        return self._locks.setdefault(run_id, asyncio.Lock())

    async def _event(self, run_id, kind, payload, *, persisted=False):
        if not persisted:
            await asyncio.to_thread(self.store.event, run_id, kind, payload)
        if self.publish:
            published = self.publish(run_id, kind, payload)
            if inspect.isawaitable(published):
                await published

    def _save_state(self, run_id, status, *, measured_rps=None, preflight=None, event=None):
        with self.store.session(immediate=True) as session:
            if session.get(Run, run_id) is None:
                raise QueueError("Run does not exist")
            row = session.get(HarvestCoordinatorState, run_id)
            if row is None:
                row = HarvestCoordinatorState(run_id=run_id)
                session.add(row)
            row.status, row.updated_at = status, self.store.now(session)
            if measured_rps is not None:
                row.measured_rps = measured_rps
            if preflight is not None:
                row.preflight = preflight
            if event:
                session.add(Event(run_id=run_id, type=event[0], payload=event[1]))

    async def record_discovery(self, run_id, product_id, summary):
        async with self._lock(run_id):
            if run_id in self._enabled:
                raise QueueError("Pause the coordinator before changing discovery scope")
            normalized = _discovery_summary(summary)
            await asyncio.to_thread(self._save_plan, run_id, product_id, normalized, None)
            return normalized

    def _save_plan(self, run_id, product_id, discovery, scope):
        with self.store.session(immediate=True) as session:
            product = session.get(Product, product_id)
            if not product or product.run_id != run_id:
                raise QueueError("Product does not belong to this run")
            plan_id = digest([run_id, product_id])
            plan = session.get(HarvestPlanState, plan_id)
            if plan is None:
                plan = HarvestPlanState(id=plan_id, run_id=run_id, product_id=product_id)
                session.add(plan)
            plan.discovery, plan.updated_at = discovery, self.store.now(session)
            plan.scope = scope

    async def install(self, run_id, product_id, scope, manifest, *, discovery_summary=None, certificates=(), max_attempts=3):
        async with self._lock(run_id):
            if run_id in self._enabled:
                raise QueueError("Pause the coordinator before replacing a manifest")
            normalized = normalize_quote_manifest(manifest)
            discovery = _discovery_summary(discovery_summary or manifest, complete=True)
            result = await asyncio.to_thread(self.queue.install_manifest, run_id, product_id, scope, normalized,
                                            certificates=certificates, max_attempts=max_attempts)
            await asyncio.to_thread(self._save_plan, run_id, product_id, discovery, scope)
            await asyncio.to_thread(self._save_state, run_id, "installed")
            return result

    async def start(self, run_id, *, measured_rps):
        async with self._lock(run_id):
            if self._stopping:
                raise QueueError("Stopped coordinator cannot restart; create a new coordinator")
            if run_id in self._enabled:
                return await asyncio.to_thread(read_status, self.queue, run_id)
            status = await asyncio.to_thread(read_status, self.queue, run_id, measured_rps=measured_rps)
            active = [p for p in status["products"] if p["product_status"] in {"queued", "running"}]
            if status["run_status"] not in {"queued", "running"} or not active:
                raise QueueError("Run has no active product scope; resume it before starting quotes")
            if any(not p["manifest_installed"] or not p["discovery"].get("discovery_complete") for p in active):
                raise QueueError("Every active product needs completed discovery and an installed manifest before quotes")
            payload = {"run_id": run_id, "scope_product_ids": [p["product_id"] for p in active],
                       "requests": status["requests"], "discovery": [{"product_id": p["product_id"], **p["discovery"]} for p in active],
                       "catalog_complete": False}
            await asyncio.to_thread(self._save_state, run_id, "preflight_ready", measured_rps=measured_rps,
                                    preflight=payload, event=("harvest_preflight", payload))
            # This awaited publication is a hard gate. No worker is enabled yet.
            try:
                await self._event(run_id, "harvest_preflight", payload, persisted=True)
            except BaseException:
                await asyncio.to_thread(self._save_state, run_id, "preflight_unpublished")
                raise
            await asyncio.to_thread(self._save_state, run_id, "running")
            self._enabled.add(run_id)
            if not self._tasks:
                self._tasks = [asyncio.create_task(self._worker(i)) for i in range(self.workers)]
                self._tasks.append(asyncio.create_task(self._monitor()))
            self._wake.set()
            return payload

    def status(self, run_id):
        return read_status(self.queue, run_id)

    async def pause(self, run_id):
        async with self._lock(run_id):
            self._enabled.discard(run_id)
            pending = [task for task, info in self._inflight.items() if info["run_id"] == run_id]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.to_thread(self._save_state, run_id, "paused")
            self._wake.set()

    async def stop(self):
        self._stopping = True
        runs = list(self._enabled)
        self._enabled.clear()
        for task in list(self._inflight):
            task.cancel()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, *list(self._inflight), return_exceptions=True)
        self._tasks.clear()
        self._inflight.clear()
        for run_id in runs:
            await asyncio.to_thread(self._save_state, run_id, "paused")

    async def _idle(self):
        try:
            await asyncio.wait_for(self._wake.wait(), self.poll_seconds)
        except TimeoutError:
            pass
        self._wake.clear()

    async def _worker(self, index):
        while not self._stopping:
            runs = sorted(self._enabled)
            if not runs:
                await self._idle()
                continue
            run_id = runs[self._round_robin % len(runs)]
            self._round_robin += 1
            info = {"run_id": run_id, "claim": None}
            async def factory(claim):
                info["claim"] = claim
                if run_id not in self._enabled:
                    raise asyncio.CancelledError()
                resource = self.transport_factory(claim)
                return await resource if inspect.isawaitable(resource) else resource
            task = asyncio.create_task(execute_worker(self.queue, run_id, f"{self.owner}:{index}", factory,
                                                      max_tasks=1, lease_seconds=self.lease_seconds))
            self._inflight[task] = info
            try:
                result = await task
                if result["succeeded"]:
                    await self._event(run_id, "harvest_response_checkpointed", {"response_ids": result["response_ids"],
                        "transport_invocations": result["transport_invocations"], "catalog_complete": False})
                if not result["claimed"]:
                    await self._check_idle(run_id)
                    await self._idle()
            except asyncio.CancelledError:
                if self._stopping:
                    raise
            except Exception as exc:
                self._enabled.discard(run_id)
                await asyncio.to_thread(self._save_state, run_id, "needs_attention", event=("harvest_coordinator_error", {"error_type": type(exc).__name__}))
            finally:
                self._inflight.pop(task, None)

    async def _check_idle(self, run_id):
        if run_id not in self._enabled or time.monotonic() - self._idle_checks.get(run_id, 0) < 1:
            return
        self._idle_checks[run_id] = time.monotonic()
        status = await asyncio.to_thread(read_status, self.queue, run_id)
        progress = status["requests"]
        if progress["unique_unfinished_requests"] == 0:
            self._enabled.discard(run_id)
            state = "paused" if not progress["active_scopes"] else "needs_attention" if progress["failed"] else "requests_drained"
            await asyncio.to_thread(self._save_state, run_id, state)
            await self._event(run_id, "harvest_queue_idle", {"status": state, "requests": progress, "catalog_complete": False})

    def _allowed(self, run_id, task_id=None):
        with self.store.session() as session:
            run = session.get(Run, run_id)
            if not run or run.status not in {"queued", "running"}:
                return False
            if task_id is None:
                return True
            return bool(session.scalar(select(HarvestTask.id).where(HarvestTask.id == task_id,
                HarvestTask.id.in_(self.queue._active_consumers(run_id)))))

    async def _monitor(self):
        while not self._stopping:
            for run_id in list(self._enabled):
                if not await asyncio.to_thread(self._allowed, run_id):
                    self._enabled.discard(run_id)
                    await asyncio.to_thread(self._save_state, run_id, "paused")
            for task, info in list(self._inflight.items()):
                claim = info["claim"]
                if (info["run_id"] not in self._enabled or
                    (claim and not await asyncio.to_thread(self._allowed, info["run_id"], claim["task_id"]))):
                    task.cancel()
            await asyncio.sleep(self.poll_seconds)
