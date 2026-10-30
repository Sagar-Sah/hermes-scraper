"""Durable monthly catalog coordination; supplier work is injected, never inferred.

The coordinator performs read-only catalog inventory and dispatches finite product
handlers. It does not quote suppliers, write WooCommerce, authorize a release, or
ask a model to calculate prices. A new month gets a new evidence namespace; a
restart never silently repeats an in-flight product handler.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import inspect
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

from . import timing


ZONE = ZoneInfo("America/Phoenix")
TERMINAL = {"verified", "mismatch", "needs_attention", "access_required", "failed", "held"}
ACTIVE_CYCLE = {"created", "inventorying", "ready", "running", "paused", "inventory_failed"}
SAFE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
ID = re.compile(r"^[a-f0-9]{32}$")
MAX_PAGES = 1000
MAX_PRODUCTS = 100000
OPERATOR_WINDOW_ENDED = "operator_time_window_ended"
DEFAULTS = {
    "enabled": False, "schedule_enabled": False,
    "site_url": "https://printitprints.com",
    "staging_url": "https://staging3.tonyl36.sg-host.com",
    "timezone": "America/Phoenix", "worker_capacity": 4, "batch_size": 99,
    "handler_timeout_seconds": 900,
    "director_enabled": False, "director_provider_id": None,
    "staging_repairs_enabled": False,
    "director_budget_usd": "25.00", "director_max_input_tokens": 8192,
    "director_max_output_tokens": 1024, "director_max_calls": 512,
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _reason(value, fallback="handler_needs_review"):
    return value if isinstance(value, str) and SAFE_REASON.fullmatch(value) else fallback


def _base_url(value):
    if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 33 for c in value):
        raise ValueError("invalid_site_url")
    p = urlsplit(value)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.port not in (None, 443) or p.path not in ("", "/") or p.query or p.fragment
            or p.hostname in {"localhost", "metadata.google.internal"} or p.hostname.endswith(".local")):
        raise ValueError("invalid_site_url")
    try:
        if not ipaddress.ip_address(p.hostname).is_global:
            raise ValueError("invalid_site_url")
    except ValueError as exc:
        if str(exc) == "invalid_site_url":
            raise
    return "https://" + p.netloc.lower()


def _public_url(value, base=None):
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 32 for c in value):
        return None
    p = urlsplit(value)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment:
        return None
    try:
        _base_url(p.scheme + "://" + p.netloc)
    except ValueError:
        return None
    if base and (p.scheme, p.netloc) != (urlsplit(base).scheme, urlsplit(base).netloc):
        return None
    return value


def _month(wall):
    return datetime.fromtimestamp(wall, ZONE).strftime("%Y-%m")


def monthly_due(wall):
    """The current calendar month's first day, 02:00 Phoenix time."""
    local = datetime.fromtimestamp(wall, ZONE)
    return local.replace(day=1, hour=2, minute=0, second=0, microsecond=0).timestamp()


def next_monthly_due(wall):
    local = datetime.fromtimestamp(wall, ZONE)
    due = monthly_due(wall)
    if wall < due:
        return due
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return datetime(year, month, 1, 2, tzinfo=ZONE).timestamp()


class AutonomousCompanion:
    """One private coordinator per Hermes data directory.

    product_handler(cycle, product) is async and returns a finite result with
    ``status`` (verified/mismatch/needs_attention/access_required/failed/continue).
    A continue receipt must advance both progress_count and a SHA256 progress_token.
    The handler receives cycle.evidence_namespace and must use it in source quote
    and write idempotency keys. Director reviews receive compact batch summaries;
    they are advisory and cannot advance a held product or approve a release.
    ``mapping_resolver`` may be set to a source-free sync/async inventory mapper.
    """

    def __init__(self, store, *, product_handler=None, director_review=None, fetch=None, clock=None):
        self.store = store
        self.root = Path(store.root) / "autonomous-companion"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("private_autonomy_directory_required")
        self.cycles_dir = self.root / "cycles"
        self.cycles_dir.mkdir(mode=0o700, exist_ok=True)
        info = self.cycles_dir.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("private_autonomy_directory_required")
        fd = os.open(self.root / ".owner", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._owner = os.fdopen(fd, "a+b")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self._owner.close()
            raise ValueError("autonomy_already_owned") from None
        self.clock = clock or time.time
        self.product_handler, self.director_review = product_handler, director_review
        self.fetch = fetch or self._fetch
        self.mapping_resolver = None
        self._lock = asyncio.Lock()
        self._active, self._review_tasks, self._inventory_tasks = {}, {}, {}
        self._loop = None
        self._closed = False
        self._session = uuid.uuid4().hex
        self.config_path = self.root / "config.json"
        self.config = self._validate_config({**DEFAULTS, **self._read(self.config_path, {})})
        self.cycles = {}
        for path in sorted(self.cycles_dir.glob("*.json")):
            record = self._read(path)
            if (record.get("schema") != "hermes-autonomous-companion-cycle/v1"
                    or not ID.fullmatch(path.stem) or record.get("id") != path.stem):
                raise ValueError("invalid_autonomy_cycle")
            self.cycles[path.stem] = record
            changed = False
            if record["status"] == "inventorying":
                record["status"] = "inventory_failed"
                record["inventory"].update(status="failed", complete=False, reason="inventory_interrupted")
                changed = True
            if record.get("kind") == "scheduled" and record["status"] in {"created", "ready"}:
                # No product dispatch occurred yet. Recover only this safe
                # catalog/start handoff; interrupted supplier intents stay held.
                record["scheduled_inventory_pending"] = True
                changed = True
            for job in record.get("products", []):
                if job["status"] == "running":
                    job.update(status="held", reason="handler_interrupted_unknown_effect", updated_at=self.clock())
                    self._sync_timing(record, job)
                    changed = True
            for review in record.get("director_reviews", {}).values():
                if review["status"] == "running":
                    review.update(status="held", reason="director_interrupted", updated_at=self.clock())
                    changed = True
            if record["status"] in ACTIVE_CYCLE and self._stop_at_expired(record):
                self._pause_at_cutoff(record)
                changed = True
            if changed:
                self._save_cycle(record)

    @staticmethod
    def _read(path, default=None):
        if not path.exists():
            if default is not None:
                return deepcopy(default)
            raise ValueError("autonomy_record_missing")
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("invalid_autonomy_record_permissions")
        if info.st_size > 100 * 1024 * 1024:
            raise ValueError("autonomy_record_too_large")
        return json.loads(path.read_text(), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_record")))

    @staticmethod
    def _atomic(path, value):
        data = value if isinstance(value, bytes) else _canonical(value).encode()
        if len(data) > 100 * 1024 * 1024:
            raise ValueError("autonomy_record_too_large")
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_config(value):
        if set(value) != set(DEFAULTS):
            raise ValueError("unknown_autonomy_setting")
        out = deepcopy(value)
        for key in ("enabled", "schedule_enabled", "director_enabled", "staging_repairs_enabled"):
            if type(out[key]) is not bool:
                raise ValueError("invalid_autonomy_boolean")
        for key in ("site_url", "staging_url"):
            out[key] = _base_url(out[key])
        if out["site_url"] == out["staging_url"]:
            raise ValueError("production_and_staging_must_differ")
        if out["timezone"] != "America/Phoenix" or type(out["batch_size"]) is not int or out["batch_size"] != 99:
            raise ValueError("fixed_monthly_schedule_or_batch_size")
        for key, minimum, maximum in (("worker_capacity", 1, 8), ("handler_timeout_seconds", 30, 3600),
                ("director_max_input_tokens", 256, 8192), ("director_max_output_tokens", 128, 2048),
                ("director_max_calls", 1, 512)):
            if type(out[key]) is not int or not minimum <= out[key] <= maximum:
                raise ValueError("invalid_autonomy_limit")
        provider = out["director_provider_id"]
        if provider is not None and (not isinstance(provider, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", provider)):
            raise ValueError("invalid_director_provider")
        try:
            amount = Decimal(out["director_budget_usd"])
            if type(out["director_budget_usd"]) is not str or not amount.is_finite() or not Decimal("0") <= amount <= Decimal("25.00"):
                raise ValueError()
            out["director_budget_usd"] = str(amount.quantize(Decimal("0.01")))
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError("invalid_director_budget") from None
        if out["director_enabled"] and (not provider or amount <= 0):
            raise ValueError("director_provider_and_budget_required")
        return out

    def configure(self, **changes):
        value = self._validate_config({**self.config, **changes})
        self._atomic(self.config_path, value)
        self.config = value
        return deepcopy(value)

    def _save_cycle(self, record):
        record["updated_at"] = self.clock()
        self._atomic(self.cycles_dir / (record["id"] + ".json"), record)

    @staticmethod
    def _inventory_pin(record):
        snapshot = {"baseline": record["baseline"], "scope": record.get("scope"), "inventory": record["inventory"],
            "products": [{k: p[k] for k in ("product_id", "storefront_url", "categories", "supplier", "source_url")}
                for p in record["products"]]}
        if "stop_at" in record:
            snapshot["stop_at"] = record["stop_at"]
        return _hash(snapshot)

    @staticmethod
    def _request_pin(record):
        return _hash({"kind": record["kind"], "created_at": record["created_at"],
            "scope": record.get("scope"), "stop_at": record.get("stop_at")})

    @classmethod
    def _check_pins(cls, record):
        if ("request_sha256" in record or "stop_at" in record
                or "exclude_product_ids" in (record.get("scope") or {})):
            if cls._request_pin(record) != record.get("request_sha256"):
                raise ValueError("cycle_request_changed")
        if _hash(record["baseline"]) != record["baseline_sha256"]:
            raise ValueError("cycle_baseline_changed")
        if _hash(record["config"]) != record.get("config_sha256"):
            raise ValueError("cycle_config_changed")
        if record["inventory"].get("complete") and cls._inventory_pin(record) != record.get("inventory_baseline_sha256"):
            raise ValueError("cycle_inventory_mapping_changed")

    def _stop_at_expired(self, record):
        stop = record.get("stop_at")
        return type(stop) in (int, float) and math.isfinite(stop) and self.clock() >= stop

    def _bounded_timeout(self, record, seconds):
        stop = record.get("stop_at")
        return min(seconds, max(0, stop - self.clock())) if type(stop) in (int, float) and math.isfinite(stop) else seconds

    def _pause_at_cutoff(self, record):
        """Fence admission before cancelling only this cycle's owned work.

        An owned coroutine may not have entered its try/finally yet. Persist
        its unknown-outcome hold here as well as retaining the normal worker
        cancellation handlers. Never cancel the caller (inventory/worker/tick).
        """
        if not self._stop_at_expired(record):
            return False
        identifier = record["id"]
        owned = [task for (cid, _), task in self._active.items() if cid == identifier]
        owned += [task for (cid, _), task in self._review_tasks.items() if cid == identifier]
        if identifier in self._inventory_tasks:
            owned.append(self._inventory_tasks[identifier])
        if (record["status"] == "paused" and record.get("pause_reason") == OPERATOR_WINDOW_ENDED
                and record.get("fault") == OPERATOR_WINDOW_ENDED and not any(not task.done() for task in owned)):
            return True
        changed = record["status"] != "paused" or record.get("pause_reason") != OPERATOR_WINDOW_ENDED
        if record["status"] == "inventorying" or identifier in self._inventory_tasks:
            record["inventory_pause_requested"] = True
        record.update(status="paused", fault=OPERATOR_WINDOW_ENDED, pause_reason=OPERATOR_WINDOW_ENDED)
        record.setdefault("cutoff_reached_at", self.clock())
        record.pop("scheduled_inventory_pending", None)
        for job in record.get("products", []):
            if job["status"] == "running":
                job.update(status="held", reason="handler_interrupted_unknown_effect", ended_at=self.clock(), updated_at=self.clock())
                changed = True
            self._sync_timing(record, job)
        for review in record.get("director_reviews", {}).values():
            if review["status"] == "running":
                review.update(status="held", reason="director_interrupted", updated_at=self.clock())
                changed = True
        try:
            caller = asyncio.current_task()
        except RuntimeError:
            caller = None
        # Persist the fence and holds before delivering cancellation.
        if changed:
            self._save_cycle(record)
        for task in set(owned):
            if task is not caller and not task.done() and not task.cancelling():
                task.cancel()
        return True

    def _cycle(self, cycle_id):
        if not isinstance(cycle_id, str) or not ID.fullmatch(cycle_id) or cycle_id not in self.cycles:
            raise KeyError("autonomy_cycle_not_found")
        return self.cycles[cycle_id]

    def _active_cycle(self):
        return next((c for c in self.cycles.values() if c["status"] in ACTIVE_CYCLE), None)

    def _supersession_block(self, record):
        """Only a never-started inventory may be retired for a fresh plan.

        Inventory response pages are historical evidence and are retained. Any
        handler intent, result, paid review or product-work artifact fences this
        operation; retiring an inventory must never become a retry mechanism.
        """
        identifier = record["id"]
        if record["status"] not in {"ready", "inventory_failed"}:
            return "only_unstarted_inventory_can_be_superseded"
        try:
            self._check_pins(record)
        except ValueError:
            return "cycle_snapshot_requires_review"
        if (record.get("started_at") is not None or record.get("finished_at") is not None
                or record.get("dispatch_sequence") != 0 or record.get("director_reviews")
                or record.get("supplier_blocks") or record.get("supplier_not_before")):
            return "cycle_work_history_prevents_supersession"
        if (identifier in self._inventory_tasks
                or any(cid == identifier for cid, _ in self._active)
                or any(cid == identifier for cid, _ in self._review_tasks)):
            return "cycle_worker_ownership_prevents_supersession"
        for job in record.get("products", []):
            if (job.get("status") != "queued" or job.get("attempts") != 0
                    or job.get("last_dispatch_sequence") != 0 or job.get("progress_count") != 0
                    or job.get("progress_token") is not None or job.get("started_at") is not None
                    or job.get("ended_at") is not None or job.get("result") is not None
                    or job.get("repair_status") is not None or job.get("reason") is not None
                    or job.get("not_before") != 0):
                return "cycle_work_history_prevents_supersession"
        for kind in ("product-work", "shipping-models"):
            path = self.root / kind / identifier
            # Read-only result inspection can create an empty product-work
            # directory. Files or symlinks, including uncertain dispatch
            # journals, must remain attached to a held cycle instead.
            if path.parent.is_symlink() or path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
                return "cycle_operation_artifacts_prevent_supersession"
        return None

    async def supersede(self, cycle_id, *, expected_revision_sha256, expected_inventory_sha256, reason):
        """Retain the pinned inventory and free a separate, explicit replan.

        No replacement is dispatched or even created here. The caller must
        create/inventory a new cycle, obtaining a new evidence namespace and
        the then-current reviewed mappings and environment baseline.
        """
        if (not isinstance(expected_revision_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_revision_sha256)
                or (expected_inventory_sha256 is not None and (not isinstance(expected_inventory_sha256, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", expected_inventory_sha256)))
                or not isinstance(reason, str) or not SAFE_REASON.fullmatch(reason)):
            raise ValueError("invalid_cycle_supersession_request")
        async with self._lock:
            record = self._cycle(cycle_id)
            if (_hash(record) != expected_revision_sha256
                    or _hash(self._read(self.cycles_dir / (cycle_id + ".json"))) != expected_revision_sha256):
                raise ValueError("cycle_revision_changed")
            if record.get("inventory_baseline_sha256") != expected_inventory_sha256:
                raise ValueError("cycle_inventory_revision_changed")
            self._check_pins(record)
            blocked = self._supersession_block(record)
            if blocked:
                raise ValueError(blocked)
            now = self.clock()
            updated = deepcopy(record)
            updated["supersession"] = {"reason": reason, "actor": "operator", "superseded_at": now,
                "previous_status": record["status"], "previous_revision_sha256": expected_revision_sha256,
                "inventory_sha256": expected_inventory_sha256, "replacement_created": False}
            updated["status"], updated["finished_at"] = "superseded", now
            updated.pop("scheduled_inventory_pending", None)
            self._save_cycle(updated)
            self.cycles[cycle_id] = updated
            return self.get_cycle(cycle_id)

    def create_cycle(self, *, kind="manual", baseline=None, scheduled_month=None, category_slugs=None, product_ids=None,
            exclude_product_ids=None, stop_at=None):
        if kind not in {"manual", "scheduled"}:
            raise ValueError("invalid_cycle_kind")
        if kind == "scheduled" and (category_slugs is not None or product_ids is not None or exclude_product_ids is not None):
            raise ValueError("scheduled_scope_must_cover_all_catalog")
        now = self.clock()
        if stop_at is not None and (kind != "manual" or type(stop_at) not in (int, float)
                or not math.isfinite(stop_at) or not now < stop_at <= now + 86400):
            raise ValueError("invalid_manual_cycle_stop_at")
        for values, limit, pattern in ((category_slugs, 10, r"[a-z0-9][a-z0-9-]{0,99}"),
                (product_ids, 1000, r"[1-9][0-9]{0,18}"), (exclude_product_ids, 99, r"[1-9][0-9]{0,18}")):
            if values is not None and (not isinstance(values, list) or not 1 <= len(values) <= limit
                    or any(not isinstance(v, str) or not re.fullmatch(pattern, v) for v in values)
                    or len(values) != len(set(values))):
                raise ValueError("invalid_manual_catalog_scope")
        if set(product_ids or []) & set(exclude_product_ids or []):
            raise ValueError("manual_catalog_scope_overlap")
        if self._active_cycle() is not None:
            raise ValueError("autonomy_cycle_already_active")
        if kind == "scheduled":
            scheduled_month = scheduled_month or _month(self.clock())
            if scheduled_month != _month(self.clock()):
                raise ValueError("invalid_scheduled_month")
            if any(c.get("scheduled_month") == scheduled_month for c in self.cycles.values()):
                raise ValueError("monthly_cycle_already_exists")
        elif scheduled_month is not None:
            raise ValueError("manual_cycle_cannot_claim_schedule")
        baseline = deepcopy(baseline) if baseline is not None else {
            "production": {"url": self.config["site_url"], "status": "unknown"},
            "staging": {"url": self.config["staging_url"], "status": "unknown"},
        }
        if not isinstance(baseline, dict) or len(_canonical(baseline)) > 100000:
            raise ValueError("invalid_cycle_baseline")
        now, identifier = self.clock(), uuid.uuid4().hex
        record = {"schema": "hermes-autonomous-companion-cycle/v1", "id": identifier,
            "kind": kind, "scheduled_month": scheduled_month, "status": "created",
            "created_at": now, "updated_at": now, "started_at": None, "finished_at": None,
            "baseline": baseline, "baseline_sha256": _hash(baseline), "config": deepcopy(self.config),
            "config_sha256": _hash(self.config),
            "evidence_namespace": "companion-cycle/" + identifier,
            "scope": {"category_slugs": deepcopy(category_slugs), "product_ids": deepcopy(product_ids)},
            "inventory": {"status": "pending", "count": 0, "pages": 0, "category_pages": 0, "complete": False},
            "products": [], "supplier_blocks": {}, "supplier_not_before": {},
            "dispatch_sequence": 0, "director_reviews": {}, "fault": None}
        if exclude_product_ids is not None:
            record["scope"]["exclude_product_ids"] = deepcopy(exclude_product_ids)
        if stop_at is not None:
            record["stop_at"] = stop_at
        if stop_at is not None or exclude_product_ids is not None:
            record["request_sha256"] = self._request_pin(record)
        self.cycles[identifier] = record
        self._save_cycle(record)
        return self.get_cycle(identifier)

    async def _fetch(self, url):
        from .transport import validate_url
        from .tls import tls_context
        await validate_url(url)
        async with httpx.AsyncClient(verify=tls_context(), timeout=30, follow_redirects=False) as client:
            response = await client.get(url)
        return response.status_code, str(response.url), response.content, dict(response.headers)

    async def _pages(self, base, resource, *, evidence=None, evidence_dir=None, cycle=None):
        items, seen_pages, observed_pages, duplicate_ids = {}, set(), 0, 0
        expected_total = None
        for page in range(1, MAX_PAGES + 1):
            if cycle is not None and self._pause_at_cutoff(cycle):
                raise ValueError(OPERATOR_WINDOW_ENDED)
            query = {"per_page": 100, "page": page, "orderby": "id", "order": "asc"}
            if resource == "products/categories":
                query["hide_empty"] = "false"
                # The Store API category route supports name/slug/count, not
                # product orderby=id. Slugs provide a stable unique order.
                query["orderby"] = "slug"
            url = base + "/wp-json/wc/store/v1/" + resource + "?" + urlencode(query)
            result = await self.fetch(url)
            if not isinstance(result, (tuple, list)) or len(result) not in (3, 4):
                raise ValueError("invalid_catalog_transport")
            status, final, raw = result[:3]
            raw_headers = result[3] if len(result) == 4 else {}
            if (type(status) is not int or not 100 <= status <= 599
                    or not isinstance(raw, (str, bytes)) or not isinstance(raw_headers, dict)):
                raise ValueError("invalid_catalog_transport")
            headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
            raw_bytes = raw.encode() if isinstance(raw, str) else raw
            if final != url or len(raw_bytes) > 16 * 1024 * 1024:
                raise ValueError("catalog_redirect_or_size_limit")
            if evidence is not None and evidence_dir is not None:
                digest = hashlib.sha256(raw_bytes).hexdigest()
                name = resource.replace("/", "-") + "-" + str(page) + "-" + digest + ".json"
                self._atomic(evidence_dir / name, raw_bytes)
                evidence.append({"resource": resource, "page": page, "status": status,
                    "path": str((evidence_dir / name).relative_to(self.root)), "sha256": digest,
                    "bytes": len(raw_bytes)})
            # Preserve the bounded same-origin response before interpreting
            # it. A server's HTML failure must remain an HTTP failure, not a
            # generic JSON parser error with its evidence discarded.
            if status != 200:
                if status == 400 and observed_pages:
                    try:
                        end = json.loads(raw_bytes)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        end = None
                    if isinstance(end, dict) and end.get("code") in {
                            "rest_post_invalid_page_number", "woocommerce_rest_invalid_page_number"}:
                        break
                raise ValueError("catalog_page_http_" + str(status))
            try:
                data = json.loads(raw_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ValueError("catalog_page_invalid_json") from None
            if not isinstance(data, list) or len(data) > 100:
                raise ValueError("catalog_page_unavailable")
            if "x-wp-total" in headers:
                total = headers["x-wp-total"]
                if not total.isdecimal() or int(total) > MAX_PRODUCTS:
                    raise ValueError("catalog_total_invalid")
                if expected_total is not None and expected_total != int(total):
                    raise ValueError("catalog_changed_during_inventory")
                expected_total = int(total)
            if not data:
                break
            observed_pages += 1
            page_ids = []
            for item in data:
                if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 1:
                    raise ValueError("invalid_catalog_identity")
                pid = str(item["id"])
                page_ids.append(pid)
                if pid in items:
                    duplicate_ids += 1
                    if items[pid] != item:
                        raise ValueError("catalog_identity_changed")
                items[pid] = item
            fingerprint = _hash(page_ids)
            if fingerprint in seen_pages:
                raise ValueError("catalog_pagination_not_advancing")
            seen_pages.add(fingerprint)
            if len(items) > MAX_PRODUCTS:
                raise ValueError("catalog_limit_reached")
            # Continue past short pages; API page-size caps must not truncate an inventory.
        else:
            raise ValueError("catalog_page_limit_reached")
        if expected_total is not None and expected_total != len(items):
            raise ValueError("catalog_total_does_not_match_inventory")
        return list(items.values()), observed_pages, duplicate_ids

    async def inventory(self, cycle_id):
        async with self._lock:
            record = self._cycle(cycle_id)
            self._check_pins(record)
            if self._pause_at_cutoff(record):
                return self.get_cycle(cycle_id)
            if record["inventory"].get("complete"):
                return self.get_cycle(cycle_id)
            if record["status"] not in {"created", "inventory_failed", "paused"}:
                raise ValueError("cycle_inventory_not_available")
            record["status"] = "inventorying"
            record.pop("inventory_pause_requested", None)
            record["inventory"]["status"] = "running"
            self._save_cycle(record)
            self._inventory_tasks[cycle_id] = asyncio.current_task()
        evidence = []
        try:
            base = record["config"]["site_url"]
            evidence_dir = self.root / "inventory-pages" / cycle_id / uuid.uuid4().hex
            evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            categories, category_pages, category_duplicates = await self._pages(base, "products/categories", evidence=evidence, evidence_dir=evidence_dir, cycle=record)
            products, product_pages, product_duplicates = await self._pages(base, "products", evidence=evidence, evidence_dir=evidence_dir, cycle=record)
            category_map = {str(c["id"]): c for c in categories}
            for category in categories:
                if (type(category.get("parent")) is not int or category["parent"] < 0
                        or not isinstance(category.get("slug"), str) or not category["slug"]
                        or (category["parent"] and str(category["parent"]) not in category_map)):
                    raise ValueError("catalog_category_coverage_incomplete")
                parent, visited = str(category["id"]), set()
                while parent != "0":
                    if parent in visited or len(visited) >= 128:
                        raise ValueError("catalog_category_hierarchy_invalid")
                    visited.add(parent)
                    parent = str(category_map[parent]["parent"])
            standard = {str(c["id"]) for c in categories if c.get("slug") == "standard-print"}
            changed = True
            while changed:
                previous = set(standard)
                standard.update(str(c["id"]) for c in categories if str(c.get("parent", "")) in standard)
                changed = previous != standard
            scope = record.get("scope") or {}
            selected_categories = None
            if scope.get("category_slugs"):
                available = {str(c.get("slug")): str(c["id"]) for c in categories}
                if any(slug not in available for slug in scope["category_slugs"]):
                    raise ValueError("catalog_requested_category_missing")
                if any(sum(c.get("slug") == slug for c in categories) != 1 for slug in scope["category_slugs"]):
                    raise ValueError("catalog_requested_category_ambiguous")
                selected_categories = {available[slug] for slug in scope["category_slugs"]}
                changed = True
                while changed:
                    previous = set(selected_categories)
                    selected_categories.update(str(c["id"]) for c in categories if str(c.get("parent", "")) in selected_categories)
                    changed = previous != selected_categories
            selected_ids = set(scope["product_ids"]) if scope.get("product_ids") else None
            if selected_ids is not None and not selected_ids.issubset({str(p["id"]) for p in products}):
                raise ValueError("catalog_requested_product_missing")
            excluded_ids = set(scope.get("exclude_product_ids") or [])
            if not excluded_ids.issubset({str(p["id"]) for p in products}):
                raise ValueError("catalog_excluded_product_missing")
            rows, now = [], self.clock()
            for product in products:
                if self._pause_at_cutoff(record):
                    raise ValueError(OPERATOR_WINDOW_ENDED)
                pid = str(product["id"])
                url = _public_url(product.get("permalink"), base)
                if url is None:
                    raise ValueError("catalog_product_url_unresolved")
                cats = product.get("categories", [])
                if not isinstance(cats, list) or any(not isinstance(c, dict) or type(c.get("id")) is not int for c in cats):
                    raise ValueError("catalog_product_categories_invalid")
                if any(str(c["id"]) not in category_map for c in cats):
                    raise ValueError("catalog_category_coverage_incomplete")
                if selected_ids is not None and pid not in selected_ids:
                    continue
                if pid in excluded_ids:
                    continue
                if selected_categories is not None and not any(str(c["id"]) in selected_categories for c in cats):
                    continue
                item = {"product_id": pid, "title": str(product.get("name", ""))[:300],
                    "storefront_url": url, "categories": [{"id": c["id"], "slug": str(category_map[str(c["id"])].get("slug", "")),
                        "name": str(c.get("name", ""))[:200]} for c in cats],
                    "standard_print_priority": any(str(c["id"]) in standard for c in cats),
                    "supplier": None, "source_url": None, "mapping_status": "unknown"}
                if self.mapping_resolver is not None:
                    mapping = self.mapping_resolver(deepcopy(item))
                    if inspect.isawaitable(mapping):
                        mapping = await mapping
                    if mapping:
                        if not isinstance(mapping, dict):
                            raise ValueError("invalid_inventory_mapping")
                        # An explicit unresolved mapping is a useful exception,
                        # not a malformed URL that aborts the whole catalog.
                        source_url = (_public_url(mapping["source_url"])
                                      if mapping.get("source_url") and mapping.get("mapping_status") != "unknown" else None)
                        supplier = mapping.get("supplier")
                        if source_url and isinstance(supplier, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", supplier):
                            canonical_supplier = {"fourover": "4over", "digital-print-solutions": "dps"}.get(supplier.lower(), supplier.lower())
                            item.update(source_url=source_url, supplier=canonical_supplier, mapping_status="mapped")
                        else:
                            item["mapping_reason"] = _reason(mapping.get("reason"), "exact_supplier_mapping_required")
                item.update(id=pid, status="queued", reason=None, result=None, repair_status=None,
                    created_at=now, updated_at=now, started_at=None, ended_at=None,
                    attempts=0, progress_count=0, progress_token=None, last_dispatch_sequence=0, not_before=0,
                    timing=timing.new_record("queued", now, time.monotonic(), self._session, created_at=now, complete=True))
                rows.append(item)
            rows.sort(key=lambda p: (not p["standard_print_priority"], int(p["product_id"])))
            for ordinal, row in enumerate(rows):
                row.update(ordinal=ordinal, batch_index=ordinal // 99)
            inventory = {"status": "complete", "complete": True, "count": len(rows),
                "catalog_count": len(products), "selected_count": len(rows),
                "pages": product_pages, "category_pages": category_pages,
                "duplicate_product_ids": product_duplicates, "duplicate_category_ids": category_duplicates,
                "observed_at": now, "snapshot_sha256": _hash({"categories": categories, "products": products}),
                "category_ids": sorted(category_map, key=int),
                "selected_category_ids": sorted(selected_categories, key=int) if selected_categories is not None else None,
                "evidence_pages": evidence}
            async with self._lock:
                if self._pause_at_cutoff(record):
                    raise ValueError(OPERATOR_WINDOW_ENDED)
                record["products"], record["inventory"], record["status"] = rows, inventory, "ready"
                record["inventory_baseline_sha256"] = self._inventory_pin(record)
                self._save_cycle(record)
        except BaseException as exc:
            async with self._lock:
                interrupted = isinstance(exc, asyncio.CancelledError)
                cutoff = self._stop_at_expired(record)
                if cutoff:
                    self._pause_at_cutoff(record)
                paused = record.get("inventory_pause_requested") is True or cutoff
                record["status"] = "paused" if paused else "created" if interrupted and record["kind"] == "scheduled" else "inventory_failed"
                record["inventory"].update(status="failed", complete=False,
                    evidence_pages=evidence, response_pages=len(evidence),
                    reason=OPERATOR_WINDOW_ENDED if cutoff else "inventory_paused" if paused else "inventory_interrupted" if interrupted else
                        _reason(str(exc), "catalog_inventory_failed") if isinstance(exc, ValueError) else "catalog_inventory_failed")
                if interrupted and record["kind"] == "scheduled" and not paused:
                    record["scheduled_inventory_pending"] = True
                self._save_cycle(record)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self._inventory_tasks.pop(cycle_id, None)
        return self.get_cycle(cycle_id)

    def _sync_timing(self, record, job, owned=False):
        state = "active" if owned else "finished" if job["status"] in TERMINAL else "paused" if record["status"] == "paused" else "waiting" if job.get("not_before", 0) > self.clock() else "queued"
        if job["status"] in TERMINAL and job["status"] != "verified":
            state = "attention"
        job["timing"] = timing.advance(job.get("timing"), state, self.clock(), time.monotonic(), self._session,
            created_at=job.get("created_at"), waiting_until=job.get("not_before") if state == "waiting" else None)

    def _summary(self, record):
        counts = {state: sum(p["status"] == state for p in record["products"]) for state in ("queued", "running", *sorted(TERMINAL))}
        counts["total"] = len(record["products"])
        counts["terminal"] = sum(counts[s] for s in TERMINAL)
        return counts

    def _ordered_pending(self, record):
        """Fill windows fairly without making one source occupy idle capacity."""
        running, groups, deferred = [], {}, []
        wall = self.clock()
        for job in record["products"]:
            if job["status"] in TERMINAL:
                continue
            if job["status"] == "running":
                running.append(job)
                continue
            source = job["supplier"] or "unmapped"
            if (source in record["supplier_blocks"] or
                    max(job.get("not_before", 0), record["supplier_not_before"].get(source, 0)) > wall):
                deferred.append(job)
            else:
                groups.setdefault(source, []).append(job)
        sources = sorted(groups, key=lambda source: groups[source][0]["ordinal"])
        ordered = running[:]
        offset = 0
        while any(offset < len(groups[source]) for source in sources):
            for source in sources:
                if offset < len(groups[source]):
                    ordered.append(groups[source][offset])
            offset += 1
        return ordered + deferred

    def get_cycle(self, cycle_id, *, compact=False):
        record = deepcopy(self._cycle(cycle_id))
        record["revision_sha256"] = _hash(record)
        record["can_supersede"] = self._supersession_block(record) is None
        for job in record["products"]:
            task = self._active.get((cycle_id, job["product_id"]))
            owned = task is not None and not task.done() and job["status"] == "running"
            state = "active" if owned else "finished" if job["status"] == "verified" else "attention" if job["status"] in TERMINAL else "paused" if record["status"] == "paused" else "waiting" if job.get("not_before", 0) > self.clock() else "queued"
            job["timing"] = timing.snapshot(job.get("timing"), state, self.clock(), time.monotonic(), self._session,
                live=owned, created_at=job["created_at"], ended_at=job.get("ended_at"), scope="monthly_audit_product")
        record["summary"] = self._summary(record)
        pending = [] if record["status"] == "superseded" else self._ordered_pending(record)
        record["working_window"] = deepcopy(pending[:99])
        record["next_window"] = deepcopy(pending[99:198])
        record["progress"] = {"completed": record["summary"]["terminal"], "total": len(record["products"]),
            "percent": round(100 * record["summary"]["terminal"] / len(record["products"]), 2) if record["products"] else 0}
        record["timing"] = {"elapsed_seconds": max(0, (record.get("finished_at") or self.clock()) - record["created_at"]),
            "active_seconds": round(sum((p["timing"].get("active_seconds") or 0) for p in record["products"]), 3)}
        record["director"] = {"status": "disabled" if not record["config"]["director_enabled"] else "running" if any(r["status"] == "running" for r in record["director_reviews"].values()) else "idle",
            "reviews": len(record["director_reviews"]), "failures": sum(r["status"] != "completed" for r in record["director_reviews"].values())}
        if compact:
            record.pop("products")
            for key in ("working_window", "next_window"):
                record[key] = [{k: p[k] for k in ("product_id", "title", "supplier", "status", "reason", "repair_status", "timing")} for p in record[key]]
        return record

    def list_cycles(self):
        return [{**{key: deepcopy(c[key]) for key in ("id", "kind", "scheduled_month", "status", "created_at", "updated_at",
                "started_at", "finished_at", "baseline_sha256", "inventory", "fault")},
                **({"stop_at": c["stop_at"]} if "stop_at" in c else {}), "summary": self._summary(c)}
            for c in sorted(self.cycles.values(), key=lambda c: c["created_at"], reverse=True)]

    def status(self):
        active = self._active_cycle()
        return {"config": deepcopy(self.config), "enabled": self.config["enabled"],
            "schedule_enabled": self.config["schedule_enabled"], "next_run_at": next_monthly_due(self.clock()),
            "active_cycle_id": active["id"] if active else None,
            "cycle": self.get_cycle(active["id"], compact=True) if active else None,
            "capabilities": {"batch_size": 99, "next_batch_size": 99, "per_supplier_active_limit": 1,
                "monthly_schedule": "1st day at 02:00 America/Phoenix", "manual_run_available": True,
                "automatic_production_release": False, "unknown_is_green": False,
                "handler_registered": callable(self.product_handler), "director_registered": callable(self.director_review)}}

    async def start(self, cycle_id):
        async with self._lock:
            record = self._cycle(cycle_id)
            if self._stop_at_expired(record):
                self._check_pins(record)
                self._pause_at_cutoff(record)
                return self.get_cycle(cycle_id)
            if not record["inventory"].get("complete") or record["status"] not in {"ready", "paused"}:
                raise ValueError("complete_inventory_required")
            if not callable(self.product_handler):
                raise ValueError("product_handler_not_configured")
            self._check_pins(record)
            record["status"] = "running"
            record["started_at"] = record["started_at"] or self.clock()
            for job in record["products"]:
                self._sync_timing(record, job)
            self._save_cycle(record)
        return self.get_cycle(cycle_id)

    async def pause(self, cycle_id):
        async with self._lock:
            record = self._cycle(cycle_id)
            if record["status"] not in {"running", "inventorying"}:
                raise ValueError("cycle_not_running")
            if record["status"] == "inventorying":
                record["inventory_pause_requested"] = True
            record["status"] = "paused"
            tasks = [task for (cid, _), task in self._active.items() if cid == cycle_id]
            if cycle_id in self._inventory_tasks:
                tasks.append(self._inventory_tasks[cycle_id])
            for task in tasks:
                task.cancel()
            for job in record["products"]:
                if job["status"] == "queued":
                    self._sync_timing(record, job)
            self._save_cycle(record)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return self.get_cycle(cycle_id)

    async def resume(self, cycle_id, *, supplier=None):
        async with self._lock:
            record = self._cycle(cycle_id)
            if self._stop_at_expired(record):
                self._check_pins(record)
                self._pause_at_cutoff(record)
                return self.get_cycle(cycle_id)
            if supplier is not None:
                if not isinstance(supplier, str) or supplier not in record["supplier_blocks"]:
                    raise ValueError("unknown_supplier_hold")
                active = self._active_cycle()
                if active and active["id"] != cycle_id:
                    raise ValueError("autonomy_cycle_already_active")
                # Only queued work is released. The interrupted/access-required
                # selection is retained for explicit evidence-backed recovery.
                del record["supplier_blocks"][supplier]
                for job in record["products"]:
                    if (job["status"] == "access_required" and job.get("blocked_by_product_id")
                            and (job["supplier"] or "unmapped") == supplier and job["attempts"] == 0):
                        job.update(status="queued", reason=None, ended_at=None)
                        job.pop("blocked_by_product_id", None)
            if record["status"] not in {"paused", "running", "completed_with_attention"} or not record["inventory"].get("complete"):
                raise ValueError("cycle_not_resumable")
            self._check_pins(record)
            record["status"] = "running"
            record["finished_at"] = None
            for job in record["products"]:
                self._sync_timing(record, job, owned=(cycle_id, job["product_id"]) in self._active)
            self._save_cycle(record)
        return self.get_cycle(cycle_id)

    @staticmethod
    def _result(raw):
        if not isinstance(raw, dict) or len(_canonical(raw)) > 200000:
            raise ValueError("invalid_product_receipt")
        result = deepcopy(raw)
        status = result.get("status")
        if status not in TERMINAL | {"continue"}:
            raise ValueError("invalid_product_receipt_status")
        if status == "verified":
            checks, coverage = result.get("checks"), result.get("coverage")
            passed = {"match", "passed", "verified"}
            complete = isinstance(coverage, dict) and coverage.get("complete") is True and coverage.get("fresh") is True
            evidence = result.get("evidence_ids")
            required = result.get("required_checks")
            if not (isinstance(checks, dict) and checks and complete and isinstance(evidence, list) and evidence
                    and all(isinstance(v, str) and v for v in evidence)
                    and isinstance(required, list) and required and all(isinstance(k, str) and k in checks for k in required)
                    and len(required) == len(set(required))
                    and all((value in passed if isinstance(value, str) else isinstance(value, dict)
                        and value.get("status") in passed and value.get("complete") is True
                        and bool(value.get("evidence_ids"))) for value in checks.values())):
                result.update(status="needs_attention", reason="incomplete_verification_evidence")
        result["evidence_complete"] = result["status"] == "verified"
        result["reason"] = _reason(result.get("reason"), "checks_passed" if result["status"] == "verified" else "handler_needs_review")
        return result

    async def _execute(self, record, job):
        try:
            self._check_pins(record)
            if self._pause_at_cutoff(record):
                return
            raw = await asyncio.wait_for(self.product_handler(self.get_cycle(record["id"], compact=True), deepcopy(job)),
                timeout=self._bounded_timeout(record, record["config"]["handler_timeout_seconds"]))
            if self._pause_at_cutoff(record):
                return
            result = self._result(raw)
            status = result["status"]
            if status == "continue":
                count, token = result.get("progress_count"), result.get("progress_token")
                delay = result.get("retry_after_seconds", 0)
                if (type(count) is not int or not job["progress_count"] < count <= 10000000
                        or not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token)
                        or token == job["progress_token"] or type(delay) not in (int, float)
                        or not math.isfinite(delay) or not 0 <= delay <= 86400):
                    raise ValueError("nonadvancing_continuation")
                job.update(status="queued", progress_count=count, progress_token=token, not_before=self.clock() + delay)
                if job["supplier"]:
                    record["supplier_not_before"][job["supplier"]] = max(record["supplier_not_before"].get(job["supplier"], 0), job["not_before"])
            else:
                job.update(status=status, ended_at=self.clock())
            job.update(result=result, reason=result["reason"], repair_status=result.get("repair_status"))
            if status == "access_required":
                supplier = job["supplier"] or "unmapped"
                record["supplier_blocks"][supplier] = result["reason"]
                for other in record["products"]:
                    if other["status"] == "queued" and (other["supplier"] or "unmapped") == supplier:
                        other.update(status="access_required", reason=result["reason"],
                            blocked_by_product_id=job["product_id"], ended_at=self.clock(), updated_at=self.clock())
                        self._sync_timing(record, other)
        except asyncio.CancelledError:
            job.update(status="held", reason="handler_interrupted_unknown_effect", ended_at=self.clock())
        except TimeoutError:
            if not self._pause_at_cutoff(record):
                job.update(status="held", reason="handler_timeout_unknown_effect", ended_at=self.clock())
        except Exception:
            job.update(status="held", reason="handler_failed_unknown_effect", ended_at=self.clock())
        finally:
            job["updated_at"] = self.clock()
            self._sync_timing(record, job)
            self._save_cycle(record)

    async def _review(self, record, batch_index, jobs):
        review = record["director_reviews"][str(batch_index)]
        try:
            self._check_pins(record)
            if self._pause_at_cutoff(record):
                return
            digest = {"cycle_id": record["id"], "batch_index": batch_index, "products": [
                {k: p[k] for k in ("product_id", "supplier", "status", "reason", "repair_status")} for p in jobs],
                "counts": {s: sum(p["status"] == s for p in jobs) for s in TERMINAL},
                "advisory_only": True}
            context = {"id": record["id"], "config": deepcopy(record["config"]),
                "baseline_sha256": record["baseline_sha256"], "evidence_namespace": record["evidence_namespace"]}
            if "stop_at" in record:
                context["stop_at"] = record["stop_at"]
            raw = await asyncio.wait_for(self.director_review(context, digest), timeout=self._bounded_timeout(record, 90))
            if self._pause_at_cutoff(record):
                return
            if not isinstance(raw, dict) or len(_canonical(raw)) > 50000:
                raise ValueError("invalid_director_review")
            review.update(status="completed", result=deepcopy(raw), updated_at=self.clock())
        except asyncio.CancelledError:
            review.update(status="held", reason="director_interrupted", updated_at=self.clock())
        except Exception:
            if not self._pause_at_cutoff(record):
                review.update(status="failed", reason="director_unavailable", updated_at=self.clock())
        finally:
            self._save_cycle(record)

    def _maybe_reviews(self, record):
        if self._pause_at_cutoff(record) or not record["config"]["director_enabled"] or not callable(self.director_review):
            return
        batches = sorted({p["batch_index"] for p in record["products"]})
        for index in batches:
            if self._pause_at_cutoff(record):
                break
            jobs = [p for p in record["products"] if p["batch_index"] == index]
            if str(index) in record["director_reviews"] or not jobs or any(p["status"] not in TERMINAL for p in jobs):
                continue
            if len(record["director_reviews"]) >= record["config"]["director_max_calls"]:
                break
            record["director_reviews"][str(index)] = {"status": "running", "created_at": self.clock(), "updated_at": self.clock()}
            self._save_cycle(record)
            self._review_tasks[(record["id"], index)] = asyncio.create_task(self._review(record, index, deepcopy(jobs)))

    async def tick(self, cycle_id=None):
        async with self._lock:
            requested = self._cycle(cycle_id) if cycle_id is not None else None
            active_cycle = self._active_cycle()
            if requested is not None and active_cycle is not None and requested["id"] != active_cycle["id"]:
                raise ValueError("autonomy_cycle_not_active")
            for record in self.cycles.values():
                identifier = record["id"]
                owned = (identifier in self._inventory_tasks or any(cid == identifier for cid, _ in self._active)
                    or any(cid == identifier for cid, _ in self._review_tasks))
                if record["status"] in ACTIVE_CYCLE or owned:
                    self._pause_at_cutoff(record)
            for key, task in list(self._active.items()):
                if task.done():
                    try:
                        task.result()
                    except BaseException:
                        record = self._cycle(key[0])
                        record["status"], record["fault"] = "paused", (OPERATOR_WINDOW_ENDED
                            if record.get("pause_reason") == OPERATOR_WINDOW_ENDED else "autonomy_persistence_or_worker_failure")
                        self._save_cycle(record)
                    del self._active[key]
            for key, task in list(self._review_tasks.items()):
                if task.done():
                    try:
                        task.result()
                    except BaseException:
                        pass
                    del self._review_tasks[key]
            if self._closed:
                return self.status()
            record = requested or self._active_cycle()
            if cycle_id is None and record is None and self.config["enabled"] and self.config["schedule_enabled"] and self.clock() >= monthly_due(self.clock()):
                month = _month(self.clock())
                if not any(c.get("scheduled_month") == month for c in self.cycles.values()):
                    created = self.create_cycle(kind="scheduled", scheduled_month=month)
                    record = self.cycles[created["id"]]
                    # Catalog GETs are scheduled separately, so status and pause
                    # requests never wait on thousands of pagination requests.
                    record["scheduled_inventory_pending"] = True
                    self._save_cycle(record)
            if record is None:
                return self.status()
            if self._pause_at_cutoff(record):
                return self.status()
            if record.get("scheduled_inventory_pending"):
                record.pop("scheduled_inventory_pending")
                self._save_cycle(record)
                self._inventory_tasks[record["id"]] = asyncio.create_task(self._scheduled_start(record["id"]))
                return self.status()
            if record["status"] != "running":
                return self.status()
            try:
                self._check_pins(record)
            except ValueError as exc:
                record["status"], record["fault"] = "paused", str(exc)
                self._save_cycle(record)
                return self.status()
            occupied = {self.cycles[cid]["products"][next(i for i, p in enumerate(self.cycles[cid]["products"]) if p["product_id"] == pid)]["supplier"] or "unmapped"
                for cid, pid in self._active}
            window = self._ordered_pending(record)[:99]
            candidates = sorted((p for p in window if p["status"] == "queued"), key=lambda p: (p["last_dispatch_sequence"], p["ordinal"]))
            for job in candidates:
                if self._pause_at_cutoff(record):
                    break
                if len(self._active) >= record["config"]["worker_capacity"]:
                    break
                supplier = job["supplier"] or "unmapped"
                if supplier in record["supplier_blocks"] or supplier in occupied:
                    continue
                if max(job.get("not_before", 0), record["supplier_not_before"].get(supplier, 0)) > self.clock():
                    continue
                record["dispatch_sequence"] += 1
                job.update(status="running", attempts=job["attempts"] + 1, started_at=job["started_at"] or self.clock(),
                    updated_at=self.clock(), last_dispatch_sequence=record["dispatch_sequence"])
                self._sync_timing(record, job, owned=True)
                self._save_cycle(record)  # Durable dispatch intent precedes any supplier or write action.
                self._active[(record["id"], job["product_id"])] = asyncio.create_task(self._execute(record, job))
                occupied.add(supplier)
            # Blocked products are explicit access-required outcomes, permitting
            # later batches/other suppliers to proceed without repeating a login.
            if self._pause_at_cutoff(record):
                return self.status()
            self._maybe_reviews(record)
            if self._pause_at_cutoff(record):
                return self.status()
            if record["inventory"].get("complete") and all(p["status"] in TERMINAL for p in record["products"]):
                record["status"] = "completed" if all(p["status"] == "verified" for p in record["products"]) and record["products"] else "completed_with_attention"
                record["finished_at"] = self.clock()
                self._save_cycle(record)
            return self.status()

    async def _scheduled_start(self, cycle_id):
        try:
            await self.inventory(cycle_id)
            if self.cycles[cycle_id]["inventory"].get("complete") and self.config["enabled"] and self.config["schedule_enabled"]:
                await self.start(cycle_id)
        except Exception:
            record = self.cycles[cycle_id]
            record["status"], record["fault"] = "inventory_failed", "scheduled_preflight_failed"
            self._save_cycle(record)

    async def open(self):
        if self._loop is not None or self._closed:
            raise ValueError("autonomy_already_started_or_closed")
        async def loop():
            while not self._closed:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    record = self._active_cycle()
                    if record:
                        record["status"], record["fault"] = "paused", "autonomy_dispatch_failed"
                        self._save_cycle(record)
                await asyncio.sleep(0.5)
        self._loop = asyncio.create_task(loop(), name="autonomous-companion")

    async def close(self):
        if self._closed:
            return
        self._closed = True
        tasks = [*self._active.values(), *self._review_tasks.values(), *self._inventory_tasks.values()]
        if self._loop:
            tasks.append(self._loop)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        self._review_tasks.clear()
        fcntl.flock(self._owner, fcntl.LOCK_UN)
        self._owner.close()
