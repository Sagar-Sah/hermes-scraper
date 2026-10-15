from crawler.itoris_options import project_itoris_dpo, project_itoris_tiers


def test_projects_source_price_attributes_without_guessing_missing_values():
    body=b'''<label for="select_1002">5' Full Wall</label><select id="select_1002" class="product-custom-option">
      <option value="10002" price="0.00">No</option>
      <option value="10003" price="100.00">Single-Sided Face in +$100.00</option>
      <option value="10004">Unpriced choice</option></select>'''
    data=project_itoris_dpo(body)
    assert data['field_count']==1 and data['choice_count']==3 and data['priced_choice_count']==1
    assert data['fields'][0]['label']=="5' Full Wall"
    assert data['fields'][0]['choices'][1]['source_price']=='100.00'
    assert data['fields'][0]['choices'][2]['source_price'] is None
    assert data['final_eligible'] is False


def test_projects_quantity_breaks_without_calling_them_cart_prices():
    body=b'''<div id="itier_prices"><table><tbody>
      <tr class="itier_price" qty="1" price="359" discount="0"><td class="column_quantity_range">1 to 5</td></tr>
      <tr class="itier_price" qty="6" price="349.50" discount="2.65"><td class="column_quantity_range">6 to 10</td></tr>
    </tbody></table></div>'''
    data=project_itoris_tiers(body)
    assert data['tier_count']==2
    assert data['tiers'][1]=={'minimum_quantity':6,'source_price':'349.50','range_label':'6 to 10','discount':'2.65'}
    assert data['final_eligible'] is False


def test_reads_embedded_one_time_fee_without_claiming_payable_total():
    body=b'''<select id="select_1007" class="product-custom-option">
      <option value="10016" price="25.00">Color Match Thread +$25.00</option></select>
      <script>window.dpoOptions.initialize({"absolute_pricing":"0","options":[
        {"items":[{"option_type_id":10016,"option_id":1007,"title":"Color Match Thread",
        "price":25,"price_type":"fixed","onetime":1,"use_qty":0}]}]});</script>'''
    data=project_itoris_dpo(body)
    policy=data['pricing_policy']
    assert policy['configuration_found'] is True
    assert policy['absolute_pricing']=='0'
    assert policy['choices']==[{'option_type_id':'10016','option_id':'1007',
        'title':'Color Match Thread','price':'25','price_type':'fixed',
        'onetime':1,'use_qty':0}]
    assert policy['pricing_semantics_verified'] is False
    assert data['final_eligible'] is False


def test_reads_itoris_items_outside_global_initializer():
    body=b'''window.productFields=[{"title":"Thread","items":[
      {"option_type_id":10016,"option_id":1007,"title":"Color Match Thread",
       "price":25,"price_type":"fixed","onetime":1}]}];
      window.dpoOptions.initialize({"absolute_pricing":"0"});'''
    policy=project_itoris_dpo(body)['pricing_policy']
    assert len(policy['choices'])==1
    assert policy['choices'][0]['onetime']==1
