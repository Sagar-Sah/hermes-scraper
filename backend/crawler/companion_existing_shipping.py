"""Register existing shipping models with private, current-cycle quote evidence.

This adapter performs no network requests and never fits a model. Trusted quote
fixtures may persist their request/retained-response envelopes beneath this
private root. Historical reports and files carrying only a claimed pass flag are
not inputs. There is deliberately no REST endpoint for registering these files.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from .companion_environments import digest, private_save


STATES = {"AZ", "FL", "NY"}
TIERS = {"metropolitan", "suburban", "rural"}
SOURCE_PRODUCERS = {
    "4over": "hermes-fourover-ground/v1",
    "b2sign": "hermes-b2sign-ground/v1",
    "dps": "hermes-dps-ground/v1",
    "wsdisplay": "hermes-wsdisplay-ground/v1",
}
SITE_PRODUCER = "hermes-staging-rate-fixture/v1"
PROFILE = "existing_shipping_evidence_v1"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}")
SHA = re.compile(r"[a-f0-9]{64}")
CONTEXT_KEYS = {"product_id", "source_product_id", "configuration", "fulfillment_id", "destination", "service", "currency"}


def _sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _money(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,8})\.\d{2}", value):
        raise ValueError("shipping_exact_money_required")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError("shipping_exact_money_required") from None


class ExistingShippingEvidence:
    """Bridge a trusted local fixture archive into MonthlyShippingModels.

    ``register(monthly, contract_path)`` is a deployment-time operation. The
    manifest pins an actual model artifact, approved destination fixtures and
    predeclared independent holdouts. ``__call__`` reads only this cycle's paired
    receipts. Unknown evidence yields no observations and an actionable local
    ``last_result``; it does not trigger supplier requests or reuse old quotes.
    """
    def __init__(self, root, *, now=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("unsafe_shipping_evidence_storage")
        self.root = self.root.resolve()
        os.chmod(self.root, 0o700)
        self.now = now
        self.contracts = {}
        self._contract_paths = {}
        self.last_result = {}

    def _read(self, relative, expected_sha=None, *, json_document=True):
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError("shipping_evidence_path_invalid")
        path = self.root / relative
        if ".." in Path(relative).parts or any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(self.root)):
            raise ValueError("shipping_evidence_path_invalid")
        if not path.is_file() or not path.resolve().is_relative_to(self.root):
            raise ValueError("shipping_evidence_missing")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("shipping_evidence_too_large")
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if expected_sha is not None and (not _sha(expected_sha) or actual != expected_sha):
            raise ValueError("shipping_evidence_hash_changed")
        if not json_document:
            return raw, actual
        try:
            value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("shipping_evidence_json_invalid") from None
        if not isinstance(value, dict):
            raise ValueError("shipping_evidence_json_invalid")
        return value, actual

    def _artifact(self, reference, *, json_document=True):
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise ValueError("shipping_artifact_reference_required")
        return self._read(reference["path"], reference["sha256"], json_document=json_document)

    @staticmethod
    def _context(value):
        if not isinstance(value, dict) or set(value) != CONTEXT_KEYS:
            raise ValueError("shipping_comparable_context_required")
        dest = value["destination"]
        if (not isinstance(dest, dict) or set(dest) != {"country", "state", "postcode", "tier"}
                or dest["country"] != "US" or dest["state"] not in STATES or dest["tier"] not in TIERS
                or not isinstance(dest["postcode"], str) or not re.fullmatch(r"\d{5}", dest["postcode"])):
            raise ValueError("shipping_approved_destination_required")
        if (value["currency"] != "USD" or value["service"] != "Ground"
                or not isinstance(value["configuration"], dict) or not value["configuration"]
                or not isinstance(value["fulfillment_id"], str) or not value["fulfillment_id"]
                or not isinstance(value["source_product_id"], str) or not value["source_product_id"]
                or not str(value["product_id"]).isdigit()):
            raise ValueError("shipping_comparable_context_required")
        # No customer records or address lines are needed for these fixtures.
        encoded = json.dumps(value["configuration"], sort_keys=True, allow_nan=False)
        if re.search(r'"(?:email|phone|street|address|password|token|secret|customer)"\s*:', encoded, re.I):
            raise ValueError("shipping_fixture_contains_private_fields")
        return digest(value)

    def _load_contract(self, relative):
        value, contract_sha = self._read(relative)
        model = value.get("model")
        if (value.get("schema") != "hermes-existing-shipping-contract/v1" or not isinstance(model, dict)
                or not isinstance(value.get("model_id"), str) or not IDENTIFIER.fullmatch(value["model_id"])
                or model.get("policy") not in {"estimated", "vendor_free"}
                or value.get("supplier") not in SOURCE_PRODUCERS
                or not _number(model.get("frozen_at")) or model["frozen_at"] > self.now()
                or not isinstance(model.get("product_ids"), list) or not model["product_ids"]
                or any(not str(x).isdigit() for x in model["product_ids"])
                or not _number(model.get("max_quote_age_hours", 24 * 31))
                or not 0 < model.get("max_quote_age_hours", 24 * 31) <= 24 * 31
                or not isinstance(model.get("scope"), dict) or not model["scope"]):
            raise ValueError("shipping_model_contract_invalid")
        _, model_sha = self._artifact(value.get("model_artifact"), json_document=False)
        if model.get("model_sha256") != model_sha:
            raise ValueError("shipping_model_artifact_mismatch")
        approved, approved_sha = self._artifact(value.get("approved_fixtures"))
        fixtures = value.get("fixtures")
        if (approved.get("schema") != "hermes-approved-shipping-fixtures/v1" or approved.get("approved_by") != "human"
                or not _number(approved.get("approved_at")) or approved["approved_at"] > model["frozen_at"]
                or approved.get("fixtures") != fixtures or not isinstance(fixtures, list) or len(fixtures) < 9):
            raise ValueError("shipping_approved_fixtures_required")
        declared = model.get("predeclared_holdout_ids")
        training = model.get("training_observation_ids")
        if (not isinstance(declared, list) or not declared or not isinstance(training, list)
                or any(not isinstance(k, str) or not IDENTIFIER.fullmatch(k) for k in [*declared, *training])
                or len(set(declared)) != len(declared) or set(training).intersection(declared)):
            raise ValueError("shipping_independent_holdout_contract_required")
        actual_contexts, regions, configurations = {}, set(), set()
        for fixture in fixtures:
            if (not isinstance(fixture, dict) or set(fixture) != {"id", "context"}
                    or fixture["id"] in actual_contexts or fixture["id"] not in declared):
                raise ValueError("shipping_fixture_contract_invalid")
            context = fixture["context"]
            actual_contexts[fixture["id"]] = self._context(context)
            if str(context["product_id"]) not in {str(x) for x in model["product_ids"]}:
                raise ValueError("shipping_fixture_product_outside_model_scope")
            regions.add((context["destination"]["state"], context["destination"]["tier"]))
            configurations.add(digest(context["configuration"]))
        if (regions != {(state, tier) for state in STATES for tier in TIERS} or len(configurations) < 2
                or set(actual_contexts) != set(declared) or actual_contexts != model.get("holdout_contexts_sha256")):
            raise ValueError("shipping_nine_region_and_representative_holdouts_required")
        result = deepcopy(value)
        result.update(contract_sha256=contract_sha, approved_fixtures_sha256=approved_sha)
        if model["policy"] == "vendor_free":
            self._free_policy(result)
        return result

    def _free_policy(self, contract):
        policy, sha = self._artifact(contract.get("free_policy_artifact"))
        model = contract["model"]
        evidence = model.get("free_shipping_evidence") or {}
        products = {str(x) for x in model["product_ids"]}
        if (policy.get("schema") != "hermes-verified-vendor-free-policy/v1"
                or policy.get("supplier") != contract["supplier"] or policy.get("service") != "Ground"
                or policy.get("approved_merchant_site") != "https://printitprints.com"
                or policy.get("membership_active") is not True or policy.get("retail_site_approved_by_vendor") is not True
                or not isinstance(policy.get("eligible_products"), list)
                or not products.issubset({str(x) for x in policy["eligible_products"]})
                or not _number(policy.get("valid_until")) or policy["valid_until"] < self.now()
                or policy.get("scope") != model["scope"] or evidence.get("sha256") != sha
                or evidence.get("valid_until") != policy["valid_until"]
                or any(evidence.get(k) is not True for k in ("vendor_policy_verified", "retail_site_approved", "eligibility_verified"))):
            raise ValueError("shipping_vendor_free_policy_unverified")
        # The trusted policy verifier pins actual vendor terms, eligibility and
        # merchant approval documents. A merchant's own zero-dollar rate alone
        # does not establish a free-shipping entitlement.
        for key in ("vendor_terms", "eligibility", "merchant_approval"):
            self._artifact(policy.get(key), json_document=False)

    def register(self, monthly, relative_contract):
        contract = self._load_contract(relative_contract)
        identifier = contract["model_id"]
        if identifier in self.contracts and self.contracts[identifier]["contract_sha256"] != contract["contract_sha256"]:
            raise ValueError("shipping_model_requires_explicit_reregistration")
        if PROFILE in monthly.collectors and monthly.collectors[PROFILE] is not self:
            raise ValueError("shipping_collector_profile_already_registered")
        self.contracts[identifier] = contract
        self._contract_paths[identifier] = relative_contract
        manifest = deepcopy(contract["model"])
        manifest.update(collector_profile=PROFILE, evidence_contract_sha256=contract["contract_sha256"])
        private_save(monthly.root / (identifier + ".json"), manifest)
        monthly.collectors[PROFILE] = self
        return {"model_id": identifier, "status": "registered", "quote_collection": "current_cycle_receipts_required",
                "contract_sha256": contract["contract_sha256"], "live_requests_performed": 0}

    def register_available(self, monthly):
        """Reload explicitly installed private contracts after service restart.

        Missing or damaged configuration affects only its model. This does not
        discover a supplier or turn an old shipping report into a new contract.
        """
        folder = self.root / "contracts"
        if not folder.exists():
            return []
        if folder.is_symlink() or not folder.is_dir():
            return [{"status": "unknown", "reason": "shipping_registry_directory_invalid"}]
        results = []
        for path in sorted(folder.glob("*.json"))[:1000]:
            identifier = path.stem
            if not IDENTIFIER.fullmatch(identifier):
                continue
            try:
                result = self.register(monthly, str(path.relative_to(self.root)))
                results.append(result)
            except (ValueError, OSError, TypeError, KeyError):
                results.append({"model_id": identifier, "status": "unknown", "reason": "shipping_model_registration_invalid"})
        return results

    def _quote(self, reference, *, cycle, model, fixture, producer, source):
        value, sha = self._artifact(reference)
        context = fixture["context"]
        observed = value.get("observed_at")
        if (value.get("schema") != "hermes-bound-shipping-quote/v1" or value.get("producer") != producer
                or value.get("cycle_id") != cycle["id"] or value.get("fixture_id") != fixture["id"]
                or not _number(observed) or not cycle["created_at"] <= observed <= self.now()
                or self.now() - observed > min(24 * 31, model.get("max_quote_age_hours", 24 * 31)) * 3600
                or value.get("request") != context or value.get("retained") != context
                or value.get("http_status") != 200 or not isinstance(value.get("response"), dict)
                or value["response"].get("context") != context
                or value["response"].get("service") != context["service"]
                or value["response"].get("currency") != context["currency"]
                or value.get("side_effects") != {"orders": 0, "payments": 0, "customer_records": 0, "account_defaults": 0}
                or (not source and (value.get("environment") != "staging"
                                    or value.get("model_sha256") != model["model_sha256"]))):
            raise ValueError("shipping_quote_binding_or_freshness_invalid")
        amount = _money(value["response"].get("shipping_total"))
        # Source quotes must retain a native selection and response identity;
        # a test writer cannot promote a mere guessed price into an observation.
        binding = value.get("native_binding")
        if (not isinstance(binding, dict) or set(binding) != {"request_identity", "response_identity", "selected_service"}
                or not isinstance(binding["request_identity"], str) or not binding["request_identity"]
                or binding["request_identity"] != binding["response_identity"]
                or binding["selected_service"] != context["service"]):
            raise ValueError("shipping_native_response_binding_required")
        return amount, sha, observed

    async def __call__(self, cycle, manifest):
        identifier = next((k for k, value in self.contracts.items()
                           if value["contract_sha256"] == manifest.get("evidence_contract_sha256")), None)
        try:
            if (identifier is None or not isinstance(cycle, dict) or not isinstance(cycle.get("id"), str)
                    or not re.fullmatch(r"[a-f0-9]{32}", cycle["id"]) or not _number(cycle.get("created_at"))):
                raise ValueError("shipping_registered_contract_and_cycle_required")
            original = self.contracts[identifier]
            contract = self._load_contract(self._contract_paths[identifier])
            expected = deepcopy(contract["model"])
            expected.update(collector_profile=PROFILE, evidence_contract_sha256=contract["contract_sha256"])
            if manifest != expected or contract["contract_sha256"] != original["contract_sha256"]:
                raise ValueError("shipping_registered_model_changed")
            bundle, _ = self._read(f"cycles/{cycle['id']}/{identifier}.json")
            if (bundle.get("schema") != "hermes-shipping-evidence-bundle/v1" or bundle.get("cycle_id") != cycle["id"]
                    or bundle.get("model_id") != identifier or bundle.get("contract_sha256") != contract["contract_sha256"]
                    or not isinstance(bundle.get("pairs"), dict)
                    or set(bundle["pairs"]) != {f["id"] for f in contract["fixtures"]}):
                raise ValueError("shipping_current_cycle_pairs_required")
            observations, used_source = [], set()
            for fixture in contract["fixtures"]:
                pair = bundle["pairs"][fixture["id"]]
                if not isinstance(pair, dict) or set(pair) != {"source", "site"}:
                    raise ValueError("shipping_quote_pair_required")
                source, source_sha, at = self._quote(pair["source"], cycle=cycle, model=manifest, fixture=fixture,
                                                  producer=SOURCE_PRODUCERS[contract["supplier"]], source=True)
                site, site_sha, _ = self._quote(pair["site"], cycle=cycle, model=manifest, fixture=fixture,
                                               producer=SITE_PRODUCER, source=False)
                if source_sha in used_source:
                    raise ValueError("shipping_duplicate_source_observation")
                used_source.add(source_sha)
                context_sha = self._context(fixture["context"])
                observations.append({"id": fixture["id"], "cycle_id": cycle["id"], "scope": manifest["scope"],
                    "model_sha256": manifest["model_sha256"], "source_sha256": source_sha, "site_sha256": site_sha,
                    "source_observed_at": at, "source_context_sha256": context_sha, "site_context_sha256": context_sha,
                    "selection_retained": True, "destination_retained": True, "service_retained": True,
                    "source_total": f"{source:.2f}", "site_total": f"{site:.2f}"})
            self.last_result[identifier] = {"status": "evidence_loaded", "cycle_id": cycle["id"], "pairs": len(observations),
                                            "scope": "declared_holdouts_only", "live_requests_performed": 0}
            return observations
        except (ValueError, OSError, TypeError, KeyError):
            self.last_result[identifier or "unregistered"] = {"status": "unknown", "reason": "current_bound_shipping_evidence_required",
                                                             "live_requests_performed": 0}
            return []
