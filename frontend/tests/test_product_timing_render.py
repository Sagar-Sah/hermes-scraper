"""One intercepted browser,99 synthetic products, no backend/profile/supplier IO."""
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit

import pytest

FRONTEND=Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_timing_99_products_live_static_keyboard_and_responsive(tmp_path):
    pw=pytest.importorskip('playwright.async_api')
    if not shutil.which('npm') or not (FRONTEND/'node_modules').is_dir():
        pytest.skip('Dashboard dependencies required')
    external=os.getenv('HERMES_TIMING_TEST_DIST')
    dist=Path(external) if external else tmp_path/'dist'
    if not external:
        subprocess.run(['npm','run','build','--','--outDir',str(dist),'--emptyOutDir'],cwd=FRONTEND,check=True,capture_output=True,text=True)
    build=json.loads((dist/'build-info.json').read_text())
    artifact=Path(os.getenv('HERMES_TIMING_SCREENSHOTS',str(tmp_path/'screenshots')));artifact.mkdir(parents=True,exist_ok=True)
    calls=[];blocked=[];errors=[];started=time.monotonic()
    def timing(state='active',active=3600):
        # Faithful complete-ledger fixture: state counters partition elapsed.
        # Unknown legacy products have no invented creation/active timestamp.
        advancing=state=='active';now=time.time()
        active=0 if state=='queued' else active+(time.monotonic()-started if advancing else 0)
        queued=3600 if state=='queued' else 120
        waiting,paused,attention=(0,0,0) if state=='queued' else (10,300,30)
        elapsed=active+queued+waiting+paused+attention
        result={'schema_version':1,'scope':'product_worker','state':state,'active_seconds':active,'queued_seconds':queued,'waiting_seconds':waiting,'paused_seconds':paused,'attention_seconds':attention,'elapsed_seconds':elapsed,'observed_since':now-elapsed,'server_now':now,'active_since':now-min(active,100) if advancing else None,'live_until':now+10 if advancing else None,'active_now':advancing,'complete_history':True,'coverage':'complete','clock_anomaly':False}
        if state=='unknown':result.update(active_seconds=None,queued_seconds=None,waiting_seconds=None,paused_seconds=None,attention_seconds=None,elapsed_seconds=None,observed_since=None,complete_history=False,coverage='unknown')
        return result
    def products():
        result=[]
        for i in range(99):
            state='paused' if i==1 else 'finished' if i==2 else 'unknown' if i==3 else 'queued' if i==4 else 'active'
            t=timing(state,125 if i==1 else 80 if i==2 else 7 if i==5 else 3600)
            if i==6:t.update(complete_history=False,coverage='partial',elapsed_seconds=None)
            result.append({'id':f'fixture-product-{i+1:03}','url':f'https://supplier-fixture.invalid/{i+1}','title':f'FIXTURE {state.title()} product {i+1:03}','status':'running' if state=='active' else 'completed' if state=='finished' else 'needs_attention' if state=='unknown' else state,'coverage_status':'complete' if state=='finished' else 'partial','row_count':0,'request_count':0,'scoped_row_count':0,'legacy_row_count':0,'timing':t})
        return result
    def run():
        return {'id':'timing-fixture-run','name':'FIXTURE timing run','status':'running','created_at':'2026-10-26T00:00:00Z','updated_at':'2026-10-26T00:00:00Z','total_products':99,'completed_products':1,'finished_attempts':1,'active_products':95,'partial_products':1,'attention_products':0,'row_count':0,'request_count':0,'cache_hits':0,'error_count':0,'coverage_status':'partial','scoped_row_count':0,'legacy_row_count':0,'products':products()}
    async def route(request):
        url=urlsplit(request.request.url);path=url.path
        if url.hostname!='hermes-timing-fixture.invalid':
            blocked.append(request.request.url);await request.fulfill(status=200,body='',content_type='text/css');return
        if path.startswith('/api/v1/'):
            calls.append((request.request.method,path))
            if path=='/api/v1/health':body={'authentication_required':False,'execution_capacity':{'effective_browser_page_limit':99,'browser_page_limit':99,'product_task_limit':99,'maximum_configured_limit':256,'browser_setting':'fixture','restart_required':False},'build':{'feature_version':'timing-fixture','features':[],'runtime_backend_sha256':'a'*64,'source_backend_sha256':'a'*64,'restart_required':False,'frontend':{'built_source_sha256':build['source_sha256'],'rebuild_required':False,'assets_valid':True}}}
            elif path=='/api/v1/runs':body={'items':[run()]}
            elif path=='/api/v1/runs/timing-fixture-run':body=run()
            elif path=='/api/v1/collection/products':body={'items':[dict(p,run_id='timing-fixture-run',run_name='FIXTURE timing run',complete=p['status']=='completed') for p in products()[:5]],'archives':[]}
            else:body={'items':[],'products':[]}
            await request.fulfill(status=200,json=body,headers={'Cache-Control':'no-store'});return
        file=(dist/('index.html' if path=='/' else path.lstrip('/'))).resolve()
        if not file.is_relative_to(dist.resolve()) or not file.is_file():
            await request.fulfill(status=404,body='fixture missing asset');return
        content_type='application/javascript' if file.suffix=='.js' else mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
        await request.fulfill(status=200,body=file.read_bytes(),content_type=content_type)
    async with pw.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():pytest.skip('Chromium unavailable')
        browser=await runtime.chromium.launch(headless=True)
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            page=await context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            await page.add_init_script('''window.__timingOneSecondIntervals=0;const prior=window.setInterval;window.setInterval=function(fn,ms,...args){if(ms===1000)window.__timingOneSecondIntervals++;return prior.call(window,fn,ms,...args);};''')
            await page.route('**/*',route)
            await page.goto('https://hermes-timing-fixture.invalid/',wait_until='networkidle')
            await page.locator('.table-title').filter(has_text='FIXTURE timing run').first.click()
            rows=page.locator('.product-status-item')
            await pw.expect(rows).to_have_count(99)
            await pw.expect(rows.nth(0).locator('.product-timing-duration')).to_contain_text('1h 00m active')
            await pw.expect(rows.nth(3).locator('.product-timing-duration')).to_have_text('Active time not measured')
            await pw.expect(rows.nth(4).locator('.product-timing-duration')).to_have_text('0s active')
            await pw.expect(rows.nth(6).locator('.product-timing-duration')).to_contain_text('At least ')
            first=await rows.nth(5).locator('.product-timing-duration').inner_text();paused=await rows.nth(1).locator('.product-timing-duration').inner_text();finished=await rows.nth(2).locator('.product-timing-duration').inner_text();before=len(calls)
            await page.wait_for_timeout(1250)
            assert await rows.nth(5).locator('.product-timing-duration').inner_text()!=first
            assert await rows.nth(1).locator('.product-timing-duration').inner_text()==paused
            assert await rows.nth(2).locator('.product-timing-duration').inner_text()==finished
            assert len(calls)-before<=4 # Existing shared poll may run; no99-row network work.
            assert await page.evaluate('window.__timingOneSecondIntervals')==1
            details=rows.first.locator('.product-timing');await details.locator('summary').focus();await page.keyboard.press('Enter');await pw.expect(details).to_have_attribute('open','');await pw.expect(details.get_by_text('Elapsed wall time',exact=True)).to_be_visible();await page.keyboard.press('Enter')
            screenshots=[]
            for name,width,height in [('desktop',1440,1000),('narrow',840,1000),('phone',390,844)]:
                await page.set_viewport_size({'width':width,'height':height})
                if width<740:await page.wait_for_function("document.querySelector('.sidebar').getBoundingClientRect().right <= 0")
                await rows.first.scroll_into_view_if_needed();await rows.first.evaluate("e=>e.scrollIntoView({block:'start'})")
                await page.screenshot(path=str(artifact/f'timing-fixture-{name}.png'))
                box=await rows.first.locator('.product-timing').bounding_box();content=await rows.first.bounding_box();assert box and content and box['width']>0 and box['x']>=content['x'] and box['x']+box['width']<=content['x']+content['width']+1 and box['x']+box['width']<=width+1
                assert await rows.first.locator('.product-timing summary').evaluate("e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}")
                screenshots.append(f'timing-fixture-{name}.png')
            await page.set_viewport_size({'width':1440,'height':1000});await page.get_by_role('button',name='Open collection',exact=True).click()
            tray=page.get_by_role('dialog',name='Your collection');await pw.expect(tray).to_be_visible();await pw.expect(tray.locator('.product-timing')).to_have_count(5)
            await pw.expect(tray.locator('.product-timing').first).to_contain_text('1h 00m active')
            assert await page.evaluate('window.__timingOneSecondIntervals')==1
            assert not errors,errors
            assert all(method=='GET' for method,_ in calls)
            report={'status':'PASS','fixture':True,'products':99,'shared_one_second_intervals':1,'static_paused_and_finished':True,'keyboard_details':True,'viewports':['desktop','narrow','phone'],'screenshots':screenshots,'collection_tray_timing_rows':5,'page_errors':errors,'requests_intercepted':len(calls),'external_requests_blocked':len(blocked),'supplier_requests':0,'user_profiles_opened':0}
            (artifact/'browser-results.json').write_text(json.dumps(report,indent=2)+'\n')
        finally:await browser.close()
