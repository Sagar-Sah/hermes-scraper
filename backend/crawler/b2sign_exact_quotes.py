"""Durable, exact-selection B2Sign quote lookup for unfinished campaign items.

The source catalog establishes option/size/production membership. Only a
saved or newly requested supplier response establishes a price. This module
never treats a server price response as shipping or cart availability proof.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from .b2sign_harvest import open_size_key, prepare_candidate


class B2SignExactQuoteManager:
    CUSTOMER_QUOTE_MAX_AGE_SECONDS = 300

    def __init__(self, store, harvest_manager):
        self.root = Path(store.root) / "b2sign-harvests"
        self.harvests = harvest_manager
        self.lock = asyncio.Lock()

    def _saved(self, candidate_id):
        quoted, pending, attention = [], [], []
        for manifest_path in self.root.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text())
                if not any(row.get("candidate_id") == candidate_id for row in manifest.get("candidates", [])):
                    continue
                report = self.harvests.get(manifest_path.parent.name)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
            status = report.get("status")
            if status in {"opening", "running"}:
                pending.append(report["id"])
            elif status == "completed" and not report.get("warnings"):
                quoted.extend((report["id"], row) for row in report.get("results", [])
                              if row.get("candidate_id") == candidate_id and row.get("status") == "quoted")
            elif status in {"attention", "completed"}:
                attention.append(report["id"])
        return quoted, pending, attention

    @staticmethod
    def _age_seconds(row):
        value = row.get('captured_at')
        if not isinstance(value, str):
            return None
        try:
            timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        if timestamp.tzinfo is None:
            return None
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        return age if age >= 0 else None

    async def quote(self, catalog, candidate, *, require_fresh=False):
        safe = prepare_candidate(catalog, candidate)
        native_size_required = open_size_key(catalog) is not None
        async with self.lock:
            quoted, pending, attention = self._saved(safe["candidate_id"])
            # A customer-facing quote must not silently reuse an old audit
            # observation. Return a pending refresh through the ordinary shared
            # source gate, while keeping historical quotes for conflict review.
            if require_fresh and pending:
                return {"status": "pending", "candidate_id": safe["candidate_id"],
                        "harvest_id": pending[0], "final_eligible": False}
            eligible = quoted
            if require_fresh:
                eligible = [(harvest_id, row) for harvest_id, row in quoted
                            if (age := self._age_seconds(row)) is not None
                            and age <= self.CUSTOMER_QUOTE_MAX_AGE_SECONDS
                            and (not native_size_required or row.get("native_size_bound") is True)]
            if eligible:
                prices = {(str(row.get("quote", {}).get("unit_price")),
                           str(row.get("quote", {}).get("subtotal"))) for _, row in eligible}
                if len(prices) != 1 or any("None" in pair for pair in prices):
                    return {"status": "conflict_requires_review", "candidate_id": safe["candidate_id"],
                            "harvest_ids": sorted({harvest_id for harvest_id, _ in eligible}),
                            "final_eligible": False}
                harvest_id, row = max(eligible, key=lambda item: item[1].get("captured_at") or "")
                age = self._age_seconds(row)
                return {"status": "quoted", "candidate_id": safe["candidate_id"],
                        "source_url": safe["source_url"], "source_product_id": safe["source_product_id"],
                        "contract_sha256": safe["contract_sha256"], "values": safe["values"],
                        "quantity": safe["quantity"], "turnaround": safe["turnaround"],
                        "production_label": safe["production_label"],
                        "unit_price": row["quote"]["unit_price"],
                        "subtotal": row["quote"]["subtotal"],
                        "captured_at": row.get("captured_at"), "harvest_id": harvest_id,
                        "price_age_seconds": age,
                        "customer_quote_fresh": bool(require_fresh and age is not None and age <= self.CUSTOMER_QUOTE_MAX_AGE_SECONDS),
                        "native_selection_bound": row.get("native_selection_bound") is True,
                        "native_size_bound": row.get("native_size_bound") is True,
                        "source_fulfillment_mode": safe.get("source_fulfillment_mode"),
                        "shipping_availability": "unverified", "cart_availability": "unverified",
                        "final_eligible": False}
            if pending:
                return {"status": "pending", "candidate_id": safe["candidate_id"],
                        "harvest_id": pending[0], "final_eligible": False}
            if attention:
                return {"status": "attention", "candidate_id": safe["candidate_id"],
                        "harvest_id": attention[0], "final_eligible": False}
            candidates = [candidate]
            # The ordinary page cannot initialize nested hole geometry from
            # its URL. Bootstrap the observed page with its native "None"
            # option, then quote the complete custom geometry in the same
            # pinned document. Both requests remain source-scoped and fenced.
            if "holespunch_CUSTOM_VALUE" in safe["values"]:
                baseline = copy.deepcopy(candidate)
                baseline["values"]["holespunch"] = "0"
                baseline["values"].pop("holespunch_CUSTOM_VALUE")
                prepare_candidate(catalog, baseline)
                candidates = [baseline, candidate]
            # Step-and-repeat item 18 renders a low-quantity subtotal without
            # sending the native request needed to capture the session protocol.
            # Quantity 11 is an observed source-supported bootstrap for that
            # same complete selection; it must precede the requested 1-10.
            if str(catalog.get("source_product_id")) == "18" and safe["quantity"] <= 10:
                bootstrap = copy.deepcopy(candidate)
                bootstrap["quantity"] = 11
                prepare_candidate(catalog, bootstrap)
                candidates = [bootstrap, *candidates]
            harvest = await self.harvests.start(catalog, candidates)
            return {"status": "pending", "candidate_id": safe["candidate_id"],
                    "harvest_id": harvest["id"], "final_eligible": False}
