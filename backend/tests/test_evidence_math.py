from copy import deepcopy
from decimal import Decimal
import json
import sqlite3

import pytest

from crawler.evidence_math import (
    AUDITED_ORDER_FORM_SHA256,
    Evidence,
    SavedEvidence,
    audit_quote,
    quote_amount,
    render_markdown,
    load_price_contract,
)
from test_service_context import service_observation

ORIGIN = "https://www.printograph.com"


def fixture(*, total="5.60", corner=None, services=None):
    controls = [
        {"key": key, "value": value, "selected_label": value}
        for key, value in {
            "size": "101",
            "paper": "10",
            "color": "1",
            "orientation": "1",
            "productionTime": "1",
            "quantity": "50",
        }.items()
    ]
    if corner is not None:
        controls.append(
            {
                "key": "roundedCorners",
                "value": corner,
                "selected_label": "None" if corner == "" else "Round Corners",
            }
        )
    quote = Evidence(
        "dom",
        ORIGIN + "/products/business-cards/order",
        10,
        200,
        {
            "kind": "visible_product_quote",
            "product_type": "1",
            "store_type": "broker",
            "controls": controls,
            "subtotal_text": "$" + total,
            **(services or {"service_context": [], "service_context_observed": True}),
        },
    )
    captures = [
        Evidence(
            "lookup",
            ORIGIN
            + "/service/rest/v1/products?productType=1&size=101&paper=10&color=1&orientation=1&turnaround=1",
            1,
            200,
            {"items": [{"id": 100000009}]},
        ),
        Evidence(
            "base",
            ORIGIN + "/service/rest/v1/products/100000009/prices?itemId=null&cid=",
            2,
            200,
            {"items": [{"quantity": 50, "markupPrice": "5.60"}]},
        ),
    ]
    return quote, captures


def catalog_option(identifier, type_id, group, *, name="OPTION"):
    return {
        "id": identifier,
        "name": name,
        "quantityDependentPrice": False,
        "type": {"id": type_id, "label": name},
        "group": {"id": group, "label": "Group"},
    }


def add_catalog(captures, items):
    captures.append(
        Evidence(
            "catalog",
            ORIGIN
            + "/service/rest/v1/products/100000009/options?requestValues=true&eddm=false",
            3,
            200,
            {"items": items},
        )
    )


def audit(quote, captures):
    return audit_quote(
        quote, captures, linked_evidence_ids=[item.id for item in captures]
    )


def test_exact_base_with_observed_absence_and_explicit_none_reconciles():
    quote, captures = fixture(corner="")
    result = audit(quote, captures)
    assert result["status"] == "pass"
    assert (
        result["known_component_subtotal"] == "5.60"
        and result["unexplained_residual"] == "0.00"
    )
    assert result["reasons"] == []
    assert result["evidence_ids"] == ["base", "dom", "lookup"]


def test_exact_selected_option_and_explicit_zero_proof_price_are_auditable():
    quote, captures = fixture(
        total="8.60", corner="2", services=service_observation("31", delivery=False)
    )
    add_catalog(captures, [catalog_option(8, 2, 2), catalog_option(9, 31, 1)])
    for identifier, value in [(8, "3.00"), (9, "0.00")]:
        captures.append(
            Evidence(
                str(identifier),
                ORIGIN
                + f"/service/rest/v1/products/100000009/options/{identifier}/prices?quantities=50",
                4,
                200,
                {"items": [{"quantity": 50, "markupPrice": value}]},
            )
        )
    result = audit(quote, captures)
    assert result["status"] == "pass"
    assert [
        item["amount"] for item in result["components"] if item["status"] == "priced"
    ] == ["5.60", "3.00", "0.00"]


def test_fully_known_sum_mismatch_is_distinct_from_unresolved_residual():
    quote, captures = fixture(total="8.60")
    result = audit(quote, captures)
    assert result["status"] == "mismatch" and result["unexplained_residual"] == "3.00"
    assert result["reasons"][0]["code"] == "exact_component_sum_mismatch"
    unscoped = Evidence(
        quote.id,
        quote.url,
        quote.created_at,
        quote.status,
        {**quote.data, "service_context_observed": False},
    )
    result = audit(unscoped, captures)
    assert result["status"] == "unresolved" and result["unexplained_residual"] == "3.00"


def test_manual_proof_100_never_maps_from_three_dollar_residual():
    quote, captures = fixture(
        total="8.60", services=service_observation("100", delivery=False)
    )
    add_catalog(
        captures,
        [
            catalog_option(8, 1, 1, name="MANUAL_SOFT_PROOF"),
            catalog_option(9, 36, 1, name="MANUAL_NO_PROOF"),
        ],
    )
    captures.append(
        Evidence(
            "manual-price",
            ORIGIN + "/service/rest/v1/products/100000009/options/8/prices",
            4,
            200,
            {"items": [{"quantity": 50, "markupPrice": "3.00"}]},
        )
    )
    result = audit(quote, captures)
    assert result["status"] == "unresolved" and result["unexplained_residual"] == "3.00"
    mapping = next(
        reason
        for reason in result["reasons"]
        if reason["code"] == "selected_option_mapping_unresolved"
    )
    assert mapping["control"] == "proofType" and mapping["candidate_option_ids"] == [
        "8",
        "9",
    ]
    assert "manual-price" not in result["evidence_ids"]
    assert any(
        item["control"] == "fileDeliveryForIop" and item["status"] == "observed_absent"
        for item in result["components"]
    )


def test_delivery_5_is_not_coating_5_and_missing_prices_are_not_zero():
    quote, captures = fixture(services=service_observation())
    add_catalog(captures, [catalog_option(8, 31, 1), catalog_option(9, 5, 4)])
    result = audit(quote, captures)
    assert result["status"] == "unresolved" and result["partial_sum_matches"] is True
    assert any(
        reason["code"] == "file_delivery_component_contract_missing"
        for reason in result["reasons"]
    )
    assert any(
        reason["code"] == "exact_component_price_missing"
        and reason["control"] == "proofType"
        for reason in result["reasons"]
    )


@pytest.mark.parametrize(
    "change",
    ["quantity_id", "null", "wrong_base", "future", "other_site", "unknown_account"],
)
def test_price_context_and_exact_quantity_must_match(change):
    quote, captures = fixture()
    base = captures[-1]
    data, url, timestamp = deepcopy(base.data), base.url, base.created_at
    if change == "quantity_id":
        data["items"][0]["quantity"] = 1234
    elif change == "null":
        data["items"][0]["markupPrice"] = None
    elif change == "wrong_base":
        url = url.replace("100000009", "100000010")
    elif change == "future":
        timestamp = 20
    elif change == "other_site":
        url = url.replace("www.printograph.com", "other.example")
    else:
        url = url.replace("cid=", "cid=account123")
    captures[-1] = Evidence(base.id, url, timestamp, 200, data)
    result = audit(quote, captures)
    assert result["status"] == "unresolved"
    assert "partial_sum_matches" not in result


def test_temporal_association_and_missing_legacy_services_cannot_pass():
    quote, captures = fixture()
    result = audit_quote(quote, captures)
    assert result["status"] == "unresolved" and result["partial_sum_matches"]
    assert any(
        reason["code"] == "row_linked_capture_provenance_missing"
        for reason in result["reasons"]
    )


@pytest.mark.parametrize("value", ["-$5.60", "($5.60)", "NaN", "$1,23", None])
def test_invalid_amounts_cannot_become_positive_prices(value):
    with pytest.raises(ValueError):
        quote_amount(value)
    assert quote_amount("$1,250.50") == Decimal("1250.50")


def test_offline_reader_opens_database_readonly_and_rejects_object_traversal(tmp_path):
    database = tmp_path / "frozen.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "create table responses (id text, url text, created_at real, status integer, object_path text)"
    )
    connection.commit()
    connection.close()
    reader = SavedEvidence(database, tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            reader.connection.execute("delete from responses")
        with pytest.raises(ValueError):
            reader.object("../outside.json")
    finally:
        reader.close()


def test_undefined_pass_rate_is_honest_and_diagnostic_matches_are_not_passes():
    report = {
        "database": "frozen.db",
        "summary": {
            "quotes_examined": 1,
            "known_service_scope_quotes": 1,
            "statuses": {"unresolved": 1},
            "pass_rate": None,
            "complete_reconciliation_denominator": 0,
            "resolution_coverage": "0.00000000",
            "partial_sum_matches_not_passes": 1,
            "partial_sum_available": 1,
            "reason_counts": {"missing": 1},
        },
    }
    text = render_markdown(report)
    assert "undefined (zero fully resolvable quotes)" in text
    assert "These are not passes" in text


CONTRACT = {
    "version": 1,
    "sha256": AUDITED_ORDER_FORM_SHA256,
    "path": "reviewed-public-fixture.js",
}


def pricing_state(**values):
    return {
        "observed": True,
        "values": {
            "roundedCorners": "",
            "spotcoating": "",
            "raisedprint": "",
            "raisedFoilColor": "",
            "proofType": "31",
            **values,
        },
        "coupon_status": "none",
        "is_admin": False,
        "price_group_id": "",
        "free_sample": False,
    }


def test_post_contract_instant_proof_delivery_and_coating_need_no_price_endpoint():
    quote, captures = fixture(services=service_observation())
    quote.data["controls"].append(
        {"key": "coating", "value": "5", "selected_label": "UV"}
    )
    quote.data["pricing_state"] = pricing_state()
    result = audit_quote(
        quote,
        captures,
        linked_evidence_ids=[item.id for item in captures],
        contract=CONTRACT,
    )
    assert result["status"] == "pass"
    zero_components = {
        item["control"]
        for item in result["components"]
        if item["status"] == "contract_no_additive_component"
    }
    assert zero_components == {"coating", "proofType", "fileDeliveryForIop"}
    assert result["source_contract"]["sha256"] == AUDITED_ORDER_FORM_SHA256


def test_post_contract_manual_subselection_is_required_and_never_defaulted():
    quote, captures = fixture(
        total="8.60", services=service_observation("100", delivery=False)
    )
    quote.data["pricing_state"] = pricing_state(proofType="100")
    add_catalog(captures, [catalog_option(8, 1, 1, name="MANUAL_SOFT_PROOF")])
    captures.append(
        Evidence(
            "manual-price",
            ORIGIN + "/service/rest/v1/products/100000009/options/8/prices",
            4,
            200,
            {"items": [{"quantity": 50, "markupPrice": "3.00"}]},
        )
    )
    kwargs = {
        "linked_evidence_ids": [item.id for item in captures],
        "contract": CONTRACT,
    }
    missing = audit_quote(quote, captures, **kwargs)
    assert (
        missing["status"] == "unresolved" and missing["unexplained_residual"] == "3.00"
    )
    assert any(
        reason.get("required") == "manualProofType" for reason in missing["reasons"]
    )
    quote.data["pricing_state"]["values"]["manualProofType"] = "1"
    assert audit_quote(quote, captures, **kwargs)["status"] == "pass"


def test_post_contract_hidden_priced_selections_and_coupon_state_are_required():
    quote, captures = fixture(services=service_observation())
    result = audit_quote(
        quote,
        captures,
        linked_evidence_ids=[item.id for item in captures],
        contract=CONTRACT,
    )
    reasons = {reason["code"] for reason in result["reasons"]}
    assert result["status"] == "unresolved" and result["partial_sum_matches"]
    assert "hidden_priced_option_state_not_recorded" in reasons
    assert "discount_override_context_not_recorded_or_unsupported" in reasons


def test_unreviewed_contract_is_not_executed_or_trusted(tmp_path):
    path = tmp_path / "unreviewed.js"
    path.write_text("throw new Error('must not execute')")
    with pytest.raises(ValueError, match="Unreviewed"):
        load_price_contract(path)
