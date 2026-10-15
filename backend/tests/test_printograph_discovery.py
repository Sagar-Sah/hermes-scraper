"""Candidate discovery plans never promote a Cartesian tuple into a valid SKU."""

from copy import deepcopy

import pytest

from crawler.printograph_discovery import (
    DiscoveryContractError, option_type_query, plan_discovery, source_contract,
)


def scripts():
    order = '''let retail={1:{specs:["size","orientation","paper","color"],options:["coating"]},27:{specs:["size","orientation","paper","color"],options:["coating"]},36:{specs:["shape","size","orientation","paper","finish","color"],options:["windPosition"]}};
let broker={1:{specs:["size","orientation","paper","color"],options:["coating","raisedPrint"]}};
let api={getProductId:function(e){return "/service/rest/v1/products?productType="+e+"&turnaround="+e;},processProductIdData:function(e,t){return 1==t.items.length;},getQuantityBySpec:function(e){return e;},getOptionQuantityBySpec:function(e){return "optionIds="+e;},getOption:function(e){return e.type.id+",";},autoDetectShapeForSize:function(r){return r.properties.shape;}};'''
    common = '''let api={getSpec:function(e){return a.get("/settings/product/specifications"+e);},getProductId:function(e){return a.get(e);},getOption:function(e){return a.get(e+"/options?requestValues=true");},getOptionQuantityBySpec:function(e){return a.get(e);},getOptionEnumById:function(e){var t="";switch(e){case 2:t="roundedCorners";break;case 135:case 138:t="raisedPrint";break;case 32:case 54:t="windPosition";break;}return t;},getTurnaround:function(e){return {method:"POST",url:"/service/rest/v1/rules/turnarounds",data:e};}};'''
    return order, common


def specifications():
    return {
        "sizes": [{"id": 10, "orientation": {"id": 1}, "properties": {"shape": 5}},
                  {"id": 11, "orientation": {"id": 2}, "properties": {"shape": 4}}],
        "shapes": [{"id": 4}, {"id": 5}, {"id": 99}],
        "papers": [{"id": 1}, {"id": 2}], "colors": [{"id": 1}],
        "finishes": [{"id": 3}], "turnarounds": [{"id": 1}, {"id": 3}],
    }


def test_contract_consensus_does_not_guess_bulk_or_store_variant():
    contract = source_contract(*scripts())
    assert contract["products"]["1"]["store_variant_resolution"] == "required_before_option_enumeration"
    assert contract["lookup_method"] == "GET"
    assert contract["lookup_consumer"] == "exactly_one_item_required"
    assert contract["bulk_product_listing"] == "not_established"
    assert contract["omitted_filter_listing"] == "not_established"
    assert contract["multiple_product_ids"] == "not_established"
    assert contract["option_type_ids_batching"] == "frontend_constructs_comma_separated_optionIds"
    broken = scripts()[0].replace('1==t.items.length', 't.items.length>0')
    with pytest.raises(DiscoveryContractError, match="single-match"):
        source_contract(broken, scripts()[1])


def test_candidates_keep_geometry_dependencies_and_never_unlock_prices():
    contract = source_contract(*scripts())
    result = plan_discovery(36, specifications(), contract, evidence_id="spec", product_url="https://example.test/roll")
    assert result["candidate_lookup_count"] == 8
    assert result["validated_base_count"] == 0
    assert not result["discovery_complete"] and not result["pricing_unlocked"]
    for request in result["requests"]:
        query = request["params"]
        assert (query["size"], query["orientation"], query["shape"]) in {("10", "1", "5"), ("11", "2", "4")}
        assert request["valid_combination"] is None
        assert request["candidate_status"] == "unvalidated"
        assert request["phase"] == "DISCOVERY" and "prices" not in request["path"]
    assert all("prices" not in request["path"] for request in result["conditional_discovery"])


@pytest.mark.parametrize("mutation", ["orientation", "shape", "duplicates"])
def test_incomplete_geometry_or_conflicting_specifications_fail_closed(mutation):
    spec = specifications()
    if mutation == "orientation":
        spec["sizes"][0].pop("orientation")
    elif mutation == "shape":
        spec["sizes"][0]["properties"]["shape"] = 998
    else:
        spec["papers"].append({"id": 1, "label": "conflicting label"})
    with pytest.raises(DiscoveryContractError):
        plan_discovery(36, spec, source_contract(*scripts()), evidence_id="spec", product_url="https://example.test/roll")


def test_historical_match_is_evidence_not_fresh_validation():
    contract = source_contract(*scripts())
    query = {"productType": ["1"], "turnaround": ["1"], "size": ["10"], "orientation": ["1"], "paper": ["1"], "color": ["1"]}
    historical = [{"product": "https://example.test/cards", "query": query, "base_product_ids": ["123"], "evidence_ids": ["old"]}]
    result = plan_discovery(1, specifications(), contract, evidence_id="spec", product_url="https://example.test/cards", historical_lookups=historical)
    known = [request for request in result["requests"] if request["historical_single_match_evidence_ids"]]
    assert len(known) == 1 and known[0]["candidate_status"] == "unvalidated"
    assert known[0]["valid_combination"] is None


def test_option_batch_uses_type_ids_and_rejects_partial_catalog():
    contract = source_contract(*scripts())
    catalog = {"items": [{"id": 990001, "type": {"id": 135}}, {"id": 990002, "type": {"id": 138}}, {"id": 990003, "type": {"id": 2}}]}
    assert option_type_query(catalog, ["raisedPrint"], contract) == "135,138"
    partial = deepcopy(catalog)
    partial["total"] = 4
    with pytest.raises(DiscoveryContractError, match="paginated"):
        option_type_query(partial, ["raisedPrint"], contract)


def test_transport_turnaround_is_disambiguated_from_cart_builder():
    order, common = scripts()
    common += 'let cart={getTurnaround:function(e){return {quantity:e.quantity};}};'
    assert source_contract(order, common)["turnaround_rules_method"] == "POST"
