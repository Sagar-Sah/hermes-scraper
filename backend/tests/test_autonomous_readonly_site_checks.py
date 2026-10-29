"""Missing supplier mappings do not hide independent storefront problems."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from crawler.autonomous_product_pipeline import AutonomousProductPipeline, REQUIRED_CHECKS
from crawler.companion_storefront_verifier import CompanionStorefrontVerifier, expected_data
from test_companion_storefront_verifier import setup, Environments


CID = "a" * 32


@pytest.mark.asyncio
async def test_read_only_snapshot_audit_observes_real_renderer_contract_without_repair():
    manifest, snapshot, observed, _, _ = setup(rollback=True)
    snapshots = Environments(snapshot)
    calls = []
    async def browser(environment, product_id, supplied, current, **kwargs):
        assert supplied["schema"] == "hermes-read-only-product-audit/v1"
        assert supplied["changes"] == [] and kwargs["rollback"] is False
        assert expected_data(supplied) == manifest["before_data"]
        calls.append((environment, product_id))
        return deepcopy(observed)
    # The browser and connector script fingerprints must be the same trusted
    # code identity; this test observes the current table with no before edit.
    snapshot["retail_rule"]["script_sha256"] = "b" * 64
    snapshot["artwork"] = {"canonical_unit": "in"}
    verifier = CompanionStorefrontVerifier(snapshots, browser_runner=browser)
    result = await verifier.audit_snapshot("staging", "9532", snapshot)
    assert result["repair_created"] is False
    assert result["checks"]["calculator_data"] == "match"
    assert result["checks"]["rendered_prices"] == "match"
    assert result["checks"]["artwork_dimensions"] == "match"
    assert result["checks"]["customer_flow"] == "unknown"
    assert result["release_ready"] is False
    assert calls == [("staging", "9532")]


def test_readonly_manifest_rejects_a_disguised_edit():
    manifest, *_ = setup()
    manifest["schema"] = "hermes-read-only-product-audit/v1"
    with pytest.raises(ValueError, match="read_only_audit_cannot_change"):
        expected_data(manifest)


class Site:
    def status(self, environment):
        return {"connected": True}
    async def snapshot(self, environment, product_id):
        return {"product_id": product_id, "revision": "r1", "shipping": {"model_id": "fixture"}}


@pytest.mark.asyncio
async def test_unmapped_product_still_checks_routing_and_customer_flow(tmp_path):
    calls = []
    async def shipping(cycle, product, snapshot):
        calls.append("shipping")
        return {"status": "mismatch", "reason": "shipping_model_drift"}
    async def audit_snapshot(environment, product_id, snapshot):
        calls.append("site")
        return {"checks": {"rendered_prices": "match", "artwork_dimensions": "match",
                           "retail_rule_unchanged": "match", "customer_flow": "unknown"}}
    companion = SimpleNamespace(pairs={}, campaigns=SimpleNamespace(list=lambda: []))
    pipeline = AutonomousProductPipeline(SimpleNamespace(root=tmp_path), companion, Site(), None, None,
                    shipping=shipping, verifier=SimpleNamespace(audit_snapshot=audit_snapshot))
    result = await pipeline({"id": CID}, {"product_id": "9532", "supplier": None, "source_url": None})
    assert calls == ["shipping", "site"]
    assert result["checks"]["source_mapping"] == "unknown"
    assert result["checks"]["shipping"] == "mismatch"
    assert result["checks"]["rendered_prices"] == "match"
    assert result["status"] == "mismatch" and result["evidence_complete"] is False


@pytest.mark.asyncio
async def test_connector_signout_is_not_reported_as_supplier_signout(tmp_path):
    class SignedOut(Site):
        async def snapshot(self, *args):
            raise ValueError("companion_connector_http_401")
    companion = SimpleNamespace(pairs={}, campaigns=SimpleNamespace(list=lambda: []))
    pipeline = AutonomousProductPipeline(SimpleNamespace(root=tmp_path), companion, SignedOut(), None, None)
    result = await pipeline({"id": CID}, {"product_id": "9532", "supplier": None, "source_url": None})
    assert result["status"] == "needs_attention"
    assert pipeline.record(CID, "9532")["storefront_reason"] == "companion_connector_http_401"
    assert all(result["checks"][key] == "unknown" for key in REQUIRED_CHECKS)
