import pytest

from crawler.fourover_bulk import child_configurations


def test_reads_exact_child_ids_from_product_initializer():
    html = '''<script type="text/x-magento-init">{"*":{"Magento_Ui/js/core/app":
      {"components":{"child-sku":{"childSkuConfig":{
        "5296":{"sku":"CARD-40"},"5294":{"sku":"CARD-50"}}}}}}}</script>'''
    assert child_configurations(html) == [
        {'child_id':'5296','sku':'CARD-40'}, {'child_id':'5294','sku':'CARD-50'}]


def test_rejects_absent_or_ambiguous_child_map():
    with pytest.raises(ValueError): child_configurations('<html></html>')
    script='''<script type="text/x-magento-init">{"*":{"Magento_Ui/js/core/app":
      {"components":{"child-sku":{"childSkuConfig":{"1":{"sku":"A"}}}}}}}</script>'''
    with pytest.raises(ValueError): child_configurations(script+script)


def test_unrelated_magento_json_arrays_or_nested_scalars_do_not_hide_valid_child_map():
    unrelated = '<script type="text/x-magento-init">["unrelated"]</script>'
    unrelated += '<script type="text/x-magento-init">{"*":{"Magento_Ui/js/core/app":[]}}</script>'
    valid = '<script type="text/x-magento-init">{"*":{"Magento_Ui/js/core/app":{"components":{"child-sku":{"childSkuConfig":{"7":{"sku":"PUBLIC"}}}}}}}</script>'
    assert child_configurations(unrelated + valid) == [{'child_id': '7', 'sku': 'PUBLIC'}]


def test_excessive_declared_children_reports_counts_without_source_values():
    import json
    from crawler.fourover_bulk import ChildConfigurationError
    children = {str(n): {'sku': 'private-source-value'} for n in range(1, 2002)}
    html = '<script type="text/x-magento-init">' + json.dumps({'*': {'Magento_Ui/js/core/app': {
        'components': {'child-sku': {'childSkuConfig': children}}}}}) + '</script>'
    with pytest.raises(ChildConfigurationError) as caught:
        child_configurations(html)
    assert caught.value.code == 'child_declaration_limit'
    assert caught.value.diagnostic['declared_child_count'] == 2001
    assert caught.value.diagnostic['maximum_children'] == 2000
    assert 'private-source-value' not in json.dumps(caught.value.diagnostic)


@pytest.mark.parametrize('html,code', [
    ('<html></html>', 'child_declaration_missing'),
    ('<script type="text/x-magento-init">{"*":{"Magento_Ui/js/core/app":{"components":{"child-sku":{"childSkuConfig":[]}}}}}</script>', 'child_declaration_invalid_schema'),
    ('<script type="text/x-magento-init">{"*":{"Magento_Ui/js/core/app":{"components":{"child-sku":{"childSkuConfig":{"7":{"sku":""}}}}}}}</script>', 'child_declaration_invalid_sku'),
])
def test_controlled_schema_error_categories(html, code):
    from crawler.fourover_bulk import ChildConfigurationError
    with pytest.raises(ChildConfigurationError) as caught:
        child_configurations(html)
    assert caught.value.code == code
    assert set(caught.value.diagnostic) == {'magento_init_scripts', 'invalid_json_scripts', 'child_map_count',
                                           'invalid_child_map_types', 'declared_child_count', 'maximum_children'}
