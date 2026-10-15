"""Ordinary local-browser login persistence; no supplier or real credentials."""

import asyncio
import hashlib
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from crawler.adapters.printograph import AdapterError
from crawler.adapters.browser import browser_slot
from crawler.browser_session import BrowserSessionPool, canonical_session_origin


class SupplierOriginAliases(unittest.IsolatedAsyncioTestCase):
    """Only source-origin metadata is used; this fake runtime cannot navigate."""

    def runtime(self):
        class Context:
            def __init__(self):
                self.pages = []
                self.listeners = {}

            def on(self, event, callback):
                self.listeners[event] = callback

            async def new_page(self):
                async def goto(url, **kwargs):
                    if url != "about:blank":
                        raise AssertionError("No supplier navigation is permitted")
                page = SimpleNamespace(url="about:blank", goto=goto)
                self.pages.append(page)
                return page

            async def close(self):
                if "close" in self.listeners:
                    self.listeners["close"]()

        browser = SimpleNamespace(new_context=AsyncMock(side_effect=lambda **kwargs: Context()),
                                  on=lambda *args: None, close=AsyncMock())
        return SimpleNamespace(chromium=SimpleNamespace(
            launch=AsyncMock(return_value=browser),
            launch_persistent_context=AsyncMock(side_effect=lambda *args, **kwargs: Context())),
            stop=AsyncMock())

    async def test_known_www_and_bare_suppliers_share_only_their_own_context(self):
        suppliers = {"printograph.com": "https://www.printograph.com",
                     "b2sign.com": "https://www.b2sign.com",
                     "soardistusa.com": "https://soardistusa.com",
                     "4over.com": "https://4over.com",
                     "printitprints.com": "https://printitprints.com",
                     "digital-print-solutions.com": "https://digital-print-solutions.com"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"ATLAS_BROWSER_PROFILE_DIR": "", "ATLAS_BROWSER_STORAGE_STATE": ""}
        ):
            for persist in (False, True):
                with self.subTest(persistent=persist):
                    runtime = self.runtime()
                    pool = BrowserSessionPool(profile_root=Path(directory), persist_sessions=persist)
                    pool._playwright = runtime
                    try:
                        contexts = []
                        for domain, canonical in suppliers.items():
                            original = await pool._context("https://www." + domain, True)
                            self.assertIs(original, await pool._context("https://" + domain, True))
                            self.assertIs(original, await pool._context("https://" + domain + ":443/", True))
                            contexts.append(original)
                            if persist:
                                expected = str((Path(directory) / hashlib.sha256(canonical.encode()).hexdigest()).resolve())
                                self.assertIn((expected, True), pool._contexts)
                        self.assertEqual(len({id(context) for context in contexts}), 6)
                        self.assertEqual(len(pool._contexts), 6)
                        if persist:
                            self.assertEqual(runtime.chromium.launch_persistent_context.await_count, 6)
                            # Existing Printograph profiles keep exactly the old www hash.
                            self.assertEqual(runtime.chromium.launch_persistent_context.await_args_list[0].args[0],
                                             str((Path(directory) / hashlib.sha256(b"https://www.printograph.com").hexdigest()).resolve()))
                    finally:
                        await pool.close()

    async def test_unknown_or_nonroot_origins_remain_separate(self):
        origins = ["https://b2sign.com", "http://b2sign.com", "https://b2sign.com:8443",
                   "https://login.b2sign.com", "https://www.www.b2sign.com", "https://b2sign.com.evil.test",
                   "https://unreviewed.test", "https://www.unreviewed.test",
                   "https://b2sign.com/product", "https://b2sign.com?x=1",
                   "https://b2sign.com#fragment", "https://user@b2sign.com", "https://b2sign.com."]
        pool = BrowserSessionPool()
        pool._playwright = self.runtime()
        try:
            contexts = [await pool._context(origin, True) for origin in origins]
            self.assertEqual(len({id(context) for context in contexts}), len(origins))
            for origin in origins[1:]:
                self.assertEqual(canonical_session_origin(origin), origin)
        finally:
            await pool.close()

    def test_invalid_or_nonstandard_ports_are_not_canonicalized(self):
        for origin in ("https://b2sign.com:not-a-port", "https://b2sign.com:99999", "https://b2sign.com:80"):
            self.assertEqual(canonical_session_origin(origin), origin)

    async def test_registered_custom_aliases_reuse_native_context_and_builtin_hashes(self):
        from crawler.supplier_registry import SupplierRegistry
        from crawler.product_diagnosis import SUPPLIERS
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"ATLAS_BROWSER_PROFILE_DIR": ""}), \
                patch("crawler.supplier_registry.validate_url", AsyncMock()):
            registry = SupplierRegistry(directory, SUPPLIERS)
            await registry.add("Custom", "https://www.vendor.example", "https://vendor.example/login")
            pool = BrowserSessionPool(profile_root=Path(directory) / "profiles", persist_sessions=True)
            pool._playwright = self.runtime()
            pool.origin_resolver = registry.canonical_origin
            try:
                original = await pool._context("https://www.vendor.example", True)
                self.assertIs(original, await pool._context("https://vendor.example", True))
                self.assertIsNot(original, await pool._context("https://account.vendor.example", True))
                default = await pool._context("https://printograph.com", True)
                self.assertIs(default, await pool._context("https://www.printograph.com", True))
                expected = str((pool.profile_root / hashlib.sha256(b"https://www.printograph.com").hexdigest()).resolve())
                self.assertIn((expected, True), pool._contexts)
            finally:
                await pool.close()


class PersistentBrowserSession(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                if not Path(p.chromium.executable_path).exists():
                    self.skipTest("Playwright Chromium runtime is not installed")
        except ImportError:
            self.skipTest("Playwright is not installed")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                # This deliberately has no Expires/Max-Age: a session cookie.
                login = self.path == "/login"
                signed_in = login or "fixture_session=present" in self.headers.get(
                    "Cookie", ""
                )
                content = (
                    "<title>Local session fixture</title><h1>"
                    + ("Signed in" if signed_in else "Signed out")
                    + "</h1>"
                ).encode()
                self.send_response(200)
                if login:
                    self.send_header(
                        "Set-Cookie",
                        "fixture_session=present; HttpOnly; SameSite=Lax; Path=/",
                    )
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory()
        self.profile_root = Path(self.temp.name) / "profiles"

    async def asyncTearDown(self):
        if hasattr(self, "server"):
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()
            self.temp.cleanup()

    async def test_duplicate_tabs_and_restart_retain_native_session_cookie(self):
        with patch.dict(
            "os.environ",
            {"ATLAS_BROWSER_PROFILE_DIR": "", "ATLAS_BROWSER_STORAGE_STATE": ""},
        ):
            pool = BrowserSessionPool(
                profile_root=self.profile_root, persist_sessions=True
            )
            try:
                async with pool.page(self.origin) as original:
                    await original.goto(self.origin + "/login")
                    self.assertEqual(
                        await original.locator("h1").inner_text(), "Signed in"
                    )
                    async with pool.page(self.origin) as duplicate:
                        self.assertIs(original.context, duplicate.context)
                        await duplicate.goto(self.origin + "/check")
                        self.assertEqual(
                            await duplicate.locator("h1").inner_text(), "Signed in"
                        )
                self.assertEqual(len(next(iter(pool._contexts.values())).pages), 1)
                self.assertEqual(
                    next(iter(pool._contexts.values())).pages[0].url, "about:blank"
                )
            finally:
                await pool.close()

            profiles = list(self.profile_root.iterdir())
            self.assertEqual(len(profiles), 1)
            self.assertEqual(stat.S_IMODE(self.profile_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(profiles[0].stat().st_mode), 0o700)

            reopened = BrowserSessionPool(
                profile_root=self.profile_root, persist_sessions=True
            )
            try:
                async with reopened.page(self.origin) as page:
                    await page.goto(self.origin + "/check")
                    self.assertEqual(await page.locator("h1").inner_text(), "Signed in")
            finally:
                await reopened.close()

    async def test_profile_lock_is_explicit_and_memory_opt_out_does_not_write_profile(
        self,
    ):
        with patch.dict(
            "os.environ",
            {"ATLAS_BROWSER_PROFILE_DIR": "", "ATLAS_BROWSER_STORAGE_STATE": ""},
        ):
            first = BrowserSessionPool(
                profile_root=self.profile_root, persist_sessions=True
            )
            second = BrowserSessionPool(
                profile_root=self.profile_root, persist_sessions=True
            )
            memory_root = Path(self.temp.name) / "memory"
            memory = BrowserSessionPool(
                profile_root=memory_root, persist_sessions=False
            )
            try:
                async with first.page(self.origin):
                    with self.assertRaises(AdapterError) as error:
                        async with second.page(self.origin):
                            self.fail("The same Chromium profile must not open twice")
                    self.assertEqual(error.exception.code, "browser_profile_in_use")
                    async with memory.page(self.origin) as page:
                        await page.goto(self.origin + "/check")
                        self.assertEqual(
                            await page.locator("h1").inner_text(), "Signed out"
                        )
                    self.assertFalse(memory_root.exists())
            finally:
                await first.close()
                await second.close()
                await memory.close()

    async def test_closed_native_context_is_replaced_without_stale_profile_claim(self):
        with patch.dict(
            "os.environ",
            {"ATLAS_BROWSER_PROFILE_DIR": "", "ATLAS_BROWSER_STORAGE_STATE": ""},
        ):
            pool = BrowserSessionPool(profile_root=self.profile_root, persist_sessions=True)
            try:
                async with pool.page(self.origin) as page:
                    await page.goto(self.origin + "/login")
                    original = page.context
                    await original.close()
                self.assertFalse(pool._contexts)
                self.assertFalse(pool._claimed_profiles)
                async with pool.page(self.origin) as replacement:
                    self.assertIsNot(replacement.context, original)
                    await replacement.goto(self.origin + "/check")
                    self.assertEqual(await replacement.locator("h1").inner_text(), "Signed in")
            finally:
                await pool.close()

    async def test_old_pool_cleanup_cannot_release_reopened_profile_claim(self):
        from crawler import browser_session

        with patch.dict(
            "os.environ",
            {"ATLAS_BROWSER_PROFILE_DIR": "", "ATLAS_BROWSER_STORAGE_STATE": ""},
        ):
            first = BrowserSessionPool(profile_root=self.profile_root, persist_sessions=True)
            second = BrowserSessionPool(profile_root=self.profile_root, persist_sessions=True)
            third = BrowserSessionPool(profile_root=self.profile_root, persist_sessions=True)
            stopped_contexts, finish_stop = asyncio.Event(), asyncio.Event()
            closing = None
            try:
                await first._context(self.origin, True)
                playwright = first._playwright
                original_stop = playwright.stop

                async def delayed_stop():
                    # close() has closed the native context and its callback has
                    # released the original claim, but final cleanup is pending.
                    stopped_contexts.set()
                    await finish_stop.wait()
                    await original_stop()

                with patch.object(playwright, "stop", delayed_stop):
                    closing = asyncio.create_task(first.close())
                    await asyncio.wait_for(stopped_contexts.wait(), timeout=15)
                    async with second.page(self.origin):
                        profile, newer_claim = next(iter(second._claimed_profiles.items()))
                        finish_stop.set()
                        await asyncio.wait_for(closing, timeout=15)
                        self.assertIs(browser_session._PROFILE_CLAIMS.get(profile), newer_claim)
                        with self.assertRaises(AdapterError) as error:
                            async with third.page(self.origin):
                                self.fail("The newer owner's live profile cannot be claimed")
                        self.assertEqual(error.exception.code, "browser_profile_in_use")
            finally:
                finish_stop.set()
                if closing is not None:
                    await asyncio.gather(closing, return_exceptions=True)
                await first.close()
                await second.close()
                await third.close()

    async def test_disconnected_ephemeral_browser_can_create_a_new_page(self):
        pool = BrowserSessionPool()
        try:
            async with pool.page(self.origin) as page:
                await page.goto(self.origin + "/check")
                original = page.context.browser
                await original.close()
            self.assertFalse(pool._contexts)
            self.assertFalse(pool._browsers)
            async with pool.page(self.origin) as replacement:
                self.assertIsNot(replacement.context.browser, original)
                await replacement.goto(self.origin + "/check")
                self.assertEqual(await replacement.locator("h1").inner_text(), "Signed out")
        finally:
            await pool.close()

    async def test_three_product_tabs_share_login_and_queued_cancel_keeps_slots(self):
        semaphore = asyncio.Semaphore(3)
        ready, release, queued, cancelled = (asyncio.Event() for _ in range(4))
        active_pages = []
        contexts = set()

        async def check():
            pass

        async def queued_check():
            queued.set()
            if cancelled.is_set():
                raise InterruptedError("fixture cancelled queued product")

        with patch.dict(
            "os.environ",
            {"ATLAS_BROWSER_PROFILE_DIR": "", "ATLAS_BROWSER_STORAGE_STATE": ""},
        ), patch("crawler.adapters.browser._BROWSER_LOCK", semaphore):
            pool = BrowserSessionPool(
                profile_root=self.profile_root, persist_sessions=True
            )
            tasks = []
            try:
                async with pool.page(self.origin) as login:
                    await login.goto(self.origin + "/login")

                async def product():
                    async with browser_slot(check):
                        async with pool.page(self.origin) as page:
                            await page.goto(self.origin + "/check")
                            self.assertEqual(
                                await page.locator("h1").inner_text(), "Signed in"
                            )
                            contexts.add(id(page.context))
                            active_pages.append(page)
                            if len(active_pages) == 3:
                                ready.set()
                            try:
                                await release.wait()
                            finally:
                                active_pages.remove(page)

                async def fourth():
                    async with browser_slot(queued_check):
                        self.fail("A fourth product must wait for a browser slot")

                tasks = [asyncio.create_task(product()) for _ in range(3)]
                await asyncio.wait_for(ready.wait(), timeout=15)
                self.assertEqual(len(active_pages), 3)
                self.assertEqual(len(contexts), 1)
                fourth_task = asyncio.create_task(fourth())
                tasks.append(fourth_task)
                await queued.wait()
                cancelled.set()
                with self.assertRaises(InterruptedError):
                    await asyncio.wait_for(fourth_task, timeout=2)
                self.assertTrue(semaphore.locked())
                self.assertEqual(len(active_pages), 3)
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                await pool.close()
            self.assertEqual(semaphore._value, 3)


if __name__ == "__main__":
    unittest.main()
