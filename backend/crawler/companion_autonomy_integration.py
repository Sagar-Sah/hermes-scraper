"""Wire the monthly coordinator to the existing collectors and scoped connectors."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, SecretStr, StrictBool, StrictStr

from .autonomous_companion import AutonomousCompanion, MAX_PAGES, MAX_PRODUCTS
from .autonomous_product_pipeline import AutonomousProductPipeline, StagingBaseline, safe_error
from .companion_director import MuseDirector
from .companion_environments import CompanionEnvironmentRegistry, private_save
from .companion_existing_shipping import ExistingShippingEvidence
from .companion_notifications import CompanionNotifications
from .companion_baseline_setup import BaselineSetupVerifier
from .companion_repair_workflow import CompanionRepairWorkflow
from .companion_shipping_audit import CompanionShippingAudit
from .companion_storefront_verifier import CompanionStorefrontVerifier


class MonthlyShippingModels:
    """One fresh benchmark per registered shipping model/cycle, shared by products.

    Real supplier quote drivers are registered in code. Neither an API request
    nor model output can supply a claimed passing shipping observation.
    """
    def __init__(self, root):
        self.root = Path(root) / "shipping-models"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.collectors = {}
        self._locks = {}

    async def __call__(self, cycle, product, snapshot):
        routing = snapshot.get("shipping") or {}
        identifier = routing.get("model_id", routing.get("profile_id"))
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
            return {"status": "unknown", "reason": "verified_product_shipping_model_required"}
        path = self.root / (identifier + ".json")
        if not path.is_file() or path.is_symlink():
            return {"status": "unknown", "reason": "shipping_model_benchmark_not_registered"}
        model = json.loads(path.read_text())
        if str(product["product_id"]) not in [str(x) for x in model.get("product_ids", [])]:
            return {"status": "unknown", "reason": "shipping_model_product_scope_unproven"}
        key = (cycle["id"], identifier)
        async with self._locks.setdefault(key, asyncio.Lock()):
            folder = self.root / cycle["id"]
            folder.mkdir(exist_ok=True, mode=0o700)
            receipt = folder / (identifier + ".json")
            if receipt.is_file() and not receipt.is_symlink():
                record = json.loads(receipt.read_text())
                if record.get("state") != "complete":
                    return {"status": "unknown", "reason": "shipping_benchmark_dispatch_uncertain"}
            else:
                collector = self.collectors.get(model.get("collector_profile"))
                if not callable(collector):
                    return {"status": "unknown", "reason": "shipping_benchmark_collector_required"}
                private_save(receipt, {"state": "dispatching", "cycle_id": cycle["id"], "model_id": identifier})
                observations = await collector(deepcopy(cycle), deepcopy(model))
                if (not isinstance(observations, list) or any(not isinstance(row, dict)
                        or row.get("cycle_id") != cycle["id"]
                        or not isinstance(row.get("source_observed_at"), (int, float))
                        or row["source_observed_at"] < cycle["created_at"] for row in observations)):
                    return {"status": "unknown", "reason": "shipping_current_cycle_evidence_required"}
                record = {"state": "complete", "cycle_id": cycle["id"], "model_id": identifier, "observations": observations}
                private_save(receipt, record)
            if (record.get("cycle_id") != cycle["id"] or record.get("model_id") != identifier
                    or not isinstance(record.get("observations"), list)
                    or any(not isinstance(row, dict) or row.get("cycle_id") != cycle["id"]
                           or not isinstance(row.get("source_observed_at"), (int, float))
                           or row["source_observed_at"] < cycle["created_at"] for row in record["observations"])):
                return {"status": "unknown", "reason": "shipping_current_cycle_evidence_required"}
            return CompanionShippingAudit.assess(model, record["observations"], runtime=routing)


class IntegratedAutonomousCompanion(AutonomousCompanion):
    def __init__(self, store, companion, diagnoses, **kwargs):
        self._maintenance = False
        self.notifications = None
        self.software_identity = lambda: {}
        super().__init__(store, **kwargs)
        self.environments = CompanionEnvironmentRegistry(self.root)
        self.director = MuseDirector(store)
        self.baseline = StagingBaseline(self.root, self.environments)
        self.verifier = CompanionStorefrontVerifier(self.environments, companion)
        self.baseline_setup = BaselineSetupVerifier(self.baseline, storefront_verifier=self.verifier)
        self.repairs = CompanionRepairWorkflow(self.root, self.environments, verifier=self.verifier)
        self.shipping_models = MonthlyShippingModels(self.root)
        self.shipping_evidence = ExistingShippingEvidence(self.root / "shipping-evidence")
        self.shipping_evidence_registration = self.shipping_evidence.register_available(self.shipping_models)
        self.notifications = CompanionNotifications(self.root)
        self.pipeline = AutonomousProductPipeline(store, companion, self.environments, self.repairs, diagnoses,
                                                 shipping=self.shipping_models, baseline=self.baseline, verifier=self.verifier)
        self.mapping_resolver = self.pipeline.mapping.resolve
        self.product_handler = self.pipeline
        self.director_review = self._review_batch
        if not self.config.get("director_provider_id"):
            candidates = [p for p in store.profiles("provider")
                          if p.get("base_url", "").rstrip("/") == "https://openrouter.ai/api/v1"
                          and str(p.get("model", "")).startswith("meta/muse-")]
            if len(candidates) == 1:
                self.configure(director_provider_id=candidates[0]["id"])

    def status(self):
        value = super().status()
        # During base construction the integration objects may not exist yet.
        if hasattr(self, "director"):
            value.update(director=self.director.status(self.config), environments=self.environments.status(),
                         baseline=self.baseline.status(), maintenance=self._maintenance)
            value["shipping_benchmarks"] = self.shipping_evidence_registration
            value["notifications"] = self.notifications.recent(limit=10) if self.notifications else []
        return value

    def create_cycle(self, *, baseline=None, **kwargs):
        context = {"server_baseline": self.baseline.status(), "software_build": self.software_identity(),
                   "request_context": baseline or {}}
        return super().create_cycle(baseline=context, **kwargs)

    @staticmethod
    def _catalog_document(raw, *, base, page):
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("companion_catalog_duplicate_json_key")
                value[key] = item
            return value
        try:
            value = json.loads(raw, object_pairs_hook=unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("companion_catalog_invalid_json") from None
        if (not isinstance(value, dict) or set(value) != {"schema", "site_origin", "environment", "items", "page", "total", "pages", "next_cursor"}
                or value["schema"] != "hermes-companion-catalog/v1" or value["site_origin"] != base
                or value["environment"] != "production" or type(value["page"]) is not int or value["page"] != page
                or type(value["total"]) is not int or not 0 <= value["total"] <= MAX_PRODUCTS
                or type(value["pages"]) is not int or value["pages"] != max(1, (value["total"] + 99) // 100)
                or not isinstance(value["items"], list) or not 1 <= value["pages"] <= MAX_PAGES
                or page > value["pages"]):
            raise ValueError("companion_catalog_identity_or_pagination_changed")
        expected_next = page + 1 if page < value["pages"] else None
        if (value["next_cursor"] != expected_next or (expected_next is not None and type(value["next_cursor"]) is not int)
                or len(value["items"]) != min(100, max(0, value["total"] - (page - 1) * 100))):
            raise ValueError("companion_catalog_partial_page")
        def label(text):
            return isinstance(text, str) and 0 < len(text) <= 600 and not any(ord(c) < 32 for c in text)
        for item in value["items"]:
            if (not isinstance(item, dict) or set(item) != {"id", "name", "permalink", "categories"}
                    or type(item["id"]) is not int or item["id"] < 1 or not label(item["name"])
                    or not isinstance(item["permalink"], str) or len(item["permalink"]) > 2000
                    or not isinstance(item["categories"], list) or len(item["categories"]) > 1000):
                raise ValueError("companion_catalog_product_schema_changed")
            try:
                link = urlsplit(item["permalink"])
            except ValueError:
                raise ValueError("companion_catalog_product_origin_changed") from None
            if (link.scheme + "://" + link.netloc != base or link.username or link.password or link.query or link.fragment
                    or not link.path or link.path == "/" or any(ord(c) < 32 for c in item["permalink"])):
                raise ValueError("companion_catalog_product_origin_changed")
            category_ids = set()
            for category in item["categories"]:
                if (not isinstance(category, dict) or set(category) != {"id", "name"}
                        or type(category["id"]) is not int or category["id"] < 1 or not label(category["name"])
                        or category["id"] in category_ids):
                    raise ValueError("companion_catalog_category_schema_changed")
                category_ids.add(category["id"])
        return value

    async def _pages(self, base, resource, *, evidence=None, evidence_dir=None):
        # Select once. A broken/expired selected connector aborts this inventory;
        # it never falls back to a different data source halfway through a scan.
        if resource != "products" or self.environments.published_catalog_connection(base) is None:
            return await super()._pages(base, resource, evidence=evidence, evidence_dir=evidence_dir)
        products, expected_total, expected_pages, previous_id = [], None, None, 0
        for page in range(1, MAX_PAGES + 1):
            url = base + "/wp-json/hermes-companion/v1/catalog?page=" + str(page)
            result = await self.environments.published_catalog_page(base, page)
            if not isinstance(result, (tuple, list)) or len(result) != 4:
                raise ValueError("companion_catalog_raw_transport_invalid")
            status, final, raw, headers = result
            if (type(status) is not int or not 100 <= status <= 599 or final != url
                    or not isinstance(raw, bytes) or len(raw) > 16 * 1024 * 1024 or not isinstance(headers, dict)):
                raise ValueError("companion_catalog_raw_transport_invalid")
            if evidence is not None and evidence_dir is not None:
                sha = hashlib.sha256(raw).hexdigest()
                path = evidence_dir / ("companion-products-" + str(page) + "-" + sha + ".json")
                self._atomic(path, raw)
                evidence.append({"resource": resource, "page": page, "status": status,
                    "url": url, "transport": "companion_production_read_catalog",
                    "path": str(path.relative_to(self.root)), "sha256": sha, "bytes": len(raw)})
            if status != 200:
                raise ValueError("companion_catalog_page_http_" + str(status))
            document = self._catalog_document(raw, base=base, page=page)
            if expected_total is not None and (document["total"] != expected_total or document["pages"] != expected_pages):
                raise ValueError("companion_catalog_changed_during_inventory")
            expected_total, expected_pages = document["total"], document["pages"]
            for product in document["items"]:
                if product["id"] <= previous_id:
                    raise ValueError("companion_catalog_id_order_or_duplicate_changed")
                previous_id = product["id"]
                products.append(product)
            if document["next_cursor"] is None:
                if len(products) != expected_total:
                    raise ValueError("companion_catalog_total_does_not_match_inventory")
                return products, page, 0
        raise ValueError("companion_catalog_page_limit_reached")

    async def tick(self, *args, **kwargs):
        if self._maintenance:
            return self.status()
        value = await super().tick(*args, **kwargs)
        if self.notifications:
            await self.notifications.observe({**value, "cycles": self.list_cycles()[:20]})
        return value

    async def close(self):
        try:
            await super().close()
        finally:
            if self.notifications:
                await self.notifications.close()

    async def inventory(self, *args, **kwargs):
        if self._maintenance:
            raise ValueError("autonomy_paused_for_maintenance")
        return await super().inventory(*args, **kwargs)

    async def start(self, *args, **kwargs):
        if self._maintenance:
            raise ValueError("autonomy_paused_for_maintenance")
        return await super().start(*args, **kwargs)

    async def resume(self, *args, **kwargs):
        if self._maintenance:
            raise ValueError("autonomy_paused_for_maintenance")
        return await super().resume(*args, **kwargs)

    async def begin_maintenance(self, *, timeout=600):
        self._maintenance = True
        # Fence new admissions first, then let finite source requests and paid
        # model reviews finish naturally. A busy update fails without turning
        # healthy source work into uncertain requests.
        draining = [task for task in [*self._active.values(), *self._review_tasks.values(), *self._inventory_tasks.values()]
                    if not task.done()]
        if draining:
            _, pending = await asyncio.wait(draining, timeout=timeout)
            if pending:
                raise ValueError("autonomy_workers_still_draining")
        current = self._active_cycle()
        if current and current["status"] in {"running", "inventorying"}:
            await self.pause(current["id"])
        for cycle in self.cycles.values():
            if cycle["status"] in {"created", "ready", "inventorying"}:
                cycle["status"] = "paused" if cycle["inventory"].get("complete") else "inventory_failed"
                cycle["fault"] = "autonomy_paused_for_maintenance"
                cycle.pop("scheduled_inventory_pending", None)
                self._save_cycle(cycle)

    async def end_maintenance(self):
        self._maintenance = False

    async def _review_batch(self, cycle, batch):
        if self._maintenance:
            return {"status": "held", "reason": "autonomy_paused_for_maintenance"}
        ids = {str(item["product_id"]) for item in batch.get("products", [])}
        proposals = [p for p in self.pipeline.proposals_for_cycle(cycle["id"]) if str(p["product_id"]) in ids]
        chunks = [proposals[i:i + 20] for i in range(0, len(proposals), 20)] or [[]]
        results = []
        for index, chunk in enumerate(chunks):
            scoped = {**batch, "id": str(batch["batch_index"]) + "/" + str(index)}
            result = await self.director.review(cycle, scoped, config=cycle["config"], proposals=chunk)
            if self._maintenance:
                return {"status": "held", "reason": "autonomy_paused_for_maintenance"}
            if result.get("status") == "reviewed":
                try:
                    result["staging_repairs"] = await self.pipeline.apply_decisions(cycle["id"], result["decision"]["decisions"])
                except Exception as exc:
                    result["staging_repairs"] = [{"status": "held", "reason": safe_error(exc)}]
            results.append(result)
        return {"status": "reviewed" if all(r.get("status") == "reviewed" for r in results) else "held",
                "reviews": results, "production_changes": 0, "budget": self.director.status(cycle["config"])}


class ProtectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: SecretStr
    password: SecretStr


class EnvironmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: StrictStr
    token: SecretStr | None = None
    role: StrictStr | None = None
    protection_auth: ProtectionInput | None = None


class ReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_sha256: StrictStr


class RollbackInput(ReleaseInput):
    environment: StrictStr = "staging"


class BaselineApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_id: StrictStr
    manifest_sha256: StrictStr
    receipt_hashes: dict[str, str]


class ReleaseConnectionInput(EnvironmentInput):
    acknowledge_manual_releases_only: StrictBool


def _repair_summary(record):
    manifest = record["manifest"]
    return {**{k: record.get(k) for k in ("id", "status", "manifest_sha256", "release_ready", "release_hold_checks", "error", "verification")},
            "manifest": {k: manifest.get(k) for k in ("product_id", "created_at", "diff")} | {
                "source": {"source_observed_at": manifest["source"].get("source_observed_at")}}}


def autonomy_integration_router(getservice, authenticated):
    router = APIRouter(prefix="/api/v1/companion/autonomy", dependencies=[Depends(authenticated)])

    def fail(exc):
        # Application errors are fixed codes, never remote bodies/credentials.
        code = str(exc)
        if not re.fullmatch(r"[a-z0-9_]{1,160}", code):
            code = "autonomy_operation_requires_attention"
        return HTTPException(409, code)

    @router.get("/environments")
    def environments():
        return getservice().environments.status()

    @router.post("/maintenance/end")
    async def end_maintenance():
        # Update middleware fences this route while an update is in progress.
        await getservice().end_maintenance()
        return getservice().status()

    @router.get("/notifications")
    def notifications():
        return {"items": getservice().notifications.recent(limit=50)}

    @router.post("/notifications/{notification_id}/retry")
    async def retry_notification(notification_id: str):
        try:
            return await getservice().notifications.retry(notification_id)
        except ValueError as exc:
            raise HTTPException(409, safe_error(exc)) from None

    @router.put("/environments/{environment}")
    async def configure_environment(environment: str, payload: EnvironmentInput):
        if payload.role == "release":
            raise HTTPException(409, "use_manual_production_release_connection")
        auth = {"username": payload.protection_auth.username.get_secret_value(),
                "password": payload.protection_auth.password.get_secret_value()} if payload.protection_auth else None
        try:
            return await getservice().environments.configure(environment, payload.base_url,
                payload.token.get_secret_value() if payload.token else None, auth, role=payload.role)
        except ValueError as exc:
            raise fail(exc) from None

    @router.put("/production-release-connection")
    async def configure_release(payload: ReleaseConnectionInput):
        if payload.acknowledge_manual_releases_only is not True or payload.protection_auth is not None:
            raise HTTPException(409, "manual_production_release_connection_acknowledgment_required")
        try:
            return await getservice().environments.configure("production", payload.base_url,
                payload.token.get_secret_value(), role="release")
        except ValueError as exc:
            raise fail(exc) from None

    @router.post("/director/preflight")
    async def director_preflight():
        service = getservice()
        try:
            return await service.director.preflight(service.config)
        except ValueError as exc:
            raise fail(exc) from None

    @router.get("/baseline")
    def baseline():
        return getservice().baseline.status()

    @router.post("/baseline/capture")
    async def capture_baseline():
        try:
            return await getservice().baseline.capture()
        except ValueError as exc:
            raise fail(exc) from None

    @router.post("/baseline/approve")
    def approve_baseline(payload: BaselineApproval):
        try:
            return getservice().baseline.approve(payload.capture_id, payload.manifest_sha256,
                                                payload.receipt_hashes, actor="human")
        except ValueError as exc:
            raise fail(exc) from None

    @router.get("/repairs")
    def repairs():
        return {"items": [_repair_summary(r) for r in getservice().repairs.history(limit=200)]}

    @router.get("/repairs/{repair_id}")
    def repair(repair_id: str):
        try:
            return getservice().repairs.get(repair_id)
        except ValueError as exc:
            raise fail(exc) from None

    @router.post("/repairs/{repair_id}/recheck")
    async def recheck(repair_id: str):
        try:
            return await getservice().repairs.recheck_staging(repair_id)
        except ValueError as exc:
            raise fail(exc) from None

    @router.post("/repairs/{repair_id}/approve-production")
    async def approve(repair_id: str, payload: ReleaseInput):
        try:
            await getservice().baseline.ensure_current()
            return await getservice().repairs.approve_production(repair_id, payload.manifest_sha256, actor="human")
        except ValueError as exc:
            raise fail(exc) from None

    @router.post("/repairs/{repair_id}/promote")
    async def promote(repair_id: str, payload: ReleaseInput):
        try:
            await getservice().baseline.ensure_current()
            return await getservice().repairs.promote(repair_id, payload.manifest_sha256, actor="human")
        except ValueError as exc:
            raise fail(exc) from None

    @router.post("/repairs/{repair_id}/rollback")
    async def rollback(repair_id: str, payload: RollbackInput):
        try:
            return await getservice().repairs.rollback(repair_id, environment=payload.environment,
                                                       actor="human", manifest_sha256=payload.manifest_sha256)
        except ValueError as exc:
            raise fail(exc) from None

    return router
