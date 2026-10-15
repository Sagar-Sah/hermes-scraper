from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import pytest
from crawler.service_context import canonical_service_context

from crawler.adapters.browser_tables import (
    TableSchemaError,
    parse_price_table,
    parse_option_catalog,
    parse_option_quantity_support,
    parse_quantity_support,
    price_capture,
    reconstruct_candidate,
    resolve_option,
    valid_quantities,
)


@pytest.fixture
def captures():
    return json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "printograph_business_cards_tables.json"
        ).read_text()
    )["captures"]


def domain(captures, *, turnaround_id=None):
    control = next(
        control
        for control in captures["dom_quote"]["data"]["controls"]
        if control["key"] == "quantity"
    )
    visible = [
        int(choice["value"])
        for choice in control["choices"]
        if not choice.get("placeholder") and not choice.get("disabled")
    ]
    options = parse_option_quantity_support(captures["option_quantity_support"]["data"])
    return valid_quantities(
        parse_price_table(captures["base_prices"]["data"]),
        parse_quantity_support(captures["quantity_support"]["data"]),
        visible_quantities=visible,
        option_support=[options["2"], options["5"]],
        turnaround_id=turnaround_id,
    )


def configuration(captures, *, corners=""):
    result = {
        control["key"]: {"value": control["value"], "label": control["selected_label"]}
        for control in captures["dom_quote"]["data"]["controls"]
        if control["key"] != "quantity"
    }
    result["roundedCorners"] = {
        "value": corners,
        "label": "None" if not corners else "Round Corners",
    }
    return result


def test_captured_base_quantity_values_are_not_option_ids(captures):
    table = price_capture(captures["base_prices"])
    assert table["product_id"] == "100000021" and table["option_id"] is None
    assert len(table["prices"]) == 26
    assert table["prices"][50].markup_price == Decimal("4.69")
    assert table["prices"][250].markup_price == Decimal("9.45")
    assert table["prices"][100000].markup_price == Decimal("685.93")
    assert table["prices"][50].list_price is None
    assert table["prices"][50].package_as_sub_item_price is None


def test_option_type_id_maps_within_base_product_and_group(captures):
    catalog = parse_option_catalog(captures["option_catalog"]["data"])
    corner = resolve_option(catalog, type_id="2", group_id="2")
    assert corner["id"] == "100000104"
    assert corner["quantityDependentPrice"] is False
    prices = parse_price_table(captures["rounded_corner_prices"]["data"])
    assert prices[50].markup_price == Decimal("3.00")
    assert prices[250].markup_price == Decimal("5.00")
    prior = parse_option_catalog(captures["previous_option_catalog"]["data"])
    assert resolve_option(prior, type_id="2", group_id="2")["id"] == "100000004"
    with pytest.raises(TableSchemaError):
        resolve_option(catalog, type_id="2", group_id="4")


def test_quantity_intersection_prevents_fake_25_tier_and_limits_rush(captures):
    assert 25 in parse_price_table(captures["rounded_corner_prices"]["data"])
    regular = domain(captures, turnaround_id="1")
    assert len(regular) == 26 and regular[0] == 50 and regular[-1] == 100000
    assert domain(captures, turnaround_id="3") == [50, 100, 250, 500, 1000, 2500]


def test_selected_baseline_matches_captured_dom_but_stays_pending(captures):
    row = reconstruct_candidate(
        captures["base_prices"],
        quantity=50,
        currency="USD",
        allowed_quantities=domain(captures),
        configuration=configuration(captures),
    )
    assert (
        "$" + row["subtotal"]
        == captures["dom_quote"]["data"]["subtotal_text"]
        == "$4.69"
    )
    assert row["unit_price"] == "0.09380000"
    assert row["configuration"]["coating"]["value"] == "5"
    assert row["configuration"]["color"]["value"] == "2"
    assert row["configuration"]["paper"]["value"] == "1"
    assert row["verification_status"] == "pending"
    assert row["table_derivation"]["independent_cart_verification"] is False
    assert row["production"]["status"] == "requires_configuration_quote"


@pytest.mark.parametrize("quantity,expected", [(50, "7.69"), (250, "14.45")])
def test_opt_in_rounded_corner_candidates_match_manual_dom_followup(
    captures, quantity, expected
):
    row = reconstruct_candidate(
        captures["base_prices"],
        quantity=quantity,
        currency="USD",
        allowed_quantities=domain(captures),
        configuration=configuration(captures, corners="2"),
        option_captures=[captures["rounded_corner_prices"]],
        selected_option_ids=["100000104"],
        allow_additive=True,
    )
    assert row["subtotal"] == expected
    assert len(row["evidence_ids"]) == 2
    assert row["verification_status"] == "pending"
    assert row["production"]["status"] == "requires_configuration_quote"


def test_candidates_reject_unapproved_addition_missing_prices_and_invalid_domains(
    captures,
):
    kwargs = dict(
        quantity=50,
        currency="USD",
        allowed_quantities=domain(captures),
        configuration=configuration(captures, corners="2"),
        selected_option_ids=["100000104"],
    )
    with pytest.raises(TableSchemaError, match="opt-in"):
        reconstruct_candidate(
            captures["base_prices"],
            **kwargs,
            option_captures=[captures["rounded_corner_prices"]]
        )
    with pytest.raises(TableSchemaError, match="no captured"):
        reconstruct_candidate(captures["base_prices"], **kwargs, allow_additive=True)
    with pytest.raises(TableSchemaError, match="outside"):
        reconstruct_candidate(
            captures["base_prices"], **{**kwargs, "quantity": 25}, allow_additive=True
        )
    with pytest.raises(TableSchemaError, match="no captured"):
        reconstruct_candidate(
            captures["base_prices"],
            **{**kwargs, "selected_option_ids": ["2"]},
            option_captures=[captures["rounded_corner_prices"]],
            allow_additive=True
        )


def test_candidates_reject_stale_other_product_option_mapping(captures):
    other = deepcopy(captures["rounded_corner_prices"])
    other["url"] = other["url"].replace("/100000021/", "/100000001/")
    with pytest.raises(TableSchemaError, match="different base"):
        reconstruct_candidate(
            captures["base_prices"],
            quantity=50,
            currency="USD",
            allowed_quantities=domain(captures),
            configuration=configuration(captures),
            option_captures=[other],
        )


def test_price_parser_and_candidate_do_not_turn_null_into_zero(captures):
    missing = deepcopy(captures["base_prices"])
    missing["data"]["items"][0]["markupPrice"] = None
    assert parse_price_table(missing["data"])[50].markup_price is None
    with pytest.raises(TableSchemaError, match="missing"):
        reconstruct_candidate(
            missing,
            quantity=50,
            currency="USD",
            allowed_quantities=[50],
            configuration={},
        )


@pytest.mark.parametrize(
    "item",
    [
        {"quantity": 50, "markupPrice": 4.69},
        {"quantity": True, "markupPrice": "4.69"},
        {"quantity": 50, "markupPrice": "NaN"},
        {"quantity": 50, "markupPrice": "4.69", "discount": "new unsupported context"},
    ],
)
def test_unsupported_price_schema_fails_visibly(item):
    with pytest.raises(TableSchemaError):
        parse_price_table({"items": [item]})


def test_duplicate_quantities_are_not_silently_overwritten(captures):
    payload = deepcopy(captures["base_prices"]["data"])
    payload["items"].append({"quantity": 50, "markupPrice": "999.00"})
    with pytest.raises(TableSchemaError, match="Duplicate"):
        parse_price_table(payload)


def test_same_product_id_cannot_join_other_site_prices(captures):
    other = deepcopy(captures["rounded_corner_prices"])
    other["url"] = other["url"].replace("www.printograph.com", "other.example")
    with pytest.raises(TableSchemaError, match="different base"):
        reconstruct_candidate(
            captures["base_prices"],
            quantity=50,
            currency="USD",
            allowed_quantities=[50],
            configuration={},
            option_captures=[other],
        )


@pytest.fixture
def binding_fixture(captures):
    # Price/catalog and lookup bodies are captured public source data. Only
    # request-start provenance and this composed DOM context are synthesized;
    # these captures came from separate live observations.
    snapshot = deepcopy(captures["dom_quote"]["data"])
    snapshot.update(
        currency="USD",
        product_slug="business-cards",
        price="$4.69",
        unsupported=[],
        service_context=[],
        service_context_observed=True,
    )
    snapshot["controls"].append(
        {
            "key": "productionTime",
            "value": "1",
            "selected_label": "Regular 2–4 business days",
            "choices": [{"value": "1", "label": "Regular 2–4 business days"}],
        }
    )
    start = {control["key"]: control["value"] for control in snapshot["controls"]}
    selected = [
        deepcopy(captures[key])
        for key in (
            "base_prices",
            "quantity_support",
            "option_quantity_support",
            "option_catalog",
            "rounded_corner_prices",
        )
    ]
    selected.append(deepcopy(captures["product_lookup"]))
    for capture in selected:
        capture.update(
            status=200,
            navigation_generation=2,
            selection_epoch=3,
            request_controls=deepcopy(start),
            request_store_type="broker",
            request_product_type="1",
            request_service_context=canonical_service_context(snapshot),
        )
    return snapshot, selected


def bind_fixture(binding_fixture, **kwargs):
    from crawler.adapters.browser_tables import bind_business_card_tables

    snapshot, captures = binding_fixture
    return bind_business_card_tables(
        snapshot, captures, navigation_generation=2, selection_epoch=5, **kwargs
    )


def test_binder_resolves_exact_lookup_and_intersects_quantity_domains(binding_fixture):
    bundle = bind_fixture(binding_fixture)
    assert bundle["source_base_product_id"] == "100000021"
    assert (
        len(bundle["allowed_quantities"]) == 26
        and 25 not in bundle["allowed_quantities"]
    )
    assert bundle["selected_option_ids"] == []
    assert bundle["anchor_candidate"]["subtotal"] == "4.69"
    assert "c1ad2c5825774e31b3d8d9343c3b5b22" in bundle["evidence_ids"]
    assert bundle["binding"]["verification_status"] == "pending"
    assert len(bundle["binding"]["request_service_contexts"]) >= 3
    assert all(
        item["service_context"] == bundle["binding"]["service_context"]
        for item in bundle["binding"]["request_service_contexts"]
    )


@pytest.mark.parametrize(
    "change",
    [
        "legacy_snapshot",
        "unknown_capture",
        "manual_capture",
        "ambiguous_snapshot",
        "newer_conflicting_capture",
    ],
)
def test_service_scope_cannot_be_omitted_or_changed_in_bulk_binding(
    binding_fixture, change
):
    from test_service_context import service_observation

    snapshot, captures = binding_fixture
    if change == "legacy_snapshot":
        snapshot.pop("service_context_observed")
    elif change == "ambiguous_snapshot":
        snapshot.update(service_observation())
        snapshot["service_context"][0]["checked_count"] = 2
    elif change == "unknown_capture":
        captures[0].pop("request_service_context")
    elif change == "manual_capture":
        captures[0]["request_service_context"] = canonical_service_context(
            service_observation("100")
        )
    else:
        newer = deepcopy(captures[0])
        newer["selection_epoch"] = 4
        newer["request_service_context"] = canonical_service_context(
            service_observation("100")
        )
        captures.append(newer)
    with pytest.raises(TableSchemaError, match="service context"):
        bind_fixture(binding_fixture)


def test_explicit_absent_service_context_is_valid_for_roll_bulk(
    standard_binding_fixture,
):
    fixture = standard_binding_fixture("36_1")
    bundle = bind_fixture(fixture)
    assert all(
        value["status"] == "absent"
        for value in bundle["binding"]["service_context"]["groups"].values()
    )


def test_binder_uses_prefetched_corner_table_by_group_and_type(binding_fixture):
    snapshot, _ = binding_fixture
    control = next(
        control
        for control in snapshot["controls"]
        if control["key"] == "roundedCorners"
    )
    control.update(value="2", selected_label="Round Corners (+$3.00)")
    snapshot["price"] = "$7.69"
    bundle = bind_fixture(binding_fixture)
    assert bundle["selected_option_ids"] == ["100000104"]
    assert bundle["anchor_candidate"]["subtotal"] == "7.69"
    assert len(bundle["allowed_quantities"]) == 26


@pytest.mark.parametrize(
    "change",
    [
        "navigation",
        "future_epoch",
        "missing_context",
        "intermediate_base",
        "other_store",
    ],
)
def test_binder_rejects_unbound_stale_or_intermediate_base_capture(
    binding_fixture, change
):
    _, captures = binding_fixture
    base = captures[0]
    if change == "navigation":
        base["navigation_generation"] = 1
    elif change == "future_epoch":
        base["selection_epoch"] = 6
    elif change == "missing_context":
        base["request_controls"].pop("paper")
    elif change == "intermediate_base":
        base["request_controls"]["paper"] = "10"
    else:
        base["request_store_type"] = "retail"
    with pytest.raises(TableSchemaError, match="No configuration-bound"):
        bind_fixture(binding_fixture)


def test_binder_ignores_unrelated_later_capture_but_not_matching_price_conflicts(
    binding_fixture,
):
    _, captures = binding_fixture
    unrelated = deepcopy(captures[0])
    unrelated["selection_epoch"] = 5
    unrelated["request_controls"]["paper"] = "10"
    unrelated["data"]["items"][0]["markupPrice"] = "999.00"
    captures.append(unrelated)
    assert bind_fixture(binding_fixture)["anchor_candidate"]["subtotal"] == "4.69"
    conflicting = deepcopy(captures[0])
    conflicting["data"]["items"][0]["markupPrice"] = "999.00"
    captures.append(conflicting)
    with pytest.raises(TableSchemaError, match="Conflicting"):
        bind_fixture(binding_fixture)


def test_binder_does_not_fall_back_to_old_success_after_latest_failure(binding_fixture):
    _, captures = binding_fixture
    failed = deepcopy(captures[0])
    failed.update(status=503, selection_epoch=4)
    captures.append(failed)
    with pytest.raises(TableSchemaError, match="unsuccessful"):
        bind_fixture(binding_fixture)


@pytest.mark.parametrize(
    "change", ["multiple_ids", "wrong_id", "wrong_query", "duplicate_query"]
)
def test_binder_requires_exact_unambiguous_product_lookup(binding_fixture, change):
    _, captures = binding_fixture
    lookup = captures[-1]
    if change == "multiple_ids":
        lookup["data"]["items"].append({"id": 100000001})
    elif change == "wrong_id":
        lookup["data"]["items"][0]["id"] = 100000001
    elif change == "wrong_query":
        lookup["url"] = lookup["url"].replace("paper=1", "paper=10")
    else:
        lookup["url"] += "&paper=10"
    with pytest.raises(TableSchemaError):
        bind_fixture(binding_fixture)


def test_binder_rejects_wrong_endpoint_option_id_and_changed_semantics(binding_fixture):
    snapshot, captures = binding_fixture
    next(
        control
        for control in snapshot["controls"]
        if control["key"] == "roundedCorners"
    )["value"] = "2"
    snapshot["price"] = "$7.69"
    catalog = captures[3]["data"]["items"]
    next(option for option in catalog if option["name"] == "ROUND_CORNERS")[
        "id"
    ] = 100000004
    with pytest.raises(TableSchemaError, match="No configuration-bound"):
        bind_fixture(binding_fixture)


def test_binder_intersects_selected_option_price_domain_and_preserves_anchor(
    binding_fixture,
):
    snapshot, captures = binding_fixture
    next(
        control
        for control in snapshot["controls"]
        if control["key"] == "roundedCorners"
    )["value"] = "2"
    snapshot["price"] = "$7.69"
    captures[4]["data"]["items"] = [
        item
        for item in captures[4]["data"]["items"]
        if item["quantity"] in (25, 50, 250)
    ]
    assert bind_fixture(binding_fixture)["allowed_quantities"] == [50, 250]
    bundle = bind_fixture(binding_fixture, filters={"quantity": [250]})
    assert bundle["allowed_quantities"] == [250]
    assert bundle["anchor_candidate"]["quantity"] == 50


def test_binder_rejects_failed_anchor_unknown_control_and_unbound_rush(binding_fixture):
    snapshot, _ = binding_fixture
    snapshot["price"] = "$4.70"
    with pytest.raises(TableSchemaError, match="subtotal"):
        bind_fixture(binding_fixture)
    snapshot["price"] = "$4.69"
    snapshot["controls"].append({"key": "unsupported_finish", "value": "yes"})
    with pytest.raises(TableSchemaError, match="complete standard print controls"):
        bind_fixture(binding_fixture)
    snapshot["controls"].pop()
    next(
        control
        for control in snapshot["controls"]
        if control["key"] == "productionTime"
    )["value"] = "3"
    with pytest.raises(TableSchemaError, match="No configuration-bound"):
        bind_fixture(binding_fixture)


def test_saved_live_lookup_confirms_schema_mapping_and_raw_byte_hash(captures):
    import hashlib
    from urllib.parse import parse_qs, urlsplit

    lookup = captures["product_lookup"]
    assert lookup["evidence_id"] == "c1ad2c5825774e31b3d8d9343c3b5b22"
    assert lookup["data"] == {"items": [{"id": 100000021}]}
    assert parse_qs(urlsplit(lookup["url"]).query) == {
        "productType": ["1"],
        "turnaround": ["1"],
        "size": ["101"],
        "orientation": ["1"],
        "paper": ["1"],
        "color": ["2"],
    }
    observed_raw_body = b'{"items":[{"id":100000021}]}'
    assert hashlib.sha256(observed_raw_body).hexdigest() == lookup["raw_sha256"]
    assert (
        lookup["raw_sha256"]
        == "9e0fe66dde4add2abb75b7f0b50319c38bad250ed579ccd8a589deafe11c8700"
    )
    assert lookup["recorded_response_hash"] != lookup["raw_sha256"]


def test_prefetched_tables_bind_by_route_dependencies_before_quantity_and_production(
    binding_fixture,
):
    _, captures = binding_fixture
    for capture in captures:
        capture["request_controls"] = {
            key: capture["request_controls"][key]
            for key in ("size", "paper", "color", "orientation")
        }
        capture["request_controls"]["quantity"] = ""
    bundle = bind_fixture(binding_fixture)
    assert bundle["anchor_candidate"]["subtotal"] == "4.69"
    assert len(bundle["allowed_quantities"]) == 26


@pytest.mark.parametrize(
    "key,value",
    [
        ("paper", "10"),
        ("coating", "9"),
        ("productionTime", "3"),
        ("roundedCorners", "7"),
    ],
)
def test_prefetch_never_overrides_explicit_conflicting_selections(
    binding_fixture, key, value
):
    _, captures = binding_fixture
    captures[0]["request_controls"][key] = value
    with pytest.raises(TableSchemaError, match="No configuration-bound"):
        bind_fixture(binding_fixture)


@pytest.mark.parametrize(
    "query",
    [
        "?itemId=null&cid=customer-account",
        "?unknownPriceContext=true",
        "?cid=&cid=customer-account",
    ],
)
def test_later_unknown_account_or_query_context_cannot_revive_older_base_prices(
    binding_fixture, query
):
    _, captures = binding_fixture
    changed = deepcopy(captures[0])
    changed["selection_epoch"] = 4
    changed["url"] = changed["url"].split("?")[0] + query
    captures.append(changed)
    with pytest.raises(TableSchemaError, match="query context"):
        bind_fixture(binding_fixture)


def test_matching_scoped_quantity_response_can_coexist_but_changed_price_cannot(
    binding_fixture,
):
    _, captures = binding_fixture
    scoped = deepcopy(captures[0])
    scoped["url"] += "&quantities=50"
    scoped["data"]["items"] = [scoped["data"]["items"][0]]
    scoped["selection_epoch"] = 4
    captures.append(scoped)
    assert bind_fixture(binding_fixture)["anchor_candidate"]["subtotal"] == "4.69"
    scoped["data"]["items"][0]["markupPrice"] = "4.70"
    with pytest.raises(TableSchemaError, match="Scoped source prices disagree"):
        bind_fixture(binding_fixture)


def test_newer_empty_scoped_price_response_does_not_revive_full_table(binding_fixture):
    _, captures = binding_fixture
    scoped = deepcopy(captures[0])
    scoped["url"] += "&quantities=50"
    scoped["data"]["items"] = []
    scoped["selection_epoch"] = 4
    captures.append(scoped)
    with pytest.raises(TableSchemaError, match="every requested quantity"):
        bind_fixture(binding_fixture)


@pytest.mark.parametrize(
    "value",
    [
        "1,23.45",
        "12,34,567.89",
        "1.234,56",
        " 1,234.56",
        "1e3",
        "1,234.",
        ",123",
        "1,,234",
    ],
)
def test_price_parser_rejects_malformed_or_ambiguous_grouping(value):
    with pytest.raises(TableSchemaError, match="decimal"):
        parse_price_table({"items": [{"quantity": 50, "markupPrice": value}]})


def test_price_parser_accepts_captured_us_grouped_amounts_exactly():
    table = parse_price_table(
        {
            "items": [
                {
                    "quantity": 100000,
                    "markupPrice": "1,062.49",
                    "listPrice": "12,345,678.90",
                }
            ]
        }
    )
    assert table[100000].markup_price == Decimal("1062.49")
    assert table[100000].list_price == Decimal("12345678.90")


@pytest.fixture
def standard_binding_fixture(binding_fixture):
    def build(branch):
        source = json.loads(
            (
                Path(__file__).parent / "fixtures" / "printograph_standard_tables.json"
            ).read_text()
        )["branches"][branch]
        snapshot = deepcopy(source.get("dom_quote", binding_fixture[0]))
        snapshot["product_type"] = source["base_selection"]["productType"]
        snapshot["product_slug"] = {
            "1": "business-cards",
            "27": "club-flyers",
            "36": "roll-labels",
        }[snapshot["product_type"]]
        snapshot.update(
            currency="USD",
            store_type="broker",
            unsupported=[],
            service_context=[],
            service_context_observed=True,
        )
        for control in snapshot["controls"]:
            if control["key"] in source["base_selection"]:
                control["value"] = source["base_selection"][control["key"]]
                control["selected_label"] = control["value"]
        snapshot["price"] = (
            "$" + source["captures"]["base_prices"]["data"]["items"][0]["markupPrice"]
        )
        start = {control["key"]: control["value"] for control in snapshot["controls"]}
        captures = list(deepcopy(source["captures"]).values())
        for capture in captures:
            capture.update(
                status=200,
                navigation_generation=2,
                selection_epoch=3,
                request_controls=deepcopy(start),
                request_store_type="broker",
                request_product_type=snapshot["product_type"],
                request_service_context=canonical_service_context(snapshot),
            )
        return snapshot, captures

    return build


@pytest.mark.parametrize("branch", ["1_10", "27_10"])
@pytest.mark.parametrize("removed", [set(), {"spotcoating"}, {"raisedprint"}])
def test_actual_inactive_finishes_preserve_none_without_option_charge(
    standard_binding_fixture,
    branch,
    removed,
):
    snapshot, captures = standard_binding_fixture(branch)
    snapshot["price"] = snapshot["subtotal_text"]
    snapshot["controls"] = [
        control for control in snapshot["controls"] if control["key"] not in removed
    ]
    for capture in captures:
        for key in removed:
            capture["request_controls"].pop(key)
    bundle = bind_fixture((snapshot, captures))
    candidate = bundle["anchor_candidate"]
    for key in {"spotcoating", "raisedprint"} - removed:
        assert candidate["configuration"][key] == {"value": "", "label": "None"}
        assert key in bundle["binding"]["finishing_controls_present"]
    for key in removed:
        assert key not in candidate["configuration"]
    assert candidate["subtotal"] == snapshot["subtotal_text"].lstrip("$")
    assert candidate["verification_status"] == "pending"
    assert bundle["selected_option_ids"] == []
    assert bundle["option_captures"] == []
    assert candidate["table_derivation"]["formula"] == "base_only"


@pytest.mark.parametrize(
    "change",
    [
        "nonempty",
        "null",
        "kind",
        "label",
        "choice_label",
        "missing_choice",
        "duplicate_choice",
        "disabled",
        "placeholder",
        "request_nonempty",
        "request_absent",
        "final_absent",
        "unknown_alias",
        "proof_residual",
    ],
)
def test_inactive_finish_acceptance_does_not_infer_selection_or_fees(
    standard_binding_fixture,
    change,
):
    snapshot, captures = standard_binding_fixture("27_10")
    snapshot["price"] = snapshot["subtotal_text"]
    control = next(
        control for control in snapshot["controls"] if control["key"] == "raisedprint"
    )
    sentinel = next(choice for choice in control["choices"] if choice["value"] == "")
    if change == "nonempty":
        control["value"] = "135"
    elif change == "null":
        control["value"] = None
    elif change == "kind":
        control["kind"] = "radio"
    elif change == "label":
        control["selected_label"] = "Choose an option"
    elif change == "choice_label":
        sentinel["label"] = "None (+$3.00)"
    elif change == "missing_choice":
        control["choices"].remove(sentinel)
    elif change == "duplicate_choice":
        control["choices"].append(deepcopy(sentinel))
    elif change in {"disabled", "placeholder"}:
        sentinel[change] = True
    elif change == "request_nonempty":
        for capture in captures:
            capture["request_controls"]["raisedprint"] = "135"
    elif change == "request_absent":
        for capture in captures:
            capture["request_controls"].pop("raisedprint")
    elif change == "final_absent":
        snapshot["controls"].remove(control)
    elif change == "unknown_alias":
        control["key"] = "spotuv"
    else:
        snapshot["price"] = "$" + str(
            Decimal(snapshot["subtotal_text"].lstrip("$")) + Decimal("3.00")
        )
    with pytest.raises(TableSchemaError):
        bind_fixture((snapshot, captures))


def test_actual_matte_aqueous_is_an_explicit_included_baseline_candidate(
    standard_binding_fixture,
):
    fixture = standard_binding_fixture("1_10_matte")
    fixture[0]["price"] = fixture[0]["subtotal_text"]
    bundle = bind_fixture(fixture)
    assert bundle["anchor_candidate"]["subtotal"] == "15.40"
    assert bundle["anchor_candidate"]["configuration"]["coating"] == {
        "value": "",
        "label": "Matte Aqueous Coating",
    }
    assert bundle["anchor_candidate"]["verification_status"] == "pending"
    assert bundle["selected_option_ids"] == []
    assert bundle["option_captures"] == []
    assert bundle["binding"]["baseline_option_types"] == []
    assert bundle["binding"]["included_coating"]["price_basis"] == "baseline_candidate"


@pytest.mark.parametrize(
    "change",
    [
        "label",
        "mismatch_label",
        "gloss",
        "none",
        "duplicate",
        "disabled",
        "placeholder",
        "missing_choice",
        "unknown_value",
        "request_nonempty",
        "request_absent",
        "residual",
    ],
)
def test_included_matte_coating_rejects_ambiguous_or_unexplained_context(
    standard_binding_fixture, change
):
    fixture = standard_binding_fixture("1_10_matte")
    snapshot, captures = fixture
    snapshot["price"] = snapshot["subtotal_text"]
    coating = next(
        control for control in snapshot["controls"] if control["key"] == "coating"
    )
    sentinel = next(choice for choice in coating["choices"] if choice["value"] == "")
    if change == "label":
        coating["selected_label"] = "Unknown Coating"
    elif change == "mismatch_label":
        sentinel["label"] = "None"
    elif change in {"gloss", "none"}:
        coating["selected_label"] = sentinel["label"] = (
            "Gloss Aqueous Coating" if change == "gloss" else "None"
        )
    elif change == "duplicate":
        coating["choices"].append(deepcopy(sentinel))
    elif change in {"disabled", "placeholder"}:
        sentinel[change] = True
    elif change == "missing_choice":
        coating["choices"].remove(sentinel)
    elif change == "unknown_value":
        coating["value"] = "999"
    elif change == "request_nonempty":
        for capture in captures:
            capture["request_controls"]["coating"] = "5"
    elif change == "request_absent":
        for capture in captures:
            capture["request_controls"].pop("coating")
    else:
        snapshot["price"] = "$18.40"
    with pytest.raises(TableSchemaError):
        bind_fixture(fixture)


@pytest.mark.parametrize(
    "branch,product_id,first,last",
    [
        ("1_1", "100000001", "4.20", "632.03"),
        ("27_1", "100125936", "3.30", "1062.49"),
        ("27_2", "100125976", "4.29", "1130.69"),
    ],
)
def test_actual_standard_tables_bind_dynamic_base_without_copying_other_prices(
    standard_binding_fixture, branch, product_id, first, last
):
    fixture = standard_binding_fixture(branch)
    bundle = bind_fixture(fixture)
    assert bundle["source_base_product_id"] == product_id
    assert bundle["anchor_candidate"]["subtotal"] == first
    assert bundle["allowed_quantities"] == [
        50,
        100,
        250,
        500,
        1000,
        2500,
        5000,
        10000,
        15000,
        20000,
        25000,
        30000,
        35000,
        40000,
        45000,
        50000,
        55000,
        60000,
        65000,
        70000,
        75000,
        80000,
        85000,
        90000,
        95000,
        100000,
    ]
    final = reconstruct_candidate(
        bundle["base_capture"],
        quantity=100000,
        currency="USD",
        allowed_quantities=bundle["allowed_quantities"],
        configuration={},
    )
    assert final["subtotal"] == last
    assert final["verification_status"] == "pending"
    assert final["production"] == {"status": "requires_configuration_quote"}


def test_actual_club_corner_type_resolves_only_within_its_own_catalog(
    standard_binding_fixture,
):
    fixture = standard_binding_fixture("27_1")
    snapshot, _ = fixture
    next(c for c in snapshot["controls"] if c["key"] == "roundedCorners")["value"] = "2"
    # This is a synthetic effective DOM anchor, not a claim of a live sample.
    snapshot["price"] = "$6.30"
    bundle = bind_fixture(fixture)
    assert bundle["selected_option_ids"] == ["101265128"]
    assert bundle["anchor_candidate"]["subtotal"] == "6.30"
    assert bundle["binding"]["production_mode"] == "1"


def test_rush_needs_exact_lookup_context_and_intersection(binding_fixture):
    snapshot, captures = binding_fixture
    next(c for c in snapshot["controls"] if c["key"] == "productionTime")["value"] = "3"
    for capture in captures:
        capture["request_controls"]["productionTime"] = "3"
    # Regular lookup cannot be reused merely because a regular table matches.
    with pytest.raises(TableSchemaError, match="No configuration-bound"):
        bind_fixture(binding_fixture)
    lookup = next(c for c in captures if "/products?" in c["url"])
    lookup["url"] = lookup["url"].replace("turnaround=1", "turnaround=3")
    # Synthetic endpoint contract test. Actual Rush base IDs must be supplied by
    # the source lookup in a real browser pass, never inferred from Regular.
    bundle = bind_fixture(binding_fixture)
    assert bundle["allowed_quantities"] == [50, 100, 250, 500, 1000, 2500]
    assert bundle["binding"]["production_mode"] == "3"
    lookup["data"]["items"][0]["id"] = 100000999
    with pytest.raises(TableSchemaError, match="No configuration-bound"):
        bind_fixture(binding_fixture)


@pytest.mark.parametrize(
    "change", ["roll", "slug", "coating", "unknown_control", "hidden_process"]
)
def test_generalized_binder_keeps_unmodeled_context_out_of_bulk(
    standard_binding_fixture, change
):
    fixture = standard_binding_fixture("27_1")
    snapshot, _ = fixture
    if change == "roll":
        snapshot["product_type"] = "36"
    elif change == "slug":
        snapshot["product_slug"] = "business-cards"
    elif change == "coating":
        next(c for c in snapshot["controls"] if c["key"] == "coating")["value"] = "9"
    elif change == "unknown_control":
        snapshot["controls"].append({"key": "process", "value": "offset"})
    else:
        snapshot["unsupported"] = [{"reason": "hidden_process"}]
    with pytest.raises(TableSchemaError):
        bind_fixture(fixture)


def test_actual_roll_tables_bind_preset_and_only_product_quantity_domain(
    standard_binding_fixture,
):
    fixture = standard_binding_fixture("36_1")
    bundle = bind_fixture(fixture)
    assert bundle["source_base_product_id"] == "100134417"
    assert bundle["anchor_candidate"]["subtotal"] == "66.23"
    assert bundle["allowed_quantities"] == [
        100,
        250,
        500,
        1000,
        2500,
        5000,
        10000,
        15000,
        20000,
        25000,
        30000,
        35000,
        40000,
        45000,
        50000,
    ]
    assert bundle["binding"]["baseline_option_types"] == ["32"]
    assert bundle["option_captures"] == []
    assert bundle["anchor_candidate"]["production"] == {
        "status": "requires_configuration_quote"
    }
    assert "roll_count" not in bundle["anchor_candidate"]
    assert bundle["binding"]["independent_cart_verification"] is False


def test_square_roll_derives_query_orientation_without_inventing_control(
    standard_binding_fixture,
):
    fixture = standard_binding_fixture("36_square")
    snapshot, captures = fixture
    spec = next(c for c in captures if "/specifications?" in c["url"])
    # The page loads these product-wide settings before the user reaches Square.
    spec["request_controls"] = {"shape": "5", "size": "742", "orientation": "2"}
    bundle = bind_fixture(fixture)
    assert bundle["source_base_product_id"] == "100134721"
    assert bundle["anchor_candidate"]["subtotal"] == "66.41"
    assert bundle["binding"]["base_selection"]["orientation"] == "1"
    assert bundle["binding"]["orientation_source"]["evidence_id"] == spec["evidence_id"]
    assert spec["evidence_id"] in bundle["evidence_ids"]
    assert "orientation" not in bundle["anchor_candidate"]["configuration"]
    assert all(control["key"] != "orientation" for control in snapshot["controls"])
    assert len(bundle["allowed_quantities"]) == 15


@pytest.mark.parametrize(
    "product,store", [(None, None), ("", ""), ("36", None), (None, "broker")]
)
def test_square_specification_can_precede_product_metadata(
    standard_binding_fixture,
    product,
    store,
):
    fixture = standard_binding_fixture("36_square")
    spec = next(
        capture for capture in fixture[1] if "/specifications?" in capture["url"]
    )
    spec.update(
        request_product_type=product, request_store_type=store, request_controls={}
    )
    bundle = bind_fixture(fixture)
    assert bundle["source_base_product_id"] == "100134721"
    assert spec["evidence_id"] in bundle["evidence_ids"]
    assert "orientation" not in bundle["anchor_candidate"]["configuration"]


@pytest.mark.parametrize(
    "change",
    [
        "price_product",
        "price_store",
        "later_product_conflict",
        "later_store_conflict",
        "extra_query",
        "duplicate_query",
        "final_store",
    ],
)
def test_early_square_spec_exception_never_weakens_price_or_context_binding(
    standard_binding_fixture,
    change,
):
    fixture = standard_binding_fixture("36_square")
    spec = next(
        capture for capture in fixture[1] if "/specifications?" in capture["url"]
    )
    spec.update(request_product_type=None, request_store_type=None, request_controls={})
    if change in {"price_product", "price_store"}:
        price = next(
            capture for capture in fixture[1] if "/100134721/prices?" in capture["url"]
        )
        price[
            (
                "request_product_type"
                if change == "price_product"
                else "request_store_type"
            )
        ] = None
    elif change.startswith("later_"):
        conflict = deepcopy(spec)
        conflict[
            (
                "request_product_type"
                if change == "later_product_conflict"
                else "request_store_type"
            )
        ] = "other"
        fixture[1].append(conflict)
    elif change == "extra_query":
        spec["url"] += "&cid=other"
    elif change == "duplicate_query":
        spec["url"] += "&productType=36"
    else:
        fixture[0]["store_type"] = "retail"
    with pytest.raises(TableSchemaError):
        bind_fixture(fixture)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "navigation",
        "future",
        "product_type",
        "store",
        "origin",
        "query",
        "duplicate_query",
        "failed",
        "evidence",
        "missing_size",
        "duplicate_size",
        "shape",
        "rectangle",
        "zero",
        "nonfinite",
        "missing_orientation",
        "conflicting",
        "explicit_wrong_orientation",
    ],
)
def test_square_roll_fails_closed_without_exact_specification_binding(
    standard_binding_fixture,
    change,
):
    fixture = standard_binding_fixture("36_square")
    _, captures = fixture
    spec = next(c for c in captures if "/specifications?" in c["url"])
    size = next(item for item in spec["data"]["sizes"] if item["id"] == 448)
    if change == "missing":
        captures.remove(spec)
    elif change == "navigation":
        spec["navigation_generation"] -= 1
    elif change == "future":
        spec["selection_epoch"] = 100
    elif change == "product_type":
        spec["request_product_type"] = "1"
    elif change == "store":
        spec["request_store_type"] = "retail"
    elif change == "origin":
        spec["url"] = spec["url"].replace("www.printograph.com", "printograph.com")
    elif change == "query":
        spec["url"] += "&cid=other-account"
    elif change == "duplicate_query":
        spec["url"] += "&productType=36"
    elif change == "failed":
        spec["status"] = 500
    elif change == "evidence":
        spec["evidence_id"] = ""
    elif change == "missing_size":
        spec["data"]["sizes"].remove(size)
    elif change == "duplicate_size":
        spec["data"]["sizes"].append(deepcopy(size))
    elif change == "shape":
        size["properties"]["shape"] = 5
    elif change == "rectangle":
        size["properties"]["height"] = 2
    elif change == "zero":
        size["properties"].update(width=0, height=0)
    elif change == "nonfinite":
        size["properties"]["width"] = "Infinity"
    elif change == "missing_orientation":
        size.pop("orientation")
    elif change == "conflicting":
        conflict = deepcopy(spec)
        conflict["data"]["sizes"].append({"id": 999999})
        captures.append(conflict)
    else:
        for capture in captures:
            if capture is not spec:
                capture["request_controls"]["orientation"] = "2"
    with pytest.raises(TableSchemaError):
        bind_fixture(fixture)


def test_unselected_custom_roll_size_retains_finite_preset_eligibility(
    standard_binding_fixture,
):
    fixture = standard_binding_fixture("36_1")
    fixture[0]["unsupported"] = ["custom_values:size"]
    assert bind_fixture(fixture)["allowed_quantities"][0] == 100
    size = next(c for c in fixture[0]["controls"] if c["key"] == "size")
    size["value"] = "custom"
    with pytest.raises(TableSchemaError):
        bind_fixture(fixture)
    size["value"] = "999999"
    with pytest.raises(TableSchemaError, match="finite size preset"):
        bind_fixture(fixture)


@pytest.mark.parametrize(
    "change",
    [
        "finish",
        "shape",
        "wind_name",
        "wind_group",
        "wind_support",
        "wind_price",
        "rush",
        "unknown_custom",
    ],
)
def test_roll_bulk_does_not_guess_changed_base_wind_price_or_custom_context(
    standard_binding_fixture, change
):
    fixture = standard_binding_fixture("36_1")
    snapshot, captures = fixture
    if change in {"finish", "shape"}:
        # Lookup still refers to the captured original choice; no ID derivation.
        next(c for c in snapshot["controls"] if c["key"] == change)["value"] = "2"
    elif change == "wind_name":
        next(
            o
            for c in captures
            if "/options?" in c["url"]
            for o in c["data"]["items"]
            if o["type"]["id"] == 32
        )["name"] = "OTHER_SEMANTICS"
    elif change == "wind_group":
        next(
            o
            for c in captures
            if "/options?" in c["url"]
            for o in c["data"]["items"]
            if o["type"]["id"] == 32
        )["group"]["id"] = 99
    elif change == "wind_support":
        next(c for c in captures if "/options/quantities?" in c["url"])["data"].pop(
            "32"
        )
    elif change == "wind_price":
        snapshot["price"] = "$67.23"
    elif change == "rush":
        next(c for c in snapshot["controls"] if c["key"] == "productionTime")[
            "value"
        ] = "3"
    else:
        snapshot["unsupported"] = ["custom_values:quantity"]
    with pytest.raises(TableSchemaError):
        bind_fixture(fixture)


def test_all_turnaround_support_can_be_prefetched_under_prior_rush(
    standard_binding_fixture,
):
    fixture = standard_binding_fixture("27_2")
    snapshot, captures = fixture
    for capture in captures:
        if (
            "/products/quantities?" in capture["url"]
            or "/products/options/quantities?" in capture["url"]
        ):
            capture["request_controls"]["productionTime"] = "3"
    bundle = bind_fixture(fixture)
    assert bundle["source_base_product_id"] == "100125976"
    assert bundle["binding"]["production_mode"] == "1"
    assert len(bundle["allowed_quantities"]) == 26
    assert bundle["anchor_candidate"]["subtotal"] == "4.29"


@pytest.mark.parametrize(
    "change", ["base", "query", "missing_all_turnarounds", "price_context"]
)
def test_mode_independent_support_never_weakens_base_query_or_price_binding(
    standard_binding_fixture, change
):
    fixture = standard_binding_fixture("27_2")
    _, captures = fixture
    support = next(c for c in captures if "/products/quantities?" in c["url"])
    support["request_controls"]["productionTime"] = "3"
    if change == "base":
        support["request_controls"]["color"] = "1"
    elif change == "query":
        support["url"] = support["url"].replace("color=2", "color=1")
    elif change == "missing_all_turnarounds":
        support["url"] = support["url"].replace(
            "includeTurnaround=true", "includeTurnaround=false"
        )
    else:
        next(c for c in captures if "/100125976/prices?" in c["url"])[
            "request_controls"
        ]["productionTime"] = "3"
    with pytest.raises(TableSchemaError, match="No configuration-bound"):
        bind_fixture(fixture)


def remove_finish_controls(fixture, keys):
    snapshot, captures = fixture
    snapshot["controls"] = [c for c in snapshot["controls"] if c["key"] not in keys]
    for capture in captures:
        for key in keys:
            capture["request_controls"].pop(key, None)
    return fixture


@pytest.mark.parametrize(
    "missing", [{"coating"}, {"roundedCorners"}, {"coating", "roundedCorners"}]
)
def test_absent_finishing_controls_are_preserved_without_default_values(
    standard_binding_fixture, missing
):
    fixture = remove_finish_controls(standard_binding_fixture("27_1"), missing)
    if "coating" in missing:
        # No selected finishing type remains. The page may omit these requests.
        fixture[1][:] = [c for c in fixture[1] if "/options" not in c["url"]]
    bundle = bind_fixture(fixture)
    assert bundle["anchor_candidate"]["subtotal"] == "3.30"
    assert not missing & set(bundle["anchor_candidate"]["configuration"])
    assert bundle["binding"]["finishing_controls_present"] == sorted(
        {"coating", "roundedCorners"} - missing
    )
    assert len(bundle["allowed_quantities"]) == 26
    assert bundle["selected_option_ids"] == []
    assert bundle["binding"]["baseline_option_types"] == (
        [] if "coating" in missing else ["5"]
    )


def test_corner_charge_remains_explicit_when_coating_control_is_absent(
    standard_binding_fixture,
):
    fixture = remove_finish_controls(standard_binding_fixture("27_1"), {"coating"})
    next(c for c in fixture[0]["controls"] if c["key"] == "roundedCorners")[
        "value"
    ] = "2"
    fixture[0]["price"] = "$6.30"
    for c in fixture[1]:
        if "/options?" in c["url"]:
            c["data"]["items"] = [o for o in c["data"]["items"] if o["type"]["id"] != 5]
        elif "/options/quantities?" in c["url"]:
            c["url"] = c["url"].replace("optionIds=2,5", "optionIds=2")
            c["data"].pop("5")
    bundle = bind_fixture(fixture)
    assert bundle["selected_option_ids"] == ["101265128"]
    assert bundle["anchor_candidate"]["subtotal"] == "6.30"
    assert "coating" not in bundle["anchor_candidate"]["configuration"]


@pytest.mark.parametrize(
    "change",
    [
        "unknown_coating",
        "unexplained_charge",
        "wrong_base",
        "wrong_lookup",
        "wrong_navigation",
        "requested_absent",
    ],
)
def test_absent_finish_extension_keeps_price_and_source_guards(
    standard_binding_fixture, change
):
    fixture = remove_finish_controls(
        standard_binding_fixture("27_1"), {"coating", "roundedCorners"}
    )
    snapshot, captures = fixture
    kwargs = {}
    if change == "unknown_coating":
        snapshot["controls"].append({"key": "coating", "value": "4"})
    elif change == "unexplained_charge":
        snapshot["price"] = "$4.30"
    elif change == "wrong_base":
        next(c for c in captures if "/100125936/prices?" in c["url"])[
            "request_controls"
        ]["paper"] = "2"
    elif change == "wrong_lookup":
        next(c for c in captures if "/products?" in c["url"])["url"] += "&unexpected=1"
    elif change == "wrong_navigation":
        next(c for c in captures if "/100125936/prices?" in c["url"])[
            "navigation_generation"
        ] = 1
    else:
        kwargs["filters"] = {"coating": "5"}
    with pytest.raises(TableSchemaError):
        bind_fixture(fixture, **kwargs)


def test_absent_and_present_empty_corner_are_distinct_configurations(
    standard_binding_fixture,
):
    absent = remove_finish_controls(
        standard_binding_fixture("27_1"), {"roundedCorners"}
    )
    present = standard_binding_fixture("27_1")
    left, right = bind_fixture(absent), bind_fixture(present)
    assert "roundedCorners" not in left["anchor_candidate"]["configuration"]
    assert right["anchor_candidate"]["configuration"]["roundedCorners"]["value"] == ""
    assert (
        left["binding"]["finishing_controls_present"]
        != right["binding"]["finishing_controls_present"]
    )


@pytest.mark.parametrize("branch", ["1_3", "27_3"])
def test_actual_color_both_support_accepts_unselected_catalog_type_40(
    standard_binding_fixture, branch
):
    fixture = standard_binding_fixture(branch)
    bundle = bind_fixture(fixture)
    assert bundle["binding"]["baseline_option_types"] == ["5"]
    assert len(bundle["allowed_quantities"]) == 26
    assert bundle["selected_option_ids"] == []
    # Extra unselected type40 can have narrower support without restricting5.
    next(c for c in fixture[1] if "/options/quantities?" in c["url"])["data"]["40"] = []
    assert len(bind_fixture(fixture)["allowed_quantities"]) == 26


@pytest.mark.parametrize("branch", ["1_3", "27_3"])
def test_type40_coating_maps_to_exact_bound_catalog_and_selected_support(
    standard_binding_fixture, branch
):
    fixture = standard_binding_fixture(branch)
    next(c for c in fixture[0]["controls"] if c["key"] == "coating")["value"] = "40"
    # Captures prefetched under5 remain base-identical. This composed anchor
    # checks the candidate contract; live publication still requires two DOM samples.
    bundle = bind_fixture(fixture)
    assert bundle["binding"]["baseline_option_types"] == ["40"]
    assert bundle["anchor_candidate"]["configuration"]["coating"]["value"] == "40"
    assert bundle["anchor_candidate"]["verification_status"] == "pending"
    support = next(c for c in fixture[1] if "/options/quantities?" in c["url"])
    support["data"]["40"] = [
        x for x in support["data"]["40"] if x["quantity"] in {50, 100}
    ]
    assert bind_fixture(fixture)["allowed_quantities"] == [50, 100]


@pytest.mark.parametrize(
    "change",
    [
        "uncataloged_extra",
        "wrong_name",
        "wrong_group",
        "unexplained_charge",
        "missing_selected_support",
    ],
)
def test_coating40_never_infers_unknown_mapping_or_fee(
    standard_binding_fixture, change
):
    fixture = standard_binding_fixture("27_3")
    snapshot, captures = fixture
    next(c for c in snapshot["controls"] if c["key"] == "coating")["value"] = "40"
    catalog = next(c for c in captures if "/options?" in c["url"])
    coating = next(x for x in catalog["data"]["items"] if x["type"]["id"] == 40)
    support = next(c for c in captures if "/options/quantities?" in c["url"])
    if change == "uncataloged_extra":
        support["url"] = support["url"].replace(
            "optionIds=2,5,40", "optionIds=2,5,40,999"
        )
    elif change == "wrong_name":
        coating["name"] = "UNPROVEN_FINISH"
    elif change == "wrong_group":
        coating["group"]["id"] = 999
    elif change == "unexplained_charge":
        snapshot["price"] = "$999.00"
    else:
        support["data"].pop("40")
    with pytest.raises(TableSchemaError):
        bind_fixture(fixture)


def test_future_table_candidate_keeps_anchor_label_as_source_text_only(captures):
    config = configuration(captures, corners="2")
    config["roundedCorners"]["label"] = "Round Corners (+$3.00 )"
    row = reconstruct_candidate(
        captures["base_prices"],
        quantity=250,
        currency="USD",
        allowed_quantities=domain(captures),
        configuration=config,
        option_captures=[captures["rounded_corner_prices"]],
        selected_option_ids=["100000104"],
        allow_additive=True,
    )
    assert row["configuration"]["roundedCorners"] == {
        "value": "2",
        "label": "Round Corners",
        "source_label": "Round Corners (+$3.00 )",
    }
    assert config["roundedCorners"]["label"] == "Round Corners (+$3.00 )"
    assert row["subtotal"] == "14.45"
    assert row["table_derivation"]["components"][1]["markup_price"] == "5.00"
