"""Physical visibility regressions for the observed Coroplast quantity widgets.

No browser/source requests. The actual capture JavaScript consumes explicit
computed CSS and rectangle witnesses; ARIA semantics cannot replace them.
"""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from crawler.product_companion_fields import (CAPTURE_RENDERED_FIELDS_JS,
    duplicate_field_findings, normalize_field_inventory, static_field_inventory)
from crawler.product_companion_writes import ProductCompanionWrites


SITE = 'https://printitprints.com/shop/grand-format/signs-banners/signs/rigid-signs/4mm-coroplast-signs/'
PROBE = Path(__file__).parent / 'fixtures' / 'companion_field_visibility' / 'probe.js'


def capture(**patch):
    assert shutil.which('node'), 'Node is required to execute the actual capture JavaScript regression'
    payload = {'script': CAPTURE_RENDERED_FIELDS_JS, 'url': SITE,
               'legacy_attrs': {'aria-hidden': 'true', 'tabindex': '-1'}, **patch}
    result = subprocess.run(['node', str(PROBE)], input=json.dumps(payload),
        capture_output=True, text=True, check=True, timeout=10)
    return normalize_field_inventory(json.loads(result.stdout))


@pytest.mark.parametrize('on_control', [False, True])
def test_visible_aria_hidden_legacy_tier_is_still_an_active_quantity_widget(on_control):
    inventory = capture(on_control=on_control)
    legacy, typed, native = inventory['controls']
    assert legacy['visible'] is typed['visible'] is True
    assert legacy['enabled'] is legacy['editable'] is True
    assert native['visible'] is False  # This control really has no rendered box.
    result = duplicate_field_findings(inventory)
    finding, = result['findings']
    assert result['status'] == finding['status'] == 'unknown'
    assert finding['kind'] == 'ambiguous_field_overlap'
    assert finding['purpose'] == 'quantity'
    assert {item['binding_key'] for item in finding['observations']} == {
        'name:print_option_quantity', 'declared:pigf_run_size'}
    assert len(result['inactive_observations']) == 1
    assert finding['proposed_correction']['requires_review'] is True
    assert finding['proposed_correction']['automatic'] is result['automatic_changes_allowed'] is False
    assert 'PRIVATE-ENTERED-VALUE' not in json.dumps(inventory)


@pytest.mark.parametrize('style', [
    {'display':'none'}, {'visibility':'hidden'}, {'visibility':'collapse'}, {'opacity':'0'},
])
def test_computed_css_hiding_excludes_legacy_tier_independently_of_aria(style):
    inventory = capture(style=style)
    assert [item['visible'] for item in inventory['controls']] == [False, True, False]
    result = duplicate_field_findings(inventory)
    assert result['status'] == 'match'
    assert result['findings'] == []
    assert len(result['inactive_observations']) == 2


def test_css_visible_override_of_hidden_attribute_uses_physical_evidence():
    inventory = capture(legacy_attrs={'hidden':'', 'aria-hidden':'true'}, style={'display':'block', 'opacity':'1'})
    assert inventory['controls'][0]['visible'] is True
    assert duplicate_field_findings(inventory)['findings'][0]['kind'] == 'ambiguous_field_overlap'


def test_differently_bound_visible_quantity_overlap_cannot_reach_wordpress_preview(tmp_path):
    findings = duplicate_field_findings(capture())
    audit = {'id':'a' * 32, 'pair_id':'woo-6383', 'observed_at':time.time(), 'report':{
        'proposed_changes':[{'kind':'review_storefront_duplicate_fields',
                             'finding_id':findings['findings'][0]['id'], 'requires_review':True, 'automatic':False}]}}
    pair = {'id':'woo-6383', 'latest_audit_id':audit['id']}
    calls = []
    async def transport(*args):
        calls.append(args)
        raise AssertionError('Different bindings require review, never automatic removal')
    writes = ProductCompanionWrites(SimpleNamespace(root=tmp_path, get_audit=lambda _:audit,
        get_pair=lambda _:pair), transport=transport)
    with pytest.raises(ValueError, match='proposal_requires_manual_content_review'):
        asyncio.run(writes.preview(audit['id'], [0]))
    assert calls == []


@pytest.mark.parametrize('on_control', [False, True])
def test_inert_is_visible_but_not_customer_editable(on_control):
    inventory = capture(legacy_attrs={'inert':'', 'aria-hidden':'true'}, on_control=on_control)
    legacy = inventory['controls'][0]
    assert legacy['visible'] is True
    assert legacy['editable'] is False
    result = duplicate_field_findings(inventory)
    assert result['findings'] == []
    assert len(result['inactive_observations']) == 2


def test_static_aria_and_inert_do_not_claim_visual_hiding():
    html = ('<body class="postid-6383"><main><div id="product-6383"><form class="cart">'
        '<div aria-hidden="true"><select name="print_option_quantity"><option>26</option></select></div>'
        '<div inert><input type="number" name="quantity"></div>'
        '<div style="display:none"><input type="number" name="quantity"></div>'
        '</form></div></main></body>')
    inventory = static_field_inventory(html, url=SITE)
    assert [field['visible'] for field in inventory['controls']] == [None, None, False]
    assert inventory['controls'][1]['editable'] is False
