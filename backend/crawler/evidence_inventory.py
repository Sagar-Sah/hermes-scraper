"""Read-only inventory of saved response evidence; never contacts a supplier.

Captured response observations are not the same as network requests. Exact URL
and content repeats describe this snapshot, not a safe cross-session cache key.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def source_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def route_family(url):
    path = urlsplit(url).path
    path = re.sub(r"(/products/)(\d+)(?=/|$)", r"\1{base_product_id}", path)
    return re.sub(r"(/options/)(\d+)(?=/|$)", r"\1{option_id}", path)


def public_query(url):
    values = defaultdict(list)
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if value and re.search(r"token|auth|secret|password|cookie|session|email|customer|account|^cid$", key, re.I):
            value = "<redacted>"
        values[key].append(value)
    return dict(sorted(values.items()))


def shape(value, depth=0):
    """Bounded structural signatures; values and account data are not copied."""
    if depth > 4:
        return type(value).__name__
    if isinstance(value, dict):
        if value and all(str(k).isdigit() for k in value):
            variants = {canonical(shape(v, depth + 1)) for v in value.values()}
            return {"{numeric_key}": [json.loads(v) for v in sorted(variants)]}
        return {k: shape(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        variants = {canonical(shape(v, depth + 1)) for v in value}
        return {"array_items": [json.loads(v) for v in sorted(variants)]}
    return "null" if value is None else type(value).__name__


def quantity_tables(value):
    """Extract observed finite tiers without treating option IDs as quantities."""
    tables = []

    def visit(node, path):
        if isinstance(node, list):
            tiers, modes = set(), defaultdict(set)
            for item in node:
                if not isinstance(item, dict) or isinstance(item.get("quantity"), bool):
                    continue
                raw = str(item.get("quantity", ""))
                if not raw.isdigit() or int(raw) <= 0:
                    continue
                quantity = int(raw)
                tiers.add(quantity)
                for mode in item.get("turnaroundList") or []:
                    modes[str(mode)].add(quantity)
            if tiers:
                tables.append({"path": path, "quantities": sorted(tiers),
                               "quantities_by_turnaround": {k: sorted(v) for k, v in sorted(modes.items())}})
        elif isinstance(node, dict):
            for key, child in node.items():
                if isinstance(child, (dict, list)):
                    visit(child, f"{path}.{key}")

    visit(value, "$")
    return tables


class Objects:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.missing = Counter()

    def read(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Evidence object path escapes the object root")
        try:
            return path.read_bytes()
        except OSError as exc:
            self.missing[type(exc).__name__] += 1
            return None

    def json(self, relative):
        raw = self.read(relative)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return None


class RouteStats:
    def __init__(self):
        self.observations = 0
        self.semantic = set()
        self.url_semantic = set()
        self.urls = set()
        self.request_keys = set()
        self.context_semantic = set()
        self.status = Counter()
        self.parameters = defaultdict(set)
        self.path_parameters = defaultdict(set)
        self.shapes = Counter()
        self.tiers = set()
        self.modes = defaultdict(set)
        self.evidence = []
        self.run_linked = 0
        self.validators = Counter()

    def add(self, row, data, linked=False):
        self.observations += 1
        digest = row["semantic_hash"] or row["raw_hash"] or row["id"]
        self.semantic.add(digest)
        self.url_semantic.add((row["url"], row["status"], digest))
        self.urls.add(row["url"])
        self.request_keys.add(row["request_key"])
        self.context_semantic.add((row["request_key"], row["status"], digest))
        self.status[str(row["status"])] += 1
        self.run_linked += bool(linked)
        self.validators["etag_present"] += bool(row["etag"])
        self.validators["last_modified_present"] += bool(row["modified"])
        for key, values in public_query(row["url"]).items():
            self.parameters[key].update(values)
        for key, pattern in (("base_product_id", r"/products/(\d+)(?:/|$)"),
                             ("option_id", r"/options/(\d+)(?:/|$)")):
            match = re.search(pattern, urlsplit(row["url"]).path)
            if match:
                self.path_parameters[key].add(match.group(1))
        if data is not None:
            self.shapes[canonical(shape(data))] += 1
            for table in quantity_tables(data):
                self.tiers.update(table["quantities"])
                for mode, quantities in table["quantities_by_turnaround"].items():
                    self.modes[mode].update(quantities)
        if len(self.evidence) < 3:
            self.evidence.append(row["id"])

    def report(self):
        exact = len(self.url_semantic)
        return {
            "captured_response_observations": self.observations,
            "direct_run_linked_observations": self.run_linked,
            "distinct_semantic_responses": len(self.semantic),
            "distinct_exact_urls": len(self.urls),
            "distinct_stored_request_keys": len(self.request_keys),
            "distinct_stored_key_status_semantic_triples": len(self.context_semantic),
            "same_stored_key_status_content_repeats": self.observations - len(self.context_semantic),
            "distinct_url_status_semantic_triples": exact,
            "same_url_status_content_repeats": self.observations - exact,
            "same_url_status_content_repeat_fraction": round((self.observations - exact) / self.observations, 6),
            "observations_per_distinct_semantic_response": round(self.observations / len(self.semantic), 4),
            "hypothetical_requests_if_every_exact_repeat_were_reusable": exact,
            "safe_cache_savings_lower_bound": 0,
            "status_counts": dict(self.status),
            "parameter_values": {k: sorted(v) for k, v in sorted(self.parameters.items())},
            "path_parameter_values": {k: sorted(v) for k, v in sorted(self.path_parameters.items())},
            "http_method": "not_recorded",
            "request_body": "not_recorded",
            "response_shapes": [{"shape": json.loads(k), "observations": n} for k, n in self.shapes.most_common()],
            "observed_quantities_union": sorted(self.tiers),
            "observed_quantities_by_turnaround_union": {k: sorted(v) for k, v in sorted(self.modes.items())},
            "validators": dict(self.validators),
            "sample_evidence_ids": self.evidence,
        }


def build_inventory(database, object_root, *, run_id=None):
    database = Path(database).resolve()
    objects = Objects(object_root)
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        products = [dict(r) for r in connection.execute(
            "SELECT id,run_id,url,title,row_count,request_count,status FROM products" +
            (" WHERE run_id=?" if run_id else ""), (run_id,) if run_id else ())]
        by_id = {p["id"]: p for p in products}
        families = defaultdict(list)
        for product in products:
            if urlsplit(product["url"]).scheme in {"http", "https"}:
                families[source_url(product["url"])].append(product)
        direct = defaultdict(lambda: defaultdict(set))
        for event in connection.execute("SELECT run_id,type,payload FROM events" +
                                        (" WHERE run_id=?" if run_id else ""), (run_id,) if run_id else ()):
            payload = json.loads(event["payload"])
            pid, evidence = payload.get("product_id"), payload.get("evidence_id")
            if pid in by_id and evidence:
                direct[evidence][source_url(by_id[pid]["url"])].add("event:" + event["type"])
        for batch in connection.execute("SELECT product_id,object_path FROM batches"):
            if batch["product_id"] not in by_id:
                continue
            raw = objects.read(batch["object_path"])
            if raw is None:
                continue
            product = source_url(by_id[batch["product_id"]]["url"])
            for line in gzip.decompress(raw).splitlines():
                row = json.loads(line)
                for evidence in row.get("evidence_ids", []):
                    direct[evidence][product].add("durable_row_evidence")

        # Only known product pages and source calculator routes are inventoried.
        all_rows = [dict(r) for r in connection.execute("SELECT * FROM responses ORDER BY created_at,id")]
        rows = [r for r in all_rows if "/service/rest/" in urlsplit(r["url"]).path or source_url(r["url"]) in families]
        types, base_products = defaultdict(set), defaultdict(set)
        data_by_object = {}

        def payload(row):
            path = row["object_path"]
            if path not in data_by_object:
                data_by_object[path] = objects.json(path)
            return data_by_object[path]

        for row in rows:
            data = payload(row)
            if isinstance(data, dict) and data.get("kind") == "visible_product_quote":
                url = source_url(str(data.get("url", row["url"])))
                if url in families and data.get("product_type") is not None:
                    types[str(data["product_type"])].add(url)
            match = re.search(r"/products/(\d+)(?:/|$)", urlsplit(row["url"]).path)
            if match:
                base_products[match.group(1)].update(direct.get(row["id"], {}))
        for row in rows:
            query = dict(parse_qsl(urlsplit(row["url"]).query))
            data = payload(row)
            if urlsplit(row["url"]).path.rstrip("/") == "/service/rest/v1/products" and 200 <= row["status"] < 300:
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    candidates = set(types.get(query.get("productType"), set())) | set(direct.get(row["id"], {}))
                    for item in data["items"]:
                        if isinstance(item, dict) and item.get("id") is not None:
                            base_products[str(item["id"])].update(candidates)

        grouped = defaultdict(RouteStats)
        attribution = Counter()
        lookup_domains, support_domains, finishing = {}, {}, {}
        response_index = []
        for row in rows:
            data = payload(row)
            parsed = urlsplit(row["url"])
            query = dict(parse_qsl(parsed.query))
            evidence = defaultdict(set)
            for product, methods in direct.get(row["id"], {}).items():
                evidence[product].update(methods)
            if isinstance(data, dict) and data.get("kind") == "visible_product_quote":
                url = source_url(str(data.get("url", row["url"])))
                if url in families:
                    evidence[url].add("observed_dom_product_url")
            for product in types.get(query.get("productType"), set()):
                evidence[product].add("explicit_product_type_query")
            match = re.search(r"/products/(\d+)(?:/|$)", parsed.path)
            if match:
                for product in base_products.get(match.group(1), set()):
                    evidence[product].add("base_id_bound_by_direct_evidence_or_lookup")
            product = next(iter(evidence)) if len(evidence) == 1 else "ambiguous" if evidence else "unattributed"
            attribution[product if product in {"ambiguous", "unattributed"} else "single_source_product"] += 1
            is_dom = isinstance(data, dict) and data.get("kind") == "visible_product_quote"
            family = "DOM_QUOTE" if is_dom else route_family(row["url"])
            grouped[(product, family)].add(row, data, bool(direct.get(row["id"])))
            response_index.append({"id": row["id"], "attribution": product,
                                   "candidate_products": {k: sorted(v) for k, v in sorted(evidence.items())},
                                   "direct_run_linked": bool(direct.get(row["id"])), "route_family": family,
                                   "stored_request_signature": row["request_key"],
                                   "exact_url_sha256": hashlib.sha256(row["url"].encode()).hexdigest()})
            if product in {"ambiguous", "unattributed"} or not 200 <= row["status"] < 300:
                continue
            if parsed.path.rstrip("/") == "/service/rest/v1/products" and isinstance(data, dict) and isinstance(data.get("items"), list):
                ids = sorted({str(item["id"]) for item in data["items"] if isinstance(item, dict) and item.get("id") is not None})
                if ids:
                    key = canonical([product, public_query(row["url"]), ids])
                    item = lookup_domains.setdefault(key, {"product": product, "query": public_query(row["url"]), "base_product_ids": ids, "observations": 0, "evidence_ids": []})
                    item["observations"] += 1
                    if len(item["evidence_ids"]) < 3:
                        item["evidence_ids"].append(row["id"])
            if parsed.path.endswith("/quantities"):
                tables = quantity_tables(data)
                key = canonical([product, public_query(row["url"]), tables, family])
                item = support_domains.setdefault(key, {"product": product, "route_family": family, "query": public_query(row["url"]), "tables": tables, "observations": 0, "evidence_ids": []})
                item["observations"] += 1
                if len(item["evidence_ids"]) < 3:
                    item["evidence_ids"].append(row["id"])
            if is_dom:
                controls = {c["key"]: c for c in data.get("controls", []) if "key" in c}
                quantity = controls.get("quantity", {}).get("value")
                if quantity is None:
                    continue
                context = {k: str(c.get("value")) for k, c in controls.items() if k not in {"quantity", "raisedprint", "spotcoating"}}
                services = {entry.get("key"): [v.get("value") for v in entry.get("selected", [])] for entry in data.get("service_context", [])}
                for finish in ("raisedprint", "spotcoating"):
                    key = canonical([product, context, services, finish])
                    item = finishing.setdefault(key, {"product": product, "context": context, "observed_services": services, "control": finish, "quantities": {}})
                    observation = item["quantities"].setdefault(str(quantity), {"visible": 0, "not_in_visible_controls": 0, "offered_values": set(), "selected_values": set(), "evidence_ids": []})
                    observation["visible" if finish in controls else "not_in_visible_controls"] += 1
                    if finish in controls:
                        control = controls[finish]
                        observation["selected_values"].add(str(control.get("value")))
                        observation["offered_values"].update(str(c.get("value")) for c in control.get("choices", []) if not c.get("placeholder") and not c.get("disabled"))
                    if len(observation["evidence_ids"]) < 2:
                        observation["evidence_ids"].append(row["id"])
        for item in finishing.values():
            for observation in item["quantities"].values():
                observation["offered_values"] = sorted(observation["offered_values"])
                observation["selected_values"] = sorted(observation["selected_values"])
        return {
            "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "database": str(database), "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "run_id_for_direct_links": run_id, "network_calls": 0,
            "database_response_rows": len(all_rows), "included_response_rows": len(rows),
            "excluded_response_rows": len(all_rows) - len(rows),
            "scope": "All saved matching source-product responses in the supplied snapshot; run membership only when directly linked",
            "limitations": [
                "Responses omit HTTP method, request body, session/pricing context and direct run/product IDs; capture count is not network request count.",
                "Semantic hashes alone do not establish reusable cache equivalence. Safe cache savings lower bound is zero until context and freshness are proven.",
                "Exact URL/status/content repeats yield only an optimistic hypothetical retained-observation count, not a safe request budget.",
                "Observed unions and successful lookup tuples are not a complete catalog and are not Cartesian-product domains.",
                "DOM control absence does not prove unsupported configuration; controls can depend on prior state, quantity, production or hidden services.",
                "Finishing clue contexts omit both finishing controls to compare visibility; companion finishing selections can confound a causal interpretation. Inspect referenced DOM evidence before deriving dependencies.",
                "Temporal adjacency is never used to attribute a response to a product or run.",
            ],
            "products": products, "attribution_counts": dict(attribution),
            "routes": [{"product": product, "route_family": family, **stats.report()} for (product, family), stats in sorted(grouped.items())],
            "successful_lookup_domains": list(lookup_domains.values()),
            "observed_quantity_support_domains": list(support_domains.values()),
            "finishing_visibility_evidence": list(finishing.values()),
            "response_attribution": response_index,
            "missing_objects": dict(objects.missing),
        }
    finally:
        connection.close()


def render_report(inventory):
    lines = ["# Offline evidence inventory", "", f"Database SHA-256: `{inventory['database_sha256']}`.",
             "", "This report made **zero supplier or model calls**. Counts below are saved response observations, not all network requests.", "",
             "| Product / route family | Captures | Distinct contents | Exact URL/status/content repeats | Direct run links |",
             "|---|---:|---:|---:|---:|"]
    for row in inventory["routes"]:
        name = row["product"].rsplit("/", 2)[-2] if row["product"].endswith("/order") else row["product"]
        lines.append(f"| {name}: `{row['route_family']}` | {row['captured_response_observations']} | {row['distinct_semantic_responses']} | {row['same_url_status_content_repeats']} | {row['direct_run_linked_observations']} |")
    lines.extend(["", "## Observed domains", "", f"- {len(inventory['successful_lookup_domains'])} distinct successful lookup query/base-ID tuples.",
                  f"- {len(inventory['observed_quantity_support_domains'])} observed quantity-support query/body domains.",
                  f"- {len(inventory['finishing_visibility_evidence'])} finishing-control/context groups.", "",
                  "Full parameter values, structural response signatures, quantity/turnaround domains, source evidence IDs and attribution reasons are in the JSON inventory. None establishes full-domain coverage.",
                  "", "## Limits and safe interpretation", ""])
    lines.extend("- " + text for text in inventory["limitations"])
    return "\n".join(lines) + "\n"
