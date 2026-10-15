"""Read-only reconciliation of saved DOM quotes and exact source components.

No HTTP client or model is used. Equal partial sums are diagnostics, not passes:
every selected component and service needs a proven mapping and exact price.
Missing endpoints, boolean catalog flags and residuals never imply a zero fee.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlsplit

from .adapters.browser_tables import (
    TableSchemaError,
    parse_option_catalog,
    parse_price_table,
)
from .service_context import canonical_service_context, service_context_is_bound

SOURCE_HOSTS = {"printograph.com", "www.printograph.com"}
BASE_KEYS = {"size", "paper", "color", "orientation", "shape", "finish"}
OPTION_GROUPS = {
    "roundedCorners": "2",
    "coating": "4",
    "spotcoating": "5",
    "raisedprint": "50",
    "windPosition": "12",
    "raisedFoilColor": "51",
}
SUPPORTED_TYPES = {"1", "27", "36"}
CONTROL_ALIASES = {
    "raisedfoilcolor": "raisedFoilColor",
    "spotCoating": "spotcoating",
    "raisedPrint": "raisedprint",
    "paperfinish": "paperFinish",
}
AUDITED_ORDER_FORM_SHA256 = (
    "fd26a9c72ba1a162d5d12aa0f0f845dfed7d1a136f276440f1b5b06a465a1d8b"
)
AUDITED_COMMON_SHA256 = (
    "577bddf60b70e3c2c8362de3f358cd9477dc21232aa1d3c41839d5e1db1ed88d"
)
CONTRACT_PRICED_FIELDS = {
    "1": ("roundedCorners", "spotcoating", "raisedprint", "raisedFoilColor"),
    "27": ("roundedCorners", "spotcoating", "raisedprint", "raisedFoilColor"),
    "36": (),
}


def load_price_contract(path):
    """Recognize an independently reviewed saved bundle; never execute it."""
    path = Path(path).resolve()
    body = path.read_bytes()
    fingerprint = hashlib.sha256(body).hexdigest()
    if fingerprint != AUDITED_ORDER_FORM_SHA256:
        raise ValueError(
            "Unreviewed source bundle; pricing contract must be reviewed before reuse"
        )
    common_path = path.with_name("chunk-common.js")
    common_fingerprint = hashlib.sha256(common_path.read_bytes()).hexdigest()
    if common_fingerprint != AUDITED_COMMON_SHA256:
        raise ValueError(
            "Unreviewed shared bundle; source helper exclusions are not proven"
        )
    return {
        "version": 1,
        "path": str(path),
        "sha256": fingerprint,
        "shared_bundle": {"path": str(common_path), "sha256": common_fingerprint},
        "formula": "productPrice + (manual mode ? proof[manualProofType] : 0) + sum(selected applied option prices) + (design mode ? designServices[selectedID] : 0); toFixed(2)",
        "no_additive_price_fields": [
            "coating",
            "windPosition",
            "paperFinish",
            "fileDelivery",
        ],
        "proof_modes": {
            "31": "no_additive_proof_component",
            "100": "manualProofType_required",
            "101": "designServices_required",
        },
        "manual_proof_types": {
            "1": "PDF Proof",
            "36": "No Proof",
            "38": "PDF Proof 24Hrs",
            "37": "High-Res Hard Copy Proof",
        },
        "required_additional_state": [
            "hidden priced option values",
            "manualProofType when proof100",
            "designServices when proof101",
            "coupon applicability",
            "admin/waive/override state",
            "priceGroupId",
        ],
        "character_offsets": {
            "appliedPrices_initialization": 45417,
            "optionsHavingPrices": 56423,
            "broker_business_cards_fields": 83784,
            "manual_proof_schema": 134298,
            "option_price_cache_assignment": 239980,
            "manual_proof_radio": 1498365,
            "coupon_function": 1593100,
            "totalPrice": 1595750,
        },
    }


@dataclass(frozen=True)
class Evidence:
    id: str
    url: str
    created_at: float
    status: int
    data: Any


def quote_amount(value):
    text = str(value or "").strip()
    if re.fullmatch(r"\$?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", text) is None:
        raise ValueError("Subtotal is not an unambiguous nonnegative USD amount")
    return Decimal(text.removeprefix("$").replace(",", ""))


def _query(url):
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in SOURCE_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
        or len({key for key, _ in pairs}) != len(pairs)
    ):
        return parsed.path, None
    return parsed.path, dict(pairs)


def audit_quote(
    quote: Evidence,
    captures: Iterable[Evidence],
    *,
    linked_evidence_ids=None,
    contract=None,
) -> dict:
    """Reconcile one quote; incomplete component contracts stay unresolved.

    Routes explicitly bind product/option IDs. Historical request-start context
    not retained by the source recorder is reported, not silently reconstructed.
    linked_evidence_ids may identify the source captures saved with this quote's
    exact persisted row. Passing None permits diagnostic temporal association,
    which cannot produce a full pass or mismatch.
    """
    data = quote.data
    problems = []
    components = []
    used = {quote.id}
    linked = set(linked_evidence_ids) if linked_evidence_ids is not None else None
    controls = {}
    for control in data.get("controls", []):
        key = CONTROL_ALIASES.get(control.get("key"), control.get("key"))
        if key in controls:
            problems.append({"code": "ambiguous_control", "control": key})
        controls[key] = control
    selections = {key: control.get("value") for key, control in controls.items()}
    pricing_state = data.get("pricing_state") or {}
    runtime = (
        pricing_state.get("values", {}) if pricing_state.get("observed") is True else {}
    )
    if not isinstance(runtime, dict):
        runtime = {}
    runtime = {CONTROL_ALIASES.get(key, key): value for key, value in runtime.items()}
    services = canonical_service_context(data)
    scoped = service_context_is_bound(services)
    if not scoped:
        problems.append(
            {
                "code": "service_context_unknown_or_ambiguous",
                "service_context": services,
            }
        )
    if linked is None:
        problems.append(
            {
                "code": "row_linked_capture_provenance_missing",
                "detail": "Timestamp association is diagnostic only; it does not establish an immutable capture binding",
            }
        )
    result = {
        "quote_evidence_id": quote.id,
        "product_url": data.get("url", quote.url),
        "observed_at": data.get("observed_at", quote.created_at),
        "product_type": str(data.get("product_type")),
        "configuration": selections,
        "service_context": services,
        "raw_service_selections": data.get("service_context"),
        "known_service_scope": scoped,
        "currency": "USD",
        "status": "unresolved",
        "cart_verified": False,
        "components": components,
        "reasons": problems,
    }
    if contract is not None:
        if contract.get("sha256") != AUDITED_ORDER_FORM_SHA256:
            raise ValueError("An unreviewed contract cannot resolve component prices")
        result["source_contract"] = {
            key: contract[key] for key in ("version", "sha256", "path")
        }
        missing = []
        for key in CONTRACT_PRICED_FIELDS.get(str(data.get("product_type")), ()):
            if key not in controls and key not in runtime:
                missing.append(key)
            elif (
                key in runtime
                and key in controls
                and str(runtime[key]) != str(controls[key].get("value"))
            ):
                problems.append(
                    {"code": "controller_dom_selection_conflict", "control": key}
                )
            elif key in runtime and key not in controls:
                controls[key] = {
                    "key": key,
                    "value": str(runtime[key]),
                    "selected_label": "None" if runtime[key] == "" else None,
                }
        if missing:
            problems.append(
                {
                    "code": "hidden_priced_option_state_not_recorded",
                    "controls": missing,
                    "detail": "Source totalPrice reads these selections even when their controls are not visible",
                }
            )
        if (
            pricing_state.get("observed") is not True
            or pricing_state.get("coupon_status") != "none"
            or pricing_state.get("is_admin") is not False
            or pricing_state.get("price_group_id") != ""
        ):
            problems.append(
                {
                    "code": "discount_override_context_not_recorded_or_unsupported",
                    "detail": "No coupon, admin waiver/override or special price-group defaults are inferred; free sample and generic branches are excluded by source product-type guards",
                }
            )
    try:
        total = quote_amount(data.get("subtotal_text"))
        quantity = int(str(selections.get("quantity")))
        if quantity <= 0 or str(quantity) != str(selections.get("quantity")):
            raise ValueError("Quantity is not an exact positive integer")
    except (ValueError, TypeError) as exc:
        problems.append({"code": "invalid_dom_amount_or_quantity", "detail": str(exc)})
        return {**result, "evidence_ids": sorted(used)}
    result.update(quantity=quantity, observed_subtotal=format(total, "f"))
    if (
        str(data.get("product_type")) not in SUPPORTED_TYPES
        or data.get("store_type") != "broker"
    ):
        problems.append({"code": "unsupported_product_or_store_context"})

    eligible = []
    for evidence in captures:
        if (
            evidence.created_at > quote.created_at
            or linked is not None
            and evidence.id not in linked
        ):
            continue
        path, query = _query(evidence.url)
        if query is not None:
            eligible.append((evidence, path, query))

    def choose(path, query_ok, *, required=True):
        candidates = [
            (evidence, query)
            for evidence, route, query in eligible
            if route == path and query_ok(query)
        ]
        if not candidates:
            if required:
                problems.append({"code": "capture_missing", "route": path})
            return None
        latest_time = max(evidence.created_at for evidence, _ in candidates)
        latest = [
            evidence for evidence, _ in candidates if evidence.created_at == latest_time
        ]
        if any(not 200 <= item.status < 300 for item in latest):
            problems.append(
                {
                    "code": "capture_unsuccessful",
                    "evidence_ids": [item.id for item in latest],
                }
            )
            used.update(item.id for item in latest)
            return None
        if len({json.dumps(item.data, sort_keys=True) for item in latest}) != 1:
            problems.append(
                {
                    "code": "conflicting_captures",
                    "evidence_ids": [item.id for item in latest],
                }
            )
            used.update(item.id for item in latest)
            return None
        selected = sorted(latest, key=lambda item: item.id)[-1]
        used.add(selected.id)
        return selected

    base_selection = {
        key: str(value) for key, value in selections.items() if key in BASE_KEYS
    }
    lookup_query = {
        **base_selection,
        "productType": str(data.get("product_type")),
        "turnaround": str(selections.get("productionTime")),
    }
    if "orientation" not in base_selection:
        problems.append(
            {
                "code": "query_orientation_not_in_dom",
                "detail": "Absent square orientation needs its retained specification binding; a unique lookup alone is only a diagnostic association",
            }
        )

    def lookup_matches(query):
        expected = set(lookup_query)
        allowed = expected | (
            {"orientation"} if "orientation" not in expected else set()
        )
        return expected <= set(query) <= allowed and all(
            query.get(key) == value for key, value in lookup_query.items()
        )

    lookup = choose("/service/rest/v1/products", lookup_matches)
    product_id = None
    if lookup:
        items = lookup.data.get("items") if isinstance(lookup.data, dict) else None
        if (
            isinstance(items, list)
            and len(items) == 1
            and isinstance(items[0], dict)
            and set(items[0]) == {"id"}
            and str(items[0]["id"]).isdigit()
        ):
            product_id = str(items[0]["id"])
        else:
            problems.append(
                {"code": "ambiguous_product_lookup", "evidence_id": lookup.id}
            )
    result["source_base_product_id"] = product_id

    def price_component(key, value, route, *, option_id=None, catalog_evidence=None):
        def price_query(query):
            allowed = {"itemId", "cid", "quantities"}
            if (
                set(query) - allowed
                or query.get("itemId", "null") not in {"", "null"}
                or query.get("cid", "") != ""
            ):
                return False
            if "quantities" in query:
                values = query["quantities"].split(",")
                return (
                    len(values) == len(set(values))
                    and all(v.isdigit() for v in values)
                    and str(quantity) in values
                )
            return True

        capture = choose(route, price_query, required=False)
        item = {
            "control": key,
            "selected_value": value,
            "option_id": option_id,
            "status": "unresolved",
        }
        if catalog_evidence:
            item["catalog_evidence_id"] = catalog_evidence
        if capture is None:
            problems.append(
                {
                    "code": "exact_component_price_missing",
                    "control": key,
                    "selected_value": value,
                    "option_id": option_id,
                    "route": route,
                    "detail": "Missing price data and quantityDependentPrice=false do not establish zero or baseline inclusion",
                }
            )
        else:
            item["evidence_id"] = capture.id
            try:
                entry = parse_price_table(capture.data).get(quantity)
                if entry is None or entry.markup_price is None:
                    raise TableSchemaError(
                        "Requested quantity has no explicit markupPrice"
                    )
                if (
                    contract is not None
                    and entry.markup_price
                    != entry.markup_price.quantize(Decimal(".01"))
                ):
                    raise TableSchemaError(
                        "Fractional-cent component requires exact source JS rounding evaluation"
                    )
                item.update(status="priced", amount=format(entry.markup_price, "f"))
            except TableSchemaError as exc:
                problems.append(
                    {
                        "code": "component_price_schema_or_quantity",
                        "control": key,
                        "evidence_id": capture.id,
                        "detail": str(exc),
                    }
                )
        components.append(item)

    catalog, catalog_capture = [], None
    if product_id:
        prefix = "/service/rest/v1/products/" + product_id
        price_component("base", product_id, prefix + "/prices")
        catalog_capture = choose(
            prefix + "/options",
            lambda query: set(query) <= {"requestValues", "eddm"}
            and query.get("requestValues") == "true"
            and query.get("eddm", "false") == "false",
            required=False,
        )
        if catalog_capture:
            try:
                catalog = list(parse_option_catalog(catalog_capture.data).values())
            except TableSchemaError as exc:
                problems.append(
                    {
                        "code": "option_catalog_schema",
                        "evidence_id": catalog_capture.id,
                        "detail": str(exc),
                    }
                )

    def selected_option(key, value, group):
        matches = [
            item
            for item in catalog
            if str(item["type"]["id"]) == str(value)
            and str(item["group"]["id"]) == group
        ]
        if len(matches) != 1:
            components.append(
                {"control": key, "selected_value": value, "status": "unresolved"}
            )
            problems.append(
                {
                    "code": "selected_option_mapping_unresolved",
                    "control": key,
                    "selected_value": value,
                    "required_catalog_group": group,
                    "candidate_option_ids": [
                        str(item["id"])
                        for item in catalog
                        if str(item["group"]["id"]) == group
                    ],
                    "catalog_evidence_id": (
                        catalog_capture.id if catalog_capture else None
                    ),
                }
            )
            return
        option = matches[0]
        price_component(
            key,
            value,
            "/service/rest/v1/products/"
            + product_id
            + "/options/"
            + str(option["id"])
            + "/prices",
            option_id=str(option["id"]),
            catalog_evidence=catalog_capture.id,
        )

    for key, control in controls.items():
        if key in BASE_KEYS | {"quantity", "productionTime"}:
            continue
        value = control.get("value")
        if contract is not None and key in {"coating", "windPosition", "paperFinish"}:
            components.append(
                {
                    "control": key,
                    "selected_value": value,
                    "status": "contract_no_additive_component",
                    "amount": "0.00",
                    "contract_sha256": contract["sha256"],
                }
            )
        elif value == "" and control.get("selected_label") == "None":
            components.append(
                {
                    "control": key,
                    "selected_value": "",
                    "status": "explicit_none",
                    "evidence_id": quote.id,
                }
            )
        elif value == "":
            components.append(
                {"control": key, "selected_value": "", "status": "unresolved"}
            )
            problems.append(
                {
                    "code": "included_option_pricing_unproven",
                    "control": key,
                    "label": control.get("selected_label"),
                    "evidence_id": quote.id,
                }
            )
        elif key in OPTION_GROUPS:
            selected_option(key, value, OPTION_GROUPS[key])
        else:
            problems.append(
                {
                    "code": "unknown_selected_control",
                    "control": key,
                    "selected_value": value,
                }
            )

    # Only exact type-ID+group matches may resolve proof. DOM100 has no such
    # match; its $3 residual must never be used to choose soft proof over no proof.
    for key, group in services["groups"].items():
        if contract is not None and key == "fileDeliveryForIop":
            components.append(
                {
                    "control": key,
                    "selected_value": (
                        group["values"][0] if group["status"] == "selected" else None
                    ),
                    "status": "contract_no_additive_component",
                    "amount": "0.00",
                    "selection_status": group["status"],
                    "contract_sha256": contract["sha256"],
                }
            )
            continue
        if contract is not None and key == "proofType":
            proof = (
                group["values"][0]
                if group["status"] == "selected"
                else str(runtime.get("proofType", ""))
            )
            if (
                group["status"] == "selected"
                and "proofType" in runtime
                and str(runtime["proofType"]) != proof
            ):
                problems.append(
                    {
                        "code": "controller_dom_selection_conflict",
                        "control": "proofType",
                    }
                )
            if not proof:
                # Positive checked-value evidence remains readable in older
                # diagnostic bodies; it does not make omitted groups observed.
                observed_groups = [
                    item
                    for item in data.get("service_context", [])
                    if item.get("key") == key
                ]
                if len(observed_groups) == 1:
                    selected = observed_groups[0].get("selected", [])
                    if (
                        observed_groups[0].get("selection_status") == "selected"
                        and observed_groups[0].get("checked_count") == 1
                        and len(selected) == 1
                        and selected[0].get("checked") is True
                    ):
                        proof = str(selected[0].get("value", ""))
            if proof == "31":
                components.append(
                    {
                        "control": key,
                        "selected_value": proof,
                        "status": "contract_no_additive_component",
                        "amount": "0.00",
                        "contract_sha256": contract["sha256"],
                    }
                )
            elif proof == "100" and "manualProofType" in runtime:
                selected_option("manualProofType", str(runtime["manualProofType"]), "1")
            elif proof == "101" and "designServices" in runtime:
                selected_option("designServices", str(runtime["designServices"]), "13")
            else:
                problems.append(
                    {
                        "code": "proof_subselection_not_recorded",
                        "observed_mode": proof or None,
                        "required": (
                            "manualProofType"
                            if proof == "100"
                            else "designServices" if proof == "101" else "proofType"
                        ),
                        "detail": "The source default is not evidence of the selected runtime value",
                    }
                )
                components.append(
                    {
                        "control": key,
                        "selected_value": proof or None,
                        "status": "unresolved",
                    }
                )
            continue
        if group["status"] == "absent":
            components.append(
                {"control": key, "status": "observed_absent", "evidence_id": quote.id}
            )
        elif group["status"] == "selected":
            value = group["values"][0]
            if key == "proofType":
                selected_option(key, value, "1")
            else:
                components.append(
                    {"control": key, "selected_value": value, "status": "unresolved"}
                )
                problems.append(
                    {
                        "code": "file_delivery_component_contract_missing",
                        "control": key,
                        "selected_value": value,
                        "detail": "File-delivery IDs are not option type IDs; absent delivery in another proof mode cannot establish a zero fee here",
                    }
                )
        else:
            components.append({"control": key, "status": "unresolved"})

    priced = [
        Decimal(item["amount"]) for item in components if item["status"] == "priced"
    ]
    if priced:
        partial = sum(priced, Decimal(0))
        result["known_component_subtotal"] = format(partial, "f")
        result["unexplained_residual"] = format(total - partial, "f")
        result["partial_sum_matches"] = total == partial
    if (
        product_id
        and not problems
        and all(
            item["status"]
            in {
                "priced",
                "explicit_none",
                "observed_absent",
                "contract_no_additive_component",
            }
            for item in components
        )
    ):
        result["status"] = "pass" if result.get("partial_sum_matches") else "mismatch"
        if result["status"] == "mismatch":
            problems.append(
                {
                    "code": "exact_component_sum_mismatch",
                    "detail": "All component mappings and exact quantity prices are present, but their sum differs",
                }
            )
    result["evidence_ids"] = sorted(used)
    return result


class SavedEvidence:
    """Frozen SQLite metadata plus immutable objects, with no writes to either."""

    def __init__(self, database, objects_root):
        self.database = Path(database).resolve()
        self.objects_root = Path(objects_root).resolve()
        self.connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.records = {
            row["id"]: dict(row)
            for row in self.connection.execute("SELECT * FROM responses")
        }

    @lru_cache(maxsize=2048)
    def object(self, relative):
        path = (self.objects_root / relative).resolve()
        if not path.is_relative_to(self.objects_root):
            raise ValueError(
                "Evidence object path is outside the immutable object root"
            )
        return json.loads(path.read_text())

    def evidence(self, identifier):
        record = self.records[identifier]
        return Evidence(
            identifier,
            record["url"],
            record["created_at"],
            record["status"],
            self.object(record["object_path"]),
        )

    def close(self):
        self.connection.close()


def audit_saved(database, objects_root, *, run_id=None, contract=None):
    saved = SavedEvidence(database, objects_root)
    try:
        # Row-linked evidence supplies stronger provenance than nearest-time
        # joins. Exact DOM control/quantity identity is still checked below.
        dom_ids = set()
        for identifier, record in saved.records.items():
            if (
                record["content_type"] != "application/json"
                or "/products/" not in record["url"]
                or "/order" not in record["url"]
            ):
                continue
            if saved.evidence(identifier).data.get("kind") == "visible_product_quote":
                dom_ids.add(identifier)
        linked = defaultdict(set)
        associated_rows = defaultdict(set)
        sql = "SELECT b.object_path FROM batches b JOIN products p ON p.id=b.product_id"
        params = ()
        if run_id:
            sql += " WHERE p.run_id=?"
            params = (run_id,)
        for batch in saved.connection.execute(sql, params):
            path = (saved.objects_root / batch["object_path"]).resolve()
            if not path.is_relative_to(saved.objects_root):
                raise ValueError("Batch path is outside the object root")
            with gzip.open(path, "rt") as stream:
                for line in stream:
                    row = json.loads(line)
                    ids = row.get("evidence_ids", [])
                    expected = {
                        key: value.get("value") if isinstance(value, dict) else value
                        for key, value in row.get("configuration", {}).items()
                    }
                    for identifier in dom_ids.intersection(ids):
                        quote = saved.evidence(identifier)
                        actual = {
                            control["key"]: control["value"]
                            for control in quote.data.get("controls", [])
                            if control["key"] not in {"quantity", "productionTime"}
                        }
                        q = next(
                            (
                                control["value"]
                                for control in quote.data.get("controls", [])
                                if control["key"] == "quantity"
                            ),
                            None,
                        )
                        if (
                            expected == actual
                            and str(row.get("quantity")) == str(q)
                            and canonical_service_context(row)
                            == canonical_service_context(quote.data)
                        ):
                            linked[identifier].update(ids)
                            associated_rows[identifier].add(row["id"])
        source_records = []
        for identifier, record in saved.records.items():
            path, query = _query(record["url"])
            if query is not None and path.startswith("/service/rest/v1/products"):
                source_records.append((identifier, record["created_at"], path))
        results = []
        for identifier in sorted(
            dom_ids, key=lambda key: (saved.records[key]["created_at"], key)
        ):
            quote = saved.evidence(identifier)
            if str(quote.data.get("product_type")) not in SUPPORTED_TYPES:
                continue
            ids = linked.get(identifier)
            # Per-quote persisted evidence is bounded. For detached observations,
            # only nearby product routes are used for explicitly unresolved
            # diagnostics; this is not a source-context proof.
            selected = (
                [key for key, timestamp, _ in source_records if key in ids]
                if ids
                else [
                    key
                    for key, timestamp, _ in source_records
                    if quote.created_at - 3600 <= timestamp <= quote.created_at
                ]
            )
            result = audit_quote(
                quote,
                [saved.evidence(key) for key in selected],
                linked_evidence_ids=ids,
                contract=contract,
            )
            result["persisted_row_ids"] = sorted(associated_rows.get(identifier, []))
            results.append(result)
        counts = Counter(result["status"] for result in results)
        scoped = [result for result in results if result["known_service_scope"]]
        scoped_counts = Counter(result["status"] for result in scoped)
        reasons = Counter(
            reason["code"] for result in results for reason in result["reasons"]
        )
        complete = counts["pass"] + counts["mismatch"]
        return {
            "audit_version": 2,
            "phase": "post_public_contract" if contract else "snapshot_only",
            "source_contract": contract,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": str(Path(database).resolve()),
            "run_id": run_id,
            "scope": "Every saved supported product DOM quote in the frozen metadata; row linkage restricted to the selected run. Detached quotes remain explicitly unresolved.",
            "network_requests": 0,
            "model_requests": 0,
            "summary": {
                "quotes_examined": len(results),
                "known_service_scope_quotes": len(scoped),
                "statuses": dict(counts),
                "known_service_scope_statuses": dict(scoped_counts),
                "complete_reconciliation_denominator": complete,
                "pass_rate": (
                    format(Decimal(counts["pass"]) / complete, ".8f")
                    if complete
                    else None
                ),
                "resolution_coverage": (
                    format(Decimal(complete) / len(results), ".8f") if results else None
                ),
                "partial_sum_matches_not_passes": sum(
                    result.get("partial_sum_matches") is True for result in results
                ),
                "partial_sum_available": sum(
                    "partial_sum_matches" in result for result in results
                ),
                "reason_counts": dict(reasons),
            },
            "quotes": results,
        }
    finally:
        saved.close()


def render_markdown(report):
    summary = report["summary"]
    statuses = summary["statuses"]
    rate = (
        "undefined (zero fully resolvable quotes)"
        if summary["pass_rate"] is None
        else str(Decimal(summary["pass_rate"]) * 100) + "%"
    )
    lines = [
        "# Offline evidence math audit",
        "",
        f"Frozen database: `{report['database']}`",
        "",
        f"Examined **{summary['quotes_examined']:,} DOM quotes**, including **{summary['known_service_scope_quotes']:,}** with explicitly known service context.",
        "",
        f"Results: **{statuses.get('pass', 0):,} pass**, **{statuses.get('mismatch', 0):,} mismatch**, **{statuses.get('unresolved', 0):,} unresolved**.",
        "",
        f"Pass rate is **{rate}**. The denominator is pass + mismatch ({summary['complete_reconciliation_denominator']:,}); unresolved quotes are excluded from that denominator and disclosed separately. Resolution coverage across every examined quote is {summary['resolution_coverage']}.",
        "",
        f"A partial known-component sum equals the displayed total in {summary['partial_sum_matches_not_passes']:,} of {summary['partial_sum_available']:,} quotes with an available partial sum. **These are not passes:** an unknown component may not be assumed free or included.",
        "",
        "## Unresolved evidence requirements",
        "",
        "| Reason | Occurrences |",
        "| --- | ---: |",
    ]
    if report.get("source_contract"):
        contract = report["source_contract"]
        lines[4:4] = [
            f"Phase: **post-contract audit** using saved orderForm.js SHA-256 `{contract['sha256']}` and the reviewed shared bundle. Public bundles were acquired in a separate bounded phase; this audit makes zero requests and no supplier quote calls.",
            "",
        ]
    lines.extend(
        f"| `{reason}` | {count:,} |"
        for reason, count in sorted(
            summary["reason_counts"].items(), key=lambda item: (-item[1], item[0])
        )
    )
    lines.extend(
        [
            "",
            "The JSON companion includes every quote, mismatch/unresolved reason, component status, residual, source evidence ID and linked saved-row ID. Legacy unknown service selections are not inferred from later quotes.",
            "",
            "The proof31/file-delivery5 selection and proof100/absent delivery are distinct. DOM proof100 is not assumed to equal catalog type1; manual soft proof and manual no-proof choices cannot be selected from a $3 residual. A missing price endpoint or quantityDependentPrice=false does not prove zero cost.",
            "",
            "No supplier/model requests, live mutations, fee guesses, cart verification or source-table verification upgrades were performed.",
            "",
        ]
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--objects-root", required=True)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--contract",
        help="Reviewed saved orderForm.js; enables a separate post-contract audit",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="JSON report path; Markdown is written beside it",
    )
    args = parser.parse_args()
    report = audit_saved(
        args.database,
        args.objects_root,
        run_id=args.run_id,
        contract=load_price_contract(args.contract) if args.contract else None,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    output.with_suffix(".md").write_text(render_markdown(report))
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
