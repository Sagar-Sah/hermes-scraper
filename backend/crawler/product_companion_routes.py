"""Authenticated Product Companion API."""
from __future__ import annotations

import csv
import io
import json
import zipfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, SecretStr


class PairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    campaign_id: str
    source_url: str
    storefront_url: str


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: StrictInt | None = None
    auto_correct: StrictBool = False


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved_proposal_indexes: list[StrictInt]


class CandidateMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str


class ConnectorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    token: SecretStr


class WritePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_indexes: list[StrictInt]


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str


def _error(exc):
    if isinstance(exc, KeyError):
        return HTTPException(404, str(exc).strip("'"))
    return HTTPException(422, str(exc))


def companion_router(service, authenticated):
    router = APIRouter(prefix="/api/v1/companion", dependencies=[Depends(authenticated)])

    @router.get("/connector")
    def connector_status():
        return service().writes.status()

    @router.put("/connector")
    async def configure_connector(payload: ConnectorRequest):
        try:
            return await service().writes.configure(payload.base_url, payload.token.get_secret_value())
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.post("/audits/{audit_id}/write-preview")
    async def write_preview(audit_id: str, payload: WritePreviewRequest):
        try:
            return await service().writes.preview(audit_id, payload.proposal_indexes)
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.get("/pairs/{pair_id}/changes")
    def changes(pair_id: str):
        try:
            return {"items": service().writes.history(pair_id)}
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.post("/changes/{change_id}/apply")
    async def apply_change(change_id: str, payload: ApplyRequest):
        try:
            return await service().writes.apply(change_id, payload.expected_revision)
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.post("/changes/{change_id}/rollback")
    async def rollback_change(change_id: str):
        try:
            return await service().writes.rollback(change_id)
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.get("/pairs")
    def pairs():
        return {"items": service().list_pairs()}

    @router.post("/import-confirmed-business-cards")
    def import_confirmed():
        try:
            return service().import_confirmed_business_cards()
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.get("/business-card-candidates")
    async def candidates():
        try:
            return await service().discover_business_cards()
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            raise _error(exc) from exc

    @router.get("/candidates/{product_id}")
    def candidate_report(product_id: str):
        if product_id != "6862":
            raise HTTPException(404, "candidate_report_not_found")
        path = service().root / "candidates" / "6862.json"
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, "candidate_report_not_found")
        from .product_companion import selling_scope_for_product
        return {**json.loads(path.read_text()), "selling_scope": selling_scope_for_product(product_id)}

    @router.post("/candidates/{product_id}/discover")
    async def discover_candidate_mapping(product_id: str, payload: CandidateMappingRequest):
        from .product_companion_mapping import discover_candidate
        try:
            return await discover_candidate(service(), product_id, payload.source_url)
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.post("/pairs")
    def create_pair(payload: PairRequest):
        try:
            return service().add_pair(payload.model_dump())
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.get("/pairs/{pair_id}")
    def pair(pair_id: str):
        try:
            return service().get_pair(pair_id)
        except KeyError as exc:
            raise _error(exc) from exc

    @router.post("/pairs/{pair_id}/audits")
    async def audit(pair_id: str):
        try:
            return await service().audit(pair_id)
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.post("/pairs/{pair_id}/refresh-child-tables")
    async def refresh_child_tables(pair_id: str):
        try:
            return await service().refresh_child_tables(pair_id)
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.get("/pairs/{pair_id}/audits")
    def history(pair_id: str):
        try:
            return {"items": service().history(pair_id)}
        except KeyError as exc:
            raise _error(exc) from exc

    @router.put("/pairs/{pair_id}/schedule")
    def schedule(pair_id: str, payload: ScheduleRequest):
        try:
            return service().set_schedule(pair_id, hours=payload.hours, auto_correct=payload.auto_correct)
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.get("/audits/{audit_id}")
    def get_audit(audit_id: str):
        try:
            return service().get_audit(audit_id)
        except KeyError as exc:
            raise _error(exc) from exc

    @router.post("/audits/{audit_id}/review")
    def review(audit_id: str, payload: ReviewRequest):
        try:
            return service().review(audit_id, payload.approved_proposal_indexes)
        except (ValueError, KeyError) as exc:
            raise _error(exc) from exc

    @router.get("/audits/{audit_id}/export")
    def export(audit_id: str):
        try:
            record = service().get_audit(audit_id)
            source_files = service().saved_source_evidence_for_audit(record)
        except (KeyError, ValueError) as exc:
            raise _error(exc) from exc
        data = io.BytesIO()
        report = record["report"]
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("audit.json", json.dumps(record, indent=2, ensure_ascii=False))
            field_rows = []
            for location, check in report.get("field_checks", {}).items():
                for finding in check.get("findings", []):
                    field_rows.append({"location": location, "status": finding["status"], "purpose": finding["purpose"],
                        "reason": finding["reason"], "dom_references": json.dumps([item["dom_ref"] for item in finding["observations"]]),
                        "purchase_scope": json.dumps(finding["purchase_scope"], sort_keys=True),
                        "product_owners": json.dumps([item.get("product_owner", {}) for item in finding["observations"]], sort_keys=True),
                        "projection_sha256": finding["evidence"].get("projection_sha256", ""),
                        "proposed_correction": finding["proposed_correction"]["instruction"], "review_required": "yes", "automatic": "no"})
            if field_rows:
                output = io.StringIO()
                writer = csv.DictWriter(output, list(field_rows[0]))
                writer.writeheader()
                writer.writerows(field_rows)
                bundle.writestr("duplicate-field-review.csv", output.getvalue())
            bundle.writestr("README.txt", "Evidence-backed comparison only. Supplier prices may be historical; this is not a WooCommerce import. Current supplier page and storefront hashes are in audit.json; raw browser pages and account sessions are excluded.\n")
            for name, raw in source_files.items():
                bundle.writestr("saved-evidence/" + name, raw)
            for key in ("missing_site_rows", "extra_site_rows", "cost_mismatches"):
                rows = report.get(key, [])
                if not rows:
                    continue
                output = io.StringIO()
                fields = sorted({field for row in rows for field in row})
                writer = csv.DictWriter(output, fields)
                writer.writeheader()
                for row in rows:
                    writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v for k, v in row.items()})
                bundle.writestr(key + ".csv", output.getvalue())
        return Response(data.getvalue(), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="hermes-companion-{audit_id}.zip"'})

    return router
