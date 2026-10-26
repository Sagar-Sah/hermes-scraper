"""Explicit finite campaign plans, independent supplier workers, durable receipts.

No supplier implementation, discovery guesses, HTTP client, database, or source
imports live here. Profiles are registered by the application. Historical runs
and their dormant tasks are deliberately outside this state machine.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
import fcntl
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid
from urllib.parse import urlsplit


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
TERMINAL = {"completed", "held", "access_required"}
MAX_CONTINUATIONS = 100000
MAX_DISPATCH_SEQUENCE = 1_000_000_000_000


def canonical_supplier(value):
    if not isinstance(value, str): raise ValueError("Invalid supplier identity")
    aliases = {"fourover": "4over", "4over": "4over", "printograph": "printograph",
               "b2sign": "b2sign", "dps": "dps", "digital print solutions": "dps",
               "digital_print_solutions": "dps", "digital-print-solutions": "dps"}
    return aliases.get(value.casefold(), value)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def dispatch_sequence(job):
    # Optional for legacy private records. Never derive fairness from attempts
    # or a wall clock: a continuing product must yield even when time is frozen.
    value = job.get("last_dispatch_sequence", 0)
    if type(value) is not int or not 0 <= value <= MAX_DISPATCH_SEQUENCE:
        raise ValueError("Invalid persisted dispatch sequence")
    return value


def identifier(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError("Invalid automation identity")
    return value


def safe_reason(value):
    return value if isinstance(value, str) and SAFE_REASON.fullmatch(value) else "profile_held"


def access_reason(value):
    """Keep known source form/profile prerequisites distinct from sign-in."""
    known = {"source_project_details_required", "source_account_profile_action_required"}
    return value if isinstance(value, str) and value in known else "supplier_authentication_required"


@dataclass(frozen=True)
class Profile:
    id: str
    supplier: str
    supports: object
    discover: object
    normalize: object
    execute: object
    # A pending result is pollable only through a separately registered,
    # source-free saved-resource reader. execute is never used as a poll.
    poll: object = None
    priority: int = 0


class CampaignAutomation:
    def __init__(self, root, profiles, resolve_associations, *, capacity=8,
                 max_polls=60, max_wait_seconds=900, call_timeout=120, clock=time.time):
        for value, ceiling in ((capacity, 99), (max_polls, 600)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("Invalid finite worker bound")
        for value in (max_wait_seconds, call_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Invalid finite time bound")
        self.root = Path(root) / "campaign-automation"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = self.root.lstat()
        if not stat.S_ISDIR(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o700 or mode.st_uid != os.getuid():
            raise ValueError("Private owned automation directory required")
        owner_fd = os.open(self.root / ".owner", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._owner = os.fdopen(owner_fd, "a+b")
        self._private_file(self.root / ".owner")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._owner.close()
            raise ValueError("Automation directory already has an owner") from None
        self.profiles = {identifier(p.id): replace(p, supplier=canonical_supplier(p.supplier)) for p in profiles}
        if len(self.profiles) != len(profiles):
            self._owner.close()
            raise ValueError("Duplicate profile")
        for p in self.profiles.values():
            identifier(p.supplier)
            if type(p.priority) is not int: raise ValueError("Profile priority must be an integer")
        self.resolve = resolve_associations
        self.capacity, self.max_polls = capacity, max_polls
        self.max_wait, self.call_timeout, self.clock = max_wait_seconds, call_timeout, clock
        self._records, self._active, self._supplier_active = {}, {}, set()
        self._lock, self._loop, self._closing = asyncio.Lock(), None, False
        for path in sorted(self.root.glob("*.json")):
            self._private_file(path)
            record = json.loads(path.read_text(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite record")))
            if record.get("schema") != 2 or identifier(record.get("campaign_id")) != path.stem:
                raise ValueError("Invalid automation record")
            # Dispatch may have reached the supplier before a crash. Preserve
            # that uncertainty; never guess that it is safe to repeat execute.
            changed = False
            for job in record["jobs"].values():
                if job["status"] == "running":
                    job.update(status="held", reason="worker_interrupted", updated_at=self.clock())
                    changed = True
            self._records[path.stem] = record
            if changed:
                self._save(record)

    @staticmethod
    def _private_file(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError("Private owned automation record required")

    def _save(self, record):
        data = canonical(record).encode()
        if len(data) > 4_000_000:
            raise ValueError("Automation record bound exceeded")
        target = self.root / (identifier(record["campaign_id"]) + ".json")
        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory_fd)
            finally: os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _record(self, campaign_id):
        identifier(campaign_id)
        return self._records.setdefault(campaign_id, {"schema": 2, "campaign_id": campaign_id,
            "enabled": False, "generation": 0, "plans": {}, "jobs": {}, "supplier_blocks": {}, "supplier_not_before": {}, "worker_faults": []})

    def _associations(self, campaign_id):
        rows = self.resolve(campaign_id)
        found = {}
        for row in rows:
            row = deepcopy(row)
            if set(row) != {"campaign_id", "product_id", "supplier", "source_url"} or row["campaign_id"] != campaign_id:
                raise ValueError("Invalid trusted product association")
            row["supplier"] = canonical_supplier(row["supplier"])
            for key in ("campaign_id", "product_id", "supplier"):
                identifier(row[key])
            if not isinstance(row["source_url"], str) or len(row["source_url"]) > 2048:
                raise ValueError("Invalid trusted source mapping")
            source = urlsplit(row["source_url"])
            if source.scheme != "https" or not source.hostname or source.username or source.password or source.query or source.fragment:
                raise ValueError("Public exact source mapping required")
            if row["product_id"] in found:
                raise ValueError("Ambiguous product association")
            found[row["product_id"]] = deepcopy(row)
        return found

    def _check_association(self, job):
        actual = self._associations(job["association"]["campaign_id"]).get(job["product_id"])
        if actual != job["association"]:
            raise ValueError("Product scope changed")
        profile = self.profiles.get(job["profile_id"])
        if profile is None or profile.supplier != actual["supplier"] or not profile.supports(deepcopy(actual)):
            raise ValueError("Profile scope changed")
        return profile

    def snapshot(self, campaign_id):
        r = self._record(campaign_id)
        jobs = [{k: deepcopy(j[k]) for k in ("id", "product_id", "profile_id", "supplier", "status",
                    "attempts", "polls", "reason", "result_reference", "created_at", "updated_at")}
                for j in r["jobs"].values()]
        for public, saved in zip(jobs, r["jobs"].values()):
            for key, maximum in (("progress_count", 10_000_000), ("continuations", MAX_CONTINUATIONS)):
                value = saved.get(key, 0)
                public[key] = value if type(value) is int and 0 <= value <= maximum else 0
        counts = {state: sum(j["status"] == state for j in jobs)
                  for state in ("queued", "running", "waiting", "completed", "held", "access_required")}
        return {"campaign_id": campaign_id, "enabled": r["enabled"], "jobs": jobs,
                "summary": counts, "supplier_blocks": deepcopy(r["supplier_blocks"]),
                "supplier_not_before": deepcopy(r["supplier_not_before"]),
                "worker_faults": deepcopy(r["worker_faults"]),
                "capabilities": [{"profile_id": p.id, "supplier": p.supplier,
                    "product_ids": [a["product_id"] for a in self._associations(campaign_id).values()
                                    if a["supplier"] == p.supplier and p.supports(deepcopy(a))],
                    "can_discover": True} for p in self.profiles.values()],
                "stage_scoped_only": True, "source_data_finalized": False, "woocommerce_import_ready": False}

    async def plan(self, campaign_id, *, product_ids=None, max_jobs=99):
        if type(max_jobs) is not int or not 1 <= max_jobs <= 99:
            raise ValueError("Plan bound must be 1 through 99")
        if product_ids is not None and (not isinstance(product_ids, list) or len(product_ids) > 99
                or len(set(product_ids)) != len(product_ids)):
            raise ValueError("Distinct product identities required")
        async with self._lock:
            r = self._record(campaign_id)
            associations = self._associations(campaign_id)
            requested = list(associations) if product_ids is None else [identifier(p) for p in product_ids]
            jobs, blockers = [], []
            for pid in requested:
                a = associations.get(pid)
                if a is None:
                    blockers.append({"product_id": pid, "reason": "product_outside_active_scope"}); continue
                try:
                    matches = [p for p in self.profiles.values() if p.supplier == a["supplier"] and p.supports(deepcopy(a))]
                except Exception:
                    blockers.append({"product_id": pid, "reason": "profile_plan_failed"}); continue
                if matches:
                    highest = max(p.priority for p in matches)
                    matches = [p for p in matches if p.priority == highest]
                if len(matches) != 1:
                    blockers.append({"product_id": pid, "reason": "needs_profile" if not matches else "ambiguous_profile"}); continue
                p = matches[0]
                remaining = max_jobs - len(jobs)
                if not remaining:
                    blockers.append({"product_id": pid, "reason": "plan_limit_reached"}); continue
                # Discovery is source-free. Calling a coroutine here is rejected
                # rather than accidentally starting a browser or source request.
                try:
                    d = p.discover(deepcopy(a), 1)
                except Exception:
                    blockers.append({"product_id": pid, "reason": "profile_plan_failed"}); continue
                if inspect.isawaitable(d):
                    if inspect.iscoroutine(d): d.close()
                    raise ValueError("Profile discovery must be source-free and synchronous")
                if not isinstance(d, dict) or set(d) != {"configurations", "blockers"} or not isinstance(d["configurations"], list) or len(d["configurations"]) > 1 or not isinstance(d["blockers"], list):
                    raise ValueError("Invalid finite profile plan")
                for reason in d["blockers"]:
                    blockers.append({"product_id": pid, "reason": safe_reason(reason)})
                if not d["configurations"] and not d["blockers"]:
                    blockers.append({"product_id": pid, "reason": "no_supported_work"})
                for config in d["configurations"]:
                    try:
                        normalized = p.normalize(deepcopy(config))
                        if not isinstance(normalized, dict) or len(canonical(normalized)) > 16000:
                            raise ValueError("Invalid profile configuration")
                    except Exception:
                        blockers.append({"product_id": pid, "reason": "invalid_profile_configuration"}); continue
                    key = identity({"association": a, "profile_id": p.id, "configuration": normalized})
                    if key in r["jobs"]:
                        blockers.append({"product_id": pid, "reason": "existing_selection_preserved"}); continue
                    if not any(j["id"] == key for j in jobs):
                        jobs.append({"id": key, "product_id": pid, "profile_id": p.id,
                                     "supplier": p.supplier, "association": a, "configuration": normalized})
            plan_id = identity({"jobs": jobs, "blockers": blockers})
            r["plans"] = {plan_id: {"plan_id": plan_id, "jobs": jobs, "blockers": blockers}}
            self._save(r)
            return deepcopy(r["plans"][plan_id])

    async def start(self, campaign_id, plan_id):
        async with self._lock:
            r = self._record(campaign_id)
            plan = r["plans"].get(plan_id)
            if plan is None: raise ValueError("Saved finite plan required")
            if len(r["jobs"]) + len(plan["jobs"]) > 2000: raise ValueError("Campaign job limit reached")
            for job in plan["jobs"]: self._check_association(job)
            now = self.clock()
            for job in plan["jobs"]:
                if job["id"] not in r["jobs"]:
                    r["jobs"][job["id"]] = {**deepcopy(job), "status": "queued", "attempts": 0,
                        "polls": 0, "reason": None, "result_reference": None, "pending_token": None,
                        "created_at": now, "updated_at": now, "next_at": now, "wait_started_at": None,
                        "continuations": 0, "progress_count": 0, "progress_token": None}
            r["enabled"] = True
            self._save(r)
            return self.snapshot(campaign_id)

    async def control(self, campaign_id, action):
        if action not in {"pause", "resume"}: raise ValueError("Unknown automation action")
        async with self._lock:
            r = self._record(campaign_id)
            r["enabled"] = action == "resume"
            if action == "resume":
                # Explicit operator resumption can release an authentication
                # hold for queued jobs. The failed/ambiguous job stays held.
                r["supplier_blocks"] = {}
            if action == "pause":
                r["generation"] += 1
                for key, (_, task) in list(self._active.items()):
                    if key in r["jobs"]: task.cancel()
            self._save(r)
        return self.snapshot(campaign_id)

    async def _execute(self, campaign_id, job_id, generation):
        r = self._records[campaign_id]; job = r["jobs"][job_id]
        async def check_scope():
            if self._closing or not r["enabled"] or r["generation"] != generation:
                raise asyncio.CancelledError()
            self._check_association(job)
        try:
            await check_scope()
            profile = self._check_association(job)
            polling = job["pending_token"] is not None
            if polling and profile.poll is None: raise ValueError("No source-free pending reader")
            sequence = max((dispatch_sequence(j) for record in self._records.values()
                            for j in record["jobs"].values()), default=0) + 1
            if sequence > MAX_DISPATCH_SEQUENCE:
                raise ValueError("Dispatch sequence bound exceeded")
            job["last_dispatch_sequence"] = sequence
            job["status"] = "running"; job["updated_at"] = self.clock()
            job["polls" if polling else "attempts"] += 1
            self._save(r)  # persist intent before handing control to a source driver
            callback = profile.poll if polling else profile.execute
            result = await asyncio.wait_for(callback(deepcopy(job), check_scope), self.call_timeout)
            await check_scope()
            if not isinstance(result, dict): raise ValueError("Invalid profile receipt")
            state = result.get("state")
            if state == "pending":
                token = result.get("pending_token")
                if (profile.poll is None or not isinstance(token, dict) or not token or
                    set(token) - {"harvest_id", "candidate_id", "diagnosis_id", "job_id"} or
                    any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{32,64}", v) for v in token.values())):
                    raise ValueError("Invalid pending receipt")
                now = self.clock()
                began = now if job["wait_started_at"] is None else job["wait_started_at"]
                if now < began or now - began >= self.max_wait or job["polls"] >= self.max_polls:
                    job.update(status="held", reason="pending_limit_reached")
                else:
                    job.update(status="waiting", pending_token=deepcopy(token), wait_started_at=began, next_at=now + 2)
            elif state in {"completed", "continue"}:
                ref = result.get("result_reference")
                if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_./:#-]{1,500}", ref):
                    raise ValueError("Durable result reference required")
                if state == "continue":
                    progress, token = result.get("progress_count"), result.get("progress_token")
                    if (type(progress) is not int or progress <= job["progress_count"] or progress > 10_000_000 or
                        not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token) or
                        token == job["progress_token"] or job["continuations"] >= MAX_CONTINUATIONS):
                        raise ValueError("Durable advancing continuation required")
                    delay = result.get("delay_seconds", 0)
                    if (isinstance(delay, bool) or not isinstance(delay, (int, float)) or
                        not math.isfinite(delay) or not 0 <= delay <= 86400):
                        raise ValueError("Finite bounded continuation delay required")
                    deadline = self.clock() + delay
                    r["supplier_not_before"][job["supplier"]] = max(deadline,
                        r["supplier_not_before"].get(job["supplier"], 0))
                    job.update(status="queued", result_reference=ref, pending_token=None,
                        progress_count=progress, progress_token=token, next_at=deadline,
                        continuations=job["continuations"] + 1)
                else:
                    job.update(status="completed", reason=None, result_reference=ref, pending_token=None)
            elif state in {"held", "access_required"}:
                job.update(status=state, reason=access_reason(result.get("reason")) if state == "access_required" else safe_reason(result.get("reason")))
                if state == "access_required":
                    r["supplier_blocks"][job["supplier"]] = job["reason"]
            else: raise ValueError("Unsupported profile receipt")
        except asyncio.CancelledError:
            job.update(status="held", reason="worker_interrupted")
        except TimeoutError:
            job.update(status="held", reason="handler_timeout_unknown_effect")
        except Exception:
            # Exception text may contain source URLs or personal context. Never
            # persist it, and never classify unknown effects as retryable.
            job.update(status="held", reason="profile_failed")
        finally:
            job["updated_at"] = self.clock()
            self._save(r)

    async def tick(self):
        async with self._lock:
            for key, (supplier, task) in list(self._active.items()):
                if task.done():
                    try: task.result()
                    except BaseException:
                        cid = next(cid for cid, record in self._records.items() if key in record["jobs"])
                        self._worker_fault(cid, key)
                    self._active.pop(key); self._supplier_active.discard(supplier)
            if self._closing: return
            blocked = {supplier for r in self._records.values() for supplier in r["supplier_blocks"]}
            not_before = {}
            for r in self._records.values():
                for supplier, deadline in r["supplier_not_before"].items():
                    not_before[supplier] = max(not_before.get(supplier, 0), deadline)
            # Consider every enabled campaign together. A persisted sequence
            # gives other eligible products a turn after each finite chunk,
            # while the existing supplier/capacity/admission gates stay below.
            candidates = [(cid, r, job) for cid, r in self._records.items() if r["enabled"]
                          for job in r["jobs"].values() if job["status"] in {"queued", "waiting"}]
            for cid, r, job in sorted(candidates,
                    key=lambda item: (dispatch_sequence(item[2]), item[0], item[2]["id"])):
                if len(self._active) >= self.capacity: return
                if job["supplier"] in blocked: continue
                if not_before.get(job["supplier"], 0) > self.clock(): continue
                if job["id"] in self._active or job["supplier"] in self._supplier_active: continue
                if job["status"] not in {"queued", "waiting"}: continue
                if job["continuations"] >= MAX_CONTINUATIONS:
                    job.update(status="held", reason="continuation_limit_reached", updated_at=self.clock()); self._save(r); continue
                if job["status"] == "waiting" and (self.clock() < job["wait_started_at"] or
                    self.clock() - job["wait_started_at"] >= self.max_wait or job["polls"] >= self.max_polls):
                    job.update(status="held", reason="pending_limit_reached", updated_at=self.clock()); self._save(r); continue
                if job["next_at"] > self.clock(): continue
                task = asyncio.create_task(self._execute(cid, job["id"], r["generation"]))
                self._active[job["id"]] = (job["supplier"], task)
                self._supplier_active.add(job["supplier"])

    def _worker_fault(self, campaign_id, job_id=None):
        r = self._records[campaign_id]
        r["enabled"] = False; r["generation"] += 1
        r["worker_faults"] = (r["worker_faults"] + [{"job_id": job_id,
            "reason": "automation_storage_or_worker_failure", "at": self.clock()}])[-10:]
        for key, (_, task) in self._active.items():
            if key in r["jobs"] and not task.done(): task.cancel()
        if job_id in r["jobs"]:
            r["jobs"][job_id].update(status="held", reason="automation_storage_or_worker_failure")
        try: self._save(r)
        except Exception: pass  # Memory status still exposes failure; restart holds persisted running intent.

    async def open(self):
        if self._loop is not None: raise ValueError("Automation already started")
        async def dispatch():
            while not self._closing:
                try: await self.tick()
                except Exception:
                    # Persistence failure outside a worker is a visible global
                    # admission stop, never a silently dead background loop.
                    for cid in self._records: self._worker_fault(cid)
                await asyncio.sleep(0.1)
        self._loop = asyncio.create_task(dispatch())

    async def close(self):
        self._closing = True
        if self._loop is not None: self._loop.cancel()
        for _, task in self._active.values(): task.cancel()
        await asyncio.gather(*([self._loop] if self._loop else []),
                             *(t for _, t in self._active.values()), return_exceptions=True)
        self._active.clear(); self._supplier_active.clear()
        fcntl.flock(self._owner, fcntl.LOCK_UN); self._owner.close()
