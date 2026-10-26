"""Authenticated API bridge; application injects reviewed profile handlers."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictStr, StrictInt


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_ids: list[StrictStr] | None = Field(default=None, max_length=99)
    max_jobs: StrictInt = Field(default=99, ge=1, le=99)


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def automation_router(get_manager, authenticated):
    router = APIRouter(prefix="/api/v1/campaigns/{campaign_id}/automation",
                       dependencies=[Depends(authenticated)])

    def failure():
        return HTTPException(409, "Campaign automation scope or saved plan needs review")

    @router.get("")
    def status(campaign_id: str):
        try: return get_manager().snapshot(campaign_id)
        except (ValueError, KeyError, FileNotFoundError): raise failure() from None

    @router.post("/plan")
    async def plan(campaign_id: str, payload: PlanRequest):
        try:
            return await get_manager().plan(campaign_id, product_ids=payload.product_ids, max_jobs=payload.max_jobs)
        except (ValueError, KeyError, FileNotFoundError): raise failure() from None

    @router.post("/start")
    async def start(campaign_id: str, payload: StartRequest):
        try: return await get_manager().start(campaign_id, payload.plan_id)
        except (ValueError, KeyError, FileNotFoundError): raise failure() from None

    @router.post("/pause")
    async def pause(campaign_id: str, payload: EmptyRequest):
        try: return await get_manager().control(campaign_id, "pause")
        except (ValueError, KeyError, FileNotFoundError): raise failure() from None

    @router.post("/resume")
    async def resume(campaign_id: str, payload: EmptyRequest):
        try: return await get_manager().control(campaign_id, "resume")
        except (ValueError, KeyError, FileNotFoundError): raise failure() from None

    return router
