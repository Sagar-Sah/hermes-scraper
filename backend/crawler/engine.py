import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import httpx
from .tls import tls_context
from sqlalchemy import select, update, func

from .store import Run, Product, Event, Response, canonical, digest, uid
from .transport import Transport, ExtractionError, RunInterrupted
from .browser_session import BrowserSessionPool
from .commerce import decimal_value, unit_price
from .record_scope import is_legacy_unscoped


def row_identity(row):
    """Identify a selection, independent of presentation or verification state."""
    configuration = {
        key: value["value"] if isinstance(value, dict) and "value" in value else value
        for key, value in (row.get("configuration") or {}).items()
    }
    production = row.get("production") or {}
    if isinstance(production, dict):
        for key in ("value", "mode", "id"):
            if key in production:
                production = {key: str(production[key])}
                break
        else:
            production = {
                key: value
                for key, value in production.items()
                if key not in {"status", "label"}
            }
    return digest(
        {
            "url": row.get("product_url"),
            "configuration": configuration,
            "quantity": row.get("quantity"),
            "currency": row.get("currency"),
            "production": production,
            "pricing_context": {
                key: value
                for key, value in row.get("pricing_context", {}).items()
                if key != "source_base_product_id"
            },
        }
    )


def price_signature(row):
    """Compare source subtotals exactly and derive unit prices consistently.

    Historical rows retain their original formatting and rounding. A different
    last digit in a derived unit-price display is not a source subtotal change.
    Rows without a subtotal still compare their available unit-price evidence.
    """
    try:
        if row.get("subtotal") is not None:
            subtotal = decimal_value(row["subtotal"])
            return subtotal, decimal_value(unit_price(subtotal, row.get("quantity")))
        return (
            None,
            (
                decimal_value(row["unit_price"])
                if row.get("unit_price") is not None
                else None
            ),
        )
    except ValueError as exc:
        raise ExtractionError(
            "A price observation contains an invalid amount or quantity",
            "invalid_price",
        ) from exc


class FixtureAdapter:
    """Deterministic synthetic catalog; never confused with supplier observations."""

    async def extract(self, spec, transport, emit):
        count = spec.get("fixture_rows_per_product", 30000)
        await emit(
            "discovery",
            {"title": spec["title"], "expected_rows": count, "fixture": True},
        )
        for i in range(count):
            q = [50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000][i % 10]
            variant = i // 10
            config = {
                "size": f"Size {variant%20+1}",
                "stock": f"Stock {(variant//20)%10+1}",
                "colorspec": f"Color {(variant//200)%3+1}",
                "finish": f"Finish {(variant//600)%5+1}",
                "variant": str(variant),
            }
            amount = (
                Decimal("5.00")
                + Decimal(q) * Decimal("0.025")
                + Decimal(variant % 31) / 10
            ).quantize(Decimal(".01"))
            yield {
                "product_url": spec["url"],
                "title": spec["title"],
                "source_product_id": str(variant),
                "configuration": config,
                "quantity": q,
                "currency": "USD",
                "subtotal": str(amount),
                "unit_price": str(amount / q),
                "provenance": "synthetic_fixture",
                "verification_status": "fixture_verified",
                "evidence_ids": [],
                "production": {
                    "mode": "regular",
                    "min_days": 2,
                    "max_days": 4,
                    "basis": "fixture",
                },
                "images": [],
                "coverage_status": "complete",
                "fixture": True,
            }
            if i % 500 == 0:
                await asyncio.sleep(0)
        await emit("coverage", {"status": "complete", "fixture": True, "valid": count})


class Engine:
    def __init__(self, store):
        self.store = store
        self.owner = uid()
        self.tasks = set()
        self.stopping = False
        self.client = httpx.AsyncClient(
            verify=tls_context(),
            timeout=30,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        )
        self.exports = asyncio.Semaphore(2)
        self.assets = asyncio.Semaphore(4)
        self.finalizing = set()
        self.browser_pool = BrowserSessionPool(
            profile_root=store.root / "browser-profiles",
            persist_sessions=store.config.persist_browser_sessions,
        )

    async def start(self):
        self.loop = asyncio.create_task(self.dispatch())

    async def stop(self):
        self.stopping = True
        self.loop.cancel()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(self.loop, *self.tasks, return_exceptions=True)
        await self.browser_pool.close()
        with self.store.session() as s:
            s.execute(
                update(Product)
                .where(Product.owner == self.owner, Product.status == "running")
                .values(status="queued", lease_until=0)
            )
        await self.client.aclose()

    async def dispatch(self):
        while not self.stopping:
            self.tasks = {t for t in self.tasks if not t.done()}
            claimed = False
            while len(self.tasks) < self.store.config.product_concurrency:
                job = await asyncio.to_thread(self.store.claim, self.owner)
                if not job:
                    break
                task = asyncio.create_task(self.process(job))
                self.tasks.add(task)
                claimed = True
            with self.store.session() as session:
                export_ids = list(
                    session.scalars(select(Run.id).where(Run.status == "exporting"))
                )
            for rid in export_ids:
                if rid not in self.finalizing:
                    self.finalizing.add(rid)
                    task = asyncio.create_task(self.finalize(rid))
                    self.tasks.add(task)
            await asyncio.sleep(0.1 if claimed else 0.5)

    async def process(self, job):
        pid, rid, epoch = job["id"], job["run_id"], job["epoch"]
        spec = dict(job["spec"], url=job["url"])
        spec["selection_filters"] = spec.get("filters", {})
        spec["title"] = spec.get("title") or job["url"].split("/")[-1] or "Product"
        if job["url"].startswith("fixture://"):
            spec["title"] = "Fixture product " + job["url"].rsplit("/", 1)[-1]
        transport = Transport(
            self.store,
            rid,
            pid,
            self.client,
            refresh=spec.get("refresh", False),
            max_requests=spec.get("max_requests"),
        )
        transport.epoch = epoch
        transport.browser_pool = self.browser_pool
        state = {"coverage": "unknown"}
        image_urls = set()

        async def emit(kind, payload):
            await transport.check()
            payload = dict(payload, product_id=pid)
            if kind == "coverage":
                state["coverage"] = payload.get(
                    "status", payload.get("coverage_status", "partial")
                )
                # Durable checkpoint rows are retained observations, not fresh
                # source validations. Keep the adapter's coverage status intact
                # for the disappearance check after extraction completes.
                retained = set(prior) - seen
                skipped = (
                    browser_progress.skipped_ids
                    if browser_progress is not None
                    else set()
                )
                unvisited = retained - skipped
                payload["retained_prior_rows_without_revalidation"] = len(retained)
                payload["unvisited_prior_rows"] = len(unvisited)
                if retained:
                    payload["source_revalidated_on_resume"] = False
                if unvisited:
                    payload["reasons"] = sorted(
                        set(payload.get("reasons") or []) | {"unvisited_prior_rows"}
                    )
                    payload["count_is_exact"] = False
            if kind == "discovery" and payload.get("title"):
                with self.store.session() as s:
                    s.execute(
                        update(Product)
                        .where(Product.id == pid, Product.epoch == epoch)
                        .values(title=payload["title"])
                    )
            await asyncio.to_thread(self.store.event, rid, kind, payload)

        async def heartbeat():
            while True:
                await asyncio.sleep(15)
                if not await asyncio.to_thread(self.store.heartbeat, pid, epoch):
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        batch = []
        try:
            mode = spec.get("adapter", "auto")
            if mode == "fixture" or job["url"].startswith("fixture://"):
                adapter = FixtureAdapter()
            elif mode == "printitprints" or (
                mode == "auto" and urlparse(job["url"]).hostname
                in {"printitprints.com", "www.printitprints.com"}
            ):
                from .adapters.printitprints import PrintItPrintsAdapter

                adapter = PrintItPrintsAdapter()
            elif mode == "browser" or (
                mode == "auto"
                and urlparse(job["url"]).hostname
                in {"printograph.com", "www.printograph.com"}
            ):
                from .adapters.browser import BrowserAdapter

                adapter = BrowserAdapter()
            elif mode == "printograph":
                from .adapters.printograph import PrintographAdapter

                adapter = PrintographAdapter()
            else:
                from .adapters.woocommerce import WooCommerceAdapter

                adapter = WooCommerceAdapter()
            seen = set()
            # Recompute semantic identities for older checkpoints without
            # changing their immutable records or original evidence IDs.
            prior = {}
            legacy_count = 0
            for saved in self.store.iter_rows(product_id=pid):
                if is_legacy_unscoped(saved):
                    legacy_count += 1
                    continue
                key = row_identity(saved)
                price = price_signature(saved)
                if key in prior and prior[key] != price:
                    raise ExtractionError(
                        "Checkpoint contains conflicting prices for one selection. Start a new refresh run.",
                        "snapshot_changed",
                    )
                prior[key] = price
                # A resumed browser may skip completed branches entirely. Their
                # images still belong in the final export/download manifest.
                for image in saved.get("images", []):
                    image_url = (
                        image
                        if isinstance(image, str)
                        else image.get("url", image.get("source_url", ""))
                    )
                    if image_url.startswith(("https://", "http://")):
                        image_urls.add(image_url)
            await asyncio.to_thread(
                self.store.set_record_scope, pid, epoch, legacy_count
            )
            known = set(prior)
            browser_progress = None
            if mode == "browser" or adapter.__class__.__name__ == "BrowserAdapter":
                from .browser_progress import BrowserProgress, extraction_scope

                browser_progress = await BrowserProgress(
                    self.store, rid, pid, epoch, extraction_scope(spec), known
                ).load()
                transport.browser_progress = browser_progress
            batch_size = (
                1
                if mode == "browser" or adapter.__class__.__name__ == "BrowserAdapter"
                else 500
            )
            async for row in adapter.extract(spec, transport, emit):
                if is_legacy_unscoped(row):
                    raise ExtractionError(
                        "Browser quote lacks the required service-context schema; refusing unscoped publication",
                        "unscoped_quote",
                    )
                if spec.get('field_policy'):
                    from .diagnosis_run import validate_policy_row
                    try:
                        validate_policy_row(spec['field_policy'], row)
                    except ValueError as exc:
                        raise ExtractionError(str(exc), 'diagnosis_selection_mismatch') from exc
                row["id"] = row_identity(row)
                seen.add(row["id"])
                if spec.get("shipping_contexts") and "shipping" not in row:
                    row["shipping"] = {
                        "status": "unavailable",
                        "reason": "This adapter has no validated live shipping integration",
                        "contexts": spec["shipping_contexts"],
                    }
                for img in row.get("images", []):
                    image_url = (
                        img
                        if isinstance(img, str)
                        else img.get("url", img.get("source_url", ""))
                    )
                    if image_url.startswith(("https://", "http://")):
                        image_urls.add(image_url)
                price = price_signature(row)
                if row["id"] in known:
                    if prior.get(row["id"]) not in (
                        None,
                        price,
                    ):
                        raise ExtractionError(
                            "Source prices changed during extraction or resume. Start a new refresh run to preserve a consistent snapshot.",
                            "snapshot_changed",
                        )
                    if browser_progress is not None:
                        browser_progress.acknowledge(row["id"], row)
                    continue
                known.add(row["id"])
                # Include accepted rows from this pass, even before their batch
                # commits. Repeated identities must never hide a price change.
                prior[row["id"]] = price
                batch.append(row)
                if len(batch) >= batch_size:
                    await transport.check()
                    await asyncio.to_thread(self.store.append_rows, pid, epoch, batch)
                    if browser_progress is not None:
                        for committed in batch:
                            browser_progress.acknowledge(committed["id"], committed)
                    batch = []
            if browser_progress is not None:
                seen.update(browser_progress.skipped_ids)
            if (
                state["coverage"] in {"complete", "visible_domain_exhausted"}
                and set(prior) - seen
            ):
                raise ExtractionError(
                    "Source configurations disappeared during resume. Start a new refresh run.",
                    "snapshot_changed",
                )
            if batch:
                await asyncio.to_thread(self.store.append_rows, pid, epoch, batch)
            await transport.check()
            with self.store.session() as s:
                count = s.scalar(select(Product.row_count).where(Product.id == pid))
            if count:
                from .exporter import export_rows

                async with self.exports:
                    manifest = await asyncio.to_thread(
                        export_rows,
                        self.store.current_rows(product_id=pid),
                        self.store.root / "exports" / rid / pid,
                        metadata=self.store.record_counts(pid),
                    )
                if spec.get("image_mode") in {"download", "both"} and image_urls:
                    from .assets import download_images

                    async with self.assets:
                        manifest["items"].extend(
                            await download_images(
                                image_urls,
                                transport,
                                self.store.root / "exports" / rid / pid / "images",
                            )
                        )
                await transport.check()
                with self.store.session(immediate=True) as s:
                    p = s.get(Product, pid)
                    if p.epoch != epoch or s.get(Run, rid).status not in {
                        "queued",
                        "running",
                    }:
                        raise RunInterrupted("Worker superseded")
                    p.export_manifest = manifest
            await transport.check()
            with self.store.session(immediate=True) as s:
                p = s.get(Product, pid)
                if s.get(Run, rid).status not in {"queued", "running"}:
                    raise RunInterrupted("Run stopped")
                if p.epoch == epoch:
                    p.status = (
                        "completed" if state["coverage"] == "complete" else "partial"
                    )
                    p.coverage_status = state["coverage"]
                    p.lease_until = 0
                    p.error = None
            await asyncio.to_thread(
                self.store.event,
                rid,
                "product_finished",
                {
                    "product_id": pid,
                    "rows": count,
                    "coverage_status": state["coverage"],
                },
            )
        except RunInterrupted:
            with self.store.session() as s:
                p = s.get(Product, pid)
                if p.epoch == epoch:
                    p.status = (
                        "cancelled"
                        if s.get(Run, rid).status == "cancelled"
                        else "queued"
                    )
                    p.lease_until = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "extraction_error")
            recovery = await asyncio.to_thread(
                self.store.retry_product, rid, pid, epoch, code, str(exc)
            )
            if recovery is not None:
                return
            with self.store.session() as s:
                p = s.get(Product, pid)
                if p.epoch == epoch:
                    p.status = (
                        "access_required"
                        if code == "access_required"
                        else "needs_attention"
                    )
                    p.error = str(exc)[:1200]
                    p.coverage_status = "partial" if p.row_count else "unknown"
                    p.lease_until = 0
            await asyncio.to_thread(
                self.store.event,
                rid,
                "product_error",
                {"product_id": pid, "code": code, "message": str(exc)[:1200]},
            )
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await transport.close()
            await asyncio.to_thread(self.finish_run, rid)

    async def finalize(self, rid):
        from .catalog_export import combine_catalog

        transport = Transport(self.store, rid, "", self.client)
        try:
            async with transport.request_guard("catalog-export:" + rid):
                if self.store.status(rid) != "exporting":
                    return
                with self.store.session() as s:
                    products = list(
                        s.scalars(select(Product).where(Product.run_id == rid))
                    )
                for product in products:
                    counts = self.store.record_counts(product.id)
                    if product.row_count and (
                        not product.export_manifest
                        or any(
                            product.export_manifest.get(key) != value
                            for key, value in counts.items()
                        )
                    ):
                        from .exporter import export_rows

                        async with self.exports:
                            partial_manifest = await asyncio.to_thread(
                                export_rows,
                                self.store.current_rows(product_id=product.id),
                                self.store.root / "exports" / rid / product.id,
                                metadata=counts,
                            )
                        with self.store.session(immediate=True) as s:
                            current = s.get(Product, product.id)
                            if (
                                s.get(Run, rid).status != "exporting"
                                or current.epoch != product.epoch
                            ):
                                raise RunInterrupted(
                                    "Run stopped during partial export"
                                )
                            current.export_manifest = partial_manifest
                        product.export_manifest = partial_manifest
                manifests = [p.export_manifest for p in products if p.export_manifest]
                if manifests:
                    async with self.exports:
                        result = await asyncio.to_thread(
                            combine_catalog,
                            manifests,
                            self.store.root / "exports" / rid / "catalog",
                        )
                    self.store.event(
                        rid,
                        "catalog_export_ready",
                        {"rows": result.get("row_count", 0)},
                    )
                with self.store.session(immediate=True) as s:
                    r = s.get(Run, rid)
                    if r.status != "exporting":
                        return
                    states = list(
                        s.scalars(select(Product.status).where(Product.run_id == rid))
                    )
                    r.status = (
                        "completed"
                        if all(x == "completed" for x in states)
                        else (
                            "completed_with_gaps"
                            if any(x in {"completed", "partial"} for x in states)
                            else "needs_attention"
                        )
                    )
                    r.updated_at = time.time()
        except RunInterrupted:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with self.store.session() as s:
                r = s.get(Run, rid)
                if r.status == "exporting":
                    r.status = "needs_attention"
                    r.error = "Catalog export failed: " + str(exc)[:1000]
            self.store.event(rid, "export_error", {"message": str(exc)[:1000]})
        finally:
            self.finalizing.discard(rid)

    def finish_run(self, rid):
        with self.store.session(immediate=True) as s:
            r = s.get(Run, rid)
            if not r or r.status not in {"queued", "running"}:
                return
            states = list(
                s.scalars(select(Product.status).where(Product.run_id == rid))
            )
            if any(x in {"queued", "running"} for x in states):
                return
            domain = getattr(self, "source_domains", None)
            probes = getattr(self, "probes", None)
            if ((domain is not None and domain.has_active_scope(rid, s)) or
                    (probes is not None and probes.has_active_scope(rid, s))):
                # Product walkers may be paused while an explicitly authorized
                # finite discovery stage is still reading source tables.
                r.status = "running"
                r.updated_at = time.time()
                return
            if "paused" in states:
                r.status = "paused"
                r.updated_at = time.time()
                return
            r.status = "exporting"
            r.updated_at = time.time()
            return True
