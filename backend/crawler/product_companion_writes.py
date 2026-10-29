"""Reviewed Woo CSV writes with remote compare-and-swap and durable recovery.

The WordPress bridge owns the exact CSV bytes and backups. Hermes owns source
evidence, approval, and verification of the resulting public product page.
"""
from __future__ import annotations

import asyncio
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit, urljoin
import uuid

import httpx

from .product_companion import (current_child_table_snapshot, supplier_csv_snapshot,
                                storefront_html_snapshot)


HOSTS = {"printitprints.com", "www.printitprints.com", "staging3.tonyl36.sg-host.com"}


def _base(value):
    p = urlsplit(value)
    if (p.scheme != "https" or p.hostname not in HOSTS or p.username or p.password
            or p.port not in (None, 443) or p.query or p.fragment or p.path not in ("", "/")):
        raise ValueError("connector_requires_approved_https_store_origin")
    return f"https://{p.hostname}"


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ProductCompanionWrites:
    def __init__(self, companion, *, transport=None):
        self.companion = companion
        self.root = companion.root / "writes"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.config_path = self.root / "connection.json"
        self.transport = transport or self._http
        self._locks = {}

    def _save(self, path, value):
        from .product_companion_service import _atomic
        _atomic(path, value)

    def _config(self):
        if not self.config_path.is_file() or self.config_path.is_symlink():
            raise ValueError("woocommerce_write_connector_not_configured")
        return json.loads(self.config_path.read_text())

    def status(self):
        try:
            cfg = self._config()
            result = {"configured": True, "base_url": cfg["base_url"], "connected": cfg.get("connected", False)}
            if cfg.get("reason"):
                result["reason"] = cfg["reason"]
            return result
        except ValueError:
            return {"configured": False, "connected": False}

    async def configure(self, base_url, token):
        base_url = _base(base_url)
        if not isinstance(token, str) or not 32 <= len(token) <= 512 or any(c.isspace() for c in token):
            raise ValueError("invalid_connector_token")
        cfg = {"base_url": base_url, "token": token, "connected": False}
        # Verify the scoped bridge before replacing a working configuration.
        pairs = self.companion.list_pairs()
        if not pairs:
            raise ValueError("confirmed_product_mapping_required")
        try:
            snapshot = await self.transport(cfg, "GET", f'/snapshot/{pairs[0]["product_id"]}', None)
        except ValueError as exc:
            # Protected staging has an upstream browser preview sign-in. Keep
            # the explicitly supplied token private without claiming a verified
            # API connection or replacing an existing working connection.
            if (base_url == "https://staging3.tonyl36.sg-host.com"
                    and str(exc) == "woocommerce_connector_request_rejected_401"
                    and not self.status().get("connected")):
                cfg["reason"] = "staging_preview_authentication_required_external_api_not_connected"
                self._save(self.config_path, cfg)
                return self.status()
            raise
        if (str(snapshot.get("product_id")) != pairs[0]["product_id"] or not snapshot.get("revision")
                or not isinstance(snapshot.get("rows"), list) or not snapshot["rows"]):
            raise ValueError("woocommerce_connector_handshake_invalid")
        cfg["connected"] = True
        self._save(self.config_path, cfg)
        return self.status()

    @staticmethod
    async def _http(cfg, method, path, payload):
        try:
            async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
                response = await client.request(method,
                    cfg["base_url"] + "/wp-json/hermes-companion/v1" + path,
                    headers={"Authorization": "Bearer " + cfg["token"], "Cache-Control": "no-cache"}, json=payload)
            if response.status_code not in (200, 201):
                # Never include remote HTML, credentials, or response bodies in errors.
                raise ValueError("woocommerce_connector_request_rejected_" + str(response.status_code))
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("woocommerce_connector_response_invalid")
            return value
        except (httpx.HTTPError, json.JSONDecodeError):
            raise ValueError("woocommerce_connector_transport_failed") from None

    def get(self, change_id):
        if not isinstance(change_id, str) or len(change_id) != 32 or any(c not in "0123456789abcdef" for c in change_id):
            raise KeyError("companion_change_not_found")
        path = self.root / (change_id + ".json")
        if not path.is_file() or path.is_symlink():
            raise KeyError("companion_change_not_found")
        return json.loads(path.read_text())

    def history(self, pair_id):
        self.companion.get_pair(pair_id)
        records = []
        for path in self.root.glob("*.json"):
            if path.name == "connection.json" or path.is_symlink():
                continue
            value = json.loads(path.read_text())
            if value.get("pair_id") == pair_id:
                records.append(value)
        return sorted(records, key=lambda value: value["created_at"], reverse=True)

    def _source(self, audit, pair):
        product = self.companion._source_product(pair)
        info = product["fourover_rows_evidence"]
        saved = supplier_csv_snapshot(self.companion._evidence(info), expected_sha256=info["sha256"], source_url=pair["source_url"])
        option_map = json.loads(self.companion._evidence(product["fourover_option_map_evidence"]))
        snapshot_id = audit.get("current_source_snapshot_id")
        if not snapshot_id or snapshot_id != pair.get("fresh_source_snapshot_id"):
            raise ValueError("fresh_complete_supplier_tables_required_for_cost_write")
        if time.time() - pair.get("fresh_source_observed_at", 0) > 6 * 3600:
            raise ValueError("supplier_price_snapshot_expired")
        collection = self.companion._collector().get(snapshot_id, pair["source_url"])
        source = current_child_table_snapshot(option_map, collection, fields=saved["fields"], source_url=pair["source_url"])
        if source["sha256"] != audit["evidence"].get("current_child_snapshot_sha256"):
            raise ValueError("audited_supplier_snapshot_changed")
        return source

    def _operations(self, audit, pair, indexes):
        proposals = audit["report"]["proposed_changes"]
        if (not isinstance(indexes, list) or not indexes or len(set(indexes)) != len(indexes)
                or any(type(i) is not int or i < 0 or i >= len(proposals) for i in indexes)):
            raise ValueError("select_verified_proposals_for_preview")
        if time.time() - audit["observed_at"] > 6 * 3600:
            raise ValueError("audit_expired_run_a_new_audit")
        operations = []
        for index in indexes:
            proposal = proposals[index]
            if proposal["kind"] == "add_missing_template_links":
                declared = set((audit.get("supplier_content") or {}).get("documents", []))
                for url in proposal.get("urls", []):
                    if url not in declared or urlsplit(url).scheme != "https" or urlsplit(url).hostname != "4over.com":
                        raise ValueError("supplier_document_evidence_missing")
                    operations.append({"kind": "add_document", "url": url, "label": Path(urlsplit(url).path).name})
            elif proposal["kind"] in {"review_stored_supplier_costs", "add_missing_configurations"}:
                source = self._source(audit, pair)
                if any(audit["report"]["checks"].get(key) != "match" for key in
                       ("current_supplier_structure", "conditional_supplier_options", "current_supplier_quantity_domain")):
                    raise ValueError("incomplete_supplier_domain_blocks_cost_write")
                rows = audit["report"]["cost_mismatches"] if proposal["kind"] == "review_stored_supplier_costs" else audit["report"]["missing_site_rows"]
                for entry in rows:
                    selection = entry["selection"] if "selection" in entry else entry
                    key = tuple(selection[field] for field in source["fields"])
                    verified = source["rows"].get(key)
                    if verified is None:
                        raise ValueError("configuration_not_in_verified_source")
                    if proposal["kind"] == "add_missing_configurations":
                        if not verified.get("production") or audit["report"]["checks"].get("production") != "match":
                            raise ValueError("verified_production_required_before_adding_configuration")
                        operations.append({"kind": "add_row", "selection": selection,
                                           "cost": verified["cost"], "production": verified["production"]})
                    else:
                        operations.append({"kind": "set_cost", "selection": selection, "cost": verified["cost"]})
            else:
                raise ValueError("proposal_requires_manual_content_review")
        if not operations or len(operations) > 1000:
            raise ValueError("empty_or_oversized_write_preview")
        return operations

    async def _site(self, pair, cfg, nonce):
        path = urlsplit(pair["storefront_url"]).path
        url = cfg["base_url"] + path
        status, final, raw = await self.companion.fetch(url + "?hermes_companion_check=" + nonce)
        if status != 200 or urlsplit(final).hostname != urlsplit(cfg["base_url"]).hostname:
            raise ValueError("storefront_recheck_unavailable")
        # Parser validates the canonical product identity in the loaded calculator.
        canonical = "https://printitprints.com" + path
        return storefront_html_snapshot(raw.decode("utf-8", "replace"), url=canonical, product_id=pair["product_id"])

    @staticmethod
    def _row_signature(rows):
        return sorted((tuple(sorted(row["selection"].items())), Decimal(row["cost"]), row.get("production") or "") for row in rows)

    async def preview(self, audit_id, indexes):
        audit = self.companion.get_audit(audit_id)
        pair = self.companion.get_pair(audit["pair_id"])
        async with self._locks.setdefault(pair["id"], asyncio.Lock()):
            if pair["latest_audit_id"] != audit_id:
                raise ValueError("latest_audit_required_for_write_preview")
            operations = self._operations(audit, pair, indexes)
            cfg = self._config()
            if not cfg.get("connected"):
                raise ValueError("woocommerce_connector_connection_not_verified")
            snapshot = await self.transport(cfg, "GET", "/snapshot/" + pair["product_id"], None)
            site = await self._site(pair, cfg, uuid.uuid4().hex)
            if str(snapshot.get("product_id")) != pair["product_id"] or not snapshot.get("revision"):
                raise ValueError("connector_snapshot_identity_invalid")
            if self._row_signature(snapshot["rows"]) != self._row_signature(list(site["rows"].values())):
                raise ValueError("connector_csv_does_not_match_rendered_calculator")
            remote = await self.transport(cfg, "POST", "/previews", {
                "product_id": int(pair["product_id"]), "expected_revision": snapshot["revision"],
                    "audit_id": audit_id, "evidence": {"verified": True, "source_observed_at": int(audit["observed_at"]),
                    "sha256": _digest(audit["evidence"])}, "changes": operations})
            if not remote.get("preview_id") or not remote.get("diff"):
                raise ValueError("connector_preview_invalid")
            change_id = uuid.uuid4().hex
            record = {"id": change_id, "audit_id": audit_id, "pair_id": pair["id"], "product_id": pair["product_id"],
                "status": "preview", "created_at": time.time(), "base_url": cfg["base_url"],
                "expected_revision": snapshot["revision"], "remote_preview_id": remote["preview_id"],
                "expires_at": remote["expires_at"], "diff": remote["diff"], "operations": operations,
                "proposal_indexes": indexes, "before_rows": snapshot["rows"], "before_documents": snapshot.get("documents", []),
                "before_retail_script_sha256": site["rule"].get("script_sha256"), "can_rollback": False}
            self._save(self.root / (change_id + ".json"), record)
            return record

    async def _recheck(self, record, cfg, *, rollback=False):
        pair = self.companion.get_pair(record["pair_id"])
        snapshot = await self.transport(cfg, "GET", "/snapshot/" + record["product_id"], None)
        site = await self._site(pair, cfg, uuid.uuid4().hex)
        expected = copy.deepcopy(record["before_rows"])
        expected_docs = copy.deepcopy(record["before_documents"])
        if not rollback:
            for operation in record["operations"]:
                if operation["kind"] == "set_cost":
                    matches = [row for row in expected if row["selection"] == operation["selection"]]
                    if len(matches) != 1:
                        raise ValueError("expected_cost_configuration_ambiguous")
                    matches[0]["cost"] = operation["cost"]
                elif operation["kind"] == "add_document":
                    expected_docs.append({"url": operation["url"], "label": operation["label"]})
                elif operation["kind"] == "add_row":
                    expected.append({"selection": operation["selection"], "cost": operation["cost"],
                                     "production": operation["production"]})
        if (self._row_signature(expected) != self._row_signature(list(site["rows"].values()))
                or self._row_signature(expected) != self._row_signature(snapshot["rows"])):
            raise ValueError("loaded_storefront_rows_do_not_match_expected_csv")
        if site["rule"].get("script_sha256") != record["before_retail_script_sha256"]:
            raise ValueError("storefront_retail_rule_changed")
        documents = {item["url"] for item in snapshot.get("documents", [])}
        expected_documents = {item["url"] for item in record["before_documents"]}
        if sorted(snapshot.get("documents", []), key=lambda item: item["url"]) != sorted(expected_docs, key=lambda item: item["url"]):
            raise ValueError("document_state_differs_from_preview")
        for operation in record["operations"]:
            if operation["kind"] == "add_document" and not rollback:
                if operation["url"] not in documents or operation["url"] not in site["content"]["documents"]:
                    raise ValueError("document_link_not_visible_after_write")
            elif operation["kind"] in {"set_cost", "add_row"} and not rollback:
                matched = [row for row in site["rows"].values() if row["selection"] == operation["selection"]]
                if len(matched) != 1 or Decimal(matched[0]["cost"]) != Decimal(operation["cost"]):
                    raise ValueError("changed_cost_not_visible_after_write")
        if rollback and documents != expected_documents:
            raise ValueError("document_rollback_not_restored")
        result = {"status": "match", "csv_rows": len(site["rows"]), "retail_rule_preserved": True,
                  "storefront_sha256": site["html_sha256"], "observed_at": time.time()}
        costs = [op for op in record["operations"] if op["kind"] in {"set_cost", "add_row"}]
        if costs and not rollback:
            selected = {key: value for key, value in site["rows"].items() if any(value["selection"] == op["selection"] for op in costs)}
            sample_pair = {**pair, "storefront_url": cfg["base_url"] + urlsplit(pair["storefront_url"]).path}
            samples = await self.companion._rendered_retail_samples(sample_pair, {**site, "rows": selected})
            if samples["status"] != "match":
                raise ValueError("changed_rendered_customer_price_unverified")
            result["rendered_price_samples"] = samples
        return result

    async def apply(self, change_id, expected_revision):
        record = self.get(change_id)
        async with self._locks.setdefault(record["pair_id"], asyncio.Lock()):
            record = self.get(change_id)
            if record["status"] == "applied_verified":
                return record
            if record["status"] not in {"preview", "applying"} or expected_revision != record["expected_revision"]:
                raise ValueError("write_preview_state_or_revision_changed")
            cfg = self._config()
            if cfg["base_url"] != record["base_url"]:
                raise ValueError("connector_target_changed")
            receipt = None
            if record["status"] == "applying":
                recovery = await self.transport(cfg, "GET", "/previews/" + record["remote_preview_id"], None)
                if recovery.get("status") == "applied" and recovery.get("change"):
                    receipt = recovery["change"]
                elif recovery.get("status") == "reverted_after_failure" and recovery.get("current_revision") == record["expected_revision"]:
                    record.update(status="rolled_back_after_failed_apply", can_rollback=False, can_resume=False,
                                  rollback_recheck=await self._recheck(record, cfg, rollback=True))
                    self._save(self.root / (change_id + ".json"), record)
                    return record
                elif recovery.get("status") == "prepared" and recovery.get("current_revision") != record["expected_revision"]:
                    # The bridge can only settle the saved after state or
                    # compensate exact known mixed resources; it holds all
                    # unexplained edits. This path never authorizes a new write.
                    try:
                        receipt = await self.transport(cfg, "POST", "/apply/" + record["remote_preview_id"],
                                                       {"expected_revision": expected_revision})
                    except ValueError:
                        recovered = await self.transport(cfg, "GET", "/previews/" + record["remote_preview_id"], None)
                        if recovered.get("status") == "reverted_after_failure" and recovered.get("current_revision") == record["expected_revision"]:
                            record.update(status="rolled_back_after_failed_apply", can_rollback=False, can_resume=False,
                                          rollback_recheck=await self._recheck(record, cfg, rollback=True))
                            self._save(self.root / (change_id + ".json"), record)
                            return record
                        raise
                elif recovery.get("status") not in {"preview", "prepared"}:
                    raise ValueError("remote_write_requires_recovery_review")
            if receipt is None:
                if time.time() >= record["expires_at"]:
                    raise ValueError("write_preview_expired")
                audit = self.companion.get_audit(record["audit_id"])
                pair = self.companion.get_pair(record["pair_id"])
                if pair["latest_audit_id"] != record["audit_id"] or self._operations(audit, pair, record["proposal_indexes"]) != record["operations"]:
                    raise ValueError("write_source_evidence_changed")
                record.update(status="applying", can_resume=True)
                self._save(self.root / (change_id + ".json"), record)
                receipt = await self.transport(cfg, "POST", "/apply/" + record["remote_preview_id"], {"expected_revision": expected_revision})
            record.update(remote_change_id=receipt["change_id"], applied_revision=receipt["after_revision"],
                          status="applied", can_rollback=True, can_resume=False, applied_at=time.time())
            self._save(self.root / (change_id + ".json"), record)
            try:
                record["recheck"] = await self._recheck(record, cfg)
                record["status"] = "applied_verified"
            except Exception as exc:
                record["error"] = str(exc)[:160]
                try:
                    await self._rollback(record, cfg)
                    record["status"] = "rolled_back_after_failed_recheck"
                except Exception as rollback_exc:
                    if record.get("status") != "rollback_recheck_pending":
                        record.update(status="attention", rollback_error=str(rollback_exc)[:160])
                    else:
                        record["rollback_error"] = str(rollback_exc)[:160]
            self._save(self.root / (change_id + ".json"), record)
            return record

    async def _rollback(self, record, cfg):
        receipt = await self.transport(cfg, "POST", "/rollback/" + record["remote_change_id"],
                                       {"expected_revision": record["applied_revision"]})
        record.update(rollback_revision=receipt["revision"], status="rollback_recheck_pending",
                      can_rollback=False, can_resume=True, rolled_back_at=time.time())
        self._save(self.root / (record["id"] + ".json"), record)
        record["rollback_recheck"] = await self._recheck(record, cfg, rollback=True)
        record.update(can_rollback=False, can_resume=False)

    async def rollback(self, change_id):
        record = self.get(change_id)
        async with self._locks.setdefault(record["pair_id"], asyncio.Lock()):
            record = self.get(change_id)
            if record["status"] in {"rollback_verified", "rolled_back_after_failed_recheck"}:
                return record
            cfg = self._config()
            if cfg["base_url"] != record["base_url"]:
                raise ValueError("connector_target_changed")
            if record["status"] == "rollback_recheck_pending":
                record["rollback_recheck"] = await self._recheck(record, cfg, rollback=True)
                record["can_resume"] = False
            else:
                if not record.get("can_rollback"):
                    raise ValueError("change_has_no_active_backup")
                await self._rollback(record, cfg)
            record["status"] = "rollback_verified"
            self._save(self.root / (change_id + ".json"), record)
            return record
