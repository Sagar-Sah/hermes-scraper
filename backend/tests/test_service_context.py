from copy import deepcopy

import pytest

from crawler.service_context import canonical_service_context, service_context_is_bound
from crawler.engine import row_identity
from crawler.adapters.browser import make_record


def service_observation(value="31", *, delivery=True):
    selected = [
        {
            "key": "proofType",
            "control_count": 2,
            "checked_count": 1,
            "selection_status": "selected",
            "selected": [
                {
                    "value": value,
                    "checked": True,
                    "label": "Service label",
                    "visible": False,
                }
            ],
        }
    ]
    if delivery:
        selected.append(
            {
                "key": "fileDeliveryForIop",
                "control_count": 2,
                "checked_count": 1,
                "selection_status": "selected",
                "selected": [{"value": "5", "checked": True}],
            }
        )
    return {"service_context_observed": True, "service_context": selected}


def test_missing_legacy_and_observed_absence_are_distinct_without_fee_inference():
    missing = canonical_service_context({})
    absent = canonical_service_context(
        {"service_context_observed": True, "service_context": []}
    )
    assert missing["status"] == "unknown" and not service_context_is_bound(missing)
    assert service_context_is_bound(absent)
    assert {group["status"] for group in absent["groups"].values()} == {"absent"}
    assert canonical_service_context({"service_context": []}) == missing
    assert "fee" not in str(absent)


def test_selected_ids_and_explicitly_absent_delivery_are_identity_not_labels():
    state = service_observation("100", delivery=False)
    signature = canonical_service_context(state)
    assert signature["groups"]["fileDeliveryForIop"] == {
        "status": "absent",
        "values": [],
    }
    assert signature["groups"]["proofType"] == {"status": "selected", "values": ["100"]}
    state["service_context"][0]["selected"][0].update(
        label="Other label", visible=True, disabled=True
    )
    assert canonical_service_context(state) == signature
    assert signature != canonical_service_context(service_observation())


@pytest.mark.parametrize(
    "change",
    [
        "duplicates",
        "unchecked",
        "unselected",
        "multiple",
        "missing_value",
        "wrong_count",
        "unknown_group",
    ],
)
def test_ambiguous_or_unselected_groups_never_bind(change):
    state = service_observation()
    group = state["service_context"][0]
    if change == "duplicates":
        state["service_context"].append(deepcopy(group))
    elif change == "unchecked":
        group["selected"][0]["checked"] = False
    elif change == "unselected":
        group.update(selected=[], checked_count=0, selection_status="unselected")
    elif change == "multiple":
        group.update(
            selected=group["selected"] * 2,
            checked_count=2,
            selection_status="ambiguous",
        )
    elif change == "missing_value":
        group["selected"][0].pop("value")
    elif change == "wrong_count":
        group["checked_count"] = 2
    else:
        group["key"] = "unrelated_input"
    assert not service_context_is_bound(canonical_service_context(state))


def test_embedded_signature_cannot_hide_conflicting_raw_observation():
    instant, manual = service_observation(), service_observation("100")
    manual["pricing_context"] = {"service_context": canonical_service_context(instant)}
    assert not service_context_is_bound(canonical_service_context(manual))


def test_make_record_new_schema_distinguishes_services_without_changing_money():
    snapshot = {
        "controls": [{"key": "quantity", "value": "50", "selected_label": "50"}],
        "title": "Cards",
        "price": "$5.60",
        "images": [],
        "currency": "USD",
        **service_observation(),
    }
    first = make_record(
        "https://www.printograph.com/products/business-cards/order",
        snapshot,
        ["instant"],
        set(),
        {},
    )
    second = make_record(
        first["product_url"],
        {**snapshot, **service_observation("100", delivery=False)},
        ["manual"],
        set(),
        {},
    )
    assert first["subtotal"] == second["subtotal"] == "5.60"
    assert first["configuration"] == second["configuration"] == {}
    assert first["record_schema_version"] == second["record_schema_version"] == 2
    assert row_identity(first) != row_identity(second)
    labels_only = deepcopy(first)
    labels_only["service_context"][0]["selected"][0]["label"] = "Renamed"
    assert row_identity(first) == row_identity(labels_only)
