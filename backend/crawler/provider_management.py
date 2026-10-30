"""Authenticated provider configuration and recoverable lifecycle management.

Configuration changes never send a prompt, test a connection or select a new
director. Credentials stay encrypted in Store and are never part of a response.
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException

from .providers import PUBLIC_FIELDS, validate_provider_profile
from .store import ProfileConflict
from .transport import ExtractionError, validate_url


def public_provider(profile: dict) -> dict:
    """Expose only provider configuration, never internal or decrypted fields."""
    result = {key: value for key, value in profile.items() if key in PUBLIC_FIELDS}
    result["has_credentials"] = bool(profile.get("has_credentials"))
    result["status"] = profile.get("status", "active")
    result["revision"] = profile.get("revision", 0)
    return result


def provider_management_router(get_store: Callable, authenticated: Callable,
                               validate_provider_url: Callable = validate_url) -> APIRouter:
    router = APIRouter(prefix="/api/v1/providers", dependencies=[Depends(authenticated)])

    def fail(exc: Exception):
        if isinstance(exc, ProfileConflict):
            raise HTTPException(409, str(exc)) from exc
        if isinstance(exc, LookupError):
            raise HTTPException(404, "Provider not found") from exc
        if isinstance(exc, ValueError):
            raise HTTPException(422, str(exc)) from exc
        raise exc

    def saved(profile_id: str) -> dict:
        profile = next((p for p in get_store().profiles("provider") if p["id"] == profile_id), None)
        if profile is None:
            raise HTTPException(404, "Provider not found")
        return public_provider(profile)

    async def save(data: dict, profile_id: str | None = None, require_existing: bool = False) -> dict:
        data = dict(data)
        if {"status", "revision"}.intersection(data):
            raise HTTPException(422, "Provider lifecycle and revision are server-managed")
        key = data.pop("api_key", None)
        if key is not None:
            if not isinstance(key, str) or any(ord(char) < 32 or ord(char) > 126 for char in key):
                raise HTTPException(422, "API key must be a single-line ASCII string")
            key = key.strip() or None
        # has_credentials is an older client response flag, not writable state.
        data.pop("has_credentials", None)
        try:
            data = validate_provider_profile(data)
            if profile_id and data.get("id", profile_id) != profile_id:
                raise ProfileConflict("Provider ID does not match the requested profile")
            await validate_provider_url(data["base_url"])
            saved_id = get_store().save_provider_profile(
                data, {"api_key": key} if key is not None else None,
                profile_id=profile_id, require_existing=require_existing,
            )
        except ExtractionError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (LookupError, ValueError) as exc:
            fail(exc)
        return saved(saved_id)

    def lifecycle(profile_id: str, action: str, body: dict | None) -> dict:
        if body:
            raise HTTPException(422, "Provider actions do not accept configuration fields")
        try:
            get_store().provider_lifecycle(profile_id, action)
        except (LookupError, ValueError) as exc:
            fail(exc)
        return saved(profile_id)

    @router.get("")
    def list_providers(include_deleted: bool = False):
        return {"items": [public_provider(p) for p in get_store().profiles("provider")
                          if include_deleted or p.get("status", "active") != "deleted"]}

    @router.post("")
    async def create_or_save_provider(data: dict):
        # Existing POST-with-ID clients remain supported; all updates use the
        # same atomic path and retain suspension/deletion protection.
        return await save(data)

    @router.put("/{provider_id}")
    async def edit_provider(provider_id: str, data: dict):
        return await save(data, provider_id, require_existing=True)

    @router.post("/{provider_id}/suspend")
    def suspend_provider(provider_id: str, data: dict | None = None):
        return lifecycle(provider_id, "suspend", data)

    @router.post("/{provider_id}/resume")
    def resume_provider(provider_id: str, data: dict | None = None):
        return lifecycle(provider_id, "resume", data)

    @router.delete("/{provider_id}")
    def delete_provider(provider_id: str, data: dict | None = None):
        return lifecycle(provider_id, "delete", data)

    @router.post("/{provider_id}/restore")
    def restore_provider(provider_id: str, data: dict | None = None):
        return lifecycle(provider_id, "restore", data)

    return router
