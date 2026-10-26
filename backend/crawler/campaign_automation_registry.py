"""Connect durable campaign work to registered supplier drivers, without AI."""
from .campaign_automation import CampaignAutomation, Profile
from .campaign_automation_bindings import active_associations, family_profile
from . import supplier_profile_executor as printograph
from . import fourover_automatic_profile as fourover


def _plan_only(result):
    # Diagnostics can accompany a profile's discovery result. The queue accepts
    # only its finite job intents and blockers, never an inferred price table.
    return {"configurations": result["configurations"], "blockers": result["blockers"]}


def build_campaign_automation(store, campaigns, diagnoses, browser_pool, *, browser_run_callbacks=None):
    def printograph_supports(association):
        try:
            printograph.association_for(printograph.PROFILE_ID, association)
            return True
        except (ValueError, LookupError):
            return False

    pg = Profile(
        printograph.PROFILE_ID, "printograph", printograph_supports,
        lambda association, maximum: _plan_only(printograph.discover(
            printograph.PROFILE_ID, association, maximum)),
        printograph.normalize_configuration,
        printograph.queue_handler(diagnoses), priority=100,
    )

    def fourover_supports(association):
        try:
            fourover.association_context(association)
            return True
        except fourover.Held:
            return False

    async def four_execute(association, configuration, *, check_scope):
        result = await fourover.execute_fourover_profile_quote(
            store, browser_pool, association, configuration, check_scope)
        result = dict(result)
        result["reason"] = result.get("reason_code") or result.get("status")
        return result

    four = family_profile(
        profile_id=fourover.PROFILE_ID, supplier="4over",
        supports=fourover_supports,
        discover=lambda association, maximum: _plan_only(
            fourover.discover_fourover_profile(store, association, maximum)),
        normalize=fourover.normalize_configuration, execute=four_execute,
    )
    # The driver's own page/quote deadlines and durable chunk bounds are
    # shorter. This outer bound also covers paced cold browser startup.
    profiles = [pg, four]
    if browser_run_callbacks is not None:
        from .printograph_browser_profile import BrowserRunProfile, PDF_PROFILE_ID, PDF_MODE, PDF_SERVICE_POLICY
        if set(browser_run_callbacks) != {'create', 'read', 'control', 'find_owned'} or not all(callable(value) for value in browser_run_callbacks.values()):
            raise ValueError('Native browser Run callbacks are incomplete')
        browser_driver = BrowserRunProfile(store.root / 'printograph-owned-browser-runs', **browser_run_callbacks, chunk_seconds=300)
        profiles.append(browser_driver.profile())
        pdf_driver = BrowserRunProfile(store.root / 'printograph-owned-browser-runs', **browser_run_callbacks, chunk_seconds=300,
                                       profile_id=PDF_PROFILE_ID, mode=PDF_MODE, service_policy=PDF_SERVICE_POLICY)
        profiles.append(pdf_driver.profile())
    return CampaignAutomation(
        store.root, profiles,
        lambda campaign_id: active_associations(campaigns, campaign_id),
        capacity=8, call_timeout=600,
    )
