"""Registered native supplier jobs; no source I/O at import time.

The first registered family is Printograph type38. The driver uses only the
manager's existing authenticated owned pages, limiter and atomic native choices.
The table receipt is deliberately separate from the unchanged passive journal:
quantity-cache epochs remain held there. Neither a receipt nor a stage finishes
a product or authorizes a Woo import. Saved journals are a skip ledger only.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

from .price_contract import calculate_source_total
from .diagnosis_policy import normalize_selection, selection_fingerprint, classify_finalization
from .printograph_initial_quantity import InitialQuantityPriceJournal
from .printograph_price_journal import (
    STAGGERED_FAMILY, CONTRACT, JournalError, project_context, _Epoch, _hash,
    MAX_QUANTITY, TTL_SECONDS,
)

PROFILE_ID = 'printograph_staggered_v3'
OPEN_TIMEOUT_SECONDS = 75
SAFE_FAILURE_REASONS = frozenset({
    'supplier_profile_cancelled','diagnosis_open_timeout','diagnosis_open_cancelled','diagnosis_not_ready','access_required',
    'page_timeout','page_unavailable','page_not_open','invalid_url','network_error',
    'browser_profile_in_use','browser_unavailable','supplier_profile_not_live',
    'source_control_unproven','source_choice_unavailable','source_coating_absence_unproven',
    'requested_configuration_not_retained','passive_observer_not_ready','passive_bundle_not_ready',
    'passive_bundle_unverified','passive_native_choice_failed','passive_observation_held',
    'passive_signed_in_required','passive_owned_page_unavailable','source_not_settled',
    'fresh_anchor_required','profile_catalog_unproven','full_component_table_missing',
    'current_production_unproven','table_ttl_expired_or_changed','table_continuity_broken',
    'native_frontier_not_advanced','source_frontier_domain_changed','selection_already_observed',
    'coating_identity_join_pending','supplier_profile_stage_held'})


def failure_context(error,record=None):
    """Closed machine enums only; never persist a browser/source exception."""
    record=record if type(record) is dict else {}
    reason=str(error)
    if reason not in SAFE_FAILURE_REASONS:
        reason='supplier_profile_stage_held'
    if reason=='diagnosis_not_ready':
        warnings=record.get('warnings',[])
        known=next((v for v in warnings if type(v) is str and v in SAFE_FAILURE_REASONS),None)
        if known:reason=known
    phase=record.get('opening_failure_phase',record.get('opening_phase'))
    return {'reason':reason,'diagnosis_status_before_close':record.get('status') if record.get('status') in
            {'opening','analyzing','ready','attention','closed','login_required'} else 'unknown',
            'session_status_before_close':record.get('session_status') if record.get('session_status') in
            {'signed_in','login_required','unknown','closed'} else 'unknown',
            'opening_phase':phase if phase in {'creating_page','navigating','settling','refreshing','attaching_observer','ready'} else 'unknown'}
SOURCE_URL = STAGGERED_FAMILY.source_url
SCHEMA = 'supplier-profile-stage/v1'
MAX_STAGE_QUANTITIES = 64
NORMAL_KEYS = {'size', 'paper', 'color', 'quantity', 'manualProofType', 'productionTime'}
LEVELS = ('size','paper','color','coating')
PROOFS = ('36','1')
MAX_FRONTIER_NODES = 4096
FIXED_SERVICES = {'proofType': '100', 'fileDelivery': '5', 'designServices': '42'}
_NUM = re.compile(r'[1-9][0-9]{0,11}\Z')
PROFILE_REGISTRY = {PROFILE_ID: {'supplier': 'printograph', 'family': '38',
    'source_associations': {'17792': SOURCE_URL}, 'driver': 'printograph_native_table',
    'manual_proofs': ('36','1'), 'production_modes': ('1',), 'fixed_services': FIXED_SERVICES}}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def association_for(profile_id, association):
    """Registry entry, not an endpoint or a profile guessed from the URL."""
    require(type(profile_id) is str and profile_id in PROFILE_REGISTRY, 'needs_profile')
    profile = PROFILE_REGISTRY[profile_id]
    require(type(association) is dict and set(association) ==
            {'campaign_id', 'product_id', 'supplier', 'source_url'}, 'association_invalid')
    require(type(association['campaign_id']) is str and
            re.fullmatch(r'[A-Za-z0-9_-]{1,128}', association['campaign_id']), 'association_invalid')
    require(type(association['product_id']) is str and association['supplier'] == profile['supplier']
            and profile['source_associations'].get(association['product_id']) == association['source_url'], 'needs_profile')
    return deepcopy(association)


def normalize_configuration(value):
    require(type(value) is dict, 'configuration_invalid')
    if value.get('mode') == 'first_unspent_native_stage':
        require(set(value) == {'mode', 'collect_quantities'} and
                type(value['collect_quantities']) is bool, 'configuration_invalid')
        return deepcopy(value)
    require(NORMAL_KEYS <= set(value) <= NORMAL_KEYS | {'collect_quantities','coating'}, 'configuration_invalid')
    require(all(type(value[k]) is str and _NUM.fullmatch(value[k]) for k in NORMAL_KEYS),
            'configuration_invalid')
    require(int(value['quantity']) <= MAX_QUANTITY and value['manualProofType'] in {'36','1'}
            and value['productionTime'] == '1', 'needs_profile')
    require(type(value.get('collect_quantities', False)) is bool, 'configuration_invalid')
    require('coating' not in value or type(value['coating']) is str and
            (value['coating']=='' or _NUM.fullmatch(value['coating'])), 'configuration_invalid')
    return {**value, 'collect_quantities': value.get('collect_quantities', False)}


def discover(profile_id, association, max_jobs=99):
    association_for(profile_id, association)
    require(type(max_jobs) is int and 1 <= max_jobs <= 99, 'plan_budget_invalid')
    return {'configurations': [{'mode': 'first_unspent_native_stage', 'collect_quantities': True}],
            'blockers': [], 'capability_scope': 'source_validated_quantity_stage',
            'stage_scoped_only': True, 'source_data_finalized': False}


def spent_selections(root):
    """Exact source selector/quantity/proof skips, never prices or eligibility.

    A closed full component observation identifies its selected proof/coating.
    An incomplete old capture may prove only a base tuple and a q-filtered
    request: retain that one quantity with wildcard service scope, never infer
    that every row in a returned full table was a configured quote.
    """
    directory=Path(root)/'printograph-price-journals'
    if directory.exists():require(directory.is_dir() and not directory.is_symlink(),'spent_ledger_invalid')
    files=sorted(directory.glob('*.json')) if directory.exists() else []
    require(len(files)<=4096 and sum(path.stat().st_size for path in files)<=128*1024*1024,'spent_ledger_bound')
    exact=set();unknown=set()
    for path in files:
        require(path.is_file() and not path.is_symlink() and path.stat().st_size<=1024*1024,'spent_ledger_invalid')
        try:data=json.loads(path.read_text())
        except (OSError,ValueError):raise ValueError('spent_ledger_invalid') from None
        require(type(data) is dict and type(data.get('events')) is list,'spent_ledger_invalid')
        if data.get('source_url')!=SOURCE_URL:continue
        ids={}
        for event in data['events']:
            if type(event) is not dict:continue
            req=event.get('request',{});projection=event.get('projection',{})
            if req.get('kind')=='product_identity' and projection.get('kind')=='product_identity':
                query=req.get('numeric_query',{});base=tuple(query.get(k) for k in LEVELS[:3])
                if query.get('productType')=='38' and all(type(v) is str and _NUM.fullmatch(v) for v in base):
                    ids.setdefault(projection.get('product_id'),set()).add(base)
        epochs={e.get('selection_epoch'):e for e in data.get('epochs',[]) if type(e) is dict}
        groups={}
        for event in data['events']:
            if type(event) is dict and event.get('request',{}).get('kind') in {'base_price','option_price'}:
                groups.setdefault(event.get('epoch'),[]).append(event)
        for number,events in groups.items():
            products={e['request'].get('product_id') for e in events}
            bases=set().union(*(ids.get(identifier,set()) for identifier in products))
            if len(bases)!=1:continue
            base=next(iter(bases));epoch=epochs.get(number,{})
            components=epoch.get('components',[])
            quantities={c.get('quantity') for c in components if type(c) is dict and c.get('field')=='productPrice'}
            filtered={e['request'].get('quantity_filter') for e in events if e['request'].get('quantity_filter')}
            quantities|=filtered
            quantities={q for q in quantities if type(q) is str and _NUM.fullmatch(q)}
            if not quantities:
                unknown.add(base);continue
            proofs={c.get('selected_value') for c in components if type(c) is dict and c.get('field')=='proofType'}
            requested_proof=data.get('supplier_profile_requested_manual_proof')
            if requested_proof in PROOFS and proofs and proofs!={requested_proof}:
                # Source-native default NoProof while mounting a requested PDF
                # is setup, not a delivered/requested NoProof quote.
                continue
            coating={c.get('selected_value') for c in components if type(c) is dict and c.get('field')=='coating'}
            has_full=bool(components) and epoch.get('context_sha256') is not None
            proof=next(iter(proofs)) if len(proofs)==1 else requested_proof if requested_proof in PROOFS else '*'
            requested_coating=data.get('supplier_profile_requested_coating')
            known_coating=type(requested_coating) is str and (requested_coating=='' or _NUM.fullmatch(requested_coating))
            coat=requested_coating if known_coating else next(iter(coating)) if len(coating)==1 else '' if has_full else '*'
            if type(proof) is not str:proof='*'
            if type(coat) is not str:coat='*'
            for quantity in quantities:exact.add((*base,coat,quantity,proof))
    receipts=Path(root)/'supplier-profile-spent'
    if receipts.exists():
        require(receipts.is_dir() and not receipts.is_symlink(),'spent_ledger_invalid')
        files=sorted(receipts.glob('*.json'))
        require(len(files)<=4096 and sum(path.stat().st_size for path in files)<=64*1024*1024,'spent_ledger_bound')
        for path in files:
            require(path.is_file() and not path.is_symlink() and path.stat().st_size<=64*1024,'spent_ledger_invalid')
            raw=path.read_bytes()
            require(path.name==hashlib.sha256(raw).hexdigest()+'.json','spent_ledger_invalid')
            try:receipt=json.loads(raw)
            except ValueError:raise ValueError('spent_ledger_invalid') from None
            require(type(receipt) is dict and set(receipt)=={'schema','source_url','source_contract','result_sha256','selections'}
                and receipt['schema']=='supplier-profile-spent/v1' and receipt['source_url']==SOURCE_URL
                and receipt['source_contract']==CONTRACT and type(receipt['selections']) is list
                and len(receipt['selections'])<=MAX_STAGE_QUANTITIES,'spent_ledger_invalid')
            for row in receipt['selections']:
                require(type(row) is list and len(row)==6 and all(type(v) is str and
                    (_NUM.fullmatch(v) or index==3 and v=='') for index,v in enumerate(row)),'spent_ledger_invalid')
                exact.add(tuple(row))
    return exact,unknown


def selection_spent(spent, branch, quantity, proof='36'):
    return any(tuple(row[:3])==tuple(branch[:3]) and row[3] in {'*',branch[3]}
               and row[4]==quantity and row[5] in {'*',proof} for row in spent)


def prefix_key(prefix):
    return canonical(list(prefix)).decode()


def new_frontier(association):
    return {'schema':'supplier-native-frontier/v3','association':deepcopy(association),
            'profile_id':PROFILE_ID,'source_contract':deepcopy(CONTRACT),'nodes':{},'leaves':{},
            'blocked_prefixes':{},'progress_count':0,'progress_unit':'resolved_native_branch',
            'open_scope':['other source process modes'], 'source_data_finalized':False}


def frontier_done(frontier,prefix=()):
    key=prefix_key(prefix)
    if key in frontier['blocked_prefixes']:return True
    if len(prefix)==len(LEVELS):return all(prefix_key((*prefix,proof)) in frontier['leaves'] for proof in PROOFS)
    node=frontier['nodes'].get(key)
    return node is not None and all(frontier_done(frontier,(*prefix,value)) for value in node['values'])


def validate_frontier(frontier,association):
    require(type(frontier) is dict and set(frontier)==set(new_frontier(association))
            and frontier['schema']=='supplier-native-frontier/v3'
            and frontier['association']==association and frontier['source_contract']==CONTRACT
            and frontier['profile_id']==PROFILE_ID and frontier['source_data_finalized'] is False
            and frontier['progress_unit']=='resolved_native_branch','frontier_invalid')
    for name in ('nodes','leaves','blocked_prefixes'):
        require(type(frontier[name]) is dict and len(frontier[name])<=MAX_FRONTIER_NODES,'frontier_bound')
        for key,value in frontier[name].items():
            try:prefix=json.loads(key)
            except (ValueError,TypeError):raise ValueError('frontier_invalid') from None
            require(type(prefix) is list and 0<=len(prefix)<=5 and prefix_key(prefix)==key
                    and all(type(v) is str and (_NUM.fullmatch(v) or index==3 and v=='')
                            for index,v in enumerate(prefix)),'frontier_invalid')
            if name=='nodes':
                require(len(prefix)<4 and type(value) is dict and set(value)=={'field','values'}
                        and value['field']==LEVELS[len(prefix)] and type(value['values']) is list
                        and len(value['values'])<=128 and len(set(value['values']))==len(value['values'])
                        and all(type(v) is str and (_NUM.fullmatch(v) or len(prefix)==3 and v=='')
                                for v in value['values']),'frontier_invalid')
            elif name=='leaves':
                require(len(prefix)==5 and prefix[4] in PROOFS and type(value) is dict and value.get('branch')==prefix[:4]
                        and value.get('manualProofType')==prefix[4]
                        and value.get('status') in {'collected','previously_spent','blocked'}
                        and type(value.get('source_quantities')) is list
                        and type(value.get('quoted_quantities')) is list
                        and type(value.get('spent_quantities')) is list
                        and type(value.get('blockers')) is list,'frontier_invalid')
            else:require(type(value) is str and value in {'native_choices_unavailable','historical_quantity_scope_unknown'},'frontier_invalid')
    require(type(frontier['progress_count']) is int and frontier['progress_count']==
            len(frontier['leaves'])+len(frontier['blocked_prefixes']),'frontier_invalid')
    return frontier


def frontier_from_task(root, association, progress_token=None, result_reference=None, progress_count=None):
    if progress_token is None and result_reference is None and progress_count in (None,0):
        return new_frontier(association)
    require(type(progress_token) is str and re.fullmatch(r'[a-f0-9]{64}',progress_token)
            and type(result_reference) is str and result_reference.count('#')==1,'frontier_reference_invalid')
    filename,digest=result_reference.rsplit('#',1)
    require(re.fullmatch(r'[a-f0-9]{64}',digest),'frontier_reference_invalid')
    root=Path(root);path=Path(filename)
    require(path.parent==root/'supplier-profile-results' and path.name==digest+'.json'
            and path.is_file() and not path.is_symlink() and path.stat().st_size<=2*1024*1024,'frontier_reference_invalid')
    raw=path.read_bytes();require(hashlib.sha256(raw).hexdigest()==digest,'frontier_reference_invalid')
    try:result=json.loads(raw)
    except ValueError:raise ValueError('frontier_reference_invalid') from None
    frontier=validate_frontier(result.get('frontier'),association)
    require(_hash(frontier)==progress_token and type(progress_count) is int
            and frontier['progress_count']==progress_count,'frontier_reference_invalid')
    return deepcopy(frontier)


def read_binding_script(script):
    """Read-only identity resolution also permits an already selected target."""
    old = '||retained[0]===target_value'
    require(script.count(old) == 1, 'binding_template_changed')
    return executor_binding_script(script.replace(old, ''))


def executor_binding_script(script):
    """Only the proven optional Coating model may have an empty choice."""
    old="!(/^[0-9]{1,12}$/).test(target_value)"
    require(script.count(old)==1, 'binding_template_changed')
    script=script.replace(old,"!(/^(?:[0-9]{1,12})?$/).test(target_value)")
    old="if(bindings.length!==1)return no();"
    require(script.count(old)==1, 'binding_template_changed')
    return script.replace(old,old+"\n    if(target_value===''&&bindings[0]!=='coating')return no();")


def executor_action_script(script):
    """Remove presentation-only shipping guards, never read shipping fields.

    The source automatically opens that panel on first quantity. All source,
    exact node, fence, identity, retained-choice and enabled guards remain.
    Only explicitly owned executor pages may use these derived scripts.
    """
    replacements = {
        " const shippingOpen=()=>[...document.querySelectorAll('#esCollapse')].some(x=>x.classList.contains('show'));": '',
        ' || shippingOpen()': '',
        " if(shippingOpen())throw Error('native select shipping state changed');": '',
        " ||\n    [...document.querySelectorAll('#esCollapse')].some(e=>e.classList.contains('show'))": '',
        " ||\n    [...document.querySelectorAll('#esCollapse')].some(x=>x.classList.contains('show'))": '',
    }
    changed = 0
    for old, new in replacements.items():
        count = script.count(old)
        require(count <= 1, 'action_template_changed')
        if count:
            script = script.replace(old, new)
            changed += count
    require(changed in {1, 3} and "some(e=>e.classList.contains('show'))" not in script
            and "some(x=>x.classList.contains('show'))" not in script,
            'action_template_changed')
    return script


@dataclass
class LiveTableAnchor:
    journal: object
    generation: str
    epoch: int
    nonquantity: str
    dependency: tuple
    context: dict
    domain: tuple
    records: tuple
    stamps: tuple
    components: tuple
    broken: bool = False

    @staticmethod
    def catalog(journal,context):
        """Independent query-dependency proof, never a legacy epoch promotion.

        Native product lookup contains only the pinned base specs/turnaround;
        option lookup contains only productId. Coating/proof alter their own
        option-price IDs, not these catalog identities. Unknown/drifted actual
        lifecycle barriers still permanently reject a prior identity.
        """
        def base_dependency(key):
            if key is None:return None
            count=len(STAGGERED_FAMILY.base_fields);options=len(STAGGERED_FAMILY.option_fields)
            return (*key[:count],*key[count+options:])
        expected=base_dependency(journal._catalog_dependency_key(context))
        require(expected is not None,'profile_catalog_unproven')
        require(not any(record.spec.kind=='product_identity' and journal._fresh(record)
                        and journal._base_matches(record,context)
                        and record.projection.get('product_id')!=context['selected']['productId']
                        for record in journal._requests.values()),'product_identity_conflict')
        candidates=[]
        for record in journal._requests.values():
            if (record.spec.kind!='product_identity' or not journal._fresh(record)
                    or not journal._base_matches(record,context)
                    or record.projection.get('product_id')!=context['selected']['productId']):continue
            origin=journal._epochs.get(record.epoch)
            if not origin or not origin.finished or origin.reasons or base_dependency(origin.catalog_after)!=expected:continue
            valid=True
            for number in range(record.epoch+1,journal._current+1):
                step=journal._epochs.get(number)
                if (not step or not step.finished or step.reasons
                        or step.field not in {'coating','manualProofType','quantity'}
                        or base_dependency(journal._catalog_dependency_key(step.before))!=expected
                        or base_dependency(step.catalog_after)!=expected
                        or step.result.get('revision_after')!=step.revision_before+1
                        or any(i not in journal._requests or not journal._requests[i].done for i in step.requests)
                        or step.before.get('context',{}).get('other_context')!=context['context']['other_context']):
                    valid=False;break
                positive=step.field=='quantity' or step.before['selected'].get('quantity') not in {'','0'}
                if positive and (not step.result.get('context_sha256') or not step.result.get('nonquantity_context_sha256')
                        or set(step.result.get('reasons',[]))-{'fresh_exact_quantity_missing','product_selection_mapping_missing','option_mapping_missing'}):
                    valid=False;break
            if valid:candidates.append(record)
        require(candidates,'profile_catalog_unproven')
        require(len({r.projection['product_id'] for r in candidates})==1,'product_identity_conflict')
        mapping=max(candidates,key=lambda r:r.finish_order)
        options=[r for r in journal._requests.values() if r.spec.kind=='option_identity'
                 and r.spec.product_id==context['selected']['productId'] and r.order>mapping.finish_order
                 and r.epoch>=mapping.epoch and journal._fresh(r)]
        require(options and len({_hash(r.projection) for r in options})==1,'option_identity_conflict')
        return mapping,max(options,key=lambda r:r.finish_order)

    @classmethod
    def create(cls, journal, capture, revision):
        require(isinstance(journal, TableQuantityJournal) and not journal._invalid,
                'table_profile_not_live')
        context = project_context(capture, family=STAGGERED_FAMILY)
        epoch = journal._epochs.get(journal._current)
        require(epoch and epoch.finished and not epoch.reasons and
                epoch.result.get('status') in {'matched_fresh_observed_components','held'} and
                (epoch.result.get('status')!='held' or bool(epoch.result.get('reasons'))) and
                not set(epoch.result.get('reasons',[]))-{'fresh_exact_quantity_missing','product_selection_mapping_missing','option_mapping_missing'} and
                epoch.result.get('revision_after') == revision and
                epoch.result.get('context_sha256') == context['context_hash'], 'fresh_anchor_required')
        mapping,options=cls.catalog(journal,context)
        query = {'productType': '38', **{key: context['selected'][key] for key in STAGGERED_FAMILY.base_fields}}
        domains = [record for record in journal._requests.values()
                   if record.spec.kind == 'quantity_domain' and record.epoch == mapping.epoch
                   and dict(record.spec.lookup) == query and journal._fresh(record)]
        require(len(domains) == 1, 'source_quantity_domain_unproven')
        domain = domains[0].projection.get('quantities')
        require(type(domain) is list and 0 < len(domain) <= MAX_STAGE_QUANTITIES and
                len(set(domain)) == len(domain) and all(type(q) is str and _NUM.fullmatch(q)
                and int(q) <= MAX_QUANTITY for q in domain), 'source_quantity_domain_unproven')
        reconciliation = calculate_source_total(capture)
        records = [mapping, options, domains[0]]
        components = []
        for component in reconciliation['components']:
            kind = 'base_price' if component['field'] == 'productPrice' else 'option_price'
            option = ''
            if kind == 'option_price':
                ids = [key for key, value in options.projection['mapping'].items()
                       if value == component['selected_value']]
                require(len(ids) == 1, 'option_mapping_ambiguous')
                option = ids[0]
            eligible = [record for record in journal._requests.values()
                        if mapping.epoch<=record.epoch<=epoch.number and record.spec.kind == kind
                        and record.spec.product_id == context['selected']['productId']
                        and record.spec.option_id == option and record.spec.quantity_filter == ''
                        and record.order>mapping.finish_order and journal._fresh(record)
                        and (kind!='base_price' or record.spec.base_query_profile=='source_literal_pair_v1')
                        and all(journal._epochs[record.epoch].before['selected'].get(k)==context['selected'][k]
                                for k in (*STAGGERED_FAMILY.base_fields,'productId','productionTime'))
                        and all(journal._epochs[record.epoch].before['selected'].get(k)==v for k,v in FIXED_SERVICES.items())
                        and journal._epochs[record.epoch].before['context'].get('other_context')==context['context']['other_context']
                        and journal._epochs[record.epoch].before['context'].get('coupon')==context['context']['coupon']
                        and journal._epochs[record.epoch].before['selected'].get('manualProofType') in {'1','36'}]
            require(eligible, 'full_component_table_missing')
            require(len({_hash(record.projection) for record in eligible}) == 1, 'fresh_tables_conflict')
            record = max(eligible, key=lambda item: item.order)
            require(set(domain) <= set(record.projection.get('rows', {})), 'full_component_table_incomplete')
            records.append(record)
            components.append((component['field'], component['selected_value'], record))
        anchor = cls(journal, journal.page_generation, epoch.number, context['nonquantity_hash'],
                     journal._catalog_dependency_key(context), deepcopy(context['context']), tuple(domain),
                     tuple(records), tuple(cls.stamp(r) for r in records), tuple(components))
        journal.table_anchor = anchor
        return anchor

    @staticmethod
    def stamp(record):
        return (id(record), record.epoch, record.order, record.finish_order, record.finished_at,
                record.body_sha256, _hash(record.projection), record.spec.public())

    def fail(self, reason):
        self.broken = True
        raise ValueError(reason)

    def validate(self, capture, revision):
        journal = self.journal
        if self.broken or journal._invalid or journal.page_generation != self.generation:
            self.fail('table_profile_not_live')
        try:
            current = project_context(capture, family=STAGGERED_FAMILY)
            require(current['nonquantity_hash'] == self.nonquantity, 'table_context_changed')
            require(all(journal._fresh(record) and self.stamp(record) == stamp
                        for record, stamp in zip(self.records, self.stamps)), 'table_ttl_expired_or_changed')
            require(current['selected']['quantity'] in self.domain, 'quantity_outside_source_domain')
            for number in range(self.epoch + 1, journal._current + 1):
                step = journal._epochs.get(number)
                require(step and step.finished and not step.reasons and step.field == 'quantity'
                        and step.value in self.domain and step.before['nonquantity_hash'] == self.nonquantity
                        and step.catalog_after == self.dependency
                        and step.result.get('nonquantity_context_sha256') == self.nonquantity
                        and step.result.get('revision_after') == step.revision_before + 1
                        and set(step.result.get('reasons', [])) <= {'fresh_exact_quantity_missing','product_selection_mapping_missing','option_mapping_missing'},
                        'table_continuity_broken')
                require(all(journal._requests[i].done for i in step.requests), 'native_requests_inflight')
            epoch = journal._epochs.get(journal._current)
            require(epoch and epoch.finished and epoch.result.get('revision_after') == revision
                    and epoch.result.get('context_sha256') == current['context_hash'], 'table_revision_changed')
            # A contradictory or failed current component response cannot be
            # masked by the earlier good table. Unselected proof tables do not
            # contaminate this exact selected service.
            q = current['selected']['quantity']
            for _, _, old in self.components:
                for record in journal._requests.values():
                    if (record.epoch >= self.epoch and record.spec.kind == old.spec.kind
                            and record.spec.product_id == old.spec.product_id
                            and record.spec.option_id == old.spec.option_id):
                        require(record.done, 'native_requests_inflight')
                        require(not record.reason or record.cached and record.status == 200
                                and record.mime == 'application/json', 'current_component_response_failed')
                        if journal._fresh(record) and q in record.projection.get('rows', {}):
                            require(record.projection['rows'][q] == old.projection['rows'][q],
                                    'fresh_tables_conflict')
            return current
        except (ValueError, KeyError, TypeError):
            self.broken = True
            raise

    def quote(self, capture, revision, metadata):
        current = self.validate(capture, revision)
        reconciliation = calculate_source_total(capture)
        require(reconciliation.get('status') == 'complete' and reconciliation.get('comparison') == 'matched',
                'arithmetic_unresolved')
        q = current['selected']['quantity']
        expected = {(field, selected): record.projection['rows'][q]['source_cents']
                    for field, selected, record in self.components}
        actual = {(row['field'], row['selected_value']): row['amount'] for row in reconciliation['components']}
        require(expected == actual, 'controller_price_mismatch')
        production = current_production(current, metadata)
        remaining = min(TTL_SECONDS - (self.journal.clock() - record.finished_at) for record in self.records)
        require(remaining >= 0, 'table_ttl_expired_or_changed')
        return {'schema': 'printograph-live-table-row-quote/v1', 'status': 'quoted',
                'source_url': SOURCE_URL, 'diagnosis_id': self.journal.diagnosis_id,
                'page_generation': self.generation, 'revision': revision,
                'selection_epoch': self.journal._current, 'anchor_epoch': self.epoch,
                'context_hash': current['context_hash'], 'pricing_context': deepcopy(current['context']),
                'selection': deepcopy(current['selected']), 'currency': 'USD',
                'source_cost_subtotal': reconciliation['subtotal'], 'components': reconciliation['components'],
                'production': production, 'expires_in_seconds': remaining,
                'table_evidence': [{'kind': record.spec.kind, 'request_ordinal': record.order,
                                    'originating_epoch': record.epoch, 'body_sha256': record.body_sha256,
                                    'projection_sha256': _hash(record.projection)} for record in self.records],
                'price_basis': 'fresh_dependency_bound_full_table_exact_row',
                'catalog_identity_basis':'native_exact_query_dependencies',
                'new_price_HTTP_for_this_quantity': all(record.epoch==self.journal._current for _,_,record in self.components),
                'legacy_journal_status': self.journal._epochs[self.journal._current].result['status'],
                'site_retail_rule_applied': False, 'source_data_finalized': False,
                'woocommerce_import_ready': False, 'shipping_verified': False,
                'live_eligibility_ends_on_page_release': True}


def current_production(current, metadata):
    selected = current['selected']
    require(type(metadata) is dict and metadata.get('observed') is True
            and metadata.get('source_contract') == CONTRACT
            and metadata.get('selected_quantity') == selected['quantity']
            and metadata.get('selected_specification_ids') == {key: selected[key] for key in ('size', 'paper', 'color')}
            and metadata.get('selected_manual_proof_id') == selected['manualProofType']
            and metadata.get('selected_coating_id') == selected['coating'], 'current_production_unproven')
    production = metadata.get('cached_production', {})
    require(production.get('observed') is True and production.get('schema') == 'printograph-current-cached-production/v1'
            and type(production.get('selected_id')) is int and production['selected_id'] == int(selected['productionTime'])
            and production.get('range_source') == 'current_source_owned_cache'
            and production.get('availability_source') == 'current_rendered_choices'
            and production.get('transport_freshness') == 'not_observed', 'current_production_unproven')
    items = production.get('items')
    require(type(items) is list and len(items) == 2 and all(type(row) is dict and type(row.get('id')) is int for row in items)
            and {row.get('id') for row in items} == {1, 3},
            'current_production_unproven')
    selected_rows = [row for row in items if row.get('selected') is True]
    require(len(selected_rows) == 1, 'current_production_unproven')
    row = selected_rows[0]
    require(type(row.get('id')) is int and row['id'] == int(selected['productionTime'])
            and row.get('rendered_disabled') is False and row.get('rendered_range_matches_cache') is True,
            'current_production_unproven')
    bounds = row.get('range', {})
    minimum, maximum = bounds.get('min'), bounds.get('max')
    require(set(bounds) == {'min', 'max'} and type(minimum) is int and 0 <= minimum <= 365
            and (maximum is None or type(maximum) is int and minimum <= maximum <= 365),
            'current_production_unproven')
    return {'value': selected['productionTime'], 'range': deepcopy(bounds), 'unit': 'business_days',
            'basis': 'current_source_cache_and_matching_rendered_choice', 'transport_freshness': 'not_observed',
            'choices': deepcopy(production['choices'])}


class TableQuantityJournal(InitialQuantityPriceJournal):
    """Explicit future executor only; default global eight-epoch policy intact."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.table_anchor = None
        self.requested_manual_proof = None
        self.requested_coating = None

    def snapshot(self):
        value=super().snapshot()
        if self.requested_manual_proof in PROOFS:
            value['supplier_profile_requested_manual_proof']=self.requested_manual_proof
        if type(self.requested_coating) is str and (self.requested_coating=='' or _NUM.fullmatch(self.requested_coating)):
            value['supplier_profile_requested_coating']=self.requested_coating
        return value

    def begin_epoch(self, capture_before, revision_before, field_key, value):
        anchor = self.table_anchor
        if anchor is None:
            return super().begin_epoch(capture_before, revision_before, field_key, value)
        if field_key=='manualProofType' and value in {'1','36'}:
            anchor.validate(capture_before,revision_before)
            before=project_context(capture_before,family=self.family)
            require(before['selected']['manualProofType']!=value,'selection_not_new')
            require(len(self._epochs)<2*MAX_STAGE_QUANTITIES+8,'epoch_budget_exceeded')
            self._current+=1
            self._epochs[self._current]=_Epoch(self._current,revision_before,before,field_key,value)
            return self._current
        if field_key != 'quantity':
            anchor.broken = True
            raise JournalError('table_profile_quantity_only')
        # Same guarded begin lifecycle, with a separate finite source-domain
        # budget. No monkeypatch, reset, old epoch deletion, or new parser mode.
        anchor.validate(capture_before, revision_before)
        require(type(value) is str and value in anchor.domain, 'quantity_outside_source_domain')
        require(type(revision_before) is int and 0 <= revision_before < 1_000_000_000, 'invalid_revision')
        before = project_context(capture_before, family=self.family)
        require(value != before['selected']['quantity'], 'selection_not_new')
        require(not any(e.field == 'quantity' and e.value == value and
                       e.result.get('nonquantity_context_sha256')==before['nonquantity_hash'] for e in self._epochs.values()),
                'quantity_already_observed')
        require(len(self._epochs) < anchor.epoch + len(anchor.domain) and
                len(self._epochs) < 2*MAX_STAGE_QUANTITIES + 8, 'epoch_budget_exceeded')
        self._current += 1
        self._epochs[self._current] = _Epoch(self._current, revision_before, before, 'quantity', value)
        return self._current


def immutable_result(root, result, *, folder='supplier-profile-results'):
    """Private content-addressed receipts/rows, never a Store or Woo mutation."""
    root = Path(root)
    require(root.is_absolute() and root.is_dir() and not any(p.is_symlink() for p in (root, *root.parents)),
            'result_root_invalid')
    raw = canonical(result)
    require(len(raw) <= 2 * 1024 * 1024, 'result_bound')
    digest = hashlib.sha256(raw).hexdigest()
    require(folder in {'supplier-profile-results','supplier-profile-spent'},'result_root_invalid')
    directory = root / folder
    directory.mkdir(mode=0o700, exist_ok=True)
    require(not directory.is_symlink() and not directory.stat().st_mode & 0o077, 'result_root_invalid')
    path = directory / (digest + '.json')
    if path.exists():
        require(not path.is_symlink() and path.read_bytes() == raw, 'result_collision')
    else:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    return {'result_json': str(path), 'result_sha256': digest}


def persist_spent_receipt(root,result,digest):
    selections=[]
    for selected in result.get('attempted_selections',[]):
        values=[selected.get(key) for key in (*LEVELS,'quantity','manualProofType')]
        require(all(type(v) is str and (_NUM.fullmatch(v) or index==3 and v=='')
                    for index,v in enumerate(values)),'spent_projection_invalid')
        if values not in selections:selections.append(values)
    if not selections:return
    receipt={'schema':'supplier-profile-spent/v1','source_url':SOURCE_URL,'source_contract':dict(CONTRACT),
             'result_sha256':digest,'selections':selections}
    return immutable_result(root,receipt,folder='supplier-profile-spent')


def normal_export_bundle(quote, bindings, diagnosis):
    """Configured rows and a normal review policy; no Store/import side effect.

    Only actually bound product controls enter the selection. Complete hidden
    services/IDs stay in pricing_context and therefore the evidence fingerprint.
    Country/address controls are not print-price requirements.
    """
    selected = quote['selection']
    keys = ('size', 'paper', 'color', 'quantity', 'proofType', 'manualProofType', 'fileDelivery', 'productionTime')
    if 'coating' in bindings:keys=(*keys,'coating')
    else:require(selected['coating']=='','export_coating_binding_missing')
    require(set(keys) <= set(bindings), 'export_control_binding_missing')
    fields = []
    configuration = {}
    for key in keys:
        field = deepcopy(bindings[key])
        require(field.get('source_key') == key and field.get('selected_values') == [selected[key]],
                'export_control_binding_changed')
        fields.append(field)
        configuration[field['key']] = selected[key]
    labels = [field['label'] for field in fields]
    for field in fields:
        if labels.count(field['label']) > 1:
            field['label'] += ' (' + field['source_key'] + ')'
    require(len({field['key'] for field in fields}) == len(fields), 'export_control_binding_ambiguous')
    export_diagnosis = {key: deepcopy(diagnosis[key]) for key in ('id', 'url', 'title', 'status', 'revision')}
    export_diagnosis['fields'] = fields
    export_diagnosis['selection'] = normalize_selection(export_diagnosis, diagnosis['revision'],
        [{'key': field['key'], 'include': True, 'fixed_values': []} for field in fields])
    evidence = ['sha256:' + _hash(quote)]
    row = {'source_url': SOURCE_URL, 'product_url': SOURCE_URL, 'title': diagnosis['title'],
           'product_type': '38', 'base_product_id': selected['productId'],
           'selection': configuration, 'configuration': deepcopy(configuration),
           'quantity': selected['quantity'], 'currency': 'USD', 'subtotal': quote['source_cost_subtotal'],
           'pricing_context': deepcopy(quote['pricing_context']),
           'service_context': {key: selected[key] for key in (*FIXED_SERVICES, 'manualProofType')},
           'pricing_scope': 'one_fully_observed_source_selection',
           'provenance': 'reconciled_source_components', 'pricing_composition_verified': True,
           'verification_status': 'passed', 'status': 'supported',
           'production': deepcopy(quote['production']), 'evidence_ids': evidence}
    source_range = row['production']['range']
    # Pinned source max=null means one minimum day value, not open-ended.
    production_range = {'min': source_range['min'], 'max': source_range['min']
                        if source_range['max'] is None else source_range['max']}
    row['production']['source_range'] = deepcopy(source_range)
    row['production']['range'] = production_range
    bound = {'full_selection': True, 'selection_fingerprint': selection_fingerprint(row), 'evidence_ids': evidence}
    row['finalization_evidence'] = {
        'availability': {**bound, 'status': 'supported', 'basis': 'current_retained_enabled_source_controls'},
        'production': {**bound, 'status': 'supported', 'range': production_range,
                       'open_ended': False, 'basis': 'current_cache_and_matching_rendered_choice'},
        'price': {**bound, 'status': 'reconciled', 'subtotal': quote['source_cost_subtotal'], 'currency': 'USD'}}
    require(classify_finalization(row) == {'status': 'eligible', 'reasons': []}, 'normal_row_policy_held')
    return {'diagnosis': export_diagnosis, 'row': row, 'review_required': True,
            'normal_review_function': 'product_finalization.review_rows',
            'source_cost_only': True, 'preserve_existing_images': True,
            'source_data_finalized': False, 'woocommerce_import_ready': False}


class SupplierProfileExecutorMixin:
    """Orchestration only. Existing manager owns all actual browser actions."""
    def discover_supplier_profile(self, profile_id, association, max_jobs=99):
        return discover(profile_id, association, max_jobs)

    async def execute_supplier_profile_quote(self, profile_id, association, configuration, *, check_scope, focus=False,
                                            progress_token=None, result_reference=None, progress_count=None):
        association = association_for(profile_id, association)
        configuration = normalize_configuration(configuration)
        require(callable(check_scope) and type(focus) is bool, 'scope_callback_required')
        automatic=configuration.get('mode')=='first_unspent_native_stage'
        frontier=frontier_from_task(self.store.root,association,progress_token,result_reference,progress_count) if automatic else None
        require(automatic or progress_token is None and result_reference is None and progress_count is None,'frontier_reference_invalid')
        lock = self._supplier_profile_locks.setdefault(association['supplier'], asyncio.Lock())
        async with lock:
            await check_scope()
            spent,unknown = spent_selections(self.store.root)
            diagnosis = None
            quotes=[];blockers=[];requested=None;leaf=None;result=None;attempted=[]
            try:
                diagnosis = await self.start_staggered_passive(check_scope=check_scope, focus=focus)
                identifier = diagnosis['id']
                self._supplier_profile_owners[identifier] = {'profile_id': profile_id,
                    'association': deepcopy(association), 'binding': {}, 'anchor': None, 'check_scope': check_scope}
                diagnosis=await self._profile_wait_ready(identifier,diagnosis,check_scope)
                if automatic:
                    requested=await self._profile_frontier_stage(identifier,frontier,spent,unknown,check_scope)
                    collect=configuration['collect_quantities']
                    if requested is None:
                        require(frontier_done(frontier),'native_frontier_not_advanced')
                        result=self._profile_stage_result(association,profile_id,identifier,None,quotes,blockers,frontier)
                    elif requested.get('_progress_only') is True:
                        requested=None
                        result=self._profile_stage_result(association,profile_id,identifier,None,quotes,blockers,frontier)
                    else:
                        leaf=frontier['leaves'][prefix_key((*tuple(requested[k] for k in LEVELS),requested['manualProofType']))]
                else:
                    requested={key:configuration[key] for key in NORMAL_KEYS}
                    if 'coating' in configuration:requested['coating']=configuration['coating']
                    collect=configuration['collect_quantities']
                    for key in ('size','paper','color'):
                        await self._profile_choose(identifier,key,requested[key],check_scope)
                    coat=await self._resolve_supplier_profile_field(identifier,self.get(identifier)['revision'],'coating_optional',None)
                    if coat is None:
                        require(requested.get('coating','')=='','source_coating_unavailable')
                        requested['coating']=''
                    else:
                        require('coating' in requested,'explicit_coating_required')
                        await self._profile_choose(identifier,'coating',requested['coating'],check_scope)
                    require(not selection_spent(spent,tuple(requested[k] for k in LEVELS),requested['quantity'],requested['manualProofType']),
                            'selection_already_observed')
                if requested is not None:
                    # Direct requested FIRST unspent positive quantity, no setup canary.
                    journal=self._passive_journals.get(identifier)
                    if journal is not None:
                        journal.requested_manual_proof=requested['manualProofType']
                        journal.requested_coating=requested['coating']
                    attempted.append(deepcopy(requested))
                    await self._profile_choose(identifier,'quantity',requested['quantity'],check_scope)
                    # A native default NoProof total may appear while quantity
                    # mounts Process. It is setup only for a requested PDF, and
                    # is never published as an additional configured quote.
                    for key,value in (('proofType','100'),('fileDelivery','5'),('manualProofType',requested['manualProofType'])):
                        await self._profile_choose(identifier,key,value,check_scope)
                    quotes.append(await self._profile_read_quote(identifier,requested,check_scope))
                    anchor=self._supplier_profile_owners[identifier]['anchor']
                    domain=anchor.domain
                    branch=tuple(requested[k] for k in LEVELS)
                    if collect:
                        for quantity in domain:
                            if quantity==requested['quantity'] or selection_spent(spent,branch,quantity,requested['manualProofType']):continue
                            await check_scope()
                            attempted.append({**requested,'quantity':quantity})
                            await self._profile_choose(identifier,'quantity',quantity,check_scope)
                            quotes.append(await self._profile_read_quote(identifier,{**requested,'quantity':quantity},check_scope))
                    await check_scope()
                    if leaf is not None:
                        leaf.update(status='collected',source_quantities=list(domain),
                            quoted_quantities=[q['selection']['quantity'] for q in quotes],
                            spent_quantities=[q for q in domain if selection_spent(spent,branch,q,requested['manualProofType'])])
                        if leaf['spent_quantities']:leaf['blockers'].append('prior_spent_rows_preserved_not_promoted')
                        if not collect:leaf['blockers'].append('quantity_collection_not_requested')
                        frontier['progress_count']=len(frontier['leaves'])+len(frontier['blocked_prefixes'])
                    result=self._profile_stage_result(association,profile_id,identifier,requested,quotes,blockers,frontier)
                await check_scope()
            except asyncio.CancelledError:
                # Cancellation is terminal for this attempt, not a reason to
                # replay an uncertain native selection. Save the same bounded
                # attempted-selection/journal projection before owned cleanup.
                blockers.append({'reason':'supplier_profile_cancelled'})
                if leaf is not None:
                    leaf.update(status='blocked',quoted_quantities=[q['selection']['quantity'] for q in quotes])
                    leaf['blockers'].append('supplier_profile_cancelled')
                    frontier['progress_count']=len(frontier['leaves'])+len(frontier['blocked_prefixes'])
                result=self._profile_stage_result(association,profile_id,diagnosis['id'] if diagnosis else None,
                    requested,quotes,blockers,frontier)
                result.update(status='held',reason='supplier_profile_cancelled',continuation_required=False)
                result['failure_context']=failure_context('supplier_profile_cancelled',
                    self.get(diagnosis['id']) if diagnosis else None)
                raise
            except (ValueError,JournalError) as error:
                detail=failure_context(error,self.get(diagnosis['id']) if diagnosis else None)
                access=detail['reason'] in {'access_required','passive_signed_in_required'}
                blockers.append({'reason':'access_required' if access else detail['reason']})
                if leaf is not None:
                    leaf.update(status='blocked',quoted_quantities=[q['selection']['quantity'] for q in quotes])
                    leaf['blockers'].append(blockers[-1]['reason'])
                    frontier['progress_count']=len(frontier['leaves'])+len(frontier['blocked_prefixes'])
                result=self._profile_stage_result(association,profile_id,diagnosis['id'] if diagnosis else None,
                    requested,quotes,blockers,frontier)
                result.update(status='access_required' if access else 'held',reason=blockers[-1]['reason'])
                result['failure_context']=detail
            finally:
                # Save immutable source/row/progress facts BEFORE normal owned
                # page release. No tab/profile is leaked on successful chunks.
                try:
                    if result is not None:
                        result['attempted_selections']=attempted
                        result['previous_result_reference']=result_reference if automatic and progress_token else None
                        owned=self._passive_journals.get(diagnosis['id']) if diagnosis else None
                        if owned is not None:result['journal_evidence']=deepcopy(owned.snapshot())
                        result['references']=immutable_result(self.store.root,result)
                        persist_spent_receipt(self.store.root,result,result['references']['result_sha256'])
                finally:
                    if diagnosis is not None:await self._profile_close_owned(diagnosis['id'])
            return result

    async def _profile_wait_ready(self,identifier,diagnosis,check_scope):
        deadline=asyncio.get_running_loop().time()+OPEN_TIMEOUT_SECONDS
        while True:
            await check_scope()
            opening=getattr(self,'_tasks',{}).get(identifier)
            busy=opening is not None and not opening.done()
            if diagnosis.get('status') not in {'opening','analyzing'} and not busy:break
            require(asyncio.get_running_loop().time()<=deadline,'diagnosis_open_timeout')
            await asyncio.sleep(.1)
            diagnosis=self.get(identifier)
        require(opening is None or not opening.cancelled(),'diagnosis_open_cancelled')
        if diagnosis.get('session_status')=='login_required' or diagnosis.get('status')=='login_required':
            raise ValueError('access_required')
        require(diagnosis.get('status')=='ready','diagnosis_not_ready')
        require(diagnosis.get('session_status')=='signed_in','access_required')
        return diagnosis


    def _profile_stage_result(self,association,profile_id,identifier,requested,quotes,blockers,frontier):
        result={'schema':SCHEMA,'status':'quoted','profile_id':profile_id,**association,
            'configuration':requested,'diagnosis_id':identifier,'quotes':quotes,'blockers':blockers,
            'capability_scope':'source_validated_NoProof_frontier','stage_scoped_only':True,
            'source_data_finalized':False,'source_domain_complete':False,'woocommerce_import_ready':False,
            'site_retail_rule_applied':False,'customer_quote_fresh':False,'live_page_released_after_receipt':True}
        if frontier is not None:
            validate_frontier(frontier,association)
            done=frontier_done(frontier)
            complete=done and not frontier['blocked_prefixes'] and bool(frontier['leaves']) and all(
                not leaf['blockers'] and set(leaf['quoted_quantities'])==set(leaf['source_quantities'])
                for leaf in frontier['leaves'].values())
            result.update(frontier=deepcopy(frontier),progress_count=frontier['progress_count'],
                progress_token=_hash(frontier),progress_unit='resolved_native_branch',continuation_required=not done,
                registered_manual_scope_exhausted=done,registered_manual_scope_complete=complete,
                PDF_scope_open=not done or any(leaf.get('manualProofType')=='1' and leaf['blockers'] for leaf in frontier['leaves'].values()))
            if done:
                result.update(status='held',reason='registered_manual_scope_complete_other_processes_open' if complete else 'registered_manual_scope_exhausted_with_blockers')
        return result

    async def _profile_frontier_stage(self,identifier,frontier,spent,unknown,check_scope):
        progress_only=object()
        async def descend(prefix):
            await check_scope()
            if frontier_done(frontier,prefix):return None
            if len(prefix)==len(LEVELS):return prefix
            semantic=LEVELS[len(prefix)]
            field=await self._resolve_supplier_profile_field(identifier,self.get(identifier)['revision'],
                'coating_optional' if semantic=='coating' else semantic,None)
            if field is None:
                require(semantic=='coating','source_control_unproven')
                values=['']
            else:
                values=sorted({choice['value'] for choice in field['choices'] if not choice.get('disabled')
                    and type(choice.get('value')) is str and (_NUM.fullmatch(choice['value']) or semantic=='coating' and choice['value']=='')},
                    key=lambda v:(v!='',int(v or '0')))
            key=prefix_key(prefix)
            node={'field':semantic,'values':values}
            require(key not in frontier['nodes'] or frontier['nodes'][key]==node,'source_frontier_domain_changed')
            frontier['nodes'][key]=node
            require(sum(len(frontier[k]) for k in ('nodes','leaves','blocked_prefixes'))<=MAX_FRONTIER_NODES,'frontier_bound')
            if not values:
                frontier['blocked_prefixes'][key]='native_choices_unavailable'
                frontier['progress_count']=len(frontier['leaves'])+len(frontier['blocked_prefixes'])
                return progress_only
            # Preferences choose intent, never validate unseen combinations.
            if semantic=='paper':values=sorted(values,key=lambda v:({'8':0,'7':1}.get(v,2),int(v)))
            for value in values:
                child=(*prefix,value)
                if frontier_done(frontier,child):continue
                if field is not None:await self._profile_choose(identifier,semantic,value,check_scope)
                branch=await descend(child)
                if branch is not None:return branch
            return None
        branch=await descend(())
        if branch is progress_only:return {'_progress_only':True}
        if branch is None:return None
        proof=next(proof for proof in PROOFS if prefix_key((*branch,proof)) not in frontier['leaves'])
        requested={**dict(zip(LEVELS,branch)),'manualProofType':proof,'productionTime':'1'}
        field=await self._resolve_supplier_profile_field(identifier,self.get(identifier)['revision'],'quantity',None)
        quantities=sorted({choice['value'] for choice in field['choices'] if not choice.get('disabled')
            and type(choice.get('value')) is str and _NUM.fullmatch(choice['value'])},key=int)
        require(quantities and len(quantities)<=MAX_STAGE_QUANTITIES,'source_quantity_domain_unavailable')
        leaf={'branch':list(branch),'manualProofType':proof,'status':'blocked','source_quantities':quantities,
              'quoted_quantities':[],'spent_quantities':[],'blockers':[]}
        frontier['leaves'][prefix_key((*branch,proof))]=leaf
        if tuple(branch[:3]) in unknown:
            leaf['blockers'].append('historical_quantity_scope_unknown')
            leaf['spent_quantities']=quantities
        else:
            leaf['spent_quantities']=[q for q in quantities if selection_spent(spent,branch,q,proof)]
        remaining=[q for q in quantities if q not in leaf['spent_quantities']]
        if not remaining:
            leaf.update(status='previously_spent')
            leaf['blockers'].append('prior_spent_rows_preserved_not_promoted')
            frontier['progress_count']=len(frontier['leaves'])+len(frontier['blocked_prefixes'])
            # Return a progress-only chunk: no fake source quote, no replay.
            return None if frontier_done(frontier) else {'_progress_only':True}
        requested['quantity']=remaining[0]
        return requested

    async def _profile_close_owned(self,identifier):
        opening=getattr(self,'_tasks',{}).get(identifier)
        if opening is not None and opening is not asyncio.current_task():
            if not opening.done():opening.cancel()
            await asyncio.gather(opening,return_exceptions=True)
            self._tasks.pop(identifier,None)
        async with self._locks.setdefault(identifier,asyncio.Lock()):
            if identifier not in self._records:return
            journal=self._passive_journals.get(identifier)
            try:await self._release(identifier)
            finally:
                if journal is not None:
                    journal.invalidate('page_closed')
                    journal.persist(Path(self.store.root))
                self._records[identifier].update(status='closed',session_status='closed')
                self._persist(self._records[identifier])

    async def _profile_choose(self, identifier, semantic, value, check_scope):
        await check_scope()
        record = self.get(identifier)
        field = await self._resolve_supplier_profile_field(identifier, record['revision'], semantic, value)
        await check_scope()
        require(self.get(identifier)['revision'] == record['revision'], 'diagnosis_revision_conflict')
        if field.get('selected_values') != [value]:
            await self.select_observed_choice(identifier, record['revision'], field['key'], value)
        await check_scope()
        await self._resolve_supplier_profile_field(identifier, self.get(identifier)['revision'], semantic, value)


def queue_handler(manager, profile_id=PROFILE_ID):
    async def invoke(task, check_scope):
        try:
            configuration=normalize_configuration(task['configuration'])
            kwargs={}
            if configuration.get('mode')=='first_unspent_native_stage' and task.get('progress_token'):
                kwargs={'progress_token':task['progress_token'],'result_reference':task.get('result_reference'),
                        'progress_count':task.get('progress_count')}
            result=await manager.execute_supplier_profile_quote(profile_id,task['association'],configuration,
                check_scope=check_scope,**kwargs)
        except (ValueError,JournalError):
            return {'state':'held','result_reference':'','reason':'needs_profile'}
        refs=result.get('references',{})
        reference=refs.get('result_json','')+'#'+refs['result_sha256'] if refs else ''
        if result.get('continuation_required') and not result.get('blockers'):
            require(type(result.get('progress_count')) is int and result['progress_count']>task.get('progress_count',0)
                    and result['progress_token']!=task.get('progress_token'),'native_frontier_not_advanced')
            return {'state':'continue','result_reference':reference,'reason':'continuation_required',
                    'progress_count':result['progress_count'],'progress_token':result['progress_token']}
        state=('completed' if result['status']=='quoted' and not result.get('blockers') else
               'access_required' if result['status']=='access_required' else 'held')
        return {'state':state,'result_reference':reference,
                'reason':'stage_scoped_only' if state=='completed' else result.get('reason','supplier_profile_stage_held')}
    return invoke
