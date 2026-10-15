"""Pure parsers for browser-captured Printograph tables; never performs requests.

These functions do not establish an additive pricing rule or cart verification.
The opt-in candidate builder keeps all reconstructed rows pending, and explicitly
requires production to be quoted for their configuration.
"""

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit

from ..commerce import unit_price
from ..presentation import present_row
from ..service_context import canonical_service_context, service_context_is_bound


class TableSchemaError(ValueError):
    pass


def _quantity(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TableSchemaError(
            "Table quantity must be a positive integer value, not an option ID"
        )
    return value


def _amount(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise TableSchemaError("Table prices must be decimal strings; null is not zero")
    # Captured Club Flyers tables use US thousands separators. Accept that
    # exact numeric grammar, never locale guessing or blanket comma removal.
    if re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value) is None:
        raise TableSchemaError("Invalid table decimal price")
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation:
        raise TableSchemaError("Invalid table decimal price") from None
    if not parsed.is_finite():
        raise TableSchemaError("Table price must be finite")
    return parsed


@dataclass(frozen=True)
class PriceEntry:
    quantity: int
    markup_price: Decimal | None
    list_price: Decimal | None
    package_as_sub_item_price: Decimal | None


def parse_price_table(payload):
    """Return exact quantity->price entries; no interpolation or flag inference."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"items"}
        or not isinstance(payload["items"], list)
    ):
        raise TableSchemaError("Expected an unpaginated items price table")
    result = {}
    for item in payload["items"]:
        if (
            not isinstance(item, dict)
            or "quantity" not in item
            or "markupPrice" not in item
            or set(item)
            - {"quantity", "markupPrice", "listPrice", "packageASubItemPrice"}
        ):
            raise TableSchemaError("Unsupported price table row schema")
        quantity = _quantity(item["quantity"])
        entry = PriceEntry(
            quantity,
            _amount(item["markupPrice"], nullable=True),
            _amount(item.get("listPrice"), nullable=True),
            _amount(item.get("packageASubItemPrice"), nullable=True),
        )
        if quantity in result:
            raise TableSchemaError("Duplicate quantity in one captured price table")
        result[quantity] = entry
    return result


def parse_quantity_support(payload):
    """Return quantity values and exact turnaround IDs, without labeling IDs days."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {"items"}
        or not isinstance(payload["items"], list)
    ):
        raise TableSchemaError("Expected an items quantity-support table")
    result = {}
    for item in payload["items"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"quantity", "turnaroundList"}
            or not isinstance(item["turnaroundList"], list)
        ):
            raise TableSchemaError("Unsupported quantity-support row schema")
        quantity = _quantity(item["quantity"])
        if quantity in result:
            raise TableSchemaError("Duplicate supported quantity")
        ids = []
        for value in item["turnaroundList"]:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, str))
                or not str(value)
            ):
                raise TableSchemaError("Invalid turnaround ID")
            ids.append(str(value))
        result[quantity] = tuple(ids)
    return result


def parse_option_quantity_support(payload):
    """Keys are option *type* IDs; these are not prices endpoint option IDs."""
    if not isinstance(payload, dict):
        raise TableSchemaError("Expected option-type quantity support")
    return {
        str(type_id): parse_quantity_support({"items": rows})
        for type_id, rows in payload.items()
    }


def parse_option_catalog(payload):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"items"}
        or not isinstance(payload["items"], list)
    ):
        raise TableSchemaError("Expected an items option catalog")
    result = {}
    for item in payload["items"]:
        try:
            option_id, type_id, group_id = (
                str(item["id"]),
                str(item["type"]["id"]),
                str(item["group"]["id"]),
            )
        except (KeyError, TypeError):
            raise TableSchemaError(
                "Option ID, type ID and group ID are required"
            ) from None
        if option_id in result:
            raise TableSchemaError("Duplicate prices endpoint option ID")
        result[option_id] = {
            **deepcopy(item),
            "id": option_id,
            "type_id": type_id,
            "group_id": group_id,
        }
    return result


def resolve_option(catalog, *, type_id, group_id):
    """Resolve within the captured base product; group avoids unrelated ID collisions."""
    matches = [
        option
        for option in catalog.values()
        if option["type_id"] == str(type_id) and option["group_id"] == str(group_id)
    ]
    if len(matches) != 1:
        raise TableSchemaError(
            "Option selection is absent or ambiguous in this product catalog"
        )
    return matches[0]


def price_capture(capture):
    """Read a fixture/browser evidence mapping containing url, data and evidence_id."""
    try:
        parsed_url = urlsplit(capture["url"])
        match = re.fullmatch(
            r"/service/rest/v1/products/(\d+)(?:/options/(\d+))?/prices",
            parsed_url.path,
        )
        if match is None:
            raise TableSchemaError("Capture is not a recognized product price route")
        evidence_id = capture["evidence_id"]
        if not isinstance(evidence_id, str) or not evidence_id:
            raise TableSchemaError("A captured table requires an evidence ID")
        return {
            "origin": (parsed_url.scheme.lower(), parsed_url.netloc.lower()),
            "product_id": match.group(1),
            "option_id": match.group(2),
            "prices": parse_price_table(capture["data"]),
            "evidence_id": evidence_id,
        }
    except (KeyError, TypeError):
        raise TableSchemaError("Incomplete captured price table") from None


def valid_quantities(
    base_prices,
    quantity_support,
    *,
    visible_quantities,
    option_support=(),
    turnaround_id=None
):
    """Intersect observed domains. Extra option tiers never create product variants."""
    option_support = tuple(option_support)
    allowed = (
        set(base_prices)
        & set(quantity_support)
        & {_quantity(value) for value in visible_quantities}
    )
    for support in option_support:
        allowed &= set(support)
    if turnaround_id is not None:
        allowed = {
            quantity
            for quantity in allowed
            if str(turnaround_id) in quantity_support[quantity]
            and all(
                str(turnaround_id) in support[quantity] for support in option_support
            )
        }
    return sorted(allowed)


def reconstruct_candidate(
    base_capture,
    *,
    quantity,
    currency,
    allowed_quantities,
    configuration,
    option_captures=(),
    selected_option_ids=(),
    allow_additive=False
):
    """Calculate an explicitly requested provisional candidate using exact Decimals.

    Callers must bind captures to the same observed configuration/account snapshot,
    pass the intersection from valid_quantities, and separately validate effective
    configuration and production. Selected IDs are prices-endpoint IDs, not DOM IDs.
    This function intentionally does not turn any row into a verified quote.
    """
    quantity = _quantity(quantity)
    if not isinstance(configuration, dict):
        raise TableSchemaError("An explicit effective configuration object is required")
    if quantity not in allowed_quantities:
        raise TableSchemaError("Quantity is outside the observed valid product domain")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise TableSchemaError("Explicit three-letter currency is required")
    base = price_capture(base_capture)
    if base["option_id"] is not None:
        raise TableSchemaError("A product base-price capture is required")
    selected = [str(value) for value in selected_option_ids]
    if len(selected) != len(set(selected)):
        raise TableSchemaError("An option charge may only be included once")
    if selected and not allow_additive:
        raise TableSchemaError(
            "Additive option reconstruction requires explicit opt-in"
        )
    options = {}
    for capture in option_captures:
        parsed = price_capture(capture)
        if (
            parsed["origin"] != base["origin"]
            or parsed["product_id"] != base["product_id"]
            or parsed["option_id"] is None
        ):
            raise TableSchemaError(
                "Option price table belongs to a different base product"
            )
        if parsed["option_id"] in options:
            raise TableSchemaError(
                "Duplicate option table; choose one consistent capture"
            )
        options[parsed["option_id"]] = parsed
    if any(option_id not in options for option_id in selected):
        raise TableSchemaError("A selected option has no captured price table")
    tables = [base] + [options[option_id] for option_id in selected]
    components = []
    for table in tables:
        entry = table["prices"].get(quantity)
        if entry is None or entry.markup_price is None:
            raise TableSchemaError(
                "A required exact quantity price is missing; no interpolation"
            )
        components.append(
            {
                "option_id": table["option_id"],
                "markup_price": format(entry.markup_price, "f"),
                "evidence_id": table["evidence_id"],
            }
        )
    total = sum(
        (Decimal(component["markup_price"]) for component in components), Decimal(0)
    )
    if total < 0:
        raise TableSchemaError("Reconstructed subtotal is negative")
    return present_row(
        {
            "source_product_id": base["product_id"],
            "configuration": deepcopy(configuration),
            "quantity": quantity,
            "currency": currency,
            "subtotal": format(total, "f"),
            "unit_price": unit_price(total, quantity),
            "provenance": "source_table",
            "verification_status": "pending",
            "evidence_ids": [table["evidence_id"] for table in tables],
            "production": {"status": "requires_configuration_quote"},
            "pricing_context": {"source_base_product_id": base["product_id"]},
            "table_derivation": {
                "formula": "base_plus_selected_options" if selected else "base_only",
                "components": components,
                "formula_status": "candidate_requires_configuration_verification",
                "independent_cart_verification": False,
                "rounding": "none; exact source decimal addition",
            },
        }
    )


def _square_roll_orientation(
    controls, captures, navigation_generation, selection_epoch
):
    """Resolve a source query selector, never synthesize an absent UI control."""
    import json
    from urllib.parse import parse_qsl

    shape = str(controls.get("shape", {}).get("value"))
    size = str(controls.get("size", {}).get("value"))
    if shape not in {"4", "9"} or not size.isdigit() or int(size) <= 0:
        raise TableSchemaError(
            "Absent orientation requires a finite square Roll preset"
        )
    found = []
    for capture in captures:
        epoch = capture.get("selection_epoch")
        parsed = urlsplit(capture.get("url", ""))
        if (
            capture.get("navigation_generation") != navigation_generation
            or not isinstance(epoch, int)
            or not 0 <= epoch <= selection_epoch
            or parsed.scheme != "https"
            or parsed.hostname not in {"printograph.com", "www.printograph.com"}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.path != "/service/rest/v1/settings/product/specifications"
        ):
            continue
        # The dimension-only endpoint may predate form metadata. The exact
        # query identifies its product, while the final snapshot binds broker
        # context. Conflicting explicit metadata must never revive an old spec.
        if capture.get("request_product_type") not in (
            None,
            "",
            "36",
            36,
        ) or capture.get("request_store_type") not in (None, "", "broker"):
            raise TableSchemaError(
                "Square specification request metadata conflicts with its product context"
            )
        # Product-wide specifications are prefetched before the selected shape,
        # size or production widget exists. Their exact query binds product type.
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query = dict(pairs)
        if len(pairs) != len(query) or query != {
            "requestValues": "true",
            "productType": "36",
        }:
            raise TableSchemaError("Square specification query context is ambiguous")
        found.append(capture)
    if not found:
        raise TableSchemaError(
            "Absent orientation requires same-navigation square specifications"
        )
    newest_epoch = max(capture["selection_epoch"] for capture in found)
    newest = [
        capture for capture in found if capture["selection_epoch"] == newest_epoch
    ]
    if any(
        not isinstance(capture.get("status"), int)
        or not 200 <= capture["status"] < 300
        or not isinstance(capture.get("evidence_id"), str)
        or not capture["evidence_id"]
        for capture in newest
    ):
        raise TableSchemaError("Latest square specifications lack successful evidence")
    if (
        len(
            {
                json.dumps(capture.get("data"), sort_keys=True, allow_nan=False)
                for capture in newest
            }
        )
        != 1
    ):
        raise TableSchemaError(
            "Conflicting square specifications in one selection epoch"
        )
    capture = newest[-1]
    data = capture.get("data")
    sizes = data.get("sizes") if isinstance(data, dict) else None
    if not isinstance(sizes, list):
        raise TableSchemaError("Square specifications contain no finite sizes")
    selected = [
        item for item in sizes if isinstance(item, dict) and str(item.get("id")) == size
    ]
    if len(selected) != 1:
        raise TableSchemaError(
            "Square preset is missing or ambiguous in specifications"
        )
    item = selected[0]
    properties = item.get("properties") or {}
    orientation = item.get("orientation") or {}
    if not isinstance(properties, dict) or not isinstance(orientation, dict):
        raise TableSchemaError("Square specification properties must be objects")
    try:
        width = Decimal(str(properties.get("width")))
        height = Decimal(str(properties.get("height")))
    except (InvalidOperation, ValueError):
        raise TableSchemaError(
            "Square specifications require explicit finite dimensions"
        ) from None
    if (
        str(properties.get("shape")) != shape
        or not width.is_finite()
        or not height.is_finite()
        or width <= 0
        or width != height
        or str(orientation.get("id")) not in {"1", "2"}
    ):
        raise TableSchemaError(
            "Selected preset is not an explicit square with source orientation"
        )
    return str(orientation["id"]), capture


def bind_business_card_tables(
    snapshot, captures, *, navigation_generation, selection_epoch, filters=None
):
    """Bind supported print-product selections to immutable browser captures.

    Each capture contains url, data, evidence_id, status, navigation_generation,
    selection_epoch and request-start request_controls/request_store_type/
    request_product_type. An older selection epoch is reusable only inside the
    exact supported dependency projection; another navigation is never reusable.
    Ambiguity or an unsupported branch raises TableSchemaError for DOM fallback.
    """
    from urllib.parse import parse_qsl

    services = canonical_service_context(snapshot)
    if not service_context_is_bound(services):
        raise TableSchemaError(
            "Bulk quantities require explicitly observed unambiguous service context"
        )
    if not isinstance(navigation_generation, int) or not isinstance(
        selection_epoch, int
    ):
        raise TableSchemaError("Current navigation and selection epoch are required")
    product_type = str(snapshot.get("product_type"))
    supported_types = {"1": "business-cards", "27": "club-flyers", "36": "roll-labels"}
    is_roll = product_type == "36"
    base_keys = ("size", "paper", "color", "orientation") + (
        ("shape", "finish") if is_roll else ()
    )
    required_keys = (
        set(base_keys)
        | {"productionTime", "quantity"}
        | ({"windPosition"} if is_roll else set())
    )
    inactive_finish_keys = set() if is_roll else {"spotcoating", "raisedprint"}
    expected_keys = required_keys | (
        set() if is_roll else {"coating", "roundedCorners"} | inactive_finish_keys
    )
    controls = {}
    for control in snapshot.get("controls", []):
        key = control.get("key")
        if key in controls:
            raise TableSchemaError("Duplicate effective controls prevent table binding")
        controls[key] = control
    for key in inactive_finish_keys & set(controls):
        control = controls[key]
        empty_choices = [
            choice for choice in control.get("choices", []) if choice.get("value") == ""
        ]
        if (
            control.get("kind") != "select"
            or control.get("value") != ""
            or control.get("selected_label") != "None"
            or len(empty_choices) != 1
            or empty_choices[0].get("label") != "None"
            or empty_choices[0].get("disabled") is not False
            or empty_choices[0].get("placeholder") is not False
        ):
            raise TableSchemaError(
                "Optional finishing controls require an explicit enabled None selection"
            )
    included_coating = False
    if not is_roll and controls.get("coating", {}).get("value") == "":
        coating_control = controls["coating"]
        empty_choices = [
            choice
            for choice in coating_control.get("choices", [])
            if choice.get("value") == ""
        ]
        if (
            coating_control.get("kind") != "select"
            or coating_control.get("selected_label") != "Matte Aqueous Coating"
            or len(empty_choices) != 1
            or empty_choices[0].get("label") != "Matte Aqueous Coating"
            or empty_choices[0].get("disabled") is not False
            or empty_choices[0].get("placeholder") is not False
        ):
            raise TableSchemaError(
                "Included coating requires one explicit enabled Matte Aqueous Coating selection"
            )
        included_coating = True
    square_capture = None
    square_orientation = None
    if is_roll and "orientation" not in controls:
        square_orientation, square_capture = _square_roll_orientation(
            controls, captures, navigation_generation, selection_epoch
        )
        required_keys.remove("orientation")
    unsupported = snapshot.get("unsupported") or []
    # The existence of an unselected custom-size choice does not invalidate a
    # finite preset. The walker retains this global coverage gap separately.
    allowed_custom_size_gap = is_roll and all(
        value == "custom_values:size" for value in unsupported
    )
    if (
        not required_keys <= set(controls)
        or set(controls) - expected_keys
        or (unsupported and not allowed_custom_size_gap)
    ):
        raise TableSchemaError(
            "Bulk binding requires the supported complete standard print controls"
        )
    if product_type not in supported_types or snapshot.get("store_type") != "broker":
        raise TableSchemaError(
            "Bulk binding requires a supported standard print broker context"
        )
    if snapshot.get("product_slug") not in (None, supported_types[product_type]):
        raise TableSchemaError("Product slug and source product type disagree")
    required = {
        key: (
            square_orientation
            if key == "orientation" and square_capture
            else str(controls[key].get("value"))
        )
        for key in (
            *base_keys,
            "productionTime",
            *(
                ("windPosition",)
                if is_roll
                else (("coating",) if "coating" in controls else ())
            ),
        )
    }
    if (
        any(not required[key].isdigit() or int(required[key]) <= 0 for key in base_keys)
        or required["orientation"] not in {"1", "2"}
        or (
            "coating" in required
            and not included_coating
            and required["coating"] not in {"5", "40"}
        )
        or required["productionTime"] not in ({"1"} if is_roll else {"1", "3"})
    ):
        raise TableSchemaError(
            "This configuration is outside the supported standard print branch"
        )
    production_mode = required["productionTime"]
    corners = (
        str(controls["roundedCorners"].get("value"))
        if "roundedCorners" in controls
        else None
    )
    if corners not in {None, "", "2"}:
        raise TableSchemaError("Unsupported active rounded-corner selection")
    wind_names = {
        "32": "WIND_POSITION_OFF_BOTTOM",
        "33": "WIND_POSITION_OFF_LEFT",
        "34": "WIND_POSITION_OFF_RIGHT",
        "35": "WIND_POSITION_OFF_TOP",
        "54": "WIND_POSITION_UNIMPORTANT",
    }
    if is_roll:
        if required["windPosition"] not in wind_names:
            raise TableSchemaError("Unsupported active wind-position selection")
        preset = [
            choice
            for choice in controls["size"].get("choices", [])
            if str(choice.get("value")) == required["size"]
            and not choice.get("disabled")
            and not choice.get("placeholder")
            and re.match(r"^(custom|other)(?:\s|$)", str(choice.get("label", "")), re.I)
            is None
        ]
        if len(preset) != 1:
            raise TableSchemaError(
                "Roll bulk binding requires one visible finite size preset"
            )
    if snapshot.get("currency") != "USD":
        raise TableSchemaError("Bulk binding requires an explicit USD source currency")
    if not isinstance(navigation_generation, int) or not isinstance(
        selection_epoch, int
    ):
        raise TableSchemaError("Current navigation and selection epoch are required")
    filters = filters or {}
    if not isinstance(filters, dict) or set(filters) - set(controls):
        raise TableSchemaError("Unknown filters prevent table binding")

    def matches(control, requested):
        values = requested if isinstance(requested, list) else [requested]
        selected = {
            str(control.get("value", "")).strip().casefold(),
            str(control.get("selected_label", control.get("label", "")))
            .strip()
            .casefold(),
        }
        return any(str(value).strip().casefold() in selected for value in values)

    for key, requested in filters.items():
        if key != "quantity" and not matches(controls[key], requested):
            raise TableSchemaError(
                "Effective selection does not match a requested filter"
            )

    eligible = []
    for capture in captures:
        epoch = capture.get("selection_epoch")
        if (
            capture.get("navigation_generation") != navigation_generation
            or not isinstance(epoch, int)
            or epoch > selection_epoch
            or epoch < 0
        ):
            continue
        if (
            str(capture.get("request_product_type")) != product_type
            or capture.get("request_store_type") != "broker"
        ):
            continue
        start = capture.get("request_controls")
        base_dependencies = {key: required[key] for key in base_keys}
        if square_capture and isinstance(start, dict) and "orientation" not in start:
            base_dependencies.pop("orientation")
        if (
            not isinstance(start, dict)
            or not set(base_dependencies) <= set(start)
            or set(start) - expected_keys
        ):
            continue
        # None is an observed selection, not a missing/default control. Keep its
        # exact presence and empty value in the request dependency projection.
        if any(
            (key in start) != (key in controls)
            or (key in controls and start[key] != "")
            for key in inactive_finish_keys
        ):
            continue
        # These public routes receive the base selectors or an already resolved
        # product ID, not the quantity/production widget's readiness state. The
        # exact lookup query supplies the selected turnaround. Missing widgets are not
        # assigned defaults; final snapshot and DOM samples must supply them.
        if any(
            str(start.get(key)) != value for key, value in base_dependencies.items()
        ):
            continue
        parsed = urlsplit(capture.get("url", ""))
        turnaround_support = parsed.path in {
            "/service/rest/v1/products/quantities",
            "/service/rest/v1/products/options/quantities",
        }
        # These two routes explicitly return all supported turnaround IDs and
        # receive no selected turnaround. A prefetch made while Rush was
        # selected is therefore usable for Regular on the same base projection.
        # select_capture still requires the exact includeTurnaround=true query;
        # lookup and all product-ID routes retain production-context binding.
        optional_dependencies = (
            [] if turnaround_support else [("productionTime", production_mode)]
        )
        if any(
            start.get(key) not in (None, "", expected)
            for key, expected in optional_dependencies
        ):
            continue
        # The base/product-ID routes do not take a selected coating. These two
        # captured coating types may share prefetched tables; the final catalog,
        # quantity support and two effective DOM quotes still bind the choice.
        if not is_roll and start.get("coating") not in (None, "", "5", "40"):
            continue
        if included_coating and ("coating" not in start or start["coating"] != ""):
            continue
        if not is_roll and start.get("roundedCorners") not in (None, "", "2"):
            continue
        if is_roll and start.get("windPosition") not in (None, "", *wind_names):
            continue
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"printograph.com", "www.printograph.com"}
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            continue
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        # Keep ambiguous queries visible so a later one cannot silently revive
        # an older matching table from another pricing/account context.
        query = dict(pairs) if len({key for key, _ in pairs}) == len(pairs) else None
        eligible.append((capture, parsed, query))

    def select_capture(path, query_check):
        found = [
            capture
            for capture, parsed, query in eligible
            if parsed.path == path and query is not None and query_check(query)
        ]
        if not found:
            raise TableSchemaError("No configuration-bound capture for " + path)
        newest_epoch = max(capture["selection_epoch"] for capture in found)
        scoped = []
        for capture, parsed, query in eligible:
            if parsed.path != path or capture["selection_epoch"] < newest_epoch:
                continue
            if query is not None and query_check(query):
                continue
            # A normal single-quantity request is a narrower view of the same
            # price context. It may coexist with a full table, but must agree.
            narrower = (
                query is not None and path.endswith("/prices") and "quantities" in query
            )
            if narrower:
                values = query["quantities"].split(",")
                remainder = {
                    key: value for key, value in query.items() if key != "quantities"
                }
                narrower = (
                    bool(values)
                    and len(values) == len(set(values))
                    and all(value.isdigit() and int(value) > 0 for value in values)
                    and query_check(remainder)
                )
            if not narrower:
                raise TableSchemaError(
                    "Later source capture changed or obscured the query context"
                )
            scoped.append((capture, {int(value) for value in values}))
        newest = [
            capture for capture in found if capture["selection_epoch"] == newest_epoch
        ]
        if any(
            capture.get("request_service_context") != services
            for capture in [*newest, *(capture for capture, _ in scoped)]
        ):
            raise TableSchemaError(
                "Source capture service context is unknown or differs from the DOM quote"
            )
        if any(
            not isinstance(capture.get("status"), int)
            or not 200 <= capture["status"] < 300
            for capture in newest
        ):
            raise TableSchemaError("Latest matching source capture was unsuccessful")
        import json

        representations = {
            json.dumps(
                capture.get("data"),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            for capture in newest
        }
        if len(representations) != 1:
            raise TableSchemaError("Conflicting source captures in one selection epoch")
        if any(
            not isinstance(capture.get("evidence_id"), str)
            or not capture["evidence_id"]
            for capture in newest
        ):
            raise TableSchemaError("Bound source captures require evidence IDs")
        if scoped:
            full_prices = parse_price_table(newest[-1]["data"])
            for capture, requested_quantities in scoped:
                if (
                    not isinstance(capture.get("status"), int)
                    or not 200 <= capture["status"] < 300
                ):
                    raise TableSchemaError(
                        "Latest scoped source capture was unsuccessful"
                    )
                scoped_prices = parse_price_table(capture["data"])
                if set(scoped_prices) != requested_quantities:
                    raise TableSchemaError(
                        "Scoped source response does not contain every requested quantity"
                    )
                if any(
                    quantity not in full_prices or entry != full_prices[quantity]
                    for quantity, entry in scoped_prices.items()
                ):
                    raise TableSchemaError(
                        "Scoped source prices disagree with the full captured table"
                    )
        return newest[-1]

    base_query = {
        "productType": product_type,
        **{key: required[key] for key in base_keys},
    }
    lookup_query = {**base_query, "turnaround": production_mode}
    lookup = select_capture(
        "/service/rest/v1/products", lambda query: query == lookup_query
    )
    lookup_data = lookup.get("data")
    if (
        not isinstance(lookup_data, dict)
        or not isinstance(lookup_data.get("items"), list)
        or len(lookup_data["items"]) != 1
    ):
        raise TableSchemaError("Exact product lookup must resolve one base product")
    # A larger declared count would mean the response was truncated/paginated.
    if any(
        key in lookup_data and lookup_data[key] not in (1, None)
        for key in ("total", "totalCount", "count")
    ):
        raise TableSchemaError(
            "Product lookup is not a complete single-product response"
        )
    item = lookup_data["items"][0]
    identifier = item.get("id") if isinstance(item, dict) else None
    if (
        isinstance(identifier, bool)
        or not str(identifier).isdigit()
        or int(identifier) <= 0
    ):
        raise TableSchemaError("Exact lookup did not return a valid base product ID")
    product_id = str(identifier)
    prefix = "/service/rest/v1/products/" + product_id
    lookup_origin = urlsplit(lookup["url"]).netloc.lower()
    if (
        square_capture
        and urlsplit(square_capture["url"]).netloc.lower() != lookup_origin
    ):
        raise TableSchemaError(
            "Square specifications and product lookup have different origins"
        )
    # Avoid joining otherwise identical numeric IDs from another origin alias.
    eligible = [entry for entry in eligible if entry[1].netloc.lower() == lookup_origin]
    base_capture = select_capture(
        prefix + "/prices",
        lambda query: set(query) <= {"itemId", "cid"}
        and query.get("itemId") in (None, "", "null")
        and query.get("cid") in (None, ""),
    )
    quantity_capture = select_capture(
        "/service/rest/v1/products/quantities",
        lambda query: query == {**base_query, "includeTurnaround": "true"},
    )
    needed_types = (
        {required["windPosition"]}
        if is_roll
        else (
            {required["coating"]}
            if "coating" in controls and not included_coating
            else set()
        )
        | ({"2"} if corners == "2" else set())
    )
    catalog_capture = (
        select_capture(
            prefix + "/options",
            lambda query: query == {"requestValues": "true", "eddm": "false"},
        )
        if needed_types
        else None
    )
    catalog = parse_option_catalog(catalog_capture["data"]) if catalog_capture else {}
    # The page requests support for every offered option together. Extra
    # unselected IDs are harmless only when this exact product catalog proves
    # they are real types; their domains never constrain the selected options.
    allowed_types = {option["type_id"] for option in catalog.values()}

    def option_domain_query(query):
        wanted = {**base_query, "includeTurnaround": "true"}
        if set(query) != set(wanted) | {"optionIds"} or any(
            query[key] != value for key, value in wanted.items()
        ):
            return False
        ids = query["optionIds"].split(",")
        return len(ids) == len(set(ids)) and needed_types <= set(ids) <= allowed_types

    option_quantity_capture = (
        select_capture(
            "/service/rest/v1/products/options/quantities", option_domain_query
        )
        if needed_types
        else None
    )
    if is_roll:
        wind = resolve_option(catalog, type_id=required["windPosition"], group_id="12")
        if wind.get("name") != wind_names[required["windPosition"]]:
            raise TableSchemaError("Wind-position semantics changed")
    elif "coating" in controls and not included_coating:
        coating = resolve_option(catalog, type_id=required["coating"], group_id="4")
        coating_name = {"5": "UV_COATING_FRONT", "40": "UV_COATING_BOTH"}[
            required["coating"]
        ]
        if coating.get("name") != coating_name:
            raise TableSchemaError("Coating semantics changed from the baseline branch")
    selected_option_ids = []
    option_captures = []
    if corners == "2":
        corner = resolve_option(catalog, type_id="2", group_id="2")
        if corner.get("name") != "ROUND_CORNERS":
            raise TableSchemaError("Rounded-corner semantics changed")
        selected_option_ids.append(corner["id"])
        option_captures.append(
            select_capture(
                prefix + "/options/" + corner["id"] + "/prices", lambda query: not query
            )
        )
    quantity_support = parse_quantity_support(quantity_capture["data"])
    option_support = (
        parse_option_quantity_support(option_quantity_capture["data"])
        if option_quantity_capture
        else {}
    )
    if not needed_types <= set(option_support):
        raise TableSchemaError("A selected option has no quantity-support response")
    visible_quantities = []
    for choice in controls["quantity"].get("choices", []):
        if choice.get("placeholder") or choice.get("disabled"):
            continue
        raw = str(choice.get("value", ""))
        if not raw.isdigit() or int(raw) <= 0:
            raise TableSchemaError("Quantity choices are not actual positive counts")
        visible_quantities.append(int(raw))
    parsed_base = price_capture(base_capture)
    allowed = valid_quantities(
        parsed_base["prices"],
        quantity_support,
        visible_quantities=visible_quantities,
        option_support=[option_support[type_id] for type_id in sorted(needed_types)],
        turnaround_id=production_mode,
    )
    allowed = [
        quantity
        for quantity in allowed
        if parsed_base["prices"][quantity].markup_price is not None
    ]
    for option_capture in option_captures:
        option_prices = price_capture(option_capture)["prices"]
        allowed = [
            quantity
            for quantity in allowed
            if quantity in option_prices
            and option_prices[quantity].markup_price is not None
        ]
    # The anchor can be outside an output-only quantity filter. Still verify it
    # against the full valid domain before selecting the output quantities.
    raw_quantity = str(controls["quantity"].get("value", ""))
    if not raw_quantity.isdigit() or int(raw_quantity) <= 0:
        raise TableSchemaError("Current source quantity is invalid")
    current_quantity = int(raw_quantity)
    configuration = {
        key: {"value": control.get("value"), "label": control.get("selected_label", "")}
        for key, control in controls.items()
        if key not in {"quantity", "productionTime"}
    }
    anchor = reconstruct_candidate(
        base_capture,
        quantity=current_quantity,
        currency="USD",
        allowed_quantities=allowed,
        configuration=configuration,
        option_captures=option_captures,
        selected_option_ids=selected_option_ids,
        allow_additive=bool(selected_option_ids),
    )
    displayed = str(snapshot.get("price", snapshot.get("subtotal_text", ""))).strip()
    match = re.fullmatch(r"\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)", displayed)
    if match is None or _amount(match.group(1).replace(",", "")) != Decimal(
        anchor["subtotal"]
    ):
        raise TableSchemaError(
            "Table reconstruction does not match the current effective DOM subtotal"
        )
    if "quantity" in filters:
        allowed = [
            quantity
            for quantity in allowed
            if matches(
                {"value": str(quantity), "selected_label": str(quantity)},
                filters["quantity"],
            )
        ]
    if not allowed:
        raise TableSchemaError("No table quantities remain after requested filters")
    evidence = [
        *([square_capture] if square_capture else []),
        lookup,
        base_capture,
        quantity_capture,
        *([catalog_capture] if catalog_capture else []),
        *([option_quantity_capture] if option_quantity_capture else []),
        *option_captures,
    ]
    return {
        "base_capture": base_capture,
        "option_captures": option_captures,
        "selected_option_ids": selected_option_ids,
        "allowed_quantities": allowed,
        "source_base_product_id": product_id,
        "evidence_ids": list(
            dict.fromkeys(capture["evidence_id"] for capture in evidence)
        ),
        "anchor_candidate": anchor,
        "binding": {
            "service_context": services,
            "request_service_contexts": [
                {
                    "evidence_id": capture["evidence_id"],
                    "service_context": deepcopy(capture["request_service_context"]),
                    "navigation_generation": capture["navigation_generation"],
                    "selection_epoch": capture["selection_epoch"],
                }
                for capture in evidence
                if capture is not square_capture
            ],
            "navigation_generation": navigation_generation,
            "selection_epoch": selection_epoch,
            "lookup_evidence_id": lookup["evidence_id"],
            "production_mode": production_mode,
            "formula_scope": (
                "roll_labels_base_with_observed_wind_selection"
                if is_roll
                else "standard_print_observed_controls_with_explicit_base_and_selected_corner_prices"
            ),
            "baseline_option_types": (
                [required["windPosition"]]
                if is_roll
                else (
                    [required["coating"]]
                    if "coating" in controls and not included_coating
                    else []
                )
            ),
            **(
                {
                    "included_coating": {
                        "value": "",
                        "label": "Matte Aqueous Coating",
                        "price_basis": "baseline_candidate",
                    }
                }
                if included_coating
                else {}
            ),
            "finishing_controls_present": sorted(
                set(controls)
                & {
                    "coating",
                    "roundedCorners",
                    "finish",
                    "windPosition",
                    *inactive_finish_keys,
                }
            ),
            "base_selection": base_query,
            **(
                {
                    "orientation_source": {
                        "kind": "square_size_specification",
                        "evidence_id": square_capture["evidence_id"],
                        "value": square_orientation,
                        "control_present": False,
                    }
                }
                if square_capture
                else {}
            ),
            "verification_status": "pending",
            "independent_cart_verification": False,
        },
    }
