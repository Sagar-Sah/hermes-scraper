"""Read-only Printograph table adapter.

The contracts below are visible in Printograph's public orderForm/chunk-common
bundles. Live API access currently requires a user-supplied vendor authorization
profile. No token acquisition, cart writes, or inferred prices are performed.
"""

from __future__ import annotations

import itertools
import json
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from math import prod
from urllib.parse import urljoin, urlparse


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


class ProductHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.attributes = {}
        self.scripts = []
        self.images = []
        self.title = ""
        self._title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id") == "order-form":
            self.attributes = a
        if tag == "script" and a.get("src"):
            self.scripts.append(a["src"])
        if tag == "meta" and a.get("property") == "og:image" and a.get("content"):
            self.images.append(a["content"])
        if tag == "title":
            self._title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False

    def handle_data(self, data):
        if self._title:
            self.title += data


SPEC_NAMES = {
    "shapes": "shape",
    "sizes": "size",
    "materials": "material",
    "pages": "page",
    "pockets": "pocket",
    "papers": "paper",
    "finishes": "finish",
    "foldings": "folding",
    "preScoredFolding": "folding",
    "colors": "color",
    "coverPapers": "coverPaper",
    "panels": "panel",
    "trays": "tray",
    "turnarounds": "turnaround",
    "productionTime": "turnaround",
}


def normalize_specs(payload: dict) -> dict[str, list[dict]]:
    """Classic products expose plural keys containing {id,label,...} arrays.

    Generic-product encoded size strings deliberately fail instead of being
    coerced into a plausible but incorrect classic-product domain.
    """
    if not isinstance(payload, dict) or "messageList" in payload:
        raise AdapterError("schema_changed", "Expected a specification object")
    result = {}
    for name, values in payload.items():
        key = SPEC_NAMES.get(name, name)
        if isinstance(values, dict) and "list" in values:
            values = values["list"]
        if not isinstance(values, list) or not values:
            continue
        if not all(isinstance(v, dict) and "id" in v for v in values):
            if key in SPEC_NAMES.values():
                raise AdapterError("schema_changed", f"Unsupported {name} values")
            continue
        dedup = {}
        for value in values:
            if value.get("disabled") is True or value.get("isHidden") is True:
                continue
            item = dict(value)
            item["label"] = str(value.get("label", value["id"]))
            dedup[str(value["id"])] = item
        result[key] = list(dedup.values())
    if not result.get("size"):
        raise AdapterError("schema_changed", "Specifications contain no usable sizes")
    return result


def parse_frontend_contract(script: str, product_type: int) -> dict:
    pattern = rf"(?<![\w]){product_type}:\{{specs:(\[[^\]]*\]),options:(\[[^\]]*\])"
    choices = {(a, b) for a, b in re.findall(pattern, script)}
    if len(choices) != 1:
        raise AdapterError(
            "schema_changed", "Product field contract is absent or ambiguous"
        )
    specs, options = next(iter(choices))
    priced = re.search(r"optionsHavingPrices:(\[[\d,\s]*\])", script)
    if not priced:
        raise AdapterError("schema_changed", "Priced-option contract is absent")
    option_fields = {}
    pattern = r"((?:case\s+\d+:)+)\w+\.(\w+)\.list\.push\([^)]*\)"
    for cases, field in re.findall(pattern, script):
        for identifier in re.findall(r"case\s+(\d+):", cases):
            option_fields[int(identifier)] = field
    return {
        "specs": json.loads(specs),
        "options": json.loads(options),
        "priced_options": set(json.loads(priced.group(1))),
        "option_fields": option_fields,
    }


def items(payload, kind: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise AdapterError("schema_changed", f"Expected {kind} response.items")
    if not all(isinstance(x, dict) for x in payload["items"]):
        raise AdapterError("schema_changed", f"Malformed {kind} records")
    # Do not silently treat the first page as the complete table.
    for key in ("total", "totalCount", "count"):
        count = payload.get(key)
        if isinstance(count, int) and count > len(payload["items"]):
            raise AdapterError("incomplete_response", f"{kind} response is paginated")
    if payload.get("hasMore") or payload.get("next") or payload.get("nextPage"):
        raise AdapterError("incomplete_response", f"{kind} response has another page")
    return payload["items"]


def money(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise AdapterError("schema_changed", "Missing or invalid monetary value")
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise AdapterError("schema_changed", "Unrecognized monetary value") from exc
    if not amount.is_finite() or amount < 0:
        raise AdapterError("schema_changed", "Non-finite or negative monetary value")
    return amount


def quantity(value) -> int:
    if isinstance(value, bool):
        raise AdapterError("schema_changed", "Invalid quantity")
    try:
        number = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise AdapterError("schema_changed", "Invalid quantity") from exc
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise AdapterError("schema_changed", "Quantity must be a positive integer")
    return int(number)


def price_table(payload) -> dict[int, Decimal]:
    result = {}
    for row in items(payload, "prices"):
        q, amount = quantity(row.get("quantity")), money(row.get("markupPrice"))
        if q in result and result[q] != amount:
            raise AdapterError("schema_changed", "Conflicting prices for one quantity")
        result[q] = amount
    return result


def matches(value: dict, requested) -> bool:
    requested = requested if isinstance(requested, list) else [requested]
    actual = {
        str(value.get("id", "")).casefold(),
        str(value.get("label", "")).casefold(),
    }
    return any(str(x).casefold() in actual for x in requested)


class PrintographAdapter:
    name = "printograph"
    capabilities = {
        "base_quantity_tables": True,
        "option_quantity_tables": True,
        "requires_authorized_api_profile": True,
        "arbitrary_bulk_ids_verified": False,
        "exact_shipping": False,
        "cart_writes": False,
        "complete_option_interaction_rules": False,
    }

    async def extract(self, spec: dict, transport, emit):
        url = spec["url"]
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "www.printograph.com",
            "printograph.com",
        }:
            raise AdapterError(
                "unsupported_product", "Printograph requires its HTTPS product URL"
            )
        origin = f"{parsed.scheme}://{parsed.netloc}"
        maximum = spec.get("max_requests")
        if maximum is not None and (not isinstance(maximum, int) or maximum < 1):
            raise AdapterError(
                "invalid_selection", "max_requests must be a positive integer"
            )
        requests = 0
        fetched = OrderedDict()

        async def fetch(path, params=None, lane="pricing"):
            nonlocal requests
            full = urljoin(origin, path)
            key = (full, json.dumps(params, sort_keys=True))
            if key in fetched:
                fetched.move_to_end(key)
                return fetched[key]
            if maximum is not None and requests >= maximum:
                raise AdapterError(
                    "request_limit",
                    "Explicit request budget reached; coverage is partial",
                )
            requests += 1
            response = await transport.fetch(
                full,
                params=params,
                context={
                    "adapter": self.name,
                    "lane": lane,
                },
            )
            if response.status in (401, 403):
                raise AdapterError(
                    "access_required",
                    "Printograph requires a valid supplied authorization profile",
                    response_id=response.response_id,
                    status=response.status,
                )
            if response.status == 429:
                raise AdapterError(
                    "rate_limited",
                    "Source asked the crawler to slow down",
                    response_id=response.response_id,
                )
            invalid_lookup = (
                response.status == 404
                and urlparse(full).path == "/service/rest/v1/products"
                and isinstance(response.data, dict)
                and any(
                    isinstance(m, dict)
                    and m.get("resource") == "ProductResource"
                    and str(m.get("id")) in {"202", "260"}
                    for m in response.data.get("messageList", [])
                )
            )
            if response.status >= 400 and not invalid_lookup:
                raise AdapterError(
                    "source_error",
                    f"Source returned HTTP {response.status}",
                    response_id=response.response_id,
                    status=response.status,
                )
            fetched[key] = response
            if len(fetched) > 256:
                fetched.popitem(last=False)
            return response

        await emit("capabilities", self.capabilities)
        page = await fetch(url, lane="discovery")
        if not isinstance(page.data, str):
            raise AdapterError("schema_changed", "Expected product HTML")
        document = ProductHTML()
        document.feed(page.data)
        raw_type = document.attributes.get(
            "data-printproducttype"
        ) or document.attributes.get("printproducttype")
        metadata = spec.get("source_metadata") or {}
        if not raw_type and metadata.get("product_type") is not None:
            raw_type = metadata["product_type"]
            await emit(
                "discovery_note",
                {
                    "message": "Using explicitly supplied source bootstrap metadata; API authorization is still required.",
                    "source_product_type": raw_type,
                },
            )
        if not raw_type:
            if re.search(
                r"authenticating|access denied|verify you are human",
                document.title,
                re.I,
            ):
                raise AdapterError(
                    "access_required",
                    "The source returned an authentication page, not the product",
                    response_id=page.response_id,
                )
            raise AdapterError(
                "schema_changed", "Product HTML lacks its source product-type metadata"
            )
        try:
            product_type = int(raw_type)
        except (TypeError, ValueError) as exc:
            raise AdapterError(
                "schema_changed", "Invalid product-type metadata"
            ) from exc
        if product_type >= 10000:
            raise AdapterError(
                "unsupported_product",
                "Generic-product calculator requires a separate verified contract",
            )
        title = (
            document.attributes.get("data-producttitle")
            or metadata.get("title")
            or document.title.strip()
        )
        image_urls = list(dict.fromkeys(urljoin(url, x) for x in document.images))
        script_urls = [
            urljoin(url, x)
            for x in document.scripts
            if re.search(r"/orderForm\.js(?:\?|$)", x)
        ]
        if not script_urls and metadata.get("order_form_script_url"):
            script_urls = [metadata["order_form_script_url"]]
        if (
            len(script_urls) != 1
            or urlparse(script_urls[0]).hostname != "static.printograph.com"
            or urlparse(script_urls[0]).scheme != "https"
        ):
            raise AdapterError(
                "schema_changed", "Expected one public Printograph orderForm bundle"
            )
        bundle = await fetch(script_urls[0], lane="discovery")
        if not isinstance(bundle.data, str):
            raise AdapterError(
                "schema_changed", "Expected the order-form JavaScript bundle"
            )
        contract = parse_frontend_contract(bundle.data, product_type)
        specifications = await fetch(
            "/service/rest/v1/settings/product/specifications",
            {
                "requestValues": "true",
                "productType": product_type,
            },
            lane="discovery",
        )
        domains = normalize_specs(specifications.data)
        filters = {
            SPEC_NAMES.get(k, k): v
            for k, v in spec.get("selection_filters", {}).items()
        }
        known_filters = (
            set(contract["specs"])
            | set(contract["options"])
            | {"quantity", "turnaround"}
        )
        unknown_filters = set(filters) - known_filters
        if unknown_filters:
            raise AdapterError(
                "invalid_selection",
                "Unknown selection filters",
                fields=sorted(unknown_filters),
            )
        field_names = [x for x in contract["specs"] if x != "orientation"]
        # The supplier encodes horizontal/vertical sizes as distinct size IDs.
        # Explicit orientation is only accepted when the API lists that domain.
        if "orientation" in filters:
            if not domains.get("orientation"):
                raise AdapterError(
                    "unsupported_filter",
                    "Filter by the source size label/ID for orientation",
                )
            field_names.append("orientation")
        for required in ("paper", "color"):
            if required not in field_names and domains.get(required):
                field_names.append(required)
        if not domains.get("turnaround"):
            raise AdapterError(
                "schema_changed", "Source did not enumerate production-time choices"
            )
        field_names.append("turnaround")
        selected_domains = []
        for name in field_names:
            values = domains.get(name)
            if not values:
                raise AdapterError(
                    "schema_changed", f"Source did not enumerate required {name}"
                )
            if name in filters:
                values = [v for v in values if matches(v, filters[name])]
            if not values:
                raise AdapterError(
                    "invalid_selection",
                    f"No source {name} values match the requested selection",
                )
            selected_domains.append(values)
        await emit(
            "discovery",
            {
                "title": title,
                "source_product_type": product_type,
                "candidate_base_combinations": prod(map(len, selected_domains)),
                "count_is_exact": False,
                "domains": {k: len(v) for k, v in zip(field_names, selected_domains)},
                "evidence_ids": [
                    page.response_id,
                    bundle.response_id,
                    specifications.response_id,
                ],
            },
        )
        rows = resolved = invalid = 0
        partial_reasons = (
            {"option_interactions_and_default_choices_not_exhaustively_verified"}
            if contract["options"]
            else set()
        )
        for combination in itertools.product(*selected_domains):
            base = dict(zip(field_names, combination))
            params = {
                "productType": product_type,
                **{k: v["id"] for k, v in base.items()},
            }
            lookup = await fetch("/service/rest/v1/products", params)
            if (
                lookup.status == 404
            ):  # Only the two validated invalid-combination codes pass fetch().
                invalid += 1
                continue
            products = items(lookup.data, "product lookup")
            if not products:
                invalid += 1
                continue
            if len(products) != 1 or not products[0].get("id"):
                raise AdapterError(
                    "incomplete_response",
                    "Fully specified lookup did not identify exactly one product",
                )
            identifier = products[0]["id"]
            quantity_params = {k: v for k, v in params.items() if k != "turnaround"}
            quantity_params["includeTurnaround"] = "true"
            quantities_response = await fetch(
                "/service/rest/v1/products/quantities", quantity_params
            )
            listed = items(quantities_response.data, "quantities")
            allowed_quantities = {quantity(x.get("quantity")) for x in listed}
            prices_response = await fetch(
                f"/service/rest/v1/products/{identifier}/prices"
            )
            base_prices = price_table(prices_response.data)
            options_response = await fetch(
                f"/service/rest/v1/products/{identifier}/options",
                {
                    "requestValues": "true",
                    "eddm": "false",
                },
            )
            source_options = items(options_response.data, "options")
            option_groups = {}
            option_prices = {}
            option_evidence = {}
            for option in source_options:
                value = option.get("type")
                if (
                    not isinstance(value, dict)
                    or "id" not in value
                    or "id" not in option
                ):
                    raise AdapterError("schema_changed", "Malformed option metadata")
                field = contract["option_fields"].get(int(value["id"]))
                if field not in contract["options"]:
                    continue  # Proof/upload services are not physical configuration dimensions.
                if field in filters and not matches(value, filters[field]):
                    continue
                option_groups.setdefault(field, []).append(option)
                if int(value["id"]) in contract["priced_options"]:
                    response = await fetch(
                        f"/service/rest/v1/products/{identifier}/options/{option['id']}/prices"
                    )
                    option_prices[str(option["id"])] = price_table(response.data)
                    option_evidence[str(option["id"])] = response.response_id
            if any(
                k in filters and not option_groups.get(k) for k in contract["options"]
            ):
                invalid += 1
                continue
            # Enumerate source-listed options only. An implicit "None" is never
            # invented; its absence makes coverage explicitly partial.
            option_names = sorted(option_groups)
            candidates = (
                itertools.product(*(option_groups[k] for k in option_names))
                if option_names
                else [()]
            )
            for selected_options in candidates:
                selected = dict(zip(option_names, selected_options))
                for q in sorted(allowed_quantities):
                    if "quantity" in filters and not matches(
                        {"id": q, "label": q}, filters["quantity"]
                    ):
                        continue
                    if q not in base_prices:
                        partial_reasons.add("listed_quantity_has_no_base_price")
                        continue
                    amount = base_prices[q]
                    evidence = [
                        page.response_id,
                        bundle.response_id,
                        specifications.response_id,
                        lookup.response_id,
                        quantities_response.response_id,
                        prices_response.response_id,
                        options_response.response_id,
                    ]
                    missing_price = False
                    for choice in selected_options:
                        oid = str(choice["id"])
                        if oid in option_prices:
                            if q not in option_prices[oid]:
                                missing_price = True
                                break
                            amount += option_prices[oid][q]
                            evidence.append(option_evidence[oid])
                    if missing_price:
                        partial_reasons.add("listed_quantity_has_no_option_price")
                        continue
                    configuration = {
                        k: {"value": v["id"], "label": v["label"]}
                        for k, v in base.items()
                        if k != "turnaround"
                    }
                    configuration.update(
                        {
                            k: {
                                "value": v["type"]["id"],
                                "label": str(v["type"].get("label", v["type"]["id"])),
                                "source_option_id": v["id"],
                            }
                            for k, v in selected.items()
                        }
                    )
                    rows += 1
                    yield {
                        "product_url": url,
                        "title": title,
                        "source_product_id": str(identifier),
                        "configuration": configuration,
                        "quantity": q,
                        "currency": "USD",
                        "subtotal": format(amount, "f"),
                        "unit_price": format(amount / q, ".8f"),
                        "provenance": "rule_derived" if selected else "source_table",
                        "verification_status": "pending",
                        "evidence_ids": list(dict.fromkeys(evidence)),
                        "production": {
                            "value": base["turnaround"]["id"],
                            "label": base["turnaround"]["label"],
                            "duration_status": "not_quoted",
                        },
                        "images": image_urls,
                        "coverage_status": (
                            "partial"
                            if partial_reasons
                            else "base_domain_exhaustion_pending"
                        ),
                        "pricing_scope": (
                            "listed_option_candidate" if selected else "base_product"
                        ),
                        "coverage_reasons": sorted(partial_reasons),
                    }
            resolved += 1
            await emit(
                "progress",
                {
                    "resolved_base_combinations": resolved,
                    "invalid_base_combinations": invalid,
                    "rows": rows,
                    "requests": requests,
                    "count_is_exact": False,
                },
            )
        await emit(
            "coverage",
            {
                "status": "partial" if partial_reasons else "source_tables_exhausted",
                "rows": rows,
                "resolved_base_combinations": resolved,
                "invalid_base_combinations": invalid,
                "reasons": sorted(partial_reasons),
                "requests": requests,
                "count_is_exact": not bool(partial_reasons),
                "verification_complete": False,
                "shipping_status": "not_requested",
            },
        )


Adapter = PrintographAdapter
