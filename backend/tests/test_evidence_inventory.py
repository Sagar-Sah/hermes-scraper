"""Offline attribution uses stored links and source IDs, never time adjacency."""

import gzip
import hashlib
import json
import sqlite3

from crawler.evidence_inventory import build_inventory, quantity_tables, render_report


def test_inventory_attributes_bound_routes_keeps_ambiguity_and_does_not_mutate(tmp_path):
    db = tmp_path / "snapshot.db"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE products(id TEXT,run_id TEXT,url TEXT,title TEXT,row_count INT,request_count INT,status TEXT);
    CREATE TABLE events(run_id TEXT,type TEXT,payload TEXT);
    CREATE TABLE batches(product_id TEXT,object_path TEXT);
    CREATE TABLE responses(id TEXT,request_key TEXT,created_at REAL,status INT,url TEXT,object_path TEXT,raw_hash TEXT,semantic_hash TEXT,etag TEXT,modified TEXT,content_type TEXT);
    """)
    first, second = "https://source.test/products/cards/order", "https://source.test/products/flyers/order"
    for pid, url in [("a", first), ("b", second)]:
        c.execute("INSERT INTO products VALUES(?,?,?,?,?,?,?)", (pid, "run", url, pid, 1, 20, "paused"))

    def response(ident, path, data, timestamp=1, status=200):
        name = ident + ".response"
        raw = json.dumps(data, sort_keys=True).encode()
        (tmp_path / name).write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        c.execute("INSERT INTO responses VALUES(?,?,?,?,?,?,?,?,?,?,?)", (ident, ident, timestamp, status,
                  "https://source.test" + path, name, digest, digest, None, None, "application/json"))

    for ident, url, typ in [("dom-a", first, "1"), ("dom-b", second, "27")]:
        response(ident, url.removeprefix("https://source.test"), {
            "kind": "visible_product_quote", "url": url, "product_type": typ,
            "controls": [{"key": "quantity", "value": "50"}],
        })
    response("lookup", "/service/rest/v1/products?productType=1&size=101", {"items": [{"id": 123}]})
    response("lookup-failed", "/service/rest/v1/products?productType=1&size=999", {"items": [{"id": 999}]}, status=404)
    for size in (101, 102):
        response("support-" + str(size), f"/service/rest/v1/products/quantities?productType=1&size={size}",
                 {"items": [{"quantity": 100, "turnaroundList": [1]}]})
    response("dom-a100", "/products/cards/order", {
        "kind": "visible_product_quote", "url": first, "product_type": "1",
        "controls": [{"key": "quantity", "value": "100"},
                     {"key": "raisedprint", "value": "", "choices": [{"value": "", "label": "None"}, {"value": "135", "label": "Raised"}]}],
    })
    for ident in ["price1", "price2"]:
        response(ident, "/service/rest/v1/products/123/prices?cid=private-account", {
            "items": [{"quantity": 50, "markupPrice": "5.00"}, {"quantity": 100, "markupPrice": "8.00"}],
        })
    response("unknown", "/service/rest/v1/rules/turnarounds", {"items": [{"id": 1}]}, timestamp=1.001)
    response("ambiguous", "/service/rest/v1/products/999/prices", {"items": [{"quantity": 50, "markupPrice": "1.00"}]})
    c.execute("INSERT INTO events VALUES(?,?,?)", ("run", "pricing_table_captured", json.dumps({"product_id": "a", "evidence_id": "price1"})))
    for pid in ["a", "b"]:
        key = pid + ".jsonl.gz"
        (tmp_path / key).write_bytes(gzip.compress(json.dumps({"evidence_ids": ["ambiguous", "dom-" + pid]}).encode()))
        c.execute("INSERT INTO batches VALUES(?,?)", (pid, key))
    c.commit()
    c.close()
    before = db.read_bytes()
    result = build_inventory(db, tmp_path, run_id="run")
    assert db.read_bytes() == before
    assert result["network_calls"] == 0
    index = {r["id"]: r for r in result["response_attribution"]}
    assert index["price2"]["attribution"] == first
    assert not index["price2"]["direct_run_linked"]
    assert index["unknown"]["attribution"] == "unattributed"
    assert index["ambiguous"]["attribution"] == "ambiguous"
    assert set(index["ambiguous"]["candidate_products"]) == {first, second}
    prices = next(r for r in result["routes"] if r["product"] == first and r["route_family"].endswith("/prices"))
    assert prices["captured_response_observations"] == 2
    assert prices["distinct_semantic_responses"] == 1
    assert prices["same_url_status_content_repeats"] == 1
    assert prices["safe_cache_savings_lower_bound"] == 0
    assert prices["direct_run_linked_observations"] == 1
    assert prices["parameter_values"] == {"cid": ["<redacted>"]}
    assert prices["path_parameter_values"] == {"base_product_id": ["123"]}
    assert prices["http_method"] == "not_recorded"
    assert index["price1"]["exact_url_sha256"] == index["price2"]["exact_url_sha256"]
    assert index["price1"]["stored_request_signature"] != index["price2"]["stored_request_signature"]
    assert "private-account" not in json.dumps(result)
    assert result["successful_lookup_domains"][0]["query"] == {"productType": ["1"], "size": ["101"]}
    assert len(result["successful_lookup_domains"]) == 1
    support = next(r for r in result["routes"] if r["route_family"] == "/service/rest/v1/products/quantities")
    assert support["distinct_semantic_responses"] == 1
    assert support["distinct_exact_urls"] == 2
    assert support["same_url_status_content_repeats"] == 0
    assert support["safe_cache_savings_lower_bound"] == 0
    clue = next(d for d in result["finishing_visibility_evidence"] if d["product"] == first and d["control"] == "raisedprint")
    assert clue["quantities"]["50"]["not_in_visible_controls"] == 1
    assert clue["quantities"]["100"]["offered_values"] == ["", "135"]
    assert "not all network requests" in render_report(result)


def test_quantity_support_preserves_modes_and_does_not_use_option_ids_as_tiers():
    support = {"135": [{"quantity": 100, "turnaroundList": [1]}, {"quantity": 250, "turnaroundList": [1, 3]}],
               "999": [{"id": 999, "name": "Unrelated option"}]}
    assert quantity_tables(support) == [{"path": "$.135", "quantities": [100, 250],
                                       "quantities_by_turnaround": {"1": [100, 250], "3": [250]}}]
