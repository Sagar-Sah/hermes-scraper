"""Conservative Companion reports before a complete supplier price table exists.

An attached, exact-URL diagnosis permits field/content checks. It does not turn
unobserved prices or conditional combinations into matching source rows.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time

from .product_companion import storefront_html_snapshot, woocommerce_public_snapshot, supplier_page_content
from .product_companion_fields import duplicate_field_findings
from .product_companion_control_coverage import observed_source_controls


def read_source_diagnosis(store_root: Path, product: dict, source_url: str) -> tuple[dict, str, bytes]:
    identifier = product.get("diagnosis_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("source_table_or_attached_diagnosis_required")
    path = store_root / "diagnoses" / (identifier + ".json")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 12 * 1024 * 1024:
        raise ValueError("attached_source_diagnosis_invalid")
    raw = path.read_bytes()
    record = json.loads(raw)
    if (not isinstance(record, dict) or record.get("id") != identifier or record.get("url") != source_url
            or record.get("supplier_id") != "fourover" or not isinstance(record.get("fields"), list)
            or any(not isinstance(field, dict) for field in record["fields"])):
        raise ValueError("attached_source_diagnosis_identity_changed")
    return record, hashlib.sha256(raw).hexdigest(), raw


def observed_option_check(diagnosis: dict, site: dict, *, now: float | None = None) -> dict:
    """Compare declared menus only; never certify their Cartesian combinations."""
    if (not isinstance(diagnosis, dict) or not isinstance(diagnosis.get("fields"), list)
            or any(not isinstance(field, dict) for field in diagnosis["fields"])):
        return {"status": "unknown", "reason": "source_observation_fields_invalid", "fields": []}
    try:
        observed_at = datetime.fromisoformat(diagnosis["created_at"]).timestamp()
        fresh = 0 <= (time.time() if now is None else now) - observed_at <= 6 * 3600
    except (KeyError, TypeError, ValueError):
        fresh = False
    if (diagnosis.get("status") != "ready" or diagnosis.get("session_status") == "login_required" or not fresh):
        return {"status": "unknown", "reason": "source_observation_not_current_and_ready", "fields": []}
    aliases = {"width": "width", "height": "height", "stock": "stock", "colorspec": "colorspec"}
    checks = []
    for field in diagnosis["fields"]:
        name = re.sub(r"[^a-z]", "", str(field.get("label", "")).casefold())
        semantic = aliases.get(name)
        if semantic is None or semantic not in site["fields"] or field.get("disabled") is not False:
            continue
        choices = field.get("choices")
        if not isinstance(choices, list) or not choices or len(choices) > 500:
            continue
        source = {str(c.get("value") if semantic in {"width", "height"} else c.get("label", "")).strip()
                  for c in choices if isinstance(c, dict) and c.get("value") and c.get("disabled") is False}
        if not source:
            continue
        target = {row["selection"][semantic] for row in site["rows"].values()}
        checks.append({"field": semantic, "status": "match" if source == target else "mismatch",
                       "source_values": sorted(source), "storefront_values": sorted(target),
                       "missing_storefront_values": sorted(source - target),
                       "extra_storefront_values": sorted(target - source), "source_field_key": field.get("key")})
    return {"status": "mismatch" if any(x["status"] == "mismatch" for x in checks) else "match" if checks else "unknown",
            "reason": "declared_menu_values_only_conditional_combinations_unverified", "fields": checks}


async def audit_observed_pair(service, pair: dict, product: dict) -> dict:
    """Run a useful read-only audit while unavailable checks remain Unknown."""
    diagnosis, diagnosis_sha, diagnosis_raw = read_source_diagnosis(Path(service.store.root), product, pair["source_url"])
    evidence_dir = service.root / "evidence"
    evidence_dir.mkdir(mode=0o700, exist_ok=True)
    preserved = evidence_dir / (diagnosis_sha + ".json")
    if preserved.is_symlink():
        raise ValueError("source_observation_evidence_invalid")
    if not preserved.exists():
        import os
        with preserved.open("xb") as handle:
            os.chmod(preserved, 0o600)
            handle.write(diagnosis_raw)
            handle.flush()
            os.fsync(handle.fileno())
    if hashlib.sha256(preserved.read_bytes()).hexdigest() != diagnosis_sha:
        raise ValueError("source_observation_evidence_changed")
    site_html = await service._document(pair["storefront_url"], {"printitprints.com", "www.printitprints.com"})
    site = storefront_html_snapshot(site_html, url=pair["storefront_url"], product_id=pair["product_id"])
    woo_url = "https://printitprints.com/wp-json/wc/store/v1/products/" + pair["product_id"]
    status, final_url, raw = await service.fetch(woo_url)
    if status != 200 or final_url != woo_url:
        raise ValueError("woocommerce_snapshot_unavailable")
    woo = woocommerce_public_snapshot(raw, product_id=pair["product_id"], storefront_url=pair["storefront_url"])
    async with service._source_lock:
        source_html = await service._document(pair["source_url"], {"4over.com"})
    source_content = supplier_page_content(source_html, url=pair["source_url"])
    rendered = await service._rendered_retail_samples(pair, site)
    if rendered.get("field_inventory"):
        site["field_inventory"] = rendered["field_inventory"]
    field_checks = {"storefront": duplicate_field_findings(site.get("field_inventory")),
                    "supplier": duplicate_field_findings(source_content.get("field_inventory"))}
    option_values = observed_option_check(diagnosis, site)
    control_review = observed_source_controls(diagnosis, site.get("field_inventory"),
        source_url=pair["source_url"], storefront_url=pair["storefront_url"],
        product_id=pair["product_id"], diagnosis_sha256=diagnosis_sha)
    area_rule = rendered.get("area_rule") or {}
    public_rule_status = "match" if area_rule.get("public_rule_status") == "verified" else "unknown"
    checks = {"observed_supplier_menu_values": option_values["status"],
              "observed_source_controls": control_review["status"],
              "storefront_duplicate_fields": field_checks["storefront"]["status"],
              "supplier_duplicate_fields": field_checks["supplier"]["status"],
              "current_supplier_structure": "unknown", "conditional_supplier_options": "unknown",
              "current_supplier_quantity_domain": "unknown", "current_supplier_prices": "unknown",
              "stored_costs_vs_current_source": "unknown", "retail_rule": "unknown",
              "loaded_public_retail_rule": public_rule_status,
              "rendered_retail_samples": rendered["status"], "woocommerce_identity": "match",
              "woocommerce_checkout_price": "unknown", "production": "unknown", "documents": "unknown",
              "images": "unknown", "description": "unknown", "faq": "unknown"}
    proposals = []
    for item in option_values["fields"]:
        if item["status"] == "mismatch":
            proposals.append({"kind": "review_observed_option_values", **item, "requires_review": True,
                              "automatic": False, "conditional_source_coverage_required": True})
    for location, findings in field_checks.items():
        for finding in findings["findings"]:
            proposals.append({"kind": "review_" + location + "_duplicate_fields", "finding_id": finding["id"],
                              "count": len(finding["observations"]), "purpose": finding["purpose"],
                              "requires_review": True, "automatic": False,
                              "correction": finding["proposed_correction"]})
    for finding in control_review["findings"]:
        proposals.append({"kind": "review_observed_source_control", "purpose": finding["purpose"],
                          "source_label": finding["source_label"], "source_field_key": finding["source_field_key"],
                          "requires_review": True, "automatic": False,
                          "merchant_selling_intent": "unknown", "conditional_source_coverage_required": True,
                          "correction": finding["proposed_correction"]})
    outcome = "mismatch" if "mismatch" in checks.values() else "unknown"
    report = {"status": outcome, "checks": checks, "field_checks": field_checks,
              "counts": {"source_rows": 0, "site_rows": site["row_count"], "matching_rows": 0,
                         "missing_site_rows": 0, "extra_site_rows": 0, "cost_mismatches": 0},
              "missing_site_rows": [], "extra_site_rows": [], "cost_mismatches": [], "retail_examples": [],
              "source_freshness": "rendered_page_observation_only_no_current_price_table",
              "retail_rule": {"status": "unknown", "reason": "server_rule_and_current_source_costs_unverified",
                              "public_rule": area_rule or None},
              "rendered_retail_samples": rendered, "observed_option_values": option_values,
              "observed_source_controls": control_review,
              "proposed_changes": proposals, "automatic_correction_eligible": False,
              "source_domain_complete": False,
              "scope": "current_rendered_fields_and_declared_menus_not_complete_source_pricing_or_combinations"}
    return {"audit_mode": "observed_snapshot", "report": report, "supplier_structure": {"status": "unknown"},
            "site_content": site["content"], "supplier_content": source_content,
            "field_observations": {"storefront": site.get("field_inventory"), "supplier": source_content.get("field_inventory")},
            "woocommerce_snapshot": woo, "current_source_snapshot_id": None,
            "source_diagnosis_id": diagnosis["id"],
            "source_diagnosis_evidence": {"object_path": str(preserved.relative_to(service.store.root)), "sha256": diagnosis_sha},
            "evidence": {"supplier_rows_sha256": None, "supplier_option_map_sha256": None,
                         "current_child_snapshot_sha256": None, "source_diagnosis_sha256": diagnosis_sha,
                         "supplier_page_sha256": source_content["html_sha256"], "storefront_page_sha256": site["html_sha256"],
                         "woocommerce_snapshot_sha256": woo["sha256"],
                         "retail_script_sha256": (rendered.get("observations", {}).get("loaded_public_script") or {}).get("sha256")}}
