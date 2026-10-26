"""Synthetic fixture functions derived from the normal portable POST tests."""
from copy import deepcopy
import json
URL = 'https://www.printograph.com/products/synthetic-rigid/order'
PRICE_URL = 'https://www.printograph.com/service/rest/v1/product/prices?itemId=null&cid='
TURN_URL = 'https://www.printograph.com/service/rest/v1/rules/turnarounds'
HID = {'hidHoleDrillingId': 'materialHoleDrilling', 'hidSideId': 'color', 'hidSizeId': 'size', 'hidWidthId': 'width', 'hidHeightId': 'height', 'hidDesignServiceId': 'designServices', 'hidMaterialColorId': 'materialColor', 'hidCoreColorId': 'coreColor', 'hidMaterialId': 'material', 'hidShapeId': 'shape', 'hidAccessoriesId': 'accessories', 'hidThicknessId': 'thickness', 'hidOrientationId': 'orientation'}
LABELS = {'Size': 'size', 'Orientation': 'orientation', 'Thickness': 'thickness', 'Material': 'material', 'Accessories': 'accessories', 'Material Color': 'materialColor', 'Material Core Color': 'coreColor'}

def raw():
    specs = {'material': '1', 'thickness': '2', 'materialColor': '1', 'coreColor': '1', 'shape': '8', 'size': '11', 'orientation': '1', 'color': '1', 'materialHoleDrilling': '0', 'accessories': ''}
    services = {'proofType': '100', 'manualProofType': '36', 'fileDelivery': '5', 'designServices': '53'}
    groups = [{'key': key, 'control_count': 0, 'choices': []} for key in m.SERVICE_KEYS]
    for group, value, label in zip(groups[:3], ('100', '36', '5'), ('Manually Processed', 'No Proof', 'Upload Files Now')):
        group.update(control_count=1, choices=[{'value': value, 'checked': True, 'label': label, 'disabled': False, 'visible': True}])
    groups[4].update(control_count=1, choices=[{'value': '53', 'checked': True, 'label': 'No Design Services', 'disabled': False, 'visible': True}])
    other = {k: '24' if k == 'hidWidthId' else '36' if k == 'hidHeightId' else services['designServices'] if v == 'designServices' else specs[v] if specs[v] != '0' else '' for k, v in HID.items()}
    other.update({'Size': '24 x 36', 'Thickness': '3/16 inch', 'Material': 'Rigid stock', 'Material Color': 'White', 'Material Core Color': 'White'})
    native = {'paper': '', 'color': '1', **{k[3].lower() + k[4:]: v for k, v in other.items() if k.startswith('hid')}}
    binding = {'product_id': '900012', 'specs': specs, 'options': {}, 'services': services, 'production': '1'}
    generic = {'observed': True, 'properties': {'size': '11', 'printProductType': '47001', 'turnaround': '1', 'quantity': '1-150', 'other': native}, 'turnaround': {'printProduct': {'productType': '47001', 'size': '11', 'color': '1', 'paper': '', 'shape': '8'}, 'other': other, 'options': ['36']}, 'quantity_choices': ['1', '2', '10', '150'], 'dimensions': {'width': '24', 'height': '36'}, 'binding': binding}
    return {'observed': True, 'source_url': URL, 'product_type': '47001', 'groups': groups, 'model_services': services, 'source_item_type': {'observed': True, 'is_blank_product': False, 'value': '5'}, 'native_total': {'observed': True, 'controller_total': '13.43', 'applied_product_price': '13.43'}, 'base_context': {'product_id': '900012', 'specs': specs, 'production': '1'}, 'selection_context': {'specs': specs, 'options': {}, 'services': services, 'quantity': '2', 'production': '1'}, 'generic_price_context': generic}

def price_body(value):
    return {'properties': deepcopy(value['generic_price_context']['properties'])}

def turn_body(value):
    g = value['generic_price_context']['turnaround']
    return {'quantity': value['selection_context']['quantity'], 'specifications': {'printProduct': {**g['printProduct'], 'turnarounds': [1, 3]}, 'other': g['other']}, 'productOptions': g['options'], 'itemType': 5}

def future_response():
    return {'items': [{'quantity': 2, 'productPrice': 13.43, 'subtotal': 13.43, 'available': True, 'itemOptionsPrice': {}}]}

def observe(gate, value=None, body=None, data=None, kind='price', **kwargs):
    value = value or raw()
    gate.observe(kwargs.pop('url', PRICE_URL if kind == 'price' else TURN_URL), kwargs.pop('status', 200), data or (future_response() if kind == 'price' else {'items': [{'id': 1, 'range': {'min': 3, 'max': 4}}]}), kwargs.pop('before', deepcopy(value)), kwargs.pop('after', deepcopy(value)), kwargs.pop('evidence_id', 'price1' if kind == 'price' else 'turn1'), 1, kwargs.pop('generation', 1), 7, kwargs.pop('epoch', 7), kwargs.pop('method', 'POST'), body if isinstance(body, str) else json.dumps(body or (price_body(value) if kind == 'price' else turn_body(value))))
    assert not kwargs

def verified(gate, value=None, display='13.43'):
    return gate.verify(value or raw(), {'value': '1', 'selected_label': 'Regular 3–4 Business Days'}, displayed_subtotal=display)

def html():
    value = raw()
    selected = {**value['selection_context']['specs'], **value['model_services'], 'quantity': '2', 'productId': '900012', 'productionTime': '1', 'paper': '', 'shoppingCartItemId': '', 'priceGroupId': '', 'centerSplit': '', 'standType': '', 'frame': ''}
    fields = {'specs': list(value['selection_context']['specs']), 'options': [], 'other': {**HID, **LABELS}}
    schema = {'size': {'list': [{'id': '11', 'width': 24, 'height': 36, 'label': '24 x 36'}]}, 'quantity': {'list': [{'quantity': q} for q in (1, 2, 10, 150)]}}
    labels = {'orientation': ('1', 'Horizontal'), 'thickness': ('2', '3/16 inch'), 'material': ('1', 'Rigid stock'), 'materialColor': ('1', 'White'), 'coreColor': ('1', 'White')}
    schema.update({k: {'list': [{'id': v, 'label': label}]} for k, (v, label) in labels.items()})
    others = {'isAdmin': False, 'waiveFees': False, 'itemId': 'null', 'editOrderItem': {}, 'blankProducts': [], 'getGenericProductsPriceFromApiList': [47001], 'subItemProducts': [], 'embroideryProducts': [], 'genericOrientation': [], 'redirectFromMediaClip': False, 'defaultSizeOptionCategories': {'Tall': []}, 'apparelSizeStyle1': []}
    radios = ''.join((f'''<input type=radio id="{g['key']}x" name="{g['key']}" value="{g['choices'][0]['value']}" checked><label for="{g['key']}x">{g['choices'][0]['label']}</label>''' for g in value['groups'] if g['choices']))
    c = {'printProductType': 47001, 'fields': {'47001': fields}, 'schema': schema, 'optionsHavingPrices': [], 'jsonObj': {'userSelectedOptions': selected, 'others': others, 'appliedPrices': {'productPrice': '13.43', 'holeDrillingPrice': 0, 'standType': {}, 'frame': {}}}}
    return '<!doctype html><div id=ajaxLoader></div><div id=order-form data-printproducttype=47001 data-currentstoretype=broker><button type=button aria-controls=collapseOne>Process</button><div id=collapseOne class=show>' + radios + '</div></div><script>window.controller=' + json.dumps(c) + ';controller.totalPrice="13.43";document.querySelector("#order-form")._vnode={component:{proxy:{$refs:{childComponents:{_:{subTree:{component:{refs:{orderFormInstance:controller}}}}}}}}};</script>'
