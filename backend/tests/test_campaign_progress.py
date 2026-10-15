from types import SimpleNamespace
import json
import pytest
from crawler.campaign_progress import CampaignManager, inspect_storefront, required_fixed_options


def test_required_fixed_options_preserves_reviewed_storefront_scope():
    product={'mapping_evidence':[{'required_fixed_options':{'runner_throw':'tr_tc_combo'}}]}
    assert required_fixed_options(product)=={'runner_throw':'tr_tc_combo'}
    product['mapping_evidence'].append({'required_fixed_options':{'runner_throw':'tro'}})
    with pytest.raises(ValueError,match='Conflicting required fixed'):
        required_fixed_options(product)


def test_custom_calculator_is_detected_without_claiming_price_verification():
    html='''<body class="single-product postid-17759"><h1 class="product_title">Pole Banner</h1>
    <form class="cart"><div id="product_calculator_display"></div></form>
    <script>const csvdata = [{"SIZE":"18 x 24","RUNSIZE":1,"TotalPrice":56.26}];</script></body>'''
    result=inspect_storefront(html,'https://printitprints.com/shop/pole/','17759')
    assert result['identity_confirmed']
    assert result['embedded_tables'][0]['rows']==1
    assert result['fields']==[]
    assert result['calculator_types']==['native_csv','simple_cart']
    assert result['prices_verified'] is False


def test_wrong_product_and_executable_csv_are_not_accepted():
    result=inspect_storefront('<body class="postid-2"><script>const csvdata=[];alert(1)</script></body>','https://printitprints.com/shop/pole/','1')
    assert not result['identity_confirmed']
    assert result['embedded_tables']==[{'error':'unsupported_embedded_table'}]


def test_campaign_counts_do_not_promote_diagnosis_or_rows_to_completion(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    row=manager.create(name='Audit',products=[{'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}])
    row['products'][0].update(stage='diagnosed',diagnosed_at='today',row_count=30000)
    manager._write(row)
    current=manager.get(row['id'])
    assert current['total_products']==1
    assert current['counts']=={'diagnosed':1,'extracted':0,'verified':0,'finalized':0,'blocked':0}
    with pytest.raises(ValueError,match='already exists'):
        manager.create(id=row['id'],name='Again',products=[{'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}])


def test_alternate_supplier_queue_preserves_finalized_storefront_product(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    campaign=manager.create(name='Audit',products=[{'id':'17430','title':'Digital Envelopes',
        'storefront_url':'https://printitprints.com/shop/digital-envelopes/',
        'supplier':'fourover','supplier_url':'https://4over.com/digital-envelopes'}])
    campaign['products'][0].update(stage='finalized',finalized_at='today')
    manager._write(campaign)
    result=manager.queue_alternate_supplier(campaign['id'],'17430','printograph','Digital Envelopes')
    assert result['counts']['finalized']==1
    assert result['active_products']==1
    assert result['products'][0]['supplier']=='fourover'
    assert result['alternate_supplier_jobs'][0]['supplier_url'] is None
    assert result['alternate_supplier_jobs'][0]['stage']=='queued'
    assert len(manager.queue_alternate_supplier(campaign['id'],'17430','printograph','Digital Envelopes')['alternate_supplier_jobs'])==1


def test_supplemental_product_queue_keeps_existing_group_and_denominator(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    campaign=manager.create(name='Audit',products=[{'id':'1','title':'Grouped',
        'storefront_url':'https://printitprints.com/shop/grouped/'}])
    manager.configure_completion_groups(campaign['id'],[['1']])
    args=(campaign['id'],'6669','Real Estate Post',
          'https://printitprints.com/shop/real-estate-post/',
          'fourover','https://4over.com/real-estate-post','Live price omits post hardware')
    result=manager.queue_supplemental_product(*args)
    assert result['total_products']==1
    assert result['active_group_product_ids']==['1']
    assert result['supplemental_product_jobs'][0]['stage']=='queued'
    assert len(manager.queue_supplemental_product(*args)['supplemental_product_jobs'])==1
    with pytest.raises(ValueError,match='already pinned'):
        manager.queue_supplemental_product(campaign['id'],'6669','Real Estate Post',
            'https://printitprints.com/shop/real-estate-post/',
            'fourover','https://4over.com/other/','Changed mapping')


def test_hold_product_keeps_group_open_and_records_exact_blocker(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    campaign=manager.create(name='Audit',products=[{'id':'1','title':'One',
        'storefront_url':'https://printitprints.com/shop/one/'},{'id':'2','title':'Two',
        'storefront_url':'https://printitprints.com/shop/two/'}])
    manager.configure_completion_groups(campaign['id'],[['1','2']])
    held=manager.hold_product(campaign['id'],'1','Supplier variant unavailable')
    assert held['products'][0]['stage']=='blocked'
    assert held['products'][0]['reason']=='Supplier variant unavailable'
    assert held['active_group_index']==0
    assert held['group_progress'][0]['held_count']==2
    assert manager.hold_product(campaign['id'],'1','Supplier variant unavailable')['counts']['blocked']==1


def test_complete_review_requires_full_coverage_and_matching_saved_rows(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    source='https://4over.com/brown-kraft-trading-cards'
    campaign=manager.create(name='Audit',products=[{'id':'1','title':'Cards',
        'storefront_url':'https://printitprints.com/shop/cards/',
        'supplier_url':source,'supplier':'fourover'}])
    product=campaign['products'][0]
    product.update(stage='extracted',extracted_at='today',diagnosis_id='a'*32,row_count=2)
    manager._write(campaign)
    review_id='b'*32
    folder=tmp_path/'finalizations'/review_id
    folder.mkdir(parents=True)
    report={'id':review_id,'diagnosis_id':'a'*32,'source_url':source,
            'catalog_complete':False,'examined_rows':2,'eligible_rows':2,
            'held_rows':0,'invalid_rows_removed':0,'hidden_choice_rows_omitted':0}
    (folder/'report.json').write_text(json.dumps(report))
    (folder/'finalized.csv').write_text('Subtotal\n1.00\n2.00\n')
    with pytest.raises(ValueError,match='complete eligible'):
        manager.attach_complete_review(campaign['id'],'1',review_id)
    assert manager.get(campaign['id'])['counts']['finalized']==0
    report['catalog_complete']=True
    report['pricing_model']='source_live_quote'
    (folder/'report.json').write_text(json.dumps(report))
    with pytest.raises(ValueError,match='separate runtime completion review'):
        manager.attach_complete_review(campaign['id'],'1',review_id)
    assert manager.get(campaign['id'])['counts']['finalized']==0
    report.pop('pricing_model')
    (folder/'report.json').write_text(json.dumps(report))
    (folder/'finalized.csv').write_text('Subtotal\n1.00\n')
    with pytest.raises(ValueError,match='CSV'):
        manager.attach_complete_review(campaign['id'],'1',review_id)
    (folder/'finalized.csv').write_text('Subtotal\n1.00\n2.00\n')
    result=manager.attach_complete_review(campaign['id'],'1',review_id)
    assert result['counts']['verified']==result['counts']['finalized']==1
    assert result['products'][0]['stage']=='finalized'
    assert manager.attach_complete_review(campaign['id'],'1',review_id)['counts']['finalized']==1


def test_complete_review_batch_is_atomic(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    products=[{'id':str(index),'title':f'Product {index}',
               'storefront_url':f'https://printitprints.com/shop/{index}/',
               'supplier_url':f'https://4over.com/product-{index}',
               'supplier':'fourover'} for index in (1,2)]
    campaign=manager.create(name='Batch',products=products)
    for product in campaign['products']:
        product.update(stage='extracted',extracted_at='today',diagnosis_id='a'*32,row_count=1)
    manager._write(campaign)
    reviews=[]
    for index in (1,2):
        review_id=str(index)*32
        folder=tmp_path/'finalizations'/review_id
        folder.mkdir(parents=True)
        (folder/'report.json').write_text(json.dumps({
            'id':review_id,'diagnosis_id':'a'*32,
            'source_url':f'https://4over.com/product-{index}',
            'catalog_complete':index==1,'examined_rows':1,'eligible_rows':1,
            'held_rows':0,'invalid_rows_removed':0,'hidden_choice_rows_omitted':0}))
        (folder/'finalized.csv').write_text('Subtotal\n1.00\n')
        reviews.append({'product_id':str(index),'review_id':review_id})
    with pytest.raises(ValueError,match='complete eligible'):
        manager.attach_complete_reviews_batch(campaign['id'],reviews)
    assert manager.get(campaign['id'])['counts']['finalized']==0
    report_path=tmp_path/'finalizations'/('2'*32)/'report.json'
    report=json.loads(report_path.read_text()); report['catalog_complete']=True
    report['pricing_model']='source_live_quote'
    report_path.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='separate runtime completion review'):
        manager.attach_complete_reviews_batch(campaign['id'],reviews)
    assert manager.get(campaign['id'])['counts']['finalized']==0
    report.pop('pricing_model')
    report_path.write_text(json.dumps(report))
    result=manager.attach_complete_reviews_batch(campaign['id'],reviews)
    assert result['counts']['finalized']==2
    assert len({row['finalized_at'] for row in result['products']})==1


def test_runtime_review_batch_is_atomic_and_has_no_fabricated_static_rows(tmp_path, monkeypatch):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    campaign=manager.create(name='Dynamic source',products=[{
        'id':str(index),'title':f'Product {index}',
        'storefront_url':f'https://printitprints.com/shop/{index}/',
        'supplier_url':f'https://www.b2sign.com/product-{index}',
        'supplier':'b2sign'} for index in (1,2)])
    for product in campaign['products']:
        product.update(stage='blocked',diagnosis_id='a'*32,source_collection_id='b'*32)
    manager._write(campaign)
    reviews=[{'product_id':str(index),'review_id':str(index)*32} for index in (1,2)]
    def validate(product, review_id):
        if product['id']=='2': raise ValueError('missing source choice')
        report={'source_url':product['supplier_url'],'source_contract_sha256':'c'*64}
        return b'{}',report,12
    monkeypatch.setattr(manager,'_validate_b2sign_runtime_review',validate)
    with pytest.raises(ValueError,match='missing source choice'):
        manager.attach_b2sign_runtime_reviews_batch(campaign['id'],reviews)
    assert manager.get(campaign['id'])['counts']['finalized']==0
    monkeypatch.setattr(manager,'_validate_b2sign_runtime_review',lambda product,review_id:(
        b'{}',{'source_url':product['supplier_url'],'source_contract_sha256':'c'*64},12))
    done=manager.attach_b2sign_runtime_reviews_batch(campaign['id'],reviews)
    assert done['counts']['finalized']==2
    assert all(p['row_count']==0 and p['finalization_evidence']['pricing_model']=='source_live_quote'
               and p['finalization_evidence']['storefront_deployed'] is False
               for p in done['products'])


def test_gator_runtime_review_recomputes_evidence_before_finalizing(tmp_path, monkeypatch):
    from crawler import fourover_gator_runtime_review

    manager = CampaignManager(SimpleNamespace(root=tmp_path))
    campaign = manager.create(name='Gator source', products=[{
        'id': '6742', 'title': 'Gator Board Signs',
        'storefront_url': 'https://printitprints.com/shop/gator/',
        'supplier_url': 'https://4over.com/gator-board-signs',
        'supplier': 'fourover'}])
    expected = {'schema': 'hermes-fourover-gator-runtime-review/v1',
                'source_url': 'https://4over.com/gator-board-signs',
                'option_map_sha256': 'a' * 64, 'dimension_grid_sha256': 'b' * 64,
                'canary_count': 8, 'source_data_complete': True,
                'pricing_model': 'source_live_quote'}
    monkeypatch.setattr(fourover_gator_runtime_review, 'inspect_gator_runtime',
                        lambda product, root, campaign_id: expected)
    review_id = 'c' * 32
    directory = tmp_path / 'finalizations' / review_id
    directory.mkdir(parents=True)
    report = dict(expected, id=review_id, canary_count=7)
    path = directory / 'report.json'
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match='no longer matches'):
        manager.attach_gator_runtime_review(campaign['id'], '6742', review_id)
    assert manager.get(campaign['id'])['counts']['finalized'] == 0
    report['canary_count'] = 8
    path.write_text(json.dumps(report))
    done = manager.attach_gator_runtime_review(campaign['id'], '6742', review_id)
    product = done['products'][0]
    assert product['stage'] == 'finalized'
    assert product['row_count'] == 0
    assert product['finalization_evidence']['static_rows_claimed'] is False
    assert product['finalization_evidence']['storefront_deployed'] is False


def test_completion_groups_hold_next_ten_until_every_current_product_is_final(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    campaign=manager.create(name='Groups',products=[{
        'id':str(index),'title':f'Product {index}',
        'storefront_url':f'https://printitprints.com/shop/{index}/'} for index in range(1,12)])
    assert [len(group) for group in campaign['completion_groups']]==[10,1]
    assert campaign['active_group_product_ids']==[str(index) for index in range(1,11)]
    with pytest.raises(ValueError,match='current product group'):
        manager.require_active_group(campaign['id'],'11')
    for product in campaign['products'][:9]: product['finalized_at']='today'
    manager._write(campaign)
    assert manager.get(campaign['id'])['active_group_index']==0
    campaign['products'][9]['finalized_at']='today'
    manager._write(campaign)
    assert manager.require_active_group(campaign['id'],'11')['active_group_index']==1


def test_excluded_member_is_not_counted_as_required_completion(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    campaign=manager.create(name='Groups',products=[{
        'id':str(index),'title':f'Product {index}',
        'storefront_url':f'https://printitprints.com/shop/{index}/'} for index in range(1,12)])
    campaign=manager.exclude_products(campaign['id'],[{'id':'10','reason':'Supplier product withdrawn'}])
    group=campaign['group_progress'][0]
    assert (group['product_count'],group['excluded_count'],group['required_count'],group['held_count']) == (10,1,9,9)
    for product in campaign['products'][:9]: product['finalized_at']='today'
    manager._write(campaign)
    result=manager.get(campaign['id'])
    assert result['active_group_index']==1
    assert result['group_progress'][0]['finalized_count']==9


def test_legacy_campaign_group_plan_requires_exact_partition(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    campaign=manager.create(name='Legacy',products=[{
        'id':str(index),'title':f'Product {index}',
        'storefront_url':f'https://printitprints.com/shop/{index}/'} for index in range(1,4)])
    campaign.pop('completion_groups');campaign.pop('active_group_index')
    manager._write(campaign)
    with pytest.raises(ValueError,match='partition'):
        manager.configure_completion_groups(campaign['id'],[['1','2']])
    result=manager.configure_completion_groups(campaign['id'],[['2','1'],['3']])
    assert result['active_group_product_ids']==['2','1']
    assert result['group_progress'][1]['status']=='queued'


def test_attached_supplier_diagnosis_is_exact_and_never_marks_prices_complete(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    source='https://4over.com/brown-kraft-bookmarks'
    row=manager.create(name='Audit',products=[{'id':'1','title':'One',
        'storefront_url':'https://printitprints.com/shop/one/','supplier_url':source}])
    diagnosis_id='a'*32
    path=tmp_path/'diagnoses'/f'{diagnosis_id}.json';path.parent.mkdir()
    path.write_text(json.dumps({'id':diagnosis_id,'url':source,'status':'ready',
        'fields':[{'configurable':True},{'configurable':False}],'warnings':[]}))
    attached=manager.attach_diagnosis(row['id'],'1',diagnosis_id)
    assert attached['products'][0]['diagnosis_field_count']==1
    assert attached['counts']['extracted']==attached['counts']['verified']==attached['counts']['finalized']==0
    path.write_text(json.dumps({'id':diagnosis_id,'url':'https://4over.com/other','status':'ready','fields':[]}))
    with pytest.raises(ValueError,match='exactly match'):
        manager.attach_diagnosis(row['id'],'1',diagnosis_id)


def test_fourover_default_probe_stays_scoped_and_does_not_finalize(tmp_path):
    class Store:
        root=tmp_path
        def put_object(self, body, suffix):
            path=tmp_path/'objects'/('probe.'+suffix);path.parent.mkdir(exist_ok=True)
            path.write_bytes(body)
            return str(path.relative_to(tmp_path))
    manager=CampaignManager(Store())
    url='https://4over.com/brown-kraft-bookmarks'
    campaign=manager.create(name='Audit',products=[{'id':'1','title':'Bookmark',
        'storefront_url':'https://printitprints.com/shop/bookmark/','supplier':'fourover',
        'supplier_url':url}])
    probe={'status':'rendered_snapshot','source_url':url,
           'observation':{'title':'Bookmarks','fields':[{'label':'Size','selected':'337'}]},
           'price_route_shapes':[{'path':'/rest/default/V1/product/prices/7837',
                                  'package_prices':[{'quantity':100,'price':23.81}],
                                  'tier_prices':[]}]}
    saved=manager.attach_fourover_default_probe(campaign['id'],'1',probe)
    assert saved['tier_count']==1
    current=manager.get(campaign['id'])
    assert current['fourover_default_probes_collected']==1
    assert current['counts']['extracted']==current['counts']['verified']==current['counts']['finalized']==0


def test_campaign_rejects_duplicate_products_and_private_url_shapes(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    p={'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}
    with pytest.raises(ValueError):manager.create(name='Duplicate',products=[p,p])
    with pytest.raises(ValueError):manager.create(name='Private',products=[dict(p,storefront_url='http://localhost/product')])


def test_interrupted_work_remains_visible(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    row=manager.create(name='Audit',products=[{'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}])
    row['status']='running';manager._write(row)
    assert manager.get(row['id'])['status']=='interrupted'


def test_identity_primary_not_related_and_empty_options_preserved():
    url='https://printitprints.com/shop/one/'
    wrong='<body class="postid-2"><form class="cart"><input name="add-to-cart" value="1"></form></body>'
    assert not inspect_storefront(wrong,url,'1')['identity_confirmed']
    primary='<body><div class="product" id="product-1"><section class="related products"><div class="product" id="product-2"></div></section></div></body>'
    assert inspect_storefront(primary,url,'1')['identity_confirmed']
    related='<body><section class="related products"><div class="product" id="product-1"></div></section></body>'
    assert not inspect_storefront(related,url,'1')['identity_confirmed']
    source='<body class="postid-1"><form class="cart"><select><option value="">None</option><option>Placeholder</option></select></form>'
    assert inspect_storefront(source,url,'1')['fields'][0]['choices']==[{'value':'','label':'None','disabled':False}]


def test_hardware_storefront_prices_scoped_not_supplier_verified():
    import json
    url='https://printitprints.com/shop/hardware/'
    retail={'display_price':18.25,'display_regular_price':20,'piso_retail_bands':[{'min':1,'max':9,'unit':18.25},{'min':10,'max':None,'unit':16}], 'private_extra':'DO_NOT_COPY','price_html':'<span>neverExecute()</span>'}
    offers={'@graph':[
        {'@type':'Product','@id':url+'#product','offers':{'@type':'Offer','url':url,'price':'18.25','priceCurrency':'USD','secret':'DO_NOT_COPY'}},
        {'@type':'Product','@id':'https://printitprints.com/shop/unrelated/#product','offers':{'price':'999','priceCurrency':'USD'}},
        {'@type':'Product','@id':url+'#product','offers':{'url':'https://printitprints.com/shop/unrelated/','price':'555'}},
    ]}
    html='<body class="postid-1"><div class="product product-type-simple" id="product-1"><h1 class="product_title">Hardware</h1><div class="summary"><span class="price">$18.25</span></div><form class="cart"></form></div><section class="related products"><div class="product"><div class="summary"><span class="price">$999</span></div></div></section>'
    html+='<script type="application/ld+json">'+json.dumps(offers)+'</script>'
    html+='<script id="print-it-supplier-options-js">(function($){\n\'use strict\';\nif (!$) return;\nvar simpleRetail='+json.dumps(retail)+'; function init(){} })(jQuery);</script></body>'
    evidence=inspect_storefront(html,url,'1');prices=evidence['storefront_prices']
    assert evidence['identity_confirmed'] and evidence['fields']==[]
    assert prices['displayed_prices']==['$18.25']
    assert prices['product_offers']==[{'source':'product_jsonld','type':'Offer','price':'18.25','currency':'USD'}]
    assert prices['simple_retail'][0]['piso_retail_bands'][-1]=={'min':10,'max':None,'unit':'16'}
    assert prices['simple_retail'][0]['display_price']=='18.25'
    assert 'DO_NOT_COPY' not in json.dumps(prices) and 'neverExecute' not in json.dumps(prices)
    assert prices['scope']=='storefront_only' and not prices['supplier_verified'] and not evidence['prices_verified']


def test_executable_or_unbound_retail_is_not_accepted():
    script='<script id="print-it-supplier-options-js">(function($){\'use strict\';if (!$) return;var simpleRetail={"display_price":18}+danger();})(jQuery);</script>'
    result=inspect_storefront('<body class="postid-1">'+script,'https://printitprints.com/shop/one/','1')
    assert result['storefront_prices']['simple_retail']==[]
    assert 'unsupported_simple_retail' in result['storefront_prices']['warnings']
    mismatch=inspect_storefront('<body class="postid-2">'+script,'https://printitprints.com/shop/one/','1')
    assert mismatch['storefront_prices']['warnings']==['storefront_identity_unconfirmed']


def test_mapping_updates_atomic_and_cannot_promote_milestones(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    row=manager.create(name='Audit',products=[{'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}]);identifier=row['id']
    out=manager.update_mappings(identifier,[{'id':'1','supplier':'B2Sign','supplier_url':'https://www.b2sign.com/hardware','mapping_evidence':[{'source':'catalog','id':'bound'}]}])
    assert out['products'][0]['supplier']=='B2Sign' and out['products'][0]['stage']=='queued'
    for patch in [[{'id':'1','verified_at':'today'}],[{'id':'1','supplier':'Changed'},{'id':'2','supplier':'Unknown'}],[{'id':'1','supplier_url':'https://127.0.0.1/private'}],[{'id':'1','supplier_url':'https://user:password@example.com'}],[{'id':'1','supplier':'Changed'},{'id':'1','supplier':'Duplicate'}]]:
        with pytest.raises(ValueError):manager.update_mappings(identifier,patch)
        assert manager.get(identifier)['products'][0]['supplier']=='B2Sign'
    manager.tasks[identifier]=object()
    with pytest.raises(ValueError,match='active'):manager.update_mappings(identifier,[])


def test_source_rebinding_discards_old_source_evidence_and_blocks_finalized_rebinding(tmp_path):
    manager=CampaignManager(SimpleNamespace(root=tmp_path))
    campaign=manager.create(name='Source change',products=[{'id':'1','title':'One',
        'storefront_url':'https://printitprints.com/shop/one/',
        'supplier_url':'https://soardistusa.com/products/old','supplier':'soardist'}])
    product=campaign['products'][0]
    product.update(stage='blocked',diagnosed_at='today',diagnosis_id='a'*32,
                   public_variant_evidence={'source_url':product['supplier_url']},
                   account_baseline_evidence={'comparison':'matched_public_variant'},
                   itoris_option_evidence={'source_url':product['supplier_url']},row_count=12)
    manager._write(campaign)
    changed=manager.update_mappings(campaign['id'],[{'id':'1',
        'supplier_url':'https://soardistusa.com/products/new'}])
    current=changed['products'][0]
    assert current['stage']=='queued' and current['row_count']==0
    assert current['source_mapping_history'][0]['supplier_url']=='https://soardistusa.com/products/old'
    for key in ('diagnosis_id','diagnosed_at','public_variant_evidence',
                'account_baseline_evidence','itoris_option_evidence'):
        assert key not in current
    current['stage']='finalized';current['finalized_at']='today'
    manager._write(changed)
    with pytest.raises(ValueError,match='finalized'):
        manager.update_mappings(campaign['id'],[{'id':'1',
            'supplier_url':'https://soardistusa.com/products/another'}])


def _local_store(tmp_path):
    import hashlib
    def put_object(body,suffix):
        digest=hashlib.sha256(body).hexdigest();key=f'objects/{digest[:2]}/{digest}.{suffix}'
        path=tmp_path/key;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body);return key
    return SimpleNamespace(root=tmp_path,put_object=put_object)


def test_offline_reinspection_no_requests_or_new_milestones(tmp_path,monkeypatch):
    import hashlib
    monkeypatch.setattr('crawler.campaign_progress.httpx.AsyncClient',lambda **kwargs:pytest.fail('Offline reinspection cannot create HTTP client'))
    store=_local_store(tmp_path);manager=CampaignManager(store)
    row=manager.create(name='Audit',products=[{'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}])
    body=b'<body class="postid-2"><form class="cart"><input name="add-to-cart" value="1"></form></body>'
    saved={'identity_confirmed':True,'object_path':store.put_object(body,'response'),'sha256':hashlib.sha256(body).hexdigest(),'source_url':row['products'][0]['storefront_url'],'observed_at':'original-date'}
    row['products'][0].update(storefront_evidence=saved,stage='diagnosed',diagnosed_at='original-date',row_count=100);manager._write(row)
    out=manager.reinspect_saved(row['id'])
    assert out['products'][0]['stage']=='blocked' and out['products'][0]['row_count']==100
    assert out['products'][0]['storefront_evidence']['observed_at']=='original-date'
    assert out['counts']=={'diagnosed':0,'extracted':0,'verified':0,'finalized':0,'blocked':1}
    manager.tasks[row['id']]=object()
    with pytest.raises(ValueError,match='active'):manager.reinspect_saved(row['id'])


@pytest.mark.asyncio
async def test_explicit_retry_retains_attempt_history_and_never_auto_retries(tmp_path,monkeypatch):
    import httpx
    from urllib.parse import urlparse
    from unittest.mock import AsyncMock
    from crawler import campaign_progress as module
    calls=[];status=[503]
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status[0],content=b'<body class="postid-1"><h1 class="product_title">One</h1></body>',headers={'retry-after':'5'})
    client=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(module,'validate_url',AsyncMock(side_effect=lambda url:urlparse(url)))
    manager=CampaignManager(_local_store(tmp_path));manager.limiter=SimpleNamespace(acquire=AsyncMock(return_value='permit'),release=lambda *args:None)
    row=manager.create(name='Audit',products=[{'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}])
    await manager.analyze(row['id']);await manager.tasks[row['id']]
    first=manager.get(row['id'])
    assert len(calls)==1 and first['counts']['diagnosed']==0
    assert first['status']=='needs_work' and first['stop_reason']=='Storefront HTTP 503'
    assert first['products'][0]['analysis_attempts'][0]['status']=='blocked'
    status[0]=200
    await manager.analyze(row['id']);await manager.tasks[row['id']]
    second=manager.get(row['id']);attempts=second['products'][0]['analysis_attempts']
    assert len(calls)==2 and second['counts']['diagnosed']==1
    assert second['counts']['verified']==second['counts']['finalized']==0
    assert [a['status'] for a in attempts]==['blocked','diagnosed'] and attempts[0]['reason']=='Storefront HTTP 503'
    assert 'stop_reason' not in second
    await manager.analyze(row['id']);await manager.tasks[row['id']]
    assert len(calls)==2


@pytest.mark.asyncio
async def test_identity_failure_not_counted_diagnosed(tmp_path,monkeypatch):
    import httpx
    from urllib.parse import urlparse
    from unittest.mock import AsyncMock
    from crawler import campaign_progress as module
    client=httpx.AsyncClient
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(lambda request:httpx.Response(200,content=b'<body class="postid-2"><form class="cart"><input name="add-to-cart" value="1"></form></body>'))))
    monkeypatch.setattr(module,'validate_url',AsyncMock(side_effect=lambda url:urlparse(url)))
    manager=CampaignManager(_local_store(tmp_path));manager.limiter=SimpleNamespace(acquire=AsyncMock(return_value='permit'),release=lambda *args:None)
    row=manager.create(name='Audit',products=[{'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}])
    await manager.analyze(row['id']);await manager.tasks[row['id']]
    result=manager.get(row['id'])
    assert result['counts']['diagnosed']==0 and result['counts']['blocked']==1
    assert 'diagnosed_at' not in result['products'][0]


def test_source_page_attachment_requires_exact_completed_evidence_and_never_promotes(tmp_path):
    import json,hashlib
    store=_local_store(tmp_path);manager=CampaignManager(store)
    source='https://www.b2sign.com/one'
    campaign=manager.create(name='Audit',products=[{'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/','supplier_url':source,'supplier':'B2Sign'}])
    envelope={'requested_url':source,'url':source,'status':200,'captured_at':'2026-09-27T01:00:00Z','stored_body':'public_projection_only_raw_envelope_not_retained','public_data':{'data_scope':'public_page_metadata_only','prices_verified':False}}
    body=json.dumps(envelope).encode();key=store.put_object(body,'public-source.json')
    evidence={k:v for k,v in envelope.items() if k!='public_data'}|{'object_path':key,'sha256':hashlib.sha256(body).hexdigest()}
    job_id='a'*32;job={'schema':'hermes-public-source-collection/v1','id':job_id,'status':'running','pages':[{'url':source,'status':'completed','evidence':evidence}]}
    target=tmp_path/'source-collections'/f'{job_id}.json';target.parent.mkdir();target.write_text(json.dumps(job))
    out=manager.attach_collection(campaign['id'],'1',job_id,source)
    assert out['source_pages_collected']==1
    assert out['counts']=={'diagnosed':0,'extracted':0,'verified':0,'finalized':0,'blocked':0}
    assert out['products'][0]['stage']=='queued' and out['products'][0]['row_count']==0
    assert out['products'][0]['source_collection_evidence']['object_path']==key
    manager.attach_collection(campaign['id'],'1',job_id,source)
    assert not manager.get(campaign['id'])['products'][0].get('source_collection_history')
    with pytest.raises(ValueError,match='exactly'):manager.attach_collection(campaign['id'],'1',job_id,source+'/different')
    job['pages'][0]['status']='failed';target.write_text(json.dumps(job))
    with pytest.raises(ValueError,match='not completed'):manager.attach_collection(campaign['id'],'1',job_id,source)
    job['pages'][0]['status']='completed';target.write_text(json.dumps(job))
    (tmp_path/key).write_text('{}')
    with pytest.raises(ValueError,match='hash'):manager.attach_collection(campaign['id'],'1',job_id,source)
    updated=manager.update_mappings(campaign['id'],[{'id':'1','supplier_url':'https://www.b2sign.com/replacement'}])
    assert updated['source_pages_collected']==0 and 'source_collection_id' not in updated['products'][0]
    assert updated['products'][0]['source_collection_history'][0]['source_collection_id']==job_id


def test_explicit_exclusions_keep_89_records_but_count_87_active_and_77_sources(tmp_path):
    manager=CampaignManager(_local_store(tmp_path))
    ids=['315779','21571']+[str(1000+n) for n in range(87)]
    products=[{'id':pid,'title':pid,'storefront_url':f'https://printitprints.com/shop/{pid}/'} for pid in ids]
    row=manager.create(name='Original 89',products=products)
    for index,p in enumerate(row['products']):
        p.update(stage='diagnosed',diagnosed_at='original-date',row_count=index+1)
        if 1 <= index <= 78:
            url=f'https://www.b2sign.com/item-{p["id"]}'
            p.update(supplier='b2sign',supplier_url=url,source_collection_id='a'*32,source_collection_status='page_collected',source_collection_evidence={'requested_url':url})
    manager._write(row)
    assert manager.get(row['id'])['source_pages_collected']==78
    reasons=[{'id':'315779','reason':'Existing hero DTF product; excluded by user request.'},
             {'id':'21571','reason':'Sample kits are outside this project.'}]
    result=manager.exclude_products(row['id'],reasons)
    assert result['total_products']==result['original_total_products']==89
    assert result['active_products']==87 and result['excluded_products']==2
    assert result['source_pages_collected']==77 and result['counts']['diagnosed']==87
    assert result['counts']['extracted']==result['counts']['verified']==result['counts']['finalized']==0
    assert len(result['products'])==89
    for index,p in enumerate(result['products'][:2]):
        assert p['stage']=='excluded' and p['reason']==reasons[index]['reason']
        assert p['exclusion_reason']==reasons[index]['reason'] and p['excluded_at']
        assert p['stage_before_exclusion']=='diagnosed' and p['row_count']==index+1
        assert p['diagnosed_at']=='original-date'
    assert result['products'][1]['source_collection_id']=='a'*32
    original_bytes=manager._path(row['id']).read_bytes()
    manager.exclude_products(row['id'],reasons)
    assert manager._path(row['id']).read_bytes()==original_bytes
    reloaded=CampaignManager(_local_store(tmp_path)).get(row['id'])
    assert reloaded['active_products']==87 and reloaded['products'][1]['stage']=='excluded'
    assert manager.list()[0]['excluded_products']==2


def test_exclusion_validation_atomic_and_existing_mutators_do_not_reactivate(tmp_path):
    manager=CampaignManager(_local_store(tmp_path))
    p={'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}
    row=manager.create(name='Audit',products=[p,dict(p,id='2')]);identifier=row['id']
    for items in ([{'id':'1','reason':'Out of scope'},{'id':'unknown','reason':'x'}],
                  [{'id':'1','reason':'Out of scope'},{'id':'1','reason':'duplicate'}],
                  [{'id':'1','reason':' '}],[{'id':'1','reason':'x','stage':'finalized'}]):
        with pytest.raises(ValueError):manager.exclude_products(identifier,items)
        assert manager.get(identifier)['excluded_products']==0
    manager.tasks[identifier]=object()
    with pytest.raises(ValueError,match='active'):manager.exclude_products(identifier,[{'id':'1','reason':'x'}])
    manager.tasks.clear()
    manager.exclude_products(identifier,[{'id':'1','reason':'Out of scope'}])
    with pytest.raises(ValueError,match='Excluded'):
        manager.update_mappings(identifier,[{'id':'2','supplier':'Must not commit'},{'id':'1','supplier':'Attempted replacement'}])
    with pytest.raises(ValueError,match='Excluded'):
        manager.attach_collection(identifier,'1','a'*32,'https://www.b2sign.com/one')
    assert manager.get(identifier)['products'][1]['supplier'] is None
    # Even invalid old evidence must not reclassify or reprocess an exclusion.
    row=manager.get(identifier);row['products'][0]['storefront_evidence']={'object_path':'invalid'};manager._write(row)
    out=manager.reinspect_saved(identifier)
    assert out['products'][0]['stage']=='excluded' and out['products'][0]['reason']=='Out of scope'
    assert out['products'][0]['storefront_evidence']=={'object_path':'invalid'}


@pytest.mark.asyncio
async def test_analysis_skips_excluded_records_even_without_diagnosis(tmp_path,monkeypatch):
    import httpx
    from crawler import campaign_progress as module
    client=httpx.AsyncClient
    def no_requests(request):pytest.fail('Excluded products must never be requested')
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kwargs:client(transport=httpx.MockTransport(no_requests)))
    manager=CampaignManager(_local_store(tmp_path))
    row=manager.create(name='Excluded',products=[{'id':'1','title':'One','storefront_url':'https://printitprints.com/shop/one/'}])
    manager.exclude_products(row['id'],[{'id':'1','reason':'User removed scope'}])
    await manager.analyze(row['id']);await manager.tasks[row['id']]
    out=manager.get(row['id']);p=out['products'][0]
    assert p['stage']=='excluded' and p['reason']=='User removed scope'
    assert not p.get('analysis_attempts') and not p.get('diagnosed_at')
    assert out['active_products']==0 and out['counts']['finalized']==0
