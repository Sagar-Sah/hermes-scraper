"""Prospective type38 first-quantity capture; existing journal policy unchanged.

Controller context can admit closed numeric native responses before the Process
UI is mounted. Only the ordinary visible after-state can certify those prices.
No saved/finished epoch can enter this path, and this module performs no I/O.
"""
from copy import deepcopy
import asyncio
import base64

from .price_contract import normalize_price_context, staggered_literal_base_query, STAGGERED_LITERAL_BASE_QUERY
from .printograph_price_journal import PriceJournal, STAGGERED_FAMILY, JournalError, MAX_BODY


async def read_initial_body(observer, identifier, provider):
    """Stage an already arrived bounded body; commit only after both guards.

    Network.getResponseBody observes an existing owned request, not a source
    fetch. Unknown/drifted context drops staged data without a projection/hash.
    """
    journal=observer.journal
    result=body=raw=None
    try:
        record=journal._requests.get(identifier)
        if (observer._closed or journal._invalid or not record or record.done
                or record.cached or not record.finish_order or record.status!=200):return
        if not callable(provider):raise JournalError('initial_context_provider_required')
        journal.authorize_initial_body(identifier,await provider())
        result=await observer._session.send('Network.getResponseBody',{'requestId':identifier})
        if observer._closed or journal._invalid:raise JournalError('initial_quantity_not_provisional')
        body,encoded=result.get('body'),result.get('base64Encoded')
        if type(body) is not str or type(encoded) is not bool:raise JournalError('body_decode_failed')
        if len(body)>(4*((MAX_BODY+2)//3) if encoded else MAX_BODY):raise JournalError('response_body_bound')
        raw=base64.b64decode(body,validate=True) if encoded else body.encode('utf-8')
        if len(raw)>MAX_BODY:raise JournalError('response_body_bound')
        journal.validate_initial_progress(await provider())
        if observer._closed or journal._invalid:raise JournalError('initial_quantity_not_provisional')
        journal.accept_body(identifier,raw)
    except asyncio.CancelledError:
        journal.invalidate('cancelled');journal.fail(identifier,'cancelled');raise
    except Exception:
        journal._hold_initial('initial_quantity_body_context_unproven')
        journal.fail(identifier,'body_unavailable')
    finally:
        # No raw body or source result is retained in state or evidence.
        result=body=raw=None


def initial_controller_context(capture, *, before=False):
    """Validate actual scalar services, never fabricate a visible-radio guard."""
    normalized = normalize_price_context(capture)
    context = normalized['context']
    selected = context['selections']
    allowed = {'staggered_manual_controls_not_retained'}
    if before:
        allowed.add('unselected_required_value:quantity')
    if (set(normalized['reasons']) - allowed or context.get('product_type') != '38'
            or context.get('source_url') != STAGGERED_FAMILY.source_url
            or not staggered_literal_base_query(context.get('base_price_query'))
            or selected.get('proofType') != '100' or selected.get('manualProofType') not in {'1','36','37','38'}
            or selected.get('fileDelivery') != '5' or selected.get('designServices') != '42'
            or context.get('coupon') != {'mode':'empty_source_coupon','is_coupon_spec_changed':False}
            or set(selected) != set(STAGGERED_FAMILY.keys)
            or (before and selected.get('quantity') not in {'','0'})):
        raise JournalError('initial_quantity_controller_context_unproven')
    if before and capture.get('manual_controls') is not None:
        raise JournalError('initial_quantity_requires_undisclosed_first_context')
    return deepcopy(context)


class InitialQuantityPriceJournal(PriceJournal):
    """Internal explicit campaign type38 mode; normalizer/base class untouched."""
    def __init__(self, diagnosis_id, *, enable_initial_quantity, **kwargs):
        if enable_initial_quantity is not True:
            raise JournalError('initial_quantity_opt_in_required')
        if kwargs.get('family') is not STAGGERED_FAMILY or kwargs.get('allow_staggered_literal_base') is not True:
            raise JournalError('initial_quantity_scope_unproven')
        super().__init__(diagnosis_id, **kwargs)
        self._initial = None
        self._initial_consumed = False
        self._initial_body_guards = set()

    def begin_epoch(self, capture_before, revision_before, field_key, value):
        selected = capture_before.get('userSelectedOptions', {}) if isinstance(capture_before, dict) else {}
        initial = (field_key == 'quantity' and selected.get('quantity') in {'',0,'0'}
                   and capture_before.get('manual_controls') is None)
        context = None
        if initial:
            if self._initial_consumed or any(e.field == 'quantity' for e in self._epochs.values()):
                raise JournalError('initial_quantity_already_consumed')
            # Attempt is consumed even if the before-state proves unsupported.
            self._initial_consumed = True
            context = initial_controller_context(capture_before, before=True)
        number = super().begin_epoch(capture_before, revision_before, field_key, value)
        if initial:
            self._initial = {'epoch':number, 'phase':'collecting', 'before':context,
                             'quantity':str(value), 'reason':None}
        return number

    def is_initial_epoch(self, number=None):
        return self._initial is not None and self._initial['epoch'] == (self._current if number is None else number)

    def is_initial_request(self, identifier):
        record = self._requests.get(identifier)
        return bool(record and self.is_initial_epoch(record.epoch))

    def _hold_initial(self, reason):
        if self._initial is not None and self._initial['phase'] != 'complete':
            self._initial['phase'] = 'held'
            self._initial['reason'] = reason
            epoch = self._epochs.get(self._initial['epoch'])
            if epoch is not None:
                epoch.reasons.add(reason)

    def invalidate(self, reason):
        self._hold_initial('initial_quantity_lifecycle_invalidated')
        super().invalidate(reason)

    def _literal_base_profile(self, epoch):
        if self.is_initial_epoch(epoch.number):
            if self._initial['phase'] == 'collecting' and not self._invalid:
                return dict(STAGGERED_LITERAL_BASE_QUERY)
            return None
        return super()._literal_base_profile(epoch)

    def request(self, identifier, frame_id, loader_id, url, method, resource_type, **kwargs):
        if (self.is_initial_epoch() and self._initial['phase'] in {'disclosing','held'}
                and frame_id == self._frame and loader_id == self._loader and self._candidate(url)):
            self._hold_initial('initial_quantity_unexpected_price_traffic')
            return False
        accepted=super().request(identifier, frame_id, loader_id, url, method, resource_type, **kwargs)
        if accepted and self.is_initial_request(identifier):
            record=self._requests[identifier]
            selected=self._initial['before']['selections']
            query=dict(record.spec.lookup)
            expected={'productType':'38',**{k:selected[k] for k in self.family.base_fields}}
            if record.spec.kind=='product_identity':expected['turnaround']=selected['productionTime']
            if (record.spec.product_id and record.spec.product_id!=selected['productId']
                    or query and query!=expected):
                self._hold_initial('initial_quantity_native_identity_drift')
                self.fail(identifier,'response_identity_mismatch')
                return False
        return accepted

    def validate_initial_progress(self, capture):
        if (not self.is_initial_epoch() or self._initial['phase'] not in {'collecting','disclosing'}
                or self._invalid or self._epochs[self._current].finished):
            raise JournalError('initial_quantity_not_provisional')
        try:
            context = initial_controller_context(capture)
            before = self._initial['before']
            # Only quantity may differ. Current control visibility is deliberately
            # not synthesized into the retained before context.
            comparable = lambda c: {k:v for k,v in c.items() if k != 'manual_controls'}
            expected = deepcopy(before)
            expected['selections']['quantity'] = self._initial['quantity']
            if comparable(context) != comparable(expected):
                raise JournalError('initial_quantity_context_drift')
            return context
        except JournalError:
            self._hold_initial('initial_quantity_context_drift')
            raise

    def authorize_initial_body(self, identifier, capture):
        if not self.is_initial_request(identifier):
            raise JournalError('initial_quantity_request_unattributed')
        self.validate_initial_progress(capture)
        if self._initial['phase'] != 'collecting':
            self._hold_initial('initial_quantity_body_after_disclosure')
            raise JournalError('initial_quantity_body_after_disclosure')
        self._initial_body_guards.add(identifier)

    def accept_body(self, identifier, raw):
        if self.is_initial_request(identifier) and (identifier not in self._initial_body_guards
                or self._initial['phase'] != 'collecting' or self._invalid):
            self._hold_initial('initial_quantity_body_guard_missing')
            self.fail(identifier, 'body_unavailable')
            return
        super().accept_body(identifier, raw)

    def start_initial_disclosure(self, capture):
        self.validate_initial_progress(capture)
        epoch = self._epochs[self._current]
        if (self._initial['phase'] != 'collecting' or epoch.reasons
                or any(not self._requests[i].done for i in epoch.requests)):
            self._hold_initial('initial_quantity_disclosure_not_ready')
            raise JournalError('initial_quantity_disclosure_not_ready')
        self._initial['phase'] = 'disclosing'

    def finalize_epoch(self, epoch_number, capture_after, revision_after):
        initial = self.is_initial_epoch(epoch_number)
        if initial:
            if self._initial['phase'] != 'disclosing':
                self._hold_initial('initial_quantity_disclosure_not_certified')
            else:
                try:
                    self.validate_initial_progress(capture_after)
                except JournalError:
                    pass
        result = super().finalize_epoch(epoch_number, capture_after, revision_after)
        if initial:
            self._initial['phase'] = ('complete' if result['status'] == 'matched_fresh_observed_components' else 'held')
            result['initial_quantity_admission'] = {
                'profile':'type38_initial_quantity_v1', 'before_control_visibility':'not_certified',
                'final_visible_controls_required':True, 'provisional_capture_only_until_disclosure':True,
                'prior_held_epochs_promoted':False}
            self._epochs[epoch_number].result = deepcopy(result)
        return result

    def snapshot(self):
        value = super().snapshot()
        if self._initial is not None:
            value['initial_quantity_provisional'] = {k:deepcopy(v) for k,v in self._initial.items() if k != 'before'}
        return value
