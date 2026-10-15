"""Embedded WooCommerce variation extraction without cart or order mutations."""

from __future__ import annotations

import itertools
import json
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, parse_qs
from decimal import Decimal
from math import prod
import re

from bs4 import BeautifulSoup

from .printograph import AdapterError, matches, money


class WooHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.variations = None
        self.product_id = None
        self.domains = {}
        self.title = ""
        self.currency = None
        self._in_title = False
        self._form_depth = 0
        self._select = None
        self._option = None
        self.ajax_url = None

    def feed(self, data):
        soup = BeautifulSoup(data, "html.parser")
        forms = soup.select("form.variations_form")
        if not forms:
            raise AdapterError(
                "unsupported_source",
                "No WooCommerce variation form found; use the browser adapter for another platform",
            )
        if len(forms) != 1:
            raise AdapterError(
                "ambiguous_product",
                "Multiple variation forms require a specific product URL",
            )
        for script in soup.select("script:not([src])"):
            text = script.string or script.get_text()
            match = re.search(
                r"(?:var\s+|window\.)?wc_add_to_cart_variation_params\s*=\s*", text
            )
            if match:
                try:
                    params, _ = json.JSONDecoder().raw_decode(
                        text[match.end() :].lstrip()
                    )
                    if isinstance(params, dict):
                        self.ajax_url = params.get("wc_ajax_url")
                except ValueError:
                    pass  # Unsupported script syntax is never executed or guessed.
        super().feed(data)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "meta" and (
            attrs.get("itemprop") == "priceCurrency"
            or attrs.get("property") == "product:price:currency"
        ):
            self.currency = attrs.get("content")
        if tag == "form" and "variations_form" in attrs.get("class", "").split():
            self._form_depth += 1
            self.product_id = attrs.get("data-product_id")
            encoded = attrs.get("data-product_variations")
            if encoded:
                try:
                    self.variations = json.loads(encoded)
                except json.JSONDecodeError as exc:
                    raise AdapterError(
                        "schema_changed", "Invalid embedded WooCommerce variation JSON"
                    ) from exc
        if (
            self._form_depth
            and tag == "select"
            and attrs.get("name", "").startswith("attribute_")
        ):
            self._select = attrs["name"]
            self.domains.setdefault(self._select, [])
        if (
            self._select
            and tag == "option"
            and attrs.get("value")
            and "disabled" not in attrs
        ):
            self._option = {"id": attrs["value"], "label": ""}

    def handle_data(self, text):
        if self._in_title:
            self.title += text
        if self._option is not None:
            self._option["label"] += text

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "option" and self._option is not None:
            self._option["label"] = self._option["label"].strip() or self._option["id"]
            self.domains[self._select].append(self._option)
            self._option = None
        if tag == "select":
            self._select = None
        if tag == "form":
            self._form_depth = max(0, self._form_depth - 1)


class WooCommerceAdapter:
    name = "woocommerce"
    capabilities = {
        "embedded_variations": True,
        "dynamic_variations": True,
        "exact_shipping": False,
        "quantity_price_breaks": False,
        "cart_writes": False,
    }

    async def extract(self, spec, transport, emit):
        response = await transport.fetch(
            spec["url"], context={"adapter": self.name, "lane": "discovery"}
        )
        if response.status in (401, 403):
            raise AdapterError(
                "access_required",
                "The source requires an authorized session",
                response_id=response.response_id,
            )
        if response.status >= 400 or not isinstance(response.data, str):
            raise AdapterError(
                "source_error",
                f"Expected product HTML; received HTTP {response.status}",
            )
        page = WooHTML()
        page.feed(response.data)
        await emit("capabilities", self.capabilities)
        await emit(
            "discovery",
            {
                "title": page.title.strip(),
                "source_product_id": page.product_id,
                "domains": page.domains,
            },
        )
        filters = dict(spec.get("selection_filters", {}))
        requested_quantity = filters.pop("quantity", 1)
        if not isinstance(requested_quantity, list):
            requested_quantity = [requested_quantity]
        if not requested_quantity or any(str(q) != "1" for q in requested_quantity):
            raise AdapterError(
                "quantity_pricing_unverified",
                "Embedded display_price establishes one-unit pricing only; quantity discounts need a verified quote source",
            )
        currency = page.currency or spec.get("source_metadata", {}).get("currency")
        if not currency:
            raise AdapterError(
                "currency_required",
                "No explicit source currency found; supply source_metadata.currency",
            )
        currency = str(currency).upper()
        if len(currency) != 3 or not currency.isalpha():
            raise AdapterError(
                "schema_changed", "Currency must be an explicit three-letter code"
            )
        normalized_filters = {}
        for key, value in filters.items():
            key = key if key.startswith("attribute_") else "attribute_" + key
            if key not in page.domains:
                raise AdapterError(
                    "invalid_selection", f"Unknown product attribute: {key}"
                )
            if not any(matches(choice, value) for choice in page.domains[key]):
                raise AdapterError(
                    "invalid_selection", f"No listed option matches {key}"
                )
            normalized_filters[key] = value
        if not page.domains or any(not values for values in page.domains.values()):
            raise AdapterError(
                "incomplete_response", "No finite listed WooCommerce option domain"
            )
        if page.variations is False:
            async for record in self._dynamic(
                spec, transport, emit, page, response, currency, normalized_filters
            ):
                yield record
            return
        if not isinstance(page.variations, list):
            raise AdapterError(
                "dynamic_variations_required",
                "No supported variation array or explicit dynamic form marker",
            )
        records = 0
        unavailable = 0
        emitted = set()
        for variation in page.variations:
            if (
                not isinstance(variation, dict)
                or not variation.get("variation_id")
                or not isinstance(variation.get("attributes"), dict)
            ):
                raise AdapterError("schema_changed", "Malformed WooCommerce variation")
            if set(variation["attributes"]) != set(page.domains) or any(
                not isinstance(value, str) for value in variation["attributes"].values()
            ):
                raise AdapterError(
                    "schema_changed",
                    "Variation attributes must cover the listed controls with string identifiers",
                )
            domains = {}
            for key, raw_value in variation["attributes"].items():
                choices = page.domains.get(key, [])
                if raw_value:
                    choices = [c for c in choices if str(c["id"]) == str(raw_value)]
                    if not choices:
                        raise AdapterError(
                            "schema_changed",
                            "Variation references an option missing from the listed dropdown domain",
                        )
                if key in normalized_filters:
                    choices = [
                        c for c in choices if matches(c, normalized_filters[key])
                    ]
                if not choices:
                    if key in normalized_filters:
                        break
                    raise AdapterError(
                        "incomplete_response",
                        "Wildcard variation has no enumerated dropdown values",
                    )
                domains[key] = choices
            else:
                if set(domains) != set(page.domains):
                    raise AdapterError(
                        "incomplete_response",
                        "Variation attributes do not cover the dropdown domain",
                    )
                names = sorted(domains)
                for values in itertools.product(*(domains[n] for n in names)):
                    signature = tuple((k, str(v["id"])) for k, v in zip(names, values))
                    # WooCommerce uses the first matching variation when wildcard
                    # and explicit entries overlap. Preserve the source ordering.
                    if signature in emitted:
                        continue
                    emitted.add(signature)
                    # First-match precedence applies even if that first match is
                    # unavailable. A later explicit variant cannot replace it.
                    if not unit_visible(variation):
                        unavailable += 1
                        continue
                    price = money(variation.get("display_price"))
                    image = variation.get("image") or {}
                    image_url = (
                        image.get("full_src") or image.get("src")
                        if isinstance(image, dict)
                        else None
                    )
                    records += 1
                    yield {
                        "product_url": spec["url"],
                        "title": page.title.strip(),
                        "source_product_id": str(variation["variation_id"]),
                        "configuration": {
                            k: {"value": v["id"], "label": v["label"]}
                            for k, v in zip(names, values)
                        },
                        "quantity": 1,
                        "currency": currency,
                        "subtotal": format(price, "f"),
                        "unit_price": format(price, "f"),
                        "provenance": "source_table",
                        "verification_status": "pending",
                        "evidence_ids": [response.response_id],
                        "production": {"status": "not_quoted"},
                        "images": (
                            [urljoin(spec["url"], image_url)] if image_url else []
                        ),
                        "coverage_status": "partial",
                        "coverage_reasons": [
                            "quantity_discount_domain_not_observed",
                            "storefront_may_omit_hidden_variations",
                        ],
                        "availability": {
                            "in_stock": variation.get("is_in_stock"),
                            "purchasable": variation.get("is_purchasable"),
                            "minimum_quantity": variation.get("min_qty"),
                            "maximum_quantity": variation.get("max_qty"),
                        },
                        "pricing_scope": "source_display_price",
                        "tax_basis": "source_display",
                    }
        await emit(
            "coverage",
            {
                "status": "partial",
                "rows": records,
                "reasons": [
                    "quantity_discount_domain_not_observed",
                    "storefront_may_omit_hidden_variations",
                ],
                "unavailable_or_nonunit_configurations": unavailable,
                "embedded_variations_exhausted": True,
                "verification_complete": False,
                "count_is_exact": False,
                "shipping_status": "not_requested",
            },
        )

    async def _dynamic(self, spec, transport, emit, page, initial, currency, filters):
        endpoint = page.ajax_url or spec.get("source_metadata", {}).get(
            "variation_endpoint"
        )
        if not endpoint or not page.product_id:
            raise AdapterError(
                "dynamic_variations_required",
                "The page did not expose a supported get_variation endpoint and product ID",
            )
        endpoint = urljoin(
            spec["url"], str(endpoint).replace("%%endpoint%%", "get_variation")
        )
        parsed, source = urlparse(endpoint), urlparse(spec["url"])
        query = parse_qs(parsed.query)
        if (
            (parsed.scheme, parsed.netloc) != (source.scheme, source.netloc)
            or query.get("wc-ajax") != ["get_variation"]
            or any(key in query for key in ("add-to-cart", "remove_item", "checkout"))
        ):
            raise AdapterError(
                "unsupported_endpoint",
                "Only the source's same-origin wc-ajax=get_variation read endpoint is supported",
            )
        domains = {
            key: [
                choice
                for choice in values
                if key not in filters or matches(choice, filters[key])
            ]
            for key, values in page.domains.items()
        }
        if any(not values for values in domains.values()):
            raise AdapterError(
                "invalid_selection", "A requested filter matches no listed options"
            )
        names = sorted(domains)
        await emit(
            "request_plan",
            {
                "strategy": "validate_each_candidate",
                "candidate_upper_bound": prod(len(domains[key]) for key in names),
                "valid_count_is_exact": False,
            },
        )
        records, invalid, candidates = 0, 0, 0
        for values in itertools.product(*(domains[key] for key in names)):
            chosen = dict(zip(names, values))
            payload = {key: value["id"] for key, value in chosen.items()}
            payload["product_id"] = str(page.product_id)
            response = await transport.fetch(
                endpoint,
                method="POST",
                data=payload,
                context={
                    "adapter": self.name,
                    "lane": "pricing",
                    "product_id": str(page.product_id),
                },
            )
            candidates += 1
            if response.status >= 400:
                raise AdapterError(
                    "source_error", f"Variation lookup returned HTTP {response.status}"
                )
            variation = response.data
            if isinstance(variation, str):
                try:
                    variation = json.loads(variation)
                except ValueError as exc:
                    raise AdapterError(
                        "schema_changed", "Variation endpoint did not return JSON"
                    ) from exc
            if variation is False:
                invalid += 1
                continue
            if (
                not isinstance(variation, dict)
                or not variation.get("variation_id")
                or not isinstance(variation.get("attributes"), dict)
            ):
                raise AdapterError(
                    "schema_changed", "Malformed dynamic WooCommerce variation"
                )
            if set(variation["attributes"]) != set(domains):
                raise AdapterError(
                    "incomplete_response",
                    "Variation attributes do not match listed controls",
                )
            if any(
                variation["attributes"][key] not in ("", value["id"])
                for key, value in chosen.items()
            ):
                raise AdapterError(
                    "configuration_mismatch",
                    "Variation lookup resolved different selections than requested",
                )
            if not unit_visible(variation):
                invalid += 1
                continue
            price = money(variation.get("display_price"))
            image = variation.get("image") or {}
            image_url = (
                (image.get("full_src") or image.get("src"))
                if isinstance(image, dict)
                else None
            )
            records += 1
            yield {
                "product_url": spec["url"],
                "title": page.title.strip(),
                "source_product_id": str(variation["variation_id"]),
                "configuration": {
                    key: {"value": value["id"], "label": value["label"]}
                    for key, value in chosen.items()
                },
                "quantity": 1,
                "currency": currency,
                "subtotal": format(price, "f"),
                "unit_price": format(price, "f"),
                "provenance": "source_variation_response",
                "verification_status": "pending",
                "evidence_ids": [initial.response_id, response.response_id],
                "production": {"status": "not_quoted"},
                "images": [urljoin(spec["url"], image_url)] if image_url else [],
                "coverage_status": "partial",
                "coverage_reasons": [
                    "quantity_discount_domain_not_observed",
                    "storefront_may_omit_hidden_variations",
                ],
                "availability": {
                    "in_stock": variation.get("is_in_stock"),
                    "purchasable": variation.get("is_purchasable"),
                    "minimum_quantity": variation.get("min_qty"),
                    "maximum_quantity": variation.get("max_qty"),
                },
                "pricing_scope": "source_display_price",
                "tax_basis": "source_display",
            }
        await emit(
            "coverage",
            {
                "status": "partial",
                "rows": records,
                "candidates_examined": candidates,
                "unavailable_candidates": invalid,
                "listed_domain_exhausted": True,
                "verification_complete": False,
                "count_is_exact": False,
                "reasons": [
                    "quantity_discount_domain_not_observed",
                    "storefront_may_omit_hidden_variations",
                ],
                "shipping_status": "not_requested",
            },
        )


def unit_visible(variation):
    if (
        variation.get("variation_is_visible") is False
        or variation.get("variation_is_active") is False
    ):
        return False
    try:
        minimum = Decimal(str(variation.get("min_qty", 1)))
        maximum = variation.get("max_qty")
        if not minimum.is_finite() or minimum < 0:
            raise ValueError("Invalid minimum")
        if maximum not in (None, ""):
            maximum = Decimal(str(maximum))
            if not maximum.is_finite() or maximum < 0:
                raise ValueError("Invalid maximum")
        return minimum <= 1 and (maximum in (None, "") or maximum >= 1)
    except Exception as exc:
        raise AdapterError(
            "schema_changed", "Invalid variation quantity limits"
        ) from exc


Adapter = WooCommerceAdapter
