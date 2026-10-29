"""Durable, opt-in product comparison jobs for the local Hermes workspace."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlsplit

import httpx

from .product_companion import (compare_snapshots, current_child_table_snapshot, live_fourover_structure,
    storefront_html_snapshot, supplier_csv_snapshot, supplier_page_content,
    woocommerce_public_snapshot, selling_scope_for_product)
from .adapters.printitprints import retail_total
from .product_companion_fields import CAPTURE_RENDERED_FIELDS_JS, normalize_field_inventory


BROWN_KRAFT_CAMPAIGN = "companion-4over-business-cards"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/155.0.0.0 Safari/537.36"


def _safe_url(value: str, hosts: set[str]) -> str:
    if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_product_url")
    p = urlsplit(value)
    if p.scheme != "https" or p.hostname not in hosts or p.username or p.password or p.port not in (None, 443) or not p.path.startswith("/"):
        raise ValueError("invalid_product_url")
    return value


def _atomic(path: Path, value: object):
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ProductCompanionService:
    def __init__(self, store, campaigns, *, fetch=None, browser_pool=None):
        self.store, self.campaigns = store, campaigns
        self.root = Path(store.root) / "product-companion"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audits_dir = self.root / "audits"
        self.audits_dir.mkdir(exist_ok=True, mode=0o700)
        self.pairs_path = self.root / "pairs.json"
        self.pairs = json.loads(self.pairs_path.read_text()) if self.pairs_path.exists() else {}
        self.fetch = fetch or self._fetch
        self.browser_pool = browser_pool
        self._child_collector = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._source_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._closed = False
        from .product_companion_writes import ProductCompanionWrites
        self.writes = ProductCompanionWrites(self)

    @staticmethod
    async def _fetch(url: str) -> tuple[int, str, bytes]:
        async with httpx.AsyncClient(follow_redirects=False, timeout=25, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(url)
        if len(response.content) > 12 * 1024 * 1024:
            raise ValueError("document_too_large")
        return response.status_code, str(response.url), response.content

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._schedule_loop(), name="product-companion-schedules")

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def _save_pairs(self):
        _atomic(self.pairs_path, self.pairs)

    def list_pairs(self):
        return sorted(self.pairs.values(), key=lambda x: (x["title"].casefold(), x["product_id"]))

    def get_pair(self, pair_id: str):
        if pair_id not in self.pairs:
            raise KeyError("companion_pair_not_found")
        return dict(self.pairs[pair_id])

    def import_confirmed_business_cards(self, campaign_id=None):
        if campaign_id is None:
            for candidate in (BROWN_KRAFT_CAMPAIGN, "printit-89-20260927"):
                try:
                    campaign = self.campaigns.get(candidate)
                    campaign_id = candidate
                    break
                except (FileNotFoundError, KeyError):
                    continue
            else:
                return {"added": [], "pairs": self.list_pairs(), "reason": "confirmed_business_card_campaign_unavailable"}
        else:
            campaign = self.campaigns.get(campaign_id)
        added = []
        for product in campaign.get("products", []):
            if ("business card" not in product.get("title", "").casefold()
                    or product.get("supplier") not in {"4over", "fourover"}
                    or not product.get("supplier_url") or not product.get("storefront_url")
                    or product.get("stage") != "finalized"):
                continue
            pair_id = "woo-" + str(product["id"])
            if pair_id in self.pairs:
                continue
            self.pairs[pair_id] = {"id": pair_id, "product_id": str(product["id"]),
                "title": product["title"], "supplier": "4over",
                "source_url": _safe_url(product["supplier_url"], {"4over.com"}),
                "storefront_url": _safe_url(product["storefront_url"], {"printitprints.com", "www.printitprints.com"}),
                "campaign_id": campaign_id, "mapping_status": "confirmed_campaign_mapping",
                "schedule_hours": None, "next_run_at": None, "auto_correct": False,
                "latest_audit_id": None, "audit_ids": []}
            added.append(pair_id)
        if added:
            self._save_pairs()
        return {"added": added, "pairs": self.list_pairs()}

    def add_pair(self, data: dict):
        product_id = str(data.get("product_id", ""))
        if not product_id.isdigit() or not 1 <= len(product_id) <= 12:
            raise ValueError("invalid_woocommerce_product_id")
        campaign_id = str(data.get("campaign_id", ""))
        campaign = self.campaigns.get(campaign_id)
        match = next((p for p in campaign.get("products", []) if str(p.get("id")) == product_id), None)
        if not match or match.get("supplier") not in {"4over", "fourover"} or not match.get("supplier_url"):
            raise ValueError("confirmed_4over_campaign_mapping_required")
        if data.get("source_url") != match["supplier_url"] or data.get("storefront_url") != match["storefront_url"]:
            raise ValueError("campaign_mapping_mismatch")
        pair_id = "woo-" + product_id
        if pair_id not in self.pairs:
            self.pairs[pair_id] = {"id": pair_id, "product_id": product_id,
                "title": match["title"], "supplier": "4over",
                "source_url": _safe_url(match["supplier_url"], {"4over.com"}),
                "storefront_url": _safe_url(match["storefront_url"], {"printitprints.com", "www.printitprints.com"}),
                "campaign_id": campaign_id, "mapping_status": "confirmed_campaign_mapping",
                "schedule_hours": None, "next_run_at": None, "auto_correct": False,
                "latest_audit_id": None, "audit_ids": []}
            self._save_pairs()
        return self.get_pair(pair_id)

    async def discover_business_cards(self):
        """Inventory only. A title never establishes a supplier mapping."""
        endpoint = "https://printitprints.com/wp-json/wc/store/v1/products?search=business%20cards&per_page=100"
        status, final, raw = await self.fetch(endpoint)
        if status != 200 or final != endpoint:
            raise ValueError("storefront_catalog_unavailable")
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) > 100:
            raise ValueError("storefront_catalog_schema_changed")
        result = []
        for item in data:
            if not isinstance(item, dict) or "business card" not in str(item.get("name", "")).casefold():
                continue
            product_id = str(item.get("id", ""))
            if not product_id.isdigit():
                continue
            pair = self.pairs.get("woo-" + product_id)
            candidate_path = self.root / "candidates" / (product_id + ".json")
            candidate = json.loads(candidate_path.read_text()) if candidate_path.is_file() and not candidate_path.is_symlink() else {}
            comparison = candidate.get("option_path_comparison") or {}
            differences = len(comparison.get("missing_storefront_option_paths", []))
            result.append({"product_id": product_id, "title": str(item["name"])[:200],
                           "storefront_url": item.get("permalink"),
                           "mapping_status": pair["mapping_status"] if pair else candidate.get("supplier_pair_status", "unresolved"),
                           "supplier_url": pair["source_url"] if pair else candidate.get("source_url"),
                           "option_audit": {"status": comparison.get("status", "unknown"),
                               "matching_paths": comparison.get("source_option_path_count", 0) - differences,
                               "source_paths": comparison.get("source_option_path_count", 0),
                               "differences": differences, "production": "unknown"} if candidate else None})
        return {"items": result, "mapping_rule": "exact_confirmed_campaign_mapping_only"}

    def _source_product(self, pair):
        campaign = self.campaigns.get(pair["campaign_id"])
        product = next((p for p in campaign.get("products", []) if str(p.get("id")) == pair["product_id"]), None)
        if not product or product.get("supplier_url") != pair["source_url"] or product.get("storefront_url") != pair["storefront_url"]:
            raise ValueError("confirmed_mapping_changed")
        return product

    def _collector(self):
        if self.browser_pool is None:
            raise ValueError("authenticated_supplier_browser_unavailable")
        if self._child_collector is None:
            from .fourover_child_prices import FourOverChildPriceCollector
            self._child_collector = FourOverChildPriceCollector(self.store, self.browser_pool)
        return self._child_collector

    async def refresh_child_tables(self, pair_id: str, *, cycle_id: str | None = None):
        """Read each declared child once into the existing resumable 4over cache."""
        if cycle_id is not None and not re.fullmatch(r"[a-f0-9]{32}", cycle_id):
            raise ValueError("invalid_companion_audit_cycle")
        async with self._locks.setdefault(pair_id, asyncio.Lock()):
            pair = self.get_pair(pair_id)
            product = self._source_product(pair)
            option_info = product.get("fourover_option_map_evidence") or {}
            rows_info = product.get("fourover_rows_evidence") or {}
            if not option_info or not rows_info:
                raise ValueError("source_table_or_option_map_missing")
            option_map = json.loads(self._evidence(option_info))
            if option_map.get("source_url") != pair["source_url"] or not option_map.get("complete_child_mapping"):
                raise ValueError("supplier_option_mapping_incomplete")
            saved = supplier_csv_snapshot(self._evidence(rows_info), expected_sha256=rows_info["sha256"],
                                          source_url=pair["source_url"])
            children = sorted({str(row["child_id"]) for row in option_map["rows"]}, key=int)
            if len(children) != len(option_map["rows"]):
                raise ValueError("duplicate_saved_child_mapping")
            current = self.pairs[pair_id]
            if cycle_id is not None and current.get("fresh_source_cycle_id") == cycle_id and current.get("fresh_source_snapshot_id"):
                observed = current.get("fresh_source_observed_at")
                if not isinstance(observed, (int, float)) or not 0 <= time.time() - observed <= 6 * 3600:
                    raise ValueError("cycle_source_snapshot_expired_new_review_cycle_required")
            if (cycle_id is not None and current.get("pending_source_cycle_id") == cycle_id
                    and current.get("pending_source_snapshot_id")):
                raise ValueError("cycle_source_dispatch_outcome_uncertain")
            if (current.get("fresh_source_snapshot_id") and current.get("fresh_source_observed_at")
                    and (cycle_id is None or current.get("fresh_source_cycle_id") == cycle_id)
                    and 0 <= time.time() - current["fresh_source_observed_at"] <= 6 * 3600):
                cached = self._collector().get(current["fresh_source_snapshot_id"], pair["source_url"])
                snapshot = current_child_table_snapshot(option_map, cached, fields=saved["fields"],
                                                       source_url=pair["source_url"])
                return {"status": "already_fresh", "pair_id": pair_id,
                        "snapshot_id": current["fresh_source_snapshot_id"], "child_count": len(children),
                        "row_count": snapshot["row_count"],
                        "scope": "one_set_sample_off_proof_off_child_tables_not_payable_quote",
                        "next_step": "run_companion_audit"}
            # A monthly cycle must not silently reuse a previous month's
            # globally-spent selections or relabel historical tables as fresh.
            pending_same_cycle = cycle_id is None or current.get("pending_source_cycle_id") == cycle_id
            snapshot_id = (current.get("pending_source_snapshot_id") if pending_same_cycle else None) or uuid.uuid4().hex
            current["pending_source_snapshot_id"] = snapshot_id
            current["pending_source_cycle_id"] = cycle_id
            self._save_pairs()
            collector = self._collector()

            async def check_scope():
                latest = self.get_pair(pair_id)
                if (latest["source_url"] != pair["source_url"] or latest["campaign_id"] != pair["campaign_id"]
                        or latest.get("pending_source_snapshot_id") != snapshot_id):
                    raise ValueError("companion_pair_changed_during_refresh")

            try:
                for index in range(0, len(children), 10):
                    chunk = children[index:index + 10]
                    result = await collector.collect(snapshot_id, pair["source_url"], chunk,
                        request_budget=len(chunk) + 1,
                        scope={"sets": 1, "job_sample": False, "digital_proof": False}, check=check_scope)
                    if result.get("status") != "completed_requested_subset":
                        reason = str((result.get("error") or {}).get("code") or "unknown")
                        raise ValueError("supplier_child_table_capture_incomplete:" + reason)
                collection = collector.get(snapshot_id, pair["source_url"])
                current_source = current_child_table_snapshot(option_map, collection, fields=saved["fields"],
                                                              source_url=pair["source_url"])
            except Exception as exc:
                current["last_source_refresh_error"] = type(exc).__name__ + ": " + str(exc)[:120]
                self._save_pairs()
                raise
            current["fresh_source_snapshot_id"] = snapshot_id
            current["fresh_source_cycle_id"] = cycle_id
            current["fresh_source_observed_at"] = time.time()
            current["fresh_source_row_count"] = current_source["row_count"]
            current["pending_source_snapshot_id"] = None
            current["pending_source_cycle_id"] = None
            current["last_source_refresh_error"] = None
            self._save_pairs()
            return {"status": "completed_child_tables", "pair_id": pair_id,
                    "snapshot_id": snapshot_id, "child_count": len(children),
                    "row_count": current_source["row_count"],
                    "scope": "one_set_sample_off_proof_off_child_tables_not_payable_quote",
                    "next_step": "run_companion_audit"}

    def _evidence(self, info: dict) -> bytes:
        relative, digest = info.get("object_path"), info.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str) or not re_full_sha(digest):
            raise ValueError("evidence_reference_invalid")
        path = (self.store.root / relative).resolve()
        if not path.is_relative_to(self.store.root.resolve()) or not path.is_file() or path.is_symlink():
            raise ValueError("evidence_path_invalid")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("evidence_hash_changed")
        return raw

    async def _document(self, url, hosts):
        _safe_url(url, hosts)
        status, final, raw = await self.fetch(url)
        if status != 200 or final != url:
            raise ValueError("page_unavailable_or_login_required")
        return raw.decode("utf-8", "replace")

    async def _rendered_retail_samples(self, pair, site):
        """Observe current purchase fields and optionally check three prices.

        Unsupported retail rules still receive the read-only field scan. The
        same public storefront visit is reused; no supplier selection is made.
        """
        if pair.get("product_id") == "6383":
            from .product_companion_area_render import sample_coroplast_public_storefront
            return await sample_coroplast_public_storefront(pair)
        supported_retail = site["rule"]["status"] == "verified" and tuple(site["fields"]) == tuple(sorted(
            ("size", "shape", "stock", "colorspec", "coating", "quantity")))
        from playwright.async_api import async_playwright
        values = list(site["rows"].values())
        sample_indexes = sorted({0, len(values) // 2, len(values) - 1})
        names = [("size", "size"), ("shape", "shape"), ("stock", "paper_stock"),
                 ("colorspec", "printing"), ("coating", "coating"),
                 ("production", "turnaround"), ("quantity", "quantity")]
        samples, field_inventory = [], None
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    response = await page.goto(pair["storefront_url"], wait_until="domcontentloaded", timeout=30000)
                    if response is None or response.status != 200 or page.url != pair["storefront_url"]:
                        raise ValueError("rendered_storefront_unavailable")
                    try:
                        await page.wait_for_function("() => !!document.querySelector('form.cart select, form.cart input[type=number], #product_addtocart_form select, #product_calculator_display select')", timeout=5000)
                    except Exception:
                        pass  # Empty/unmounted captures stay unknown.
                    captured_fields = normalize_field_inventory(await page.evaluate(CAPTURE_RENDERED_FIELDS_JS))
                    if captured_fields["url"] != pair["storefront_url"] or captured_fields["product_id"] != pair["product_id"]:
                        raise ValueError("rendered_field_identity_changed")
                    field_inventory = captured_fields
                    if not supported_retail:
                        return {"status": "unknown", "reason": "unsupported_rendered_calculator_scope",
                                "samples": [], "field_inventory": field_inventory}
                    for i in sample_indexes:
                        if i != sample_indexes[0]:
                            response = await page.goto(pair["storefront_url"], wait_until="domcontentloaded", timeout=30000)
                            if response is None or response.status != 200 or page.url != pair["storefront_url"]:
                                raise ValueError("rendered_storefront_unavailable")
                        row = values[i]
                        for field, control in names:
                            selected = row["production"] if field == "production" else row["selection"][field]
                            if not selected:
                                raise ValueError("rendered_selection_missing")
                            await page.locator(f'select[name="print_option_{control}"]').select_option(selected, timeout=4000)
                        observed = await page.locator("#price2").inner_text(timeout=4000)
                        match = re.search(r"\$\s*([\d,]+\.\d{2})", observed)
                        if match is None:
                            raise ValueError("rendered_price_unavailable")
                        expected, _ = retail_total(row["cost"], site["rule"]["member"])
                        actual = match.group(1).replace(",", "")
                        samples.append({"selection": row["selection"], "production": row["production"],
                                        "site_cost": row["cost"], "expected_retail": expected,
                                        "displayed_retail": actual, "match": expected == actual})
                finally:
                    await browser.close()
        except Exception as exc:
            return {"status": "unknown", "reason": type(exc).__name__, "samples": samples,
                    "field_inventory": field_inventory}
        return {"status": "match" if all(item["match"] for item in samples) else "mismatch",
                "scope": "three_rendered_rows_only", "samples": samples, "field_inventory": field_inventory}

    async def audit(self, pair_id: str):
        async with self._locks.setdefault(pair_id, asyncio.Lock()):
            pair = self.get_pair(pair_id)
            product = self._source_product(pair)
            rows_info = product.get("fourover_rows_evidence") or {}
            options_info = product.get("fourover_option_map_evidence") or {}
            if not rows_info or not options_info:
                from .product_companion_observed import audit_observed_pair
                details = await audit_observed_pair(self, pair, product)
                return self._record_audit(pair, details)
            source = supplier_csv_snapshot(self._evidence(rows_info), expected_sha256=rows_info["sha256"], source_url=pair["source_url"])
            option_map = json.loads(self._evidence(options_info))
            if option_map.get("source_url") != pair["source_url"] or not option_map.get("complete_child_mapping"):
                raise ValueError("supplier_option_mapping_incomplete")
            child_ids = {str(row["child_id"]) for row in option_map["rows"]}
            freshness, current_snapshot_id = "historical_saved_table", None
            if (pair.get("fresh_source_snapshot_id") and pair.get("fresh_source_observed_at")
                    and 0 <= time.time() - pair["fresh_source_observed_at"] <= 6 * 3600):
                collection = self._collector().get(pair["fresh_source_snapshot_id"], pair["source_url"])
                source = current_child_table_snapshot(option_map, collection, fields=source["fields"],
                                                      source_url=pair["source_url"])
                freshness, current_snapshot_id = "current_child_price_tables_only", pair["fresh_source_snapshot_id"]
            site_html = await self._document(pair["storefront_url"], {"printitprints.com", "www.printitprints.com"})
            site = storefront_html_snapshot(site_html, url=pair["storefront_url"], product_id=pair["product_id"])
            woo_url = f'https://printitprints.com/wp-json/wc/store/v1/products/{pair["product_id"]}'
            woo_status, woo_final, woo_raw = await self.fetch(woo_url)
            if woo_status != 200 or woo_final != woo_url:
                raise ValueError("woocommerce_snapshot_unavailable")
            woo = woocommerce_public_snapshot(woo_raw, product_id=pair["product_id"],
                                              storefront_url=pair["storefront_url"])
            # Serialize supplier checks so an opt-in schedule cannot flood 4over.
            async with self._source_lock:
                source_html = await self._document(pair["source_url"], {"4over.com"})
                structure = live_fourover_structure(source_html, url=pair["source_url"], expected_child_ids=child_ids)
                source_content = supplier_page_content(source_html, url=pair["source_url"])
                await asyncio.sleep(1)
            rendered = await self._rendered_retail_samples(pair, site)
            if rendered.get("field_inventory"):
                site["field_inventory"] = rendered["field_inventory"]
            report = compare_snapshots(source, site, source_freshness=freshness, structure=structure,
                                       source_content=source_content,
                                       selling_scope=selling_scope_for_product(pair["product_id"]))
            if current_snapshot_id:
                table_check = report["checks"].pop("stored_costs_vs_saved_source")
                report["checks"]["stored_costs_vs_current_child_tables"] = table_check
                # A complete child response includes hidden Show More tiers,
                # but does not prove the rendered/conditional quantity controls
                # expose the same full domain. Never certify that UI from a
                # table-only capture or a collapsed seven-row list.
                report["checks"]["current_child_table_quantity_domain"] = report["checks"]["options"]
                report["checks"]["current_supplier_quantity_domain"] = "unknown"
                report["source_quantity_coverage"] = {
                    **source["quantity_coverage"],
                    "quantities_by_child": source["quantity_domains_by_child"],
                }
                report["checks"]["conditional_supplier_options"] = "match"
                report["checks"]["current_child_table_prices"] = table_check
            report["checks"]["woocommerce_identity"] = "match"
            report["checks"]["woocommerce_gallery_vs_page"] = (
                "match" if set(woo["images"]).issubset(set(site["content"]["images"])) else "mismatch")
            report["checks"]["woocommerce_checkout_price"] = "unknown"
            if report["checks"]["woocommerce_gallery_vs_page"] == "mismatch":
                report["status"] = "mismatch"
            report["rendered_retail_samples"] = rendered
            report["checks"]["rendered_retail_samples"] = rendered["status"]
            if rendered["status"] == "mismatch":
                report["status"] = "mismatch"
            details = {
                "current_source_snapshot_id": current_snapshot_id,
                "report": report, "supplier_structure": structure,
                "site_content": site["content"], "supplier_content": source_content,
                "field_observations": {"storefront": site.get("field_inventory"),
                                       "supplier": source_content.get("field_inventory")},
                "woocommerce_snapshot": woo,
                "evidence": {"supplier_rows_sha256": rows_info["sha256"],
                    "current_child_snapshot_sha256": source["sha256"] if current_snapshot_id else None,
                    "supplier_option_map_sha256": options_info["sha256"],
                    "supplier_page_sha256": structure.get("html_sha256"),
                    "storefront_page_sha256": site["html_sha256"],
                    "woocommerce_snapshot_sha256": woo["sha256"],
                    "retail_script_sha256": site["rule"].get("script_sha256")}}
            return self._record_audit(pair, details)

    def _record_audit(self, pair, details):
        # Visibility and scope semantics are part of the evidence contract,
        # including corrections such as visually exposed aria-hidden controls.
        details.setdefault("evidence", {})["field_observer_sha256"] = hashlib.sha256(
            CAPTURE_RENDERED_FIELDS_JS.encode()).hexdigest()
        observed, audit_id = time.time(), uuid.uuid4().hex
        record = {**details, "id": audit_id, "pair_id": pair["id"], "product_id": pair["product_id"],
                  "title": pair["title"], "observed_at": observed, "status": details["report"]["status"],
                  "source_url": pair["source_url"], "storefront_url": pair["storefront_url"],
                  "review": {"status": "pending", "approved_proposal_indexes": []}, "automatic_changes_applied": 0}
        _atomic(self.audits_dir / (audit_id + ".json"), record)
        current = self.pairs.get(pair["id"])
        if current:
            current["latest_audit_id"] = audit_id
            current["audit_ids"] = ([audit_id] + current["audit_ids"])[:100]
            if current.get("schedule_hours"):
                current["next_run_at"] = observed + current["schedule_hours"] * 3600
            self._save_pairs()
        return record

    def _read_audit(self, audit_id: str):
        if not re_full_id(audit_id):
            raise KeyError("companion_audit_not_found")
        path = self.audits_dir / (audit_id + ".json")
        if not path.is_file() or path.is_symlink():
            raise KeyError("companion_audit_not_found")
        return json.loads(path.read_text())

    @staticmethod
    def _audit_read_view(record: dict) -> dict:
        """Reassess legacy certainty without changing the saved evidence.

        Earlier table-only audits marked the complete supplier quantity domain
        as matching. Child-table coverage does not establish the expanded
        rendered/conditional domain. Existing evidence remains immutable; every
        API, history and write-preview consumer receives the conservative view.
        """
        report = record.get("report", {})
        checks = report.get("checks", {})
        coverage = report.get("source_quantity_coverage") or {}
        quantity_unproved = (checks.get("current_supplier_quantity_domain") == "match"
                and not (coverage.get("expanded_rendered_domain") == "verified"
                         and coverage.get("source_domain_complete") is True))
        missing_field_checks = ["%s_duplicate_fields" % location for location in ("storefront", "supplier")
                                if "%s_duplicate_fields" % location not in checks or location not in report.get("field_checks", {})]
        if quantity_unproved or missing_field_checks:
            # JSON round-trip isolates the view from review's raw record.
            record = json.loads(json.dumps(record))
            report = record["report"]
            if quantity_unproved:
                report["checks"]["current_supplier_quantity_domain"] = "unknown"
            for check in missing_field_checks:
                report["checks"][check] = "unknown"
            report["status"] = "mismatch" if record.get("status") == "mismatch" or "mismatch" in report["checks"].values() else "unknown"
            record["status"] = report["status"]
            record["automatic_correction_eligible"] = False
            record["read_assessment"] = {
                "schema": "hermes-companion-audit-read-assessment/v1",
                "reason": "expanded_quantity_domain_evidence_missing" if quantity_unproved else "duplicate_field_observations_missing",
                "original_quantity_domain_check": checks.get("current_supplier_quantity_domain"),
                "unobserved_field_checks": missing_field_checks,
                "source_evidence_changed": False,
                "note": ("Saved child tables do not prove the full expanded supplier quantity domain. " if quantity_unproved else "")
                        + ("Repeated purchase fields and headings were not observed in this saved audit." if missing_field_checks else ""),
            }
        return record

    def get_audit(self, audit_id: str):
        return self._audit_read_view(self._read_audit(audit_id))

    def saved_source_evidence_for_audit(self, record: dict) -> dict[str, bytes]:
        files = {}
        observation = record.get("source_diagnosis_evidence")
        if observation is not None:
            if (not isinstance(observation, dict)
                    or observation.get("sha256") != record["evidence"].get("source_diagnosis_sha256")):
                raise ValueError("audited_observation_reference_changed")
            files["saved-supplier-page-observation.json"] = self._evidence(observation)
        if record.get("audit_mode") == "observed_snapshot":
            if observation is None:
                raise ValueError("audited_observation_reference_missing")
            # Later collection must not add unaudited tables to, or prevent
            # downloading, this immutable observation-only evidence bundle.
            return files
        pair = self.get_pair(record["pair_id"])
        product = self._source_product(pair)
        for field, name, digest_field in (
            ("fourover_rows_evidence", "saved-supplier-costs.csv", "supplier_rows_sha256"),
            ("fourover_option_map_evidence", "saved-supplier-option-map.json", "supplier_option_map_sha256"),
            ("fourover_price_table_evidence", "saved-supplier-price-tables.json", None),
        ):
            info = product.get(field)
            if not isinstance(info, dict):
                continue
            if digest_field and info.get("sha256") != record["evidence"].get(digest_field):
                raise ValueError("audited_evidence_reference_changed")
            files[name] = self._evidence(info)
        snapshot_id = record.get("current_source_snapshot_id")
        if snapshot_id:
            collection = self._collector().get(snapshot_id, pair["source_url"])
            saved = supplier_csv_snapshot(files["saved-supplier-costs.csv"],
                expected_sha256=record["evidence"]["supplier_rows_sha256"], source_url=pair["source_url"])
            projected = current_child_table_snapshot(json.loads(files["saved-supplier-option-map.json"]),
                collection, fields=saved["fields"], source_url=pair["source_url"])
            if projected["sha256"] != record["evidence"].get("current_child_snapshot_sha256"):
                raise ValueError("current_child_snapshot_changed")
            files["current-child-tables.json"] = json.dumps(collection, sort_keys=True).encode()
        return files

    def history(self, pair_id: str):
        pair = self.get_pair(pair_id)
        return [{"id": r["id"], "observed_at": r["observed_at"], "status": r["status"],
                 "checks": r["report"]["checks"], "counts": r["report"]["counts"]}
                for r in (self.get_audit(x) for x in pair["audit_ids"])]

    def set_schedule(self, pair_id: str, *, hours: int | None, auto_correct: bool = False):
        if hours is not None and (type(hours) is not int or not 1 <= hours <= 24 * 30):
            raise ValueError("schedule_hours_out_of_bounds")
        if auto_correct:
            raise ValueError("automatic_correction_not_enabled_use_reviewed_preview")
        pair = self.pairs.get(pair_id)
        if pair is None:
            raise KeyError("companion_pair_not_found")
        pair["schedule_hours"] = hours
        pair["next_run_at"] = time.time() + hours * 3600 if hours else None
        pair["auto_correct"] = bool(auto_correct)
        # Schedules only compare. Writes use a separate reviewed preview.
        self._save_pairs()
        return dict(pair)

    def review(self, audit_id: str, approved_indexes: list[int]):
        record = self._read_audit(audit_id)
        proposals = record["report"]["proposed_changes"]
        if not isinstance(approved_indexes, list) or any(type(i) is not int or i < 0 or i >= len(proposals) for i in approved_indexes):
            raise ValueError("invalid_proposal_index")
        record["review"] = {"status": "approved" if approved_indexes else "dismissed",
                            "approved_proposal_indexes": sorted(set(approved_indexes)), "reviewed_at": time.time()}
        _atomic(self.audits_dir / (audit_id + ".json"), record)
        return self._audit_read_view(record)

    async def _schedule_loop(self):
        while not self._closed:
            for pair in list(self.pairs.values()):
                due = pair.get("next_run_at")
                if due is not None and due <= time.time():
                    try:
                        await self.audit(pair["id"])
                    except Exception as exc:
                        current = self.pairs.get(pair["id"])
                        if current and current.get("schedule_hours"):
                            current["last_error"] = type(exc).__name__
                            current["next_run_at"] = time.time() + current["schedule_hours"] * 3600
                            self._save_pairs()
            await asyncio.sleep(30)


def re_full_sha(value):
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def re_full_id(value):
    return isinstance(value, str) and len(value) == 32 and all(ch in "0123456789abcdef" for ch in value)
