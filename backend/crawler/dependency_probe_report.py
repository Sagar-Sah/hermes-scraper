"""Read-only, rerunnable report of durable supplier dependency experiments.

No HTTP, browser, profile, credential, or mutation APIs are used. Only completed
saved phase captures are formula observations; request-start states are not
substituted for a settled quote. A paired table observation is never a cache
certificate or a global dependency proof.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, parse_qsl

from .price_contract import SOURCE_CONTRACT, calculate_source_total

PRODUCT_URLS = {
    "https://www.printograph.com/products/business-cards/order": "Business Cards",
    "https://www.printograph.com/products/club-flyers/order": "Club Flyers",
    "https://www.printograph.com/products/roll-labels/order": "Roll Labels",
}
PUBLIC_PARAMS = {
    "productType", "requestValues", "turnaround", "size", "orientation", "paper",
    "color", "coating", "shape", "paperFinish", "finish", "quantity", "quantities",
    "optionIds", "optionId", "includePrices", "includeTurnaround", "count", "offset",
    "limit", "page", "eddm",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", value) else None


def _load_response(row, objects_root):
    if not row:
        return {"available": False, "reason": "response_record_missing"}
    result = {"evidence_id": row["id"], "http_status": row["status"],
              "path": urlsplit(row["url"]).path, "available": False}
    path = Path(row["object_path"])
    root = Path(objects_root).resolve()
    if not path.is_absolute():
        path = root / path
    if not path.resolve().is_relative_to(root / "objects"):
        result["reason"] = "response_object_outside_object_store"
        return result
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError):
        result["reason"] = "response_body_unreadable"
        return result
    items = data.get("items") if isinstance(data, dict) else None
    quantities = sorted({int(item["quantity"]) for item in (items or [])
                         if isinstance(item, dict) and isinstance(item.get("quantity"), (str, int))
                         and not isinstance(item.get("quantity"), bool)
                         and str(item["quantity"]).isdigit()}) if isinstance(items, list) else []
    result.update(available=True, raw_body_sha256=hashlib.sha256(raw).hexdigest(),
                  full_json_sha256=_digest(data),
                  item_count=len(items) if isinstance(items, list) else None,
                  quantity_count=len(quantities), quantities=quantities,
                  priced_quantity_table=bool(items) and isinstance(items, list)
                    and all(isinstance(item, dict) and "quantity" in item and "markupPrice" in item for item in items))
    return result


def _formula(capture, loaded_contract):
    value = calculate_source_total(capture)
    reasons = set(value.get("reasons", []))
    if loaded_contract != SOURCE_CONTRACT:
        reasons.add("loaded_source_contract_unverified")
    if capture.get("capture_kind") != "controller":
        reasons.add("settled_controller_capture_required")
    if not capture.get("displayed_total") or capture.get("controller_total") is None:
        reasons.add("controller_and_displayed_total_required")
    complete = value["status"] == "complete" and not reasons
    comparison = value.get("comparison") if complete else "unresolved"
    if comparison not in {"matched", "mismatch"}:
        comparison = "unresolved"
        reasons.add("comparison_not_established")
    return {"status": "complete" if complete else "unresolved", "comparison": comparison,
            "context_hash": value.get("context_hash") if complete else None,
            "reasons": sorted(reasons), "subtotal": value.get("subtotal") if complete else None,
            "subtotal_before_coupon": value.get("subtotal_before_coupon"),
            "displayed_total": capture.get("displayed_total"),
            "controller_total": capture.get("controller_total"),
            "components": [{k: c.get(k) for k in ("field", "selected_value", "amount")}
                           for c in value.get("components", [])]}, value.get("context", {})


def _fresh_base(phase, captures, bodies):
    evidence = phase.get("base_recheck", {}).get("evidence_id")
    candidates = [c for c in captures if c.get("evidence_id") == evidence
                  and c.get("kind") == "explicit_base_recheck" and c.get("cached") is False]
    if len(candidates) != 1 or phase.get("base_recheck", {}).get("cached") is not False:
        return None, "fresh_uncached_base_recheck_missing"
    candidate = candidates[0]
    query = parse_qsl(urlsplit(candidate.get("url", "")).query, keep_blank_values=True)
    if (len(query) != len(dict(query)) or set(dict(query)) - {"itemId", "cid"}
            or dict(query).get("itemId", "null") != "null" or dict(query).get("cid", "") != ""
            or candidate.get("phase") != phase.get("name")):
        return None, "base_recheck_query_or_phase_unbound"
    body = bodies.get(evidence, {})
    if not body.get("available") or body.get("http_status") != 200 or not body.get("priced_quantity_table"):
        return None, "fresh_base_response_unavailable_or_not_quantity_table"
    selected = phase.get("capture", {}).get("userSelectedOptions", {})
    expected_path = "/service/rest/v1/products/" + str(selected.get("productId")) + "/prices"
    if body.get("path") != expected_path or urlsplit(candidate.get("url", "")).path != expected_path:
        return None, "base_recheck_product_mismatch"
    left = calculate_source_total(phase.get("capture", {}))
    right = calculate_source_total(candidate.get("controller_context", {}))
    if not left.get("context_hash") or right.get("context_hash") != left["context_hash"]:
        return None, "base_recheck_context_mismatch"
    return {"evidence_id": evidence, "full_json_sha256": body["full_json_sha256"],
            "quantity_count": body["quantity_count"], "path": body["path"]}, None


def compare_phases(baseline, phase, report, bodies):
    """A narrowly scoped observational pair, never a reusable certificate."""
    result = {"baseline": baseline["name"], "phase": phase["name"],
              "status": "unresolved", "reasons": [], "global_proof": False,
              "cache_certificate": False, "compared_route": "base_quantity_prices"}
    bf, bc = _formula(baseline.get("capture", {}), report.get("loaded_contract"))
    pf, pc = _formula(phase.get("capture", {}), report.get("loaded_contract"))
    if bf["comparison"] != "matched" or pf["comparison"] != "matched":
        result["reasons"].append("both_complete_matching_formula_captures_required")
        return result
    before, after = bc.get("selections", {}), pc.get("selections", {})
    changes = {key: {"before": before.get(key), "after": after.get(key)}
               for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}
    extra = [key for key in sorted(set(bc) | set(pc)) if key != "selections" and bc.get(key) != pc.get(key)]
    result["changed_selections"], result["other_context_changes"] = changes, extra
    if len(changes) != 1 or extra:
        result.update(status="confounded", reasons=["exactly_one_changed_selection_and_no_other_context_change_required"])
        return result
    btable, berr = _fresh_base(baseline, report.get("captures", []), bodies)
    ptable, perr = _fresh_base(phase, report.get("captures", []), bodies)
    if berr or perr:
        result["reasons"] = sorted({reason for reason in (berr, perr) if reason})
        return result
    if btable["evidence_id"] == ptable["evidence_id"]:
        result["reasons"] = ["distinct_fresh_response_observations_required"]
        return result
    result.update(status="observed_pair", baseline_table=btable, phase_table=ptable,
                  full_base_bodies_equal=btable["full_json_sha256"] == ptable["full_json_sha256"],
                  interpretation="Only this captured pair and route; no option-table, catalog-wide, future-state, or cache-reuse conclusion.")
    return result


def build_report(db_path, objects_root, *, run_id=None):
    db = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    jobs = list(db.execute("SELECT id,run_id,status,budget,used,created_at,updated_at,manifest,results,error FROM probe_jobs" +
                           (" WHERE run_id=?" if run_id else "") + " ORDER BY created_at", (run_id,) if run_id else ()))
    response_ids, decoded, attempts = set(), [], {}
    for job in jobs:
        manifest, results = json.loads(job["manifest"]), json.loads(job["results"])
        decoded.append((job, manifest, results))
        attempts[job["id"]] = list(db.execute("SELECT phase,method,status FROM probe_attempts WHERE job_id=? ORDER BY sequence", (job["id"],)))
        for result in results:
            if result.get("response_id"):
                response_ids.add(result["response_id"])
            response_ids.update(c["evidence_id"] for c in result.get("report", {}).get("captures", []) if c.get("evidence_id"))
    responses = {rid: db.execute("SELECT id,status,url,object_path FROM responses WHERE id=?", (rid,)).fetchone() for rid in response_ids}
    db.rollback()
    db.close()
    bodies = {rid: _load_response(row, objects_root) for rid, row in responses.items()}
    output = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
              "snapshot_semantics": "One read transaction; rerun to include later durable probe results.",
              "requests_made_by_report": 0, "source_contract": SOURCE_CONTRACT,
              "jobs": [], "http_results": [], "browser_experiments": [], "products": [],
              "formula_issues": [], "one_dimension_comparisons": [], "routes": [],
              "global_dependency_proof": False, "cache_certificates_created": 0}
    routes = defaultdict(list)
    formulas = Counter()
    latest_products = {}
    for job, manifest, results in decoded:
        ledger = attempts[job["id"]]
        prior = manifest.get("prior_public_contract_requests", 0)
        prior = prior if isinstance(prior, int) and not isinstance(prior, bool) and prior >= 0 else 0
        output["jobs"].append({"id": job["id"], "run_id": job["run_id"], "status": job["status"],
            "error": _safe_code(job["error"]), "budget": job["budget"], "durable_used": job["used"],
            "attempt_ledger_count": len(ledger), "prior_public_contract_gets": prior,
            "total_recorded_attempts_including_prior_gets": job["used"] + prior,
            "remaining_job_budget": max(0, job["budget"] - job["used"]),
            "attempt_status_counts": dict(sorted(Counter(str(a["status"]) for a in ledger).items())),
            "attempt_phase_counts": dict(sorted(Counter(a["phase"] for a in ledger).items())),
            "results_count": len(results), "planned_http_requests": len(manifest.get("requests", [])),
            "planned_browser_experiments": len(manifest.get("browser_probes", [])),
            "updated_at": datetime.fromtimestamp(job["updated_at"], timezone.utc).isoformat()})
        requests = {r.get("id", str(i)): r for i, r in enumerate(manifest.get("requests", []))}
        for index, result in enumerate(results):
            ident = result.get("id")
            if "report" not in result:
                request = requests.get(ident, {})
                body = bodies.get(result.get("response_id"), {})
                row = {"job_id": job["id"], "result_index": index, "id": ident,
                       "status": result.get("status"), "code": _safe_code(result.get("code")),
                       "method": request.get("method"), "path": request.get("path"),
                       "params": {k:v for k,v in request.get("params", {}).items() if k in PUBLIC_PARAMS},
                       "cached": result.get("cached"), "response": body}
                output["http_results"].append(row)
                if body:
                    routes[(row["method"], row["path"])].append({"id": ident, "params": row["params"], **body})
                continue
            report = result["report"]
            summary = {"job_id": job["id"], "id": ident, "product_url": report.get("product_url"),
                       "status": report.get("status"), "reason": _safe_code(report.get("reason")),
                       "source_bundle_match": report.get("loaded_contract") == SOURCE_CONTRACT,
                       "capture_count": len(report.get("captures", [])), "phases": [],
                       "evidence_ids": [c.get("evidence_id") for c in report.get("captures", [])],
                       "request_errors": [{k:e.get(k) for k in ("phase", "status", "path")} for e in report.get("errors", [])]}
            for capture in report.get("captures", []):
                body = bodies.get(capture.get("evidence_id"), {})
                if body:
                    routes[(capture.get("request_method"), body.get("path"))].append({"id": ident, "phase": capture.get("phase"), "params": {k:v for k,v in parse_qsl(urlsplit(capture.get("url", "")).query, keep_blank_values=True) if k in PUBLIC_PARAMS}, **body})
            baseline = next((p for p in report.get("phases", []) if p.get("name") == "baseline"), None)
            for phase in report.get("phases", []):
                evidence = sorted(set(phase.get("evidence_ids", [])) | {c["evidence_id"] for c in report.get("captures", []) if c.get("phase") == phase.get("name") and c.get("evidence_id")})
                row = {"name": phase.get("name"), "status": phase.get("status"),
                       "reason": _safe_code(phase.get("reason")), "evidence_ids": evidence}
                if "capture" in phase:
                    formula, context = _formula(phase["capture"], report.get("loaded_contract"))
                    row.update(formula=formula, selections=context.get("selections", {}))
                    formulas[formula["comparison"]] += 1
                    if formula["comparison"] != "matched":
                        output["formula_issues"].append({"job_id": job["id"], "experiment_id": ident,
                            "product_url": summary["product_url"], "phase": phase.get("name"),
                            "evidence_ids": evidence, **formula})
                    if baseline is not None and phase is not baseline:
                        output["one_dimension_comparisons"].append({"job_id": job["id"], "experiment_id": ident,
                            "product_url": summary["product_url"], **compare_phases(baseline, phase, report, bodies)})
                summary["phases"].append(row)
            if not baseline:
                summary["baseline_status"] = "not_captured"
                output["formula_issues"].append({"job_id": job["id"], "experiment_id": ident,
                    "product_url": summary["product_url"], "phase": "baseline", "comparison": "unresolved",
                    "reasons": ["settled_baseline_not_captured"], "evidence_ids": summary["evidence_ids"]})
            else:
                summary["baseline_status"] = next(p for p in summary["phases"] if p["name"] == "baseline").get("formula", {}).get("comparison", "unresolved")
            output["browser_experiments"].append(summary)
            if summary["product_url"] in PRODUCT_URLS:
                latest_products[summary["product_url"]] = summary
    for url, title in PRODUCT_URLS.items():
        latest = latest_products.get(url)
        output["products"].append({"title": title, "product_url": url, "latest_experiment_id": latest["id"] if latest else None,
            "status": latest["status"] if latest else "not_probed", "baseline_status": latest["baseline_status"] if latest else "not_captured",
            "phases": latest["phases"] if latest else []})
    for (method, path), observations in sorted(routes.items(), key=lambda x: str(x[0])):
        valid = [r for r in observations if r.get("available")]
        output["routes"].append({"method": method, "path": path, "observation_count": len(observations),
            "distinct_evidence_count": len({r.get("evidence_id") for r in observations}),
            "distinct_raw_bodies": len({r["raw_body_sha256"] for r in valid}),
            "distinct_full_json_bodies": len({r["full_json_sha256"] for r in valid}),
            "observed_quantity_counts": sorted({r["quantity_count"] for r in valid}), "observations": observations})
    denominator = formulas["matched"] + formulas["mismatch"]
    output["formula_summary"] = {"phase_captures": sum(formulas.values()), "matched": formulas["matched"],
        "mismatch": formulas["mismatch"], "unresolved": formulas["unresolved"],
        "missing_baseline_observations": sum(e["baseline_status"] == "not_captured" for e in output["browser_experiments"]),
        "pass_rate_denominator": denominator, "pass_rate": formulas["matched"] / denominator if denominator else None,
        "definition": "Matched / (matched + mismatch) complete source-pinned controller and displayed-total captures; unresolved and missing captures excluded, separately reported."}
    return output


def markdown(report):
    summary = report["formula_summary"]
    lines = ["# Saved dependency-probe results", "", f"Generated {report['generated_at']}. Zero supplier/model requests by this report.",
        "One read transaction; rerun after later probe batches. This report creates no cache certificate or global dependency proof.", "",
        "## Attempts and outcomes", ""]
    for job in report["jobs"]:
        lines.append(f"- `{job['id']}`: {job['status']}; {job['durable_used']}/{job['budget']} job attempts + {job['prior_public_contract_gets']} prior public-bundle GETs = **{job['total_recorded_attempts_including_prior_gets']}** recorded attempts. Error: `{job['error'] or 'none'}`. Ledger statuses: `{job['attempt_status_counts']}`. Status 0/null means no completed HTTP response, not success.")
    lines += ["", "## Latest product observations", "", "| Product | Experiment | Outcome | Baseline formula |", "|---|---|---|---|"]
    for product in report["products"]:
        lines.append(f"| {product['title']} | {product['latest_experiment_id'] or 'none'} | {product['status']} | {product['baseline_status']} |")
    rate = "undefined (no eligible complete comparisons)" if summary["pass_rate"] is None else f"{summary['pass_rate']:.1%}"
    lines += ["", f"All saved phase captures: **{summary['matched']} matched, {summary['mismatch']} mismatched, {summary['unresolved']} unresolved**, plus {summary['missing_baseline_observations']} experiments without a captured baseline. Formula pass rate: **{rate}**, denominator {summary['pass_rate_denominator']}. {summary['definition']}",
        "", "## Unresolved and unsupported phases", ""]
    for experiment in report["browser_experiments"]:
        problems = [p for p in experiment["phases"] if p["status"] != "observed" or p.get("formula", {}).get("comparison") == "unresolved"]
        details = [f"{p['name']}: {p.get('reason') or ', '.join(p.get('formula', {}).get('reasons', [])) or p['status']}" for p in problems]
        if experiment["baseline_status"] == "not_captured":
            details.insert(0, "baseline not captured")
        if details:
            lines.append(f"- `{experiment['id']}`: " + "; ".join(details) + ".")
    failed = Counter(r.get("code") or "unknown" for r in report["http_results"] if r["status"] != "observed")
    lines += ["", f"Historical failed HTTP result entries (including earlier retries): `{dict(failed)}`. JSON preserves every result, formula mismatch/unresolved reason, and evidence reference.",
        "", "## Route observations", "", "Quantity counts count distinct top-level `items[].quantity` values; zero does not establish an empty valid domain. Routes aggregate different request filters; body differences alone are not dependency findings.", "", "| Method/path | Observations | Distinct full JSON bodies | Quantity counts observed |", "|---|---:|---:|---|"]
    for route in report["routes"]:
        lines.append(f"| {route['method']} `{route['path']}` | {route['observation_count']} | {route['distinct_full_json_bodies']} | {route['observed_quantity_counts']} |")
    counts = Counter(p["status"] for p in report["one_dimension_comparisons"])
    lines += ["", f"Paired dependency outcomes: `{dict(counts)}`. A pair requires two complete matching formula captures, pinned loaded source, exactly one changed selection with no other context change, and two distinct fresh uncached base-table reads. Multiple changed selections are confounded. Equal bodies prove only that recorded pair and route, never option-price independence or reusable caching.",
        "", "Rerun from `backend`:", "", "```sh", "python -m crawler.dependency_probe_report --db /absolute/path/atlas.db --objects-root /absolute/path/atlas-data --output-dir ../docs", "```", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--objects-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    report = build_report(args.db, args.objects_root, run_id=args.run_id)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for name, content in (("dependency-probe-results.json", json.dumps(report, indent=2, allow_nan=False) + "\n"),
                          ("dependency-probe-results.md", markdown(report))):
        temp = output / (name + ".tmp")
        temp.write_text(content)
        temp.replace(output / name)
    print(json.dumps({"jobs": len(report["jobs"]), "formula_summary": report["formula_summary"],
                      "output": str(output.resolve())}))


if __name__ == "__main__":
    main()
