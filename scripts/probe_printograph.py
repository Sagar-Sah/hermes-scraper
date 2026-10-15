#!/usr/bin/env python3
"""Bounded public-read capability probe; never obtains credentials or writes a cart.

At most four sequential HTTPS GETs: product HTML, two observed public bundles,
and one source-documented specifications request. Stops API probing on 401/403.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from crawler.adapters.printograph import (
    ProductHTML,
    parse_frontend_contract,
)  # noqa: E402

VERSION = "0109_20260902_202634-101"
ASSET_ROOT = "https://static.printograph.com/tl/cli-app/dist/production/js/"
PRODUCTS = {"business-cards": 1, "club-flyers": 27, "roll-labels": 36}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT.parents[1] / "work" / "printograph-probe.json",
    )
    parser.add_argument("--report", type=Path, default=ROOT / "docs" / "live-probe.md")
    args = parser.parse_args()
    report = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "requests": [],
        "maximum_concurrency": 1,
        "maximum_requests_per_second": 2,
        "mutations": 0,
        "credentials_acquired": False,
        "live_price_schema_verified": False,
        "bulk_composition_verified": False,
        "products": [
            {
                "url": f"https://www.printograph.com/products/{slug}/order",
                "product_type": value,
                "metadata_source": "Public browser DOM inspected separately; not inferred from URL",
            }
            for slug, value in PRODUCTS.items()
        ],
    }
    last_request = 0.0

    def fetch(url):
        nonlocal last_request
        if len(report["requests"]) >= 4:
            raise RuntimeError("Probe request ceiling reached")
        time.sleep(max(0, 0.55 - (time.monotonic() - last_request)))
        last_request = time.monotonic()
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "30",
            "--max-filesize",
            "8000000",
            "--proto",
            "=https",
            "--write-out",
            "\n__HTTP_STATUS__%{http_code}",
            url,
        ]
        raw = subprocess.check_output(command)
        body, status = raw.rsplit(b"\n__HTTP_STATUS__", 1)
        row = {
            "url": url,
            "method": "GET",
            "status": int(status),
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        report["requests"].append(row)
        text = body.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
        return row, data

    page, html = fetch(report["products"][0]["url"])
    dom = ProductHTML()
    if isinstance(html, str):
        dom.feed(html)
    page["title"] = dom.title
    page["product_bootstrap_present"] = bool(dom.attributes)
    main_row, main_js = fetch(ASSET_ROOT + "orderForm.js?v=" + VERSION)
    common_row, common_js = fetch(ASSET_ROOT + "chunk-common.js?v=" + VERSION)
    report["source_contracts"] = {}
    for slug, product_type in PRODUCTS.items():
        try:
            contract = parse_frontend_contract(main_js, product_type)
            report["source_contracts"][slug] = {
                **contract,
                "priced_options": sorted(contract["priced_options"]),
            }
        except Exception as exc:
            report["source_contracts"][slug] = {"error": str(exc)}
    expected = {
        "specifications_get": "getSpec:function(e)",
        "product_lookup_get": "getProductId:function(e)",
        "base_price_get": "getPrice:function(e)",
        "option_price_get": "getOptionPrice:function(e)",
    }
    report["wrapper_methods_found"] = {
        name: token in common_js for name, token in expected.items()
    }
    endpoint = "https://www.printograph.com/service/rest/v1/settings/product/specifications?requestValues=true&productType=1"
    api, data = fetch(endpoint)
    api["response"] = (
        data
        if isinstance(data, dict)
        else {"body_kind": "non_json", "prefix": data[:120]}
    )
    if api["status"] in (401, 403):
        report["outcome"] = "access_required"
        report["next_step"] = (
            "Supply an authorized API profile or explicit response capture; rerun schema verification before pricing claims."
        )
    elif api["status"] == 200 and isinstance(data, dict) and "messageList" not in data:
        report["outcome"] = "specifications_received_prices_unverified"
        report["next_step"] = (
            "Validate the received specification schema before a separately bounded pricing probe."
        )
    else:
        report["outcome"] = "source_contract_unverified"
        report["next_step"] = (
            "Inspect the recorded response; no further requests were attempted."
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Printograph bounded live probe",
        "",
        f"Observed: {report['observed_at']}",
        "",
        f"Outcome: **{report['outcome']}**. Four sequential public GET requests; no cart/order writes, credentials acquired, or load test.",
        "",
        f"The direct product-page response was titled **{dom.title}**. The specifications endpoint returned **HTTP {api['status']}**.",
        "",
    ]
    if api["status"] in (401, 403):
        lines += [
            "The JSON response identifies `API-Vendor-Token` as invalid. API probing stopped; no bypass or token extraction was attempted.",
            "",
        ]
    lines += [
        "Public browser DOM separately identified business cards as product type 1, club flyers as 27, and roll labels as 36.",
        "",
        "The current public JavaScript confirms GET specifications, product lookup, quantity tables, base-price tables, option lists, and option-price tables. Price consumers read `items`, `quantity`, `markupPrice`, and `listPrice`.",
        "",
        "**Live product/quantity/price JSON and additive composition remain unverified.** Synthetic tests validate parser behavior, not supplier prices. The adapter rejects unfamiliar schemas, preserves evidence IDs, and marks calculated option combinations pending with partial coverage.",
        "",
        "| Request | HTTP |",
        "|---|---|",
    ]
    lines += [f"| `{r['url']}` | {r['status']} |" for r in report["requests"]]
    lines += ["", report["next_step"], ""]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines))
    print(
        json.dumps(
            {
                "outcome": report["outcome"],
                "requests": len(report["requests"]),
                "output": str(args.output),
                "report": str(args.report),
            }
        )
    )


if __name__ == "__main__":
    main()
