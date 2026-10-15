import json
from pathlib import Path

import pytest

from crawler.dps_route_harvest import plan_cases
from crawler.dps_pilot import SCRIPT_SHA256


def pilot():
    choices = lambda *values: [{'value': str(v), 'disabled': False} for v in values]
    return {'status': 'completed', 'source_script_sha256': SCRIPT_SHA256,
            'source_contract_verified': True, 'observations': [{
                'context': {'product_id': '13997', 'width': '3', 'height': '3',
                            'quantity': '25', 'turnaroundId': '0', 'options': '1E2C3E4'},
                'price': {'price': '0.61'},
                'controls': {'width': {'choices': choices(2, 3, 4)},
                             'height': {'choices': choices(2, 3, 4)},
                             'quantity': {'kind': 'number', 'minimum': '1',
                                          'maximum': '25000', 'step': None},
                             'options': [{'id': '1', 'value': '2', 'label': 'Cut',
                                          'choices': [{'value': '2', 'label': 'Default', 'disabled': False},
                                                      {'value': '5', 'label': 'Kiss', 'disabled': False} ]},
                                         {'id': '3', 'value': '4', 'label': 'Production',
                                          'choices': [{'value': '4', 'label': 'Standard', 'disabled': False},
                                                      {'value': '6', 'label': 'Rush', 'disabled': False}]}]}}]}


def test_plan_is_bounded_scoped_and_baseline_first():
    cases = plan_cases(pilot(), [100, 1], [[2, 4], [3, 3]])
    assert len(cases) == 6
    assert cases[0]['scope'] == 'native_baseline'
    assert cases[0]['request']['quantity'] == '25'
    assert cases[1]['request']['quantity'] == '100'
    assert len({case['key'] for case in cases}) == len(cases)
    assert {case['choice'] for case in cases if case['scope'] == 'one_option_change'} == {'Kiss', 'Rush'}


@pytest.mark.parametrize('quantities,dimensions', [([0], []), ([25001], []), ([1, 1], []),
                                                 ([True], []), ([1], [[8, 2]]), ([1], [[2, 8]])])
def test_plan_rejects_outside_rendered_domain(quantities, dimensions):
    with pytest.raises(ValueError):
        plan_cases(pilot(), quantities, dimensions)


def test_plan_requires_pinned_completed_browser_evidence():
    evidence = pilot()
    evidence['source_contract_verified'] = False
    with pytest.raises(ValueError):
        plan_cases(evidence, [100], [])
