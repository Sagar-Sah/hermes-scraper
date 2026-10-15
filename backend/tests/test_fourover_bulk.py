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
