"""Authenticated monthly Companion control; no production approval route."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class ConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    schedule_enabled: StrictBool | None = None
    site_url: StrictStr | None = None
    staging_url: StrictStr | None = None
    timezone: StrictStr | None = None
    worker_capacity: StrictInt | None = None
    batch_size: StrictInt | None = None
    handler_timeout_seconds: StrictInt | None = None
    director_enabled: StrictBool | None = None
    staging_repairs_enabled: StrictBool | None = None
    director_provider_id: StrictStr | None = None
    director_budget_usd: StrictStr | None = None
    director_max_input_tokens: StrictInt | None = None
    director_max_output_tokens: StrictInt | None = None
    director_max_calls: StrictInt | None = None


class CycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: StrictStr = "manual"
    baseline: dict | None = None
    category_slugs: list[StrictStr] | None = Field(default=None, min_length=1, max_length=10)
    product_ids: list[StrictStr] | None = Field(default=None, min_length=1, max_length=99)


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier: StrictStr | None = None


class SupersedeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    expected_inventory_sha256: StrictStr | None = Field(pattern=r"^[a-f0-9]{64}$")
    reason: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,99}$")


def autonomous_router(getservice, authenticated):
    router = APIRouter(prefix="/api/v1/companion/autonomy", dependencies=[Depends(authenticated)])

    def failure(exc):
        return HTTPException(404 if isinstance(exc, KeyError) else 409, str(exc).strip("'"))

    @router.get("/status")
    def status():
        return getservice().status()

    @router.get("/config")
    def config():
        return dict(getservice().config)

    @router.put("/config")
    def configure(payload: ConfigureRequest):
        try:
            return getservice().configure(**payload.model_dump(exclude_unset=True))
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.get("/cycles")
    def cycles():
        return {"items": getservice().list_cycles()}

    @router.post("/cycles")
    def create_cycle(payload: CycleRequest):
        try:
            return getservice().create_cycle(**payload.model_dump())
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.get("/cycles/{cycle_id}")
    def cycle(cycle_id: str):
        try:
            return getservice().get_cycle(cycle_id)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.post("/cycles/{cycle_id}/inventory")
    async def inventory(cycle_id: str, payload: EmptyRequest):
        try:
            return await getservice().inventory(cycle_id)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.post("/cycles/{cycle_id}/start")
    async def start(cycle_id: str, payload: EmptyRequest):
        try:
            return await getservice().start(cycle_id)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.post("/cycles/{cycle_id}/pause")
    async def pause(cycle_id: str, payload: EmptyRequest):
        try:
            return await getservice().pause(cycle_id)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.post("/cycles/{cycle_id}/supersede")
    async def supersede(cycle_id: str, payload: SupersedeRequest):
        try:
            return await getservice().supersede(cycle_id, **payload.model_dump())
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.post("/cycles/{cycle_id}/resume")
    async def resume(cycle_id: str, payload: ResumeRequest):
        try:
            return await getservice().resume(cycle_id, **payload.model_dump())
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    @router.post("/cycles/{cycle_id}/tick")
    async def tick(cycle_id: str, payload: EmptyRequest):
        try:
            getservice().get_cycle(cycle_id)
            await getservice().tick(cycle_id=cycle_id)
            return getservice().get_cycle(cycle_id)
        except (ValueError, KeyError) as exc:
            raise failure(exc) from None

    return router
