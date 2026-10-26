"""Read-only schema metadata from the exact type38 source-owned turnaround cache.

No transport, callbacks, source choices, raw request/body persistence or freshness
promotion. The manager independently binds active scope/auth/page/revision/bundles.
"""
from __future__ import annotations

from .price_contract import SPECIAL_SHAPES_SOURCE_CONTRACT, STAGGERED_CUT_SOURCE_URL

TURNAROUND_METADATA_JS = '({ source_contract }) => {\n  // READ ONLY: inspect the source-owned cache; never call controller actions.\n  const expected = {\n    order_form_sha256: \'0aef40e4995a0ecfbcae99f8ad4dd900fdc2201147a37e0b5bbbf0a7fa5525f6\',\n    common_sha256: \'22fd26adacdf0e55142139c0eff52ffc227b8028cb3121662d8678b1d9e337a7\'\n  };\n  const source = \'https://www.printograph.com/products/staggered-cut-flyers/order\';\n  const result = reason => ({ observed: false, reason, schema: \'printograph-turnaround-cache-metadata/v1\',\n    source_requests_issued: false, current_price_eligible: false, source_data_finalized: false });\n  const plain = value => value !== null && typeof value === \'object\' && !Array.isArray(value);\n  const own = (object, key) => Object.hasOwn(object, key);\n  const sameKeys = (object, keys) => plain(object) && Object.keys(object).length === keys.length && keys.every(key => own(object, key));\n  const integer = (value, max = 999999999999) => {\n    if (typeof value !== \'number\' && typeof value !== \'string\') return null;\n    if (typeof value === \'number\' && (!Number.isSafeInteger(value) || value < 1 || value > max)) return null;\n    if (typeof value === \'string\' && !/^[1-9][0-9]{0,11}$/.test(value)) return null;\n    const number = Number(value);\n    return Number.isSafeInteger(number) && number >= 1 && number <= max ? String(number) : null;\n  };\n  const type = value => value === null ? \'null\' : Array.isArray(value) ? \'array\' :\n    [\'string\', \'number\', \'boolean\', \'undefined\'].includes(typeof value) ? typeof value :\n    plain(value) ? \'object\' : \'other\';\n  const shape = (object, known) => {\n    if (!plain(object)) return { type: type(object) };\n    const keys = Object.keys(object);\n    if (keys.length > 32) return { type: \'object\', keys_over_bound: true };\n    // Unknown names and values never enter output. Count their types only.\n    const unknownTypes = { string: 0, number: 0, boolean: 0, null: 0, array: 0, object: 0, undefined: 0, other: 0 };\n    for (const key of keys) if (!known.includes(key)) unknownTypes[type(object[key])] += 1;\n    return { type: \'object\', key_count: keys.length,\n      known_fields: Object.fromEntries(known.map(key => [key, own(object, key) ? type(object[key]) : \'missing\'])),\n      unknown_key_count: keys.filter(key => !known.includes(key)).length, unknown_value_types: unknownTypes };\n  };\n  const dayType = value => {\n    if (value === null || value === undefined) return type(value);\n    if (typeof value === \'number\') return Number.isSafeInteger(value) && value >= 0 && value <= 365 ? \'bounded_integer_number\' : \'other_number\';\n    if (typeof value === \'string\') return /^(?:0|[1-9][0-9]{0,2})$/.test(value) && Number(value) <= 365 ? \'bounded_integer_string\' : \'redacted_string\';\n    return type(value);\n  };\n  try {\n  if (location.href !== source || !sameKeys(source_contract, Object.keys(expected)) ||\n      !Object.keys(expected).every(key => source_contract[key] === expected[key])) return result(\'source_scope_unproven\');\n  const forms = [...document.querySelectorAll(\'#order-form\')];\n  if (forms.length !== 1) return result(\'controller_scope_unproven\');\n  const element = forms[0];\n  if (element.getAttribute(\'data-printproducttype\') !== \'38\' || element.getAttribute(\'data-currentstoretype\') !== \'broker\')\n    return result(\'controller_scope_unproven\');\n  if ([...document.querySelectorAll(\'#esCollapse\')].some(e => e.classList.contains(\'show\')))\n    return result(\'shipping_panel_open\');\n  const roots = [element._vnode?.component?.proxy, element.__vue_app__?._instance?.proxy].filter(Boolean);\n  const controllers = [...new Set(roots.map(root => root.$refs?.childComponents?._?.subTree?.component?.refs?.orderFormInstance).filter(Boolean))];\n  if (controllers.length !== 1) return result(\'controller_missing\');\n  const c = controllers[0], selected = c.jsonObj?.userSelectedOptions, others = c.jsonObj?.others;\n  if (String(c.printProductType) !== \'38\' || JSON.stringify(c.fields?.[\'38\']?.specs) !== JSON.stringify([\'size\',\'orientation\',\'paper\',\'color\']) ||\n      JSON.stringify(c.fields?.[\'38\']?.options) !== JSON.stringify([\'coating\']) || !plain(selected) || !plain(others) ||\n      own(selected, \'productSpecificationId\')) return result(\'controller_schema_unproven\');\n  // Every normal-context flag needs an explicit own-property witness. A null\n  // editOrderItem would not yield the existing controller\'s false flag.\n  const editOrder = others.editOrderItem;\n  const editOrderPrototype = plain(editOrder) ? Object.getPrototypeOf(editOrder) : undefined;\n  const emptyEditOrder = plain(editOrder) &&\n    (editOrderPrototype === null || Object.getPrototypeOf(editOrderPrototype) === null) &&\n    Object.keys(editOrder).length === 0 && Object.getOwnPropertySymbols(editOrder).length === 0;\n  if (!own(others, \'itemId\') || others.itemId !== \'null\' ||\n      !own(others, \'isAdmin\') || others.isAdmin !== false || !own(others, \'waiveFees\') || others.waiveFees !== false ||\n      !own(others, \'editOrderItem\') || !emptyEditOrder ||\n      [\'shoppingCartItemId\', \'priceGroupId\'].some(key => !own(selected, key) || ![\'\', null, undefined].includes(selected[key])))\n    return result(\'private_or_special_context\');\n  const normalized = {};\n  for (const key of [\'size\', \'orientation\', \'paper\', \'color\', \'manualProofType\', \'proofType\', \'fileDelivery\', \'quantity\', \'productionTime\']) {\n    normalized[key] = integer(selected[key], key === \'quantity\' ? 10000000 : 999999999999);\n    if (normalized[key] === null) return result(\'selected_context_incomplete\');\n  }\n  if (normalized.proofType !== \'100\' || normalized.fileDelivery !== \'5\' ||\n      ![\'1\',\'36\',\'37\',\'38\'].includes(normalized.manualProofType) || ![\'1\',\'3\'].includes(normalized.productionTime) ||\n      ![\'1\',\'2\'].includes(normalized.orientation) || ![\'1\',\'3\'].includes(normalized.color)) return result(\'selected_context_unsupported\');\n  if (!own(selected, \'coating\')) return result(\'selected_context_incomplete\');\n  let coating = \'\';\n  if (selected.coating !== \'\') {\n    coating = integer(selected.coating);\n    if (coating === null) return result(\'selected_context_unsupported\');\n  }\n  const visible = e => e.getClientRects().length > 0 && getComputedStyle(e).display !== \'none\' &&\n    getComputedStyle(e).visibility !== \'hidden\' && !e.closest(\'[hidden],[aria-hidden="true"]\');\n  for (const [name, value] of [[\'proofType\',\'100\'], [\'proofTypeForManual\',normalized.manualProofType], [\'fileDeliveryForManual\',\'5\']]) {\n    const radios = [...element.querySelectorAll(\'input[type="radio"][name="\' + name + \'"]\')].filter(e => e.checked);\n    if (radios.length !== 1 || String(radios[0].value) !== value || radios[0].matches(\':disabled\') || !visible(radios[0]))\n      return result(\'retained_manual_controls_unproven\');\n  }\n  const cachedProduction = response => {\n    const schema = \'printograph-current-cached-production/v1\';\n    const hold = reason => ({ observed: false, schema, reason, transport_freshness: \'not_observed\', source_data_finalized: false });\n    if (!sameKeys(response, [\'items\']) || !Array.isArray(response.items) || response.items.length < 1 || response.items.length > 2)\n      return hold(\'cache_schema_unproven\');\n    const ids = response.items.map(row => plain(row) ? row.id : null);\n    if (![[1],[3],[1,3]].some(order => JSON.stringify(order) === JSON.stringify(ids))) return hold(\'cache_schema_unproven\');\n    for (const row of response.items) {\n      if (!sameKeys(row, [\'id\',\'range\']) || !plain(row.range) || !own(row.range,\'min\') || !own(row.range,\'max\'))\n        return hold(\'cache_schema_unproven\');\n      const extra = Object.keys(row.range).filter(key => ![\'min\',\'max\'].includes(key));\n      // Actual saved metadata proves one additional null-valued range field.\n      // Its unknown name and value never enter the projection.\n      if (extra.length !== 1 || row.range[extra[0]] !== null) return hold(\'cache_schema_unproven\');\n      if (typeof row.range.min !== \'number\' || !Number.isSafeInteger(row.range.min) || row.range.min < 0 || row.range.min > 365 ||\n          (row.range.max !== null && (typeof row.range.max !== \'number\' || !Number.isSafeInteger(row.range.max) ||\n           row.range.max < row.range.min || row.range.max > 365))) return hold(\'cache_range_unproven\');\n    }\n    const candidates = [...element.querySelectorAll(\'#radioBtn a.turnAround[data-toggle="turnaround"][data-title]\')];\n    if (candidates.length > 4) return hold(\'rendered_choices_unproven\');\n    const buttons = candidates.filter(visible);\n    if (buttons.length !== 2) return hold(\'rendered_choices_unproven\');\n    const choices = [], textById = new Map();\n    for (const button of buttons) {\n      const id = integer(button.getAttribute(\'data-title\'));\n      if (![\'1\',\'3\'].includes(id) || choices.some(choice => choice.id === Number(id))) return hold(\'rendered_choices_unproven\');\n      if (typeof button.textContent !== \'string\' || button.textContent.length > 256) return hold(\'rendered_choices_unproven\');\n      const text = button.textContent.replace(/\\s+/g,\' \').trim();\n      const disabled = button.getAttribute(\'aria-disabled\') === \'true\' || button.classList.contains(\'disabled\') || /\\bnot\\s+available\\b/i.test(text);\n      choices.push({ id: Number(id), disabled, selected: button.classList.contains(\'active\') });\n      textById.set(Number(id), text);\n    }\n    choices.sort((a,b) => a.id-b.id);\n    if (choices.filter(choice => choice.selected).length !== 1 || !choices.some(choice => choice.selected && String(choice.id) === normalized.productionTime && !choice.disabled) ||\n        !ids.includes(Number(normalized.productionTime))) return hold(\'rendered_choices_unproven\');\n    const items = [];\n    for (const row of response.items) {\n      const choice = choices.find(choice => choice.id === row.id);\n      let agrees = null;\n      if (!choice.disabled) {\n        const match = textById.get(row.id).match(/(?:^|[^0-9])([0-9]{1,3})(?:\\s*-\\s*([0-9]{1,3}))?\\s+Business\\s+Days\\b/i);\n        if (!match || Number(match[1]) !== row.range.min || (match[2] === undefined ? null : Number(match[2])) !== row.range.max)\n          return hold(\'rendered_range_mismatch\');\n        agrees = true;\n      }\n      items.push({ id: row.id, range: { min: row.range.min, max: row.range.max },\n        rendered_disabled: choice.disabled, selected: choice.selected, rendered_range_matches_cache: agrees });\n    }\n    return { observed: true, schema, selected_id: Number(normalized.productionTime), items, choices,\n      range_source: \'current_source_owned_cache\', availability_source: \'current_rendered_choices\',\n      transport_freshness: \'not_observed\', source_data_finalized: false };\n  };\n  const cache = c.cache?.turnaroundsApiData;\n  if (!plain(cache)) return result(\'cache_missing\');\n  const keys = Object.keys(cache);\n  if (keys.length > 64) return result(\'cache_entries_over_bound\');\n  const expectedOptions = [normalized.manualProofType, ...(coating ? [coating] : [])].sort();\n  const matches = [];\n  let matchedResponse;\n  let rejected = 0;\n  for (const key of keys) {\n    // Cache keys are source-serialized requests. Never output a key or raw body.\n    if (typeof key !== \'string\' || key.length === 0 || key.length > 16384) { rejected += 1; continue; }\n    let request;\n    try { request = JSON.parse(key); } catch (_) { rejected += 1; continue; }\n    if (JSON.stringify(request) !== key) { rejected += 1; continue; }\n    if (!sameKeys(request, [\'quantity\',\'specifications\',\'productOptions\',\'itemType\']) ||\n        !sameKeys(request.specifications, [\'printProduct\',\'other\']) || !sameKeys(request.specifications.other, []) ||\n        !sameKeys(request.specifications.printProduct, [\'productType\',\'size\',\'paper\',\'color\',\'turnarounds\']) ||\n        !Array.isArray(request.productOptions) || request.productOptions.length < 1 || request.productOptions.length > 2 ||\n        !Array.isArray(request.specifications.printProduct.turnarounds) ||\n        request.specifications.printProduct.turnarounds.length !== 2 || request.specifications.printProduct.turnarounds[0] !== 1 ||\n        request.specifications.printProduct.turnarounds[1] !== 3 || integer(request.itemType) !== \'5\' ||\n        integer(request.quantity, 10000000) !== normalized.quantity || integer(request.specifications.printProduct.productType) !== \'38\' ||\n        [\'size\',\'paper\',\'color\'].some(field => integer(request.specifications.printProduct[field]) !== normalized[field]) ||\n        request.productOptions.some(value => integer(value) === null) ||\n        JSON.stringify(request.productOptions.map(value => integer(value)).sort()) !== JSON.stringify(expectedOptions)) {\n      rejected += 1; continue;\n    }\n    const response = cache[key];\n    const responseShape = shape(response, [\'items\']);\n    const rows = plain(response) && Array.isArray(response.items) ? response.items : null;\n    const entry = { request_metadata: { quantity_type: type(request.quantity), item_type: type(request.itemType),\n        specification_types: Object.fromEntries([\'productType\',\'size\',\'paper\',\'color\'].map(field => [field,type(request.specifications.printProduct[field])])),\n        option_types: request.productOptions.map(type), other_key_count: 0, turnarounds_are_native_1_3: true },\n      response_metadata: responseShape, items: null };\n    if (rows && rows.length <= 2) entry.items = rows.map(row => ({ shape: shape(row, [\'id\',\'range\']),\n      id_class: plain(row) && integer(row.id) === \'1\' ? \'regular\' : plain(row) && integer(row.id) === \'3\' ? \'rush\' : \'other\',\n      id_type: plain(row) ? type(row.id) : \'missing\',\n      range: plain(row) ? shape(row.range, [\'min\',\'max\']) : { type: \'missing\' },\n      min_type: plain(row) && plain(row.range) ? dayType(row.range.min) : \'missing\',\n      max_type: plain(row) && plain(row.range) ? dayType(row.range.max) : \'missing\' }));\n    if (rows && rows.length > 2) entry.items_over_bound = true;\n    if (rows) entry.item_count = Math.min(rows.length, 3);\n    matchedResponse = response;\n    matches.push(entry);\n  }\n  if (matches.length !== 1) return { ...result(matches.length === 0 ? \'current_cache_entry_missing\' : \'current_cache_entry_ambiguous\'),\n    cache_entry_count: keys.length, rejected_entry_count: rejected, matching_entry_count: Math.min(matches.length, 2) };\n  return { observed: true, schema: \'printograph-turnaround-cache-metadata/v1\', source_url: source, source_contract: expected,\n    selected_quantity: normalized.quantity, selected_specification_ids: { size: normalized.size, paper: normalized.paper, color: normalized.color },\n    selected_manual_proof_id: normalized.manualProofType, selected_coating_id: coating,\n    cache_entry_count: keys.length, rejected_entry_count: rejected, matching_entry_count: 1, current_entry: matches[0], cached_production: cachedProduction(matchedResponse),\n    transport_freshness: \'not_observed\', production_ranges_collected: false, historical_or_cache_metadata_only: true,\n    source_requests_issued: false, current_price_eligible: false, source_data_finalized: false, woocommerce_import_ready: false };\n  } catch (_) { return result(\'metadata_unavailable\'); }\n}\n'

_SCHEMA = 'printograph-turnaround-cache-metadata/v1'
_TYPES = {'string','number','boolean','null','array','object','undefined','other'}
_DAY_TYPES = _TYPES | {'bounded_integer_number','other_number','bounded_integer_string','redacted_string','missing'}
_REASONS = {'source_scope_unproven','controller_scope_unproven','shipping_panel_open',
    'controller_missing','controller_schema_unproven','private_or_special_context',
    'selected_context_incomplete','selected_context_unsupported','retained_manual_controls_unproven',
    'cache_missing','cache_entries_over_bound','current_cache_entry_missing',
    'current_cache_entry_ambiguous','metadata_unavailable'}


def _fail():
    raise ValueError('staggered_turnaround_metadata_unproven')


def _keys(value, required, optional=()):
    if type(value) is not dict or set(value)-set(required)-set(optional) or not set(required)<=set(value):
        _fail()


def _count(value, maximum=64, minimum=0):
    if type(value) is not int or not minimum<=value<=maximum:
        _fail()
    return value


def _identifier(value, maximum=999999999999, empty=False):
    if empty and value=='':
        return value
    if type(value) is not str or not value.isascii() or not value.isdigit() or not 1<=len(value)<=12 or value[0]=='0':
        _fail()
    if not 1<=int(value)<=maximum:
        _fail()
    return value


def _enum(value, choices):
    if type(value) is not str or value not in choices:
        _fail()
    return value


def _shape(value, known, missing=False):
    if type(value) is not dict:
        _fail()
    kind=_enum(value.get('type'), _TYPES | ({'missing'} if missing else set()))
    if kind!='object':
        _keys(value, {'type'})
        return {'type':kind}
    if value.get('keys_over_bound') is True:
        _keys(value, {'type','keys_over_bound'})
        return {'type':'object','keys_over_bound':True}
    _keys(value, {'type','key_count','known_fields','unknown_key_count','unknown_value_types'})
    count=_count(value['key_count'],32)
    unknown=_count(value['unknown_key_count'],32)
    fields=value['known_fields'];_keys(fields, set(known))
    fields={key:_enum(fields[key],_TYPES|{'missing'}) for key in known}
    types=value['unknown_value_types'];_keys(types, _TYPES)
    types={key:_count(types[key],32) for key in sorted(_TYPES)}
    if sum(types.values())!=unknown or count-unknown!=sum(kind!='missing' for kind in fields.values()):
        _fail()
    return {'type':'object','key_count':count,'known_fields':fields,'unknown_key_count':unknown,'unknown_value_types':types}


def _entry(value, coating):
    _keys(value, {'request_metadata','response_metadata','items'}, {'item_count','items_over_bound'})
    request=value['request_metadata']
    _keys(request, {'quantity_type','item_type','specification_types','option_types','other_key_count','turnarounds_are_native_1_3'})
    specifications=request['specification_types'];_keys(specifications, {'productType','size','paper','color'})
    options=request['option_types']
    if (type(options) is not list or len(options)!=(2 if coating else 1)
            or type(request['other_key_count']) is not int or request['other_key_count']!=0
            or request['turnarounds_are_native_1_3'] is not True):
        _fail()
    projected={'request_metadata':{
        'quantity_type':_enum(request['quantity_type'],{'string','number'}),
        'item_type':_enum(request['item_type'],{'string','number'}),
        'specification_types':{key:_enum(specifications[key],{'string','number'}) for key in ('productType','size','paper','color')},
        'option_types':[_enum(kind,{'string','number'}) for kind in options],
        'other_key_count':0,'turnarounds_are_native_1_3':True},
        'response_metadata':_shape(value['response_metadata'], ('items',)), 'items':None}
    rows=value['items']
    if type(rows) is list:
        if len(rows)>2 or 'items_over_bound' in value or value.get('item_count')!=len(rows) or type(value.get('item_count')) is not int:
            _fail()
        projected['items']=[]
        for row in rows:
            _keys(row, {'shape','id_class','id_type','range','min_type','max_type'})
            projected['items'].append({'shape':_shape(row['shape'],('id','range')),
                'id_class':_enum(row['id_class'],{'regular','rush','other'}),
                'id_type':_enum(row['id_type'],_TYPES|{'missing'}),
                'range':_shape(row['range'],('min','max'),missing=True),
                'min_type':_enum(row['min_type'],_DAY_TYPES), 'max_type':_enum(row['max_type'],_DAY_TYPES)})
        projected['item_count']=len(rows)
    elif rows is not None:
        _fail()
    elif 'items_over_bound' in value:
        if value['items_over_bound'] is not True or type(value.get('item_count')) is not int or value['item_count']!=3:
            _fail()
        projected.update(items_over_bound=True,item_count=3)
    elif 'item_count' in value:
        _fail()
    return projected


def _cached_production(value, metadata_entry):
    """Typed current cache values with independently rendered availability."""
    schema = 'printograph-current-cached-production/v1'
    if type(value) is not dict or value.get('schema') != schema or type(value.get('observed')) is not bool:
        _fail()
    if value.get('transport_freshness') != 'not_observed' or value.get('source_data_finalized') is not False:
        _fail()
    if value['observed'] is False:
        _keys(value, {'observed','schema','reason','transport_freshness','source_data_finalized'})
        return {'observed':False,'schema':schema,
                'reason':_enum(value['reason'],{'cache_schema_unproven','cache_range_unproven','rendered_choices_unproven','rendered_range_mismatch'}),
                'transport_freshness':'not_observed','source_data_finalized':False}
    _keys(value, {'observed','schema','selected_id','items','choices','range_source','availability_source','transport_freshness','source_data_finalized'})
    if value['range_source'] != 'current_source_owned_cache' or value['availability_source'] != 'current_rendered_choices':
        _fail()
    selected=value['selected_id']
    if type(selected) is not int or selected not in {1,3}:
        _fail()
    choices=value['choices']
    if type(choices) is not list or len(choices)!=2:
        _fail()
    projected_choices=[]
    for choice,expected_id in zip(choices,(1,3)):
        _keys(choice,{'id','disabled','selected'})
        if type(choice['id']) is not int or choice['id']!=expected_id or type(choice['disabled']) is not bool or type(choice['selected']) is not bool:
            _fail()
        projected_choices.append({'id':expected_id,'disabled':choice['disabled'],'selected':choice['selected']})
    active=[choice for choice in projected_choices if choice['selected']]
    if len(active)!=1 or active[0]['id']!=selected or active[0]['disabled']:
        _fail()
    rows=value['items'];metadata_rows=metadata_entry.get('items')
    if type(rows) is not list or not 1<=len(rows)<=2 or type(metadata_rows) is not list or len(rows)!=len(metadata_rows):
        _fail()
    if [row.get('id') for row in rows if type(row) is dict] not in ([1],[3],[1,3]):
        _fail()
    response=metadata_entry['response_metadata']
    if response.get('type')!='object' or response.get('key_count')!=1 or response.get('unknown_key_count')!=0 or response.get('known_fields')!={'items':'array'}:
        _fail()
    projected_rows=[]
    for row,metadata in zip(rows,metadata_rows):
        _keys(row,{'id','range','rendered_disabled','selected','rendered_range_matches_cache'})
        identifier=row['id']
        if type(identifier) is not int or identifier not in {1,3}:
            _fail()
        _keys(row['range'],{'min','max'})
        minimum=row['range']['min'];maximum=row['range']['max']
        if type(minimum) is not int or not 0<=minimum<=365 or (maximum is not None and (type(maximum) is not int or not minimum<=maximum<=365)):
            _fail()
        role='regular' if identifier==1 else 'rush'
        shape=metadata['shape'];range_shape=metadata['range']
        if (metadata['id_class']!=role or metadata['id_type']!='number' or shape.get('type')!='object'
                or shape.get('key_count')!=2 or shape.get('unknown_key_count')!=0 or shape.get('known_fields')!={'id':'number','range':'object'}
                or range_shape.get('type')!='object' or range_shape.get('key_count')!=3 or range_shape.get('unknown_key_count')!=1
                or range_shape.get('known_fields')!={'min':'number','max':'null' if maximum is None else 'number'}
                or range_shape.get('unknown_value_types',{}).get('null')!=1
                or metadata['min_type']!='bounded_integer_number'
                or metadata['max_type']!=('null' if maximum is None else 'bounded_integer_number')):
            _fail()
        choice=next(choice for choice in projected_choices if choice['id']==identifier)
        if type(row['rendered_disabled']) is not bool or type(row['selected']) is not bool or row['rendered_disabled']!=choice['disabled'] or row['selected']!=choice['selected']:
            _fail()
        agrees=row['rendered_range_matches_cache']
        if (choice['disabled'] and agrees is not None) or (not choice['disabled'] and agrees is not True):
            _fail()
        projected_rows.append({'id':identifier,'range':{'min':minimum,'max':maximum},
            'rendered_disabled':choice['disabled'],'selected':choice['selected'],'rendered_range_matches_cache':agrees})
    if selected not in [row['id'] for row in projected_rows]:
        _fail()
    return {'observed':True,'schema':schema,'selected_id':selected,'items':projected_rows,'choices':projected_choices,
            'range_source':'current_source_owned_cache','availability_source':'current_rendered_choices',
            'transport_freshness':'not_observed','source_data_finalized':False}


def project_turnaround_metadata(observed):
    """Closed fixed-key metadata reducer; never returns raw page values."""
    if type(observed) is not dict or observed.get('schema')!=_SCHEMA or type(observed.get('observed')) is not bool:
        _fail()
    flags={'source_requests_issued':False,'current_price_eligible':False,'source_data_finalized':False}
    if any(observed.get(key) is not value for key,value in flags.items()):
        _fail()
    if observed['observed'] is False:
        _keys(observed, {'observed','reason','schema',*flags}, {'cache_entry_count','rejected_entry_count','matching_entry_count'})
        reason=_enum(observed['reason'],_REASONS)
        result={'observed':False,'reason':reason,'schema':_SCHEMA,**flags,'woocommerce_import_ready':False,
                'production_ranges_collected':False,'transport_freshness':'not_observed'}
        counts={'cache_entry_count','rejected_entry_count','matching_entry_count'}
        if counts & set(observed):
            if not counts<=set(observed) or reason not in {'current_cache_entry_missing','current_cache_entry_ambiguous'}:
                _fail()
            total=_count(observed['cache_entry_count']);rejected=_count(observed['rejected_entry_count']);matching=_count(observed['matching_entry_count'],2)
            if rejected>total or (reason=='current_cache_entry_missing' and (matching!=0 or rejected!=total)) or (reason=='current_cache_entry_ambiguous' and (matching!=2 or total-rejected<2)):
                _fail()
            result.update(cache_entry_count=total,rejected_entry_count=rejected,matching_entry_count=matching)
        return result
    _keys(observed, {'observed','schema','source_url','source_contract','selected_quantity','selected_specification_ids',
        'selected_manual_proof_id','selected_coating_id','cache_entry_count','rejected_entry_count','matching_entry_count',
        'current_entry','cached_production','transport_freshness','production_ranges_collected','historical_or_cache_metadata_only',*flags,'woocommerce_import_ready'})
    if (observed['source_url']!=STAGGERED_CUT_SOURCE_URL or observed['source_contract']!=SPECIAL_SHAPES_SOURCE_CONTRACT
            or observed['transport_freshness']!='not_observed' or observed['production_ranges_collected'] is not False
            or observed['historical_or_cache_metadata_only'] is not True or observed['woocommerce_import_ready'] is not False):
        _fail()
    specs=observed['selected_specification_ids'];_keys(specs, {'size','paper','color'})
    specs={key:_identifier(specs[key]) for key in ('size','paper','color')}
    if specs['color'] not in {'1','3'}:
        _fail()
    proof=_identifier(observed['selected_manual_proof_id'])
    if proof not in {'1','36','37','38'}:
        _fail()
    coating=_identifier(observed['selected_coating_id'],empty=True)
    total=_count(observed['cache_entry_count'],64,1);rejected=_count(observed['rejected_entry_count'])
    if type(observed['matching_entry_count']) is not int or observed['matching_entry_count']!=1 or rejected+1!=total:
        _fail()
    entry=_entry(observed['current_entry'],coating)
    production=_cached_production(observed['cached_production'],entry)
    return {'observed':True,'schema':_SCHEMA,'source_url':STAGGERED_CUT_SOURCE_URL,
        'source_contract':dict(SPECIAL_SHAPES_SOURCE_CONTRACT),
        'selected_quantity':_identifier(observed['selected_quantity'],maximum=10000000),
        'selected_specification_ids':specs,'selected_manual_proof_id':proof,'selected_coating_id':coating,
        'cache_entry_count':total,'rejected_entry_count':rejected,'matching_entry_count':1,
        'current_entry':entry,'cached_production':production,
        'transport_freshness':'not_observed','production_ranges_collected':False,'historical_or_cache_metadata_only':True,
        **flags,'woocommerce_import_ready':False}
