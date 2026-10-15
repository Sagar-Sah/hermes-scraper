"""Inventory assembly from durable evidence only; no HTTP or supplier access."""

from copy import deepcopy
import gzip
import json
from urllib.parse import urlencode

import pytest

from crawler.config import Settings
from crawler.route_dependencies import request_key
from crawler.source_domains import DomainJob, DomainTask
from crawler.store import Product, Response, ResponseObservation, Run, Store, digest
from crawler.variation_inventory import API, load_inventories, missing_table_manifest


CONTEXT = {
    "origin": "https://www.printograph.com", "session_scope": "inventory-fixture",
    "source_epoch": "epoch-one", "store_type": "broker", "currency": "USD",
    "service_signature": "instant-upload-fixture",
}
SELECTORS = {"productType": "1", "size": "101", "orientation": "1", "paper": "1", "color": "2"}
LOOKUP_PARAMS = {**SELECTORS, "turnaround": "1"}
SPEC_PARAMS = {"productType": "1", "requestValues": "true"}
SPEC = {
    "sizes": [{"id": 101, "label": '3.5" × 2"'}, {"id": 102, "label": '2" × 2"'}],
    "papers": [{"id": 1, "label": "14 pt. stock"}],
    "colors": [{"id": 2, "label": "Full color front"}],
    "turnarounds": [{"id": 1, "label": "Regular"}, {"id": 3, "label": "Rush"}],
}
CATALOG = {"items": [{"id": 210, "label": "Rounded corners", "type": {"id": 2, "label": "Rounded corners"}}]}
PRICES = {"items": [{"quantity": 100, "markupPrice": 20}, {"quantity": 250, "markupPrice": 35}]}
SUPPORT = {"items": [{"quantity": 100, "turnaround": 1}, {"quantity": 250, "turnaround": 1}]}
OPTION_SUPPORT = {"2": deepcopy(SUPPORT)}


class Evidence:
    def __init__(self, store):
        self.store, self.sequence = store, 0

    def save(self, path, params, data, *, context=None, binding="valid", status=200,
             task_status="succeeded", url=None, gzip_body=False, raw_hash=None):
        self.sequence += 1
        ident = f"response-{self.sequence}"
        request = {"method": "GET", "path": API + path, "params": deepcopy(params),
                   "context": deepcopy(context or CONTEXT)}
        signature = request_key(request)
        raw = json.dumps(data).encode()
        object_path = self.store.put_object(gzip.compress(raw) if gzip_body else raw, "json")
        encoded = {key: str(value).lower() if isinstance(value, bool) else str(value)
                   for key, value in params.items()}
        response_url = url or request["context"]["origin"] + request["path"] + "?" + urlencode(encoded)
        with self.store.session() as session:
            session.add(Response(id=ident, request_key=signature, created_at=self.sequence,
                status=status, url=response_url, object_path=object_path,
                raw_hash=raw_hash or digest(raw.hex()), semantic_hash=digest(data), content_type="application/json"))
            session.add(DomainTask(id="task-" + ident, job_id="job", request_key=signature,
                request=request, products=["product"], status=task_status, response_id=ident))
            if binding != "missing":
                session.add(ResponseObservation(id="observation-" + ident,
                    run_id="another-run" if binding == "wrong_run" else "run",
                    product_id="another-product" if binding == "wrong_product" else "product",
                    response_id=ident, kind="browser" if binding == "wrong_kind" else "harvest",
                    request_signature="unrelated-signature" if binding == "wrong_signature" else signature,
                    context=request["context"], created_at=self.sequence))
        return ident

    def spec(self, **kwargs):
        return self.save("settings/product/specifications", SPEC_PARAMS, SPEC, **kwargs)

    def lookup(self, *, params=None, data=None, **kwargs):
        return self.save("products", params or LOOKUP_PARAMS,
                         {"items": [{"id": 123}]} if data is None else data, **kwargs)

    def inventory(self):
        return load_inventories(self.store, "run")[0]


@pytest.fixture
def evidence(tmp_path):
    store = Store(Settings(data_dir=tmp_path, database_url="", s3_bucket=""))
    with store.session() as session:
        session.add(Run(id="run", name="Inventory fixture", status="paused"))
        session.add(Product(id="product", run_id="run", title="Business Cards",
            url="https://www.printograph.com/products/business-cards/order", status="paused"))
        session.add(DomainJob(id="job", run_id="run", scope="inventory-fixture", manifest_hash="fixture",
            manifest={"phase": "DOMAIN", "requests": []}, status="requests_drained", budget=1000))
    yield Evidence(store)
    store.engine.dispose()


@pytest.mark.parametrize("binding", ["missing", "wrong_run", "wrong_product", "wrong_kind", "wrong_signature"])
def test_lookup_requires_exact_product_run_signature_and_harvest_binding(evidence, binding):
    evidence.spec()
    evidence.lookup(binding=binding)
    assert evidence.inventory()["bases"] == []


@pytest.mark.parametrize("mutation", ["different_query", "duplicate_query", "wrong_origin", "fragment", "failed_task", "failed_response"])
def test_unbound_or_unsuccessful_response_cannot_supply_lookup(evidence, mutation):
    evidence.spec()
    url = CONTEXT["origin"] + API + "products?" + urlencode(LOOKUP_PARAMS)
    kwargs = {}
    if mutation == "different_query": url = url.replace("paper=1", "paper=2")
    elif mutation == "duplicate_query": url += "&paper=1"
    elif mutation == "wrong_origin": url = url.replace("www.printograph.com", "example.test")
    elif mutation == "fragment": url += "#unbound"
    elif mutation == "failed_task": kwargs["task_status"] = "failed"
    elif mutation == "failed_response": kwargs["status"] = 503
    evidence.lookup(url=url, **kwargs)
    assert evidence.inventory()["bases"] == []


def test_latest_specification_context_cannot_borrow_old_tables(evidence):
    old_spec = evidence.spec()
    evidence.lookup()
    evidence.save("products/123/options", {"requestValues": "true"}, CATALOG)
    evidence.save("products/123/prices", {"itemId": "null", "cid": ""}, PRICES)
    evidence.save("products/123/options/210/prices", {}, PRICES)
    evidence.save("products/quantities", {**SELECTORS, "includeTurnaround": "true"}, SUPPORT)
    evidence.save("products/options/quantities", {**SELECTORS, "optionIds": "2", "includeTurnaround": "true"}, OPTION_SUPPORT)
    current = {**CONTEXT, "source_epoch": "epoch-two", "service_signature": "manual-fixture"}
    new_spec = evidence.spec(context=current)
    new_lookup = evidence.lookup(context=current)
    inventory = evidence.inventory()
    assert inventory["context"] == current and inventory["specification_evidence_id"] == new_spec
    assert len(inventory["bases"]) == 1
    entry = inventory["bases"][0]
    assert entry["lookup"]["response_id"] == new_lookup
    assert old_spec not in entry["evidence_ids"]
    assert entry["base_prices"] is None and entry["base_support"] is None
    assert entry["option_prices"] == {} and entry["option_support"] == {}
    assert entry["table_evidence"]["catalog"] is None
    assert inventory["source_context_verified_for_pricing"] is False
    assert inventory["catalog_complete"] is False


@pytest.mark.parametrize("suffix", ["prices", "options/210/prices"])
@pytest.mark.parametrize("extra", [{"quantity": "100"}, {"priceGroupId": "9"}, {"proofType": "100"}, {"itemId": "123"}, {"cid": "456"}])
def test_filtered_or_overridden_price_tables_cannot_replace_full_tables(evidence, suffix, extra):
    evidence.spec()
    evidence.lookup()
    ordinary = evidence.save("products/123/" + suffix, {}, PRICES)
    evidence.save("products/123/" + suffix, extra, {"items": [{"quantity": 100, "markupPrice": 1}]})
    entry = evidence.inventory()["bases"][0]
    actual = entry["table_evidence"]["base_prices"] if suffix == "prices" else entry["table_evidence"]["option_prices"]["210"]
    assert actual == ordinary


@pytest.mark.parametrize("route", ["products/quantities", "products/options/quantities"])
@pytest.mark.parametrize("extra", [{"quantity": "100"}, {"priceGroupId": "9"}, {"turnaround": "3"}, {"unproven_filter": "1"}])
def test_filtered_support_cannot_shadow_full_selector_support(evidence, route, extra):
    evidence.spec()
    evidence.lookup()
    is_option = route == "products/options/quantities"
    params = {**SELECTORS, "includeTurnaround": "true", **({"optionIds": "2"} if is_option else {})}
    ordinary = evidence.save(route, params, OPTION_SUPPORT if is_option else SUPPORT)
    evidence.save(route, {**params, **extra}, {"2": {"items": []}} if is_option else {"items": []})
    entry = evidence.inventory()["bases"][0]
    assert entry["table_evidence"]["option_support" if is_option else "base_support"] == ([ordinary] if is_option else ordinary)


@pytest.mark.parametrize("extra", [{"quantity": "100"}, {"priceGroupId": "9"}, {"proofType": "100"}, {"unproven_filter": "1"}])
def test_lookup_with_unrelated_parameters_is_not_an_ordinary_selector(evidence, extra):
    evidence.spec()
    evidence.lookup(params={**LOOKUP_PARAMS, **extra})
    assert evidence.inventory()["bases"] == []


def test_support_uses_exact_geometry_and_requires_all_turnarounds(evidence):
    evidence.spec()
    evidence.lookup()
    evidence.save("products/quantities", {**SELECTORS, "orientation": "2", "includeTurnaround": "true"}, SUPPORT)
    evidence.save("products/quantities", {**SELECTORS, "includeTurnaround": "false"}, SUPPORT)
    assert evidence.inventory()["bases"][0]["base_support"] is None


def test_missing_option_catalog_is_not_a_proven_empty_option_domain(evidence):
    evidence.spec()
    evidence.lookup()
    inventory = evidence.inventory()
    assert inventory["bases"][0]["table_evidence"]["catalog"] is None
    requests = missing_table_manifest(inventory)["requests"]
    assert any(entry["purpose"] == "option_catalog" and
               entry["request"]["path"] == API + "products/123/options" and
               entry["request"]["params"] == {"requestValues": "true", "eddm": "false"}
               for entry in requests)


def test_repeated_lookups_and_aliases_deduplicate_tables_but_not_selector_support(evidence):
    evidence.spec()
    first = evidence.lookup()
    latest = evidence.lookup()
    evidence.lookup(params={**LOOKUP_PARAMS, "size": "102"})
    evidence.save("products/123/options", {"requestValues": "true"}, CATALOG)
    inventory = evidence.inventory()
    assert len(inventory["bases"]) == 2
    refs = [entry["lookup"]["response_id"] for entry in inventory["bases"]]
    assert latest in refs and first not in refs
    requests = missing_table_manifest(inventory)["requests"]
    base_prices = [entry for entry in requests if entry["request"]["path"] == API + "products/123/prices"]
    option_prices = [entry for entry in requests if entry["request"]["path"] == API + "products/123/options/210/prices"]
    supports = [entry for entry in requests if entry["purpose"] == "base_quantity_support"]
    assert len(base_prices) == len(option_prices) == 1 and len(supports) == 2
    assert {entry["request"]["params"]["size"] for entry in supports} == {"101", "102"}


def test_subset_option_support_does_not_satisfy_other_catalog_types(evidence):
    evidence.spec()
    evidence.lookup()
    catalog = deepcopy(CATALOG)
    catalog["items"].append({"id": 211, "label": "Manual proof", "type": {"id": 1, "label": "Manual proof"}})
    evidence.save("products/123/options", {"requestValues": "true"}, catalog)
    evidence.save("products/options/quantities", {**SELECTORS, "optionIds": "2", "includeTurnaround": "true"}, OPTION_SUPPORT)
    requests = missing_table_manifest(evidence.inventory())["requests"]
    options = [entry for entry in requests if entry["purpose"] == "option_quantity_support"]
    assert len(options) == 1 and options[0]["request"]["params"]["optionIds"] == "1,2"


def test_multiple_option_support_subsets_merge_without_losing_regular_or_rush_choices(evidence):
    evidence.spec()
    evidence.lookup()
    evidence.lookup(params={**LOOKUP_PARAMS, "turnaround": "3"}, data={"items": [{"id": 124}]})
    first = evidence.save("products/options/quantities", {**SELECTORS, "optionIds": "2", "includeTurnaround": "true"}, OPTION_SUPPORT)
    manual = {"items": [{"quantity": 100, "turnaround": 1}, {"quantity": 100, "turnaround": 3}]}
    second = evidence.save("products/options/quantities", {**SELECTORS, "optionIds": "1", "includeTurnaround": "true"}, {"1": manual})
    inventory = evidence.inventory()
    assert len(inventory["bases"]) == 2
    for entry in inventory["bases"]:
        assert entry["option_support"] == {"1": manual, **OPTION_SUPPORT}
        assert entry["option_support_requested_types"] == ["1", "2"]
        assert entry["option_support_conflicted_types"] == []
        assert entry["table_evidence"]["option_support"] == [first, second]


def test_conflicting_support_stays_unresolved_without_discarding_other_types(evidence):
    evidence.spec()
    evidence.lookup()
    evidence.save("products/options/quantities", {**SELECTORS, "optionIds": "1,2", "includeTurnaround": "true"}, {"1": SUPPORT, **OPTION_SUPPORT})
    evidence.save("products/options/quantities", {**SELECTORS, "optionIds": "2", "includeTurnaround": "true"}, {"2": {"items": []}})
    inventory = evidence.inventory()
    entry = inventory["bases"][0]
    assert entry["option_support"] == {"1": SUPPORT}
    assert entry["option_support_conflicted_types"] == ["2"]
    assert any("Conflicting option quantity support" in message for message in inventory["diagnostics"])


def test_unrequested_option_type_in_response_cannot_expand_support(evidence):
    evidence.spec()
    evidence.lookup()
    evidence.save("products/options/quantities", {**SELECTORS, "optionIds": "2", "includeTurnaround": "true"}, {"1": SUPPORT, **OPTION_SUPPORT})
    entry = evidence.inventory()["bases"][0]
    assert entry["option_support"] == OPTION_SUPPORT
    assert entry["option_support_requested_types"] == ["2"]


def test_requested_but_absent_type_is_unknown_without_unbounded_repeat_request(evidence):
    evidence.spec()
    evidence.lookup()
    evidence.save("products/123/options", {"requestValues": "true"}, CATALOG)
    evidence.save("products/options/quantities", {**SELECTORS, "optionIds": "2", "includeTurnaround": "true"}, {})
    inventory = evidence.inventory()
    entry = inventory["bases"][0]
    assert entry["option_support"] == {} and entry["option_support_requested_types"] == ["2"]
    assert not any(request["purpose"] == "option_quantity_support"
                   for request in missing_table_manifest(inventory)["requests"])
    assert inventory["catalog_complete"] is False


@pytest.mark.parametrize("data", [{"items": []}, {"items": [{"id": 123}, {"id": 124}]}, {"items": [{"id": 123}], "hasMore": True}])
def test_empty_ambiguous_and_partial_lookup_responses_are_diagnostic_not_valid(evidence, data):
    evidence.spec()
    evidence.lookup(data=data)
    inventory = evidence.inventory()
    assert inventory["bases"] == [] and inventory["diagnostics"]


def test_missing_specification_is_explicit_and_does_not_infer_a_domain(evidence):
    evidence.lookup()
    inventory = evidence.inventory()
    assert inventory["product_type"] is None and inventory["bases"] == []
    assert inventory["diagnostics"] == ["No saved specification response."]


def test_compressed_saved_body_keeps_labels_and_hash_binding(evidence):
    evidence.spec(gzip_body=True)
    evidence.lookup(gzip_body=True)
    entry = evidence.inventory()["bases"][0]
    assert entry["source_labels"]["size"] == '3.5" × 2"'
    assert entry["source_labels"]["paper"] == "14 pt. stock"


@pytest.mark.parametrize("damage", ["missing_object", "wrong_hash", "invalid_json"])
def test_damaged_evidence_fails_explicitly_instead_of_inventing_empty_inventory(evidence, damage):
    evidence.spec()
    ident = evidence.lookup(raw_hash="not-the-body-hash" if damage == "wrong_hash" else None)
    with evidence.store.session() as session:
        row = session.get(Response, ident)
        if damage == "missing_object":
            (evidence.store.root / row.object_path).unlink()
        elif damage == "invalid_json":
            raw = b"not-json"
            row.object_path = evidence.store.put_object(raw, "json")
            row.raw_hash = digest(raw.hex())
    with pytest.raises((ValueError, FileNotFoundError)):
        evidence.inventory()
