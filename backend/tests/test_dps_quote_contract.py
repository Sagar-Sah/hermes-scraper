import json
from pathlib import Path

import pytest

from crawler.dps_quote_contract import annotate_scoped_price_report, scoped_request


PILOT = Path(__file__).parent / "fixtures/dps-high-tack-pilot.json"


def source_pilot():
    return json.loads(PILOT.read_text())


def test_saved_native_selection_round_trips_exact_source_encoding():
    pilot = source_pilot()
    observed = pilot["observations"][0]["context"]
    request = scoped_request(
        pilot, width=observed["width"], height=observed["height"], quantity=observed["quantity"],
        selected_options={"982": "4001", "703": "3083", "884": "3740"},
    )
    assert request == {key: observed[key] for key in ("width", "height", "quantity", "turnaroundId", "options")}


def test_transfer_cut_requires_visible_child_choices_in_page_order():
    pilot = source_pilot()
    request = scoped_request(
        pilot, width=3, height=3, quantity=25,
        selected_options={"884": "3740", "10077": "10162", "982": "4003", "703": "3083", "432": "1743"},
    )
    assert request["options"] == "982E4003C432E1743C10077E10162C703E3083C884E3740"
    with pytest.raises(ValueError, match="visible_option_missing"):
        scoped_request(pilot, width=3, height=3, quantity=25,
                       selected_options={"982": "4003", "432": "1743", "703": "3083", "884": "3740"})


def test_hidden_child_and_out_of_domain_values_are_rejected():
    pilot = source_pilot()
    baseline = {"982": "4001", "703": "3083", "884": "3740"}
    with pytest.raises(ValueError, match="hidden_or_unrecognized"):
        scoped_request(pilot, width=3, height=3, quantity=25,
                       selected_options={**baseline, "432": "1743"})
    with pytest.raises(ValueError, match="quantity_outside"):
        scoped_request(pilot, width=3, height=3, quantity=25001, selected_options=baseline)
    with pytest.raises(ValueError, match="dimension_not_in"):
        scoped_request(pilot, width=1, height=3, quantity=25, selected_options=baseline)


def test_published_transfer_size_limit_overrides_a_200_price_route_response():
    pilot = source_pilot()
    transfer = {"982": "4003", "432": "1743", "10077": "10162",
                "703": "3083", "884": "3740"}
    assert scoped_request(pilot, width=120, height=50, quantity=25,
                          selected_options=transfer)["width"] == "120"
    with pytest.raises(ValueError, match="exceeds_published_size_limit"):
        scoped_request(pilot, width=120, height=51, quantity=25, selected_options=transfer)
    # The size restriction belongs to Transfer Cut, not the whole product.
    die_cut = {"982": "4001", "703": "3083", "884": "3740"}
    assert scoped_request(pilot, width=120, height=51, quantity=25,
                          selected_options=die_cut)["height"] == "51"


def test_old_price_responses_with_missing_child_options_are_flagged_not_removed():
    pilot = source_pilot()
    baseline = pilot["observations"][0]["context"]
    valid = {key: baseline[key] for key in ("width", "height", "quantity", "turnaroundId", "options")}
    incomplete = {**valid, "options": "982E4003C703E3083C884E3740"}
    report = {"results": [{"request": valid, "price": {"price": "0.61"}},
                          {"request": incomplete, "price": {"price": "1.17"}}]}
    annotated = annotate_scoped_price_report(report, pilot)
    assert annotated["ui_complete_quote_count"] == 1
    assert annotated["ui_incomplete_quote_count"] == 1
    assert annotated["results"][0]["ui_selection_complete"] is True
    assert annotated["results"][1]["ui_selection_complete"] is False
    assert annotated["availability"] == "unverified" and not annotated["final_eligible"]
    assert "ui_selection_complete" not in report["results"][0]
