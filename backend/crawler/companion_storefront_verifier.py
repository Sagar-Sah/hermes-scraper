"""Independent, bounded storefront checks after a typed Companion repair.

The built-in browser lane reads only one approved store origin and never submits
a form, uploads artwork, changes a cart, visits a supplier, or places an order.
Shipping and customer-flow checks require separately registered trusted code
fixtures. Missing adapters or evidence stay unknown; model output is not a
fixture registration or a verification receipt.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import inspect
import json
import re
import time
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .adapters.printitprints import _declaration, retail_total
from .companion_environments import digest
from .product_companion import _index, _money, _retail_rule


REQUIRED_CHECKS = ("calculator_data", "rendered_prices", "artwork_dimensions", "shipping", "customer_flow")
ALL_CHECKS = (*REQUIRED_CHECKS, "documents", "retail_rule_unchanged")
MAX_CASES = 12
CONTROL_NAMES = {"size": "size", "shape": "shape", "stock": "paper_stock", "colorspec": "printing", "coating": "coating", "quantity": "quantity"}
SUPPORTED_LEGACY_FIELDS = frozenset(CONTROL_NAMES)
SHA = re.compile(r"^[a-f0-9]{64}$")
PRODUCT_ID = re.compile(r"^[1-9][0-9]{0,11}$")


def _sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _selection(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("storefront_selection_missing")
    out = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or not isinstance(item, (str, int)) or isinstance(item, bool):
            raise ValueError("storefront_selection_invalid")
        out[key] = " ".join(str(item).split())
    if "quantity" in out:
        if not out["quantity"].isdigit() or int(out["quantity"]) < 1:
            raise ValueError("storefront_quantity_invalid")
        out["quantity"] = str(int(out["quantity"]))
    return out


def row_signature(rows):
    """Semantic full-table comparison, preserving exact decimal costs."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100000:
        raise ValueError("storefront_rows_missing")
    values, identities = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("storefront_row_invalid")
        selection = json.dumps(_selection(row.get("selection")), sort_keys=True)
        if selection in identities:
            raise ValueError("storefront_duplicate_configuration")
        identities.add(selection)
        production = row.get("production")
        if production is not None and not isinstance(production, str):
            raise ValueError("storefront_production_invalid")
        values.append((selection, _money(row.get("cost")), " ".join((production or "").split())))
    return sorted(values)


def expected_data(manifest, *, rollback=False):
    """Derive the expected result from the frozen before-state and typed edits."""
    before = manifest.get("before_data")
    if not isinstance(before, dict) or before.get("pricing_basis") != "supplier_cost" or before.get("currency") != "USD":
        raise ValueError("storefront_supported_cost_basis_required")
    result = deepcopy(before)
    row_signature(result.get("rows"))
    if not isinstance(result.get("documents"), list):
        raise ValueError("storefront_document_baseline_missing")
    if rollback:
        return result
    changes = manifest.get("changes")
    if manifest.get("schema") == "hermes-read-only-product-audit/v1":
        if changes != []:
            raise ValueError("storefront_read_only_audit_cannot_change_data")
        return result
    if not isinstance(changes, list) or not changes or len(changes) > 1000:
        raise ValueError("storefront_typed_changes_required")
    for operation in changes:
        if not isinstance(operation, dict):
            raise ValueError("storefront_repair_operation_invalid")
        kind = operation.get("kind")
        if kind == "add_document":
            document = {"url": operation.get("url"), "label": operation.get("label")}
            parsed = urlsplit(document["url"] or "")
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or not isinstance(document["label"], str) or not document["label"]):
                raise ValueError("storefront_document_invalid")
            if any(item.get("url") == document["url"] for item in result["documents"]):
                raise ValueError("storefront_duplicate_document")
            result["documents"].append(document)
            continue
        if kind not in {"set_cost", "add_row"}:
            raise ValueError("storefront_repair_operation_unsupported")
        wanted = _selection(operation.get("selection"))
        matches = [row for row in result["rows"] if _selection(row["selection"]) == wanted]
        cost = format(_money(operation.get("cost")), "f")
        if kind == "set_cost":
            if len(matches) != 1:
                raise ValueError("storefront_repair_configuration_ambiguous")
            matches[0]["cost"] = cost
        else:
            if matches or not isinstance(operation.get("production"), str) or not operation["production"]:
                raise ValueError("storefront_added_configuration_incomplete")
            result["rows"].append({"selection": wanted, "cost": cost, "production": operation["production"]})
    row_signature(result["rows"])
    return result


def sample_rows(manifest, expected, *, rollback=False):
    """Render every changed row up to the declared bound; never silently sample it."""
    changes = [item for item in manifest.get("changes", []) if item.get("kind") in {"set_cost", "add_row"}]
    wanted = [_selection(item["selection"]) for item in changes]
    rows = [row for row in expected["rows"] if any(_selection(row["selection"]) == selection for selection in wanted)]
    # Added rows are absent after rollback; check the restored baseline instead.
    if rollback:
        rows = [row for row in rows if any(_selection(old["selection"]) == _selection(row["selection"]) for old in manifest["before_data"]["rows"])]
    if not rows:
        values = expected["rows"]
        rows = [values[index] for index in sorted({0, len(values) // 2, len(values) - 1})]
    if len(rows) > MAX_CASES:
        raise ValueError("storefront_changed_row_render_bound_exceeded")
    return deepcopy(rows)


def allowed_read_request(method, url, origin):
    try:
        parsed, base = urlsplit(url), urlsplit(origin)
        if (method not in {"GET", "HEAD"} or parsed.scheme != "https" or parsed.netloc != base.netloc
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return False
        if set(parse_qs(parsed.query)) & {"add-to-cart", "remove_item", "undo_item", "wc-ajax", "wc-api", "action"}:
            return False
        return not re.search(r"/(?:cart|checkout|my-account|wp-admin|wp-login\.php|wc-api)(?:/|$)|/wp-json/wc/(?:store/v\d+/cart|v\d+/orders)(?:/|$)", parsed.path)
    except ValueError:
        return False


def dimensions(value, *, assumed_unit=None):
    """Read one unambiguous pair; unlabelled numbers need a trusted unit contract."""
    if not isinstance(value, str) or len(value) > 300:
        return None
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(inches|inch|in|["″]|ft|feet|[\'′])?\s*[x×]\s*(\d+(?:\.\d+)?)\s*(inches|inch|in|["″]|ft|feet|[\'′])?\s*', value, re.I)
    if not match:
        return None
    units = [match[2], match[4]]
    default = units[0] or units[1] or assumed_unit
    default = default.lower() if isinstance(default, str) else default
    if default not in {"in", "inches", "inch", '"', '″', "ft", "feet", "'", "′"}:
        return None
    out = []
    for number, unit in ((match[1], units[0]), (match[3], units[1])):
        amount = Decimal(number)
        if amount <= 0:
            return None
        out.append(format(amount * (12 if (unit or default).lower() in {"ft", "feet", "'", "′"} else 1), "f"))
    return out


def _check_artwork(expected_size, observation, contract):
    unit = (contract or {}).get("canonical_unit") or (contract or {}).get("dimension_unit")
    if unit == "inches":
        unit = "in"
    if unit not in {"in", "ft"}:
        return "unknown", "artwork_unit_contract_missing"
    wanted = dimensions(expected_size, assumed_unit=unit)
    if wanted is None:
        return "unknown", "artwork_expected_dimensions_unreadable"
    candidates = observation.get("artwork_dimensions")
    if not isinstance(candidates, list) or not candidates:
        return "unknown", "artwork_dimension_bridge_not_observed"
    actual = [dimensions(item.get("value"), assumed_unit=item.get("unit") or unit) for item in candidates if isinstance(item, dict)]
    if not actual or any(value is None for value in actual):
        return "unknown", "artwork_observed_dimensions_unreadable"
    same = all([Decimal(value) for value in pair] == [Decimal(value) for value in wanted] for pair in actual)
    return ("match", "selected_artwork_bridge_matches") if same else ("mismatch", "artwork_dimension_bridge_changed")


@dataclass(frozen=True)
class TrustedStorefrontFixture:
    """Registered in server code by a developer, never by a model/API payload.

    A staging_sandbox callback may exercise a disposable staging cart using its
    own declared test data, but must restore it. A read_only callback can verify
    a production rate/runtime contract without making a purchase. Both return
    a manifest-bound, independently observed receipt with evidence IDs.
    """
    fixture_id: str
    fixture_sha256: str
    run: Callable
    environments: tuple[str, ...] = ("staging",)
    mode: str = "staging_sandbox"

    def __post_init__(self):
        if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", self.fixture_id) or not _sha(self.fixture_sha256)
                or not callable(self.run) or self.mode not in {"staging_sandbox", "read_only"}
                or not self.environments or any(item not in {"production", "staging"} for item in self.environments)
                or self.mode == "staging_sandbox" and self.environments != ("staging",)):
            raise ValueError("trusted_storefront_fixture_invalid")


class CompanionStorefrontVerifier:
    def __init__(self, environments, companion=None, *, browser_runner=None, fixtures=None, playwright_factory=None, clock=time.time):
        self.environments, self.companion, self.clock = environments, companion, clock
        self.browser_runner = browser_runner or self._browser
        self.fixtures: Mapping[str, Mapping[str, TrustedStorefrontFixture]] = fixtures or {}
        self.playwright_factory = playwright_factory

    async def audit_snapshot(self, environment, product_id, snapshot):
        """Observe the current calculator without creating a repair or write.

        Complete stored rows are compared to the rendered table; displayed
        prices/artwork checks cover the explicitly recorded sample selections.
        The supplier-domain audit is separate and cannot be certified here.
        """
        manifest = {"schema": "hermes-read-only-product-audit/v1", "product_id": str(product_id),
                    "production_origin": self.environments.status("production").get("base_url"),
                    "staging_origin": self.environments.status("staging").get("base_url"),
                    "snapshot_revision": snapshot.get("revision"),
                    "before_data": {key: deepcopy(snapshot.get(key)) for key in
                        ("rows", "documents", "headers", "columns", "pricing_basis", "currency")},
                    "before_retail_rule": deepcopy(snapshot.get("retail_rule") or {}),
                    "before_artwork": deepcopy(snapshot.get("artwork") or {}), "changes": []}
        result = await self(environment, str(product_id), manifest, snapshot)
        result["scope"] = "read_only_product_snapshot_and_declared_rendered_samples"
        result["repair_created"] = False
        return result

    def _target(self, environment, product_id, manifest, snapshot):
        if environment not in {"production", "staging"} or not PRODUCT_ID.fullmatch(str(product_id)):
            raise ValueError("storefront_verifier_target_invalid")
        if str(manifest.get("product_id")) != str(product_id) or str(snapshot.get("product_id")) != str(product_id):
            raise ValueError("storefront_verifier_product_identity_mismatch")
        if not isinstance(snapshot.get("revision"), str) or not snapshot["revision"]:
            raise ValueError("storefront_verifier_revision_missing")
        status = self.environments.status(environment)
        base = status.get("base_url")
        if not status.get("connected") or base != manifest.get(environment + "_origin"):
            raise ValueError("storefront_verifier_environment_changed")
        url = snapshot.get("storefront_url")
        parsed = urlsplit(url or "")
        if (not allowed_read_request("GET", url or "", base) or parsed.query or parsed.fragment
                or parsed.path in {"", "/"}):
            raise ValueError("storefront_verifier_permalink_invalid")
        return base, url

    async def __call__(self, environment, product_id, manifest, snapshot, *, rollback=False):
        checks = {key: "unknown" for key in ALL_CHECKS}
        result = {"schema": "hermes-storefront-verification/v1", "status": "unknown", "manifest_sha256": digest(manifest),
                  "environment": environment, "product_id": str(product_id), "rollback": rollback,
                  "checks": checks, "reasons": {}, "evidence": {}, "observed_at": self.clock(),
                  "scope": "typed_manifest_recheck_not_whole_product_source_finalization"}
        try:
            base, url = self._target(environment, product_id, manifest, snapshot)
            expected = expected_data(manifest, rollback=rollback)
            if (row_signature(expected["rows"]) != row_signature(snapshot.get("rows"))
                    or sorted(expected["documents"], key=lambda row: row["url"]) != sorted(snapshot.get("documents", []), key=lambda row: row["url"])):
                checks["calculator_data"] = "mismatch"
                result["reasons"]["calculator_data"] = "connector_snapshot_differs_from_manifest"
                return self._finish(result)
            if snapshot.get("calculator", {}).get("family") != "custom_csv":
                result["reasons"]["calculator_data"] = "storefront_calculator_adapter_missing"
                return self._finish(result)
            observed = await asyncio.wait_for(self.browser_runner(environment, str(product_id), manifest, snapshot, expected=expected, rollback=rollback), timeout=180)
            if not isinstance(observed, dict) or observed.get("url") != url or str(observed.get("product_id")) != str(product_id):
                raise ValueError("storefront_browser_identity_mismatch")
            if observed.get("status") == "unknown":
                reason = observed.get("reason")
                result["reasons"]["browser"] = reason if isinstance(reason, str) and re.fullmatch(r"storefront_[a-z0-9_]{1,80}", reason) else "storefront_browser_unavailable"
                return self._finish(result)
            observed_at = observed.get("observed_at")
            if type(observed_at) not in (int, float) or not 0 <= self.clock() - observed_at <= 300:
                raise ValueError("storefront_browser_observation_not_fresh")
            if (not _sha(observed.get("html_sha256")) or row_signature(observed.get("rows")) != row_signature(expected["rows"])):
                checks["calculator_data"] = "mismatch"
                result["reasons"]["calculator_data"] = "rendered_calculator_table_differs_from_manifest"
            else:
                checks["calculator_data"] = "match"
            links, connector_docs = observed.get("document_links"), observed.get("connector_documents")
            if isinstance(links, list) and isinstance(connector_docs, list):
                checks["documents"] = "match" if (all(item["url"] in links for item in expected["documents"])
                    and sorted(connector_docs, key=lambda item: item["url"]) == sorted(expected["documents"], key=lambda item: item["url"])) else "mismatch"
                if checks["documents"] != "match":
                    result["reasons"]["documents"] = "audited_document_link_missing_from_page"
            before_rule = manifest.get("before_retail_rule") or {}
            after_rule = snapshot.get("retail_rule") or {}
            expected_script = manifest.get("before_retail_script_sha256") or before_rule.get("script_sha256")
            browser_rule = observed.get("retail_rule") or {}
            server_same = _sha(before_rule.get("fingerprint")) and before_rule.get("fingerprint") == after_rule.get("fingerprint")
            if server_same and _sha(expected_script) and browser_rule.get("script_sha256") == expected_script:
                checks["retail_rule_unchanged"] = "match"
            elif (_sha(before_rule.get("fingerprint")) and before_rule.get("fingerprint") != after_rule.get("fingerprint")
                    or _sha(expected_script) and browser_rule.get("script_sha256") and expected_script != browser_rule["script_sha256"]):
                checks["retail_rule_unchanged"] = "mismatch"
            else:
                result["reasons"]["retail_rule_unchanged"] = "bound_retail_code_fingerprint_missing"
            if checks["retail_rule_unchanged"] == "match" and browser_rule.get("status") == "verified" and type(browser_rule.get("member")) is bool:
                declared = sample_rows(manifest, expected, rollback=rollback)
                samples = observed.get("samples")
                if isinstance(samples, list) and len(samples) == len(declared):
                    price_states, artwork_states = [], []
                    for row, sample in zip(declared, samples):
                        retained = sample.get("retained_selection")
                        if (not isinstance(retained, dict) or _selection(retained) != _selection(row["selection"])
                                or sample.get("retained_production") != row.get("production")):
                            price_states.append("mismatch"); artwork_states.append("unknown")
                            continue
                        retail, _ = retail_total(row["cost"], browser_rule["member"])
                        displayed = sample.get("displayed_retail")
                        price_states.append("match" if isinstance(displayed, str) and _money(displayed) == Decimal(retail) else "mismatch")
                        state, reason = _check_artwork(row["selection"].get("size"), sample, manifest.get("before_artwork"))
                        artwork_states.append(state)
                        if state != "match":
                            result["reasons"]["artwork_dimensions"] = reason
                    checks["rendered_prices"] = self._states(price_states)
                    checks["artwork_dimensions"] = self._states(artwork_states)
                else:
                    result["reasons"]["rendered_prices"] = "declared_rendered_samples_incomplete"
            else:
                result["reasons"]["rendered_prices"] = "effective_loaded_retail_rule_unverified"
            result["evidence"]["browser"] = {
                "html_sha256": observed.get("html_sha256"), "observed_at": self.clock(),
                "retail_rule": {key: browser_rule[key] for key in ("status", "kind", "script_sha256", "member", "reason") if key in browser_rule},
                "samples": [{key: sample[key] for key in ("retained_selection", "retained_production", "displayed_retail", "artwork_dimensions") if key in sample}
                            for sample in observed.get("samples", []) if isinstance(sample, dict)],
                "request_guards": {key: value for key, value in (observed.get("request_guards") or {}).items()
                                   if key in {"blocked_requests", "mutating_requests", "supplier_requests", "cart_actions"} and type(value) is int},
            }
            context = {"environment": environment, "product_id": str(product_id), "url": url, "origin": base,
                       "manifest_sha256": result["manifest_sha256"], "snapshot_revision": snapshot.get("revision"), "rollback": rollback}
            for kind in ("shipping", "customer_flow"):
                check = await self._fixture(kind, context)
                checks[kind] = check["status"]
                result["evidence"][kind] = check
                if check["status"] != "match":
                    result["reasons"][kind] = check["reason"]
            latest = await self.environments.snapshot(environment, product_id)
            self._target(environment, product_id, manifest, latest)
            if (latest.get("revision") != snapshot["revision"]
                    or row_signature(latest.get("rows")) != row_signature(snapshot["rows"])
                    or latest.get("documents") != snapshot.get("documents")
                    or latest.get("retail_rule") != snapshot.get("retail_rule")
                    or latest.get("artwork") != snapshot.get("artwork")):
                checks["calculator_data"] = "mismatch"
                result["reasons"]["calculator_data"] = "storefront_snapshot_changed_during_recheck"
        except (ValueError, KeyError, TypeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"storefront_[a-z0-9_]{1,80}", str(exc)) else "storefront_verifier_evidence_invalid"
            result["reasons"]["verifier"] = reason
            if checks["calculator_data"] != "mismatch":
                checks["calculator_data"] = "unknown"
        except Exception:
            # Never include raw pages, browser credentials, remote bodies or
            # exception text from registered fixtures in public evidence.
            result["reasons"]["verifier"] = "storefront_verifier_unavailable"
            if checks["calculator_data"] != "mismatch":
                checks["calculator_data"] = "unknown"
        return self._finish(result)

    @staticmethod
    def _states(values):
        return "mismatch" if "mismatch" in values else "match" if values and all(value == "match" for value in values) else "unknown"

    def _finish(self, result):
        result["status"] = self._states(list(result["checks"].values()))
        result["release_ready"] = all(result["checks"][key] == "match" for key in ALL_CHECKS)
        result["release_hold_checks"] = [key for key in ALL_CHECKS if result["checks"][key] != "match"]
        return result

    async def _fixture(self, kind, context):
        fixture = self.fixtures.get(context["product_id"], {}).get(kind)
        unknown = {"status": "unknown", "reason": kind + "_trusted_fixture_required"}
        if not isinstance(fixture, TrustedStorefrontFixture):
            return unknown
        if context["environment"] not in fixture.environments:
            return {"status": "unknown", "reason": kind + "_fixture_not_allowed_in_environment"}
        try:
            value = fixture.run(deepcopy(context))
            value = await asyncio.wait_for(value, timeout=90) if inspect.isawaitable(value) else value
            if (not isinstance(value, dict) or value.get("status") not in {"match", "mismatch", "unknown"}
                    or value.get("manifest_sha256") != context["manifest_sha256"]
                    or value.get("product_id") != context["product_id"] or value.get("environment") != context["environment"]
                    or value.get("fixture_sha256") != fixture.fixture_sha256
                    or value.get("snapshot_revision") != context["snapshot_revision"]
                    or not isinstance(value.get("evidence_ids"), list) or not value["evidence_ids"]
                    or any(not isinstance(item, str) or not item for item in value["evidence_ids"])):
                return {"status": "unknown", "reason": kind + "_fixture_receipt_unbound"}
            observed_at = value.get("observed_at")
            if type(observed_at) not in (int, float) or not 0 <= self.clock() - observed_at <= 300:
                return {"status": "unknown", "reason": kind + "_fixture_receipt_not_fresh"}
            if any(type(value.get(key)) is not int or value[key] != 0 for key in ("orders_placed", "payments_submitted", "supplier_orders_placed")):
                return {"status": "mismatch", "reason": kind + "_fixture_prohibited_side_effect"}
            if fixture.mode == "read_only" and any(type(value.get(key)) is not int or value[key] != 0 for key in ("cart_actions", "uploads", "wordpress_writes")):
                return {"status": "mismatch", "reason": kind + "_fixture_prohibited_side_effect"}
            if fixture.mode == "staging_sandbox" and (kind == "customer_flow" or value.get("cart_actions", 0)) and value.get("cart_restored") is not True:
                return {"status": "unknown", "reason": "customer_flow_sandbox_cleanup_unverified"}
            return {"status": value["status"], "reason": kind + "_declared_fixture_only", "fixture_id": fixture.fixture_id,
                    "fixture_sha256": fixture.fixture_sha256, "evidence_ids": value["evidence_ids"],
                    "manifest_sha256": value["manifest_sha256"], "observed_at": observed_at, "mode": fixture.mode}
        except Exception:
            return {"status": "unknown", "reason": kind + "_fixture_unavailable"}

    async def _browser(self, environment, product_id, manifest, snapshot, *, expected, rollback):
        from .adapters.browser import browser_slot
        async def check():
            return None
        base = self.environments.status(environment).get("base_url")
        async with browser_slot(check, source_origin=base, per_source_limit=1):
            return await self._read_browser(environment, product_id, manifest, snapshot,
                                            expected=expected, rollback=rollback)

    async def _read_browser(self, environment, product_id, manifest, snapshot, *, expected, rollback):
        """Actual anonymous/read-only renderer, with normal staging Basic auth."""
        base, url = self._target(environment, product_id, manifest, snapshot)
        cases = sample_rows(manifest, expected, rollback=rollback)
        factory = self.playwright_factory
        if factory is None:
            from playwright.async_api import async_playwright
            factory = async_playwright
        guards = {"blocked_requests": 0, "mutating_requests": 0, "supplier_requests": 0, "cart_actions": 0}
        result = {"status": "observed", "url": url, "product_id": product_id, "samples": [], "request_guards": guards, "observed_at": self.clock()}

        async def route_request(route):
            request = route.request
            if allowed_read_request(request.method, request.url, base):
                await route.continue_()
            else:
                guards["blocked_requests"] += 1
                guards["mutating_requests"] += request.method not in {"GET", "HEAD"}
                await route.abort()

        async with factory() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            try:
                context = await browser.new_context(service_workers="block", ignore_https_errors=False, **self.environments._browser_options(environment))
                await context.route("**/*", route_request)
                page = await context.new_page()
                for index, row in enumerate(cases):
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if response is None or response.status != 200 or page.url != url:
                        raise ValueError("storefront_renderer_unavailable_or_login_required")
                    page_identity = await page.evaluate("""() => ({ids:Array.from(document.querySelectorAll('[id^="product-"]')).map(e=>e.id)})""")
                    if "product-" + product_id not in page_identity.get("ids", []):
                        raise ValueError("storefront_rendered_product_identity_mismatch")
                    if index == 0:
                        html = await page.content()
                        if len(html.encode()) > 12 * 1024 * 1024:
                            raise ValueError("storefront_page_size_exceeded")
                        soup = BeautifulSoup(html, "html.parser")
                        scripts = [node.string or node.get_text() for node in soup.select("script:not([src])")]
                        if str(_declaration(scripts, "current_product_id_is_this")) != product_id:
                            raise ValueError("storefront_inline_product_identity_mismatch")
                        indexed, _ = _index(_declaration(scripts, "csvdata"), site=True)
                        result.update(html_sha256=hashlib.sha256(html.encode()).hexdigest(), rows=list(indexed.values()), retail_rule=_retail_rule(scripts, product_id))
                        # Document metadata belongs to this connector section.
                        # Comparing its exact visible set also detects a stale
                        # page retaining an added template after rollback.
                        result.update(await page.evaluate(r"""() => {const visible=n=>{for(let e=n;e;e=e.parentElement){const s=getComputedStyle(e);if(e.hidden||s.display==='none'||s.visibility==='hidden')return false;}return !!n.getClientRects().length;};return {document_links:Array.from(document.querySelectorAll('a[href]')).filter(visible).map(e=>e.href),connector_documents:Array.from(document.querySelectorAll('.hermes-artwork-documents a[href]')).filter(visible).map(e=>({url:e.href,label:(e.textContent||'').trim()}))};}"""))
                        if (any(set(item["selection"]) != SUPPORTED_LEGACY_FIELDS or not item.get("production") for item in cases)
                                or result["retail_rule"].get("status") != "verified"):
                            result["reason"] = "storefront_rendered_adapter_or_effective_rule_unverified"
                            return result
                    await page.wait_for_selector('#product_calculator_display select[name="print_option_size"]', state="attached", timeout=10000)
                    for field, control_name in CONTROL_NAMES.items():
                        if field == "quantity":
                            continue
                        await self._select(page, control_name, row["selection"][field])
                    if not row.get("production"):
                        raise ValueError("storefront_rendered_production_selection_missing")
                    await self._select(page, "turnaround", row["production"])
                    await self._select(page, "quantity", row["selection"]["quantity"])
                    wanted = {CONTROL_NAMES[key]: value for key, value in row["selection"].items()}
                    wanted["turnaround"] = row["production"]
                    await page.wait_for_function(r"""wanted => Object.entries(wanted).every(([key,value])=>document.querySelector('select[name="print_option_'+key+'"]:not([data-pit-du-proxy])')?.value===String(value)) && /\$\s*[\d,]+\.\d{2}/.test(document.querySelector('#price2')?.textContent||'')""", arg=wanted, timeout=7000)
                    sample = await page.evaluate(r"""() => {const read=key=>document.querySelector('select[name="print_option_'+key+'"]:not([data-pit-du-proxy])')?.value;const node=document.querySelector('#price2');const visible=n=>{for(let e=n;e;e=e.parentElement){const s=getComputedStyle(e);if(e.hidden||s.display==='none'||s.visibility==='hidden')return false;}return !!n?.getClientRects().length;};const m=(node?.textContent||'').match(/\$\s*([\d,]+\.\d{2})/);return {retained_selection:{size:read('size'),shape:read('shape'),stock:read('paper_stock'),colorspec:read('printing'),coating:read('coating'),quantity:read('quantity')},retained_production:read('turnaround'),displayed_retail:visible(node)&&m?m[1].replaceAll(',',''):null,artwork_dimensions:Array.from(document.querySelectorAll('[data-piad-dimension-bridge],select[data-pigf-proof-size],.pila-proof-fields select,.pirc-proof select,.pirc-finished select')).filter(e=>/x|×/i.test(e.value||'')).map(e=>({value:e.value,unit:e.getAttribute('data-unit')||null}))};}""")
                    result["samples"].append(sample)
            finally:
                await browser.close()
        return result

    @staticmethod
    async def _select(page, name, value):
        control = page.locator(f'select[name="print_option_{name}"]:not([data-pit-du-proxy])').first
        if await control.is_visible():
            await control.select_option(value, timeout=4000)
        else:
            await page.locator(f'select[data-pit-du-proxy][data-companion-proxy-for="print_option_{name}"]').first.select_option(value, timeout=4000)
