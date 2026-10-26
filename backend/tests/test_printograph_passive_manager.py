"""Opt-in manager fixture: exacthost mapped only to local TLS, no real suppliers."""
import asyncio
from contextlib import asynccontextmanager
import copy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

ROOT=Path(__file__).parents[2]
PROTO=ROOT/'backend/tests/test_printograph_price_journal.py'
spec=importlib.util.spec_from_file_location('_passive_journal_fixtures',PROTO)
fixtures=importlib.util.module_from_spec(spec);sys.modules[spec.name]=fixtures;spec.loader.exec_module(fixtures)
browser_page=fixtures.browser_page
m=fixtures.m
spec=importlib.util.spec_from_file_location('crawler.product_diagnosis_passive_candidate',ROOT/'backend/crawler/product_diagnosis.py')
d=importlib.util.module_from_spec(spec);sys.modules[spec.name]=d;spec.loader.exec_module(d)


def local_html():
    controls=[]
    choices={'shape':[('', 'Choose shape'),('15','Shape Fifteen')],
             'size':[('', 'Choose size'),('101','Size One Hundred One')],
             'orientation':[('1','Horizontal'),('2','Vertical')],
             'paper':[('', 'Choose paper'),('1','Paper One')],
             'color':[('', 'Choose color'),('3','Color Three')],
             'quantity':[('0','Choose quantity'),('50','50'),('100','100')]}
    for name,options in choices.items():
        controls.append('<label for="'+name+'">'+name.title()+'</label><select name="'+name+'" id="'+name+'">'+
                        ''.join('<option value="'+value+'">'+label+'</option>' for value,label in options)+'</select>')
    state=fixtures.capture(shape='',size='',paper='',color='',quantity='0',productId='',base='0')
    return '''<!doctype html><h1>Special Shapes</h1><div id="order-form" data-printproducttype="39" data-currentstoretype="broker">'''+''.join(controls)+'''
      <label for="notes">Private notes</label><input id="notes" value="PRIVATE-EDIT-INPUT">
      <div class="totalSection"><div class="col-8"><div class="subTotal">Subtotal (excludes shipping)</div></div><div class="col-4">$0.00</div></div>
      </div><script>
      const seed=__SEED__, selected=seed.userSelectedOptions;
      selected.shoppingCartItemId='';selected.priceGroupId='';
      window.stats={coupon:0,httpMethod:0,nativeFetch:0,total:0};
      const forbidden=()=>{stats.httpMethod++;throw new Error('PRIVATE-MUST-NOT-CALL');};
      const c=window.fixtureController={printProductType:39,fields:{39:{specs:seed.field_specs,options:seed.field_options}},
        jsonObj:{userSelectedOptions:selected,appliedPrices:seed.appliedPrices,others:{isAdmin:false,waiveFees:false,itemId:'',editOrderItem:{}}},
        couponData:{coupon:{},isCouponSpecChanged:false},getCouponPrice:()=>{stats.coupon++;throw new Error('PRIVATE-COUPON');},
        getPrice:forbidden,getGenericProductsPrice:forbidden,getOptionPrice:forbidden,getTurnaround:forbidden};
      Object.defineProperty(c,'totalPrice',{get(){stats.total++;return Number(c.jsonObj.appliedPrices.productPrice||0).toFixed(2);},configurable:true});
      document.querySelector('#order-form')._vnode={component:{proxy:{$refs:{childComponents:{_:{subTree:{component:{refs:{orderFormInstance:c}}}}}}}}};
      let table=null;
      const render=()=>{document.querySelector('.totalSection > .col-4').textContent='$'+c.totalPrice;};
      for(const e of document.querySelectorAll('select'))e.addEventListener('change',async()=>{
        selected[e.name]=e.value;
        if(e.name==='color'&&selected.shape&&selected.size&&selected.paper&&selected.orientation&&selected.color) {
          const q=new URLSearchParams({productType:'39',turnaround:'1',shape:selected.shape,size:selected.size,orientation:selected.orientation,paper:selected.paper,color:selected.color});
          stats.nativeFetch++;const identity=await fetch('/service/rest/v1/products?'+q).then(r=>r.json());
          selected.productId=identity.items[0].id;selected.quantity='50';document.querySelector('#quantity').value='50';
          stats.nativeFetch++;table=await fetch('/service/rest/v1/products/'+selected.productId+'/prices').then(r=>r.json());
        }
        if(table) {const row=table.items.find(r=>r.quantity==selected.quantity);if(row)c.jsonObj.appliedPrices.productPrice=row.markupPrice;}
        render();
      });
      </script>'''.replace('__SEED__',json.dumps(state))


@pytest_asyncio.fixture
async def candidate_manager(browser_page,tmp_path,monkeypatch):
    initial,server=browser_page
    original=fixtures.LocalHandler.do_GET
    html=local_html().encode()
    def local_handler(self):
        if self.path=='/products/special-shapes/order':
            self.server.seen.append(self.path);self.send_response(200);self.send_header('Content-Type','text/html')
            self.send_header('Content-Length',str(len(html)));self.end_headers();self.wfile.write(html)
        else:original(self)
    monkeypatch.setattr(fixtures.LocalHandler,'do_GET',local_handler)
    class Pool:
        @asynccontextmanager
        async def page(self,*args,**kwargs):
            page=await initial.context.new_page()
            original_on=page.on;page.fixture_response_handlers=[]
            def remember(event,callback):
                if event=='response':page.fixture_response_handlers.append(callback)
                return original_on(event,callback)
            page.on=remember
            try:yield page
            finally:
                if not page.is_closed():await page.close()
    pool=Pool();value=d.ProductDiagnosisManager(SimpleNamespace(root=tmp_path/'manager-data'),pool)
    monkeypatch.setattr(d,'validate_url',AsyncMock())
    value._validated_target=AsyncMock(return_value=('https://www.printograph.com',{'www.printograph.com','printograph.com'},'printograph'))
    value._choice_limiter=SimpleNamespace(acquire=AsyncMock(return_value='permit'),release=MagicMock())
    original_new=value._new_page
    async def with_synthetic_observed_hashes(*args):
        page=await original_new(*args)
        value._bundle_hashes[args[0]].update({key:{digest} for key,digest in m.CONTRACT.items()})
        return page
    value._new_page=with_synthetic_observed_hashes
    try:yield value,server
    finally:await value.close()


async def opened(value,**kwargs):
    record=await value.start(m.SOURCE_URL,**kwargs)
    await value._tasks[record['id']]
    return value.get(record['id'])


def native_field(record,name):return next(f for f in record['fields'] if f.get('source_name')==name)


@pytest.mark.asyncio
async def test_default_off_never_attaches_or_adds_record_fields(candidate_manager):
    value,server=candidate_manager;record=await opened(value)
    assert 'observe_printograph_prices' not in record and not value._passive_journals
    with pytest.raises(ValueError,match='not_enabled'):await value.read_passive_price_evidence(record['id'],record['revision'])
    with pytest.raises(ValueError,match='source_unsupported'):await value.start('https://www.printograph.com/products/catalogs/order',observe_printograph_prices=True)
    with pytest.raises(ValueError):await value.start(m.SOURCE_URL,observe_printograph_prices='yes')


@pytest.mark.asyncio
async def test_opt_in_blank_chain_final_identity_q100_reuse_and_cached_read_add_no_HTTP(candidate_manager):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    assert record['status']=='ready' and record['passive_price_observation']['status']=='observing',record
    source_before=list(server.seen)
    sequence=[('shape','15'),('size','101'),('orientation','2'),('paper','1'),('color','3'),('quantity','100')]
    for index,(name,target) in enumerate(sequence):
        field=native_field(record,name)
        record=await value.select_observed_choice(record['id'],record['revision'],field['key'],target)
        if index<4:assert record['passive_price_observation']['status']=='held'
    internal=copy.deepcopy(value._passive_journals[record['id']].snapshot())
    evidence=await value.read_passive_price_evidence(record['id'],record['revision'])
    assert value._passive_journals[record['id']].snapshot()==internal
    epochs=evidence['evidence']['epochs'];assert len(epochs)==6
    assert epochs[4]['status']=='held' and 'observation_revision_not_current' in epochs[4]['reasons']
    assert epochs[5]['status']=='matched_fresh_observed_components',evidence
    assert epochs[5]['components'][0]['status']=='reused_fresh_observed_table'
    assert epochs[5]['components'][0]['quantity']=='100' and epochs[5]['components'][0]['source_cents']=='16.00'
    assert epochs[5]['components'][0]['originating_epoch']==5
    assert len(server.seen)==len(source_before)+2
    assert value._choice_limiter.acquire.await_count==6 and value._choice_limiter.release.call_count==6
    page=value._pages[record['id']];stats=await page.evaluate('stats')
    assert stats['coupon']==0 and stats['httpMethod']==0 and stats['nativeFetch']==2
    assert 'PRIVATE' not in json.dumps(evidence)
    before=list(server.seen);evaluate=page.evaluate;page.evaluate=AsyncMock(side_effect=AssertionError('cached read must not evaluate'))
    try:
        again=await value.read_passive_price_evidence(record['id'],record['revision'])
        assert again['status']==evidence['status']
        assert again['evidence']['epochs'][-1]['components'][0]['table_projection_sha256']==epochs[-1]['components'][0]['table_projection_sha256']
        assert again['evidence']['epochs'][-1]['components'][0]['ttl_remaining_seconds']<=epochs[-1]['components'][0]['ttl_remaining_seconds']
    finally:page.evaluate=evaluate
    assert server.seen==before
    # A later diagnosis refresh cannot promote the last native epoch to its new revision.
    value._records[record['id']]['revision']+=1
    changed=await value.read_passive_price_evidence(record['id'],record['revision']+1)
    assert changed['status']=='held' and changed['historical_only'] is True
    assert 'observation_revision_not_current' in changed['evidence']['epochs'][-1]['reasons']
    assert changed['evidence']['epochs'][-1]['components'][0]['status']=='historical_only'
    value._records[record['id']]['revision']-=1
    # A navigation can be visible before CDP frame invalidation; cached reads bind the exact URL too.
    value._pages[record['id']]=SimpleNamespace(is_closed=lambda:False,url='https://www.printograph.com/products/catalogs/order')
    try:
        moved=await value.read_passive_price_evidence(record['id'],record['revision'])
        assert moved['status']=='held' and moved['historical_only'] is True
        assert 'observation_not_live' in moved['evidence']['epochs'][-1]['reasons']
    finally:value._pages[record['id']]=page
    bundle_hashes=copy.deepcopy(value._bundle_hashes[record['id']])
    value._bundle_hashes[record['id']]['order_form_sha256'].add('a'*64)
    drifted=await value.read_passive_price_evidence(record['id'],record['revision'])
    assert drifted['status']=='held' and drifted['historical_only'] is True
    assert 'observation_not_live' in drifted['evidence']['epochs'][-1]['reasons']
    value._bundle_hashes[record['id']]=bundle_hashes
    pending=asyncio.create_task(asyncio.Event().wait())
    value._bundle_tasks[record['id']].add(pending)
    try:
        unproven=await value.read_passive_price_evidence(record['id'],record['revision'])
        assert unproven['status']=='held' and unproven['historical_only'] is True
    finally:
        pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        value._bundle_tasks[record['id']].discard(pending)
    assert server.seen==before
    clock=value._passive_journals[record['id']].clock
    value._passive_journals[record['id']].clock=lambda:clock()+301
    expired=await value.read_passive_price_evidence(record['id'],record['revision'])
    assert expired['status']=='held' and expired['historical_only'] is True
    assert expired['evidence']['epochs'][-1]['components'][0]['status']=='historical_only'
    assert value._passive_journals[record['id']].snapshot()==internal
    assert server.seen==before
    with pytest.raises(ValueError,match='revision'):await value.read_passive_price_evidence(record['id'],record['revision']-1)


@pytest.mark.asyncio
async def test_missing_bundle_or_coupon_holds_before_native_action(candidate_manager):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']];source_before=list(server.seen)
    await value._release(record['id'])
    # Separate ordinary diagnosis without opt-in gets no observer; enabling below is a synthetic guard probe.
    record=await opened(value)
    value._records[record['id']]['observe_printograph_prices']=True
    value._bundle_hashes[record['id']]={}
    with pytest.raises(ValueError):await value.select_observed_choice(record['id'],record['revision'],native_field(record,'shape')['key'],'15')
    assert value._choice_limiter.acquire.await_count==0
    assert await value._pages[record['id']].locator('#shape').input_value()==''
    value._bundle_hashes[record['id']]={k:{v} for k,v in m.CONTRACT.items()}
    await value._pages[record['id']].evaluate('fixtureController.couponData=null')
    with pytest.raises(ValueError):await value.select_observed_choice(record['id'],record['revision'],native_field(record,'shape')['key'],'15')
    assert value._choice_limiter.acquire.await_count==0


@pytest.mark.asyncio
async def test_opt_in_radio_rejected_before_permit_or_native_action(candidate_manager):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']]
    await page.evaluate('''() => document.querySelector('#order-form').insertAdjacentHTML('beforeend',
      '<div class="form-group"><h3>Proof Processing</h3><input type="radio" id="instant" name="proofType" value="31" checked><label for="instant">Instantly Processed</label><input type="radio" id="manual" name="proofType" value="100"><label for="manual">Manually Processed</label></div>')''')
    record=await value.refresh(record['id'])
    field=native_field(record,'proofType');assert field['kind']=='radio'
    before=list(server.seen)
    with pytest.raises(ValueError,match='passive_radio_not_supported'):
        await value.select_observed_choice(record['id'],record['revision'],field['key'],'100')
    assert value._choice_limiter.acquire.await_count==0
    assert await page.locator('#instant').is_checked() and not await page.locator('#manual').is_checked()
    assert server.seen==before and not value._passive_journals[record['id']]._epochs


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[200,503])
async def test_unproven_later_bundle_response_cannot_retain_old_verified_pair(candidate_manager,status):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']];before=list(server.seen)
    response=SimpleNamespace(url='https://static.printograph.com/js/orderForm.js',status=status,
        body=AsyncMock(side_effect=RuntimeError('PRIVATE-MUST-NOT-EXPOSE')))
    assert page.fixture_response_handlers
    for callback in page.fixture_response_handlers:callback(response)
    pending=tuple(value._bundle_tasks[record['id']]);assert pending
    await asyncio.gather(*pending)
    assert 'unproven_loaded_bundle' in value._bundle_hashes[record['id']]['order_form_sha256']
    result=await value.read_passive_price_evidence(record['id'],record['revision'])
    assert result['status']=='held' and result['historical_only'] is True
    assert 'PRIVATE' not in json.dumps(result) and server.seen==before
    assert response.body.await_count==(1 if status==200 else 0)


@pytest.mark.asyncio
async def test_passive_capture_and_fence_never_read_edit_values_or_call_source_methods(candidate_manager):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']]
    await page.evaluate("Object.defineProperty(document.querySelector('#notes'),'value',{get(){throw new Error('PRIVATE-READ-EDIT');}})")
    before=list(server.seen)
    raw=await value._passive_capture_locked(record['id'])
    assert raw['observed'] is True and raw['userSelectedOptions']['productId']==''
    assert (await page.evaluate('stats'))['coupon']==0
    assert server.seen==before
    # Different user input identity invalidates, while text input values are never inspected.
    await page.locator('#orientation').select_option('2')
    with pytest.raises(ValueError):await value.select_observed_choice(record['id'],record['revision'],native_field(record,'shape')['key'],'15')
    assert value._passive_journals[record['id']]._invalid=='unattributed_control_change'


@pytest.mark.asyncio
async def test_failed_and_cancelled_native_choice_is_held_and_permit_released(candidate_manager,monkeypatch):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']]
    actual=page.locator
    entered=asyncio.Event()
    class WaitingControl:
        async def select_option(self,*args,**kwargs):entered.set();await asyncio.Event().wait()
    monkeypatch.setattr(page,'locator',lambda selector:WaitingControl() if selector=='select[id="shape"]' else actual(selector))
    task=asyncio.create_task(value.select_observed_choice(record['id'],record['revision'],native_field(record,'shape')['key'],'15'))
    await asyncio.wait_for(entered.wait(),2);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert value._choice_limiter.release.call_count==1
    assert value.get(record['id'])['passive_price_observation']['status']=='held'
    assert value._passive_journals[record['id']]._invalid=='cancelled'


@pytest.mark.asyncio
async def test_actual_pinned_computed_bodies_and_transitive_helpers_never_fetch_or_read_cart_fields(candidate_manager):
    import hashlib
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']]
    # Keep the verified public function bodies in the test package. A developer
    # checkout must not depend on another user's absolute evidence-file paths.
    evidence=json.loads((Path(__file__).parent/'fixtures/printograph_type39_computed.json').read_text())
    for item in [*evidence['total_variants'],*evidence['is_generic'],*evidence['free_sample_gate']]:
        exact=item['function'].encode()
        assert len(exact)==item['bytes']
        assert hashlib.sha256(exact).hexdigest()==item['sha256']
    before=list(server.seen)
    result=await page.evaluate('''e=>{
      let network=0,cartField=0,cartLoad=0,coupon=0;
      const trap=()=>{network++;throw Error('PRIVATE-NETWORK');};
      const cart={};Object.defineProperty(cart,'userSettings',{get(){cartField++;throw Error('PRIVATE-CART');}});
      window.cacheCart=cart;
      const r={Ay:{loadAllItems(){cartLoad++;throw Error('PRIVATE-CART-LOADER');}}};
      const isGeneric=Function('return('+e.is_generic[0].function+')')();
      const canFree=Function('r','return('+e.free_sample_gate[0].function+')')(r);
      const w={A:{isGeneric,getPrice:trap,getGenericProductsPrice:trap,getTurnaround:trap}};
      const c=fixtureController;c.parameter={canOrderFreeSampleKit:canFree};
      Object.assign(c.jsonObj.userSelectedOptions,{shape:'15',size:'101',orientation:'1',paper:'1',color:'3',productId:'900',quantity:'50'});
      c.jsonObj.appliedPrices.productPrice='10.00';c.getCouponPrice=()=>{coupon++;throw Error('PRIVATE-COUPON');};
      const totals=e.total_variants.map(x=>Function('w','return('+x.function+')')(w).call(c));
      const fn=Function('w','return('+e.total_variants[0].function+')')(w);
      Object.defineProperty(c,'totalPrice',{get(){return fn.call(c);},configurable:true});
      document.querySelector('.totalSection > .col-4').textContent='$10.00';
      return {totals,network,cartField,cartLoad,coupon};
    }''',evidence)
    assert result=={'totals':['10.00']*3,'network':0,'cartField':0,'cartLoad':0,'coupon':0}
    raw=await value._passive_capture_locked(record['id'])
    assert raw['controller_total']=='10.00' and raw['displayed_total']=='$10.00'
    assert server.seen==before and (await page.evaluate('stats'))['httpMethod']==0


@pytest.mark.asyncio
async def test_unexpected_native_request_after_completed_epoch_invalidates_cached_evidence(candidate_manager):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']]
    # An explicit synthetic finalized partial epoch is enough to exercise the post-epoch request guard.
    j=value._passive_journals[record['id']]
    ep=j.begin_epoch(fixtures.capture(color='1',productId='899'),record['revision'],'color','3')
    j.finalize_epoch(ep,fixtures.capture(),record['revision']+1)
    before=len(server.seen)
    await fixtures.native_get(page,fixtures.PRICE)
    assert len(server.seen)==before+1
    assert j._invalid=='unattributed_control_change'
    result=await value.read_passive_price_evidence(record['id'],record['revision'])
    assert result['status']=='held' and result['historical_only'] is True


@pytest.mark.asyncio
async def test_ninth_epoch_refused_before_native_control_and_failure_is_held(candidate_manager):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    j=value._passive_journals[record['id']]
    for rev in range(8):
        before=fixtures.capture(color='',paper='',quantity='0',productId='',base='0',orientation='1' if rev%2==0 else '2')
        target='2' if rev%2==0 else '1'
        ep=j.begin_epoch(before,rev,'orientation',target)
        after=copy.deepcopy(before);after['userSelectedOptions']['orientation']=target
        j.finalize_epoch(ep,after,rev+1)
    page=value._pages[record['id']];before_requests=list(server.seen)
    with pytest.raises(ValueError,match='passive_native_choice_failed'):
        await value.select_observed_choice(record['id'],record['revision'],native_field(record,'shape')['key'],'15')
    assert await page.locator('#shape').input_value()=='' and server.seen==before_requests
    assert len(j._epochs)==8 and j._invalid
    assert value.get(record['id'])['passive_price_observation']['status']=='held'
    assert value._choice_limiter.release.call_count==1


@pytest.mark.asyncio
async def test_opt_in_choice_lock_covers_refresh_and_prevents_stale_concurrent_action(candidate_manager,monkeypatch):
    value,server=candidate_manager;record=await opened(value,observe_printograph_prices=True)
    page=value._pages[record['id']];actual=page.locator;entered=asyncio.Event();release=asyncio.Event()
    class Delayed:
        async def select_option(self,*args,**kwargs):
            entered.set();await release.wait();return await actual('select[id="shape"]').select_option(*args,**kwargs)
    monkeypatch.setattr(page,'locator',lambda selector:Delayed() if selector=='select[id="shape"]' else actual(selector))
    first=asyncio.create_task(value.select_observed_choice(record['id'],record['revision'],native_field(record,'shape')['key'],'15'))
    await asyncio.wait_for(entered.wait(),2)
    second=asyncio.create_task(value.select_observed_choice(record['id'],record['revision'],native_field(record,'size')['key'],'101'))
    await asyncio.sleep(.03);assert not second.done();release.set()
    result=await first;assert result['revision']==record['revision']+1
    with pytest.raises(ValueError,match='revision_conflict'):await second
    assert value._choice_limiter.acquire.await_count==1
    assert await actual('#size').input_value()==''
