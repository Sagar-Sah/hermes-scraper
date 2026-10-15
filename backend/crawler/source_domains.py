"""Dormant, bounded source-domain stages, separate from product row harvesting.

A stage is an immutable list of known read requests, not a complete catalog. It
may inspect price-table *tier domains* before configuration validity is known;
it never composes prices, emits product rows or unlocks QUOTE manifests. Explicit
start and a caller-installed session transport are both required for network I/O.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import httpx

from sqlalchemy import Column, Float, Integer, JSON, String, Text, func, select
from sqlalchemy.orm import deferred

from .harvest_queue import QueueError, execute_worker
from .probe_jobs import ORIGIN, public_url, validate_probe
from .route_dependencies import request_identity, request_key, validate_unique_lookup
from .store import Base, Event, Product, Response, ResponseObservation, Run, digest, uid
from .transport import ExtractionError

API = "/service/rest/v1/"
PURPOSES = {"specifications", "base_listing", "base_lookup", "option_catalog",
            "base_quantity_support", "option_quantity_support", "tier_domain"}
UNRESOLVED = ["Unrequested listing pages and selector relationships remain unverified.",
              "Option combinations, service state and combined turnaround rules remain unverified.",
              "Final configuration count and complete quote request budget are unknown."]
SESSION_FAILURES = {"domain_session_unavailable", "domain_context_changed", "session_bootstrap_required",
                    "domain_controller_unavailable", "domain_services_unresolved", "domain_session_busy",
                    "access_required", "tls_trust_error", "invalid_source_pacing", "worker_error", "queue_integrity_error"}
PRE_IO_SESSION_FAILURES = {"domain_session_unavailable", "domain_context_changed", "session_bootstrap_required",
                           "domain_controller_unavailable", "domain_services_unresolved", "domain_session_busy"}


class DomainJob(Base):
    __tablename__ = "source_domain_jobs"
    id = Column(String, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    scope = Column(String, nullable=False)
    manifest_hash = Column(String, nullable=False)
    manifest = deferred(Column(JSON, nullable=False))
    status = Column(String, nullable=False, default="dormant")
    owner = Column(String, nullable=False, default="")
    epoch = Column(Integer, nullable=False, default=0)
    budget = Column(Integer, nullable=False)
    used = Column(Integer, nullable=False, default=0)
    measured_rps = Column(Float, nullable=True)
    preflight = Column(JSON, nullable=False, default=dict)
    updated_at = Column(Float, nullable=False, default=time.time)


class DomainTask(Base):
    __tablename__ = "source_domain_tasks"
    id = Column(String, primary_key=True)
    job_id = Column(String, nullable=False, index=True)
    request_key = Column(String, nullable=False)
    request = deferred(Column(JSON, nullable=False))
    products = Column(JSON, nullable=False)
    status = Column(String, nullable=False, default="pending")
    owner = Column(String, nullable=False, default="")
    epoch = Column(Integer, nullable=False, default=0)
    job_epoch = Column(Integer, nullable=False, default=0)
    lease_until = Column(Float, nullable=False, default=0)
    not_before = Column(Float, nullable=False, default=0)
    attempts = Column(Integer, nullable=False, default=0)
    response_id = Column(String, nullable=True)
    error = Column(Text, nullable=True)


class DomainAttempt(Base):
    __tablename__ = "source_domain_attempts"
    id = Column(String, primary_key=True, default=uid)
    job_id = Column(String, nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    phase = Column(String, nullable=False)
    method = Column(String, nullable=False)
    public_url = Column(Text, nullable=False)
    status = Column(Integer, nullable=True)
    elapsed = Column(Float, nullable=True)


def normalize_manifest(value):
    """Full-context exact dedup only; no inferred parameter independence."""
    if not isinstance(value, dict) or value.get("phase") != "DOMAIN":
        raise QueueError("A DOMAIN-stage manifest is required")
    if any(value.get(key) is True for key in ("discovery_complete", "catalog_complete", "pricing_unlocked")):
        raise QueueError("A domain request stage cannot declare completed catalog discovery")
    entries = value.get("requests")
    if not isinstance(entries, list) or not entries or len(entries) > 20000:
        raise QueueError("A domain stage needs 1 to 20000 explicit requests")
    prepared = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("purpose") not in PURPOSES or not entry.get("product_id"):
            raise QueueError("Every domain read needs product attribution and a known purpose")
        request = request_identity(entry["request"])
        if request["context"]["origin"] != ORIGIN or request["method"] != "GET" or request["body"] is not None:
            raise QueueError("Domain stages support ordinary Printograph GET reads only")
        validate_probe({"method": "GET", "path": request["path"], "params": request["params"],
                        "context": request["context"]})
        if "/prices" in request["path"] and entry["purpose"] != "tier_domain":
            raise QueueError("A domain price-table read must be explicitly labeled tier_domain")
        key = request_key(request)
        prior = prepared.setdefault(key, {"id": key, "request": request, "products": [], "purposes": [], "evidence_ids": []})
        prior["products"] = sorted(set(prior["products"] + [str(entry["product_id"])]))
        prior["purposes"] = sorted(set(prior["purposes"] + [entry["purpose"]]))
        refs = entry.get("evidence_ids", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) or len(ref) > 128 for ref in refs):
            raise QueueError("Evidence references must be bounded response identifiers")
        prior["evidence_ids"] = sorted(set(prior["evidence_ids"] + refs))
    unresolved = value.get("unresolved", [])
    if not isinstance(unresolved, list) or any(not isinstance(v, str) or len(v) > 1000 for v in unresolved):
        raise QueueError("Unresolved discovery items must be short descriptions")
    return {"phase": "DOMAIN", "requests": [prepared[k] for k in sorted(prepared)],
            "unresolved": list(dict.fromkeys(UNRESOLVED + unresolved)), "discovery_complete": False,
            "catalog_complete": False, "pricing_unlocked": False, "final_configuration_count": None}


def plan_known_reads(product_id, product_type, context, *, lookup_observations=(),
                     include_specs=True, listing_offsets=(0,), listing_limit=1000,
                     include_base_tiers=False):
    """Plan finite bootstrap/pages and reads bound to explicitly validated lookups.

    A listing page is only a page; callers must inspect responses and install a
    separately budgeted next stage. An ID-only listing is not a selector lookup.
    Option catalogs/support operations can be added as explicit manifest entries
    once their actual source IDs and conditional selector domains are established.
    """
    product_type = str(product_type)
    if product_type not in {"1", "27", "36"}:
        raise QueueError("Unsupported Printograph product type")
    if not isinstance(listing_limit, int) or isinstance(listing_limit, bool) or not 1 <= listing_limit <= 1000:
        raise QueueError("Listing limit must be from 1 to 1000")
    entries = []
    def add(purpose, path, params, refs=()):
        entries.append({"product_id": product_id, "purpose": purpose, "evidence_ids": list(refs),
                        "request": {"method": "GET", "path": API + path, "params": params, "context": deepcopy(context)}})
    if include_specs:
        add("specifications", "settings/product/specifications", {"productType": product_type, "requestValues": "true"})
    for offset in listing_offsets:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise QueueError("Listing offsets must be nonnegative integers")
        add("base_listing", "products", {"productType": product_type, "requestValues": "true", "limit": listing_limit, "offset": offset})
    for observation in lookup_observations:
        lookup = validate_unique_lookup(observation)
        if lookup["request"]["context"] != context or str(lookup["request"]["params"]["productType"]) != product_type:
            raise QueueError("Lookup context/product differs from the requested domain stage")
        base, refs = lookup["base_product_id"], [lookup["response_id"]]
        add("option_catalog", f"products/{base}/options", {"requestValues": "true"}, refs)
        selector_keys = {"productType", "size", "orientation", "paper", "color"} | ({"shape", "finish"} if product_type == "36" else set())
        selectors = {k: lookup["request"]["params"][k] for k in selector_keys}
        add("base_quantity_support", "products/quantities", {**selectors, "includeTurnaround": "true"}, refs)
        if include_base_tiers:
            add("tier_domain", f"products/{base}/prices", {}, refs)
    return {"phase": "DOMAIN", "requests": entries, "unresolved": UNRESOLVED}


def preflight(manifest, *, budget, measured_rps=None):
    normalized = normalize_manifest(manifest)
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        raise QueueError("An explicit positive HTTP attempt budget is required")
    if measured_rps is not None and (not isinstance(measured_rps, (int, float)) or isinstance(measured_rps, bool)
                                    or not math.isfinite(measured_rps) or measured_rps <= 0):
        raise QueueError("Measured request rate must be positive and finite")
    count = len(normalized["requests"])
    return {"phase": "DOMAIN", "planned_unique_requests": count, "completed_requests": 0,
            "remaining_unique_requests": count, "attempt_budget": budget, "attempts_used": 0,
            "budget_covers_one_attempt_per_known_request": budget >= count,
            "request_seconds_at_measured_rate": count / measured_rps if measured_rps else None,
            "measured_requests_per_second": measured_rps, "is_end_to_end_eta": False,
            "supplied_preflight_requests_per_second": measured_rps,
            "measured_rate_basis": "Caller-supplied planning rate; not measured by this stage.",
            "catalog_complete": False, "discovery_complete": False, "pricing_unlocked": False,
            "final_configuration_count": None, "unresolved": normalized["unresolved"],
            "request_count_scope": "This immutable stage only; excludes undiscovered pages, conditional reads and retries.",
            "budget_scope": "All outgoing HTTP attempts, including session bootstrap and transport retries."}


class DomainQueue:
    """An execute_worker-compatible queue for one separately authorized domain job."""
    def __init__(self, store, job_id):
        self.store, self.job_id = store, job_id

    def _job(self, session, *, running=True):
        job = session.get(DomainJob, self.job_id, with_for_update=not self.store.sqlite)
        run = session.get(Run, job.run_id) if job else None
        if not job or not run or (running and (job.status != "running" or run.status in {"paused", "cancelled"})):
            return None
        return job

    def claim(self, run_id, owner, *, lease_seconds=60):
        with self.store.session(immediate=True) as s:
            job = self._job(s)
            if not job or job.run_id != run_id or job.used >= job.budget:
                return None
            now = self.store.now(s)
            tasks = s.scalars(select(DomainTask).where(DomainTask.job_id == job.id,
                ((DomainTask.status == "pending") & (DomainTask.not_before <= now)) |
                ((DomainTask.status == "leased") & (DomainTask.lease_until <= now))).order_by(DomainTask.id).limit(1))
            for task in tasks:
                job.updated_at = now
                if task.attempts >= 3:
                    task.status, task.error = "failed", "Logical task attempt limit reached"
                    continue
                task.status, task.owner, task.job_epoch = "leased", owner, job.epoch
                task.epoch += 1
                task.attempts += 1
                task.lease_until = now + lease_seconds
                return {"task_id": task.id, "run_id": run_id, "product_id": task.products[0], "owner": owner,
                        "epoch": task.epoch, "job_epoch": job.epoch, "job_owner": job.owner,
                        "request": task.request, "request_key": task.request_key, "attempt": task.attempts,
                        "max_attempts": 3, "lease_until": task.lease_until}

    def _leased(self, s, task_id, owner, epoch):
        job = self._job(s)
        task = s.get(DomainTask, task_id)
        return task if (job and task and task.job_id == job.id and task.job_epoch == job.epoch and task.status == "leased" and task.owner == owner
                        and task.epoch == epoch and task.lease_until > self.store.now(s)) else None

    def heartbeat(self, task_id, owner, epoch, *, lease_seconds=60):
        with self.store.session(immediate=True) as s:
            task = self._leased(s, task_id, owner, epoch)
            if not task:
                return False
            task.lease_until = self.store.now(s) + lease_seconds
            return True

    def complete(self, task_id, owner, epoch, response_id):
        with self.store.session(immediate=True) as s:
            task = self._leased(s, task_id, owner, epoch)
            if not task:
                return False
            response = s.get(Response, response_id)
            job = s.get(DomainJob, self.job_id)
            binding = s.scalar(select(ResponseObservation.id).where(ResponseObservation.run_id == job.run_id,
                ResponseObservation.product_id == task.products[0], ResponseObservation.response_id == response_id,
                ResponseObservation.kind == "harvest", ResponseObservation.request_signature == task.request_key).limit(1))
            if not response or response.status != 200 or not binding:
                raise QueueError("Domain completion requires a persisted successful full-context response binding")
            parsed = urlsplit(response.url or "")
            query = parse_qs(str(httpx.QueryParams(task.request["params"])), keep_blank_values=True)
            if (f"{parsed.scheme}://{parsed.netloc}" != task.request["context"]["origin"] or
                    parsed.path != task.request["path"] or parse_qs(parsed.query, keep_blank_values=True) != query):
                raise QueueError("Domain response URL differs from the leased request")
            task.status, task.response_id, task.lease_until, task.error = "succeeded", response_id, 0, None
            job.updated_at = self.store.now(s)
            return True

    def retry(self, task_id, owner, epoch, error, *, delay=0, retryable=True):
        with self.store.session(immediate=True) as s:
            task = self._leased(s, task_id, owner, epoch)
            if not task:
                return False
            job = s.get(DomainJob, self.job_id)
            job.updated_at = self.store.now(s)
            code = str(error).split(":", 1)[0]
            if code in SESSION_FAILURES:
                # A missing shared session cannot be repaired by trying the
                # next hundreds of unrelated product IDs. Fence all in-flight
                # work atomically; leave the immutable tasks available for an
                # explicitly repaired same-session resume or a new scope.
                job.status, job.epoch = "needs_attention", job.epoch + 1
                if code in PRE_IO_SESSION_FAILURES:
                    task.attempts = max(0, task.attempts - 1)
                task.error = str(error)
                for leased in s.scalars(select(DomainTask).where(DomainTask.job_id == job.id, DomainTask.status == "leased")):
                    leased.status, leased.owner, leased.lease_until, leased.not_before = "pending", "", 0, 0
                    leased.epoch += 1
                s.add(Event(run_id=job.run_id, type="source_domain_attention",
                            payload={"job_id": job.id, "code": code, "scope": "whole_domain_stage"}))
                return True
            task.status = "pending" if retryable and task.attempts < 3 else "failed"
            task.error, task.not_before, task.lease_until, task.owner = str(error), self.store.now(s) + delay, 0, ""
            task.epoch += 1
            return True


class DomainBudget:
    """Duck type accepted by BrowserHTTPClient and its bootstrap request budget."""
    def __init__(self, store, job_id, owner, epoch):
        self.store, self.job_id, self.owner, self.epoch = store, job_id, owner, epoch

    def _valid(self, s):
        job = s.get(DomainJob, self.job_id, with_for_update=not self.store.sqlite)
        run = s.get(Run, job.run_id) if job else None
        if not job or job.status != "running" or job.owner != self.owner or job.epoch != self.epoch:
            raise ExtractionError("Domain planner scope ended", "interrupted")
        if not run or run.status in {"paused", "cancelled"}:
            raise ExtractionError("Run paused or cancelled", "interrupted")
        return job

    async def check(self):
        with self.store.session() as s:
            self._valid(s)

    def reserve(self, phase, method, url):
        with self.store.session(immediate=True) as s:
            job = self._valid(s)
            if job.used >= job.budget:
                raise ExtractionError("Durable domain HTTP attempt budget exhausted", "domain_budget_exhausted")
            job.used += 1
            job.updated_at = self.store.now(s)
            row = DomainAttempt(job_id=job.id, sequence=job.used, phase=str(phase), method=method, public_url=public_url(url))
            s.add(row)
            s.flush()
            return row.id

    def finish(self, attempt_id, status, elapsed):
        with self.store.session(immediate=True) as s:
            row = s.get(DomainAttempt, attempt_id)
            if row and row.job_id == self.job_id:
                row.status, row.elapsed = status, elapsed
                job = s.get(DomainJob, self.job_id)
                if job and job.status == "running" and job.owner == self.owner and job.epoch == self.epoch:
                    job.updated_at = self.store.now(s)


class _BudgetClient:
    def __init__(self, client, budget):
        self.client, self.budget = client, budget

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        await self.budget.check()
        attempt = await asyncio.to_thread(self.budget.reserve, "domain", method, str(url))
        started, status = time.monotonic(), 0
        try:
            async with self.client.stream(method, url, **kwargs) as response:
                status = response.status_code
                yield response
        finally:
            await asyncio.to_thread(self.budget.finish, attempt, status, time.monotonic() - started)

    async def aclose(self):
        await self.client.aclose()


class SourceDomainCoordinator:
    def __init__(self, store, transport_factory=None, *, workers=2, publish=None, poll_seconds=0.25, on_stage_finished=None, context_validator=None):
        if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1 or workers > 16:
            raise QueueError("Domain worker count must be from 1 to 16")
        self.store, self.transport_factory, self.publish = store, transport_factory, publish
        self.on_stage_finished = on_stage_finished
        self.context_validator = context_validator
        self.workers, self.poll_seconds, self.owner = workers, poll_seconds, uid()
        self._active, self._workers = set(), []
        self._starting = set()
        self._first_preflight_at = {}
        Base.metadata.create_all(store.engine, tables=[DomainJob.__table__, DomainTask.__table__, DomainAttempt.__table__])

    def dry_run(self, run_id, manifest, *, budget, measured_rps=None):
        normalized = normalize_manifest(manifest)
        with self.store.session() as s:
            if not s.get(Run, run_id):
                raise LookupError("Run not found")
            for entry in normalized["requests"]:
                for product_id in entry["products"]:
                    product = s.get(Product, product_id)
                    if not product or product.run_id != run_id:
                        raise QueueError("Every domain consumer must belong to the selected run")
        return preflight(manifest, budget=budget, measured_rps=measured_rps)

    def install(self, run_id, scope, manifest, *, budget, measured_rps=None):
        if not isinstance(scope, str) or not scope or len(scope) > 128:
            raise QueueError("A bounded explicit planner scope is required")
        report = self.dry_run(run_id, manifest, budget=budget, measured_rps=measured_rps)
        normalized = normalize_manifest(manifest)
        job_id, fingerprint = digest([run_id, scope]), digest(normalized)
        with self.store.session(immediate=True) as s:
            existing = s.get(DomainJob, job_id)
            if existing:
                if existing.manifest_hash != fingerprint or existing.budget != budget:
                    raise QueueError("Immutable domain scope changed; install a new scope")
            else:
                s.add(DomainJob(id=job_id, run_id=run_id, scope=scope, manifest_hash=fingerprint, manifest=normalized,
                                budget=budget, measured_rps=measured_rps, preflight=report, updated_at=self.store.now(s)))
                for entry in normalized["requests"]:
                    s.add(DomainTask(id=digest([job_id, entry["id"]]), job_id=job_id, request_key=entry["id"],
                                     request=entry["request"], products=entry["products"]))
        return self.status(job_id)

    def _observed_timing(self, s, job, completed):
        """Evidence window, not active runtime or an outgoing HTTP attempt rate.

        Cache only the immutable first event timestamp. Operational polling skips
        this aggregation; public status queries never load full request bodies.
        """
        started = self._first_preflight_at.get(job.id)
        if started is None:
            started = s.scalar(select(func.min(Event.created_at)).where(
                Event.run_id == job.run_id, Event.type == "source_domain_preflight",
                Event.payload["job_id"].as_string() == job.id))
            if started is not None:
                self._first_preflight_at[job.id] = started
        # SQLite chose the low-selectivity product_id index for the former
        # multi-predicate join, rereading a product's full history for every task.
        # A response-ID-only lookup uses the existing selective index. Apply the
        # exact same remaining binding predicates after each bounded batch.
        count, first, last = 0, None, None
        tasks = s.execute(select(DomainTask.id, DomainTask.response_id, DomainTask.request_key,
                DomainTask.products[0].as_string().label("product_id"))
            .where(DomainTask.job_id == job.id, DomainTask.status == "succeeded")
            .execution_options(yield_per=512))
        for batch in tasks.partitions(512):
            bindings = {}
            for task in batch:
                bindings.setdefault((task.response_id, task.request_key, task.product_id), set()).add(task.id)
            response_ids = {task.response_id for task in batch if task.response_id is not None}
            if not response_ids:
                continue
            matched = set()
            observations = s.execute(select(ResponseObservation.response_id, ResponseObservation.request_signature,
                    ResponseObservation.product_id, ResponseObservation.run_id, ResponseObservation.kind,
                    ResponseObservation.created_at)
                .where(ResponseObservation.response_id.in_(response_ids)))
            for observed in observations:
                if observed.run_id != job.run_id or observed.kind != "harvest":
                    continue
                members = bindings.get((observed.response_id, observed.request_signature, observed.product_id))
                if not members:
                    continue
                matched.update(members)
                timestamp = observed.created_at
                if timestamp is not None:
                    first = timestamp if first is None else min(first, timestamp)
                    last = timestamp if last is None else max(last, timestamp)
            count += len(matched)
        covered = bool(completed and count == completed and started is not None and first is not None and first >= started)
        elapsed = last - started if covered and last > started else None
        return {"first_preflight_at": started, "first_response_at": first, "last_response_at": last,
                "bound_completed_requests": count, "complete_timing_coverage": covered,
                "elapsed_seconds": elapsed, "requests_per_second": count / elapsed if elapsed else None,
                "basis": "Unique completed request bindings from first preflight through last successful response; includes intervening pauses and retries. Not an HTTP attempt rate or active-runtime measurement. Preexisting cached bindings leave the rate unknown."}

    def status(self, job_id, *, include_responses=True, include_timing=True):
        with self.store.session() as s:
            job = s.get(DomainJob, job_id)
            if not job:
                raise LookupError("Domain job not found")
            grouped = dict(s.execute(select(DomainTask.status, func.count(DomainTask.id))
                                     .where(DomainTask.job_id == job_id).group_by(DomainTask.status)).all())
            counts = {state: grouped.get(state, 0) for state in ("pending", "leased", "succeeded", "failed")}
            remaining = sum(counts.values()) - counts["succeeded"]
            errors = s.execute(select(DomainTask.request_key, DomainTask.error)
                .where(DomainTask.job_id == job_id, DomainTask.error.is_not(None)).limit(20)).all()
            responses = (s.execute(select(DomainTask.request_key, DomainTask.response_id, DomainTask.products)
                .where(DomainTask.job_id == job_id, DomainTask.status == "succeeded")).all() if include_responses else [])
            run = s.get(Run, job.run_id)
            return {**job.preflight, "job_id": job.id, "run_id": job.run_id, "scope": job.scope,
                    "status": job.status, "run_paused": bool(run and run.status in {"paused", "cancelled"}),
                    "completed_requests": counts["succeeded"], "remaining_unique_requests": remaining,
                    "attempts_used": job.used, "attempts_remaining": max(0, job.budget - job.used),
                    "task_counts": counts, "known_stage_requests_complete": remaining == 0,
                    "request_seconds_at_measured_rate": remaining / job.measured_rps if job.measured_rps else None,
                    "supplied_preflight_requests_per_second": job.measured_rps,
                    "measured_rate_basis": "Caller-supplied planning rate; not measured by this stage.",
                    "observed_stage_timing": self._observed_timing(s, job, counts["succeeded"]) if include_timing else None,
                    "observed_timing_included": include_timing,
                    "factory_available": self.transport_factory is not None,
                    "updated_at": job.updated_at,
                    "task_errors": [{"request_key": key, "error": error} for key, error in errors],
                    "responses_included": include_responses,
                    "responses": [{"request_key": t.request_key, "response_id": t.response_id, "product_ids": t.products}
                                  for t in responses]}

    def has_active_scope(self, run_id, session):
        """Use the caller's transaction so auto-pause cannot race scope ownership."""
        return session.scalar(select(DomainJob.id).where(DomainJob.run_id == run_id,
            DomainJob.owner == self.owner, DomainJob.status.in_(["preflight_unpublished", "running"])).limit(1)) is not None

    async def _stage_finished(self, run_id):
        if self.on_stage_finished:
            result = self.on_stage_finished(run_id)
            if inspect.isawaitable(result):
                await result

    async def start(self, job_id, *, resume_run=False):
        if self.transport_factory is None:
            raise QueueError("Domain session transport is not installed; this planner is dormant")
        if job_id in self._starting:
            raise QueueError("Domain preflight publication is already in progress")
        if job_id in self._active and not self.status(job_id, include_responses=False)["run_paused"]:
            return self.status(job_id)
        self._starting.add(job_id)
        run_id = None
        try:
            with self.store.session(immediate=True) as s:
                job = s.get(DomainJob, job_id)
                if not job:
                    raise LookupError("Domain job not found")
                run = s.get(Run, job.run_id)
                if not run or run.status == "cancelled" or (run.status == "paused" and not resume_run):
                    raise QueueError("The run is paused or cancelled")
                if job.status == "running" and job.owner != self.owner:
                    raise QueueError("Domain stage belongs to another active coordinator; explicitly pause it first")
                if job.used >= job.budget:
                    raise QueueError("Domain HTTP attempt budget is exhausted")
                if run.status == "paused" and resume_run:
                    # Explicitly authorized planner-only resume. Product pause
                    # statuses/epochs and all successful responses are untouched.
                    run.status = "running"
                job.owner, job.epoch, job.status = self.owner, job.epoch + 1, "preflight_unpublished"
                job.updated_at = self.store.now(s)
                for task in s.scalars(select(DomainTask).where(DomainTask.job_id == job_id, DomainTask.status == "leased")):
                    task.status, task.owner, task.epoch, task.lease_until = "pending", "", task.epoch + 1, 0
                epoch, run_id = job.epoch, job.run_id
                payload = self.status(job_id)
                s.add(Event(run_id=run_id, type="source_domain_preflight", payload=payload, created_at=job.updated_at))
            if self.publish:
                result = self.publish(run_id, "source_domain_preflight", payload)
                if inspect.isawaitable(result):
                    await result
            with self.store.session(immediate=True) as s:
                job = s.get(DomainJob, job_id)
                run = s.get(Run, run_id)
                if job.owner != self.owner or job.epoch != epoch or job.status != "preflight_unpublished" or run.status in {"paused", "cancelled"}:
                    raise QueueError("Domain scope changed before preflight publication completed")
                job.status = "running"
                job.updated_at = self.store.now(s)
            self._active.add(job_id)
            if not self._workers:
                self._workers = [asyncio.create_task(self._pump(i)) for i in range(self.workers)]
            return self.status(job_id)
        except BaseException:
            changed = False
            with self.store.session(immediate=True) as s:
                job = s.get(DomainJob, job_id)
                if job and job.owner == self.owner and job.status == "preflight_unpublished":
                    job.status, job.epoch = "preflight_failed", job.epoch + 1
                    job.updated_at = self.store.now(s)
                    run_id, changed = job.run_id, True
            if changed:
                await self._stage_finished(run_id)
            raise
        finally:
            self._starting.discard(job_id)

    @asynccontextmanager
    async def _transport(self, job_id, claim):
        budget = DomainBudget(self.store, job_id, claim["job_owner"], claim["job_epoch"])
        await budget.check()
        resource = self.transport_factory(claim, budget)
        if inspect.isawaitable(resource):
            resource = await resource
        async with AsyncExitStack() as stack:
            if hasattr(resource, "__aenter__"):
                transport = await stack.enter_async_context(resource)
            else:
                transport = resource
                stack.push_async_callback(transport.close)
            if not hasattr(transport, "client"):
                raise QueueError("Domain transport must expose its budgeted HTTP client")
            old_check, old_client = transport.check, transport.client
            # This dedicated transport uses the explicit planner scope. The legacy
            # product's pause is unchanged and is never treated as row-write authority.
            transport.check = budget.check
            if getattr(old_client, "budget", None) is not budget:
                transport.client = _BudgetClient(old_client, budget)
            try:
                yield transport
            finally:
                transport.check, transport.client = old_check, old_client

    async def retry_failed(self, job_id, request_keys, *, resume_run=False):
        """Explicit finite retry; no manifest/context/budget replacement or refetch."""
        if (not isinstance(request_keys, list) or not request_keys or len(request_keys) > 100
                or any(not isinstance(key, str) or len(key) != 64 for key in request_keys)
                or len(set(request_keys)) != len(request_keys)):
            raise QueueError("Provide 1–100 distinct failed request keys")
        validator = self.context_validator or getattr(getattr(self.transport_factory, "__self__", None), "validate_context", None)
        if validator is None:
            raise QueueError("A current session context validator is required before retry")
        with self.store.session() as s:
            job = s.get(DomainJob, job_id)
            if not job:
                raise LookupError("Domain job not found")
            run = s.get(Run, job.run_id)
            if not run or run.status == "cancelled" or (run.status == "paused" and not resume_run):
                raise QueueError("The run is paused or cancelled")
            if job.status == "running" and job.owner != self.owner:
                raise QueueError("Another coordinator owns the active stage")
            if job.used >= job.budget:
                raise QueueError("Domain HTTP attempt budget is exhausted")
            tasks = list(s.scalars(select(DomainTask).where(DomainTask.job_id == job_id, DomainTask.request_key.in_(request_keys))))
            if len(tasks) != len(request_keys) or any(task.status != "failed" or task.attempts >= 3 for task in tasks):
                raise QueueError("Every selected key must be failed with remaining logical attempts")
            expected_epoch = job.epoch
            expected_run_status = run.status
            snapshots = [(task.id, task.epoch, task.error, task.attempts) for task in tasks]
            claims = [{"run_id": job.run_id, "product_id": task.products[0], "request": task.request} for task in tasks]
        for claim in claims:
            result = validator(claim)
            if inspect.isawaitable(result):
                await result
        with self.store.session(immediate=True) as s:
            job = s.get(DomainJob, job_id)
            run = s.get(Run, job.run_id)
            if (job.epoch != expected_epoch or run.status != expected_run_status or run.status == "cancelled"
                    or (run.status == "paused" and not resume_run)):
                raise QueueError("Planner or run changed during context validation")
            for task_id, epoch, error, attempts in snapshots:
                task = s.get(DomainTask, task_id)
                if task.status != "failed" or task.epoch != epoch or task.error != error or task.attempts != attempts:
                    raise QueueError("Selected request changed during context validation")
                task.status, task.owner, task.lease_until, task.not_before = "pending", "", 0, 0
                task.epoch += 1
            job.updated_at = self.store.now(s)
            s.add(Event(run_id=job.run_id, type="source_domain_retry_requested", payload={
                "job_id": job.id, "request_keys": request_keys, "remaining_http_budget": job.budget - job.used,
                "completed_responses_preserved": True, "resume_run": resume_run}))
        return await self.start(job_id, resume_run=resume_run)

    async def _pump(self, index):
        while True:
            claimed_work = False
            for job_id in list(self._active):
                state = self.status(job_id, include_responses=False, include_timing=False)
                if state["run_paused"] or state["status"] != "running":
                    if state["run_paused"] and state["status"] == "running":
                        await self.pause(job_id)
                    else:
                        self._active.discard(job_id)
                    continue
                queue = DomainQueue(self.store, job_id)
                result = await execute_worker(queue, state["run_id"], f"{self.owner}:{index}",
                                              lambda claim: self._transport(job_id, claim), max_tasks=1, lease_seconds=3)
                claimed_work = claimed_work or bool(result["claimed"])
                state = self.status(job_id, include_responses=False, include_timing=False)
                if state["status"] != "running":
                    if job_id in self._active:
                        self._active.discard(job_id)
                        await self._stage_finished(state["run_id"])
                    continue
                terminal = ("requests_drained" if state["known_stage_requests_complete"] else
                            "budget_exhausted" if state["attempts_remaining"] == 0 and not state["task_counts"]["leased"] else
                            "needs_attention" if not state["task_counts"]["pending"] and not state["task_counts"]["leased"] else None)
                if terminal:
                    changed = False
                    with self.store.session(immediate=True) as s:
                        job = s.get(DomainJob, job_id)
                        if job.owner == self.owner and job.status == "running":
                            job.status = terminal
                            job.updated_at = self.store.now(s)
                            changed = True
                    self._active.discard(job_id)
                    if changed:
                        await self._stage_finished(state["run_id"])
            # The shared source limiter already spaces outgoing requests. Poll
            # only when no task could be claimed, including future retries and
            # work leased by another worker; never spin on an unavailable queue.
            if not claimed_work:
                await asyncio.sleep(self.poll_seconds)

    async def pause(self, job_id):
        with self.store.session(immediate=True) as s:
            job = s.get(DomainJob, job_id)
            if not job:
                raise LookupError("Domain job not found")
            job.status, job.epoch, job.owner = "paused", job.epoch + 1, ""
            job.updated_at = self.store.now(s)
            for task in s.scalars(select(DomainTask).where(DomainTask.job_id == job_id, DomainTask.status == "leased")):
                task.status, task.epoch, task.owner, task.lease_until = "pending", task.epoch + 1, "", 0
        self._active.discard(job_id)
        await self._stage_finished(job.run_id)
        return self.status(job_id)

    async def close(self):
        for job_id in list(self._active):
            await self.pause(job_id)
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
