"""Self-contained 705-child source corpus; no saved account/supplier data."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json

from crawler.campaign_progress import CampaignManager
from crawler.config import Settings
from crawler.fourover_child_prices import FourOverChildPriceCollector, _encoded
from crawler.fourover_collected_evidence import SOURCE, PRODUCT, CHILD, SCOPE, VALUES, digest
from crawler.fourover_options import inspect_option_contract
from crawler.store import Store

SNAPSHOT = 'a'*32
CAMPAIGN = 'native-observer-fixture'
PRIVATE = 'PRIVATE-ACCOUNT-FORM-KEY-NEVER-PERSIST'


def document(children, config):
    declared = {child['child_id']:{'sku':child['sku']} for child in children}
    return ('<script type="text/x-magento-init">'+json.dumps({'*':{
        'Magento_Ui/js/core/app':{'components':{'child-sku':{'childSkuConfig':declared}}}}})+'</script>'+
        '<script type="text/x-magento-init">'+json.dumps({'#product_addtocart_form':{
            'configurable':{'spConfig':{**config,'accountToken':PRIVATE}}}})+'</script>')


def build(root):
    store=Store(Settings(data_dir=Path(root),database_url='sqlite:///'+str(Path(root)/'atlas.db'),s3_bucket=''))
    ids=[CHILD]+[str(200000+i) for i in range(704)]
    children=[{'child_id':child,'sku':'PUBLIC-FIXTURE-'+child} for child in ids]
    attrs={};index={child:dict(VALUES) for child in ids}
    for offset,child in enumerate(ids[1:],1):index[child]['211']=str(100000+offset)
    for key,value in VALUES.items():
        if key=='211':options=[{'id':index[child][key],'label':'Size '+child,'products':[child]} for child in ids]
        else:options=[{'id':value,'label':'Choice '+key,'products':ids}]
        attrs[key]={'id':key,'code':'attr_'+key,'label':'Attribute '+key,'options':options}
    config={'attributes':attrs,'index':index}
    html=document(children,config)
    contract=inspect_option_contract(html,children,source_url=SOURCE)
    assert contract['binding_complete'] and len(contract['rows'])==705
    observations={};objects={}
    for offset,child in enumerate(ids):
        count=19 if offset<120 else 18
        package=[{'quantity':500,'price':'12.340001'}]+[{'quantity':n,'price':str(20+n)+'.00'} for n in range(1,count)]
        value={'source_url':SOURCE,'child_id':child,'scope':SCOPE,'package_prices':package,
               'tier_prices':[],'captured_at':'2026-10-22T00:00:00+00:00'}
        raw=_encoded(value);path=store.put_object(raw,'fourover-child-price.json');sha=digest(raw)
        observations[child]={'child_id':child,'status':'collected','sha256':sha,'object_path':path}
        objects[child]=value
    job={'schema':'hermes-fourover-child-price-collection/v1','snapshot_id':SNAPSHOT,
         'source_url':SOURCE,'scope':SCOPE,'status':'completed_requested_subset','error':None,
         'declared_children':children,'option_contract':contract,'observations':observations,
         'document_body_sha256':digest(html.encode()),'option_contract_document_sha256':digest(html.encode())}
    reader=FourOverChildPriceCollector(store,None);path=reader._path(SNAPSHOT,SOURCE);reader._save(path,job)
    manager=CampaignManager(store)
    data=manager.create(id=CAMPAIGN,name='Fixture',products=[
        {'id':PRODUCT,'title':'Z Fold','supplier':'fourover','supplier_url':SOURCE,
         'storefront_url':'https://printitprints.com/shop/fixture/'},
        {'id':'999','title':'Other','storefront_url':'https://printitprints.com/shop/other/'}])
    manager.configure_completion_groups(CAMPAIGN,[[PRODUCT,'999']])
    return SimpleNamespace(store=store,manager=manager,job=job,path=path,objects=objects,
        html=html,checkpoint=digest(path.read_bytes()),rows=digest(_encoded(contract['rows'])))


def rewrite_job(env):
    FourOverChildPriceCollector(env.store,None)._save(env.path,env.job)
    env.checkpoint=digest(env.path.read_bytes())
    env.rows=digest(_encoded(env.job['option_contract']['rows']))


def rewrite_child(env,mutate,child=CHILD):
    value=deepcopy(env.objects[child]);mutate(value)
    raw=_encoded(value);path=env.store.put_object(raw,'fourover-child-price.json')
    env.job['observations'][child].update(object_path=path,sha256=digest(raw))
    rewrite_job(env)


def state():
    return {'attributes':{key:{'count':1,'value':value,'enabled':True,'visible':True,'choice_enabled':True} for key,value in VALUES.items()},
        'services':{key:{'count':1,'checked':False,'enabled':True} for key in ('job_sample','digital_proof')},
        'child_id':CHILD,'run_size':'500','sets':{'value':'1','valid':True,'enabled':True},
        'production':{'value':'3','label':'3 Business Days','visible':True},'parent_ids':['123']}


def summary_form():
    return {'product_id':'123','run_size':'500','custom_run_size':'0','qty':'1',
        **{f'product_options[super_attribute][{key}]':value for key,value in VALUES.items()},
        'product_options[turnaround_time]':'3','job_sample':'0','digital_proof':'0',
        'form_key':PRIVATE,'shipments[0][jobs][0][uuid]':PRIVATE,'shipments[0][address][name]':PRIVATE}
