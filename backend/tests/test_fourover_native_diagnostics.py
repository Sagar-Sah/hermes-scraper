"""Pure projections and synthetic lower-driver diagnostics; public q500 stays closed."""
from contextlib import asynccontextmanager
import json
import pytest
from test_fourover_native_observation import native,SOURCE,STATIC_TEMPLATE
from crawler.fourover_native_observation import blocked_request_classification,safe_exception_type

PRIVATE='DIAGNOSTIC-PRIVATE-QUERY-BODY-EXCEPTION-SENTINEL'

@pytest.mark.parametrize('url,origin,path',[
 ('https://4over.com/customer/'+PRIVATE+'?token='+PRIVATE,'source','account'),
 ('https://4over.com/rest/default/V1/product/prices/'+PRIVATE+'?token='+PRIVATE,'source','child_price'),
 ('https://4over.com/print/shipment/'+PRIVATE+'?address='+PRIVATE,'source','shipping'),
 ('https://4over.com/checkout/cart/'+PRIVATE,'source','cart'),
 ('https://4over.com/print/project/summary/?token='+PRIVATE,'source','project_summary'),
 ('https://'+PRIVATE+'.invalid/'+PRIVATE+'?token='+PRIVATE,'external','other'),
 ('https://user:'+PRIVATE+'@4over.com/private/'+PRIVATE,'invalid_authority','other'),
 ('https://4over.com:not-a-port/private/'+PRIVATE,'invalid','invalid'),
])
def test_blocked_url_projection_contains_only_fixed_enums(url,origin,path):
 projected=blocked_request_classification(url,'POST','xhr',True)
 assert projected=={'origin_class':origin,'path_class':path,'method_class':'POST','resource_type':'xhr','main_frame':True}
 assert PRIVATE not in json.dumps(projected)
 assert 'url' not in projected and 'body' not in projected


def test_exception_projection_never_contains_message_or_dynamic_class_name():
 assert safe_exception_type(TimeoutError(PRIVATE))=='TimeoutError'
 secret_type=type(PRIVATE,(Exception,),{})
 assert safe_exception_type(secret_type(PRIVATE))=='Exception'
 assert PRIVATE not in json.dumps([safe_exception_type(TimeoutError(PRIVATE)),safe_exception_type(secret_type(PRIVATE))])


@pytest.mark.parametrize('record_guard,expected',[ (True,'unexpected_native_request'),(False,'native_observation_stopped') ])
@pytest.mark.asyncio
async def test_synthetic_lower_driver_timeout_retains_guard_without_public_retry(native,record_guard,expected):
 original_pool=native.observer.pool
 class Pool:
  @asynccontextmanager
  async def page(self,*args,**kwargs):
   async with original_pool.page(*args,**kwargs) as page:
    async def stopped(*args,**kwargs):
     if record_guard:
      await page.evaluate("""privateValue=>fetch('/customer/'+privateValue+'?token='+privateValue,{method:'POST',body:privateValue}).catch(()=>null)""",PRIVATE)
     raise TimeoutError(PRIVATE)
    page.wait_for_function=stopped
    yield page
 native.observer.pool=Pool()
 result=await native.attempt()
 assert result['status']=='held' and result['reason_code']==expected,result
 assert result['controlled_requests']=={'document':1,'price':0,'summary':0}
 assert native.sent==[SOURCE,STATIC_TEMPLATE]
 assert all(result.get(flag) is not True for flag in ('price_fresh','final_eligible','checkout_eligible'))
 diag=result['diagnostics']
 assert diag['stage']=='select_attributes' and diag['exception_type']=='TimeoutError'
 assert diag['recorded_guard_failure']==('unexpected_native_request' if record_guard else None)
 if record_guard:
  assert diag['blocked_request_classes']==[{'category':'forbidden_mutation','origin_class':'source','path_class':'account','method_class':'POST','resource_type':'fetch','main_frame':True,'count':1}]
 else:assert diag['blocked_request_classes']==[]
 assert PRIVATE not in json.dumps(result)
 sent=list(native.sent)
 again=await native.run()
 assert again['supplier_requests_this_call']==0 and native.sent==sent
 assert again['status']=='held' and again['reason_code']=='prior_native_context_historical_only'
 assert len(native.pages)==1 and native.observer.limiter.grants==native.observer.limiter.releases
 assert not again['price_fresh'] and not again['final_eligible'] and not again['checkout_eligible']
