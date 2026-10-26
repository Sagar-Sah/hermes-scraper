import asyncio
import csv
import io
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse, urljoin

from fastapi import FastAPI, HTTPException, Request, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response as RawResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy import select, update, func

from .config import settings
from .browser_capacity import browser_workload
from .store import (
    Store,
    Run,
    Product,
    ProductRecordScope,
    ResponseObservation,
    TrafficWindow,
    Event,
    Response,
    Origin,
    canonical,
    uid,
)
from .engine import Engine
from .probe_jobs import ProbeRunner
from .source_domains import DomainJob, SourceDomainCoordinator
from .source_domain_session import SourceDomainSessions
from .variation_plan import VariationPlanCache
from .product_diagnosis import ProductDiagnosisManager
from .dps_workflow_references import DPS_ACCOUNT_URL
from .diagnosis_policy import normalize_selection
from .product_finalization import saved_products, review_product, review_file
from .transport import validate_url, ExtractionError
from .campaign_progress import CampaignManager, required_fixed_options

store = Store(settings)


@asynccontextmanager
async def lifespan(app):
    engine = Engine(store)
    app.state.engine = engine
    app.state.probes = ProbeRunner(store, engine.browser_pool, on_stage_finished=engine.finish_run)
    engine.probes = app.state.probes
    # Discovery stages remain dormant until a session factory is installed and
    # their separate planner scope is explicitly started. Product pauses persist.
    app.state.domain_sessions = SourceDomainSessions(store, engine.browser_pool)
    app.state.probes.domain_sessions = app.state.domain_sessions
    app.state.variation_plans = VariationPlanCache(store)
    app.state.product_diagnoses = ProductDiagnosisManager(store, engine.browser_pool)
    app.state.pricing_pilots = None
    app.state.dps_pilots = None
    app.state.dps_exact_quotes = None
    app.state.campaigns = CampaignManager(store)
    app.state.fourover_child_prices = None
    app.state.fourover_native_observer = None
    app.state.source_collections = None
    app.state.b2sign_harvests = None
    app.state.b2sign_canaries = None
    app.state.b2sign_exact_quotes = None
    from .b2sign_harvest import SharedQuoteGate
    app.state.b2sign_quote_gate = SharedQuoteGate(2)
    app.state.source_domains = SourceDomainCoordinator(store, app.state.domain_sessions.factory,
                                                       on_stage_finished=engine.finish_run,
                                                       context_validator=app.state.domain_sessions.validate_context)
    engine.source_domains = app.state.source_domains
    from .campaign_automation_registry import build_campaign_automation
    async def create_owned_browser_run(spec):
        created = await create_run(JobSpec(**spec))
        # Same event-loop turn: the engine cannot begin the queued child
        # before this synchronous normal pause fences it. The profile saves
        # its owner/ID before it checks scope again and resumes the child.
        return control(created['id'], 'pause')

    async def read_owned_browser_run(run_id):
        return get_run(run_id)

    async def control_owned_browser_run(run_id, action):
        return control(run_id, action)

    async def find_owned_browser_runs(owner):
        with store.session() as session:
            ids = list(session.scalars(select(Run.id).where(
                Run.spec['source_metadata']['automation_owner'].as_string() == owner).limit(2)))
        return [get_run(run_id) for run_id in ids]

    app.state.campaign_automation = build_campaign_automation(
        store, app.state.campaigns, app.state.product_diagnoses, engine.browser_pool,
        browser_run_callbacks={'create': create_owned_browser_run, 'read': read_owned_browser_run,
            'control': control_owned_browser_run, 'find_owned': find_owned_browser_runs})
    await app.state.campaign_automation.open()
    await engine.start()
    yield
    await app.state.campaign_automation.close()
    await app.state.campaigns.close()
    if app.state.source_collections is not None:
        await app.state.source_collections.close()
    if app.state.b2sign_harvests is not None:
        await app.state.b2sign_harvests.close()
    if app.state.b2sign_canaries is not None:
        await app.state.b2sign_canaries.close()
    if app.state.pricing_pilots is not None:
        await app.state.pricing_pilots.close()
    if app.state.dps_pilots is not None:
        await app.state.dps_pilots.close()
    await app.state.product_diagnoses.close()
    await app.state.source_domains.close()
    await app.state.domain_sessions.close()
    await app.state.probes.close()
    await engine.stop()


app = FastAPI(title="Hermes Product Crawler", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)


async def authenticated(request: Request):
    if settings.api_token:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not secrets.compare_digest(supplied, settings.api_token):
            raise HTTPException(401, "API token required")


from .campaign_automation_routes import automation_router
app.include_router(automation_router(lambda: app.state.campaign_automation, authenticated))


def active_campaign_product(campaign_id:str,product_id:str):
    try:return app.state.campaigns.require_active_group(campaign_id,product_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


def active_or_supplemental_fourover(campaign_id:str,product_id:str):
    """Allow an explicitly queued 4over correction to receive a read-only probe."""
    try:
        campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:
        raise HTTPException(404,'Campaign not found')
    if product_id in campaign.get('active_group_product_ids',[]):
        return campaign
    matched=next((j for j in campaign.get('supplemental_product_jobs',[])
                  if j.get('storefront_product_id')==product_id
                  and j.get('supplier')=='fourover' and j.get('stage')=='queued'),None)
    if matched is None:
        raise HTTPException(409,'Finish the current product group before starting another')
    return campaign


def active_or_queued_soardist_offer(campaign_id:str,product_id:str,candidate_url:str=''):
    """Allow read-only probes of a pinned alternate offer on a finalized product."""
    try:
        return app.state.campaigns.require_active_group(campaign_id,product_id)
    except FileNotFoundError:
        raise HTTPException(404,'Campaign not found')
    except ValueError as exc:
        if not candidate_url:
            raise HTTPException(409,str(exc)) from exc
        campaign=app.state.campaigns.get(campaign_id)
        product=next((item for item in campaign['products'] if item['id']==product_id),None)
        matched=any(job.get('storefront_product_id')==product_id and
                    job.get('supplier')=='soardist_usa' and
                    job.get('supplier_url')==candidate_url and
                    job.get('stage')=='queued'
                    for job in campaign.get('alternate_supplier_jobs',[]))
        if not product or product.get('stage')!='finalized' or not matched:
            raise HTTPException(409,'A pinned queued alternate offer is required') from exc
        return campaign


class CampaignRequest(BaseModel):
    model_config = {"extra": "forbid"}
    id: str | None = None
    name: str = Field(min_length=1,max_length=300)
    products: list[dict] = Field(min_length=1,max_length=1000)


@app.get('/api/v1/campaigns', dependencies=[Depends(authenticated)])
def campaigns():
    return {'items':app.state.campaigns.list()}


@app.get('/api/v1/campaigns/{campaign_id}', dependencies=[Depends(authenticated)])
def campaign(campaign_id:str):
    try:return app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')


@app.get('/api/v1/campaigns/{campaign_id}/dps-route-prices/{product_id}', dependencies=[Depends(authenticated)])
def dps_route_prices(campaign_id:str,product_id:str):
    try:return app.state.campaigns.dps_route_price_snapshot(campaign_id,product_id)
    except FileNotFoundError:raise HTTPException(404,'DPS route prices not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class DPSExactQuoteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    width: StrictStr | StrictInt | StrictFloat
    height: StrictStr | StrictInt | StrictFloat
    quantity: StrictStr | StrictInt
    selected_options: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=30)


@app.post('/api/v1/campaigns/{campaign_id}/dps-exact-quote/{product_id}',
          dependencies=[Depends(authenticated)])
async def dps_exact_quote(campaign_id: str, product_id: str, payload: DPSExactQuoteRequest):
    campaign = active_campaign_product(campaign_id, product_id)
    product = next((p for p in campaign['products'] if p['id'] == product_id), None)
    if not product or product.get('supplier') != 'digital_print_solutions' or product.get('stage') == 'finalized':
        raise HTTPException(409, 'Active unfinished DPS product required')
    evidence = product.get('dps_route_price_evidence') or {}
    pilot_id = evidence.get('native_pilot_id')
    if not pilot_id:
        raise HTTPException(409, 'Saved native DPS pilot required')
    try:
        pilot = dps_pilot_manager().get(pilot_id)
    except (FileNotFoundError, KeyError):
        raise HTTPException(409, 'Saved native DPS pilot unavailable')
    if pilot.get('url') != product.get('supplier_url'):
        raise HTTPException(409, 'DPS source mapping changed')
    if app.state.dps_exact_quotes is None:
        from .dps_exact_quotes import DPSExactQuoteManager
        app.state.dps_exact_quotes = DPSExactQuoteManager(store)
    try:
        return await app.state.dps_exact_quotes.quote(
            pilot, width=payload.width, height=payload.height, quantity=payload.quantity,
            selected_options=payload.selected_options,
        )
    except ValueError as exc:
        if str(exc) == 'source_exact_price_unavailable':
            raise HTTPException(503, 'Supplier exact price unavailable') from exc
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        import httpx
        if isinstance(exc, httpx.HTTPError):
            raise HTTPException(503, 'Supplier exact price unavailable') from exc
        raise


@app.post('/api/v1/campaigns/{campaign_id}/dps-rendered-quote/{product_id}',
          dependencies=[Depends(authenticated)])
async def dps_rendered_quote(campaign_id: str, product_id: str, payload: DPSExactQuoteRequest):
    """Observe one complete DPS choice on its native calculator, with a fresh bound price."""
    campaign = active_campaign_product(campaign_id, product_id)
    product = next((p for p in campaign['products'] if p['id'] == product_id), None)
    if not product or product.get('supplier') != 'digital_print_solutions' or product.get('finalized_at'):
        raise HTTPException(409, 'Active unfinished DPS product required')
    evidence = product.get('dps_route_price_evidence') or {}
    pilot_id = evidence.get('native_pilot_id')
    if not pilot_id:
        raise HTTPException(409, 'Saved native DPS pilot required')
    try:
        pilot = dps_pilot_manager().get(pilot_id)
        if pilot.get('url') != product.get('supplier_url'):
            raise ValueError('DPS source mapping changed')
        from .dps_quote_contract import scoped_request
        request = scoped_request(pilot, width=payload.width, height=payload.height,
                                 quantity=payload.quantity, selected_options=payload.selected_options)
        return await dps_pilot_manager().start(product['supplier_url'], expected_request=request)
    except (FileNotFoundError, KeyError):
        raise HTTPException(409, 'Saved native DPS pilot unavailable') from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get('/api/v1/campaigns/{campaign_id}/b2sign-scoped-draft/{product_id}', dependencies=[Depends(authenticated)])
def b2sign_scoped_draft(campaign_id:str,product_id:str):
    try:body=app.state.campaigns.b2sign_scoped_draft(campaign_id,product_id)
    except FileNotFoundError:raise HTTPException(404,'B2Sign scoped draft not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc
    return RawResponse(content=body,media_type='text/csv',headers={
        'Content-Disposition':f'attachment; filename="hermes-b2sign-{product_id}-verification-pending.csv"'})


@app.get('/api/v1/campaigns/{campaign_id}/fourover-shipping-baseline/{product_id}', dependencies=[Depends(authenticated)])
def fourover_shipping_baseline(campaign_id:str,product_id:str):
    try:return app.state.campaigns.fourover_shipping_baseline(campaign_id,product_id)
    except FileNotFoundError:raise HTTPException(404,'4over shipping baseline not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/soardist-public-variants', dependencies=[Depends(authenticated)])
async def collect_soardist_public_variants(campaign_id:str):
    try:return await app.state.campaigns.collect_soardist_variants(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')


@app.get('/api/v1/campaigns/{campaign_id}/soardist-public-variants/{product_id}', dependencies=[Depends(authenticated)])
def soardist_public_variants(campaign_id:str,product_id:str):
    try:return app.state.campaigns.soardist_variant_snapshot(campaign_id,product_id)
    except FileNotFoundError:raise HTTPException(404,'Public variant evidence not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/soardist-account-baselines', dependencies=[Depends(authenticated)])
async def collect_soardist_account_baselines(campaign_id:str, limit:int=28):
    try:
        return await app.state.campaigns.collect_soardist_account_baselines(
            campaign_id, app.state.engine.browser_pool, limit=limit)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/soardist-option-tables', dependencies=[Depends(authenticated)])
async def collect_soardist_option_tables(campaign_id:str,limit:int=28):
    try:return await app.state.campaigns.collect_itoris_option_tables(
        campaign_id,app.state.engine.browser_pool,limit=limit)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/soardist-quantity-tables', dependencies=[Depends(authenticated)])
async def collect_soardist_quantity_tables(campaign_id:str,limit:int=28):
    try:return await app.state.campaigns.collect_itoris_quantity_tables(
        campaign_id,app.state.engine.browser_pool,limit=limit)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@app.get('/api/v1/campaigns/{campaign_id}/soardist-option-probe/{product_id}', dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def soardist_option_probe(campaign_id:str,product_id:str,option_value:str):
    from .soardist_account import PRODUCT_PRICE_JS, observed_price, exact_product_page, ORIGIN
    from .itoris_options import project_itoris_dpo
    from urllib.parse import urlsplit,parse_qsl
    if not option_value.isdecimal() or len(option_value)>30:
        raise HTTPException(422,'Option value must be a source numeric ID')
    try: campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError: raise HTTPException(404,'Campaign not found')
    product=next((p for p in campaign['products'] if p['id']==product_id),None)
    if not product or product.get('supplier')!='soardist' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped Soardist product not found')
    source_url=product.get('supplier_url')
    if not exact_product_page(source_url,source_url) or not urlparse(source_url).path.startswith('/products/'):
        raise HTTPException(422,'Invalid mapped Soardist URL')
    async with app.state.engine.browser_pool.page(ORIGIN,headless=False) as page:
        responses=[]
        page.on('response',lambda response: responses.append(response)
                if response.request.resource_type in {'xhr','fetch'} else None)
        await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        if not exact_product_page(page.url,source_url):
            raise HTTPException(422,'Product page redirected')
        await page.locator('.product-single__meta span.product__price').first.wait_for(state='visible',timeout=12000)
        money_nodes_js="""() => [...document.querySelectorAll('.product-single__meta *')]
          .filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'
            && /\\$\\s*[0-9]/.test((e.innerText || '').trim())
            && ![...e.children].some(c => /\\$\\s*[0-9]/.test((c.innerText || '').trim())))
          .slice(0, 50).map(e => ({tag:e.tagName.toLowerCase(),id:e.id.slice(0,80),
            class_name:typeof e.className==='string'?e.className.slice(0,120):'',
            text:(e.innerText || '').trim().slice(0,160)}))"""
        before=await page.evaluate(PRODUCT_PRICE_JS)
        before_money_nodes=await page.evaluate(money_nodes_js)
        selected=page.locator('.product-single__meta select:visible').filter(
            has=page.locator(f'option[value="{option_value}"]'))
        if await selected.count()!=1:
            raise HTTPException(422,'Option unavailable or ambiguous')
        option_label=await selected.locator(f'option[value="{option_value}"]').first.text_content()
        await selected.select_option(option_value)
        try:
            await page.wait_for_function("""({before}) => {
                const p=[...document.querySelectorAll('.product-single__meta span.product__price')]
                    .filter(e=>e.getClientRects().length)[0];
                return p && (p.innerText||p.textContent||'').trim()!==before;
            }""",{'before':before['prices'][0] if before.get('prices') else ''},timeout=6000)
        except Exception: pass
        after=await page.evaluate(PRODUCT_PRICE_JS)
        after_money_nodes=await page.evaluate(money_nodes_js)
        route_shapes=[]
        option_app=[]
        quantity_app=[]
        for response in responses[-80:]:
            parts=urlsplit(response.url)
            if parts.scheme!='https':continue
            route_shapes.append({'host':parts.hostname,'path':parts.path[:180],
                'parameter_names':sorted({k for k,_ in parse_qsl(parts.query)})[:20],
                'method':response.request.method,'status':response.status,
                'content_type':response.headers.get('content-type','').split(';')[0][:50]})
            if parts.hostname=='node1.itoris.com' and parts.path=='/dpo/storefront/include.js' and response.status==200:
                import hashlib
                try:
                    body=await response.body()
                    at=body.find(option_value.encode())
                    lower=body.lower()
                    policy_terms={}
                    for term in (b'relative',b'absolute',b'fixed',b'onetime',
                                 b'one_time',b'price_type',b'formula'):
                        locations=[match.start() for match in re.finditer(re.escape(term),lower)][:3]
                        if locations:
                            policy_terms[term.decode()]={
                                'count':lower.count(term),
                                'excerpts':[body[max(0,pos-90):pos+150].decode('utf8','replace')
                                            for pos in locations]}
                    option_app.append({'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),
                        'selected_option_id_occurrences':body.count(option_value.encode()),
                        'price_term_occurrences':body.lower().count(b'price'),
                        'policy_terms':policy_terms,
                        'projected_options':project_itoris_dpo(body),
                        'option_excerpt':body[max(0,at-160):at+220].decode('utf8','replace') if at>=0 else None})
                except Exception as exc: option_app.append({'error':type(exc).__name__})
            if parts.hostname=='node1.itoris.com' and parts.path=='/tierprice/storefront/include.js' and response.status==200:
                import hashlib
                try:
                    body=await response.body()
                    quantity_app.append({'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),
                        'quantity_term_occurrences':body.lower().count(b'quantity'),
                        'discount_term_occurrences':body.lower().count(b'discount'),
                        'tier_term_occurrences':body.lower().count(b'tier'),
                        'start_excerpt':body[:600].decode('utf8','replace')})
                except Exception as exc: quantity_app.append({'error':type(exc).__name__})
    return {'product_id':product_id,'source_url':source_url,'option_value':option_value,
            'option_label':(option_label or '').strip()[:160],
            'before_price':observed_price(before),'after_price':observed_price(after),
            'before_money_nodes':before_money_nodes,'after_money_nodes':after_money_nodes,
            'route_shapes':route_shapes,
            'option_app':option_app,
            'quantity_app':quantity_app,
            'scope':'one_source_option_change_only','final_eligible':False}


@app.get('/api/v1/campaigns/{campaign_id}/soardist-quantity-grid/{product_id}',
         dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def soardist_quantity_grid(campaign_id:str,product_id:str,quantities:str):
    """Read rendered quantity/production controls on one signed-in product page."""
    from .soardist_account import PRODUCT_PRICE_JS, observed_price, exact_product_page, ORIGIN
    if not re.fullmatch(r'[0-9]+(?:,[0-9]+){0,11}',quantities or ''):
        raise HTTPException(422,'Provide one to twelve comma-separated quantities')
    values=[int(value) for value in quantities.split(',')]
    if any(not 1<=value<=1000 for value in values) or len(values)!=len(set(values)):
        raise HTTPException(422,'Quantities must be unique integers from 1 to 1000')
    try:campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((item for item in campaign['products'] if item['id']==product_id),None)
    if not product or product.get('supplier')!='soardist' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped Soardist product not found')
    source_url=product.get('supplier_url')
    if not exact_product_page(source_url,source_url):
        raise HTTPException(422,'Invalid mapped Soardist URL')
    capture_js="""() => {
      const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
      const inputs=[...document.querySelectorAll('input[name="quantity"]')].filter(visible)
        .map(e=>({value:e.value,min:e.min,max:e.max,step:e.step,valid:e.validity.valid,
          validation_message:e.validationMessage.slice(0,160)}));
      const selects=[...document.querySelectorAll('.product-single__meta select.product-custom-option')]
        .filter(visible).map(e=>({id:e.id,selected:e.value,options:[...e.options]
          .map(o=>({value:o.value,label:o.textContent.trim().slice(0,160),disabled:o.disabled}))}));
      return {quantity_inputs:inputs,selects};
    }"""
    async with app.state.engine.browser_pool.page(ORIGIN,headless=False) as page:
        await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        if not exact_product_page(page.url,source_url):
            raise HTTPException(422,'Product page redirected')
        quantity=page.locator('input[name="quantity"]:visible')
        if await quantity.count()!=1:
            raise HTTPException(422,'A unique visible quantity input is required')
        observations=[]
        for value in values:
            await quantity.fill(str(value))
            await quantity.dispatch_event('change')
            await page.wait_for_timeout(300)
            snapshot=await page.evaluate(capture_js)
            price=observed_price(await page.evaluate(PRODUCT_PRICE_JS))
            observations.append({'requested_quantity':value,'rendered':snapshot,
                                 'main_unit_price':price})
    return {'product_id':product_id,'source_url':source_url,'observations':observations,
            'scope':'rendered_quantity_controls_only','final_eligible':False}


@app.get('/api/v1/campaigns/{campaign_id}/soardist-pricing-runtime/{product_id}',
         dependencies=[Depends(authenticated),Depends(active_or_queued_soardist_offer)])
async def soardist_pricing_runtime(campaign_id:str,product_id:str,click_customize:bool=False,
                                   probe_quantities:bool=False,candidate_url:str=''):
    """Read source pricing runtime structure without changing a cart or form."""
    from .soardist_account import exact_product_page, ORIGIN
    from .itoris_options import project_itoris_dpo
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    try:campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((item for item in campaign['products'] if item['id']==product_id),None)
    if not product or product.get('supplier')!='soardist' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped Soardist product not found')
    source_url=candidate_url or product.get('supplier_url')
    if not exact_product_page(source_url,source_url) or not urlparse(source_url).path.startswith('/products/'):
        raise HTTPException(422,'Invalid mapped Soardist URL')
    async with app.state.engine.browser_pool.page(ORIGIN,headless=False) as page:
        script_responses=[]
        option_responses=[]
        page.on('response',lambda response:script_responses.append(response)
                if response.url.startswith('https://node1.itoris.com/dpo/storefront/assets/js/options.js?')
                and response.status==200 else None)
        page.on('response',lambda response:option_responses.append(response)
                if response.url.startswith('https://node1.itoris.com/dpo/storefront/include.js?')
                and response.status==200 else None)
        await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        if not exact_product_page(page.url,source_url):
            raise HTTPException(422,'Product page redirected')
        await page.locator('.product-single__meta span.product__price').first.wait_for(state='visible',timeout=12000)
        customize_clicked=False
        pages_before=len(page.context.pages)
        if click_customize:
            control=page.get_by_text('Customize',exact=True)
            if await control.count()!=1:
                raise HTTPException(422,'A unique Customize control is required')
            await control.click(timeout=5000)
            customize_clicked=True
            await page.wait_for_timeout(2000)
        try:
            await page.locator('.product-single__meta select.product-custom-option').first.wait_for(state='visible',timeout=15000)
        except PlaywrightTimeoutError:
            pass
        projection=await page.evaluate("""() => ({
          option_grid_present:!!document.querySelector('.product-single__meta select.product-custom-option'),
          customize_controls:[...document.querySelectorAll('a,button')]
            .filter(e=>(e.innerText||'').trim()==='Customize')
            .map(e=>({tag:e.tagName.toLowerCase(),href:e.getAttribute('href'),
              target:e.getAttribute('target'),class_name:String(e.className).slice(0,160),
              role:e.getAttribute('role')})).slice(0,5),
          visible_dialogs:[...document.querySelectorAll('[role="dialog"],dialog,.modal')]
            .filter(e=>e.getClientRects().length)
            .map(e=>({tag:e.tagName.toLowerCase(),id:e.id,
              class_name:String(e.className).slice(0,120),
              heading:(e.querySelector('h1,h2,h3')?.textContent||'').trim().slice(0,120)})).slice(0,10),
          iframe_origins:[...document.querySelectorAll('iframe')]
            .filter(e=>e.getClientRects().length && e.src)
            .map(e=>{try {const u=new URL(e.src);return u.origin+u.pathname} catch {return null}})
            .filter(Boolean).slice(0,10),
          product_meta_text:(document.querySelector('.product-single__meta')?.innerText||'').slice(0,3500),
          visible_selects:[...document.querySelectorAll('.product-single__meta select')]
            .filter(e=>e.getClientRects().length).map(e=>({name:e.name,id:e.id,value:e.value,
              choices:[...e.options].map(o=>({value:o.value,label:(o.textContent||'').trim().slice(0,100),disabled:o.disabled}))})).slice(0,30),
          variant_inputs:[...document.querySelectorAll('.product-single__meta [name="id"]')]
            .map(e=>({tag:e.tagName.toLowerCase(),type:e.type||null,
              value:e.value||null,visible:!!e.getClientRects().length,
              choices:e.options?[...e.options].map(o=>({value:o.value,label:(o.textContent||'').trim().slice(0,100)})):[]})).slice(0,10),
          scripts:[...document.scripts].map(e=>e.src).filter(u=>/itoris|dpo|tierprice/i.test(u)).slice(0,30),
          forms:[...document.querySelectorAll('.product-single__meta form')].map(f=>({
            action:f.action,method:f.method,inputs:[...f.querySelectorAll('input')]
              .filter(e=>/price|qty|quantity|variant|properties/i.test(e.name||e.id))
              .map(e=>({name:e.name,id:e.id,type:e.type,value:e.type==='hidden'?null:e.value})).slice(0,30)
          })).slice(0,5),
          price_nodes:[...document.querySelectorAll('[class*=price],[id*=price]')]
            .filter(e=>e.getClientRects().length && e.children.length===0)
            .map(e=>({tag:e.tagName.toLowerCase(),class_name:typeof e.className==='string'?e.className.slice(0,100):'',
              text:(e.innerText||'').trim().slice(0,120)})).filter(e=>e.text).slice(0,30),
          runtime_keys:Object.keys(window).filter(k=>/itoris|dpo|tierprice/i.test(k)).slice(0,30)
          ,variant_selector:window.dpoObserver?.selectors?.variantId||null,
          native_variants:window.dpoOptions?.productConfig?.variants?.map(v=>({id:v.id,
            title:v.title,price:v.price,available:v.available}))||[],
          price_formula_present:!!window.PriceFormula,
          extra_js_length:String(window.dpoOptions?.config?.extra_js||'').length,
          datepicker_start_days:Number(String(window.dpoOptions?.config?.extra_js||'')
            .match(/setDate\(d\.getDate\(\)\+(\d+)\)/)?.[1]||0),
          quantity_controls:[...document.querySelectorAll('input[name="quantity"]')]
            .filter(e=>e.getClientRects().length).map(e=>({min:e.min,max:e.max,step:e.step,
              maxlength:e.maxLength,pattern:e.pattern,value:e.value,valid:e.validity.valid})),
          tier_runtime:window.tpObserver?{minQty:window.tpObserver.minQty,
            maxQty:window.tpObserver.maxQty,qtyIncrement:window.tpObserver.qtyIncrement,
            tierCount:Array.isArray(window.tpObserver.tierPrices)?window.tpObserver.tierPrices.length:null}:null,
          configuration_keys:Object.keys(window.dpoOptions?.config||{}).slice(0,100),
          section_conditions:window.dpoOptions?.config?.section_conditions||null,
          option_configuration:(window.dpoOptions?.options||[]).map(o=>({
            id:o.option_id,type:o.type,title:o.title,keys:Object.keys(o).filter(k=>k!=='elm'),
            visibility:o.visibility,visibility_condition:o.visibility_condition,
            visibility_action:o.visibility_action,
            choices:(o.items||[]).map(c=>({id:c.option_type_id,title:c.title,
              visibility:c.visibility,visibility_action:c.visibility_action,
              is_disabled:c.is_disabled}))
          })).slice(0,35)
        })""")
        import hashlib
        script=[]
        for response in script_responses[:1]:
            body=await response.body()
            script.append({'url':response.url,'sha256':hashlib.sha256(body).hexdigest()})
        option_projection=[]
        for response in option_responses[:3]:
            body=await response.body()
            projected=project_itoris_dpo(body)
            if projected.get('field_count',0):
                option_projection.append({'sha256':hashlib.sha256(body).hexdigest(),
                                          'projection':projected})
                break
        designer_frames=[]
        for frame in page.frames:
            if 'launchmywear.com' not in frame.url:continue
            try:
                u=urlparse(frame.url)
                details=await frame.evaluate("""() => ({title:document.title,
                  visible_text:(document.body?.innerText||'').replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+[.][A-Z]{2,}/gi,'[email]').slice(0,3000),
                  headings:[...document.querySelectorAll('h1,h2,h3')].filter(e=>e.getClientRects().length)
                    .map(e=>(e.innerText||'').trim().slice(0,100)).filter(Boolean).slice(0,20),
                  selects:[...document.querySelectorAll('select')].filter(e=>e.getClientRects().length)
                    .map(e=>({name:e.name,id:e.id,choices:[...e.options]
                      .map(o=>(o.textContent||'').trim().slice(0,100)).slice(0,25)})).slice(0,20),
                  buttons:[...document.querySelectorAll('button')].filter(e=>e.getClientRects().length)
                    .map(e=>(e.innerText||'').trim().slice(0,100)).filter(Boolean).slice(0,25),
                  prices:[...document.querySelectorAll('[class*=price],[id*=price]')]
                    .filter(e=>e.getClientRects().length&&e.children.length===0)
                    .map(e=>(e.innerText||'').trim().slice(0,100)).filter(Boolean).slice(0,15)})""")
                designer_frames.append({'origin_path':u.scheme+'://'+u.netloc+u.path,'details':details})
            except Exception:
                designer_frames.append({'status':'frame_unavailable'})
        quantity_probes=[]
        if probe_quantities:
            control=page.locator('input[name="quantity"]:visible')
            if await control.count()!=1:
                raise HTTPException(422,'A unique visible quantity input is required')
            for quantity in (1,10,21,1000):
                await control.fill(str(quantity))
                await control.dispatch_event('change')
                observation=await page.evaluate("""() => ({
                  quantity:document.querySelector('input[name="quantity"]')?.value,
                  valid:document.querySelector('input[name="quantity"]')?.validity.valid,
                  price:(document.querySelector('.product-single__meta span.product__price')?.innerText||'').trim(),
                  tier_count:Array.isArray(window.tpObserver?.tierPrices)?window.tpObserver.tierPrices.length:0
                })""")
                quantity_probes.append(observation)
        post_click_url=page.url
        new_page_count=len(page.context.pages)-pages_before
    return {'product_id':product_id,'source_url':source_url,'projection':projection,
            'customize_clicked':customize_clicked,'post_click_url':post_click_url,
            'new_page_count':new_page_count,
            'designer_frames':designer_frames,
            'quantity_probes':quantity_probes,
            'pricing_scripts':script,
            'option_responses':option_projection,
            'scope':'read_only_runtime_structure','final_eligible':False}


@app.get('/api/v1/campaigns/{campaign_id}/soardist-native-price-grid/{product_id}',
         dependencies=[Depends(authenticated),Depends(active_or_queued_soardist_offer)])
async def soardist_native_price_grid(campaign_id:str,product_id:str,quantities:str,
                                     option_values:str='',variant_id:str='',combined_values:str='',
                                     candidate_url:str=''):
    """Read the installed supplier calculator's own price result, without cart changes."""
    from .soardist_account import exact_product_page, ORIGIN
    if not re.fullmatch(r'[0-9]+(?:,[0-9]+){0,9}',quantities or ''):
        raise HTTPException(422,'Provide one to ten comma-separated quantities')
    qtys=[int(value) for value in quantities.split(',')]
    if len(set(qtys))!=len(qtys) or any(not 1<=value<=1000 for value in qtys):
        raise HTTPException(422,'Quantities must be unique integers from 1 to 1000')
    if option_values and not re.fullmatch(r'[0-9]+(?:,[0-9]+){0,19}',option_values):
        raise HTTPException(422,'Provide at most twenty numeric source option IDs')
    choices=option_values.split(',') if option_values else []
    if len(set(choices))!=len(choices):
        raise HTTPException(422,'Option IDs must be unique')
    if combined_values and not re.fullmatch(r'[0-9]+(?:,[0-9]+){0,19}',combined_values):
        raise HTTPException(422,'Combined choices must be numeric source IDs')
    combined=combined_values.split(',') if combined_values else []
    if len(set(combined))!=len(combined):
        raise HTTPException(422,'Combined choice IDs must be unique')
    if variant_id and not re.fullmatch(r'[0-9]{1,30}',variant_id):
        raise HTTPException(422,'Variant ID must be numeric')
    try:campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((item for item in campaign['products'] if item['id']==product_id),None)
    if not product or product.get('supplier')!='soardist' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped Soardist product not found')
    source_url=candidate_url or product.get('supplier_url')
    if not exact_product_page(source_url,source_url) or not urlparse(source_url).path.startswith('/products/'):
        raise HTTPException(422,'Invalid mapped Soardist URL')
    async with app.state.engine.browser_pool.page(ORIGIN,headless=False) as page:
        await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        if not exact_product_page(page.url,source_url):
            raise HTTPException(422,'Product page redirected')
        await page.locator('.product-single__meta select.product-custom-option').first.wait_for(state='visible',timeout=15000)
        quantity=page.locator('input[name="quantity"]:visible')
        if await quantity.count()!=1:
            raise HTTPException(422,'A unique visible quantity input is required')
        if variant_id:
            variant_selected=await page.evaluate("""id => {
              const variants=window.dpoOptions?.productConfig?.variants||[];
              const selector=window.dpoObserver?.selectors?.variantId;
              const input=selector&&document.querySelector(selector);
              if (!variants.some(v=>String(v.id)===id)||!input) return false;
              input.value=id;
              input.dispatchEvent(new Event('change',{bubbles:true}));
              const element=document.querySelector('#itoris_dynamicproductoptions');
              if (element) element.dpoProductInitialPrices=false;
              return input.value===id;
            }""",variant_id)
            if not variant_selected:
                raise HTTPException(422,'Variant is absent from the source calculator')
        baseline=await page.evaluate("""() => [...document.querySelectorAll('.product-single__meta select.product-custom-option')]
          .filter(e=>e.getClientRects().length).map(e=>({id:e.id,value:e.value,
            choices:[...e.options].map(o=>({value:o.value,label:(o.textContent||'').trim(),disabled:o.disabled}))}))""")
        option_to_field={choice['value']:field for field in baseline for choice in field['choices']}
        if any(value not in option_to_field for value in [*choices,*combined]):
            raise HTTPException(422,'A requested option is missing from the visible source controls')
        if len({option_to_field[value]['id'] for value in combined})!=len(combined):
            raise HTTPException(422,'Combined choices must use distinct source fields')
        observations=[]
        for qty in qtys:
            await quantity.fill(str(qty))
            await quantity.dispatch_event('change')
            for value in [None,*choices,*(['combined'] if combined else [])]:
                for field in baseline:
                    await page.locator(f'#{field["id"]}').select_option(field['value'],timeout=3000)
                selected=combined if value=='combined' else [value] if value is not None else []
                disabled_values=[]
                for selected_value in selected:
                    field_id=option_to_field[selected_value]['id']
                    disabled=await page.locator(f'#{field_id}').evaluate(
                        "(e,value) => [...e.options].find(o=>o.value===value)?.disabled===true",selected_value)
                    if disabled:
                        disabled_values.append(selected_value)
                        continue
                    await page.locator(f'#{field_id}').select_option(selected_value,timeout=3000)
                if disabled_values:
                    observations.append({'quantity':qty,'changed_option_value':value,
                                         'changed_option_values':selected,
                                         'status':'source_disabled','disabled_values':disabled_values})
                    continue
                result=await page.evaluate("""() => {
                  const calculator=window.dpoOptions;
                  if (!calculator || typeof calculator.updatePrice!=='function') return {error:'calculator_missing'};
                  const result=calculator.updatePrice(true);
                  return {current_prices:result?.current_prices||null,
                    initial_prices:result?.initial_prices||null,
                    tier_price:result?.tier_prices?.price??null,
                    quantity:document.querySelector('input[name="quantity"]')?.value||null};
                }""")
                observations.append({'quantity':qty,'changed_option_value':value,
                                     'changed_option_values':selected,
                                     'changed_field':option_to_field[value]['id'] if value in option_to_field else None,
                                     'native_calculation':result})
    return {'product_id':product_id,'source_url':source_url,'baseline_fields':baseline,
            'variant_id':variant_id or None,
            'observations':observations,'scope':'native_dpo_calculator_read_only',
            'final_eligible':False}


@app.get('/api/v1/campaigns/{campaign_id}/soardist-cart-preflight/{product_id}',
         dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def soardist_cart_preflight(campaign_id:str,product_id:str):
    """Read only cart occupancy and visible form controls before a price pilot."""
    from .soardist_account import exact_product_page, ORIGIN
    try:campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((item for item in campaign['products'] if item['id']==product_id),None)
    if not product or product.get('supplier')!='soardist' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped Soardist product not found')
    source_url=product.get('supplier_url')
    if not exact_product_page(source_url,source_url):
        raise HTTPException(422,'Invalid mapped Soardist URL')
    async with app.state.engine.browser_pool.page(ORIGIN,headless=False) as page:
        await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        if not exact_product_page(page.url,source_url):
            raise HTTPException(422,'Product page redirected')
        await page.locator('.product-single__meta span.product__price').first.wait_for(state='visible',timeout=12000)
        controls=await page.evaluate("""() => ({
          buttons:[...document.querySelectorAll('.product-single__meta button')]
            .filter(e=>e.getClientRects().length).map(e=>({text:(e.innerText||'').trim().slice(0,100),
              type:e.type,disabled:e.disabled})).filter(e=>e.text).slice(0,30),
          forms:[...document.querySelectorAll('.product-single__meta form')]
            .map(e=>({action:e.action,method:e.method})).slice(0,10),
          quantity_inputs:[...document.querySelectorAll('.product-single__meta input[name="quantity"]')]
            .filter(e=>e.getClientRects().length).map(e=>({value:e.value,min:e.min,max:e.max})),
          variant_id:document.querySelector('.product-single__meta input[name="id"]')?.value||null
        })""")
        cart=await page.evaluate("""async () => {
          const response=await fetch('/cart.js',{credentials:'same-origin',cache:'no-store'});
          if(!response.ok) return {status:response.status};
          const value=await response.json();
          return {status:response.status,item_count:value.item_count,
            total_price_cents:value.total_price,token_present:!!value.token};
        }""")
    return {'product_id':product_id,'source_url':source_url,'controls':controls,'cart':cart,
            'scope':'read_only_cart_occupancy','final_eligible':False}


@app.get('/api/v1/campaigns/{campaign_id}/b2sign-quantity-control/{product_id}',
         dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def b2sign_quantity_control(campaign_id:str,product_id:str):
    """Read the source's quantity input bounds without making a cart change."""
    from urllib.parse import urlsplit
    try:campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((p for p in campaign['products'] if p['id']==product_id),None)
    if not product or product.get('supplier')!='b2sign' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped B2Sign product not found')
    source_url=product.get('supplier_url')
    parts=urlsplit(source_url or '')
    if (parts.scheme!='https' or parts.hostname not in {'b2sign.com','www.b2sign.com'}
            or parts.query or parts.fragment or parts.username or parts.password):
        raise HTTPException(422,'Invalid mapped B2Sign URL')
    async with app.state.engine.browser_pool.page('https://www.b2sign.com',headless=False) as page:
        await page.goto(source_url,wait_until='domcontentloaded',timeout=30000)
        if urlsplit(page.url).path.rstrip('/')!=parts.path.rstrip('/'):
            raise HTTPException(422,'Product page redirected')
        try:
            await page.locator('div[class*="_instant-quote_"] div[class*="_quantity_"] input').first.wait_for(
                state='visible',timeout=12000)
        except Exception:
            raise HTTPException(422,'Source quantity control unavailable') from None
        controls=await page.evaluate("""() => {
          const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';
          const roots=[...document.querySelectorAll('div[class*="_instant-quote_"]')].filter(visible);
          return [...new Set(roots.flatMap(r=>[...r.querySelectorAll('div[class*="_quantity_"] input')]))]
            .filter(visible).slice(0,10).map(e=>({type:e.type,name:e.name.slice(0,80),
              min:e.getAttribute('min'),max:e.getAttribute('max'),step:e.getAttribute('step'),
              inputmode:e.getAttribute('inputmode'),pattern:e.getAttribute('pattern'),
              maxlength:e.getAttribute('maxlength'),value:/^\\d{1,12}$/.test(e.value)?e.value:null}));
        }""")
    return {'product_id':product_id,'source_url':source_url,'controls':controls,
            'scope':'rendered_quantity_control_only','final_eligible':False}


@app.get('/api/v1/campaigns/{campaign_id}/fourover-probe/{product_id}', dependencies=[Depends(authenticated),Depends(active_or_supplemental_fourover)])
async def fourover_product_probe(campaign_id:str, product_id:str, first_value:str|None=None,
                                colorspec_value:str|None=None, complete_defaults:bool=False):
    from .fourover_probe import PRODUCT_JS, json_shape, price_tiers, area_tiers
    from urllib.parse import urlsplit, parse_qsl
    try: campaign = app.state.campaigns.get(campaign_id)
    except FileNotFoundError: raise HTTPException(404,'Campaign not found')
    product = next((p for p in campaign['products'] if p['id'] == product_id), None)
    if product is None:
        product = next((p for p in campaign.get('supplemental_product_jobs',[])
                        if p.get('storefront_product_id')==product_id
                        and p.get('supplier')=='fourover' and p.get('stage')=='queued'),None)
    if not product or product.get('supplier') != 'fourover' or product.get('stage') == 'excluded':
        raise HTTPException(404,'Mapped 4over product not found')
    source_url = product.get('supplier_url')
    parts = urlsplit(source_url or '')
    if parts.scheme != 'https' or parts.hostname not in {'4over.com','www.4over.com'} or parts.query or parts.fragment:
        raise HTTPException(422,'Invalid mapped 4over URL')
    if first_value is not None and (len(first_value)>100 or not first_value.isdecimal()):
        raise HTTPException(422,'First choice must be a source option ID')
    if colorspec_value is not None and (first_value is None or len(colorspec_value)>100 or not colorspec_value.isdecimal()):
        raise HTTPException(422,'Colorspec choice must follow a source size choice')
    async with app.state.engine.browser_pool.page('https://4over.com', headless=False) as page:
        responses=[]
        page.on('response',lambda response: responses.append(response)
                if response.request.resource_type in {'xhr','fetch'} and
                urlsplit(response.url).hostname in {'4over.com','www.4over.com'} else None)
        await page.goto(source_url, wait_until='domcontentloaded', timeout=45000)
        current = urlsplit(page.url)
        if current.hostname not in {'4over.com','www.4over.com'} or current.path.rstrip('/') != parts.path.rstrip('/'):
            return {'product_id':product_id,'status':'product_page_redirected'}
        try: await page.locator('h1.page-title').wait_for(state='visible',timeout=15000)
        except Exception: pass
        try: await page.locator('.column.main select').first.wait_for(state='visible',timeout=12000)
        except Exception: pass
        observation = await page.evaluate(PRODUCT_JS)
        if first_value is not None:
            first=next((f for f in observation['fields'] if any(c['value']==first_value and not c['disabled'] for c in f['choices'])),None)
            if first is None or not first['id']:
                raise HTTPException(422,'First choice unavailable on this product')
            await page.locator('#'+first['id']).select_option(first_value)
            try: await page.wait_for_function("""() => [...document.querySelectorAll('.column.main select')]
                .some(e => !e.disabled && e.options.length > 1 && e.value === '')""",timeout=5000)
            except Exception: pass
            observation=await page.evaluate(PRODUCT_JS)
        if colorspec_value is not None:
            color=next((f for f in observation['fields'] if 'colorspec' in f['label'].lower()
                        and any(c['value']==colorspec_value and not c['disabled'] for c in f['choices'])),None)
            if color is None or not color['id']:
                raise HTTPException(422,'Colorspec choice unavailable on this product')
            await page.locator('#'+color['id']).select_option(colorspec_value)
            try: await page.wait_for_function("""() => [...document.querySelectorAll('.column.main select')]
                .some(e => !e.disabled && e.id.toLowerCase().includes('coating') && e.options.length > 1)""",timeout=5000)
            except Exception: pass
            observation=await page.evaluate(PRODUCT_JS)
        if complete_defaults:
            for _ in range(20):
                selectable=next((f for f in observation['fields'] if not f['disabled'] and not f['selected']
                                 and f['id'] and any(c['value'] and not c['disabled'] for c in f['choices'])),None)
                if selectable is None: break
                choice=next(c['value'] for c in selectable['choices'] if c['value'] and not c['disabled'])
                await page.locator('#'+selectable['id']).select_option(choice)
                try:
                    await page.wait_for_function("""({id,value}) => {
                      const control=document.getElementById(id); return control && control.value===value;
                    }""",{'id':selectable['id'],'value':choice},timeout=3000)
                except Exception: pass
                observation=await page.evaluate(PRODUCT_JS)
            else:
                raise HTTPException(422,'Dependent option walk exceeded safe bound')
            for _ in range(30):
                if any(urlsplit(response.url).path.startswith('/rest/default/V1/product/prices/')
                       and response.status == 200 for response in responses): break
                await asyncio.sleep(0.1)
        routes=[]
        price_route_shapes=[]
        page_cache_summary=[]
        for response in responses[-200:]:
            parsed=urlsplit(response.url)
            routes.append({'path':parsed.path[:200],
                           'parameter_names':sorted({key for key,_ in parse_qsl(parsed.query)})[:30],
                           'method':response.request.method,'status':response.status,
                           'request_header_names':sorted(response.request.headers.keys()),
                           'content_type':response.headers.get('content-type','').split(';')[0][:50]})
            if parsed.path.startswith('/page_cache/block/render/id/') and response.status==200:
                try:
                    cache=await response.json()
                    page_cache_summary.append({'shape':json_shape(cache),
                                               'keys':list(cache)[:30] if isinstance(cache,dict) else []})
                except (ValueError,TypeError): pass
            if (parsed.path.startswith('/rest/default/V1/product/prices/')
                    and response.status == 200 and len(price_route_shapes) < 3):
                try:
                    body = await response.json()
                    price_route_shapes.append({'path':parsed.path[:200],
                                               'method':response.request.method,
                                               'request_shape':json_shape(response.request.post_data_json)
                                                   if response.request.post_data and response.request.headers.get('content-type','').startswith('application/json') else None,
                                               'shape':json_shape(body), 'package_prices':price_tiers(body),
                                               'tier_prices':area_tiers(body)})
                except Exception: price_route_shapes.append({'path':parsed.path[:200],
                                                              'shape':{'type':'unreadable'}})
    return {'product_id':product_id,'source_url':source_url,'status':'rendered_snapshot',
            'observation':observation,'route_shapes':routes,'price_route_shapes':price_route_shapes,
            'page_cache_summary':page_cache_summary,
            'price_scope':'unverified_rendered_product_state'}


@app.post('/api/v1/campaigns/{campaign_id}/fourover-default-probes/{product_id}', dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def save_fourover_default_probe(campaign_id:str, product_id:str):
    probe=await fourover_product_probe(campaign_id,product_id,complete_defaults=True)
    if probe.get('status')!='rendered_snapshot':
        raise HTTPException(422,'Mapped 4over product did not render')
    try:return app.state.campaigns.attach_fourover_default_probe(campaign_id,product_id,probe)
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/fourover-price-tables/{product_id}', dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def harvest_fourover_price_tables(campaign_id:str, product_id:str):
    """Fetch each source-declared child once; each response carries quantity tiers."""
    from .fourover_bulk import child_configurations
    from .fourover_probe import price_tiers, area_tiers
    try: campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError: raise HTTPException(404,'Campaign not found')
    product=next((p for p in campaign['products'] if p['id']==product_id),None)
    if not product or product.get('supplier')!='fourover' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped 4over product not found')
    source_url=product.get('supplier_url')
    parsed=urlparse(source_url or '')
    if parsed.scheme!='https' or parsed.hostname not in {'4over.com','www.4over.com'} or parsed.query or parsed.fragment:
        raise HTTPException(422,'Invalid mapped 4over URL')
    async with app.state.engine.browser_pool.page('https://4over.com',headless=False) as page:
        document_response=await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        current=urlparse(page.url)
        if current.hostname not in {'4over.com','www.4over.com'} or current.path.rstrip('/')!=parsed.path.rstrip('/'):
            raise HTTPException(409,'4over product page redirected; check supplier session')
        try: children=child_configurations(await document_response.text() if document_response else '')
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
        observations=[]
        for child in children:
            child_id=child['child_id']
            route=f'https://4over.com/rest/default/V1/product/prices/{child_id}?_={int(time.time()*1000)}'
            status='unavailable'; package=None; area=None
            for attempt in range(3):
                reply=await page.evaluate('''async url => {
                  const controller=new AbortController();
                  const timer=setTimeout(()=>controller.abort(),20000);
                  try {
                    const response=await fetch(url,{credentials:'same-origin',signal:controller.signal,
                      headers:{'x-requested-with':'XMLHttpRequest','content-type':'application/json'}});
                    return {status:response.status,body:response.ok?await response.json():null};
                  } finally {clearTimeout(timer)}
                }''',route)
                if reply['status'] in {429,500,502,503,504}:
                    await asyncio.sleep(min(10,2**attempt))
                    continue
                if reply['status']==200:
                    try:
                        body=reply['body']
                        package=price_tiers(body)
                        area=area_tiers(body)
                        if package is not None or area is not None:
                            status='priced'
                    except (ValueError,TypeError): pass
                elif reply['status'] in {401,403}:
                    raise HTTPException(409,'4over pricing session expired')
                break
            observations.append({'child_id':child_id,'status':status,
                                 'package_prices':package,'tier_prices':area})
            await asyncio.sleep(0.18)
    try:return app.state.campaigns.attach_fourover_price_tables(
        campaign_id,product_id,source_url,children,observations)
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


class FourOverChildPricesRequest(BaseModel):
    model_config = {"extra": "forbid"}
    snapshot_id: StrictStr = Field(pattern=r'^[a-f0-9]{32}$')
    child_ids: list[StrictStr] = Field(max_length=10)
    request_budget: StrictInt = Field(ge=1, le=11)
    sets: StrictInt = Field(ge=1, le=1)
    job_sample: StrictBool
    digital_proof: StrictBool


class FourOverCollectedEvidenceRequest(BaseModel):
    model_config = {"extra":"forbid"}
    snapshot_id: StrictStr = Field(pattern=r'^[a-f0-9]{32}$')
    expected_checkpoint_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    expected_option_rows_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    sets: StrictInt = Field(ge=1,le=1)
    job_sample: StrictBool
    digital_proof: StrictBool
    materialize_rows: StrictBool
    finalize: StrictBool


class FourOverNativeObservationRequest(BaseModel):
    model_config = {"extra":"forbid", "json_schema_extra":{
        "description":"One reviewed, once-only native10359/160495/q2000 source-cost observation. Zeroed example provenance/fingerprint values are placeholders; replace only with separately reviewed exact pins. Historical q500/q1000 pairs are read-only and never repeatable. Services remain off; this does not finalize a product or import retail prices.",
        "examples":[{"snapshot_id":"0"*32,"expected_checkpoint_sha256":"0"*64,
            "expected_option_rows_sha256":"0"*64,"child_id":"160495","package_quantity":2000,
            "sets":1,"job_sample":False,"digital_proof":False,
            "allow_summary_panel_without_shipping":True,"attach_canary":False,
            "expected_observer_sha256":"0"*64}]}}
    snapshot_id: StrictStr = Field(pattern=r'^[a-f0-9]{32}$')
    expected_checkpoint_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    expected_option_rows_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    child_id: StrictStr = Field(pattern=r'^(?:160399|160492|160495)$')
    package_quantity: StrictInt = Field(ge=500,le=2000)
    sets: StrictInt = Field(ge=1,le=1)
    job_sample: StrictBool
    digital_proof: StrictBool
    allow_summary_panel_without_shipping: StrictBool
    attach_canary: StrictBool
    expected_observer_sha256: StrictStr | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def exact_reviewed_pair(self):
        if ((self.child_id,self.package_quantity) not in {
                ('160399',500),('160492',500),('160495',500),('160495',1000),('160495',2000)}
                or self.package_quantity in (1000,2000) and self.expected_observer_sha256 is None):
            raise ValueError('Only exact historical pairs or the reviewed native2000 pair are supported')
        return self


class FourOverBootstrapDiagnosisRequest(BaseModel):
    model_config = {"extra":"forbid"}
    snapshot_id: StrictStr = Field(pattern=r'^[a-f0-9]{32}$')
    expected_checkpoint_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    expected_option_rows_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')
    expected_observer_sha256: StrictStr = Field(pattern=r'^[a-f0-9]{64}$')


@app.post('/api/v1/campaigns/{campaign_id}/fourover-bootstrap-diagnosis/{product_id}',
          dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def diagnose_fourover_static_bootstrap(campaign_id:str,product_id:str,payload:FourOverBootstrapDiagnosisRequest):
    """One asset-only diagnosis per reviewed observer import; no quote selections."""
    if product_id!='10359':raise HTTPException(422,'Only the fixed queued10359 bootstrap diagnosis is supported')
    from .fourover_native_observation import NativeObserver,Held
    if getattr(app.state,'fourover_native_observer',None) is None:
        app.state.fourover_native_observer=NativeObserver(store,app.state.engine.browser_pool)
    try:
        return await app.state.fourover_native_observer.diagnose_bootstrap(app.state.campaigns,campaign_id,
            payload.snapshot_id,payload.expected_checkpoint_sha256,payload.expected_option_rows_sha256,
            expected_observer_sha256=payload.expected_observer_sha256)
    except FileNotFoundError:raise HTTPException(404,'Pinned source cache unavailable') from None
    except (Held,ValueError,KeyError,TypeError,OSError):raise HTTPException(409,'Pinned bootstrap diagnosis scope needs review') from None


@app.post('/api/v1/campaigns/{campaign_id}/fourover-collected-evidence/{product_id}',
          dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def attach_fourover_collected_evidence(campaign_id:str,product_id:str,payload:FourOverCollectedEvidenceRequest):
    """Attach all pinned historical 10359 tables/bindings; no source/CSV/import."""
    if (product_id!='10359' or payload.job_sample is not False or payload.digital_proof is not False
            or payload.materialize_rows is not False or payload.finalize is not False):
        raise HTTPException(422,'Only the fixed queued historical evidence scope is supported')
    from .fourover_collected_evidence import attach_cache,mapped_product,SOURCE
    from .fourover_child_prices import FourOverChildPriceCollector
    manager=app.state.campaigns
    async def check():mapped_product(manager,campaign_id)
    reader=FourOverChildPriceCollector(store,None)
    try:
        async with reader._locked(reader._path(payload.snapshot_id,SOURCE),check):
            return await asyncio.to_thread(attach_cache,manager,campaign_id,payload.snapshot_id,
                payload.expected_checkpoint_sha256,payload.expected_option_rows_sha256)
    except FileNotFoundError:raise HTTPException(404,'Pinned source cache unavailable') from None
    except (ValueError,KeyError,TypeError,OSError):raise HTTPException(409,'Pinned source evidence or campaign needs review') from None


@app.post('/api/v1/campaigns/{campaign_id}/fourover-native-observation/{product_id}',
          dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def observe_fourover_native_context(campaign_id:str,product_id:str,payload:FourOverNativeObservationRequest):
    """One retained 10359 native context; forbidden routes blocked before transport."""
    if (product_id!='10359' or payload.job_sample is not False or payload.digital_proof is not False
            or payload.allow_summary_panel_without_shipping is not True or payload.attach_canary is not False):
        raise HTTPException(422,'Only the approved one-context no-shipping observation is supported')
    from .fourover_native_observation import NativeObserver,Held
    if getattr(app.state,'fourover_native_observer',None) is None:
        app.state.fourover_native_observer=NativeObserver(store,app.state.engine.browser_pool)
    try:
        kwargs={'child_id':payload.child_id,'package_quantity':payload.package_quantity}
        if payload.package_quantity in (1000,2000):
            kwargs['expected_observer_sha256']=payload.expected_observer_sha256
        return await app.state.fourover_native_observer.observe(app.state.campaigns,campaign_id,
            payload.snapshot_id,payload.expected_checkpoint_sha256,payload.expected_option_rows_sha256,
            **kwargs)
    except FileNotFoundError:raise HTTPException(404,'Pinned source cache unavailable') from None
    except (Held,ValueError,KeyError,TypeError,OSError):raise HTTPException(409,'Pinned native source scope needs review') from None


def fourover_child_price_collector():
    if app.state.fourover_child_prices is None:
        from .fourover_child_prices import FourOverChildPriceCollector
        app.state.fourover_child_prices = FourOverChildPriceCollector(
            store, app.state.engine.browser_pool)
    return app.state.fourover_child_prices


@app.post('/api/v1/campaigns/{campaign_id}/fourover-child-prices/{product_id}',
          dependencies=[Depends(authenticated), Depends(active_campaign_product)])
async def collect_fourover_child_prices(campaign_id: str, product_id: str,
                                       payload: FourOverChildPricesRequest):
    """Read a bounded subset; no legacy table attachment or completion claim."""
    campaign = active_campaign_product(campaign_id, product_id)
    product = next((p for p in campaign['products'] if p['id'] == product_id), None)
    if not product or product.get('supplier') != 'fourover' or product.get('stage') == 'excluded':
        raise HTTPException(409, 'An active mapped 4over product is required')
    if payload.job_sample is not False or payload.digital_proof is not False:
        raise HTTPException(422, 'Only one-set, sample-off, proof-off table evidence is supported')
    source_url = product.get('supplier_url')

    async def check_scope():
        current = active_campaign_product(campaign_id, product_id)
        matched = next((p for p in current['products'] if p['id'] == product_id), None)
        if (not matched or matched.get('supplier') != 'fourover'
                or matched.get('stage') == 'excluded'
                or matched.get('supplier_url') != source_url):
            raise ValueError('The active supplier mapping changed; stop this collection')

    try:
        from .fourover_child_prices import _url
        _url(source_url)
        return await fourover_child_price_collector().collect(
            payload.snapshot_id, source_url, payload.child_ids,
            request_budget=payload.request_budget,
            scope={'sets': payload.sets, 'job_sample': payload.job_sample,
                   'digital_proof': payload.digital_proof}, check=check_scope)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/fourover-option-map/{product_id}', dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def harvest_fourover_option_map(campaign_id:str, product_id:str):
    """Bind each rendered option path to its source child ID; hold incomplete maps."""
    from .fourover_probe import PRODUCT_JS
    try: campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError: raise HTTPException(404,'Campaign not found')
    product=next((p for p in campaign['products'] if p['id']==product_id),None)
    if not product or product.get('supplier')!='fourover' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped 4over product not found')
    source_url=product.get('supplier_url')
    parsed=urlparse(source_url or '')
    if parsed.scheme!='https' or parsed.hostname not in {'4over.com','www.4over.com'} or parsed.query or parsed.fragment:
        raise HTTPException(422,'Invalid mapped 4over URL')
    rows=[]; selected_count=0; last_child=''
    async with app.state.engine.browser_pool.page('https://4over.com',headless=False) as page:
        await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        current=urlparse(page.url)
        if current.hostname not in {'4over.com','www.4over.com'} or current.path.rstrip('/')!=parsed.path.rstrip('/'):
            raise HTTPException(409,'4over product page redirected; check supplier session')
        try:
            await page.locator('.column.main select').first.wait_for(state='visible',timeout=15000)
        except PlaywrightTimeoutError as exc:
            raise HTTPException(
                409,
                '4over calculator unavailable in the saved browser session; check supplier sign-in before retrying',
            ) from exc

        async def walk(processed=frozenset()):
            nonlocal selected_count,last_child
            if len(processed)>25 or selected_count>5000 or len(rows)>2000:
                raise ValueError('Dependent option walk exceeded safe bound')
            snapshot=await page.evaluate(PRODUCT_JS)
            field=next((f for f in snapshot['fields'] if not f['disabled'] and f['id']
                        and f['id'] not in processed
                        and any(c['value'] and not c['disabled'] for c in f['choices'])),None)
            if field is None:
                try:
                    await page.wait_for_function('''previous => {
                      const control=document.querySelector('input[name="selected_configurable_option"]');
                      return control && control.value && control.value!==previous;
                    }''',last_child,timeout=3000)
                except Exception: pass
                snapshot=await page.evaluate(PRODUCT_JS)
                child=snapshot.get('selected_child_id') or ''
                if child and child!=last_child and child.isdecimal():
                    fields=[]
                    for f in snapshot['fields']:
                        if f['selected']:
                            choice=next((c for c in f['choices'] if c['value']==f['selected']),None)
                            fields.append({'label':f['label'],'value':f['selected'],
                                           'choice':choice['label'] if choice else ''})
                    rows.append({'child_id':child,'fields':fields})
                    last_child=child
                return
            choices=[c['value'] for c in field['choices'] if c['value'] and not c['disabled']]
            for value in choices:
                fresh=await page.evaluate(PRODUCT_JS)
                current_field=next((f for f in fresh['fields'] if f['id']==field['id']),None)
                if not current_field or current_field['disabled'] or not any(
                        c['value']==value and not c['disabled'] for c in current_field['choices']):
                    continue
                await page.locator('#'+field['id']).select_option(value)
                selected_count+=1
                await asyncio.sleep(0.12)
                await walk(processed | {field['id']})
        try: await walk()
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    try:return {**app.state.campaigns.attach_fourover_option_map(campaign_id,product_id,source_url,rows),
                'select_actions':selected_count}
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/fourover-draft/{product_id}', dependencies=[Depends(authenticated),Depends(active_campaign_product)])
def materialize_fourover_draft(campaign_id:str,product_id:str):
    try:return app.state.campaigns.materialize_fourover_package_rows(campaign_id,product_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@app.get('/api/v1/campaigns/{campaign_id}/fourover-draft/{product_id}', dependencies=[Depends(authenticated)])
def download_fourover_draft(campaign_id:str,product_id:str):
    import hashlib
    import re
    try: campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((p for p in campaign['products'] if p['id']==product_id),None)
    evidence=(product or {}).get('fourover_rows_evidence') or {}
    relative=evidence.get('object_path','')
    if (not product or product.get('supplier')!='fourover'
            or not isinstance(relative,str)
            or not re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.fourover-draft\.csv',relative)
            or evidence.get('source_url')!=product.get('supplier_url')):
        raise HTTPException(404,'Draft rows not found')
    root=Path(store.root).resolve()
    path=(root/relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=evidence.get('sha256'):
        raise HTTPException(409,'Draft row evidence unavailable or changed')
    return FileResponse(path,media_type='text/csv',filename=f'hermes-{product_id}-verification-pending.csv')


@app.post('/api/v1/campaigns/{campaign_id}/fourover-area-draft/{product_id}', dependencies=[Depends(authenticated),Depends(active_campaign_product)])
def materialize_fourover_area_draft(campaign_id:str,product_id:str,dimension_grid:list[dict]):
    try:return app.state.campaigns.materialize_fourover_area_rows(campaign_id,product_id,dimension_grid)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(422,str(exc)) from exc


@app.get('/api/v1/campaigns/{campaign_id}/fourover-area-draft/{product_id}', dependencies=[Depends(authenticated)])
def download_fourover_area_draft(campaign_id:str,product_id:str):
    import hashlib
    import re
    try: campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((p for p in campaign['products'] if p['id']==product_id),None)
    evidence=(product or {}).get('fourover_area_rows_evidence') or {}
    relative=evidence.get('object_path','')
    if (not product or product.get('supplier')!='fourover' or not isinstance(relative,str)
            or not re.fullmatch(r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.fourover-area-draft\.csv',relative)
            or evidence.get('source_url')!=product.get('supplier_url')):
        raise HTTPException(404,'Area draft rows not found')
    root=Path(store.root).resolve();path=(root/relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=evidence.get('sha256'):
        raise HTTPException(409,'Area draft evidence unavailable or changed')
    return FileResponse(path,media_type='text/csv',filename=f'hermes-{product_id}-one-set-verification-pending.csv')


@app.post('/api/v1/campaigns/{campaign_id}/fourover-shipping-pilot/{product_id}', dependencies=[Depends(authenticated),Depends(active_or_supplemental_fourover)])
async def fourover_shipping_pilot(campaign_id:str,product_id:str,width:int|None=None,height:int|None=None,
                                 child_id:str|None=None,sets:int=1,replay_shipping_rate:bool=False,
                                 probe_child_id:str|None=None,job_sample:bool=False,
                                 digital_proof:bool=False,turnaround_time:int|None=None,
                                 package_quantity:int|None=None,source_size_value:str|None=None,
                                 source_stock_value:str|None=None):
    """Advance one exact default selection to the shipping calculator, never checkout."""
    from .fourover_probe import PRODUCT_JS
    import re
    try: campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((p for p in campaign['products'] if p['id']==product_id),None)
    if product is None:
        product=next((p for p in campaign.get('supplemental_product_jobs',[])
                      if p.get('storefront_product_id')==product_id
                      and p.get('supplier')=='fourover' and p.get('stage')=='queued'),None)
    if not product or product.get('supplier')!='fourover' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped 4over product not found')
    if source_size_value is not None and (product.get('storefront_product_id')!=product_id
            or not re.fullmatch(r'[0-9]{1,12}',source_size_value)):
        raise HTTPException(422,'Source size override is only for a queued supplemental product')
    if source_stock_value is not None and (product.get('storefront_product_id')!=product_id
            or source_size_value is None or not re.fullmatch(r'[0-9]{1,12}',source_stock_value)):
        raise HTTPException(422,'Source stock override requires a queued supplemental product and size')
    source_url=product.get('supplier_url')
    if ((width is None)!=(height is None) or (width is not None and not (1<=width<=1000 and 1<=height<=1000))
            or not 1<=sets<=10000 or turnaround_time not in (None,1,2)
            or (package_quantity is not None and not 1<=package_quantity<=100000)):
        raise HTTPException(422,'Both bounded dimensions are required')
    target_fields=None
    probe_fields=None
    if probe_child_id is not None and (child_id is None or not replay_shipping_rate or probe_child_id==child_id):
        raise HTTPException(422,'A distinct mapped base child and replay check are required')
    if child_id is not None or probe_child_id is not None:
        import hashlib
        import re
        evidence=product.get('fourover_option_map_evidence') or {}
        relative=evidence.get('object_path','')
        if (any(not value.isdecimal() for value in (child_id,probe_child_id) if value is not None)
                or not isinstance(relative,str) or not re.fullmatch(
                r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.fourover-option-map\.json',relative)):
            raise HTTPException(422,'Mapped child required')
        root=Path(store.root).resolve();path=(root/relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(422,'Option map unavailable')
        body=path.read_bytes()
        if hashlib.sha256(body).hexdigest()!=evidence.get('sha256'):
            raise HTTPException(409,'Option map changed')
        mapping=json.loads(body)
        if child_id is not None:
            target=next((row for row in mapping['rows'] if row['child_id']==child_id),None)
            if target is None:raise HTTPException(422,'Mapped child required')
            target_fields=target['fields']
        if probe_child_id is not None:
            probe_target=next((row for row in mapping['rows'] if row['child_id']==probe_child_id),None)
            if probe_target is None:raise HTTPException(422,'Mapped probe child required')
            probe_fields=probe_target['fields']
    routes=[]
    shipping_request_shapes=[]
    shipping_response_shapes=[]
    project_request_shapes=[]
    project_response_shapes=[]
    shipping_payloads=[]
    project_payloads=[]
    pending_shipping_shapes=set()
    def safe_shape(value,depth=0):
        if depth>5:return 'depth_limit'
        if isinstance(value,dict):return {str(key)[:80]:safe_shape(item,depth+1) for key,item in list(value.items())[:40]}
        if isinstance(value,list):return {'type':'list','length':len(value),'item_shape':safe_shape(value[0],depth+1) if value else None}
        return type(value).__name__
    def request_shape(request):
        path=urlparse(request.url).path
        if path not in {'/print/shipment/shippingRates/','/print/project/summary/'}:return
        try:
            parsed=request.post_data_json
            body=safe_shape(parsed)
            values={key:str(parsed[key])[:40] for key in ('product_id','run_size','custom_run_size')
                    if isinstance(parsed,dict) and key in parsed}
            if isinstance(parsed,dict) and path=='/print/shipment/shippingRates/':shipping_payloads.append(parsed)
            if isinstance(parsed,dict) and path=='/print/project/summary/':project_payloads.append(parsed)
        except Exception:body='unreadable';values={}
        record={'method':request.method,'body_shape':body,'safe_values':values}
        if path=='/print/shipment/shippingRates/':shipping_request_shapes.append(record)
        else:project_request_shapes.append(record)
    async def response_shape(response):
        path=urlparse(response.url).path
        if path not in {'/print/shipment/shippingRates/','/print/project/summary/'}:return
        parsed=None
        try:
            parsed=await response.json()
            body=safe_shape(parsed)
            projected=[]
            if isinstance(parsed,list) and len(parsed)<=30:
                for item in parsed:
                    if not isinstance(item,dict):continue
                    projected.append({key:item.get(key) for key in ('method_code','method_title','amount',
                                      'available','production_estimate','shipping_time','in_hand_delivery',
                                      'handling_fee')})
        except Exception:body='unreadable';projected=[]
        if path=='/print/shipment/shippingRates/':
            shipping_response_shapes.append({'status':response.status,'body_shape':body,'rates':projected})
        else:project_response_shapes.append({'status':response.status,'body_shape':body,
                                              'totals':parsed.get('totals') if isinstance(parsed,dict) else None})
    def queue_response_shape(response):
        if urlparse(response.url).path not in {'/print/shipment/shippingRates/','/print/project/summary/'}:return
        task=asyncio.create_task(response_shape(response))
        pending_shipping_shapes.add(task)
        task.add_done_callback(pending_shipping_shapes.discard)
    async with app.state.engine.browser_pool.page('https://4over.com',headless=False) as page:
        page.on('request',request_shape)
        page.on('response',queue_response_shape)
        page.on('response',lambda response:routes.append((urlparse(response.url).path,response.status))
                if response.request.resource_type in {'xhr','fetch'} and
                urlparse(response.url).hostname in {'4over.com','www.4over.com'} else None)
        await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        if urlparse(page.url).path.rstrip('/')!=urlparse(source_url).path.rstrip('/'):
            raise HTTPException(409,'Supplier page redirected')
        try:
            await page.locator('.column.main select').first.wait_for(state='visible',timeout=15000)
        except PlaywrightTimeoutError as exc:
            raise HTTPException(
                409,
                '4over calculator unavailable in the saved browser session; check supplier sign-in before retrying',
            ) from exc
        snapshot=await page.evaluate(PRODUCT_JS)
        if source_size_value is not None:
            source_size=next((f for f in snapshot['fields'] if f['label'].lower().startswith('size')
                              and f['id'] and any(c['value']==source_size_value and not c['disabled']
                                                  for c in f['choices'])),None)
            if source_size is None:
                raise HTTPException(422,'Requested source size unavailable')
            await page.locator('#'+source_size['id']).select_option(source_size_value)
            await asyncio.sleep(0.12)
            snapshot=await page.evaluate(PRODUCT_JS)
        if source_stock_value is not None:
            source_stock=next((f for f in snapshot['fields'] if f['label'].lower().startswith('stock')
                               and f['id'] and any(c['value']==source_stock_value and not c['disabled']
                                                   for c in f['choices'])),None)
            if source_stock is None:
                raise HTTPException(422,'Requested source stock unavailable')
            await page.locator('#'+source_stock['id']).select_option(source_stock_value)
            await asyncio.sleep(0.12)
            snapshot=await page.evaluate(PRODUCT_JS)
        if target_fields is not None:
            for chosen in target_fields:
                field=next((f for f in snapshot['fields'] if f['label']==chosen['label'] and f['id']
                            and any(c['value']==chosen['value'] and not c['disabled'] for c in f['choices'])),None)
                if field is None:raise HTTPException(422,'Mapped option unavailable in rendered calculator')
                await page.locator('#'+field['id']).select_option(chosen['value'])
                await asyncio.sleep(0.12)
                snapshot=await page.evaluate(PRODUCT_JS)
        else:
            for _ in range(20):
                field=next((f for f in snapshot['fields'] if not f['disabled'] and not f['selected']
                            and f['id'] and any(c['value'] and not c['disabled'] for c in f['choices'])),None)
                if field is None:break
                choice=next(c['value'] for c in field['choices'] if c['value'] and not c['disabled'])
                await page.locator('#'+field['id']).select_option(choice)
                await asyncio.sleep(0.12)
                snapshot=await page.evaluate(PRODUCT_JS)
        if not snapshot.get('selected_child_id'):
            raise HTTPException(422,'Default child selection unavailable')
        if child_id is not None and snapshot['selected_child_id']!=child_id:
            raise HTTPException(422,'Rendered child did not match requested options')
        if package_quantity is not None:
            selected=await page.evaluate('''quantity => {
              const rows=[...document.querySelectorAll('li.runsizes_row')];
              const matches=rows.filter(row=>row.querySelector('span.runsize')?.textContent?.trim()===String(quantity));
              if(matches.length!==1)return false;
              matches[0].click();
              return true;
            }''',package_quantity)
            if not selected:
                raise HTTPException(422,'Requested package quantity is unavailable for this child')
            await asyncio.sleep(0.5)
        try:
            await page.locator('input[name="turnaround_time"]').first.wait_for(state='attached',timeout=3000)
        except Exception:
            pass
        pre_shipping_run_controls=await page.evaluate('''() => {
          const clean=s=>String(s||'').replace(/\\s+/g,' ').trim().slice(0,120);
          const visible=e=>e.getClientRects().length>0;
          const root=document.querySelector('.column.main')||document.querySelector('main');
          return [...(root?.querySelectorAll('input,select,button,[role="radio"],[role="option"]')||[])]
            .filter(visible).slice(0,100).map(e=>({tag:e.tagName.toLowerCase(),name:clean(e.name),id:clean(e.id),
              type:clean(e.type),checked:e.type==='radio'||e.type==='checkbox'?!!e.checked:undefined,
              label:clean(e.closest('.field')?.querySelector('label')?.textContent),
              value:clean(e.value),text:clean(e.textContent),parent_text:clean(e.parentElement?.textContent),
              choices:e.tagName==='SELECT'?[...e.options].slice(0,40).map(o=>({
                value:clean(o.value),label:clean(o.textContent),disabled:!!o.disabled})):undefined}));
        }''')
        pre_shipping_quantity_nodes=await page.evaluate('''() => {
          const clean=s=>String(s||'').replace(/\\s+/g,' ').trim();
          const root=document.querySelector('.column.main')||document.querySelector('main');
          return [...(root?.querySelectorAll('*')||[])]
            .filter(e=>e.getClientRects().length>0 && /^(?:100|250|500|1,?000|25,?000)$/.test(
              clean([...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join(''))))
            .slice(0,30).map(e=>({tag:e.tagName.toLowerCase(),class_name:clean(e.className).slice(0,100),
              text:clean(e.textContent).slice(0,80),parent_tag:e.parentElement?.tagName.toLowerCase(),
              parent_class:clean(e.parentElement?.className).slice(0,100),
              parent_text:clean(e.parentElement?.textContent).slice(0,180)}));
        }''')
        for control_name, selected in (('job_sample',job_sample),('digital_proof',digital_proof)):
            control=page.locator(f'input[name="{control_name}"]')
            count=await control.count()
            # Some 4over products have no sample/proof add-on. A missing
            # control can only represent the explicitly requested off state.
            if count==0 and not selected:
                continue
            if count!=1 or not await control.is_enabled():
                raise HTTPException(422,'Requested service control unavailable')
            await control.evaluate('(e,want) => { if (e.checked !== want) e.click(); }',selected)
            if await control.is_checked()!=selected:
                raise HTTPException(422,'Requested service control did not remain selected')
        turnaround_selected_before_shipping=False
        if turnaround_time is not None:
            early=page.locator(f'input[name="turnaround_time"][value="{turnaround_time}"]')
            if await early.count()==1 and await early.is_visible() and await early.is_enabled():
                await early.evaluate('(e) => e.click()')
                turnaround_selected_before_shipping=await early.is_checked()
                if not turnaround_selected_before_shipping:
                    raise HTTPException(422,'Requested turnaround did not remain selected')
        button=page.locator('.column.main button').filter(has_text='Proceed to Shipping')
        try: await button.first.wait_for(state='visible',timeout=6000)
        except Exception: pass
        if await button.count()!=1:
            raise HTTPException(422,'Shipping step unavailable')
        name=page.locator('input[name="project_name"]')
        if await name.count() and await name.is_visible():
            await name.fill('Hermes pricing verification')
        if width is not None:
            for dimension in ('width','height'):
                control=page.locator(f'select[name="dynamic_size[{dimension}]"]')
                try:await control.wait_for(state='attached',timeout=10000)
                except Exception:raise HTTPException(422,'Dimension selector unavailable') from None
                await control.evaluate("e => {e.value=''; e.dispatchEvent(new Event('change',{bubbles:true}))}")
            await button.click(timeout=10000)
            try:await page.locator('select[name="dynamic_size[width]"]').wait_for(state='visible',timeout=10000)
            except Exception:raise HTTPException(422,'Dimension selector did not open') from None
            for dimension,value in [('width',width),('height',height)]:
                control=page.locator(f'select[name="dynamic_size[{dimension}]"]')
                if await control.count()!=1:
                    raise HTTPException(422,'Dimension selector unavailable')
                await control.select_option(str(value))
            dimension_control=await page.evaluate('''() => Object.fromEntries(
              ['width','height'].map(name => {
                const controls=[...document.querySelectorAll(`select[name="dynamic_size[${name}]"]`)];
                if (controls.length!==1) return [name,{present:false}];
                const e=controls[0], option=e.selectedOptions[0];
                return [name,{present:true,value:e.value,valid:e.checkValidity(),
                  enabled:!e.disabled && !!option && !option.disabled}];
              }))''')
            if any(dimension_control.get(name)!={'present':True,'value':str(value),
                   'valid':True,'enabled':True} for name,value in (('width',width),('height',height))):
                raise HTTPException(422,'Supplier did not retain requested dimensions')
        else:
            dimension_control=None
        before=len(routes)
        await button.click(timeout=10000)
        control=page.locator('input[type="number"]:visible')
        try:await control.wait_for(state='visible',timeout=10000)
        except Exception:raise HTTPException(422,'Sets control unavailable') from None
        if await control.count()!=1:raise HTTPException(422,'Sets control ambiguous')
        await control.fill(str(sets))
        await control.press('Tab')
        await asyncio.sleep(1)
        set_control=await control.evaluate('''e => ({value:e.value,min:e.min,max:e.max,step:e.step,
          valid:e.checkValidity(),rangeOverflow:e.validity.rangeOverflow,
          rangeUnderflow:e.validity.rangeUnderflow})''')
        if await control.input_value() != str(sets):
            raise HTTPException(422,'Supplier rejected requested set count')
        if not set_control['valid']:
            raise HTTPException(422,'Supplier set count is outside its rendered limits')
        if turnaround_time is not None and not turnaround_selected_before_shipping:
            turnaround=page.locator(f'input[name="turnaround_time"][value="{turnaround_time}"]')
            try: await turnaround.wait_for(state='attached',timeout=8000)
            except Exception: raise HTTPException(422,'Requested turnaround is unavailable for this child') from None
            if await turnaround.count()!=1 or not await turnaround.is_enabled():
                raise HTTPException(422,'Requested turnaround is unavailable for this child')
            await turnaround.evaluate('(e) => e.click()')
            if not await turnaround.is_checked():
                raise HTTPException(422,'Requested turnaround did not remain selected')
            if await button.is_visible():
                await button.click(timeout=10000)
            await asyncio.sleep(1)
        try:
            await page.wait_for_function('''() => [...document.querySelectorAll('[class*="subtotal"]')]
              .some(e=>e.getClientRects().length>0 && /\$\s*\d+[,.]\d{2}/.test(e.textContent||''))''',timeout=12000)
        except Exception: await asyncio.sleep(1)
        if product_id == '6742':
            # 4over can return a nonzero project summary while the shipping
            # step still paints "Price Per Set: $0.00". A zero placeholder is
            # not an exact source price. Wait for both visible amounts; the
            # reducer below still fails closed if they never reconcile.
            try:
                await page.wait_for_function('''() => {
                  const texts=[...document.querySelectorAll('span,strong,b,div')]
                    .filter(e=>e.getClientRects().length>0)
                    .map(e=>String(e.textContent||'').replace(/\\s+/g,' ').trim())
                    .filter(t=>t.length<80);
                  const per=texts.find(t=>/^Price Per Set:\\s*\\$[0-9,]+\\.[0-9]{2}$/.test(t));
                  const total=texts.find(t=>/\\bTotal:\\s*\\$[0-9,]+\\.[0-9]{2}/.test(t));
                  const amount=per && Number(per.match(/\\$([0-9,]+\\.[0-9]{2})/)[1].replace(/,/g,''));
                  return amount>0 && !!total;
                }''',timeout=8000)
            except Exception:
                pass
        after=await page.evaluate('''() => {
          const clean=s=>String(s||'').replace(/\\s+/g,' ').trim().slice(0,120);
          const visible=e=>e.getClientRects().length>0;
          const buttons=[...document.querySelectorAll('button')].filter(visible)
            .map(e=>clean(e.textContent)).filter(Boolean).slice(0,30);
          const amounts=[...document.querySelectorAll('[class*="subtotal"], [data-bind*="subtotal"], [class*="shipping-rate"]')]
            .filter(visible).map(e=>clean(e.textContent)).filter(Boolean).slice(0,20);
          const money=[...document.querySelectorAll('span, strong, b, div')]
            .filter(e=>visible(e) && /\$\s*\d/.test(clean(e.textContent,100)) && clean(e.textContent,100).length<80)
            .map(e=>clean(e.textContent)).filter(Boolean).slice(0,30);
          const fields=[...document.querySelectorAll('input,select')].filter(visible)
            .slice(0,50).map(e=>({name:clean(e.name),type:clean(e.type),
              value:clean(e.value),checked:e.type==='radio'||e.type==='checkbox'?!!e.checked:undefined,
              label:clean(e.closest('.field')?.querySelector('label')?.textContent),
              choice_count:e.tagName==='SELECT'?e.options.length:undefined,
              choices:e.tagName==='SELECT'?[...e.options].slice(0,30).map(o=>({
                value:clean(o.value),label:clean(o.textContent),disabled:!!o.disabled})):undefined}));
          const dimensions=[...document.querySelectorAll('select[name^="dynamic_size["]')]
            .filter(visible).map(e=>({name:clean(e.name),selected:clean(e.value),
              choices:[...e.options].map(o=>({value:clean(o.value),label:clean(o.textContent),disabled:!!o.disabled}))}));
          const quantityTexts=[...document.querySelectorAll('label,span,strong,div')]
            .filter(visible).filter(e=>/^(?:run size|quantity|qty|sets)\\s*[:*]?$/i.test(clean(e.textContent)))
            .slice(0,20).map(e=>({label:clean(e.textContent),context:clean(e.parentElement?.textContent)}));
          const packageSizeNodes=[...document.querySelectorAll('span,div,td,label,li')]
            .filter(visible).filter(e=>/^(?:100|250|500|1,?000|25,?000)$/.test(
              clean([...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join(''))))
            .slice(0,30).map(e=>({tag:e.tagName.toLowerCase(),text:clean(e.textContent),
              parent_text:clean(e.parentElement?.textContent)}));
          return {buttons,amounts,money:[...new Set(money)],fields,dimensions,quantityTexts,packageSizeNodes};
        }''')
        rendered_sets=[field.get('value') for field in after.get('fields') or []
                       if field.get('type')=='number' and field.get('label')=='Sets']
        if rendered_sets and rendered_sets!=[str(sets)]:
            raise HTTPException(422,'Supplier changed requested set count')
        import re
        canary=None
        amounts=after.get('amounts') or []
        observed=amounts[0] if len(amounts)==1 else None
        if observed is not None:
            subtotal_match=re.fullmatch(r'(?:Subtotal:\s*)?(\$[0-9,]+\.[0-9]{2})',observed)
            observed=subtotal_match.group(1) if subtotal_match else None
        basis='shipping_step_subtotal'
        if observed is None:
            candidates=[re.fullmatch(r'Price Per Set:\s*(\$[0-9,]+\.[0-9]{2})',item)
                        for item in after.get('money') or []]
            candidates=[match.group(1) for match in candidates if match]
            if len(set(candidates))==1:
                observed=candidates[0];basis='price_per_set'
        # Source package prices describe one set. A multi-set subtotal must not
        # replace the saved one-set price canary or be compared as a tier price.
        if (observed is not None and width is None and sets == 1 and not job_sample
                and not digital_proof and turnaround_time is None and package_quantity is None):
            try:canary=app.state.campaigns.attach_fourover_rendered_canary(
                campaign_id,product_id,source_url,snapshot['selected_child_id'],observed,basis)
            except ValueError as exc:canary={'status':'held','reason':str(exc)[:100]}
        if pending_shipping_shapes:
            await asyncio.gather(*pending_shipping_shapes,return_exceptions=True)
        from decimal import Decimal
        selector_rates={}
        for field in after.get('fields') or []:
            if field.get('type')!='select-one':continue
            for choice in field.get('choices') or []:
                code=re.fullmatch(r'mapi_(\d+)',choice.get('value') or '')
                amount=re.search(r'\$\s*([0-9,]+\.\d{2})',choice.get('label') or '')
                if code and amount:
                    selector_rates[code.group(1)]=Decimal(amount.group(1).replace(',',''))
        native_rates={}
        if shipping_response_shapes and shipping_response_shapes[-1].get('rates'):
            for rate in shipping_response_shapes[-1]['rates']:
                if rate.get('available') and rate.get('method_code') and rate.get('amount') is not None:
                    native_rates[str(rate['method_code'])]=Decimal(str(rate['amount']))
        shipping_rate_binding={'status':'matched' if selector_rates and selector_rates==native_rates else 'held',
                               'selector_count':len(selector_rates),'native_count':len(native_rates)}
        replay={'status':'not_requested'}
        option_probe={'status':'not_requested'}
        if replay_shipping_rate:
            if shipping_rate_binding['status']!='matched' or not shipping_payloads or not shipping_response_shapes or not shipping_response_shapes[-1].get('rates'):
                replay={'status':'held','reason':'native_shipping_request_missing'}
            else:
                expected=[{key:item.get(key) for key in ('method_code','amount','available','production_estimate')}
                          for item in shipping_response_shapes[-1]['rates']]
                echoed=await page.evaluate('''async payload => {
                  const response=await fetch('/print/shipment/shippingRates/',{
                    method:'POST',credentials:'same-origin',
                    headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                      'X-Requested-With':'XMLHttpRequest'},
                    body:new URLSearchParams(payload).toString()});
                  const body=await response.json().catch(()=>null);
                  return {status:response.status,rates:Array.isArray(body)&&body.length<=30?
                    body.map(item=>({method_code:item.method_code,amount:item.amount,
                      available:item.available,production_estimate:item.production_estimate})):[]};
                }''',shipping_payloads[-1])
                replay={'status':'matched' if echoed['status']==200 and echoed['rates']==expected else 'held',
                        'http_status':echoed['status'],'rate_count':len(echoed['rates']),
                        'reason':None if echoed['status']==200 and echoed['rates']==expected else 'same_context_replay_mismatch'}
        if probe_fields is not None:
            if replay['status']!='matched' or not project_payloads:
                option_probe={'status':'held','reason':'same_context_replay_required'}
            else:
                payload=dict(shipping_payloads[-1])
                project_payload=dict(project_payloads[-1])
                for chosen in probe_fields:
                    field=next((f for f in snapshot['fields'] if f['label']==chosen['label'] and
                                f.get('name','').startswith('super_attribute[') and
                                any(c['value']==chosen['value'] and not c['disabled'] for c in f['choices'])),None)
                    if field is None:
                        option_probe={'status':'held','reason':'mapped_option_not_rendered'}
                        break
                    parameter=field['name'].replace('super_attribute','product_options[super_attribute]',1)
                    if parameter not in payload or parameter not in project_payload:
                        option_probe={'status':'held','reason':'shipping_route_option_parameter_missing'}
                        break
                    payload[parameter]=chosen['value']
                    project_payload[parameter]=chosen['value']
                else:
                    summary=await page.evaluate('''async payload => {
                      const response=await fetch('/print/project/summary/',{
                        method:'POST',credentials:'same-origin',
                        headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                          'X-Requested-With':'XMLHttpRequest'},
                        body:new URLSearchParams(payload).toString()});
                      const body=await response.json().catch(()=>null);
                      return {status:response.status,totals:body&&typeof body==='object'?body.totals:null};
                    }''',project_payload)
                    if summary['status']!=200 or not isinstance(summary.get('totals'),dict):
                        option_probe={'status':'held','reason':'changed_project_summary_unavailable',
                                      'summary_http_status':summary['status']}
                        changed=None
                    else:
                        changed=await page.evaluate('''async payload => {
                          const response=await fetch('/print/shipment/shippingRates/',{
                            method:'POST',credentials:'same-origin',
                            headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',
                              'X-Requested-With':'XMLHttpRequest'},
                            body:new URLSearchParams(payload).toString()});
                          const body=await response.json().catch(()=>null);
                          return {status:response.status,rates:Array.isArray(body)&&body.length<=30?
                            body.map(item=>({method_code:item.method_code,method_title:item.method_title,
                              amount:item.amount,available:item.available,
                              production_estimate:item.production_estimate})):[]};
                        }''',payload)
                    if changed is not None:
                        same_rates=(changed['status']==200 and
                                    [{key:item.get(key) for key in ('method_code','amount','available','production_estimate')}
                                     for item in changed['rates']]==expected)
                        option_probe={'status':'held',
                                      'target_child_id':probe_child_id,
                                      'summary_http_status':summary['status'],
                                      'summary_totals':summary['totals'],
                                      'http_status':changed['status'],
                                      'rates':changed['rates'],
                                      'same_as_base_rates':same_rates,
                                      'reason':'independent_rendered_target_check_required',
                                      'verification':'not_bound_to_target_configuration'}
        return {'product_id':product_id,'selected_child_id':snapshot['selected_child_id'],
                'width':width,'height':height,'sets':sets,'set_control':set_control,
                'dimension_control':dimension_control,
                'selected_turnaround_time':turnaround_time,
                'selected_package_quantity':package_quantity,
                'selected_services':{'job_sample':job_sample,'digital_proof':digital_proof},
                'pre_shipping_run_controls':pre_shipping_run_controls,
                'pre_shipping_quantity_nodes':pre_shipping_quantity_nodes,
                'page_path':urlparse(page.url).path,'response_paths':routes[before:][-30:],
                'shipping_request_shapes':shipping_request_shapes[-3:],
                'shipping_response_shapes':shipping_response_shapes[-3:],
                'project_request_shapes':project_request_shapes[-3:],
                'project_response_shapes':project_response_shapes[-3:],
                'shipping_rate_binding':shipping_rate_binding,
                'shipping_rate_replay':replay,
                'shipping_rate_option_probe':option_probe,
                'shipping_view':after,'canary':canary,
                'scope':'one_default_configuration_no_order_placed'}


class FourOverGatorExactQuoteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    child_id: str = Field(pattern=r"^[0-9]+$", min_length=1, max_length=16)
    width: int = Field(ge=6, le=36)
    height: int = Field(ge=6, le=36)
    sets: int = Field(ge=1, le=200)


@app.post('/api/v1/campaigns/{campaign_id}/fourover-gator-exact-quote/{product_id}',
          dependencies=[Depends(authenticated), Depends(active_campaign_product)])
async def fourover_gator_exact_quote(campaign_id: str, product_id: str,
                                    payload: FourOverGatorExactQuoteRequest,
                                    require_fresh: bool = False):
    """One current source selection; never extrapolate saved one-set rows."""
    from .fourover_exact_quote import summarize_gator_quote
    from .fourover_quote_archive import GatorQuoteArchive
    if product_id != '6742':
        raise HTTPException(422, 'Mapped Gator Board product required')
    campaign = app.state.campaigns.get(campaign_id)
    product = next((p for p in campaign['products'] if p['id'] == product_id), None)
    if not product or product.get('supplier_url') != 'https://4over.com/gator-board-signs':
        raise HTTPException(409, 'Pinned Gator Board source required')
    selection = {'product_id': product_id, 'source_url': product['supplier_url'],
                 'child_id': payload.child_id, 'width_in': payload.width,
                 'height_in': payload.height, 'sets': payload.sets}
    archive = GatorQuoteArchive(store.root)
    try:
        saved = archive.get(campaign_id, selection)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if saved and not require_fresh:
        return {**saved['quote'], 'cache': {'status': 'historical_evidence',
                'captured_at': saved['captured_at'], 'price_fresh': False,
                'checkout_eligible': False}}
    pilot = await fourover_shipping_pilot(campaign_id, product_id, width=payload.width,
                                          height=payload.height, child_id=payload.child_id,
                                          sets=payload.sets)
    try:
        quote = summarize_gator_quote(pilot, product_id=product_id, child_id=payload.child_id,
                                      width=payload.width, height=payload.height, sets=payload.sets)
        record = archive.save(campaign_id, selection, quote)
        return {**quote, 'cache': {'status': 'new_source_observation',
                'captured_at': record['captured_at'], 'price_fresh': True,
                'checkout_eligible': False}}
    except ValueError as exc:
        from .fourover_quote_conflicts import save_gator_quote_conflict
        save_gator_quote_conflict(store.root, selection, pilot, str(exc))
        raise HTTPException(409, str(exc)) from exc


@app.get('/api/v1/campaigns/{campaign_id}/fourover-production-grid/{product_id}/{child_id}', dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def fourover_production_grid(campaign_id:str,product_id:str,child_id:str):
    """Read every visible package tier's turnaround choices in one source page visit."""
    from .fourover_probe import PRODUCT_JS
    import hashlib
    import re
    campaign=app.state.campaigns.get(campaign_id)
    product=next((p for p in campaign['products'] if p['id']==product_id),None)
    evidence=(product or {}).get('fourover_option_map_evidence') or {}
    relative=evidence.get('object_path','')
    if (not product or product.get('supplier')!='fourover'
            or not re.fullmatch(r'[0-9]+',child_id)
            or not isinstance(relative,str) or not re.fullmatch(
                r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.fourover-option-map\.json',relative)):
        raise HTTPException(422,'Mapped 4over child required')
    path=(Path(store.root)/relative).resolve()
    if not path.is_relative_to(Path(store.root).resolve()) or not path.is_file():
        raise HTTPException(409,'Option map unavailable')
    body=path.read_bytes()
    if hashlib.sha256(body).hexdigest()!=evidence.get('sha256'):
        raise HTTPException(409,'Option map changed')
    mapping=json.loads(body)
    target=next((row for row in mapping['rows'] if row['child_id']==child_id),None)
    if target is None:
        raise HTTPException(422,'Mapped child unavailable')
    source_url=product['supplier_url']
    async with app.state.engine.browser_pool.page('https://4over.com',headless=False) as page:
        await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        if urlparse(page.url).path.rstrip('/')!=urlparse(source_url).path.rstrip('/'):
            raise HTTPException(409,'Supplier page redirected')
        await page.locator('.column.main select').first.wait_for(state='visible',timeout=15000)
        snapshot=await page.evaluate(PRODUCT_JS)
        for chosen in target['fields']:
            field=next((f for f in snapshot['fields'] if f['label']==chosen['label'] and f['id']
                        and any(c['value']==chosen['value'] and not c['disabled'] for c in f['choices'])),None)
            if field is None:
                raise HTTPException(422,'Mapped option unavailable in rendered calculator')
            await page.locator('#'+field['id']).select_option(chosen['value'])
            await asyncio.sleep(0.12)
            snapshot=await page.evaluate(PRODUCT_JS)
        if snapshot.get('selected_child_id')!=child_id:
            raise HTTPException(422,'Rendered child mismatch')
        try:await page.locator('li.runsizes_row').first.wait_for(state='visible',timeout=10000)
        except Exception:raise HTTPException(422,'Package tiers unavailable') from None
        quantities=await page.evaluate('''() => [...document.querySelectorAll('li.runsizes_row')]
          .filter(row=>row.getClientRects().length>0)
          .map(row=>Number((row.querySelector('span.runsize')?.textContent||'').replace(/,/g,'')))
          .filter(value=>Number.isInteger(value)&&value>0)''')
        if not quantities or len(quantities)>100 or len(set(quantities))!=len(quantities):
            raise HTTPException(422,'Finite package tier list unavailable')
        tiers=[]
        for quantity in quantities:
            selected=await page.evaluate('''quantity => {
              const rows=[...document.querySelectorAll('li.runsizes_row')]
                .filter(row=>row.getClientRects().length>0 &&
                  Number((row.querySelector('span.runsize')?.textContent||'').replace(/,/g,''))===quantity);
              if(rows.length!==1)return false;
              rows[0].click();return true;
            }''',quantity)
            if not selected:raise HTTPException(422,'Package tier changed during probe')
            try:await page.locator('input[name="turnaround_time"]').first.wait_for(state='attached',timeout=3500)
            except Exception:pass
            await asyncio.sleep(0.25)
            controls=await page.evaluate('''() => [...document.querySelectorAll('input[name="turnaround_time"]')]
              .filter(input=>input.getClientRects().length>0)
              .map(input=>({value:String(input.value),checked:!!input.checked,
                disabled:!!input.disabled,label:String(input.closest('.field')?.querySelector('label')?.textContent||
                  input.closest('label')?.textContent||'').replace(/\\s+/g,' ').trim()}))''')
            tiers.append({'quantity':quantity,'turnaround_choices':controls})
        return {'product_id':product_id,'child_id':child_id,'source_url':source_url,
                'option_map_sha256':evidence['sha256'],'tiers':tiers,
                'scope':'rendered_package_and_turnaround_choices_only_no_order'}


@app.get('/api/v1/campaigns/{campaign_id}/fourover-dimension-probe/{product_id}', dependencies=[Depends(authenticated),Depends(active_campaign_product)])
async def fourover_dimension_probe(campaign_id:str,product_id:str,child_id:str):
    """Read enabled height choices at each supplier width for one mapped child."""
    from .fourover_probe import PRODUCT_JS
    import hashlib
    import re
    try:campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((p for p in campaign['products'] if p['id']==product_id),None)
    if not product or product.get('supplier')!='fourover' or product.get('stage')=='excluded':
        raise HTTPException(404,'Mapped 4over product not found')
    evidence=product.get('fourover_option_map_evidence') or {}
    relative=evidence.get('object_path','')
    if not child_id.isdecimal() or not isinstance(relative,str) or not re.fullmatch(
            r'objects/[a-f0-9]{2}/[a-f0-9]{64}\.fourover-option-map\.json',relative):
        raise HTTPException(422,'Mapped child required')
    root=Path(store.root).resolve();path=(root/relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():raise HTTPException(422,'Option map unavailable')
    body=path.read_bytes()
    if hashlib.sha256(body).hexdigest()!=evidence.get('sha256'):
        raise HTTPException(409,'Option map changed')
    mapping=json.loads(body)
    target=next((row for row in mapping['rows'] if row['child_id']==child_id),None)
    if target is None:raise HTTPException(422,'Mapped child required')
    source_url=product['supplier_url']
    async with app.state.engine.browser_pool.page('https://4over.com',headless=False) as page:
        await page.goto(source_url,wait_until='domcontentloaded',timeout=45000)
        if urlparse(page.url).path.rstrip('/')!=urlparse(source_url).path.rstrip('/'):
            raise HTTPException(409,'Supplier page redirected')
        await page.locator('.column.main select').first.wait_for(state='visible',timeout=15000)
        snapshot=await page.evaluate(PRODUCT_JS)
        for chosen in target['fields']:
            field=next((f for f in snapshot['fields'] if f['label']==chosen['label'] and f['id']
                        and any(c['value']==chosen['value'] and not c['disabled'] for c in f['choices'])),None)
            if field is None:raise HTTPException(422,'Mapped option unavailable')
            await page.locator('#'+field['id']).select_option(chosen['value'])
            await asyncio.sleep(0.1)
            snapshot=await page.evaluate(PRODUCT_JS)
        if snapshot.get('selected_child_id')!=child_id:
            raise HTTPException(422,'Rendered child mismatch')
        for name in ('width','height'):
            control=page.locator(f'select[name="dynamic_size[{name}]"]')
            try:await control.wait_for(state='attached',timeout=10000)
            except Exception:raise HTTPException(422,'Dimension controls unavailable') from None
            await control.evaluate("e => {e.value=''; e.dispatchEvent(new Event('change',{bubbles:true}))}")
        project=page.locator('input[name="project_name"]')
        if await project.count() and await project.is_visible():
            await project.fill('Hermes dimension diagnosis')
        await page.locator('.column.main button').filter(has_text='Proceed to Shipping').click(timeout=10000)
        width_control=page.locator('select[name="dynamic_size[width]"]')
        height_control=page.locator('select[name="dynamic_size[height]"]')
        try:await width_control.wait_for(state='visible',timeout=10000)
        except Exception:raise HTTPException(422,'Dimension choices did not open') from None
        widths=await width_control.evaluate('''e => [...e.options]
          .filter(o=>o.value && !o.disabled).map(o=>o.value)''')
        if not 1<=len(widths)<=200 or any(not str(value).isdecimal() for value in widths):
            raise HTTPException(422,'Width choices unsupported')
        grid={}
        for width in widths:
            await width_control.select_option(width)
            await asyncio.sleep(0.08)
            heights=await height_control.evaluate('''e => [...e.options]
              .filter(o=>o.value && !o.disabled).map(o=>o.value)''')
            if len(heights)>200 or any(not str(value).isdecimal() for value in heights):
                raise HTTPException(422,'Height choices unsupported')
            grid[width]=heights
    return {'product_id':product_id,'child_id':child_id,'source_url':source_url,
            'width_count':len(grid),'valid_dimension_pairs':sum(len(v) for v in grid.values()),
            'heights_by_width':grid,'scope':'rendered_enabled_dimension_choices'}


@app.post('/api/v1/campaigns', dependencies=[Depends(authenticated)])
def create_campaign(payload:CampaignRequest):
    try:return app.state.campaigns.create(**payload.model_dump())
    except (ValueError,KeyError) as exc:raise HTTPException(400,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/analyze', dependencies=[Depends(authenticated)])
async def analyze_campaign(campaign_id:str):
    try:return await app.state.campaigns.analyze(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc


class CampaignMappingsRequest(BaseModel):
    model_config = {"extra":"forbid"}
    products:list[dict] = Field(min_length=1,max_length=1000)


class CampaignExclusion(BaseModel):
    model_config = {"extra":"forbid"}
    id:StrictStr = Field(min_length=1,max_length=100)
    reason:StrictStr = Field(min_length=1,max_length=1000)


class CampaignExclusionsRequest(BaseModel):
    model_config = {"extra":"forbid"}
    products:list[CampaignExclusion] = Field(min_length=1,max_length=1000)


@app.post('/api/v1/campaigns/{campaign_id}/exclusions', dependencies=[Depends(authenticated)])
def exclude_campaign_products(campaign_id:str,payload:CampaignExclusionsRequest):
    try:return app.state.campaigns.exclude_products(campaign_id,[p.model_dump() for p in payload.products])
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/mappings', dependencies=[Depends(authenticated)])
def update_campaign_mappings(campaign_id:str,payload:CampaignMappingsRequest):
    try:return app.state.campaigns.update_mappings(campaign_id,payload.products)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/reinspect', dependencies=[Depends(authenticated)])
def reinspect_campaign(campaign_id:str):
    try:return app.state.campaigns.reinspect_saved(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class CampaignCollectionRequest(BaseModel):
    model_config = {"extra":"forbid"}
    product_id:str
    job_id:str
    source_url:str


@app.post('/api/v1/campaigns/{campaign_id}/source-collection', dependencies=[Depends(authenticated)])
def attach_campaign_collection(campaign_id:str,payload:CampaignCollectionRequest):
    try:return app.state.campaigns.attach_collection(campaign_id,payload.product_id,payload.job_id,payload.source_url)
    except FileNotFoundError:raise HTTPException(404,'Campaign or source evidence not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class CampaignDiagnosisRequest(BaseModel):
    model_config = {"extra":"forbid"}
    product_id:str
    diagnosis_id:str


@app.post('/api/v1/campaigns/{campaign_id}/diagnosis', dependencies=[Depends(authenticated)])
def attach_campaign_diagnosis(campaign_id:str,payload:CampaignDiagnosisRequest):
    try:return app.state.campaigns.attach_diagnosis(campaign_id,payload.product_id,payload.diagnosis_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class CampaignCompletionRequest(BaseModel):
    model_config = {"extra":"forbid"}
    product_id:StrictStr = Field(min_length=1,max_length=100)
    review_id:StrictStr = Field(pattern=r'^[a-f0-9]{32}$')


class FourOverOneSetReviewRequest(BaseModel):
    model_config = {"extra":"forbid"}
    sets:StrictInt
    job_sample:bool
    digital_proof:bool


@app.post('/api/v1/campaigns/{campaign_id}/fourover-one-set-review/{product_id}', dependencies=[Depends(authenticated),Depends(active_campaign_product)])
def prepare_fourover_one_set_review(campaign_id:str,product_id:str,payload:FourOverOneSetReviewRequest):
    from .fourover_completion import review_finite_one_set
    try:campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((item for item in campaign['products'] if item['id']==product_id),None)
    if product is None:raise HTTPException(404,'Campaign product not found')
    try:return review_finite_one_set(product,Path(store.root),payload.model_dump())
    except (ValueError,FileNotFoundError,KeyError,json.JSONDecodeError) as exc:
        raise HTTPException(409,str(exc)) from exc


class DigitalEnvelopeReviewRequest(BaseModel):
    model_config = {"extra":"forbid"}
    sets:StrictInt
    job_sample:bool
    digital_proof:bool
    production:StrictStr


@app.post('/api/v1/campaigns/{campaign_id}/digital-envelopes-review/{product_id}',
          dependencies=[Depends(authenticated),Depends(active_campaign_product)])
def prepare_digital_envelopes_review(campaign_id:str,product_id:str,payload:DigitalEnvelopeReviewRequest):
    from .fourover_completion import review_digital_envelopes_one_set
    try:campaign=app.state.campaigns.get(campaign_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    product=next((item for item in campaign['products'] if item['id']==product_id),None)
    if product is None:raise HTTPException(404,'Campaign product not found')
    try:return review_digital_envelopes_one_set(product,Path(store.root),payload.model_dump())
    except (ValueError,FileNotFoundError,KeyError,json.JSONDecodeError) as exc:
        raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/complete-review', dependencies=[Depends(authenticated)])
def attach_campaign_complete_review(campaign_id:str,payload:CampaignCompletionRequest):
    try:return app.state.campaigns.attach_complete_review(campaign_id,payload.product_id,payload.review_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class CampaignCompletionBatchRequest(BaseModel):
    model_config = {"extra":"forbid"}
    reviews:list[CampaignCompletionRequest] = Field(min_length=1,max_length=100)


class CampaignCompletionGroupsRequest(BaseModel):
    model_config = {"extra":"forbid"}
    groups:list[list[StrictStr]] = Field(min_length=1,max_length=100)


class CampaignAlternateSupplierRequest(BaseModel):
    model_config = {"extra":"forbid"}
    storefront_product_id:StrictStr = Field(min_length=1,max_length=100)
    supplier:StrictStr = Field(min_length=2,max_length=40)
    source_title:StrictStr = Field(min_length=1,max_length=200)
    supplier_url:StrictStr | None = None


class CampaignAlternateSupplierCompletionRequest(BaseModel):
    model_config = {"extra":"forbid"}
    job_id:StrictStr = Field(min_length=1,max_length=160)
    review_id:StrictStr = Field(pattern=r'^[a-f0-9]{32}$')


class CampaignSupplementalProductRequest(BaseModel):
    model_config = {"extra":"forbid"}
    storefront_product_id:StrictStr = Field(pattern=r'^[0-9]{1,12}$')
    storefront_title:StrictStr = Field(min_length=1,max_length=200)
    storefront_url:StrictStr = Field(min_length=1,max_length=4096)
    supplier:StrictStr = Field(min_length=2,max_length=40)
    supplier_url:StrictStr = Field(min_length=1,max_length=4096)
    reason:StrictStr = Field(min_length=1,max_length=500)


class CampaignHoldRequest(BaseModel):
    model_config = {"extra":"forbid"}
    product_id:StrictStr = Field(min_length=1,max_length=100)
    reason:StrictStr = Field(min_length=1,max_length=500)


@app.post('/api/v1/campaigns/{campaign_id}/hold-product', dependencies=[Depends(authenticated)])
def hold_campaign_product(campaign_id:str,payload:CampaignHoldRequest):
    try:return app.state.campaigns.hold_product(campaign_id,payload.product_id,payload.reason)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/alternate-suppliers', dependencies=[Depends(authenticated)])
def queue_campaign_alternate_supplier(campaign_id:str,payload:CampaignAlternateSupplierRequest):
    try:return app.state.campaigns.queue_alternate_supplier(
        campaign_id,payload.storefront_product_id,payload.supplier,
        payload.source_title,payload.supplier_url)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/supplemental-products', dependencies=[Depends(authenticated)])
def queue_campaign_supplemental_product(campaign_id:str,payload:CampaignSupplementalProductRequest):
    try:return app.state.campaigns.queue_supplemental_product(
        campaign_id,payload.storefront_product_id,payload.storefront_title,
        payload.storefront_url,payload.supplier,payload.supplier_url,payload.reason)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/alternate-suppliers/complete', dependencies=[Depends(authenticated)])
def complete_campaign_alternate_supplier(campaign_id:str,payload:CampaignAlternateSupplierCompletionRequest):
    try:return app.state.campaigns.complete_alternate_supplier(campaign_id,payload.job_id,payload.review_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/completion-groups', dependencies=[Depends(authenticated)])
def configure_campaign_completion_groups(campaign_id:str,payload:CampaignCompletionGroupsRequest):
    try:return app.state.campaigns.configure_completion_groups(campaign_id,payload.groups)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/complete-review-batch', dependencies=[Depends(authenticated)])
def attach_campaign_complete_review_batch(campaign_id:str,payload:CampaignCompletionBatchRequest):
    try:return app.state.campaigns.attach_complete_reviews_batch(
        campaign_id,[entry.model_dump() for entry in payload.reviews])
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/complete-b2sign-runtime-review-batch',
          dependencies=[Depends(authenticated)])
def attach_campaign_b2sign_runtime_review_batch(campaign_id:str,payload:CampaignCompletionBatchRequest):
    try:return app.state.campaigns.attach_b2sign_runtime_reviews_batch(
        campaign_id,[entry.model_dump() for entry in payload.reviews])
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/complete-gator-runtime-review',
          dependencies=[Depends(authenticated)])
def attach_campaign_gator_runtime_review(campaign_id:str,payload:CampaignCompletionRequest):
    try:return app.state.campaigns.attach_gator_runtime_review(
        campaign_id,payload.product_id,payload.review_id)
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/complete-dps-runtime-review-batch',
          dependencies=[Depends(authenticated)])
def attach_campaign_dps_runtime_review_batch(campaign_id:str,payload:CampaignCompletionBatchRequest):
    try:return app.state.campaigns.attach_dps_runtime_reviews_batch(
        campaign_id,[entry.model_dump() for entry in payload.reviews])
    except FileNotFoundError:raise HTTPException(404,'Campaign not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class SourceCollectionRequest(BaseModel):
    model_config = {"extra":"forbid"}
    urls:list[str] = Field(min_length=1,max_length=500)


def source_collector():
    if app.state.source_collections is None:
        from .campaign_sources import SourceCollector
        app.state.source_collections=SourceCollector(store)
    return app.state.source_collections


@app.post('/api/v1/source-collections', dependencies=[Depends(authenticated)])
async def start_source_collection(payload:SourceCollectionRequest):
    try:return await source_collector().start(payload.urls)
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc


@app.get('/api/v1/source-collections', dependencies=[Depends(authenticated)])
def list_source_collections():
    return {'items':source_collector().list()}


@app.get('/api/v1/source-collections/{job_id}', dependencies=[Depends(authenticated)])
def get_source_collection(job_id:str,include_data:bool=False):
    try:return source_collector().get(job_id,include_data=include_data)
    except (FileNotFoundError,KeyError):raise HTTPException(404,'Source collection not found')
    except ValueError as exc:raise HTTPException(400,str(exc)) from exc


@app.post('/api/v1/source-collections/{job_id}/resume', dependencies=[Depends(authenticated)])
async def resume_source_collection(job_id:str):
    try:return await source_collector().resume(job_id)
    except (FileNotFoundError,KeyError):raise HTTPException(404,'Source collection not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


@app.post('/api/v1/source-collections/{job_id}/pause', dependencies=[Depends(authenticated)])
async def pause_source_collection(job_id:str):
    try:return await source_collector().pause(job_id)
    except (FileNotFoundError,KeyError):raise HTTPException(404,'Source collection not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class B2HarvestRequest(BaseModel):
    model_config = {"extra":"forbid", "allow_inf_nan":False}
    source_collection_id:str = Field(min_length=32,max_length=32)
    source_url:str = Field(min_length=1,max_length=4096)
    quantities:list[StrictInt] = Field(default_factory=lambda:[1],min_length=1,max_length=100)
    baseline_only:bool = False
    canary_only:bool = False
    fixed_options:dict[StrictStr, StrictStr | StrictInt | StrictFloat] = Field(default_factory=dict,max_length=100)
    explicit_sizes:dict[StrictStr, list[StrictStr]] = Field(default_factory=dict,max_length=10)
    diagnosis_id:str | None = None
    attempt_budget:StrictInt | None = None


class B2ExactQuoteRequest(BaseModel):
    model_config = {"extra": "forbid", "allow_inf_nan": False}
    values:dict[StrictStr, StrictStr | StrictInt | StrictFloat] = Field(min_length=1,max_length=30)
    quantity:StrictInt = Field(ge=1,le=9999)
    turnaround:StrictStr = Field(min_length=1,max_length=100)
    fulfillment_mode:Literal['dropship','storepickup'] | None = None
    require_fresh:bool = False


def b2sign_harvest_manager():
    if app.state.b2sign_harvests is None:
        from .b2sign_harvest import B2SignHarvestManager
        app.state.b2sign_harvests=B2SignHarvestManager(
            store,app.state.engine.browser_pool,quote_gate=app.state.b2sign_quote_gate)
    return app.state.b2sign_harvests


def b2sign_harvest_plan(payload:B2HarvestRequest):
    from .b2sign_catalog import parse_public_product,enumerate_catalog,PINNED_BUNDLE_SHA256
    from .b2sign_harvest import prepare_candidate,source_without_production_field,quote_only_candidates
    source=source_collector().get(payload.source_collection_id,include_data=True)
    pages=[p for p in source['pages'] if p['url']==payload.source_url and p['status']=='completed']
    if len(pages)!=1 or not pages[0].get('public_data',{}).get('item'):
        raise ValueError('A completed source collection with an exact product page is required')
    catalog=parse_public_product(pages[0]['public_data']['item'],source_url=payload.source_url,source_bundle_sha256=PINNED_BUNDLE_SHA256)
    source_keys={field['key'] for field in catalog['contract']['attributes']
                 if field.get('component') not in {'divider','price_break'}}
    if set(payload.fixed_options)-source_keys:
        raise ValueError('fixed_options contains an unknown source attribute')
    iterator=enumerate_catalog(catalog,quantities=payload.quantities,
                               explicit_sizes=payload.explicit_sizes,max_candidates=100000)
    candidates=list(iterator)
    summary=iterator.summary()
    if not candidates and source_without_production_field(catalog):
        candidates=quote_only_candidates(catalog,payload.quantities)
        summary={**summary,'quote_only_count':len(candidates),'production_status':'not_exposed_no_lead_time_claim'}
    unfiltered_count=len(candidates)
    # Restrict the existing source-derived candidates; never rewrite defaults,
    # dependencies, production availability or the original source contract.
    for key,value in payload.fixed_options.items():
        if not any(type(row['values'].get(key)) is type(value) and row['values'].get(key)==value
                   for row in candidates):
            raise ValueError('fixed_options contains a value unavailable in the source-supported scope')
    candidates=[row for row in candidates if all(type(row['values'].get(key)) is type(value)
                and row['values'].get(key)==value for key,value in payload.fixed_options.items())]
    if payload.fixed_options and not candidates:
        raise ValueError('fixed_options has no matching source-supported candidates')
    summary={**summary,'fixed_options':dict(payload.fixed_options),'explicit_sizes':dict(payload.explicit_sizes),
             'unfiltered_candidate_count':unfiltered_count,
             'fixed_scope_candidate_count':len(candidates)}
    if payload.baseline_only and payload.canary_only:
        raise ValueError('baseline_only and canary_only cannot be combined')
    if payload.canary_only:
        keys=[dimension['key'] for dimension in catalog['dimensions']
              if dimension.get('domain_kind')=='finite_options' and dimension.get('visible')]
        selected=[]
        for quantity in payload.quantities:
            scoped=[row for row in candidates if row['quantity']==quantity]
            if not scoped:continue
            baseline=scoped[0]
            selected.append(baseline)
            changes=set()
            for row in scoped[1:]:
                changed=[key for key in keys if row['values'].get(key)!=baseline['values'].get(key)]
                if len(changed)!=1:continue
                signature=(changed[0],str(row['values'].get(changed[0])))
                if signature not in changes:
                    changes.add(signature)
                    selected.append(row)
        candidates=selected
        summary['canary_mode']='baseline_plus_one_visible_option_change'
        summary['canary_request_count']=len(selected)
    if payload.baseline_only:candidates=candidates[:1]
    # These tent pages render low-quantity subtotals but do not emit the native
    # quote request from which this session captures its XSRF/protocol headers.
    # A supported 11-piece state in the same option context seeds the session;
    # the requested 1-10 piece state is then quoted through the native route.
    # Account for the extra request in preflight instead of silently retrying.
    if (str(catalog.get('source_product_id')) in {'18','35','238'}
            and candidates and all(quantity<=10 for quantity in payload.quantities)):
        bootstrap=next((row for row in enumerate_catalog(
            catalog,quantities=[11],explicit_sizes=payload.explicit_sizes,max_candidates=100000)
            if row['values']==candidates[0]['values']),None)
        if bootstrap is None:
            raise ValueError('No source-supported 11-piece bootstrap matches the requested tent options')
        candidates.insert(0,bootstrap)
        summary['bootstrap_quantity']=11
        summary['bootstrap_request_count']=1
    unique, seen = [], set()
    for candidate in candidates:
        prepared = prepare_candidate(catalog,candidate)
        if prepared['candidate_id'] in seen:
            continue
        seen.add(prepared['candidate_id'])
        unique.append(candidate)
    summary['deduplicated_request_count'] = len(unique)
    summary['duplicate_source_states'] = len(candidates)-len(unique)
    candidates = unique
    if not candidates:raise ValueError('No source-supported candidates are available for the requested scope')
    return catalog,candidates,summary


@app.post('/api/v1/b2sign-harvests/preflight', dependencies=[Depends(authenticated)])
def preflight_b2sign_harvest(payload:B2HarvestRequest):
    try:
        catalog,candidates,summary=b2sign_harvest_plan(payload)
        return {'title':catalog['title'],'url':catalog['url'],'requested_count':len(candidates),
                'quantities':payload.quantities,'enumeration':summary,'catalog_complete':False,
                'quote_rate_limit':2,'minimum_pricing_seconds':len(candidates)/2,
                'verification_status':'pending','source_collection_id':payload.source_collection_id,
                'fixed_options':dict(payload.fixed_options),'explicit_sizes':dict(payload.explicit_sizes)}
    except (ValueError,KeyError) as exc:raise HTTPException(422,str(exc)) from exc


@app.post('/api/v1/campaigns/{campaign_id}/b2sign-exact-quote/{product_id}',
          dependencies=[Depends(authenticated)])
async def b2sign_exact_quote(campaign_id:str,product_id:str,payload:B2ExactQuoteRequest):
    """Resolve one complete source-rule selection; never infer a missing price."""
    from .b2sign_catalog import parse_public_product, PINNED_BUNDLE_SHA256
    from .b2sign_exact_quotes import B2SignExactQuoteManager
    try:
        campaign=active_campaign_product(campaign_id,product_id)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        later=app.state.campaigns.get(campaign_id)
        finalized=next((row for row in later['products'] if row['id']==product_id),None)
        if (not finalized or finalized.get('stage') != 'finalized'
                or not finalized.get('finalization_id')
                or finalized.get('finalization_evidence', {}).get('pricing_model') != 'source_live_quote'):
            raise exc
        campaign=later
    product=next((row for row in campaign['products'] if row['id']==product_id),None)
    live_finalized = bool(product and product.get('stage') == 'finalized'
                          and product.get('finalization_id')
                          and product.get('finalization_evidence', {}).get('pricing_model') == 'source_live_quote')
    if (not product or product.get('supplier')!='b2sign'
            or product.get('stage')=='finalized' and not live_finalized
            or not product.get('supplier_url') or not product.get('source_collection_id')):
        raise HTTPException(409,'Active or live-quote-finalized B2Sign product with pinned source required')
    if live_finalized and payload.fulfillment_mode != 'dropship':
        raise HTTPException(422,'finalized_live_quote_requires_explicit_dropship')
    # PrintItPrints fulfills these products by shipment. A supplier pickup-only
    # configuration is source-priceable, but cannot become a storefront offer.
    if payload.fulfillment_mode == 'storepickup':
        raise HTTPException(422,'supplier_pickup_only_unavailable_for_storefront')
    try:
        source=source_collector().get(product['source_collection_id'],include_data=True)
        pages=[page for page in source['pages'] if page['url']==product['supplier_url']
               and page['status']=='completed' and page.get('public_data',{}).get('item')]
        if len(pages)!=1:
            raise ValueError('Exact supplier product page unavailable')
        if pages[0]['public_data']['item'].get('saleable') is False:
            raise ValueError('source_product_not_saleable')
        catalog=parse_public_product(pages[0]['public_data']['item'],
            source_url=product['supplier_url'],source_bundle_sha256=PINNED_BUNDLE_SHA256)
        fixed_options=required_fixed_options(product)
        if any(payload.values.get(key)!=value for key,value in fixed_options.items()):
            raise ValueError('selection_violates_storefront_fixed_supplier_choice')
        # The finite planner intentionally omits nested custom editors. For an
        # exact customer selection, validate the complete submitted geometry
        # against the pinned source contract instead of enumerating a subset.
        candidate={'source_url':product['supplier_url'],
            'source_product_id':catalog['source_product_id'],
            'contract_sha256':catalog['contract_sha256'],
            'values':payload.values,'quantity':payload.quantity,
            'turnaround':payload.turnaround}
        if payload.fulfillment_mode is not None:
            candidate['runtime_context']={'_shipping':payload.fulfillment_mode}
        from .b2sign_harvest import prepare_candidate
        prepare_candidate(catalog,candidate)
        if app.state.b2sign_exact_quotes is None:
            app.state.b2sign_exact_quotes=B2SignExactQuoteManager(store,b2sign_harvest_manager())
        return await app.state.b2sign_exact_quotes.quote(catalog,candidate,
            require_fresh=payload.require_fresh or live_finalized)
    except (ValueError,KeyError) as exc:
        raise HTTPException(422,str(exc)) from exc


@app.post('/api/v1/b2sign-harvests', dependencies=[Depends(authenticated)])
async def start_b2sign_harvest(payload:B2HarvestRequest):
    try:
        catalog,candidates,_=b2sign_harvest_plan(payload)
        diagnosis=app.state.product_diagnoses.get(payload.diagnosis_id) if payload.diagnosis_id else None
        return await b2sign_harvest_manager().start(catalog,candidates,attempt_budget=payload.attempt_budget,diagnosis=diagnosis)
    except (ValueError,KeyError) as exc:raise HTTPException(422,str(exc)) from exc


@app.get('/api/v1/b2sign-harvests', dependencies=[Depends(authenticated)])
def list_b2sign_harvests():
    return {'items':b2sign_harvest_manager().list()}


@app.get('/api/v1/b2sign-harvests/{harvest_id}', dependencies=[Depends(authenticated)])
def get_b2sign_harvest(harvest_id:str):
    try:return b2sign_harvest_manager().get(harvest_id)
    except KeyError:raise HTTPException(404,'Harvest not found')


@app.post('/api/v1/b2sign-harvests/{harvest_id}/resume', dependencies=[Depends(authenticated)])
async def resume_b2sign_harvest(harvest_id:str):
    try:return await b2sign_harvest_manager().resume(harvest_id)
    except KeyError:raise HTTPException(404,'Harvest not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class B2CanaryRequest(BaseModel):
    model_config = {"extra":"forbid"}
    harvest_id:str = Field(min_length=32,max_length=32)
    quantities:list[StrictInt] = Field(default_factory=lambda:[1,10,100,101,201],min_length=1,max_length=50)
    diagnosis_id:str | None = None
    max_cases:StrictInt | None = Field(default=None,ge=1,le=1000)
    harvest_scope_only:bool = False


def b2sign_canary_manager():
    if app.state.b2sign_canaries is None:
        from .b2sign_canary import B2SignCanaryManager
        app.state.b2sign_canaries=B2SignCanaryManager(
            store,app.state.engine.browser_pool,quote_gate=app.state.b2sign_quote_gate)
    return app.state.b2sign_canaries


@app.post('/api/v1/b2sign-canaries', dependencies=[Depends(authenticated)])
async def start_b2sign_canary(payload:B2CanaryRequest):
    from .b2sign_canary import prepare_canaries
    try:
        harvest=b2sign_harvest_manager().get(payload.harvest_id)
        manifest=json.loads((Path(store.root)/'b2sign-harvests'/harvest['id']/'manifest.json').read_text())
        open_size_keys={field['key'] for field in manifest['catalog']['dimensions']
                        if field.get('domain_kind')=='open_or_dynamic_size' and field.get('component')=='size'}
        explicit_sizes={key:list(dict.fromkeys(candidate['values'][key] for candidate in manifest['candidates']
                              if key in candidate.get('values',{}))) for key in open_size_keys}
        plan=prepare_canaries(manifest['catalog'],harvest['results'],quantities=payload.quantities,
                              explicit_sizes=explicit_sizes,
                              scope_candidates=manifest['candidates'] if payload.harvest_scope_only else None,
                              max_canaries=payload.max_cases)
        diagnosis=app.state.product_diagnoses.get(payload.diagnosis_id) if payload.diagnosis_id else None
        return await b2sign_canary_manager().start(manifest['catalog'],plan,diagnosis=diagnosis)
    except (ValueError,KeyError,LookupError) as exc:raise HTTPException(422,str(exc)) from exc


@app.get('/api/v1/b2sign-canaries', dependencies=[Depends(authenticated)])
def list_b2sign_canaries():
    return {'items':b2sign_canary_manager().list()}


@app.get('/api/v1/b2sign-canaries/{canary_id}', dependencies=[Depends(authenticated)])
def get_b2sign_canary(canary_id:str):
    try:return b2sign_canary_manager().get(canary_id)
    except KeyError:raise HTTPException(404,'Canary run not found')


@app.post('/api/v1/b2sign-canaries/{canary_id}/resume', dependencies=[Depends(authenticated)])
async def resume_b2sign_canary(canary_id:str):
    try:return await b2sign_canary_manager().resume(canary_id)
    except KeyError:raise HTTPException(404,'Canary run not found')
    except ValueError as exc:raise HTTPException(409,str(exc)) from exc


class DiagnosisRequest(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(min_length=1, max_length=4096)
    focus: StrictBool = False
    observe_printograph_prices: StrictBool = False


class DiagnosisSelectionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    revision: StrictStr | StrictInt
    fields: list[dict] = Field(max_length=300)


class DiagnosisObservedChoiceRequest(BaseModel):
    model_config = {"extra": "forbid"}
    revision: StrictInt
    field_key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=128)


class DiagnosisScopedPriceRequest(BaseModel):
    model_config = {"extra": "forbid"}
    revision: StrictInt



class DiagnosisSourceReferencesRequest(BaseModel):
    model_config = {"extra": "forbid"}
    revision: StrictInt = Field(ge=0)
    source_url: StrictStr = Field(min_length=1, max_length=768)


def diagnosis_error(exc):
    if isinstance(exc, LookupError):
        return HTTPException(404, "Product diagnosis or supplier not found")
    if "revision" in str(exc) or "stale" in str(exc):
        return HTTPException(409, "The page options changed. Review the diagnosis and save your choices again.")
    if isinstance(exc, ExtractionError):
        return HTTPException(422, {"code": exc.code, "message": "Use an accessible public HTTPS product page."})
    return HTTPException(422, str(exc))


@app.get("/api/v1/supplier-sessions", dependencies=[Depends(authenticated)])
def supplier_sessions():
    return {"items": app.state.product_diagnoses.suppliers()}


class SupplierRegistrationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=120)
    website_url: str = Field(min_length=1, max_length=4096)
    login_url: str | None = Field(default=None, min_length=1, max_length=4096)


@app.post("/api/v1/supplier-sessions", dependencies=[Depends(authenticated)])
async def add_supplier_session(payload: SupplierRegistrationRequest):
    try:
        return await app.state.product_diagnoses.add_supplier(payload.name, payload.website_url, payload.login_url)
    except ExtractionError as exc:
        raise HTTPException(422, "The supplier website must resolve to an accessible public address") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/v1/supplier-sessions/{supplier_id}", dependencies=[Depends(authenticated)])
async def delete_supplier_session(supplier_id: str):
    try:
        await app.state.product_diagnoses.remove_supplier(supplier_id)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc
    return {"deleted": supplier_id}


class SupplierSessionOpenRequest(BaseModel):
    model_config = {'extra':'forbid'}
    target_url: Literal[DPS_ACCOUNT_URL] | None = None

    @model_validator(mode='before')
    @classmethod
    def explicit_target_is_nonnull(cls, value):
        if isinstance(value, dict) and 'target_url' in value and value['target_url'] is None:
            raise ValueError('Explicit target_url must be the known DPS account URL')
        return value


class SupplierWorkflowReferencesRequest(BaseModel):
    model_config = {'extra':'forbid'}
    expected_url: Literal[DPS_ACCOUNT_URL]


@app.post("/api/v1/supplier-sessions/{supplier_id}/open", dependencies=[Depends(authenticated)])
async def open_supplier_session(supplier_id: str, payload: SupplierSessionOpenRequest | None = None):
    try:
        if payload is None or payload.target_url is None:
            return await app.state.product_diagnoses.open_supplier(supplier_id)
        return await app.state.product_diagnoses.open_supplier(supplier_id, target_url=payload.target_url)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/supplier-sessions/{supplier_id}/check", dependencies=[Depends(authenticated)])
async def check_supplier_session(supplier_id: str):
    try:
        return await app.state.product_diagnoses.check_supplier(supplier_id)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.post('/api/v1/supplier-sessions/{supplier_id}/workflow-references', dependencies=[Depends(authenticated)])
async def supplier_workflow_references(supplier_id: str, payload: SupplierWorkflowReferencesRequest):
    try:
        return await app.state.product_diagnoses.read_supplier_workflow_references(supplier_id, payload.expected_url)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.get('/api/v1/supplier-sessions/fourover/account-access', dependencies=[Depends(authenticated)])
async def check_fourover_account_access():
    """Check only whether the Hermes 4over browser reaches its account page."""
    from urllib.parse import urlsplit
    async with app.state.engine.browser_pool.page('https://4over.com', headless=False) as page:
        await page.goto('https://4over.com/customer/account/', wait_until='domcontentloaded', timeout=45000)
        parts = urlsplit(page.url)
        if parts.scheme != 'https' or parts.hostname not in {'4over.com', 'www.4over.com'}:
            return {'supplier': 'fourover', 'status': 'unknown', 'account_price_access': 'not_yet_checked'}
        path = parts.path.rstrip('/')
        if path == '/customer/account' or path == '/customer/account/index':
            return {'supplier': 'fourover', 'status': 'signed_in', 'account_price_access': 'not_yet_checked'}
        if path.startswith('/customer/account/login'):
            return {'supplier': 'fourover', 'status': 'login_required', 'account_price_access': 'unavailable'}
        return {'supplier': 'fourover', 'status': 'unknown', 'account_price_access': 'not_yet_checked'}


@app.get("/api/v1/product-diagnoses", dependencies=[Depends(authenticated)])
def product_diagnoses():
    return {"items": app.state.product_diagnoses.list()}


@app.post("/api/v1/product-diagnoses", dependencies=[Depends(authenticated)])
async def start_product_diagnosis(payload: DiagnosisRequest):
    try:
        if payload.observe_printograph_prices:
            from .price_contract import SPECIAL_SHAPES_SOURCE_URL
            if payload.url != SPECIAL_SHAPES_SOURCE_URL:
                raise HTTPException(422, "passive_price_evidence_source_scope_required")
            return await app.state.product_diagnoses.start(
                payload.url, focus=payload.focus, observe_printograph_prices=True)
        # Preserve legacy/default manager signatures: no new keyword unless explicitly opted in.
        return await app.state.product_diagnoses.start(payload.url, focus=payload.focus)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


class SupplierProfileQuoteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    profile_id: StrictStr
    configuration: dict
    focus: StrictBool = False


@app.post('/api/v1/campaigns/{campaign_id}/supplier-profile-quote/{product_id}',
          dependencies=[Depends(authenticated), Depends(active_campaign_product)])
async def execute_supplier_profile_quote(campaign_id: str, product_id: str, payload: SupplierProfileQuoteRequest):
    """Registered native supplier driver. No endpoint per supplier/Woo product."""
    from .supplier_profile_executor import association_for, normalize_configuration
    try:
        current=active_campaign_product(campaign_id,product_id)
        matched=next((p for p in current['products'] if p['id']==product_id),None)
        if not matched or matched.get('stage')=='excluded':
            raise ValueError('active_product_required')
        association={'campaign_id':campaign_id,'product_id':product_id,
                     'supplier':matched.get('supplier'),'source_url':matched.get('supplier_url')}
        association_for(payload.profile_id,association)
        normalize_configuration(payload.configuration)
        async def check_scope():
            latest=active_campaign_product(campaign_id,product_id)
            item=next((p for p in latest['products'] if p['id']==product_id),None)
            if (not item or item.get('stage')=='excluded' or item.get('supplier')!=association['supplier']
                    or item.get('supplier_url')!=association['source_url']):
                raise ValueError('active_product_mapping_changed')
        return await app.state.product_diagnoses.execute_supplier_profile_quote(
            payload.profile_id,association,payload.configuration,check_scope=check_scope,focus=payload.focus)
    except (LookupError,ValueError,ExtractionError) as exc:
        if str(exc)=='needs_profile':
            return {'status':'needs_profile','reason':'needs_profile','source_data_finalized':False}
        raise diagnosis_error(exc) from exc


class StaggeredPassiveDiagnosisRequest(BaseModel):
    model_config = {"extra": "forbid"}
    focus: StrictBool = False


@app.post('/api/v1/campaigns/{campaign_id}/printograph-passive-diagnosis/{product_id}',
          dependencies=[Depends(authenticated), Depends(active_campaign_product)])
async def start_staggered_passive_diagnosis(campaign_id: str, product_id: str,
                                           payload: StaggeredPassiveDiagnosisRequest):
    """One active mapped product; no completion or supplier request replay."""
    from .price_contract import STAGGERED_CUT_SOURCE_URL
    if product_id != '17792':
        raise HTTPException(422, 'Only the reviewed Staggered Cut source is supported')

    async def check_scope():
        current=active_campaign_product(campaign_id,product_id)
        matched=next((p for p in current['products'] if p['id']==product_id),None)
        if (not matched or matched.get('supplier')!='printograph' or matched.get('stage')=='excluded'
                or matched.get('supplier_url')!=STAGGERED_CUT_SOURCE_URL):
            raise ValueError('The active Printograph mapping changed')

    try:
        await check_scope()
        return await app.state.product_diagnoses.start_staggered_passive(
            check_scope=check_scope,focus=payload.focus)
    except (LookupError,ValueError,ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.get("/api/v1/product-diagnoses/{diagnosis_id}", dependencies=[Depends(authenticated)])
def get_product_diagnosis(diagnosis_id: str):
    try:
        return app.state.product_diagnoses.get(diagnosis_id)
    except (LookupError, ValueError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/product-diagnoses/{diagnosis_id}/refresh", dependencies=[Depends(authenticated)])
async def refresh_product_diagnosis(diagnosis_id: str):
    try:
        return await app.state.product_diagnoses.refresh(diagnosis_id)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/product-diagnoses/{diagnosis_id}/observed-choice", dependencies=[Depends(authenticated)])
async def select_product_observed_choice(diagnosis_id: str, payload: DiagnosisObservedChoiceRequest):
    try:
        return await app.state.product_diagnoses.select_observed_choice(
            diagnosis_id, payload.revision, payload.field_key, payload.value)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/product-diagnoses/{diagnosis_id}/scoped-price", dependencies=[Depends(authenticated)])
async def read_product_scoped_price(diagnosis_id: str, payload: DiagnosisScopedPriceRequest):
    try:
        return await app.state.product_diagnoses.read_scoped_price(diagnosis_id, payload.revision)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/product-diagnoses/{diagnosis_id}/passive-price-evidence", dependencies=[Depends(authenticated)])
async def read_product_passive_price_evidence(diagnosis_id: str, payload: DiagnosisScopedPriceRequest):
    """Read an opted-in owned diagnosis journal; never initiate source work."""
    try:
        return await app.state.product_diagnoses.read_passive_price_evidence(diagnosis_id, payload.revision)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/product-diagnoses/{diagnosis_id}/source-references", dependencies=[Depends(authenticated)])
async def read_product_source_references(diagnosis_id: str, payload: DiagnosisSourceReferencesRequest):
    try:
        return await app.state.product_diagnoses.read_source_references(
            diagnosis_id, payload.revision, payload.source_url)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/product-diagnoses/{diagnosis_id}/observed-gallery", dependencies=[Depends(authenticated)])
async def read_product_observed_gallery(diagnosis_id: str, payload: DiagnosisScopedPriceRequest):
    try:
        return await app.state.product_diagnoses.read_observed_gallery(diagnosis_id, payload.revision)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/product-diagnoses/{diagnosis_id}/selection", dependencies=[Depends(authenticated)])
async def save_product_selection(diagnosis_id: str, payload: DiagnosisSelectionRequest):
    try:
        diagnosis = app.state.product_diagnoses.get(diagnosis_id)
        selection = normalize_selection(diagnosis, payload.revision, payload.fields)
        return await app.state.product_diagnoses.save_selection(diagnosis_id, payload.revision, selection)
    except (LookupError, ValueError) as exc:
        raise diagnosis_error(exc) from exc


class FinalizationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    revision: StrictStr | StrictInt
    run_id: str = Field(min_length=1, max_length=128)
    product_id: str = Field(min_length=1, max_length=128)
    source: str = 'quotes'


@app.get("/api/v1/product-diagnoses/{diagnosis_id}/saved-products", dependencies=[Depends(authenticated)])
def diagnosis_saved_products(diagnosis_id: str):
    try:
        return {"items": saved_products(store, app.state.product_diagnoses.get(diagnosis_id))}
    except (LookupError, ValueError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/product-diagnoses/{diagnosis_id}/finalization", dependencies=[Depends(authenticated)])
async def finalize_diagnosed_product(diagnosis_id: str, payload: FinalizationRequest):
    try:
        diagnosis = app.state.product_diagnoses.get(diagnosis_id)
        if diagnosis['status'] != 'ready' or diagnosis['revision'] != payload.revision:
            raise ValueError('diagnosis_revision_conflict')
        report = await asyncio.to_thread(review_product, store, diagnosis, payload.run_id, payload.product_id, payload.source)
        # A review is immutable. If the user refreshed during computation, keep
        # the audit files but refuse to present an old policy as the current one.
        current = app.state.product_diagnoses.get(diagnosis_id)
        if (current['revision'] != diagnosis['revision'] or current.get('selection') != diagnosis.get('selection')):
            raise ValueError('diagnosis_revision_conflict')
        return report
    except (LookupError, ValueError) as exc:
        raise diagnosis_error(exc) from exc


@app.get("/api/v1/finalizations/{review_id}/{kind}", dependencies=[Depends(authenticated)])
def download_finalization(review_id: str, kind: str):
    if kind not in {'download', 'report', 'rows', 'policy'}:
        raise HTTPException(404, 'Review file not found')
    try:
        path = review_file(store, review_id, kind)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    media = {'download': 'text/csv', 'report': 'application/json', 'rows': 'application/x-ndjson', 'policy': 'application/json'}[kind]
    return FileResponse(path, media_type=media, filename='hermes-' + path.name)


class ProbeRequest(BaseModel):
    manifest: dict
    budget: int = Field(default=398, ge=1, le=400)
    job_id: str | None = None
    resume_run: bool = False


@app.post("/api/v1/runs/{run_id}/probes", dependencies=[Depends(authenticated)])
async def create_probe(run_id: str, payload: ProbeRequest):
    try:
        job_id = app.state.probes.start(run_id, payload.manifest,
                                       job_id=payload.job_id, budget=payload.budget,
                                       resume_run=payload.resume_run)
        return app.state.probes.status(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/probes/{job_id}", dependencies=[Depends(authenticated)])
def get_probe(job_id: str):
    try:
        return app.state.probes.status(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


class DomainPreflightRequest(BaseModel):
    manifest: dict
    budget: int = Field(ge=1, le=1000000)
    measured_rps: float | None = Field(default=None, gt=0)


class DomainInstallRequest(DomainPreflightRequest):
    scope: str = Field(min_length=1, max_length=128)


class DomainSessionRequest(BaseModel):
    product_id: str = Field(min_length=1, max_length=128)
    wait_seconds: float = Field(default=20, gt=0, le=20)


class DomainStartRequest(BaseModel):
    resume_run: bool = False


class DomainRetryRequest(DomainStartRequest):
    request_keys: list[str] = Field(min_length=1, max_length=100)


@app.post("/api/v1/runs/{run_id}/source-domain-sessions", dependencies=[Depends(authenticated)])
async def initialize_domain_session(run_id: str, payload: DomainSessionRequest):
    try:
        return await app.state.domain_sessions.initialize(run_id, payload.product_id,
                                                          wait_seconds=payload.wait_seconds)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ExtractionError as exc:
        raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/source-domain-sessions/{session_scope}", dependencies=[Depends(authenticated)])
def get_domain_session(session_scope: str):
    try:
        return app.state.domain_sessions.status(session_scope)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/v1/runs/{run_id}/source-domains", dependencies=[Depends(authenticated)])
def list_domain_stages(run_id: str):
    with store.session() as s:
        if not s.get(Run, run_id):
            raise HTTPException(404, "Run not found")
        ids = list(s.scalars(select(DomainJob.id).where(DomainJob.run_id == run_id)
                            .order_by(DomainJob.updated_at.desc(), DomainJob.id.desc()).limit(20)))
    return {"items": [app.state.source_domains.status(job_id, include_responses=False,
                                                    include_timing=False) for job_id in ids]}


@app.get("/api/v1/runs/{run_id}/variation-plan", dependencies=[Depends(authenticated)])
async def variation_plan(run_id: str):
    try:
        return await app.state.variation_plans.get(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/v1/runs/{run_id}/variation-plan/export", dependencies=[Depends(authenticated)])
async def export_variation_plan(run_id: str, product_id: str):
    try:
        path, count = await app.state.variation_plans.export(run_id, product_id)
        return FileResponse(path, media_type='text/csv', filename='hermes-product-candidates.csv',
                            headers={'X-Hermes-Candidate-Rows':str(count)})
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/runs/{run_id}/source-domains/preflight", dependencies=[Depends(authenticated)])
def domain_preflight(run_id: str, payload: DomainPreflightRequest):
    try:
        return app.state.source_domains.dry_run(run_id, payload.manifest, budget=payload.budget,
                                                measured_rps=payload.measured_rps)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/v1/runs/{run_id}/source-domains", dependencies=[Depends(authenticated)])
def install_domain_stage(run_id: str, payload: DomainInstallRequest):
    try:
        return app.state.source_domains.install(run_id, payload.scope, payload.manifest,
                                                budget=payload.budget, measured_rps=payload.measured_rps)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/v1/source-domains/{job_id}", dependencies=[Depends(authenticated)])
def get_domain_stage(job_id: str, include_responses: bool = False):
    try:
        return app.state.source_domains.status(job_id, include_responses=include_responses)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/v1/source-domains/{job_id}/start", dependencies=[Depends(authenticated)])
async def start_domain_stage(job_id: str, payload: DomainStartRequest | None = None):
    try:
        return await app.state.source_domains.start(job_id, resume_run=bool(payload and payload.resume_run))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v1/source-domains/{job_id}/retry", dependencies=[Depends(authenticated)])
async def retry_domain_tasks(job_id: str, payload: DomainRetryRequest):
    try:
        return await app.state.source_domains.retry_failed(job_id, payload.request_keys, resume_run=payload.resume_run)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ExtractionError as exc:
        raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v1/source-domains/{job_id}/pause", dependencies=[Depends(authenticated)])
async def pause_domain_stage(job_id: str):
    try:
        return await app.state.source_domains.pause(job_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def export_root(run_id):
    with store.session() as s:
        if not s.get(Run, run_id):
            raise HTTPException(404, "Run not found")
    parent = (store.root / "exports").resolve()
    root = (parent / run_id).resolve()
    if root == parent or not root.is_relative_to(parent):
        raise HTTPException(404, "Invalid run")
    return root


def scope_counts(product, scope):
    if scope is not None:
        return {
            "record_scope": "service_context_v2",
            "legacy_row_count": scope.legacy_row_count,
            "scoped_row_count": max(0, product.row_count - scope.legacy_row_count),
        }
    if urlparse(product.url or "").hostname not in {
        "www.printograph.com",
        "printograph.com",
    }:
        return {
            "record_scope": "not_applicable",
            "legacy_row_count": 0,
            "scoped_row_count": product.row_count,
        }
    return {
        "record_scope": "not_assessed",
        "legacy_row_count": None,
        "scoped_row_count": None,
    }


def product_dict(p, counts=None, retry=None):
    return {
        "id": p.id,
        "url": p.url,
        "title": p.title,
        "status": p.status,
        "row_count": p.row_count,
        "request_count": p.request_count,
        "cache_hits": p.cache_hits,
        "coverage_status": p.coverage_status,
        "error": p.error,
        **(counts or {}),
        "retry": retry,
        "retry_at": (
            iso(p.lease_until)
            if p.status == "queued" and p.lease_until and p.lease_until > time.time()
            else None
        ),
    }


def run_dict(r, details=False):
    with store.session() as s:
        products = list(s.scalars(select(Product).where(Product.run_id == r.id)))
        scopes = {
            item.product_id: item
            for item in s.scalars(
                select(ProductRecordScope).where(
                    ProductRecordScope.product_id.in_([p.id for p in products])
                )
            )
        }
        counts = {p.id: scope_counts(p, scopes.get(p.id)) for p in products}
        retries = {}
        waiting = {
            p.id: p
            for p in products
            if p.status == "queued" and p.lease_until > time.time()
        }
        if details and waiting:
            for event in s.scalars(
                select(Event)
                .where(Event.run_id == r.id, Event.type == "product_retry_scheduled")
                .order_by(Event.id.desc())
            ):
                payload = event.payload or {}
                pid = payload.get("product_id")
                if (
                    pid in waiting
                    and pid not in retries
                    and payload.get("retry_at") == waiting[pid].lease_until
                ):
                    retries[pid] = payload
                if len(retries) == len(waiting):
                    break
    assessed = all(c["record_scope"] != "not_assessed" for c in counts.values())
    data = {
        "id": r.id,
        "name": r.name,
        "status": r.status,
        "created_at": iso(r.created_at),
        "updated_at": iso(r.updated_at),
        "total_products": len(products),
        "completed_products": sum(p.status == "completed" for p in products),
        "finished_attempts": sum(
            p.status
            in {
                "completed",
                "partial",
                "needs_attention",
                "access_required",
                "failed",
                "cancelled",
            }
            for p in products
        ),
        "partial_products": sum(p.status == "partial" for p in products),
        "active_products": sum(p.status in {"queued", "running"} for p in products),
        "attention_products": sum(
            p.status in {"needs_attention", "access_required", "failed"}
            for p in products
        ),
        "configuration_total": None,
        "eta_seconds": None,
        "row_count": sum(p.row_count for p in products),
        "legacy_row_count": (
            sum(c["legacy_row_count"] for c in counts.values()) if assessed else None
        ),
        "scoped_row_count": (
            sum(c["scoped_row_count"] for c in counts.values()) if assessed else None
        ),
        "record_scope": "assessed" if assessed else "not_assessed",
        "request_count": sum(p.request_count for p in products),
        "cache_hits": sum(p.cache_hits for p in products),
        "error_count": sum(bool(p.error) for p in products),
        "coverage_status": (
            "complete"
            if products and all(p.coverage_status == "complete" for p in products)
            else "partial" if any(p.row_count for p in products) else "unknown"
        ),
        "error": r.error,
        "fixture": r.spec.get("adapter") == "fixture",
        "adapter": r.spec.get("adapter", "auto"),
    }
    if details:
        data.update(
            products=[
                product_dict(p, counts[p.id], retries.get(p.id)) for p in products
            ],
            spec=r.spec,
        )
    return data


class JobSpec(BaseModel):
    name: str = Field(default="Untitled catalog", max_length=180)
    urls: list[str] = Field(default_factory=list, max_length=10000)
    titles: list[str] = Field(default_factory=list, max_length=1000)
    source_url: str | None = None
    instructions: str = ""
    adapter: str = "auto"
    image_mode: str = "urls"
    filters: dict = Field(default_factory=dict)
    shipping_contexts: list = Field(default_factory=list)
    fixture_products: int = Field(default=3, ge=1, le=1000)
    fixture_rows_per_product: int = Field(default=30000, ge=1, le=1000000)
    refresh: bool = False
    max_requests: int | None = Field(default=None, ge=1)
    provider_id: str | None = None
    max_configurations: int | None = Field(default=None, ge=1)
    browser_bulk_quantities: bool = False
    source_metadata: dict = Field(default_factory=dict)
    diagnosis_id: str | None = Field(default=None, max_length=128)
    diagnosis_revision: StrictStr | StrictInt | None = None


@app.get("/api/v1/health")
def health():
    return {
        "status": "ok",
        "database": "sqlite-local" if store.sqlite else "postgresql",
        "version": "0.1.0",
        "authentication_required": bool(settings.api_token),
        "browser_session_persistence": settings.persist_browser_sessions,
        "execution_capacity": {**settings.execution_capacity(), **browser_workload.snapshot()},
    }


@app.get("/api/v1/runs", dependencies=[Depends(authenticated)])
def runs():
    with store.session() as s:
        result = list(s.scalars(select(Run).order_by(Run.created_at.desc()).limit(200)))
    return {"items": [run_dict(r) for r in result]}


@app.post("/api/v1/runs", dependencies=[Depends(authenticated)])
async def create_run(spec: JobSpec):
    if spec.adapter not in {"auto", "printograph", "printitprints", "woocommerce", "browser", "fixture"}:
        raise HTTPException(422, "Unknown adapter")
    if spec.image_mode not in {"urls", "download", "both"}:
        raise HTTPException(422, "Unknown image mode")
    if spec.adapter == "fixture":
        urls = [
            f"fixture://catalog/product-{i+1:03}" for i in range(spec.fixture_products)
        ]
    else:
        urls = list(dict.fromkeys(u.strip() for u in spec.urls if u.strip()))
        for url in urls:
            try:
                await validate_url(url)
            except ExtractionError as exc:
                raise HTTPException(422, str(exc))
        if spec.titles:
            from .discovery import resolve_titles

            if not spec.source_url:
                raise HTTPException(422, "A source URL is required for product titles")
            if (
                urlparse(spec.source_url).hostname
                in {"printograph.com", "www.printograph.com"}
                or spec.adapter == "browser"
            ):
                from .browser_discovery import resolve_titles as browser_resolve
                from .transport import RateLimiter

                resolved = await browser_resolve(
                    spec.source_url,
                    spec.titles,
                    limiter=RateLimiter(store),
                    browser_pool=getattr(
                        getattr(app.state, "engine", None), "browser_pool", None
                    ),
                )
            else:
                resolved = await resolve_titles(spec.source_url, spec.titles)
            if resolved["unresolved"]:
                raise HTTPException(
                    409,
                    {
                        "message": "Choose a URL for ambiguous or missing titles",
                        "matches": resolved,
                    },
                )
            urls.extend(x["url"] for x in resolved["matches"])
        if not urls:
            raise HTTPException(422, "Add at least one product URL or title")
    data = spec.model_dump()
    if spec.instructions.strip():
        if not spec.provider_id:
            raise HTTPException(
                422,
                "Select an AI provider to interpret instructions, or use structured filters",
            )
        from .providers import interpret

        try:
            interpretation = await interpret(store, spec.provider_id, spec.instructions)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except ExtractionError as exc:
            raise HTTPException(502, str(exc))
        except Exception:
            raise HTTPException(
                502,
                "Provider could not interpret instructions; check the configured endpoint and credentials",
            )
        data["interpreted_instructions"] = interpretation
        if interpretation.get("needs_clarification"):
            raise HTTPException(409, interpretation)
        data["filters"] = {**interpretation.get("filters", {}), **data["filters"]}
    if spec.diagnosis_id is not None:
        from .diagnosis_run import compile_browser_policy
        try:
            diagnosis = app.state.product_diagnoses.get(spec.diagnosis_id)
            if diagnosis['revision'] != spec.diagnosis_revision or type(diagnosis['revision']) is not type(spec.diagnosis_revision):
                raise ValueError('diagnosis_revision_conflict')
            compiled = compile_browser_policy(diagnosis, urls, data['adapter'], data['filters'])
            data.update(compiled)
            # The worker uses this immutable snapshot, never a subsequently
            # edited diagnosis. The campaign UI does not start this job itself.
        except (LookupError, ValueError) as exc:
            raise diagnosis_error(exc) from exc
    elif spec.diagnosis_revision is not None:
        raise HTTPException(422, 'A diagnosis ID is required with its revision')
    with store.session() as s:
        r = Run(id=uid(), name=spec.name, status="queued", spec=data)
        s.add(r)
        for url in dict.fromkeys(urls):
            s.add(Product(run_id=r.id, url=url))
    store.event(
        r.id,
        "run_created",
        {
            "products": len(urls),
            "adapter": spec.adapter,
            "fixture": spec.adapter == "fixture",
        },
    )
    return run_dict(r, True)


class BatchProductRequest(BaseModel):
    model_config = {"extra": "forbid"}
    diagnosis_id: str = Field(min_length=1, max_length=128)
    revision: StrictStr | StrictInt
    fields: list[dict] = Field(max_length=300)


class PricingPilotRequest(BaseModel):
    model_config = {"extra": "forbid"}
    revision: StrictStr | StrictInt
    quantities: list[StrictInt] = Field(default_factory=lambda: [1, 10, 100, 101], min_length=1, max_length=4)


def pricing_pilot_manager():
    if app.state.pricing_pilots is None:
        from .b2sign_pilot import B2SignPilotManager
        app.state.pricing_pilots = B2SignPilotManager(store, app.state.engine.browser_pool)
    return app.state.pricing_pilots


@app.post("/api/v1/product-diagnoses/{diagnosis_id}/pricing-pilot", dependencies=[Depends(authenticated)])
async def start_pricing_pilot(diagnosis_id: str, payload: PricingPilotRequest):
    try:
        diagnosis = app.state.product_diagnoses.get(diagnosis_id)
        if diagnosis['revision'] != payload.revision or type(diagnosis['revision']) is not type(payload.revision):
            raise ValueError('diagnosis_revision_conflict')
        return await pricing_pilot_manager().start(diagnosis, quantities=payload.quantities)
    except (LookupError, ValueError, ExtractionError) as exc:
        raise diagnosis_error(exc) from exc


@app.get("/api/v1/pricing-pilots", dependencies=[Depends(authenticated)])
def pricing_pilots():
    return {'items': pricing_pilot_manager().list()}


@app.get("/api/v1/pricing-pilots/{pilot_id}", dependencies=[Depends(authenticated)])
def pricing_pilot(pilot_id: str):
    try:
        return pricing_pilot_manager().get(pilot_id)
    except (LookupError, ValueError) as exc:
        raise diagnosis_error(exc) from exc


class DPSPilotRequest(BaseModel):
    model_config = {"extra": "forbid"}
    url: StrictStr = Field(min_length=1, max_length=2048)
    quantities: list[StrictInt] | None = Field(default=None, min_length=1, max_length=4)


def dps_pilot_manager():
    if app.state.dps_pilots is None:
        from .dps_pilot import DPSPilotManager
        app.state.dps_pilots = DPSPilotManager(store, app.state.engine.browser_pool)
    return app.state.dps_pilots


@app.post("/api/v1/dps-pilots", dependencies=[Depends(authenticated)])
async def start_dps_pilot(payload: DPSPilotRequest):
    try:
        return await dps_pilot_manager().start(payload.url, quantities=payload.quantities)
    except (ValueError, ExtractionError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/dps-pilots", dependencies=[Depends(authenticated)])
def dps_pilots():
    return {"items": dps_pilot_manager().list()}


@app.get("/api/v1/dps-pilots/{pilot_id}", dependencies=[Depends(authenticated)])
def dps_pilot(pilot_id: str):
    try:
        return dps_pilot_manager().get(pilot_id)
    except (FileNotFoundError, KeyError):
        raise HTTPException(404, "DPS pilot not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class DiagnosisBatchRequest(BaseModel):
    model_config = {"extra": "forbid"}
    products: list[BatchProductRequest] = Field(min_length=1, max_length=100)


class DiagnosisBatchLaunchRequest(DiagnosisBatchRequest):
    plan_token: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=32, max_length=36)


@app.post("/api/v1/product-diagnoses/preflight", dependencies=[Depends(authenticated)])
def diagnosis_batch_preflight(payload: DiagnosisBatchRequest):
    from .diagnosis_batch import prepare_batch
    try:
        plan = prepare_batch(app.state.product_diagnoses, [p.model_dump() for p in payload.products])
        return {k: v for k, v in plan.items() if not k.startswith('_')}
    except (LookupError, ValueError) as exc:
        raise diagnosis_error(exc) from exc


@app.post("/api/v1/product-diagnoses/launch", dependencies=[Depends(authenticated)])
async def diagnosis_batch_launch(payload: DiagnosisBatchLaunchRequest):
    from .diagnosis_batch import prepare_batch, queue_batch, existing_batch, request_fingerprint
    from sqlalchemy.exc import IntegrityError
    try:
        products = [p.model_dump() for p in payload.products]
        prior = existing_batch(store, products, payload.plan_token, payload.idempotency_key)
        if prior is not None:
            with store.session() as session:
                return {'batch_id': payload.idempotency_key,
                        'runs': [run_dict(session.get(Run, run_id), True) for run_id in prior]}
        plan = prepare_batch(app.state.product_diagnoses, products)
        if not plan['ready']:
            raise HTTPException(409, {'message': 'Resolve the product blockers before running this batch.',
                                     'blockers': [p for p in plan['products'] if p['status'] == 'blocked']})
        for spec in plan['_specs']:
            await validate_url(spec['urls'][0])
        # DNS validation yields; read revisions and policies again immediately
        # before the synchronous all-or-nothing database transaction.
        plan = prepare_batch(app.state.product_diagnoses, products)
        try:
            ids = queue_batch(store, plan, payload.plan_token, payload.idempotency_key,
                              request_identity=request_fingerprint(products))
        except IntegrityError:
            # Another API worker may have committed the identical launch after
            # our initial lookup. Recover only its exact persisted request.
            ids = existing_batch(store, products, payload.plan_token, payload.idempotency_key)
            if ids is None:
                raise HTTPException(409, 'The batch could not be queued; retry the same launch identifier.') from None
        with store.session() as session:
            runs = [run_dict(session.get(Run, run_id), True) for run_id in ids]
        return {'batch_id': payload.idempotency_key, 'runs': runs}
    except (LookupError, ValueError, ExtractionError) as exc:
        raise HTTPException(409 if isinstance(exc, ValueError) else 422, str(exc)) from exc


@app.get("/api/v1/runs/{run_id}", dependencies=[Depends(authenticated)])
def get_run(run_id: str):
    with store.session() as s:
        r = s.get(Run, run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    return run_dict(r, True)


@app.delete("/api/v1/runs/{run_id}", dependencies=[Depends(authenticated)])
def delete_run(run_id: str):
    try:
        store.delete_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"deleted": run_id}


@app.post("/api/v1/runs/{run_id}/{action}", dependencies=[Depends(authenticated)])
def control(run_id: str, action: str):
    if action not in {"pause", "resume", "cancel"}:
        raise HTTPException(404, "Unknown action")
    with store.session(immediate=True) as s:
        r = s.get(Run, run_id)
        if not r:
            raise HTTPException(404, "Run not found")
        if action == "pause":
            if r.status not in {"completed", "completed_with_gaps", "cancelled"}:
                r.status = "paused"
                s.execute(
                    update(Product)
                    .where(Product.run_id == run_id, Product.status == "running")
                    .values(status="queued", epoch=Product.epoch + 1, lease_until=0)
                )
        elif action == "cancel":
            r.status = "cancelled"
            s.execute(
                update(Product)
                .where(
                    Product.run_id == run_id, Product.status.in_(["queued", "running"])
                )
                .values(status="cancelled", epoch=Product.epoch + 1, lease_until=0)
            )
        else:
            if r.status == "completed":
                return run_dict(r, True)
            r.status = "queued"
            r.error = None
            s.execute(
                update(Product)
                .where(
                    Product.run_id == run_id,
                    Product.status.in_(
                        ["access_required", "needs_attention", "cancelled", "partial"]
                    ),
                )
                .values(status="queued", error=None, lease_until=0)
            )
            if not s.scalar(
                select(func.count())
                .select_from(Product)
                .where(
                    Product.run_id == run_id, Product.status.in_(["queued", "running"])
                )
            ):
                # Product pauses are independent from the run-level switch.
                # With no runnable product, do not publish an unfinished catalog.
                individually_paused = s.scalar(
                    select(func.count()).select_from(Product).where(
                        Product.run_id == run_id, Product.status == "paused"
                    )
                )
                r.status = "paused" if individually_paused else "exporting"
        r.updated_at = time.time()
    store.event(run_id, "run_" + action, {})
    return get_run(run_id)


@app.post("/api/v1/runs/{run_id}/products/{product_id}/{action}", dependencies=[Depends(authenticated)])
def control_product(run_id: str, product_id: str, action: str):
    try:
        store.control_product(run_id, product_id, action)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return get_run(run_id)


@app.get("/api/v1/runs/{run_id}/efficiency", dependencies=[Depends(authenticated)])
def efficiency(run_id: str):
    with store.session() as s:
        if s.get(Run, run_id) is None:
            raise HTTPException(404, "Run not found")
        products = list(s.scalars(select(Product).where(Product.run_id == run_id)))
        windows = {w.product_id:w for w in s.scalars(select(TrafficWindow).where(TrafficWindow.run_id == run_id))}
        observed = list(s.execute(select(ResponseObservation.product_id, ResponseObservation.request_signature,
            Response.semantic_hash, Response.status).join(Response, Response.id == ResponseObservation.response_id)
            .where(ResponseObservation.run_id == run_id, ResponseObservation.kind == "source")))
        scopes = {x.product_id:x for x in s.scalars(select(ProductRecordScope).where(ProductRecordScope.product_id.in_([p.id for p in products])))}
    def summarize(selected):
        ids = {p.id for p in selected}
        samples = [o for o in observed if o[0] in ids and 200 <= o[3] < 300]
        network = sum(max(0,p.request_count-windows[p.id].requests_start) for p in selected if p.id in windows)
        new_rows = sum(max(0,p.row_count-windows[p.id].rows_start) for p in selected if p.id in windows)
        keys = {o[1] for o in samples}
        bodies = {(o[1],o[2]) for o in samples}
        assessed = all(p.id in scopes for p in selected)
        return {"network_requests": network, "source_response_observations": len(samples),
            "distinct_response_keys": len(keys), "distinct_semantic_responses": len(bodies),
            "current_rows": sum(p.row_count-scopes[p.id].legacy_row_count for p in selected) if assessed else None,
            "new_rows": new_rows, "rows_per_request": new_rows/network if network else None,
            "requests_per_unique_response": len(samples)/len(bodies) if bodies else None,
            "attribution_scope": "since_telemetry_start",
            "started_at": min((iso(windows[p.id].started_at) for p in selected if p.id in windows),default=None),
            "limits": "Rows/request uses this telemetry window. Response reuse ratio covers successful captured source responses per distinct request-and-body, not all traffic or a complete request budget."}
    return {**summarize(products), "products": [{"product_id":p.id,"title":p.title,**summarize([p])} for p in products]}


@app.get("/api/v1/runs/{run_id}/events", dependencies=[Depends(authenticated)])
def events(
    run_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = 250,
    category: str = "all",
):
    if (
        after < 0
        or (before is not None and before < 1)
        or (after and before is not None)
    ):
        raise HTTPException(422, "Use either an after or before event cursor")
    if category not in {"all", "attention"}:
        raise HTTPException(422, "Event category must be all or attention")
    limit = max(1, min(limit, 500))
    with store.session() as s:
        query = select(Event).where(Event.run_id == run_id)
        if category == "attention":
            query = query.where(
                Event.type.in_(
                    {
                        "product_error",
                        "attention",
                        "browser_request_error",
                        "branch_skipped",
                        "bulk_fallback",
                        "product_retry_scheduled",
                        "product_retry_exhausted",
                        "legacy_rows_quarantined",
                    }
                )
            )
        if after:
            query = query.where(Event.id > after).order_by(Event.id.asc())
        else:
            if before is not None:
                query = query.where(Event.id < before)
            query = query.order_by(Event.id.desc())
        items = list(s.scalars(query.limit(limit + 1)))
    has_more = len(items) > limit
    items = items[:limit]
    if not after:
        items.reverse()
    return {
        "items": [
            {
                "id": x.id,
                "created_at": iso(x.created_at),
                "type": x.type,
                "payload": x.payload,
            }
            for x in items
        ],
        "has_older": has_more if not after else None,
        "has_newer": has_more if after else False,
        "next_before": items[0].id if items else before,
        "next_after": items[-1].id if items else after,
    }


@app.get("/api/v1/runs/{run_id}/rows", dependencies=[Depends(authenticated)])
def rows(
    run_id: str,
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    scope: str = "current",
):
    from .presentation import present_row
    from .record_scope import is_legacy_unscoped
    from .commerce import price_comparison_context

    if scope not in {"current", "legacy", "all"}:
        raise HTTPException(422, "Row scope must be current, legacy, or all")
    with store.session() as s:
        products = list(s.scalars(select(Product).where(Product.run_id == run_id)))
        scopes = {
            item.product_id: item
            for item in s.scalars(
                select(ProductRecordScope).where(
                    ProductRecordScope.product_id.in_([p.id for p in products])
                )
            )
        }
    counts = [scope_counts(p, scopes.get(p.id)) for p in products]
    known_total = None
    if not search:
        if scope == "all":
            known_total = sum(p.row_count for p in products)
        elif all(c["record_scope"] != "not_assessed" for c in counts):
            known_total = sum(
                c["scoped_row_count" if scope == "current" else "legacy_row_count"]
                for c in counts
            )

    limit = max(1, min(limit, 250))
    offset = max(0, offset)
    result = []
    total = 0
    if known_total is not None and offset >= known_total:
        return {"items": [], "total": known_total, "scope": scope}
    for row in store.iter_rows(run_id=run_id):
        legacy = is_legacy_unscoped(row)
        if (scope == "current" and legacy) or (scope == "legacy" and not legacy):
            continue
        comparison = price_comparison_context(row)
        row = present_row(row)
        row["price_comparison_context"] = comparison
        row["record_scope"] = (
            "legacy_unscoped" if legacy else row.get("record_scope", "current")
        )
        if search and search.casefold() not in canonical(row).casefold():
            continue
        if offset <= total < offset + limit:
            result.append(row)
        total += 1
        if known_total is not None and total >= offset + limit:
            break
    return {
        "items": result,
        "total": known_total if known_total is not None else total,
        "scope": scope,
    }


@app.get("/api/v1/runs/{run_id}/exports", dependencies=[Depends(authenticated)])
def exports(run_id: str, scope: str = "current"):
    if scope not in {"current", "all"}:
        raise HTTPException(422, "Export scope must be current or all")
    root = export_root(run_id)
    items = []
    with store.session() as s:
        products = list(s.scalars(select(Product).where(Product.run_id == run_id)))
        run = s.get(Run, run_id)
    requires_scoped_export = run.spec.get("adapter") != "fixture" and any(
        urlparse(p.url or "").hostname in {"www.printograph.com", "printograph.com"}
        for p in products
    )
    manifests = [
        (
            root / p.id,
            p.export_manifest,
            p.title,
            p.row_count,
            p.status,
            p.coverage_status,
            run.spec.get("adapter") != "fixture"
            and urlparse(p.url or "").hostname
            in {"printograph.com", "www.printograph.com"},
        )
        for p in products
        if p.export_manifest
    ]
    catalog = root / "catalog" / "manifest.json"
    if catalog.exists():
        manifests.insert(
            0,
            (
                catalog.parent,
                json.loads(catalog.read_text()),
                "Entire catalog",
                sum(p.row_count for p in products),
                None,
                (
                    "complete"
                    if products
                    and all(p.coverage_status == "complete" for p in products)
                    else "partial"
                ),
                requires_scoped_export,
            ),
        )
    withheld = 0
    for (
        folder,
        manifest,
        title,
        current_count,
        product_status,
        coverage,
        needs_scope,
    ) in manifests:
        unassessed = (
            needs_scope and manifest.get("record_scope") != "service_context_v2"
        )
        if scope == "current" and unassessed:
            withheld += 1
            continue
        published = list(manifest.get("items", []))
        # The manifest belongs to the same immutable generation as its files.
        generation = manifest.get("generation")
        manifest_path = folder / generation / "manifest.json" if generation else None
        if manifest_path is not None and manifest_path.is_file():
            published.append({"name": "manifest.json", "path": str(manifest_path)})
        for item in published:
            target = Path(item["path"])
            if not target.is_absolute():
                target = folder / target
            if target.exists() and target.resolve().is_relative_to(root.resolve()):
                rel = str(target.relative_to(root))
                items.append(
                    {
                        "name": title + " / " + item.get("name", target.name),
                        "url": f"/api/v1/runs/{run_id}/download/{rel}",
                        "size": target.stat().st_size,
                        "snapshot_row_count": manifest.get("row_count"),
                        "current_row_count": current_count,
                        "created_at": manifest.get("created_at"),
                        "generation": generation,
                        "stale": manifest.get(
                            "source_row_count", manifest.get("row_count")
                        )
                        != current_count,
                        "record_scope": manifest.get(
                            "record_scope",
                            "not_assessed" if unassessed else "not_applicable",
                        ),
                        "legacy_row_count": manifest.get("legacy_row_count"),
                        "scoped_row_count": manifest.get("scoped_row_count"),
                        "product_status": product_status,
                        "current_coverage_status": coverage,
                        "verification_claim": manifest.get("verification_claim"),
                    }
                )
    return {"items": items, "scope": scope, "withheld_historical_snapshots": withheld}


@app.get("/api/v1/runs/{run_id}/analytics", dependencies=[Depends(authenticated)])
def analytics(run_id: str):
    from .commerce import analyze_price_breaks
    from .presentation import present_row

    sample = []
    for row in store.current_rows(run_id=run_id):
        sample.append(present_row(row))
        if len(sample) >= 50000:
            break
    return {
        "items": analyze_price_breaks(sample),
        "scope": "first 50000 rows maximum",
        "sampled": len(sample),
    }


@app.post("/api/v1/commerce/shipping", dependencies=[Depends(authenticated)])
def shipping(data: dict):
    from .commerce import quote_shipping

    try:
        return quote_shipping(data.get("tariff", {}), data.get("inputs", {}))
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/v1/commerce/production", dependencies=[Depends(authenticated)])
def production(data: dict):
    from .commerce import printograph_delivery_window

    try:
        return printograph_delivery_window(**data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))


@app.get(
    "/api/v1/runs/{run_id}/download/{path:path}", dependencies=[Depends(authenticated)]
)
def download(run_id: str, path: str):
    root = export_root(run_id)
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "Export not found")
    return FileResponse(target, filename=target.name)


@app.get("/api/v1/evidence/{response_id}", dependencies=[Depends(authenticated)])
def evidence(response_id: str):
    with store.session() as s:
        r = s.get(Response, response_id)
    if not r:
        raise HTTPException(404, "Evidence not found")
    return {
        "id": r.id,
        "url": r.url,
        "status": r.status,
        "observed_at": iso(r.created_at),
        "hash": r.raw_hash,
        "content_type": r.content_type,
        "body": store.get_object(r.object_path)[:100000].decode(errors="replace"),
    }


@app.get("/api/v1/sources", dependencies=[Depends(authenticated)])
def sources():
    return {
        "items": store.profiles("source"),
        "defaults": {
            "requests_per_second": settings.requests_per_second,
            "max_concurrency": settings.source_concurrency,
        },
    }


@app.post("/api/v1/sources", dependencies=[Depends(authenticated)])
async def save_source(data: dict):
    data = dict(data)
    credentials = data.pop("credentials", None)
    allowed = {
        "id",
        "name",
        "host",
        "domain",
        "base_url",
        "url",
        "adapter",
        "requests_per_second",
        "max_concurrency",
        "concurrency",
    }
    if set(data) - allowed:
        raise HTTPException(
            422, "Unrecognized source setting; put credentials in the credential field"
        )
    host = data.get("host")
    if not isinstance(host, str) or not host or any(c in host for c in "/@:#?"):
        raise HTTPException(422, "Use a source hostname without a path or credentials")
    data["host"] = host.lower().strip()
    try:
        rate = float(data.get("requests_per_second", settings.requests_per_second))
        concurrency = int(
            data.get(
                "max_concurrency", data.get("concurrency", settings.source_concurrency)
            )
        )
        if not 0.1 <= rate <= 100 or not 1 <= concurrency <= 64:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(
            422, "Use 0.1–100 requests per second and 1–64 concurrent requests"
        )
    data.update(requests_per_second=rate, max_concurrency=concurrency)
    if credentials is not None:
        if (
            not isinstance(credentials, dict)
            or set(credentials) - {"headers"}
            or not isinstance(credentials.get("headers", {}), dict)
        ):
            raise HTTPException(422, "Source credentials must contain a headers object")
        if any(
            not isinstance(k, str)
            or not isinstance(v, str)
            or "\n" in k + v
            or "\r" in k + v
            for k, v in credentials.get("headers", {}).items()
        ):
            raise HTTPException(
                422, "Header names and values must be single-line strings"
            )
    try:
        await validate_url("https://" + data["host"])
    except ExtractionError as exc:
        raise HTTPException(422, str(exc))
    existing = next(
        (p for p in store.profiles("source") if p.get("host") == data["host"]), None
    )
    if existing and not data.get("id"):
        data["id"] = existing["id"]
    return {"id": store.save_profile("source", data, credentials)}


@app.get("/api/v1/providers", dependencies=[Depends(authenticated)])
def providers():
    return {"items": store.profiles("provider")}


@app.post("/api/v1/providers", dependencies=[Depends(authenticated)])
async def save_provider(data: dict):
    data = dict(data)
    key = data.pop("api_key", None)
    if key is not None and (
        not isinstance(key, str) or any(ord(c) < 32 or ord(c) > 126 for c in key)
    ):
        raise HTTPException(422, "API key must be a single-line ASCII string")
    from .providers import validate_provider_profile

    try:
        data = validate_provider_profile(data)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not data.get("base_url") or not data.get("model"):
        raise HTTPException(422, "Base URL and model are required")
    try:
        await validate_url(data["base_url"])
    except ExtractionError as exc:
        raise HTTPException(422, str(exc))
    return {
        "id": store.save_profile(
            "provider", data, {"api_key": key} if key is not None else None
        )
    }


@app.post("/api/v1/providers/{provider_id}/test", dependencies=[Depends(authenticated)])
async def test_provider(provider_id: str):
    from .providers import complete

    try:
        result = await complete(store, provider_id, "Reply with the word connected.")
    except Exception:
        raise HTTPException(
            502, "Provider connection failed; check its URL, model, and credentials"
        )
    return {"status": "connected", "response": result[:200]}


@app.post("/api/v1/import-urls", dependencies=[Depends(authenticated)])
async def import_urls(file: UploadFile = File(...)):
    data = await file.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(413, "CSV exceeds 2 MB")
    reader = csv.reader(io.StringIO(data.decode("utf-8-sig")))
    return {
        "urls": list(
            dict.fromkeys(
                cell.strip()
                for row in reader
                for cell in row
                if cell.strip().startswith(("https://", "http://"))
            )
        )
    }


frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend.exists():
    app.mount("/", StaticFiles(directory=frontend, html=True), name="dashboard")
