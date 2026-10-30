"""Evidence-backed monthly product work, independent of model availability.

Adapters are explicit. A native menu observation is useful evidence but never
stands in for unobserved prices, conditional combinations or checkout tests.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .companion_environments import digest, private_save
from .product_companion import selling_scope_for_product
from .product_companion_fields import duplicate_field_findings


SOURCE_HOSTS = {"4over.com": "4over", "www.4over.com": "4over",
                "printograph.com": "printograph", "www.printograph.com": "printograph",
                "b2sign.com": "b2sign", "www.b2sign.com": "b2sign",
                "wsdisplay.com": "wsdisplay", "www.wsdisplay.com": "wsdisplay",
                "digital-print-solutions.com": "dps", "www.digital-print-solutions.com": "dps"}
REQUIRED_CHECKS = ["source_mapping", "options", "quantities", "source_prices", "production", "documents",
                   "images", "description", "retail_rule", "rendered_prices", "duplicate_fields",
                   "artwork_dimensions", "shipping", "customer_flow"]
SAFE_ERRORS = {"supplier_login_required", "source_access_required", "login_required", "source_session_required",
    "source_table_or_option_map_missing", "supplier_option_mapping_incomplete", "duplicate_saved_child_mapping",
    "cycle_source_dispatch_outcome_uncertain", "cycle_source_snapshot_expired_new_review_cycle_required",
    "source_dispatch_outcome_uncertain", "woocommerce_snapshot_unavailable", "page_unavailable_or_login_required",
    "source_table_or_attached_diagnosis_required", "companion_environment_not_configured",
    "companion_environment_connection_unverified", "companion_connector_http_401", "companion_connector_http_403",
    "companion_connector_transport_failed", "source_mapping_changed", "source_or_storefront_check_unavailable"}


def safe_error(exc):
    text = str(exc)
    if text in SAFE_ERRORS:
        return text
    if text.startswith("supplier_child_table_capture_incomplete:"):
        suffix = text.split(":", 1)[1]
        if suffix in {"login_required", "source_access_required", "source_session_required"}:
            return "supplier_login_required"
        if suffix in {"rate_limited", "source_rate_limited", "supplier_rate_limited"}:
            return "supplier_rate_limited"
    return "source_or_storefront_check_unavailable"


def file_sha(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def source_identity(url):
    p = urlsplit(str(url))
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.query or p.fragment:
        raise ValueError("source_mapping_url_invalid")
    supplier = SOURCE_HOSTS.get(p.hostname)
    if not supplier or not p.path.strip("/"):
        raise ValueError("source_supplier_profile_not_registered")
    return supplier


class SourceMappingIndex:
    def __init__(self, companion):
        self.companion = companion
        self._campaign_version = None
        self._campaign_products = []

    def _products(self):
        manager = self.companion.campaigns
        root = getattr(manager, "root", None)
        if not isinstance(root, Path):
            return [(campaign["id"], item) for summary in manager.list()
                    for campaign in [manager.get(summary["id"])] for item in campaign.get("products", [])]
        # Campaign writes are atomic replacements. Stat their identities instead
        # of reparsing every entire campaign for each catalog product (including
        # thousands of unmapped products), and rebuild immediately on changes.
        def version():
            paths = sorted(root.glob("*.json"))
            if any(path.is_symlink() for path in paths):
                raise ValueError("source_mapping_storage_invalid")
            return tuple((path.name, path.stat().st_ino, path.stat().st_size, path.stat().st_mtime_ns) for path in paths)
        current = version()
        if current != self._campaign_version:
            rows = [(campaign["id"], item) for name, *_ in current
                    for campaign in [manager.get(Path(name).stem)] for item in campaign.get("products", [])]
            if current != version():
                raise ValueError("source_mapping_changed_during_inventory")
            self._campaign_products, self._campaign_version = rows, current
        return self._campaign_products

    def resolve(self, product):
        pid = str(product.get("product_id", product.get("id", "")))
        pair = self.companion.pairs.get("woo-" + pid)
        if pair:
            try:
                source_product = self.companion._source_product(pair)
                if (source_product.get("stage") in {"excluded", "cancelled"} or source_product.get("excluded_at")
                        or source_product.get("excluded") is True
                        or not str(pair.get("mapping_status", "")).startswith("confirmed")
                        or not (source_product.get("stage") == "finalized" or source_product.get("mapping_evidence")
                                or source_product.get("fourover_option_map_evidence"))):
                    return {"mapping_status": "unknown", "reason": "confirmed_mapping_changed"}
                supplier = source_identity(pair["source_url"])
                return {"mapping_status": "confirmed", "supplier": supplier,
                        "source_url": pair["source_url"], "pair_id": pair["id"], "campaign_id": pair["campaign_id"]}
            except (ValueError, KeyError, FileNotFoundError):
                return {"mapping_status": "unknown", "reason": "confirmed_mapping_changed"}
        matches = {}
        for campaign_id, item in self._products():
            if (str(item.get("id")) != pid or item.get("stage") in {"excluded", "cancelled"}
                    or item.get("excluded_at") or item.get("excluded") is True):
                continue
            # A supplied title/URL alone is not proof of the vendor pair.
            if item.get("stage") != "finalized" and not item.get("mapping_evidence"):
                continue
            try:
                supplier = source_identity(item.get("supplier_url"))
            except ValueError:
                continue
            matches[(supplier, item["supplier_url"])] = {"mapping_status": "confirmed",
                "supplier": supplier, "source_url": item["supplier_url"], "campaign_id": campaign_id,
                "product": copy.deepcopy(item)}
        if len(matches) != 1:
            return {"mapping_status": "unknown", "reason": "source_mapping_ambiguous" if matches else "exact_supplier_mapping_required"}
        return next(iter(matches.values()))


class StagingBaseline:
    """An environment snapshot is not a backup or a passed functional test."""
    def __init__(self, root, environments):
        self.root = Path(root) / "baselines"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.environments = environments
        self.receipts = self.root / "receipts"
        self.receipts.mkdir(exist_ok=True, mode=0o700)

    def _capture(self, identifier):
        if not re.fullmatch(r"[a-f0-9]{64}", str(identifier)):
            raise ValueError("baseline_capture_not_found")
        path = self.root / (identifier + ".json")
        if not path.is_file() or path.is_symlink():
            raise ValueError("baseline_capture_not_found")
        record = json.loads(path.read_text())
        if (not isinstance(record, dict) or record.get("schema") != "hermes-staging-baseline/v1"
                or record.get("id") != identifier
                or digest({k: v for k, v in record.items() if k not in {"id", "manifest_sha256"}}) != identifier):
            raise ValueError("baseline_capture_hash_changed")
        claimed = record.pop("manifest_sha256", None)
        if digest(record) != claimed:
            raise ValueError("baseline_capture_hash_changed")
        record["manifest_sha256"] = claimed
        return record

    def latest_capture(self):
        """Return saved observations without repeating remote environment scans.

        A saved capture is historical inventory, never approval or proof of the
        current runtime. A damaged newest record stays visible as an error; do
        not quietly fall back to an older, apparently healthy environment.
        """
        paths = [p for p in self.root.glob("*.json") if re.fullmatch(r"[a-f0-9]{64}\.json", p.name)]
        if not paths:
            return None
        if any(p.is_symlink() or not p.is_file() for p in paths):
            raise ValueError("baseline_capture_storage_invalid")
        newest = max(paths, key=lambda p: (p.stat().st_mtime_ns, p.name))
        return self._capture(newest.stem)

    def _verify_receipts(self, capture_id, receipts):
        required = {"production_backup", "staging_backup", "unreleased_work_preserved", "runtime_parity", "isolation"}
        if not isinstance(receipts, dict) or set(receipts) != required:
            raise ValueError("baseline_backups_parity_and_isolation_receipts_required")
        for kind, sha in receipts.items():
            if not re.fullmatch(r"[a-f0-9]{64}", str(sha)):
                raise ValueError("baseline_receipt_not_registered")
            path = self.receipts / (sha + ".json")
            if not path.is_file() or path.is_symlink():
                raise ValueError("baseline_receipt_not_registered")
            raw = path.read_bytes()
            evidence = json.loads(raw)
            if (hashlib.sha256(raw).hexdigest() != sha or evidence.get("kind") != kind
                    or evidence.get("capture_id") != capture_id or evidence.get("status") != "passed"
                    or evidence.get("producer") != "hermes-baseline-verifier"):
                raise ValueError("baseline_receipt_evidence_invalid")
            # Only the trusted backup/test runner writes receipts. No REST
            # route accepts arbitrary receipt JSON or AI attestation flags.
            artifacts = evidence.get("artifacts")
            if not isinstance(artifacts, list) or not artifacts:
                raise ValueError("baseline_receipt_artifacts_required")
            for artifact in artifacts:
                relative = artifact.get("path")
                if not isinstance(relative, str):
                    raise ValueError("baseline_receipt_artifact_invalid")
                file = (self.root / relative).resolve()
                if (not file.is_relative_to(self.root.resolve()) or not file.is_file()
                        or (self.root / relative).is_symlink()
                        or file_sha(file) != artifact.get("sha256")):
                    raise ValueError("baseline_receipt_artifact_changed")

    @staticmethod
    def _known_runtime(record):
        for environment in ("production", "staging"):
            runtime = record.get(environment, {}).get("runtime_fingerprints")
            if not isinstance(runtime, dict):
                raise ValueError("baseline_runtime_fingerprints_unknown")
            for name in ("retail", "shipping"):
                value = runtime.get(name)
                if isinstance(value, dict):
                    value = value.get("sha256") if value.get("status") in {"match", "verified", "passed"} else None
                if not re.fullmatch(r"[a-f0-9]{64}", str(value)):
                    raise ValueError("baseline_runtime_fingerprints_unknown")
        isolation = record.get("staging", {}).get("staging_isolation") or {}
        safe = {"search_indexing": {"disabled"}, "outbound_mail": {"disabled"},
                "payments": {"disabled", "sandbox"}, "scheduled_actions": {"disabled", "isolated"},
                "outbound_http": {"disabled", "approved_rate_hosts", "rates_only"}}
        if any(isolation.get(key) not in allowed for key, allowed in safe.items()):
            raise ValueError("baseline_staging_isolation_unknown")

    def status(self):
        path = self.root / "approved.json"
        if not path.is_file() or path.is_symlink():
            return {"ready": False, "reason": "staging_baseline_backup_and_parity_required"}
        try:
            record = json.loads(path.read_text())
            captured = self._capture(record["id"])
            if any(record.get(key) != value for key, value in captured.items()):
                raise ValueError("baseline_approved_capture_changed")
            self._known_runtime(record)
            self._verify_receipts(record["id"], record["receipt_hashes"])
        except (ValueError, KeyError, OSError, json.JSONDecodeError):
            return {"ready": False, "reason": "staging_baseline_receipts_invalid"}
        return {"ready": True, "id": record["id"], "manifest_sha256": record["manifest_sha256"],
                "approved_at": record["approved_at"], "reason": "approved_staging_baseline"}

    async def capture(self):
        production, staging = await asyncio.gather(self.environments.environment_snapshot("production"),
                                                    self.environments.environment_snapshot("staging"))
        record = {"schema": "hermes-staging-baseline/v1", "captured_at": time.time(),
                  "production": production, "staging": staging,
                  "backups_verified": False, "runtime_parity_verified": False,
                  "isolation_verified": False, "ready": False}
        identifier = digest(record)
        record["id"] = identifier
        record["manifest_sha256"] = digest(record)
        private_save(self.root / (identifier + ".json"), record)
        return record

    def approve(self, identifier, expected_manifest, receipts, *, actor):
        if actor != "human" or not re.fullmatch(r"[a-f0-9]{64}", str(identifier)):
            raise ValueError("human_baseline_review_required")
        record = self._capture(identifier)
        if expected_manifest != record.get("manifest_sha256"):
            raise ValueError("baseline_revision_changed")
        self._known_runtime(record)
        self._verify_receipts(identifier, receipts)
        # These are operator-reviewed receipt hashes, not AI output. Keep the
        # immutable captured environments; don't accept replacement snapshots.
        record.update(approved_at=time.time(), reviewed_by="human", receipt_hashes=receipts)
        private_save(self.root / "approved.json", record)
        return self.status()

    async def ensure_current(self):
        if not self.status().get("ready"):
            raise ValueError("approved_staging_baseline_required")
        approved = json.loads((self.root / "approved.json").read_text())
        for environment in ("production", "staging"):
            current = await self.environments.environment_snapshot(environment)
            previous = approved[environment]
            # Product CSVs are separately protected by per-product CAS.
            for key in ("plugins", "themes", "mu_plugins", "custom_css", "snippets", "staging_isolation", "runtime_fingerprints"):
                if key not in previous or current.get(key) != previous[key]:
                    raise ValueError("staging_baseline_environment_changed")
        return self.status()


class AutonomousProductPipeline:
    def __init__(self, store, companion, environments, repairs, diagnoses, *, shipping=None, baseline=None, verifier=None):
        self.store, self.companion = store, companion
        self.environments, self.repairs, self.diagnoses = environments, repairs, diagnoses
        self.shipping, self.baseline = shipping, baseline
        self.verifier = verifier
        self.mapping = SourceMappingIndex(companion)
        self.root = Path(store.root) / "autonomous-companion" / "product-work"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._locks = {}

    def _path(self, cycle_id, product_id):
        if not re.fullmatch(r"[a-f0-9]{32}", str(cycle_id)) or not re.fullmatch(r"[1-9][0-9]{0,11}", str(product_id)):
            raise ValueError("monthly_product_identity_invalid")
        folder = self.root / cycle_id
        folder.mkdir(exist_ok=True, mode=0o700)
        return folder / (str(product_id) + ".json")

    def record(self, cycle_id, product_id):
        path = self._path(cycle_id, product_id)
        return json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else None

    def proposals_for_cycle(self, cycle_id):
        if not re.fullmatch(r"[a-f0-9]{32}", str(cycle_id)):
            return []
        result = []
        for path in (self.root / cycle_id).glob("*.json"):
            if not path.is_symlink():
                result.extend(json.loads(path.read_text()).get("repair_proposals", []))
        return result

    async def _diagnose(self, cycle, product, state, path):
        if state.get("diagnosis_id"):
            diagnosis = self.diagnoses.get(state["diagnosis_id"])
        else:
            state["source_dispatch_started"] = True
            private_save(path, state)
            diagnosis = await self.diagnoses.start(state["mapping"]["source_url"], focus=False)
            state["diagnosis_id"] = diagnosis["id"]
            private_save(path, state)
        task = self.diagnoses._tasks.get(diagnosis["id"])
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=120)
        return self.diagnoses.get(diagnosis["id"])

    async def _storefront_checks(self, cycle, product, state, checks, evidence):
        """Site checks remain useful even when a supplier mapping is missing."""
        if not self.environments.status("production").get("connected"):
            return
        try:
            production = await self.environments.snapshot("production", product["product_id"])
            state["production_snapshot"] = production
            evidence.append(digest(production))
            inventory = production.get("field_inventory")
            if inventory:
                checks["duplicate_fields"] = duplicate_field_findings(inventory)["status"]
            if self.shipping:
                shipping = await self.shipping(cycle, product, production)
                state["shipping_audit"] = shipping
                checks["shipping"] = shipping.get("status", "unknown")
            if self.verifier:
                runtime = await self.verifier.audit_snapshot("production", product["product_id"], production)
                state["storefront_verification"] = runtime
                evidence.append(digest(runtime))
                for name in ("rendered_prices", "artwork_dimensions", "customer_flow"):
                    checks[name] = runtime.get("checks", {}).get(name, "unknown")
                checks["retail_rule"] = runtime.get("checks", {}).get("retail_rule_unchanged", "unknown")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A connector/rate outage must not be mislabelled as a supplier
            # sign-out and block otherwise independent source collection.
            state["storefront_reason"] = safe_error(exc)

    async def __call__(self, cycle, product):
        pid, cid = str(product["product_id"]), cycle["id"]
        path = self._path(cid, pid)
        async with self._locks.setdefault((cid, pid), asyncio.Lock()):
            previous = self.record(cid, pid)
            mapping = self.mapping.resolve(product)
            checks = {key: "unknown" for key in REQUIRED_CHECKS}
            checks["source_mapping"] = "match" if mapping.get("mapping_status") == "confirmed" else "unknown"
            state = previous or {"cycle_id": cid, "product_id": pid, "mapping": mapping, "started_at": time.time(),
                                  "checks": checks, "repair_proposals": []}
            identity = lambda item: (item.get("supplier"), item.get("source_url"))
            if (identity(product) != identity(mapping) or (previous and identity(previous["mapping"]) != identity(mapping))):
                checks["source_mapping"] = "unknown"
                return self._finish(path, state, checks, "source_mapping_changed")
            if previous and previous.get("result"):
                return previous["result"]
            private_save(path, state)
            evidence_ids, diagnosis_id = [], None
            try:
                await self._storefront_checks(cycle, product, state, checks, evidence_ids)
                if mapping.get("mapping_status") != "confirmed":
                    return self._finish(path, state, checks, mapping.get("reason", "exact_supplier_mapping_required"), evidence=evidence_ids)
                if previous and previous.get("source_dispatch_started") and not previous.get("diagnosis_id"):
                    return self._finish(path, state, checks, "source_dispatch_outcome_uncertain", evidence=evidence_ids)
                pair_id = mapping.get("pair_id")
                if not pair_id and mapping["supplier"] == "4over":
                    item = mapping.get("product", {})
                    pair = self.companion.add_pair({"product_id": pid, "campaign_id": mapping["campaign_id"],
                        "source_url": mapping["source_url"], "storefront_url": item.get("storefront_url")})
                    pair_id = pair["id"]
                if mapping["supplier"] == "4over" and pair_id:
                    pair = self.companion.get_pair(pair_id)
                    source_product = self.companion._source_product(pair)
                    if source_product.get("fourover_option_map_evidence") and source_product.get("fourover_rows_evidence"):
                        # Cycle-aware collector IDs permit a new monthly read,
                        # while retaining exact once-only reads within a cycle.
                        if state.get("refresh_dispatched") and not state.get("source_refresh"):
                            return self._finish(path, state, checks, "source_dispatch_outcome_uncertain")
                        state["refresh_dispatched"] = True
                        private_save(path, state)
                        refresh = await self.companion.refresh_child_tables(pair_id, cycle_id=cid)
                        state["source_refresh"] = refresh
                        private_save(path, state)
                        audit = await self.companion.audit(pair_id)
                    else:
                        diagnosis = await self._diagnose(cycle, product, state, path)
                        diagnosis_id = diagnosis["id"]
                        if diagnosis.get("session_status") == "login_required":
                            return self._finish(path, state, checks, "supplier_login_required", status="access_required")
                        from .product_companion_observed import audit_observed_pair
                        details = await audit_observed_pair(self.companion, pair,
                                                           {**source_product, "diagnosis_id": diagnosis_id})
                        audit = self.companion._record_audit(pair, details)
                    state["audit_id"] = audit["id"]
                    report = audit["report"]
                    raw = report.get("checks", {})
                    aliases = {"options": "options", "quantities": "current_supplier_quantity_domain",
                        "source_prices": "current_supplier_prices", "production": "production", "documents": "documents",
                        "images": "images", "description": "description", "retail_rule": "retail_rule",
                        "rendered_prices": "rendered_retail_samples", "duplicate_fields": "storefront_duplicate_fields"}
                    for name, origin in aliases.items():
                        source_check = raw.get(origin, "unknown")
                        if checks[name] == "unknown" or source_check == "mismatch":
                            checks[name] = source_check
                    evidence_ids.append(audit["id"])
                    state["repair_proposals"] = self._proposals(audit, pair)
                    state["source_domain_complete"] = report.get("source_domain_complete") is True
                else:
                    # Native adapters expose current menus and source content.
                    # Unregistered complete pricing contracts stay explicit
                    # work for Codex rather than using sampled price formulas.
                    diagnosis = await self._diagnose(cycle, product, state, path)
                    diagnosis_id = diagnosis["id"]
                    state["source_observation"] = {key: diagnosis.get(key) for key in
                        ("id", "status", "session_status", "created_at", "fields", "content", "coverage")}
                    evidence_ids.append(digest(state["source_observation"]))
                    if diagnosis.get("session_status") == "login_required":
                        return self._finish(path, state, checks, "supplier_login_required", status="access_required", evidence=evidence_ids)
                    state["source_domain_complete"] = False
                    return self._finish(path, state, checks, "complete_supplier_comparison_adapter_required", evidence=evidence_ids)
                state["merchant_selling_scope"] = selling_scope_for_product(pid)
                return self._finish(path, state, checks, "product_checks_require_review", evidence=evidence_ids)
            except asyncio.CancelledError:
                state["interrupted"] = True
                private_save(path, state)
                raise
            except Exception as exc:
                reason = safe_error(exc)
                status = "access_required" if any(x in reason for x in ("login_required", "401", "access_required")) else "needs_attention"
                return self._finish(path, state, checks, reason, status=status, evidence=evidence_ids)
            finally:
                owned = diagnosis_id or state.get("diagnosis_id")
                if owned:
                    task = getattr(self.diagnoses, "_tasks", {}).get(owned)
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    await self.diagnoses._release(owned)

    def _proposals(self, audit, pair):
        result = []
        for index, proposal in enumerate(audit.get("report", {}).get("proposed_changes", [])):
            try:
                changes = self.companion.writes._operations(audit, pair, [index])
            except (ValueError, KeyError):
                changes = []
            if not changes:
                continue
            evidence = {"audit_id": audit["id"], "source_observed_at": audit["observed_at"],
                        "source_sha256": digest(audit["evidence"]), "changes_sha256": digest(changes),
                        "mapping_confirmed": True,
                        "source_domain_complete": audit["report"].get("source_domain_complete") is True,
                        "checks": {"options": audit["report"]["checks"].get("options", "unknown"),
                                   "quantities": audit["report"]["checks"].get("current_supplier_quantity_domain", "unknown"),
                                   "source_prices": audit["report"]["checks"].get("current_supplier_prices", "unknown"),
                                   "production": audit["report"]["checks"].get("production", "unknown"),
                                   "documents": "match" if all(op["kind"] == "add_document" and op["url"] in
                                       (audit.get("supplier_content") or {}).get("documents", []) for op in changes) else "unknown"}}
            proposal_id = digest({"audit": audit["id"], "index": index, "changes": changes})[:32]
            docs_only = all(op["kind"] == "add_document" for op in changes)
            eligible = docs_only or (evidence["source_domain_complete"] and all(evidence["checks"][key] == "match"
                                                                  for key in ("options", "quantities", "source_prices")))
            result.append({"id": proposal_id, "product_id": audit["product_id"], "kind": "add_document" if docs_only else "source_cost_or_options",
                           "eligible": eligible, "changes": changes, "evidence": evidence})
        return result

    def _finish(self, path, state, checks, reason, *, status=None, evidence=()):
        complete = (state.get("source_domain_complete") is True and bool(evidence)
                    and all(checks[key] == "match" for key in REQUIRED_CHECKS))
        status = status or ("verified" if complete else "mismatch" if "mismatch" in checks.values() else "needs_attention")
        result = {"status": status, "reason": "verified" if complete else reason,
                  "checks": checks, "required_checks": REQUIRED_CHECKS,
                  "coverage": {"complete": complete, "fresh": complete}, "evidence_complete": complete,
                  "evidence_ids": list(evidence), "repair_proposal_ids": [p["id"] for p in state["repair_proposals"]],
                  "repair_status": "proposed" if state["repair_proposals"] else "none",
                  "elapsed_seconds": max(0, time.time() - state["started_at"])}
        state.update(result=result, finished_at=time.time(), checks=checks)
        private_save(path, state)
        return result

    async def apply_decisions(self, cycle_id, decisions, *, authorization_guard=None):
        proposals = {p["id"]: p for p in self.proposals_for_cycle(cycle_id)}
        results = []
        for item in decisions:
            proposal = proposals.get(item.get("proposal_id"))
            if not proposal:
                raise ValueError("director_proposal_not_in_cycle")
            if item.get("action") != "stage_repair":
                results.append({"proposal_id": proposal["id"], "status": item.get("action", "held")})
                continue
            if authorization_guard is not None:
                authorization_guard()
            if not proposal.get("eligible") or not self.baseline or not self.baseline.status().get("ready"):
                results.append({"proposal_id": proposal["id"], "status": "held", "reason": "approved_staging_baseline_required"})
                continue
            async with self._locks.setdefault(("repair", cycle_id, proposal["id"]), asyncio.Lock()):
                await self.baseline.ensure_current()
                if authorization_guard is not None:
                    authorization_guard()
                folder = self._path(cycle_id, proposal["product_id"]).parent / "proposal-decisions"
                folder.mkdir(exist_ok=True, mode=0o700)
                if folder.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}", proposal["id"]):
                    raise ValueError("monthly_repair_identity_invalid")
                path = folder / (proposal["id"] + ".json")
                if path.is_symlink():
                    raise ValueError("monthly_repair_identity_invalid")
                receipt = json.loads(path.read_text()) if path.is_file() else None
                if receipt and receipt.get("proposal_sha256") != digest(proposal):
                    raise ValueError("monthly_repair_proposal_changed")
                if receipt and receipt.get("repair_id"):
                    preview = self.repairs.get(receipt["repair_id"])
                elif receipt:
                    # A crash after sending a preview may have left an orphaned
                    # remote preview. Never ask the model to dispatch it again.
                    results.append(receipt.get("result") or {"proposal_id": proposal["id"], "status": "held",
                                   "reason": "repair_preview_outcome_uncertain"})
                    continue
                else:
                    receipt = {"proposal_sha256": digest(proposal), "status": "preview_dispatching"}
                    private_save(path, receipt)
                    preview = await self.repairs.preview(proposal["product_id"], proposal["changes"], proposal["evidence"])
                    receipt.update(repair_id=preview.get("id"), status=preview["status"])
                    if not preview.get("id"):
                        receipt["result"] = {"proposal_id": proposal["id"], "status": preview["status"],
                                             "reason": preview.get("reason", "repair_preview_held")}
                    private_save(path, receipt)
                # The saved repair id is written before any product mutation.
                # Its workflow reconciles interrupted remote outcomes using the
                # same preview id and revision rather than creating a new one.
                if preview.get("status") in {"preview", "staging_applying", "staging_applied"}:
                    if authorization_guard is not None:
                        authorization_guard()
                    preview = await self.repairs.apply(preview["id"], preview["manifest"]["staging_revision"],
                                                       authorization_guard=authorization_guard)
                result = {"proposal_id": proposal["id"], "repair_id": preview.get("id"), "status": preview["status"]}
                receipt.update(result=result, status=preview["status"])
                private_save(path, receipt)
                results.append(result)
        return results
