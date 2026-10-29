"""Review directly observed source controls, never infer full option coverage."""
from __future__ import annotations

from datetime import datetime
import re
import time

from .product_companion_fields import normalize_field_inventory


SOURCE_ALIASES = {
    "width": "width", "height": "height", "stock": "stock", "paperstock": "stock",
    "colorspec": "colorspec", "printing": "colorspec",
    "flutedirection": "flute_direction", "flutedirections": "flute_direction",
    "finishoptions": "finish_options", "finishingoptions": "finish_options",
    "grommet": "grommets", "grommets": "grommets", "hstake": "h_stakes", "hstakes": "h_stakes",
}
SITE_ALIASES = {
    **SOURCE_ALIASES, "pigfflute": "flute_direction", "pigfstakespersign": "h_stakes",
    "stakespersign": "h_stakes", "quantity": "quantity", "pigfrunsize": "quantity",
    "size": "size", "sets": "sets", "piecesperset": "pieces_per_set",
}


def _label(value, limit=180):
    return " ".join(str(value or "").split())[:limit]


def _key(value):
    return re.sub(r"[^a-z]", "", _label(value).casefold())


def _current(value, now):
    try:
        moment = datetime.fromisoformat(value)
        return moment.utcoffset() is not None and 0 <= now - moment.timestamp() <= 6 * 3600
    except (TypeError, ValueError):
        return False


def observed_source_controls(diagnosis, inventory, *, source_url, storefront_url,
                             product_id, diagnosis_sha256, now=None):
    """Compare source select purposes with one current rendered purchase state.

    A missing witness is a review item, not proof that a merchant should offer
    the option. Hidden, scoped or unidentified controls cannot prove absence.
    """
    now = time.time() if now is None else now
    result = {"schema": "hermes-companion-observed-controls/v1", "status": "unknown",
              "reason": "source_or_storefront_observation_unverified", "controls": [], "findings": [],
              "source_domain_complete": False, "automatic_changes_allowed": False,
              "coverage": {"scope": "current_source_and_storefront_state_only",
                           "other_conditional_states": "unknown", "merchant_selling_intent": "unknown",
                           "prices": "unknown", "quantities": "unknown", "production": "unknown",
                           "non_select_controls": "unassessed"}}
    if (not isinstance(diagnosis, dict) or diagnosis.get("url") != source_url
            or diagnosis.get("supplier_id") != "fourover" or diagnosis.get("status") != "ready"
            or diagnosis.get("session_status") == "login_required"
            or not _current(diagnosis.get("created_at"), now)
            or not isinstance(diagnosis.get("fields"), list) or len(diagnosis["fields"]) > 256
            or any(not isinstance(field, dict) for field in diagnosis["fields"])
            or not isinstance(diagnosis_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", diagnosis_sha256)):
        return result
    try:
        site = normalize_field_inventory(inventory)
    except (TypeError, ValueError):
        return result
    if (site["observation_kind"] != "rendered_dom" or site["url"] != storefront_url
            or site["product_id"] != str(product_id) or site["truncated"]
            or not _current(site["observed_at"], now)):
        return result
    result["evidence"] = {
        "source_diagnosis_id": diagnosis.get("id"), "source_diagnosis_sha256": diagnosis_sha256,
        "source_url": source_url, "source_observed_at": diagnosis["created_at"],
        "source_diagnosis_revision": diagnosis.get("revision"),
        "storefront_url": storefront_url, "storefront_observed_at": site["observed_at"],
        "storefront_projection_sha256": site["projection_sha256"], "product_id": str(product_id),
    }
    result["coverage"]["source_capture"] = ("limited_observed_controls_only"
        if "diagnosis_limits_reached" in (diagnosis.get("warnings") or []) else "observed_controls_only")
    active, uncertain, inactive = {}, [], {}
    for control in site["controls"]:
        owner = control["product_owner"]
        if not owner["verified"] or not owner["primary"] or owner["product_id"] != str(product_id):
            if control["visible"] is not False:
                uncertain.append(control["dom_ref"])
            continue
        purpose = SITE_ALIASES.get(_key(control["purpose"]))
        if control["visible"] is False or not control["enabled"] or not control["editable"]:
            if purpose:
                inactive.setdefault(purpose, []).append(control["dom_ref"])
            continue
        if control["visible"] is not True or control["scopes"] or purpose is None:
            uncertain.append(control["dom_ref"])
            continue
        active.setdefault(purpose, []).append(control["dom_ref"])
    unclassified = []
    for field in diagnosis["fields"]:
        if field.get("kind") != "select" or field.get("configurable") is not True or field.get("disabled") is not False:
            continue
        purpose = SOURCE_ALIASES.get(_key(field.get("label")))
        if purpose is None:
            unclassified.append(_label(field.get("label")))
            continue
        choices = field.get("choices")
        if not isinstance(choices, list) or not choices or len(choices) > 500:
            continue
        options = [{"value": _label(choice["value"]), "label": _label(choice.get("label"))}
                   for choice in choices if isinstance(choice, dict) and choice.get("value")
                   and choice.get("disabled") is False]
        if not options:
            continue
        refs = active.get(purpose, [])
        ambiguous = uncertain or inactive.get(purpose, []) or (purpose in {"width", "height"} and active.get("size"))
        status = "match" if refs else "unknown" if ambiguous else "mismatch"
        item = {"purpose": purpose, "status": status, "source_label": _label(field.get("label")),
                "source_field_key": _label(field.get("key"), 100), "source_choices": options,
                "storefront_dom_references": refs, "inactive_storefront_dom_references": inactive.get(purpose, []),
                "reason": "corresponding_control_observed" if refs else "possible_equivalent_control_requires_review" if ambiguous
                          else "declared_supplier_axis_without_matching_current_storefront_control",
                "merchant_selling_intent": "unknown", "conditional_equivalence": "unknown"}
        result["controls"].append(item)
        if status == "mismatch":
            result["findings"].append({**item, "requires_review": True, "automatic": False,
                "proposed_correction": "Review whether this source option belongs in the online offer; verify its conditional rules and price before adding a control."})
    result["unclassified_source_select_labels"] = unclassified
    result["unverified_storefront_dom_references"] = sorted(set(uncertain))
    statuses = {item["status"] for item in result["controls"]}
    result["status"] = "mismatch" if "mismatch" in statuses else "unknown" if "unknown" in statuses or unclassified or not statuses else "match"
    result["reason"] = "observed_control_purposes_only_no_complete_domain_or_selling_intent_claim"
    return result
