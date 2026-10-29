import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from crawler.product_companion_writes import ProductCompanionWrites


DOC = "https://4over.com/media/asset/templates/card.pdf"


class Bridge:
    def __init__(self):
        self.revision = "before"
        self.documents = []
        self.calls = []
        self.rows = [{"selection": {"size": "2x3.5", "quantity": "100"}, "cost": "20", "production": "4 Business Days"}]

    async def __call__(self, cfg, method, path, payload):
        self.calls.append((method, path))
        if path.startswith("/snapshot/"):
            return {"product_id": 9532, "revision": self.revision,
                    "rows": copy.deepcopy(self.rows), "documents": copy.deepcopy(self.documents)}
        if path == "/previews":
            assert payload["expected_revision"] == self.revision
            return {"preview_id": "b" * 32, "expires_at": time.time() + 900,
                    "diff": [{"kind": "add_document", "before": None, "after": {"url": DOC, "label": "card.pdf"}}]}
        if path.startswith("/previews/"):
            return {"status": "applied" if self.revision == "after" else "preview",
                    "change": {"change_id": "c" * 32, "after_revision": "after"} if self.revision == "after" else None}
        if path.startswith("/apply/"):
            if self.revision != "before":
                if self.revision == "after":
                    return {"change_id": "c" * 32, "after_revision": "after"}
                raise ValueError("remote_revision_conflict")
            self.revision = "after"
            self.documents = [{"url": DOC, "label": "card.pdf"}]
            return {"change_id": "c" * 32, "after_revision": "after"}
        if path.startswith("/rollback/"):
            if self.revision != "after":
                raise ValueError("remote_revision_conflict")
            self.revision = "before"
            self.documents = []
            return {"revision": "before", "after_revision": "after"}
        raise AssertionError(path)


def setup(tmp_path):
    pair = {"id": "woo-9532", "product_id": "9532", "latest_audit_id": "a" * 32,
            "storefront_url": "https://printitprints.com/shop/cards/"}
    audit = {"id": "a" * 32, "pair_id": pair["id"], "observed_at": time.time(),
             "evidence": {"supplier_page_sha256": "a" * 64},
             "supplier_content": {"documents": [DOC]},
             "report": {"proposed_changes": [{"kind": "add_missing_template_links", "urls": [DOC]}]}}
    companion = SimpleNamespace(root=Path(tmp_path), list_pairs=lambda: [pair],
        get_pair=lambda _: pair, get_audit=lambda _: audit)
    bridge = Bridge()
    writes = ProductCompanionWrites(companion, transport=bridge)
    async def site(_pair, _cfg, _nonce):
        return {"rows": {("2x3.5", "100"): copy.deepcopy(bridge.rows[0])},
                "rule": {"script_sha256": "d" * 64}, "html_sha256": "e" * 64,
                "content": {"documents": [doc["url"] for doc in bridge.documents]}}
    writes._site = site
    return writes, bridge, pair, audit


def test_reviewed_preview_apply_and_rollback_preserve_csv(tmp_path):
    async def scenario():
        writes, bridge, _, audit = setup(tmp_path)
        status = await writes.configure("https://printitprints.com", "secret-token-" * 6)
        assert status == {"configured": True, "connected": True, "base_url": "https://printitprints.com"}
        assert "secret-token" not in str(status)
        assert writes.config_path.stat().st_mode & 0o777 == 0o600
        preview = await writes.preview(audit["id"], [0])
        assert bridge.documents == [] and preview["status"] == "preview"
        applied = await writes.apply(preview["id"], "before")
        assert applied["status"] == "applied_verified" and applied["can_rollback"]
        assert (await writes.apply(preview["id"], "before"))["status"] == "applied_verified"
        rolled_back = await writes.rollback(preview["id"])
        assert rolled_back["status"] == "rollback_verified" and not rolled_back["can_rollback"]
        assert bridge.revision == "before" and bridge.documents == []
    asyncio.run(scenario())


def test_failed_storefront_recheck_automatically_restores_backup(tmp_path):
    async def scenario():
        writes, bridge, _, audit = setup(tmp_path)
        await writes.configure("https://printitprints.com", "secret-token-" * 6)
        preview = await writes.preview(audit["id"], [0])
        original = writes._site
        async def stale_site(pair, cfg, nonce):
            value = await original(pair, cfg, nonce)
            value["content"]["documents"] = []
            return value
        writes._site = stale_site
        result = await writes.apply(preview["id"], "before")
        assert result["status"] == "rolled_back_after_failed_recheck"
        assert bridge.revision == "before" and bridge.documents == []
    asyncio.run(scenario())


def test_revision_conflict_prevents_overwrite_and_rollback_drift(tmp_path):
    async def scenario():
        writes, bridge, _, audit = setup(tmp_path)
        await writes.configure("https://printitprints.com", "secret-token-" * 6)
        preview = await writes.preview(audit["id"], [0])
        bridge.revision = "merchant-edit"
        with pytest.raises(ValueError, match="remote_revision_conflict"):
            await writes.apply(preview["id"], "before")
        assert bridge.revision == "merchant-edit" and bridge.documents == []
    asyncio.run(scenario())


def test_unknown_cost_domain_and_copy_proposals_cannot_write(tmp_path):
    writes, bridge, pair, audit = setup(tmp_path)
    audit["report"]["proposed_changes"] = [{"kind": "review_stored_supplier_costs"}]
    writes._source = lambda *_: (_ for _ in ()).throw(ValueError("fresh_complete_supplier_tables_required_for_cost_write"))
    with pytest.raises(ValueError, match="fresh_complete_supplier_tables_required"):
        writes._operations(audit, pair, [0])
    audit["report"]["proposed_changes"] = [{"kind": "hide_empty_faq_section"}]
    with pytest.raises(ValueError, match="manual_content_review"):
        writes._operations(audit, pair, [0])
    assert bridge.calls == []


def test_connector_rejects_unapproved_origin_and_unverified_document(tmp_path):
    async def scenario():
        writes, bridge, pair, audit = setup(tmp_path)
        with pytest.raises(ValueError, match="approved_https"):
            await writes.configure("https://example.com", "secret-token-" * 6)
        audit["supplier_content"]["documents"] = []
        with pytest.raises(ValueError, match="document_evidence_missing"):
            writes._operations(audit, pair, [0])
        assert not writes.config_path.exists() and bridge.calls == []
    asyncio.run(scenario())


def test_protected_staging_token_is_private_but_never_claims_connection(tmp_path):
    async def scenario():
        writes, _, _, audit = setup(tmp_path)
        async def protected(*_):
            raise ValueError("woocommerce_connector_request_rejected_401")
        writes.transport = protected
        status = await writes.configure("https://staging3.tonyl36.sg-host.com", "secret-token-" * 6)
        assert status["configured"] and not status["connected"]
        assert "preview_authentication_required" in status["reason"]
        assert "secret-token" not in str(status)
        with pytest.raises(ValueError, match="connection_not_verified"):
            await writes.preview(audit["id"], [0])
    asyncio.run(scenario())


def test_lost_apply_response_recovers_receipt_after_preview_expires(tmp_path):
    async def scenario():
        writes, bridge, _, audit = setup(tmp_path)
        await writes.configure("https://printitprints.com", "secret-token-" * 6)
        preview = await writes.preview(audit["id"], [0])
        original = writes.transport
        async def lost_response(cfg, method, path, payload):
            result = await original(cfg, method, path, payload)
            if path.startswith("/apply/"):
                raise ValueError("lost_response")
            return result
        writes.transport = lost_response
        with pytest.raises(ValueError, match="lost_response"):
            await writes.apply(preview["id"], "before")
        record = writes.get(preview["id"])
        assert record["status"] == "applying" and record["can_resume"]
        record["expires_at"] = time.time() - 1
        audit["observed_at"] = time.time() - 30000
        writes._save(writes.root / (record["id"] + ".json"), record)
        writes.transport = original
        recovered = await writes.apply(preview["id"], "before")
        assert recovered["status"] == "applied_verified"
        assert sum(path.startswith("/apply/") for _, path in bridge.calls) == 1
    asyncio.run(scenario())


def test_recheck_rejects_unrelated_row_drift(tmp_path):
    async def scenario():
        writes, bridge, _, audit = setup(tmp_path)
        await writes.configure("https://printitprints.com", "secret-token-" * 6)
        preview = await writes.preview(audit["id"], [0])
        await bridge({}, "POST", "/apply/" + "b" * 32, {"expected_revision": "before"})
        bridge.rows[0]["cost"] = "999"
        with pytest.raises(ValueError, match="rows_do_not_match_expected"):
            await writes._recheck(preview, {})
    asyncio.run(scenario())


def test_remote_rollback_is_not_repeated_when_only_public_recheck_failed(tmp_path):
    async def scenario():
        writes, bridge, _, audit = setup(tmp_path)
        await writes.configure("https://printitprints.com", "secret-token-" * 6)
        preview = await writes.preview(audit["id"], [0])
        await writes.apply(preview["id"], "before")
        original = writes._site
        async def unavailable(*_):
            raise ValueError("temporary_storefront_failure")
        writes._site = unavailable
        with pytest.raises(ValueError, match="temporary_storefront_failure"):
            await writes.rollback(preview["id"])
        record = writes.get(preview["id"])
        assert record["status"] == "rollback_recheck_pending" and not record["can_rollback"]
        writes._site = original
        result = await writes.rollback(preview["id"])
        assert result["status"] == "rollback_verified" and result["rollback_revision"] == "before"
        assert sum(path.startswith("/rollback/") for _, path in bridge.calls) == 1
    asyncio.run(scenario())


def test_cost_fix_uses_source_cost_and_checks_rendered_customer_price(tmp_path):
    async def scenario():
        writes, bridge, pair, audit = setup(tmp_path)
        selection = bridge.rows[0]["selection"]
        source_row = {"selection": selection, "cost": "21.25"}
        writes._source = lambda *_: {"fields": ("size", "quantity"), "rows": {("2x3.5", "100"): source_row}}
        audit["report"] = {"checks": {key: "match" for key in
            ("current_supplier_structure", "conditional_supplier_options", "current_supplier_quantity_domain")},
            "proposed_changes": [{"kind": "review_stored_supplier_costs"}],
            "cost_mismatches": [{"selection": selection, "supplier_cost": "21.25", "site_cost": "20"}]}
        operations = writes._operations(audit, pair, [0])
        assert operations == [{"kind": "set_cost", "selection": selection, "cost": "21.25"}]
        record = {"pair_id": pair["id"], "product_id": "9532", "before_rows": copy.deepcopy(bridge.rows),
                  "before_documents": [], "operations": operations, "before_retail_script_sha256": "d" * 64}
        bridge.rows[0]["cost"] = "21.25"
        async def rendered(_pair, site):
            assert next(iter(site["rows"].values()))["cost"] == "21.25"
            return {"status": "match", "samples": [{"site_cost": "21.25", "displayed_retail": "63.75"}]}
        writes.companion._rendered_retail_samples = rendered
        result = await writes._recheck(record, {"base_url": "https://printitprints.com"})
        assert result["rendered_price_samples"]["samples"][0]["displayed_retail"] == "63.75"
    asyncio.run(scenario())
