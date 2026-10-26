"""Authenticated collection delivery routes, independent of supplier workers."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr


class CollectionExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_ids: list[StrictStr] = Field(min_length=1, max_length=1000)
    allow_incomplete_product_ids: list[StrictStr] = Field(default_factory=list, max_length=1000)


class CollectionFinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archive_product_ids: list[StrictStr] = Field(max_length=1000)
    saved: StrictBool


class CollectionDiscardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discard: StrictBool


def collection_router(get_store: Callable, authenticated: Callable, cleanup_guard: Callable = nullcontext) -> APIRouter:
    # Imports do not initialize a store, launch a browser or contact a source.
    from .collection_exports import (
        create_export_receipt, discard_export, export_download_path, list_collection_products,
        list_pending_exports, read_export_receipt,
    )
    from .collection_lifecycle import (
        LifecycleError, archive_download_path, finalize_export, list_archives,
    )

    router = APIRouter(prefix="/api/v1/collection", dependencies=[Depends(authenticated)])

    def fail(exc: Exception):
        if isinstance(exc, LifecycleError):
            raise HTTPException(getattr(exc, "status_code", 409), str(exc)) from exc
        if isinstance(exc, (LookupError, FileNotFoundError)):
            raise HTTPException(404, str(exc)) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(422, str(exc)) from exc
        raise exc

    @router.get("/products")
    async def products():
        try:
            result = await asyncio.to_thread(list_collection_products, get_store())
            result["archives"] = await asyncio.to_thread(list_archives, get_store())
            result["pending_exports"] = (await asyncio.to_thread(list_pending_exports, get_store()))["items"]
            return result
        except (LifecycleError, LookupError, ValueError, FileNotFoundError) as exc:
            fail(exc)

    @router.post("/exports")
    async def create_export(payload: CollectionExportRequest):
        try:
            return await asyncio.to_thread(create_export_receipt, get_store(), payload.model_dump())
        except (LifecycleError, LookupError, ValueError, FileNotFoundError) as exc:
            fail(exc)

    @router.get("/exports/{receipt_id}")
    async def receipt(receipt_id: str):
        try:
            return await asyncio.to_thread(read_export_receipt, get_store(), receipt_id)
        except (LifecycleError, LookupError, ValueError, FileNotFoundError) as exc:
            fail(exc)

    @router.post("/exports/{receipt_id}/discard")
    async def discard(receipt_id: str, payload: CollectionDiscardRequest):
        if payload.discard is not True:
            raise HTTPException(422, "Confirm deletion of the server ZIP; source products will be kept")
        try:
            return await asyncio.to_thread(discard_export, get_store(), receipt_id)
        except (LifecycleError, LookupError, ValueError, FileNotFoundError) as exc:
            fail(exc)

    @router.get("/exports/{receipt_id}/download")
    async def download(receipt_id: str):
        try:
            store = get_store()
            path = await asyncio.to_thread(export_download_path, store, receipt_id)
            info = await asyncio.to_thread(read_export_receipt, store, receipt_id)
            return FileResponse(path, filename=info["filename"], media_type="application/zip",
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except (LifecycleError, LookupError, ValueError, FileNotFoundError) as exc:
            fail(exc)

    @router.post("/exports/{receipt_id}/finalize")
    async def finalize(receipt_id: str, payload: CollectionFinalizeRequest):
        def finalize_saved():
            with cleanup_guard():
                return finalize_export(get_store(), receipt_id, payload.archive_product_ids, saved=payload.saved)
        try:
            return await asyncio.to_thread(finalize_saved)
        except (LifecycleError, LookupError, ValueError, FileNotFoundError) as exc:
            fail(exc)

    @router.get("/archives/{archive_id}/download")
    async def download_archive(archive_id: str):
        try:
            store = get_store()
            path = await asyncio.to_thread(archive_download_path, store, archive_id)
            archives = await asyncio.to_thread(list_archives, store)
            info = next((item for item in archives if item["id"] == archive_id), None)
            if info is None:
                raise HTTPException(404, "Product archive not found")
            return FileResponse(path, filename=info["filename"], media_type="application/zip",
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except (LifecycleError, LookupError, ValueError, FileNotFoundError) as exc:
            fail(exc)

    return router
