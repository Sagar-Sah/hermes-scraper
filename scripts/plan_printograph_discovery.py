#!/usr/bin/env python3
"""Build a discovery-only request manifest from frozen local evidence."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from crawler.evidence_inventory import Objects
from crawler.printograph_discovery import plan_discovery, source_contract

PRODUCTS = {"1": "business-cards", "27": "club-flyers", "36": "roll-labels"}


def generate(database, object_root, bundles, inventory=None):
    database, bundles = Path(database).resolve(), Path(bundles)
    contract = source_contract((bundles / "orderForm.js").read_text(), (bundles / "chunk-common.js").read_text())
    objects = Objects(object_root)
    latest = {}
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        for row in connection.execute("SELECT * FROM responses WHERE status=200 AND url LIKE '%/settings/product/specifications?%' ORDER BY created_at"):
            url = urlsplit(row["url"])
            params = parse_qs(url.query)
            if url.path != "/service/rest/v1/settings/product/specifications" or params.get("requestValues") != ["true"]:
                continue
            product_type = params.get("productType", [None])[0]
            if product_type in PRODUCTS:
                latest[product_type] = dict(row)
    history = (inventory or {}).get("successful_lookup_domains", [])
    plans = []
    for product_type, slug in PRODUCTS.items():
        if product_type not in latest:
            raise ValueError(f"No saved successful specifications for product type {product_type}")
        row = latest[product_type]
        data = objects.json(row["object_path"])
        if data is None:
            raise ValueError(f"Missing specification object {row['id']}")
        plans.append(plan_discovery(product_type, data, contract, evidence_id=row["id"],
                                    product_url=f"https://www.printograph.com/products/{slug}/order",
                                    historical_lookups=history))
    return {"schema_version": 1, "phase": "DISCOVERY", "network_calls": 0,
            "source": "frozen local specifications and frontend bundles",
            "contract": contract, "products": plans,
            "candidate_lookup_count": sum(p["candidate_lookup_count"] for p in plans),
            "discovery_complete": False, "pricing_unlocked": False}


def report(result):
    lines = ["# Offline discovery request manifest", "", "This is a finite candidate discovery plan, not a catalog of valid combinations. No supplier or model requests were made. No price requests are present or unlocked.", "", "| Product | Bound size/orientation/shape tuples | Papers | Colors | Finishes | Modes | Candidate lookups |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for product in result["products"]:
        counts = product["domain_counts"]
        lines.append(f"| {PRODUCTS[product['product_type']]} | {counts['size_orientation_shape_tuples']} | {counts['papers']} | {counts['colors']} | {counts['finishes']} | {counts['production_modes']} | {product['candidate_lookup_count']} |")
    lines += ["", f"Total candidate base lookups: **{result['candidate_lookup_count']}**. A candidate must still yield a fresh, context-bound unique lookup before its base ID can be used. Historical matches are evidence links only. Invalid and unresolved results need separate durable states; errors cannot count as discovered valid products.", "", "The Roll Labels shape and orientation come from each saved size record. The plan never multiplies every shape by every size. Custom dimensions are outside the finite preset scope.", "", "The client uses GET `/products` with productType, turnaround and selected base selectors. Its consumer accepts exactly one returned item. The observed caller does not establish pagination, omission-of-filter listing, or multiple base-product-ID lookup. Its comma-separated batching is for eligible option **type IDs** in `/products/options/quantities`, not base-product listing.", "", "After a validated base lookup, conditional discovery requests can obtain its option catalog, base quantity/turnaround support, and option quantity/turnaround support. Business Cards and Club Flyers have distinct frontend option variants; the active store variant must be resolved before option-domain exhaustion is claimed. POST `/rules/turnarounds` exists, but its historical response table lacks the request payload, so no invented request body is emitted.", "", "Completion requires all declared finite candidates and dependent domains to be resolved. Ambiguous results, paging markers, source errors, missing service/session context and unsupported custom dimensions remain explicit blockers or scoped exclusions. Candidate count is not a complete request budget: option discovery and unresolved branches can add work.", "", "## Source evidence", "", "Offsets are zero-based character positions in the frozen bundles.", "", "| Evidence | Source function | Offset |", "| --- | --- | ---: |"]
    for key, reference in result["contract"]["evidence"].items():
        lines.append(f"| {key} | {reference['function']} | {reference['offset']} |")
    lines += ["", "Bundle SHA-256:", ""]
    for file, digest in result["contract"]["bundle_sha256"].items():
        lines.append(f"- `{file}`: `{digest}`")
    lines += ["", "Specification evidence:", ""]
    for product in result["products"]:
        lines.append(f"- {PRODUCTS[product['product_type']]}: `{product['specification_evidence_id']}`")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--object-root", type=Path, required=True)
    parser.add_argument("--bundles", type=Path, required=True)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text()) if args.inventory else None
    result = generate(args.database, args.object_root, args.bundles, inventory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    args.report.write_text(report(result))
    print(json.dumps({"candidate_lookup_count": result["candidate_lookup_count"], "network_calls": 0,
                      "output": str(args.output), "report": str(args.report)}))


if __name__ == "__main__":
    main()
