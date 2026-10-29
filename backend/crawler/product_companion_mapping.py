"""Bounded authenticated mapping discovery for the first Standard Cards audit.

One product document is read by the existing 4over collector. No child-price
route is requested. A name or a matching row count cannot confirm the mapping.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from .product_companion import storefront_html_snapshot, selling_scope_for_product
from .product_companion_service import _atomic


STANDARD_SOURCE = "https://4over.com/standard-business-cards"
STANDARD_SITE = "https://printitprints.com/shop/standard-print/marketing-products/business-cards/popular-business-cards/standard-business-cards/"
_FIELDS = {"size", "shape", "radiusofcorners", "stock", "colorspec", "coating", "spotuvsides"}
_EMPTY_OPTIONAL_SOURCE_SENTINELS = {"radiusofcorners", "spotuvsides"}


def _label(value):
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _value(value):
    return " ".join(str(value or "").split())


def compare_option_paths(contract: dict, site: dict) -> dict:
    """Compare full conditional source bindings, including empty optional fields."""
    fields = tuple(sorted(set(site["fields"]) - {"quantity"}))
    if set(fields) != _FIELDS:
        return {"status": "unknown", "reason": "storefront_option_schema_changed"}
    if not contract.get("binding_complete"):
        return {"status": "unknown", "reason": contract.get("reason_code") or "source_binding_unverified"}
    rows = contract.get("rows")
    if not isinstance(rows, list) or not rows:
        return {"status": "unknown", "reason": "source_binding_rows_missing"}
    source_paths, children, normalized_sentinels = set(), set(), {}
    for row in rows:
        child = str(row.get("child_id", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,12}", child) or child in children:
            return {"status": "unknown", "reason": "source_binding_child_invalid"}
        children.add(child)
        selection = {}
        for field in row.get("fields", []):
            name = _label(field.get("label"))
            if not name or name in selection:
                return {"status": "unknown", "reason": "source_binding_field_invalid"}
            value = _value(field.get("choice"))
            # This source uses '-' only for an inapplicable optional field.
            # The reviewed storefront CSV stores that state as an empty cell,
            # which its calculator skips. Other labels retain their meaning.
            if name in _EMPTY_OPTIONAL_SOURCE_SENTINELS and value == "-":
                normalized_sentinels[name] = normalized_sentinels.get(name, 0) + 1
                value = ""
            selection[name] = value
        if set(selection) != set(fields):
            return {"status": "unknown", "reason": "source_option_schema_changed"}
        signature = tuple(selection[name] for name in fields)
        if signature in source_paths:
            return {"status": "unknown", "reason": "source_duplicate_option_path"}
        source_paths.add(signature)
    site_paths = {tuple(row["selection"][name] for name in fields) for row in site["rows"].values()}
    missing, extra = source_paths - site_paths, site_paths - source_paths
    return {"status": "match" if source_paths == site_paths else "mismatch",
            "fields": list(fields), "source_option_path_count": len(source_paths),
            "site_option_path_count": len(site_paths),
            "representation_normalization": {"source_value": "-", "site_value": "",
                "fields": sorted(_EMPTY_OPTIONAL_SOURCE_SENTINELS),
                "source_field_values_normalized": normalized_sentinels,
                "scope": "reviewed_inapplicable_optional_field_sentinel_only"},
            "missing_storefront_option_paths": [dict(zip(fields, path)) for path in sorted(missing)],
            "extra_storefront_option_paths": [dict(zip(fields, path)) for path in sorted(extra)]}


async def _discover_candidate_locked(service, product_id: str, source_url: str) -> dict:
    if str(product_id) != "6862" or source_url != STANDARD_SOURCE:
        raise ValueError("reviewed_standard_business_cards_mapping_scope_required")
    folder = Path(service.root) / "candidates"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / "6862.json"
    if path.is_symlink():
        raise ValueError("candidate_state_invalid")
    if path.exists():
        record = json.loads(path.read_text())
        if record.get("source_url") != source_url or record.get("storefront_url") != STANDARD_SITE:
            raise ValueError("candidate_mapping_changed")
        snapshot_id = record["snapshot_id"]
    else:
        snapshot_id = uuid.uuid4().hex
        record = {"schema": "hermes-companion-candidate/v1", "product_id": "6862",
                  "source_url": source_url, "storefront_url": STANDARD_SITE,
                  "snapshot_id": snapshot_id, "status": "pending", "created_at": time.time()}
        _atomic(path, record)
    async with service._locks.setdefault("candidate-6862", asyncio.Lock()):
        collection = await service._collector().collect(snapshot_id, source_url, [], request_budget=1,
            scope={"sets": 1, "job_sample": False, "digital_proof": False})
        site_html = await service._document(STANDARD_SITE, {"printitprints.com"})
        site = storefront_html_snapshot(site_html, url=STANDARD_SITE, product_id="6862")
        contract = collection.get("option_contract") or {}
        paths = compare_option_paths(contract, site)
        identity_status = record.get("supplier_pair_status")
        identity_confirmed = identity_status in {"confirmed_by_importer_and_source_binding", "confirmed_by_exact_source_binding"}
        if paths["status"] == "match":
            status, identity_status = "confirmed_option_mapping", identity_status or "confirmed_by_exact_source_binding"
        elif identity_confirmed:
            status = "confirmed_mapping_with_option_mismatch" if paths["status"] == "mismatch" else "confirmed_mapping_source_unknown"
        else:
            status = "unknown"
        observed_now = time.time()
        source_observed_at = (record.get("source_observed_at") or record.get("observed_at")
                              if collection.get("requests_this_call", 0) == 0 else observed_now)
        record.update(status=status, supplier_pair_status=identity_status,
            observed_at=source_observed_at or observed_now,
            source_observed_at=source_observed_at or observed_now, storefront_observed_at=observed_now,
            option_path_comparison=paths,
            source_discovery_status=collection.get("status"),
            source_error=(collection.get("error") or {}).get("code"),
            source_child_count=len(collection.get("declared_children", [])),
            source_option_contract=contract,
            site_row_count=site["row_count"],
            site_quantities=sorted({int(row["selection"]["quantity"]) for row in site["rows"].values()}),
            source_quantity_domain="unknown", source_current_prices="unknown", auto_correct_eligible=False,
            selling_scope=selling_scope_for_product(product_id),
            source_price_requests=0,
            requests_this_call=collection.get("requests_this_call", 0),
            source_document_sha256=collection.get("document_body_sha256"),
            storefront_document_sha256=site["html_sha256"],
            source_binding_sha256=hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest(),
            site_retail_rule=site["rule"], site_production=site["content"]["production"],
            next_step=("collect_current_child_tables_and_verify_quantity_services_production" if paths["status"] == "match"
                       else "review_conditional_option_differences" if paths["status"] == "mismatch" and identity_confirmed
                       else "review_source_mapping_or_session"))
        _atomic(path, record)
        return record


async def discover_candidate(service, product_id: str, source_url: str) -> dict:
    async with service._locks.setdefault("candidate-discovery-request", asyncio.Lock()):
        return await _discover_candidate_locked(service, product_id, source_url)
