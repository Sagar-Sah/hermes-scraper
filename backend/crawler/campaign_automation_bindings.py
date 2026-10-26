"""Pure registration glue for reviewed family drivers; never imports app/store.

execute(association, configuration, check_scope=...) belongs to each supplier
profile. It retains all source guards. This adapter neither computes prices nor
upgrades a profile's coverage into a product-finalization claim.
"""
from copy import deepcopy
from .campaign_automation import Profile, access_reason


def family_profile(*, profile_id, supplier, supports, discover, normalize, execute, priority=100):
    async def invoke(task, check_scope):
        await check_scope()
        raw = await execute(deepcopy(task["association"]), deepcopy(task["configuration"]),
                            check_scope=check_scope)
        await check_scope()
        if not isinstance(raw, dict):
            return {"state": "held", "reason": "invalid_profile_receipt"}
        status = raw.get("status")
        if status in {"quoted", "matched_current_source_quote"}:
            return {"state": "completed", "result_reference": raw.get("result_reference")}
        if status == "continuation_required":
            return {"state": "continue", "result_reference": raw.get("result_reference"),
                    "progress_count": raw.get("progress_count"), "progress_token": raw.get("progress_token"),
                    "delay_seconds": raw.get("delay_seconds", 0)}
        if status in {"access_required", "login_required"}:
            return {"state": "access_required", "reason": access_reason(raw.get("reason_code") or raw.get("reason")) if status == "access_required" else "supplier_authentication_required"}
        return {"state": "held", "reason": raw.get("reason_code") or raw.get("reason", status)}
    return Profile(profile_id, supplier, supports, discover, normalize, invoke, priority=priority)


def active_associations(campaigns, campaign_id):
    """Scope planning to the current group; do not revive excluded/old work."""
    campaign = campaigns.get(campaign_id)
    active = set(campaign.get("active_group_product_ids") or [])
    result = []
    for product in campaign.get("products", []):
        if (product.get("id") not in active or product.get("stage") in {"excluded", "finalized"}
                or product.get("excluded_at") or not product.get("supplier_url")):
            continue
        # Use the campaign's same current-group guard, including its exclusions.
        campaigns.require_active_group(campaign_id, product["id"])
        result.append({"campaign_id": campaign_id, "product_id": product["id"],
                       "supplier": product["supplier"], "source_url": product["supplier_url"]})
    return result
