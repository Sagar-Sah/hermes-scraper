"""Saved table export is evidence only; malformed and zero-attempt states stay explicit."""
import csv
import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from crawler.source_domain_report import export_stage


def fixture(tmp_path, *, empty=False):
    root=tmp_path/'data';(root/'objects').mkdir(parents=True)
    db=tmp_path/'source.db';c=sqlite3.connect(db)
    c.executescript('''CREATE TABLE source_domain_jobs(id TEXT,run_id TEXT,scope TEXT,status TEXT,used INTEGER,budget INTEGER);
    CREATE TABLE source_domain_tasks(id TEXT,job_id TEXT,request_key TEXT,request TEXT,status TEXT,response_id TEXT,products TEXT);
    CREATE TABLE source_domain_attempts(job_id TEXT,phase TEXT,public_url TEXT,status INTEGER);
    CREATE TABLE responses(id TEXT,status INTEGER,url TEXT,object_path TEXT);
    CREATE TABLE products(id TEXT,title TEXT,url TEXT);''')
    c.execute('INSERT INTO source_domain_jobs VALUES(?,?,?,?,?,?)',('job','run','Roll fixture','paused' if empty else 'running',0 if empty else 3,10))
    c.execute('INSERT INTO products VALUES(?,?,?)',('product-1','Roll Labels','https://fixture.invalid/roll'))
    if empty:
        c.execute('INSERT INTO source_domain_tasks VALUES(?,?,?,?,?,?,?)',('pending','job','key','{}','pending',None,json.dumps(['product-1'])))
    else:
        for name,path,payload in [
            ('base','/service/rest/v1/products/123/prices',{'items':[{'quantity':100,'markupPrice':'1,234.50'},{'quantity':250,'markupPrice':'1300.00'}]}),
            ('catalog','/service/rest/v1/products/123/options',{'items':[{'id':456,'type':{'id':32}}]}),
        ]:
            object_path=root/'objects'/f'{name}.response'
            content=json.dumps(payload).encode()
            object_path.write_bytes(gzip.compress(content) if name=='catalog' else content)
            request={'method':'GET','path':path,'params':{},'body':None,'context':{'origin':'https://www.printograph.com','currency':'USD','session_scope':'MUST-NOT-EXPORT'}}
            c.execute('INSERT INTO source_domain_tasks VALUES(?,?,?,?,?,?,?)',(name,'job',name,json.dumps(request),'succeeded',name,json.dumps(['product-1'])))
            c.execute('INSERT INTO responses VALUES(?,?,?,?)',(name,200,'https://www.printograph.com'+path,str(object_path)))
            c.execute('INSERT INTO source_domain_attempts VALUES(?,?,?,?)',('job','domain','https://www.printograph.com'+path,200))
        c.execute('INSERT INTO source_domain_attempts VALUES(?,?,?,?)',('job','domain','https://www.printograph.com/service/rest/v1/products/123/prices',503))
    c.commit();c.close()
    return db,root,tmp_path/'export'


def rows(output):
    with (output/'base-price-evidence.csv').open(newline='') as f:return list(csv.DictReader(f))


def test_streamed_evidence_keeps_raw_price_currency_provenance_and_retry_denominator(tmp_path):
    db,root,output=fixture(tmp_path);before=db.read_bytes()
    result=export_stage(db,root,'job',output)
    assert db.read_bytes()==before
    assert rows(output)==[
        {'source_product_id':'123','quantity':'100','raw_markup_price':'1,234.50','currency':'USD','response_evidence_id':'base','product_ids':'["product-1"]','product_titles':'["Roll Labels"]','product_urls':'["https://fixture.invalid/roll"]'},
        {'source_product_id':'123','quantity':'250','raw_markup_price':'1300.00','currency':'USD','response_evidence_id':'base','product_ids':'["product-1"]','product_titles':'["Roll Labels"]','product_urls':'["https://fixture.invalid/roll"]'}]
    assert result['exported_base_price_entries']==2
    assert result['base_price_entries_per_source_attempt']==2/3
    assert result['base_price_entries_per_base_price_attempt']==1
    assert result['option_catalog_entry_count']==1
    assert result['families']['option_catalogs']['distinct_full_semantic_response_bodies']==1
    assert result['verified'] is False and result['product_rows_inserted']==0
    assert result['final_configuration_count'] is None
    assert 'MUST-NOT-EXPORT' not in (output/'source-domain-stage-report.json').read_text()
    again=export_stage(db,root,'job',output)
    assert again['exported_base_price_entries']==2 and len(rows(output))==2  # replace, never append duplicates


def test_zero_http_checkpoint_is_not_a_successful_collection(tmp_path):
    db,root,output=fixture(tmp_path,empty=True)
    result=export_stage(db,root,'job',output)
    assert result['observation_status']=='no_source_attempts'
    assert result['succeeded_unique_requests']==0
    assert result['base_price_entries_per_source_attempt'] is None
    assert rows(output)==[]
    assert 'zero-attempt checkpoint, not a successful' in (output/'source-domain-stage-report.md').read_text()


def test_unknown_price_shapes_and_duplicate_tiers_are_diagnostics_not_invented_prices(tmp_path):
    db,root,output=fixture(tmp_path)
    (root/'objects/base.response').write_text(json.dumps({'items':[
        {'quantity':100,'markupPrice':'2.00'}, {'quantity':100,'markupPrice':'3.00'},
        {'quantity':250,'markupPrice':None}, {'quantity':'unknown','markupPrice':'5.00'},
        {'quantity':500,'markupPrice':'=HYPERLINK("external")'}, ['unexpected']]}))
    result=export_stage(db,root,'job',output)
    assert len(rows(output))==2
    assert result['quantity_tier_entry_count']==4
    assert result['diagnostics']['codes']=={'duplicate_quantity_in_base_table':1,'unsupported_quantity_or_markup_price':3,'unsupported_base_price_entry':1}
    diagnostics=[json.loads(x) for x in (output/'base-price-diagnostics.jsonl').read_text().splitlines()]
    assert len(diagnostics)==5 and all(x['response_evidence_id']=='base' for x in diagnostics)


@pytest.mark.parametrize('issue',['scope','outside','unknown_currency','items'])
def test_missing_scope_and_unsupported_bodies_remain_explicit(tmp_path,issue):
    db,root,output=fixture(tmp_path);c=sqlite3.connect(db)
    if issue=='scope':c.execute("UPDATE responses SET url='https://www.printograph.com/service/rest/v1/products/999/prices' WHERE id='base'")
    elif issue=='outside':c.execute("UPDATE responses SET object_path=? WHERE id='base'",(str(tmp_path/'private'),))
    elif issue=='unknown_currency':
        request=json.loads(c.execute("SELECT request FROM source_domain_tasks WHERE id='base'").fetchone()[0]);request['context'].pop('currency')
        c.execute("UPDATE source_domain_tasks SET request=? WHERE id='base'",(json.dumps(request),))
    else:(root/'objects/base.response').write_text('{"items":null}')
    c.commit();c.close()
    result=export_stage(db,root,'job',output)
    assert result['diagnostics']['count']==1
    if issue=='unknown_currency':assert all(x['currency']=='' for x in rows(output)) and len(rows(output))==2
    else:assert rows(output)==[]


def test_unknown_job_does_not_replace_existing_evidence(tmp_path):
    db,root,output=fixture(tmp_path);export_stage(db,root,'job',output)
    original=(output/'base-price-evidence.csv').read_bytes()
    with pytest.raises(LookupError):export_stage(db,root,'missing',output)
    assert (output/'base-price-evidence.csv').read_bytes()==original


def test_shared_evidence_has_explicit_mixed_attribution_without_duplicate_csv_rows(tmp_path):
    db,root,output=fixture(tmp_path);c=sqlite3.connect(db)
    c.execute('INSERT INTO products VALUES(?,?,?)',('product-2','Club Flyers','https://fixture.invalid/club'))
    c.execute("UPDATE products SET title='Business Cards' WHERE id='product-1'")
    c.execute("UPDATE source_domain_tasks SET products=? WHERE id='base'",(json.dumps(['product-2','product-1','product-2']),))
    c.commit();c.close()
    result=export_stage(db,root,'job',output)
    assert len(rows(output))==2
    assert json.loads(rows(output)[0]['product_ids'])==['product-1','product-2']
    products={p['product_id']:p for p in result['products']}
    assert products['product-1']['title']=='Business Cards'
    assert products['product-1']['base_price_entries']==products['product-2']['base_price_entries']==2
    assert products['product-1']['option_catalog_entries']==1
    assert products['product-2']['option_catalog_entries']==0
    assert result['exported_base_price_entries']==2
    assert 'can overlap' in result['product_attribution_scope']


def test_lookup_is_valid_evidence_but_not_a_base_price_csv_entry(tmp_path):
    db,root,output=fixture(tmp_path);c=sqlite3.connect(db)
    path='/service/rest/v1/products'
    request={'method':'GET','path':path,'params':{'productType':'36'},'body':None,'context':{'origin':'https://www.printograph.com','currency':'USD'}}
    (root/'objects/lookup.response').write_text('{"items":[{"id":123}]}')
    c.execute('INSERT INTO responses VALUES(?,?,?,?)',('lookup',200,'https://www.printograph.com'+path+'?productType=36',str(root/'objects/lookup.response')))
    c.execute('INSERT INTO source_domain_tasks VALUES(?,?,?,?,?,?,?)',('lookup','job','lookup',json.dumps(request),'succeeded','lookup',json.dumps(['product-1'])))
    c.commit();c.close()
    result=export_stage(db,root,'job',output)
    assert result['families']['product_lookup_or_listing']['succeeded_tasks']==1
    assert result['diagnostics']['codes']=={'response_family_not_exported':1}
    assert result['exported_base_price_entries']==2
    assert result['products'][0]['succeeded_unique_requests']==3


def test_explicit_legacy_prefix_and_path_traversal_guard(tmp_path):
    db,root,output=fixture(tmp_path)
    result=export_stage(db,root,'job',output,filename_prefix='roll-labels-base-price')
    assert result['files']['csv']=='roll-labels-base-price-evidence.csv'
    assert (output/result['files']['csv']).is_file()
    with pytest.raises(ValueError):export_stage(db,root,'job',output,filename_prefix='../outside')


def test_option_tiers_are_counted_separately_without_exporting_as_base_prices(tmp_path):
    db,root,output=fixture(tmp_path);c=sqlite3.connect(db)
    path='/service/rest/v1/products/123/options/456/prices'
    request={'method':'GET','path':path,'params':{},'body':None,
             'context':{'origin':'https://www.printograph.com','currency':'USD'}}
    (root/'objects/option.response').write_text(json.dumps({'items':[
        {'quantity':25,'markupPrice':'4.00'}, {'quantity':50,'markupPrice':'4.00'},
        {'quantity':100,'markupPrice':None}]}))
    c.execute('INSERT INTO responses VALUES(?,?,?,?)',('option',200,'https://www.printograph.com'+path,str(root/'objects/option.response')))
    c.execute('INSERT INTO source_domain_tasks VALUES(?,?,?,?,?,?,?)',('option','job','option',json.dumps(request),'succeeded','option',json.dumps(['product-1'])))
    c.execute('INSERT INTO source_domain_attempts VALUES(?,?,?,?)',('job','domain','https://www.printograph.com'+path,200))
    c.execute('UPDATE source_domain_jobs SET used=4')
    c.commit();c.close()
    result=export_stage(db,root,'job',output)
    assert len(rows(output))==2 and result['exported_base_price_entries']==2
    assert result['option_price_entry_count']==2
    assert result['quantity_tier_entry_count']==5
    assert result['products'][0]['option_price_entries']==2
    assert result['families']['option_price_tables']['supported_response_bodies']==1
    assert result['families']['option_price_tables']['distinct_source_product_ids']==1
    assert result['diagnostics']['codes']=={'unsupported_quantity_or_markup_price':1}
