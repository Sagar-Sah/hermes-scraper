"""Evidence-bound supplier/storefront comparison. No storefront writes.

The companion deliberately separates a saved supplier cost table from a fresh
supplier quote. A historic table can reveal a mismatch with stored Woo data,
but cannot certify today's vendor price or a green product.
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .adapters.printitprints import _declaration, retail_total
from .fourover_bulk import inspect_child_configurations, ChildConfigurationError
from .product_companion_fields import static_field_inventory, duplicate_field_findings


KNOWN_RETAIL_SCRIPTS = {
    # Original pinned calculator and the 2026-10-28 live calculator. Any new
    # script must be reviewed before prices are marked verified.
    "948b63c3764d3781dad5c369d1b0b3e6c7f8f3811ec3df80e5b459e1f85bafb5",
    "e1e423d549dacdfaf8eeaea852433fdafd49a315008290cdf2c41e54c3d9966b",
}
SOURCE_METADATA = {"product", "suppliertableprice", "currency", "productiontimeproductpagereference",
                   "imageurl", "verification", "sourceurl", "unitprice", "totalprice"}
SITE_METADATA = {"unitprice", "totalprice", "turnaround"}
MAX_ROWS = 100_000


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _money(value: object) -> Decimal:
    """Parse a bare nonnegative US amount without guessing its currency.

    Commas are removed only after validating three-digit thousands groups.
    Decimal also accepts exponent/underscore/sign syntax that source prices
    must not use; validate the spelling before constructing the exact value.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid_price")
    text = str(value).strip()
    if not re.fullmatch(r"(?:[0-9]+|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?", text):
        raise ValueError("invalid_price")
    try:
        amount = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid_price") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid_price")
    return amount


def _value(value: object) -> str:
    return " ".join(str(value or "").split())


def _columns(row: dict) -> dict[str, str]:
    names = {}
    for key in row:
        normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
        if normalized == "runsize":
            normalized = "quantity"
        if normalized in names:
            raise ValueError("duplicate_normalized_column")
        names[normalized] = key
    return names


def _row(row: dict, *, site: bool, dimensions: tuple[str, ...]) -> dict:
    names = _columns(row)
    fields = {}
    for name in dimensions:
        key = names.get(name)
        if key is None:
            raise ValueError("required_column_missing:" + name)
        fields[name] = _value(row[key])
    if "quantity" not in fields or not fields["quantity"].isdigit() or int(fields["quantity"]) < 1:
        raise ValueError("invalid_quantity")
    fields["quantity"] = str(int(fields["quantity"]))
    price_key = names.get("totalprice" if site else "suppliertableprice")
    if price_key is None:
        raise ValueError("cost_column_missing")
    production_key = names.get("turnaround") if site else None
    return {"selection": fields, "cost": format(_money(row[price_key]), "f"),
            "production": _value(row[production_key]) if production_key else None}


def _index(rows: list[dict], *, site: bool) -> tuple[dict[tuple[str, ...], dict], tuple[str, ...]]:
    if not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError("row_count_out_of_bounds")
    first = _columns(rows[0])
    dimensions = tuple(sorted(set(first) - (SITE_METADATA if site else SOURCE_METADATA)))
    if "quantity" not in dimensions:
        raise ValueError("quantity_column_missing")
    indexed = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("invalid_row")
        item = _row(raw, site=site, dimensions=dimensions)
        key = tuple(item["selection"][name] for name in dimensions)
        if key in indexed:
            raise ValueError("duplicate_configuration")
        indexed[key] = item
    return indexed, dimensions


def supplier_csv_snapshot(raw: bytes, *, expected_sha256: str, source_url: str) -> dict:
    if _sha(raw) != expected_sha256:
        raise ValueError("source_evidence_hash_changed")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    indexed, dimensions = _index(rows, site=False)
    if any(_value(row.get("Source URL")) != source_url for row in rows):
        raise ValueError("source_url_mismatch")
    return {"rows": indexed, "row_count": len(indexed), "fields": dimensions, "sha256": expected_sha256}


def selling_scope_for_product(product_id: str) -> dict | None:
    # Explicit merchant decision for Standard Business Cards only. This is a
    # selling limit, never a supplier collection limit or a generic tier cap.
    if str(product_id) == "6862":
        return {"quantity_maximum": 25000, "authority": "merchant",
                "reason": "merchant_requested_online_quantity_maximum"}
    return None


def current_child_table_snapshot(option_map: dict, collection: dict, *, fields: tuple[str, ...], source_url: str) -> dict:
    """Project every current 4over child table into exact option/quantity rows.

    This is a child-price table comparison, not a payable order quote. Missing
    children, changed options, or duplicate rows abort the projection.
    """
    if (collection.get("source_url") != source_url or collection.get("status") != "completed_requested_subset"
            or collection.get("scope") != {"sets": 1, "job_sample": False, "digital_proof": False}):
        raise ValueError("current_child_table_scope_incomplete")
    mapped = {str(row["child_id"]): row for row in option_map.get("rows", [])}
    declared = {str(row["child_id"]) for row in collection.get("declared_children", [])}
    observations = {str(row["child_id"]): row for row in collection.get("observations", [])}
    if not mapped or set(mapped) != declared or set(mapped) != set(observations):
        raise ValueError("current_child_table_coverage_incomplete")
    contract = collection.get("option_contract") or {}
    if not contract.get("binding_complete"):
        raise ValueError("current_option_binding_unverified")
    current_bindings = {str(row["child_id"]): row for row in contract.get("rows", [])}
    if set(current_bindings) != set(mapped):
        raise ValueError("current_option_binding_changed")
    def signature(row):
        return sorted((re.sub(r"[^a-z0-9]", "", field["label"].casefold()),
                       str(field["value"]), _value(field["choice"])) for field in row["fields"])
    if any(signature(mapped[child]) != signature(current_bindings[child]) for child in mapped):
        raise ValueError("current_option_binding_changed")
    rows, quantity_domains = {}, {}
    for child_id, mapping in mapped.items():
        selections = {}
        for field in mapping["fields"]:
            key = re.sub(r"[^a-z0-9]", "", field["label"].casefold())
            if key in selections:
                raise ValueError("duplicate_child_option_field")
            selections[key] = _value(field["choice"])
        if set(selections) != set(fields) - {"quantity"}:
            raise ValueError("child_option_fields_changed")
        observation = observations[child_id]
        if (observation.get("status") != "collected" or observation.get("source_url") != source_url
                or observation.get("scope") != collection["scope"] or observation.get("tier_prices")):
            raise ValueError("current_child_table_invalid")
        tiers = observation.get("package_prices")
        if not isinstance(tiers, list) or not tiers:
            raise ValueError("current_child_quantity_domain_missing")
        quantity_domains[child_id] = []
        for tier in tiers:
            quantity = tier.get("quantity")
            if type(quantity) is not int or quantity < 1:
                raise ValueError("invalid_current_child_quantity")
            quantity_domains[child_id].append(quantity)
            selection = {**selections, "quantity": str(quantity)}
            key = tuple(selection[field] for field in fields)
            if key in rows:
                raise ValueError("duplicate_current_child_configuration")
            rows[key] = {"selection": selection, "cost": format(_money(tier.get("price")), "f"), "production": None}
    # Coverage is a derived read annotation added after the first saved
    # snapshots. It is not supplier evidence and must not alter their digest.
    evidence_collection = {key: value for key, value in collection.items() if key != "quantity_coverage"}
    digest = _sha(json.dumps(evidence_collection, sort_keys=True, default=str).encode())
    return {"rows": rows, "row_count": len(rows), "fields": fields, "sha256": digest,
            "scope": "current_4over_child_price_tables_only",
            "quantity_domains_by_child": quantity_domains,
            "quantity_coverage": {
                "method": "all_returned_child_package_price_rows",
                "presentation_limit_applied": False,
                "table_children_complete": True,
                "expanded_rendered_domain": "unknown",
                "source_domain_complete": False,
            }}


def _scope(scripts: list[str]) -> dict | None:
    values = []
    for text in scripts:
        match = re.search(r"(?:window\.)?PILC_SCOPE\s*=\s*", text)
        if match:
            try:
                value, _ = json.JSONDecoder().raw_decode(text[match.end():].lstrip())
                if isinstance(value, dict):
                    values.append(value)
            except ValueError:
                pass
    return values[0] if len(values) == 1 else None


def _retail_rule(scripts: list[str], product_id: str) -> dict:
    calculators = [text for text in scripts if "function profit_for_printing(" in text]
    digest = _sha(calculators[0].encode()) if len(calculators) == 1 else None
    scope = _scope(scripts)
    if digest not in KNOWN_RETAIL_SCRIPTS:
        return {"status": "unknown", "reason": "calculator_script_unreviewed", "script_sha256": digest}
    if digest == "e1e423d549dacdfaf8eeaea852433fdafd49a315008290cdf2c41e54c3d9966b":
        if scope is None or type(scope.get("retailMarkup")) is not bool:
            return {"status": "unknown", "reason": "retail_scope_unavailable", "script_sha256": digest}
        if scope["retailMarkup"]:
            # This hash has a separate server-authoritative path. It requires
            # a reviewed server rule before an exact customer price claim.
            return {"status": "unknown", "reason": "server_retail_rule_not_read", "script_sha256": digest}
    membership = set()
    for name in ["business_product_ids_are"] + [f"marketing_product_ids_are_part_{i}" for i in range(1, 5)]:
        match = re.search(r"const\s+" + name + r"\s*=\s*(\[[\d\s,]+\])\s*;", calculators[0])
        if not match:
            return {"status": "unknown", "reason": "retail_membership_unavailable", "script_sha256": digest}
        membership.update(json.loads(match.group(1)))
    return {"status": "verified", "kind": "site_pinned_legacy_markup", "script_sha256": digest,
            "member": int(product_id) in membership or int(product_id) in {7426, 6669},
            "scope": scope}


def storefront_html_snapshot(html: str, *, url: str, product_id: str) -> dict:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"printitprints.com", "www.printitprints.com"}:
        raise ValueError("storefront_url_invalid")
    soup = BeautifulSoup(html, "html.parser")
    scripts = [node.string or node.get_text() for node in soup.select("script:not([src])")]
    bound = _declaration(scripts, "current_product_id_is_this")
    if str(bound) != str(product_id):
        raise ValueError("storefront_product_identity_mismatch")
    raw_rows = _declaration(scripts, "csvdata")
    if not isinstance(raw_rows, list):
        raise ValueError("storefront_rows_unavailable")
    indexed, dimensions = _index(raw_rows, site=True)
    rule = _retail_rule(scripts, str(product_id))
    title = soup.select_one("h1.product_title")
    summary = soup.select_one(".woocommerce-product-details__short-description")
    description = soup.select_one("#tab-description")
    images = sorted({urljoin(url, node.get("data-large_image") or node.get("src"))
                     for node in soup.select(".woocommerce-product-gallery img")
                     if node.get("data-large_image") or node.get("src")})
    documents = sorted({urljoin(url, node["href"]) for node in soup.select("a[href]")
                        if re.search(r"\.(?:pdf|ai|eps|psd|zip)(?:[?#]|$)", node["href"], re.I)})
    faq_nodes = soup.select(".faq, #tab-faq, [itemtype*='FAQPage']")
    faq = [node.get_text(" ", strip=True) for node in faq_nodes]
    production = sorted(set(re.findall(r"\b\d+\s*(?:-\s*\d+)?\s*Business Days?\b",
                        (summary.get_text(" ", strip=True) if summary else "") + " " +
                        (description.get_text(" ", strip=True) if description else ""), re.I)))
    return {"product_id": str(product_id), "url": url, "title": title.get_text(" ", strip=True) if title else "",
            "rows": indexed, "row_count": len(indexed), "fields": dimensions, "rule": rule,
            "field_inventory": static_field_inventory(html, url=url),
            "content": {"summary": summary.get_text(" ", strip=True) if summary else "",
                        "description": description.get_text(" ", strip=True) if description else "",
                        "images": images, "documents": documents, "faq": faq,
                        "faq_section_present": bool(faq_nodes), "production": production},
            "html_sha256": _sha(html.encode())}


def woocommerce_public_snapshot(raw: bytes, *, product_id: str, storefront_url: str) -> dict:
    """Read the public Woo Store API, without mistaking its base price for the custom calculator."""
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("woocommerce_snapshot_invalid") from exc
    if not isinstance(payload, dict) or str(payload.get("id")) != str(product_id):
        raise ValueError("woocommerce_product_identity_mismatch")
    if payload.get("permalink", "").rstrip("/") != storefront_url.rstrip("/"):
        raise ValueError("woocommerce_permalink_mismatch")
    prices = payload.get("prices")
    if not isinstance(prices, dict) or prices.get("currency_code") != "USD":
        raise ValueError("woocommerce_price_currency_unknown")
    images = payload.get("images")
    if not isinstance(images, list):
        raise ValueError("woocommerce_images_unavailable")
    return {"product_id": str(product_id), "name": str(payload.get("name", "")),
            "type": str(payload.get("type", "")), "sku": str(payload.get("sku", "")),
            "native_price_minor_units": str(prices.get("price", "")),
            "currency_minor_unit": prices.get("currency_minor_unit"),
            "has_native_options": payload.get("has_options"),
            "attributes": payload.get("attributes") if isinstance(payload.get("attributes"), list) else [],
            "images": [str(item.get("src", "")) for item in images if isinstance(item, dict)],
            "description": str(payload.get("description", "")),
            "short_description": str(payload.get("short_description", "")),
            "sha256": _sha(raw)}


def supplier_page_content(html: str, *, url: str) -> dict:
    p = urlsplit(url)
    if p.scheme != "https" or p.hostname != "4over.com":
        raise ValueError("supplier_url_invalid")
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h1.page-title, h1")
    documents = sorted({urljoin(url, node["href"]) for node in soup.select("a[href]")
                        if "/media/asset/templates/" in node["href"]
                        and re.search(r"\.(?:pdf|ai|eps|psd|zip)(?:[?#]|$)", node["href"], re.I)})
    faq_nodes = soup.select(".faq, #tab-faq, [itemtype*='FAQPage']")
    images = sorted({urljoin(url, node.get("src") or node.get("data-src"))
                     for node in soup.select(".product.media img") if node.get("src") or node.get("data-src")})
    return {"title": heading.get_text(" ", strip=True) if heading else "",
            "documents": documents, "images": images,
            "field_inventory": static_field_inventory(html, url=url),
            "faq_section_present": bool(faq_nodes),
            "faq": [node.get_text(" ", strip=True) for node in faq_nodes],
            "html_sha256": _sha(html.encode())}


def _document_key(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]).casefold()
    # WordPress may rename an uploaded duplicate with a terminal -1.
    return re.sub(r"-1(?=\.(?:pdf|ai|eps|psd|zip)$)", "", name)


def live_fourover_structure(html: str, *, url: str, expected_child_ids: set[str]) -> dict:
    try:
        children, diagnostic = inspect_child_configurations(html, source_url=url)
    except ChildConfigurationError as exc:
        return {"status": "unknown", "reason": exc.code, "diagnostic": exc.diagnostic}
    current = {item["child_id"] for item in children}
    return {"status": "match" if current == expected_child_ids else "mismatch",
            "child_count": len(current), "saved_child_count": len(expected_child_ids),
            "new_child_ids": sorted(current - expected_child_ids),
            "missing_child_ids": sorted(expected_child_ids - current), "diagnostic": diagnostic,
            "html_sha256": _sha(html.encode())}


def compare_snapshots(source: dict, site: dict, *, source_freshness: str,
                      structure: dict, source_content: dict | None = None,
                      selling_scope: dict | None = None) -> dict:
    source_rows, site_rows = source["rows"], site["rows"]
    full_source_rows = source_rows
    if selling_scope is not None:
        maximum = selling_scope.get("quantity_maximum")
        if (selling_scope.get("authority") != "merchant" or type(maximum) is not int
                or maximum < 1 or maximum > 10**12):
            raise ValueError("invalid_merchant_selling_scope")
        source_rows = {key: row for key, row in source_rows.items()
                       if int(row["selection"]["quantity"]) <= maximum}
    missing_fields = sorted(set(source["fields"]) - set(site["fields"]))
    extra_fields = sorted(set(site["fields"]) - set(source["fields"]))
    fields_match = not missing_fields and not extra_fields
    source_keys, site_keys = set(source_rows), set(site_rows)
    missing, extra = source_keys - site_keys, site_keys - source_keys
    mismatched = []
    for key in sorted(source_keys & site_keys) if fields_match else []:
        vendor, stored = _money(source_rows[key]["cost"]), _money(site_rows[key]["cost"])
        if vendor != stored:
            mismatched.append({"selection": source_rows[key]["selection"], "supplier_cost": format(vendor, "f"), "site_cost": format(stored, "f")})
    costs = "unknown" if not fields_match else "mismatch" if mismatched else "match"
    options = "mismatch" if missing or extra or not fields_match else "match"
    rule = site["rule"]
    price = "unknown"
    price_examples = []
    if rule["status"] == "verified":
        for key in (sorted(source_keys & site_keys)[:5] if fields_match else []):
            stored = site_rows[key]
            expected, _ = retail_total(stored["cost"], rule["member"])
            price_examples.append({"selection": stored["selection"], "site_cost": stored["cost"], "expected_retail": expected})
        # Static HTML has no selected rendered price and cannot prove the cart.
    documents = "unknown"
    missing_documents = []
    if source_content is not None:
        site_doc_keys = {_document_key(url) for url in site["content"]["documents"]}
        missing_documents = sorted(url for url in source_content["documents"] if _document_key(url) not in site_doc_keys)
        documents = "mismatch" if missing_documents else "match"
    faq = "unknown"
    if source_content is not None:
        if site["content"]["faq_section_present"] and not any(site["content"]["faq"]):
            faq = "mismatch"
        elif not source_content["faq_section_present"]:
            # Merchant-authored FAQs remain valid; an empty block does not.
            faq = "match"
    field_checks = {"storefront": duplicate_field_findings(site.get("field_inventory")),
                    "supplier": duplicate_field_findings((source_content or {}).get("field_inventory"))}
    checks = {"options": options, "stored_costs_vs_saved_source": costs,
              "storefront_duplicate_fields": field_checks["storefront"]["status"],
              "supplier_duplicate_fields": field_checks["supplier"]["status"],
              "current_supplier_structure": structure["status"],
              "current_supplier_quantity_domain": "unknown",
              "conditional_supplier_options": "unknown",
              "conditional_supplier_services": "unknown",
              "current_supplier_prices": "unknown" if source_freshness != "current_exact_quotes" else costs,
              "retail_rule": rule["status"], "rendered_retail_prices": price,
              "documents": documents, "production": "unknown", "images": "unknown",
              "description": "unknown", "faq": faq}
    status = "mismatch" if "mismatch" in checks.values() else "unknown" if "unknown" in checks.values() else "green"
    proposals = []
    if missing_fields or extra_fields:
        proposals.append({"kind": "review_option_fields", "missing_fields": missing_fields,
                          "extra_fields": extra_fields, "requires_review": True})
    if fields_match and missing:
        proposals.append({"kind": "add_missing_configurations", "count": len(missing),
                          "requires_review": True, "source_price_freshness_required": True})
    if fields_match and extra:
        proposals.append({"kind": "review_extra_storefront_configurations", "count": len(extra),
                          "requires_review": True})
    if mismatched:
        proposals.append({"kind": "review_stored_supplier_costs", "count": len(mismatched),
                          "requires_review": True, "source_price_freshness_required": True})
    if missing_documents:
        proposals.append({"kind": "add_missing_template_links", "count": len(missing_documents),
                          "urls": missing_documents, "requires_review": True})
    if faq == "mismatch":
        proposals.append({"kind": "hide_empty_faq_section", "requires_review": True})
    for location, analysis in field_checks.items():
        for finding in analysis["findings"]:
            proposals.append({"kind": "review_" + location + "_duplicate_fields", "count": len(finding["observations"]),
                              "finding_id": finding["id"], "purpose": finding["purpose"],
                              "requires_review": True, "automatic": False,
                              "correction": finding["proposed_correction"]})
    return {"status": status, "checks": checks,
            "field_checks": field_checks,
            "counts": {"source_rows": len(source_rows), "site_rows": len(site_rows),
                       "source_rows_outside_selling_scope": len(full_source_rows) - len(source_rows),
                       "matching_rows": len(source_keys & site_keys) if fields_match else 0, "missing_site_rows": len(missing) if fields_match else 0,
                       "extra_site_rows": len(extra) if fields_match else 0, "cost_mismatches": len(mismatched)},
            "missing_site_fields": missing_fields, "extra_site_fields": extra_fields,
            "missing_site_rows": [source_rows[k]["selection"] for k in sorted(missing)] if fields_match else [],
            "extra_site_rows": [site_rows[k]["selection"] for k in sorted(extra)] if fields_match else [],
            "cost_mismatches": mismatched, "missing_documents": missing_documents,
            "source_document_count": len(source_content["documents"]) if source_content else None,
            "site_document_count": len(site["content"]["documents"]),
            "retail_rule": rule, "retail_examples": price_examples,
            "source_freshness": source_freshness,
            "selling_scope": selling_scope,
            "saved_supplier_quantities": sorted({int(row["selection"]["quantity"]) for row in full_source_rows.values()}),
            "applicable_supplier_quantities": sorted({int(row["selection"]["quantity"]) for row in source_rows.values()}),
            "proposed_changes": proposals}
