"""Typed staging repairs and a distinct, human-authorized production release.

This layer cannot create code, change retail rules, remove rows, or replace a
database. It reuses the connector's exact preview/CAS/backup/rollback protocol.
An injected independent storefront verifier is required before applying edits.
"""
from __future__ import annotations

import asyncio
import copy
from decimal import Decimal
import json
from pathlib import Path
import re
import time
import uuid

from .companion_environments import digest, private_save


ALLOWED_REPAIRS = {"set_cost", "add_row", "add_document"}
RELEASE_CHECKS = {"calculator_data", "rendered_prices", "artwork_dimensions", "shipping", "customer_flow"}


class CompanionRepairWorkflow:
    def __init__(self, root, environments, *, verifier=None, clock=time.time):
        self.root = Path(root) / "repairs"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.environments, self.verifier, self.clock = environments, verifier, clock
        self._locks = {}

    def _path(self, repair_id):
        if not isinstance(repair_id, str) or not re.fullmatch(r"[a-f0-9]{32}", repair_id):
            raise ValueError("invalid_companion_repair_id")
        path = self.root / (repair_id + ".json")
        if path.is_symlink():
            raise ValueError("unsafe_companion_repair_storage")
        return path

    def get(self, repair_id):
        path = self._path(repair_id)
        if not path.is_file():
            raise ValueError("companion_repair_not_found")
        record = json.loads(path.read_text())
        if digest(record["manifest"]) != record["manifest_sha256"]:
            raise ValueError("companion_repair_manifest_changed")
        return record

    def history(self, limit=None):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 200):
            raise ValueError("repair_history_limit_out_of_bounds")
        paths = [path for path in self.root.glob("*.json") if not path.is_symlink()]
        if limit is not None:
            paths = sorted(paths, key=lambda path: path.stat().st_mtime_ns, reverse=True)[:limit]
            return [self.get(path.stem) for path in paths]
        return sorted((self.get(path.stem) for path in paths),
                      key=lambda r: r["manifest"]["created_at"], reverse=True)

    def _save(self, record):
        private_save(self._path(record["id"]), record)

    @staticmethod
    def _data(snapshot):
        return {key: snapshot.get(key) for key in ("rows", "documents", "headers", "columns", "pricing_basis", "currency")}

    def _fresh(self, evidence, changes):
        observed = evidence.get("source_observed_at")
        if (not isinstance(observed, (int, float)) or not 0 <= self.clock() - observed <= 6 * 3600
                or not re.fullmatch(r"[a-f0-9]{64}", str(evidence.get("source_sha256", "")))
                or evidence.get("changes_sha256") != digest(changes)
                or evidence.get("mapping_confirmed") is not True):
            raise ValueError("fresh_complete_bound_source_evidence_required")
        checks = evidence.get("checks", {})
        cost_changes = any(change["kind"] in {"set_cost", "add_row"} for change in changes)
        required = {"options", "quantities", "source_prices"} if cost_changes else set()
        if cost_changes and evidence.get("source_domain_complete") is not True:
            raise ValueError("fresh_complete_bound_source_evidence_required")
        if any(change["kind"] == "add_row" for change in changes):
            required.add("production")
        if any(change["kind"] == "add_document" for change in changes):
            required.add("documents")
        if any(checks.get(key) != "match" for key in required):
            raise ValueError("verified_source_checks_required_for_repair")

    async def preview(self, product_id, changes, evidence):
        if (not isinstance(changes, list) or not changes or len(changes) > 1000
                or any(not isinstance(op, dict) or op.get("kind") not in ALLOWED_REPAIRS for op in changes)):
            raise ValueError("repair_operation_requires_manual_implementation")
        if not isinstance(evidence, dict):
            raise ValueError("fresh_complete_bound_source_evidence_required")
        self._fresh(evidence, changes)
        production = await self.environments.snapshot("production", product_id)
        staging = await self.environments.snapshot("staging", product_id)
        for snapshot in (production, staging):
            if str(snapshot.get("product_id")) != str(product_id) or not snapshot.get("revision"):
                raise ValueError("repair_snapshot_identity_missing")
            if snapshot.get("calculator", {}).get("family") != "custom_csv" or not snapshot.get("calculator", {}).get("write_supported"):
                return {"status": "held", "reason": "calculator_family_repair_unsupported", "product_id": str(product_id)}
        if self._data(production) != self._data(staging):
            return {"status": "held", "reason": "staging_baseline_differs_from_production", "product_id": str(product_id)}
        before_rule = production.get("retail_rule") or {}
        staging_rule = staging.get("retail_rule") or {}
        for key in ("fingerprint", "script_sha256"):
            if before_rule.get(key) != staging_rule.get(key):
                return {"status": "held", "reason": "staging_pricing_code_differs_from_production", "product_id": str(product_id)}
        remote = await self.environments.request("staging", "POST", "/previews", {
            "product_id": int(product_id), "expected_revision": staging["revision"],
            "audit_id": evidence.get("audit_id") or uuid.uuid4().hex,
            "evidence": {"verified": True, "source_observed_at": int(evidence["source_observed_at"]),
                         "sha256": evidence["source_sha256"]}, "changes": changes})
        if not remote.get("preview_id") or not remote.get("diff"):
            raise ValueError("staging_repair_preview_invalid")
        manifest = {"schema": "hermes-staging-repair-manifest/v1", "product_id": str(product_id),
                    "created_at": self.clock(), "source": evidence, "changes": changes,
                    "production_revision": production["revision"], "staging_revision": staging["revision"],
                    "before_data": self._data(production), "staging_preview_id": remote["preview_id"],
                    "before_retail_rule": copy.deepcopy(before_rule),
                    "before_artwork": copy.deepcopy(production.get("artwork", {"status": "unknown"})),
                    "before_retail_script_sha256": evidence.get("retail_script_sha256"),
                    "diff": remote["diff"], "production_origin": self.environments.status("production")["base_url"],
                    "staging_origin": self.environments.status("staging")["base_url"]}
        record = {"id": uuid.uuid4().hex, "status": "preview", "manifest": manifest,
                  "manifest_sha256": digest(manifest), "remote_expires_at": remote.get("expires_at"),
                  "production_approval": None, "can_rollback": False}
        self._save(record)
        return record

    def _targets(self, record, *, fresh=True):
        m = record["manifest"]
        if (self.environments.status("production").get("base_url") != m["production_origin"]
                or self.environments.status("staging").get("base_url") != m["staging_origin"]):
            raise ValueError("repair_environment_target_changed")
        if fresh:
            self._fresh(m["source"], m["changes"])

    async def _verify(self, record, environment, snapshot, *, rollback=False, require_release=False):
        before = record["manifest"]["before_data"]
        rows, documents = copy.deepcopy(before["rows"]), copy.deepcopy(before["documents"])
        if not rollback:
            for operation in record["manifest"]["changes"]:
                if operation["kind"] == "add_document":
                    documents.append({"url": operation["url"], "label": operation["label"]})
                    continue
                matching = [row for row in rows if row["selection"] == operation["selection"]]
                if operation["kind"] == "set_cost":
                    if len(matching) != 1:
                        raise ValueError("repair_configuration_ambiguous")
                    matching[0]["cost"] = operation["cost"]
                else:
                    if matching:
                        raise ValueError("repair_duplicate_configuration")
                    rows.append({"selection": operation["selection"], "cost": operation["cost"], "production": operation["production"]})
        def signature(values):
            return sorted((json.dumps(row["selection"], sort_keys=True), Decimal(row["cost"]), row.get("production")) for row in values)
        if (signature(rows) != signature(snapshot.get("rows", []))
                or sorted(documents, key=lambda d: d["url"]) != sorted(snapshot.get("documents", []), key=lambda d: d["url"])):
            raise ValueError("repair_snapshot_differs_from_manifest")
        before_rule = record["manifest"].get("before_retail_rule") or {}
        current_rule = snapshot.get("retail_rule") or {}
        if any(before_rule.get(key) != current_rule.get(key) for key in ("fingerprint", "script_sha256")):
            raise ValueError("repair_must_preserve_retail_code")
        if self.verifier is None:
            raise ValueError("independent_storefront_verifier_required")
        verification = await self.verifier(environment, record["manifest"]["product_id"],
                                            record["manifest"], snapshot, rollback=rollback)
        kinds = {change["kind"] for change in record["manifest"]["changes"]}
        required = (set(RELEASE_CHECKS) if require_release or (environment == "production" and not rollback) else
                    {"calculator_data", "rendered_prices", "artwork_dimensions"} if kinds & {"set_cost", "add_row"} else
                    {"calculator_data", "documents", "retail_rule_unchanged"})
        if "add_document" in kinds:
            required.add("documents")
        if (not isinstance(verification, dict)
                or any(verification.get("checks", {}).get(key) != "match" for key in required)
                or verification.get("manifest_sha256") != record["manifest_sha256"]):
            raise ValueError("repaired_storefront_recheck_incomplete")
        return verification

    @staticmethod
    def _release_holds(record, verification):
        required = set(RELEASE_CHECKS)
        if any(op["kind"] == "add_document" for op in record["manifest"]["changes"]):
            required.add("documents")
        missing = sorted(key for key in required if verification.get("checks", {}).get(key) != "match")
        record.update(release_ready=not missing, release_hold_checks=missing)

    async def _recover_receipt(self, record, environment):
        """Read an interrupted remote outcome before deciding to repeat a write."""
        self._targets(record, fresh=False)
        if record.get(environment + "_change"):
            return record[environment + "_change"]
        preview_id = (record["manifest"]["staging_preview_id"] if environment == "staging"
                      else record["production_preview"]["preview_id"])
        remote = await self.environments.request(environment, "GET", "/previews/" + preview_id)
        if remote.get("status") == "applied" and remote.get("change"):
            receipt = remote["change"]
            snapshot = await self.environments.snapshot(environment, record["manifest"]["product_id"])
            if snapshot.get("revision") != receipt.get("after_revision", receipt.get("revision")):
                raise ValueError("interrupted_repair_revision_requires_review")
            record[environment + "_change"] = receipt
            record["status"] = environment + "_applied"
            self._save(record)
            return receipt
        if remote.get("status") in ("preview", "prepared"):
            return None
        raise ValueError("interrupted_repair_requires_recovery_review")

    async def apply(self, repair_id, expected_revision, *, authorization_guard=None):
        async with self._locks.setdefault(repair_id, asyncio.Lock()):
            record = self.get(repair_id)
            if record["status"] == "staging_verified":
                return record
            if self.verifier is None:
                return {**record, "status": "held", "reason": "independent_storefront_verifier_required"}
            m = record["manifest"]
            if record["status"] not in ("preview", "staging_applying", "staging_applied") or expected_revision != m["staging_revision"]:
                raise ValueError("staging_repair_revision_or_state_changed")
            if authorization_guard is not None:
                authorization_guard()
            receipt = await self._recover_receipt(record, "staging") if record["status"] != "preview" else None
            if receipt is None:
                self._targets(record)
                if record.get("remote_expires_at", 0) <= self.clock():
                    raise ValueError("staging_repair_preview_expired")
                production = await self.environments.snapshot("production", m["product_id"])
                if production["revision"] != m["production_revision"]:
                    raise ValueError("production_changed_since_staging_baseline")
                if authorization_guard is not None:
                    authorization_guard()
                record["status"] = "staging_applying"
                self._save(record)
                receipt = await self.environments.request("staging", "POST", "/apply/" + m["staging_preview_id"],
                                                           {"expected_revision": expected_revision})
            record.update(status="staging_applied", staging_change=receipt, can_rollback=True)
            self._save(record)
            try:
                snapshot = await self.environments.snapshot("staging", m["product_id"])
                if snapshot.get("revision") != receipt.get("after_revision", receipt.get("revision")):
                    raise ValueError("staging_write_receipt_revision_mismatch")
                record["staging_verification"] = await self._verify(record, "staging", snapshot)
                self._release_holds(record, record["staging_verification"])
                record["status"] = "staging_verified"
                self._save(record)
            except Exception:
                await self._rollback(record, "staging")
                record.update(status="staging_rolled_back_after_failed_recheck", reason="staging_storefront_recheck_failed")
                self._save(record)
            return record

    async def _rollback(self, record, environment, authorization=None):
        self._targets(record, fresh=False)
        receipt = record[environment + "_change"]
        await self.environments.request(environment, "POST", "/rollback/" + receipt["change_id"],
            {"expected_revision": receipt.get("after_revision", receipt.get("revision"))}, release_authorization=authorization)
        restored = await self.environments.snapshot(environment, record["manifest"]["product_id"])
        if self._data(restored) != record["manifest"]["before_data"]:
            record["status"] = environment + "_rollback_attention"
            self._save(record)
            raise ValueError("repair_rollback_state_not_restored")
        record[environment + "_rollback"] = {"status": "backend_restored_frontend_pending", "revision": restored["revision"]}
        record["status"] = environment + "_rollback_recheck_pending"
        self._save(record)
        verification = await self._verify(record, environment, restored, rollback=True)
        record[environment + "_rollback"].update(status="verified", verification=verification)
        record["can_rollback"] = False
        self._save(record)

    async def rollback(self, repair_id, *, environment="staging", actor=None, manifest_sha256=None):
        async with self._locks.setdefault(repair_id, asyncio.Lock()):
            record = self.get(repair_id)
            if environment not in ("staging", "production") or not record.get(environment + "_change"):
                raise ValueError("repair_has_no_backup")
            authorization = None
            if environment == "production":
                authorization = record.get("production_approval")
                if actor != "human" or manifest_sha256 != record["manifest_sha256"] or not authorization:
                    raise ValueError("manual_production_approval_required")
            await self._rollback(record, environment, authorization)
            record["status"] = environment + "_rollback_verified"
            self._save(record)
            return record

    async def approve_production(self, repair_id, manifest_sha256, *, actor):
        """Human-only route. Never expose this capability to the AI director."""
        async with self._locks.setdefault(repair_id, asyncio.Lock()):
            record = self.get(repair_id)
            if actor != "human" or manifest_sha256 != record["manifest_sha256"]:
                raise ValueError("human_approval_of_exact_manifest_required")
            self._targets(record)
            if record["status"] != "staging_verified":
                raise ValueError("verified_staging_release_required")
            stage = await self.environments.snapshot("staging", record["manifest"]["product_id"])
            record["staging_verification"] = await self._verify(record, "staging", stage, require_release=True)
            self._release_holds(record, record["staging_verification"])
            current = await self.environments.snapshot("production", record["manifest"]["product_id"])
            if current["revision"] != record["manifest"]["production_revision"]:
                raise ValueError("production_changed_before_manual_approval")
            if record.get("production_approval"):
                raise ValueError("production_manifest_already_approved")
            record["production_approval"] = {"actor": "human", "approval_id": uuid.uuid4().hex,
                "manifest_sha256": manifest_sha256, "approved_at": self.clock()}
            self._save(record)
            return record

    async def recheck_staging(self, repair_id):
        async with self._locks.setdefault(repair_id, asyncio.Lock()):
            record = self.get(repair_id)
            if record["status"] != "staging_verified":
                raise ValueError("verified_staging_repair_required")
            self._targets(record, fresh=False)
            snapshot = await self.environments.snapshot("staging", record["manifest"]["product_id"])
            record["staging_verification"] = await self._verify(record, "staging", snapshot)
            self._release_holds(record, record["staging_verification"])
            self._save(record)
            return record

    async def promote(self, repair_id, manifest_sha256, *, actor):
        """Explicit manual production promotion with rollback, never a batch step."""
        async with self._locks.setdefault(repair_id, asyncio.Lock()):
            record = self.get(repair_id)
            if record["status"] == "production_verified":
                return record
            approval = record.get("production_approval") or {}
            if (actor != "human" or manifest_sha256 != record["manifest_sha256"]
                    or approval.get("manifest_sha256") != manifest_sha256 or approval.get("actor") != "human"):
                raise ValueError("manual_production_approval_required")
            if record["status"] not in ("staging_verified", "production_applying", "production_applied"):
                raise ValueError("verified_staging_release_required")
            if self.verifier is None:
                raise ValueError("independent_storefront_verifier_required")
            m = record["manifest"]
            receipt = await self._recover_receipt(record, "production") if record["status"] != "staging_verified" else None
            if receipt is None:
                self._targets(record)
                stage = await self.environments.snapshot("staging", m["product_id"])
                await self._verify(record, "staging", stage, require_release=True)
                current = await self.environments.snapshot("production", m["product_id"])
                if current["revision"] != m["production_revision"]:
                    raise ValueError("production_changed_after_approval")
                # Prove production's read-only fixtures on the unchanged
                # before state before creating a production preview or write.
                record["production_preflight"] = await self._verify(record, "production", current,
                                                                      rollback=True, require_release=True)
                self._save(record)
            if receipt is None and not record.get("production_preview"):
                preview = await self.environments.request("production", "POST", "/previews", {
                    "product_id": int(m["product_id"]), "expected_revision": m["production_revision"],
                    "audit_id": m["source"].get("audit_id") or record["id"],
                    "evidence": {"verified": True, "source_observed_at": int(m["source"]["source_observed_at"]),
                                 "sha256": m["source"]["source_sha256"]}, "changes": m["changes"]},
                    release_authorization=approval)
                if preview.get("diff") != m["diff"]:
                    raise ValueError("production_preview_differs_from_approved_manifest")
                record["production_preview"] = preview
            if receipt is None:
                record["status"] = "production_applying"
                self._save(record)
                receipt = await self.environments.request("production", "POST", "/apply/" + record["production_preview"]["preview_id"],
                    {"expected_revision": m["production_revision"]}, release_authorization=approval)
            record.update(production_change=receipt, status="production_applied")
            self._save(record)
            try:
                snapshot = await self.environments.snapshot("production", m["product_id"])
                if snapshot.get("revision") != receipt.get("after_revision", receipt.get("revision")):
                    raise ValueError("production_write_receipt_revision_mismatch")
                record["production_verification"] = await self._verify(record, "production", snapshot)
                record["status"] = "production_verified"
            except Exception:
                await self._rollback(record, "production", approval)
                record.update(status="production_rolled_back_after_failed_recheck", reason="production_storefront_recheck_failed")
            self._save(record)
            return record
