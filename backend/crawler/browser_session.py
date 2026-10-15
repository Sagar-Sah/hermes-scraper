"""Ordinary browser sessions owned by one running engine.

Duplicate pages share a context. Optional private Chromium profiles retain login
across engine restarts using Chromium's own session persistence. This module
never reads or exports cookies, authorization headers, or passwords.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .adapters.printograph import AdapterError

_PROFILE_CLAIMS = {}
_PROFILE_CLAIMS_LOCK = threading.Lock()

_SUPPLIER_SESSION_ORIGINS = {
    "printograph.com": "https://www.printograph.com",
    "b2sign.com": "https://www.b2sign.com",
    "soardistusa.com": "https://soardistusa.com",
    "4over.com": "https://4over.com",
    "printitprints.com": "https://printitprints.com",
    "digital-print-solutions.com": "https://digital-print-solutions.com",
}


def canonical_session_origin(origin):
    """Unify only reviewed supplier www/bare HTTPS root-origin aliases.

    This changes profile ownership, not the requested page URL or cookie scope.
    Other origins, subdomains, ports, paths and schemes remain isolated.
    """
    try:
        parts = urlsplit(origin)
        if (parts.scheme != "https" or parts.port not in (None, 443)
                or parts.username is not None or parts.password is not None
                or parts.path not in ("", "/") or parts.query or parts.fragment):
            return origin
        host = parts.hostname or ""
        bare = host.removeprefix("www.")
        return _SUPPLIER_SESSION_ORIGINS.get(bare, origin)
    except (TypeError, ValueError):
        return origin


class BrowserSessionPool:
    def __init__(self, profile_root=None, persist_sessions=False):
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browsers = {}
        self._contexts = {}
        self._closed = False
        self.profile_root = Path(profile_root) if profile_root is not None else None
        self.persist_sessions = bool(persist_sessions)
        self._claimed_profiles = {}
        self._persistent_contexts = set()
        self.origin_resolver = None

    def _track_context(self, key, context, profile=None):
        self._contexts[key] = context
        claim = self._claimed_profiles.get(str(profile)) if profile is not None else None

        def closed():
            # Chromium may be closed independently of the engine. Keeping that
            # context cached makes every subsequent duplicate tab fail forever.
            if self._contexts.get(key) is context:
                self._contexts.pop(key, None)
            self._persistent_contexts.discard(context)
            if profile is not None:
                if self._claimed_profiles.get(str(profile)) is claim:
                    self._claimed_profiles.pop(str(profile), None)
                self._release_profile_claim(str(profile), claim)

        context.on("close", closed)
        return context

    @staticmethod
    def _release_profile_claim(profile, claim):
        # Closing an older pool/context must not erase a newer owner's claim.
        with _PROFILE_CLAIMS_LOCK:
            if claim is not None and _PROFILE_CLAIMS.get(profile) is claim:
                _PROFILE_CLAIMS.pop(profile, None)

    def _profile_path(self, origin):
        explicit = os.getenv("ATLAS_BROWSER_PROFILE_DIR")
        if explicit:
            path = Path(explicit).expanduser().resolve()
        else:
            if self.profile_root is None:
                raise AdapterError(
                    "browser_profile_required",
                    "Persistent sessions need a private browser profile directory",
                )
            self.profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.profile_root.chmod(0o700)
            path = (
                self.profile_root / hashlib.sha256(origin.encode()).hexdigest()
            ).resolve()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        return path

    async def _context(self, origin, headless):
        if callable(self.origin_resolver):
            origin = self.origin_resolver(origin)
        origin = canonical_session_origin(origin)
        async with self._lock:
            if self._closed:
                raise AdapterError(
                    "browser_unavailable", "The browser session pool has stopped"
                )
            profile = self._profile_path(origin) if self.persist_sessions else None
            key = (str(profile) if profile else origin, headless)
            if key in self._contexts:
                return self._contexts[key]
            try:
                if self._playwright is None:
                    from playwright.async_api import async_playwright

                    self._playwright = await async_playwright().start()
                if profile is not None:
                    with _PROFILE_CLAIMS_LOCK:
                        if str(profile) in _PROFILE_CLAIMS:
                            raise AdapterError(
                                "browser_profile_in_use",
                                "This source login profile is already open; reuse its browser session or close it before relaunching",
                            )
                        claim = object()
                        _PROFILE_CLAIMS[str(profile)] = claim
                    self._claimed_profiles[str(profile)] = claim
                    try:
                        context = (
                            await self._playwright.chromium.launch_persistent_context(
                                str(profile),
                                headless=headless,
                                channel="chromium",
                                service_workers="block",
                                args=["--restore-last-session"],
                            )
                        )
                    except BaseException as exc:
                        self._release_profile_claim(str(profile), claim)
                        if self._claimed_profiles.get(str(profile)) is claim:
                            self._claimed_profiles.pop(str(profile), None)
                        if isinstance(exc, Exception) and any(
                            word in str(exc).lower()
                            for word in (
                                "singleton",
                                "profile in use",
                                "user data directory is already",
                            )
                        ):
                            raise AdapterError(
                                "browser_profile_in_use",
                                "The source login profile is open in another browser process; close that browser before retrying",
                            ) from exc
                        raise
                    self._persistent_contexts.add(context)
                    # Keep a blank anchor tab so the ordinary browser window
                    # stays open without issuing idle supplier requests.
                    if not context.pages:
                        await context.new_page()
                    return self._track_context(key, context, profile)
                if headless not in self._browsers:
                    self._browsers[headless] = await self._playwright.chromium.launch(
                        headless=headless
                    )
                    browser = self._browsers[headless]
                    def disconnected():
                        if self._browsers.get(headless) is browser:
                            self._browsers.pop(headless, None)
                    browser.on("disconnected", disconnected)
                # Only an explicitly supplied state file may seed a context.
                # Its updated state is never written back to disk.
                state = os.getenv("ATLAS_BROWSER_STORAGE_STATE")
                context = await self._browsers[headless].new_context(
                    service_workers="block",
                    **({"storage_state": state} if state else {}),
                )
                return self._track_context(key, context)
            except AdapterError:
                raise
            except Exception as exc:
                raise AdapterError(
                    "browser_unavailable",
                    "Chromium could not start; install its runtime or configure a headed browser environment",
                ) from exc

    @asynccontextmanager
    async def page(self, origin, *, headless=True):
        context = await self._context(origin, headless)
        page = await context.new_page()
        try:
            yield page
        finally:
            if not page.is_closed():
                await page.close()

    async def close(self):
        """Call after all engine extraction tasks have been stopped and joined."""
        async with self._lock:
            self._closed = True
            contexts, self._contexts = list(self._contexts.values()), {}
            browsers, self._browsers = list(self._browsers.values()), {}
            playwright, self._playwright = self._playwright, None
            persistent, self._persistent_contexts = self._persistent_contexts, set()
            claims, self._claimed_profiles = self._claimed_profiles, {}
        # Attempt every cleanup even if one target has already disappeared.
        try:
            # Native restoration keeps session cookies. Save only blank tabs so
            # the next launch does not replay old product-page navigations.
            await asyncio.gather(
                *(
                    page.goto("about:blank", wait_until="commit", timeout=5000)
                    for context in persistent
                    for page in context.pages
                ),
                return_exceptions=True,
            )
            await asyncio.gather(
                *(context.close() for context in contexts), return_exceptions=True
            )
            await asyncio.gather(
                *(browser.close() for browser in browsers), return_exceptions=True
            )
            if playwright is not None:
                await playwright.stop()
        finally:
            for profile, claim in claims.items():
                self._release_profile_claim(profile, claim)


@asynccontextmanager
async def browser_page(transport, origin, *, headless=True):
    """Use the engine's pool, or a short-lived context for standalone callers."""
    pool = getattr(transport, "browser_pool", None)
    if pool is not None:
        async with pool.page(origin, headless=headless) as page:
            yield page
        return
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = None
        context = None
        try:
            profile = os.getenv("ATLAS_BROWSER_PROFILE_DIR")
            state = os.getenv("ATLAS_BROWSER_STORAGE_STATE")
            try:
                if profile:
                    context = await playwright.chromium.launch_persistent_context(
                        profile, headless=headless, service_workers="block"
                    )
                else:
                    browser = await playwright.chromium.launch(headless=headless)
                    context = await browser.new_context(
                        service_workers="block",
                        **({"storage_state": state} if state else {}),
                    )
            except Exception as exc:
                raise AdapterError(
                    "browser_unavailable",
                    "Chromium could not start; install its runtime or configure a headed browser environment",
                ) from exc
            page = await context.new_page()
            yield page
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
