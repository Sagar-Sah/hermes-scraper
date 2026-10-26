"""Authenticated local update controls. The browser supplies no URLs, paths or PIDs."""
import asyncio
import subprocess
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from .managed_update import UpdateError


class ApplyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_commit: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    check_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")


class CheckUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")


def update_router(get_updater, authenticated):
    router = APIRouter(prefix="/api/v1/updates", dependencies=[Depends(authenticated)])

    def local_origin(request):
        host = urlsplit(str(request.url))
        origin = request.headers.get("origin", "")
        if (request.client is None or request.client.host not in {"127.0.0.1", "::1", "localhost"}
                or host.hostname not in {"127.0.0.1", "localhost", "::1"}
                or origin != f"{host.scheme}://{host.netloc}"
                or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"):
            raise HTTPException(403, "Same-origin local update request required")

    @router.get("/status")
    async def status():
        await get_updater().reconcile_failure()
        return get_updater().status()

    @router.post("/check")
    async def check(payload: CheckUpdate, request: Request):
        local_origin(request)
        try: return await get_updater().check()
        except (UpdateError, OSError, subprocess.SubprocessError) as error:
            raise HTTPException(409, str(error) if isinstance(error, UpdateError) else "update_check_failed") from None

    @router.post("/apply", status_code=202)
    async def apply(payload: ApplyUpdate, request: Request):
        local_origin(request)
        try: return await get_updater().apply(payload.expected_commit, payload.check_id)
        except (UpdateError, OSError) as error:
            raise HTTPException(409, str(error) if isinstance(error, UpdateError) else "update_not_ready") from None

    return router


def pause_runs_for_update(store):
    from sqlalchemy import select
    from .store import Run, Product
    with store.session(immediate=True) as session:
        for run in session.scalars(select(Run).where(Run.status.in_(
                ["queued", "running", "discovering", "harvesting", "verifying", "exporting"]))):
            run.status = "paused"
            for product in session.scalars(select(Product).where(Product.run_id == run.id)):
                store.sync_product_timing(session, product, run)


async def reconcile_interrupted_update(app, store):
    """Called before automation.open/engine.start, never after admissions begin."""
    if app.state.updater.startup_pause_required():
        await app.state.campaign_automation.begin_maintenance()
        await app.state.campaign_automation.pause_drained_for_update()
        pause_runs_for_update(store)
        app.state.updater._operation(status="failed", stage="failed",
            error_code="update_interrupted", collection_paused=True)
        await app.state.campaign_automation.end_maintenance()
        app.state.updater.fenced = False


async def drain_for_update(app, store, *, timeout=600):
    """Fence new admissions, await natural finite chunks, then persist pauses.

    Unknown independent source work holds the update. No task is cancelled, no
    held job is reset and no source selection is replayed by this operation.
    """
    automation, engine = app.state.campaign_automation, app.state.engine
    await automation.begin_maintenance()
    engine_fenced = False
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while (any(not task.done() for _, task in automation._active.values())
                or getattr(app.state, "update_mutations", set())):
            if asyncio.get_running_loop().time() >= deadline: raise UpdateError("workers_busy")
            await asyncio.sleep(0.1)
        await automation.tick()  # collect completed tasks; maintenance forbids dispatch
        def busy(value):
            if isinstance(value, asyncio.Task): return not value.done()
            if isinstance(value, dict): return any(busy(item) for item in value.values())
            if isinstance(value, (list, tuple, set)): return any(busy(item) for item in value)
            if isinstance(value, asyncio.Lock): return value.locked()
            return bool(value)
        census = {"probes": ("tasks",), "source_domains": ("_active",),
            "product_diagnoses": ("_tasks", "_pages", "_leases", "_bundle_tasks", "_supplier_profile_owners"),
            "domain_sessions": ("sessions", "_initializing"), "source_collections": ("_tasks",),
            "b2sign_harvests": ("_tasks",), "b2sign_canaries": ("_tasks",),
            "pricing_pilots": ("_tasks",), "dps_pilots": ("_tasks",),
            "dps_exact_quotes": ("_locks",), "b2sign_exact_quotes": ("lock",)}
        for name, attributes in census.items():
            manager = getattr(app.state, name, None)
            if any(busy(getattr(manager, attribute, None)) for attribute in attributes):
                raise UpdateError("independent_source_work_active")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0: raise UpdateError("workers_busy")
        try:
            await asyncio.wait_for(engine.begin_maintenance(), timeout=remaining)
        except asyncio.TimeoutError:
            raise UpdateError("workers_busy") from None
        engine_fenced = True
        while any(not task.done() for task in engine.tasks):
            if asyncio.get_running_loop().time() >= deadline: raise UpdateError("workers_busy")
            await asyncio.sleep(0.1)
        await automation.pause_drained_for_update()
        # At this point the engine's admission lock is fenced and no owned task
        # is active. Preserve product epochs/cursors/attempts; pause Runs only.
        pause_runs_for_update(store)
    except BaseException:
        await automation.end_maintenance()
        if engine_fenced: await engine.end_maintenance()
        raise
