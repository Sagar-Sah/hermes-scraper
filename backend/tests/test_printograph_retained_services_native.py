"""Real isolated Chromium, every URL intercepted by synthetic fixture."""
import ast,json
from pathlib import Path
import pytest
from crawler import printograph_modern_browser as m
from test_printograph_retained_services import URL
ROOT=Path(__file__).resolve().parent

HTML=(ROOT/'fixtures/printograph_retained_services/native.html').read_text()

class CountPage:
    def __init__(self,page):self.page=page;self.reads=0;self.actions=0
    async def evaluate(self,script,*args):
        if script==m.SERVICE_READ_JS:self.reads+=1
        if script==m.SERVICE_ACTION_JS:self.actions+=1
        return await self.page.evaluate(script,*args)

@pytest.fixture
async def local_native():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(headless=True)
        context=await browser.new_context(service_workers='block');page=await context.new_page();seen=[]
        async def intercept(route):
            seen.append(route.request.url)
            if route.request.url==URL:await route.fulfill(status=200,content_type='text/html',body=HTML)
            else:await route.abort();raise AssertionError('No source API permitted in no-action native fixture')
        await context.route('**/*',intercept);await page.goto(URL)
        await page.evaluate('''() => {
          const selected=controller.jsonObj.userSelectedOptions;
          Object.assign(selected,{proofType:'902',manualProofType:'712',fileDelivery:'502',designServices:'608'});
          document.querySelector('#collapseOne').classList.add('show');
          for(const [name,value] of Object.entries({proofType:'902',proofTypeForManual:'712',fileDeliveryForManual:'502',designServices:'608'}))
            for(const input of document.querySelectorAll('input[name="'+name+'"]'))input.checked=input.value===value;
        }''')
        try:yield page,seen
        finally:await context.close();await browser.close()

@pytest.mark.asyncio
async def test_actual_native_retained_no_proof_is_one_read_zero_actions(local_native):
    page,seen=local_native;counted=CountPage(page)
    async def noop():pass
    actual=await m.prepare_services(counted,URL,noop,noop,noop)
    assert counted.reads==1 and counted.actions==0 and await page.evaluate('actions')==[]
    assert m.policy_available(actual[0]) and m.modern_bound(m.service_signature(actual[0]))
    assert actual[1]=={'proofType':'902','proofTypeForManual':'712','fileDeliveryForManual':'502','designServices':'608'}
    assert seen==[URL]

@pytest.mark.asyncio
@pytest.mark.parametrize('drift',['private','shipping','source','duplicate'])
async def test_actual_source_private_and_ambiguous_controls_cannot_shortcut(local_native,drift):
    page,seen=local_native
    changes={
      'private':"controller.jsonObj.others.editOrderItem={secret:'SYNTHETIC-PRIVATE'}",
      'shipping':"document.body.insertAdjacentHTML('beforeend','<div id=esCollapse class=show></div>')",
      'source':"history.replaceState(null,'','/products/unrelated/order')",
      'duplicate':"const clone=document.querySelector('#proofTypeForManual712').cloneNode();clone.id='duplicateproof';clone.value='38';clone.checked=false;document.querySelector('#collapseOne').append(clone);const l=document.createElement('label');l.htmlFor=clone.id;l.textContent='No Proof';clone.after(l)"}
    await page.evaluate(changes[drift]);counted=CountPage(page)
    async def noop():pass
    with pytest.raises(m.ModernHeld):await m.prepare_services(counted,URL,noop,noop,noop)
    assert counted.actions==0 and seen==[URL]
