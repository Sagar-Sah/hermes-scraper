"""Source-derived option interactions remain distinct from individual support."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from crawler.printograph_constraints import ConstraintError, configuration_fingerprint, extract_rule_contract, validate_configuration


@pytest.fixture
def contract():
    return json.loads((Path(__file__).parent / 'fixtures' / 'printograph-constraints.json').read_text())


def inputs(product_type="1", **changes):
    selection = {"size": "101", "orientation": "1", "paper": "1", "color": "2", "quantity": "100",
                 "productionTime": "1", "proofType": "31", "fileDelivery": "5", "designServices": "",
                 "roundedCorners": "", "coating": "5", "spotCoating": "", "raisedPrint": "", "raisedFoilColor": ""}
    if product_type == "36":
        selection = {k:v for k,v in selection.items() if k not in {"roundedCorners", "coating", "spotCoating", "raisedPrint", "raisedFoilColor"}}
        selection.update({"shape": "5", "size": "742", "paper": "12", "color": "1", "finish": "1", "windPosition": "54"})
    selection.update({k:str(v) for k,v in changes.items()})
    context = {"origin": "https://example.test", "currency": "USD", "store_type": "broker", "store_id": "200", "price_group_id": "group",
               "session_scope": "session", "source_epoch": "epoch", "service_signature": "service",
               "is_admin": False, "editing_existing_item": False, "images_uploaded": False,
               "mailing_opted": False, "custom_dimensions": False, "domains_bound": True}
    fields = {"size", "orientation", "paper", "color"} | ({"shape", "finish"} if product_type == "36" else set())
    lookup = {"validated": True, "base_product_id": "123", "request": {"context": deepcopy(context),
              "params": {"productType": product_type, "turnaround": selection['productionTime'], **{k:selection[k] for k in fields}}}}
    type_ids = [2,5,6,40,3,4,39,135,136,137,138,139,140,141,142,143] if product_type != "36" else [32,33,34,35,54]
    catalog = {"items": [{"id": 100000+tid, "type": {"id": tid}, "group": {"id": tid}} for tid in type_ids]}
    tiers = [{"quantity": 50, "turnaroundList": [1,3]}, {"quantity": 100, "turnaroundList": [1,3]}]
    values = {"lookup": lookup, "catalog": catalog, "base_support": {"items": deepcopy(tiers)},
              "option_support": {str(tid): deepcopy(tiers) for tid in type_ids},
              "option_prices": {str(100000+tid): {"items": [{"quantity":50,"markupPrice":"3.00"}, {"quantity":100,"markupPrice":"4.00"}]} for tid in type_ids},
              "context": context}
    bind_rule(product_type, selection, values)
    return selection, values


def bind_rule(product_type, selection, values, modes=(1,3)):
    values['turnaround_rule'] = {"selection_fingerprint": configuration_fingerprint(product_type, selection, values['context'], '123'),
                                 "evidence_id": "rules-response", "data": {"items": [{"id": m, "range": {"min":2,"max":4}} for m in modes]}}


def test_real_static_literals_include_pair_rules_and_forced_foil_default(contract):
    assert len(contract['validation_rules']) == 39
    assert contract['display_options']['coating']['40']['color'] == [1]
    assert contract['display_options']['spotCoating']['3']['coating'] == [40]
    assert contract['display_fields']['raisedFoilColor']['raisedPrint'] == ['',135,136,137]
    assert any(rule['criteria'] == {'raisedPrint':[138,139,140], 'raisedFoilColor':['']} and rule['result'] == {'raisedFoilColor':[141]} for rule in contract['validation_rules'])
    with pytest.raises(ConstraintError, match='Unknown source'):
        extract_rule_contract('changed', 'bundle')


@pytest.mark.parametrize('product_type',['1','27','36'])
def test_supported_candidate_requires_all_bound_domains_and_runtime_rule(contract, product_type):
    selection, values = inputs(product_type)
    result = validate_configuration(product_type, selection, contract=contract, **values)
    assert result['status'] == 'supported', result
    assert result['production'] == {'min':2,'max':4}
    assert not result['pricing_composition_verified'] and not result['full_catalog_complete']
    values['turnaround_rule'] = None
    result = validate_configuration(product_type, selection, contract=contract, **values)
    assert result['status'] == 'unresolved'
    assert 'full_option_combination_turnaround_rule_not_bound' in result['unresolved']


def test_individual_available_options_do_not_prove_coating_raised_pair(contract):
    selection, values = inputs(paper=10, coating=5, raisedPrint=135, proofType=100)
    result = validate_configuration('1',selection,contract=contract,**values)
    assert result['status'] == 'invalid'
    assert 'raised_print_and_coating_or_spot_are_mutually_reset' in result['invalid']


def test_finishing_change_reports_proof_service_and_foil_changes(contract):
    selection, values = inputs(paper=10, coating='', raisedPrint=138, raisedFoilColor='')
    result = validate_configuration('1',selection,contract=contract,**values)
    assert result['status'] == 'invalid'
    assert {c['field']:c['to'] for c in result['changes']} == {'raisedFoilColor':'141','proofType':'100'}
    assert result['resulting_selection']['fileDelivery'] == '5'
    # Caller can explicitly validate the resulting state under newly bound evidence.
    corrected, current = inputs(paper=10,coating='',raisedPrint=138,raisedFoilColor=141,proofType=100)
    assert validate_configuration('1',corrected,contract=contract,**current)['status'] == 'supported'


def test_single_sided_print_rejects_back_side_raised_and_clears_foil(contract):
    selection, values = inputs(paper=10,color=1,coating='',raisedPrint=139,raisedFoilColor=141,proofType=100)
    result = validate_configuration('1',selection,contract=contract,**values)
    assert result['status'] == 'invalid'
    assert result['resulting_selection']['raisedPrint'] == result['resulting_selection']['raisedFoilColor'] == ''


@pytest.mark.parametrize('paper,finish,expected',[(13,3,1),(14,1,2),(15,1,3),(12,3,1)])
def test_roll_finish_stock_corrections_are_explicit(contract,paper,finish,expected):
    selection, values = inputs('36',paper=paper,finish=finish)
    result=validate_configuration('36',selection,contract=contract,**values)
    assert result['status']=='invalid'
    assert result['resulting_selection']['finish']==str(expected)


def test_rush_checks_combined_rules_and_each_selected_option(contract):
    selection, values = inputs('27',paper=4,coating='',color=1,productionTime=3)
    result=validate_configuration('27',selection,contract=contract,**values)
    assert result['status']=='invalid' and result['resulting_selection']['productionTime']=='1'
    selection, values = inputs(roundedCorners=2,productionTime=3)
    values['option_support']['2'][1]['turnaroundList']=[1]
    result=validate_configuration('1',selection,contract=contract,**values)
    assert 'option_quantity_or_mode_unsupported:2' in result['invalid']
    selection, values = inputs(productionTime=3)
    bind_rule('1',selection,values,modes=(1,))
    result=validate_configuration('1',selection,contract=contract,**values)
    assert 'selected_mode_absent_from_combined_turnaround_rules' in result['invalid']


def test_quantity50_individual_support_does_not_override_missing_price_domain(contract):
    selection, values = inputs(paper=10,coating='',raisedPrint=135,proofType=100,quantity=50)
    values['option_prices']['100135']['items']=[{'quantity':100,'markupPrice':'4.00'},{'quantity':250,'markupPrice':'8.00'}]
    result=validate_configuration('1',selection,contract=contract,**values)
    assert result['status']=='invalid'
    assert 'selected_option_has_no_exact_price_tier:135' in result['invalid']


@pytest.mark.parametrize('mutation',['source','upload','unknown','lookup','context','rule','domain','service'])
def test_unknown_or_unbound_inputs_never_become_supported(contract,mutation):
    selection, values=inputs()
    if mutation=='source': contract['bundle_sha256']['orderForm.js']='changed'
    elif mutation=='upload': values['context']['images_uploaded']=True
    elif mutation=='unknown': selection['unmodeled_extra']='1'
    elif mutation=='lookup': values['lookup']['request']['params']['paper']='9'
    elif mutation=='context': values['context']['price_group_id']='changed'
    elif mutation=='rule': values['turnaround_rule']['selection_fingerprint']='other'
    elif mutation=='domain': values['context']['domains_bound']=False
    else: selection['proofType']='37'
    assert validate_configuration('1',selection,contract=contract,**values)['status']=='unresolved'


def test_action_dependent_color3_coating_spot_pair_stays_unresolved(contract):
    selection,values=inputs(paper=10,color=3,coating=40,spotCoating=4,proofType=100)
    result=validate_configuration('1',selection,contract=contract,**values)
    assert result['status']=='unresolved'
    assert 'color3_coating_spot_action_order_requires_source_validation' in result['unresolved']


def test_numeric_store_variant_must_enable_selected_spot_field(contract):
    selection, values=inputs(paper=10,coating='',spotCoating=3,proofType=100)
    values['context']['store_id']='100'
    values['lookup']['request']['context']['store_id']='100'
    bind_rule('1',selection,values)
    result=validate_configuration('1',selection,contract=contract,**values)
    assert result['status']=='invalid'
    assert 'option_disabled_by_store_variant:spotCoating' in result['invalid']
    values['context'].pop('store_id')
    assert 'missing_numeric_store_variant' in validate_configuration('1',selection,contract=contract,**values)['unresolved']


def test_visible_velvet_finish_is_a_required_source_singleton(contract):
    selection,values=inputs(paper=31,color=1,coating='')
    result=validate_configuration('1',selection,contract=contract,**values)
    assert result['status']=='unresolved'
    assert 'missing_explicit_state:paperFinish' in result['unresolved']
    selection['paperFinish']='1'
    bind_rule('1',selection,values)
    assert validate_configuration('1',selection,contract=contract,**values)['status']=='supported'


def test_single_tier_and_quantity_dependent_false_do_not_prove_expansion(contract):
    selection,values=inputs(paper=10,coating='',raisedPrint=135,proofType=100,quantity=50)
    values['option_prices']['100135']['items']=[{'quantity':100,'markupPrice':'4.00'}]
    for item in values['catalog']['items']:
        item['quantityDependentPrice']=False
    result=validate_configuration('1',selection,contract=contract,**values)
    assert result['status']=='unresolved'
    assert 'single_tier_option_visibility_requires_validation:135' in result['unresolved']


def test_foil_and_raised_quantity_mode_domains_are_intersected(contract):
    selection,values=inputs('27',paper=10,coating='',raisedPrint=138,raisedFoilColor=143,proofType=100,quantity=2500,productionTime=3)
    values['base_support']['items'].append({'quantity':2500,'turnaroundList':[1,3]})
    values['option_support']['143'].append({'quantity':2500,'turnaroundList':[3]})
    result=validate_configuration('27',selection,contract=contract,**values)
    assert result['status']=='invalid'
    assert 'option_quantity_or_mode_unsupported:138' in result['invalid']
