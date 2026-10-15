"""Read-only, product-scoped projection of the authenticated 4over calculator."""

PRODUCT_JS = r"""() => {
  const clean = (value, limit=160) => String(value || '').replace(/\s+/g,' ').trim().slice(0,limit);
  const visible = e => !!e && e.getClientRects().length > 0 &&
    getComputedStyle(e).visibility !== 'hidden';
  const root = document.querySelector('.column.main') ||
    document.querySelector('.product-info-main') || document.querySelector('.product-info') ||
    document.querySelector('main');
  if (!root) return {title:'', fields:[], prices:[], login_prompt:false};
  const fields = [...root.querySelectorAll('select')].filter(visible).slice(0,30).map(e => {
    const label = e.id ? document.querySelector(`label[for="${CSS.escape(e.id)}"]`) : null;
    const container = e.closest('.field, .product-options, .form-group') || e.parentElement;
    return {id:clean(e.id,100), name:clean(e.name,100),
      label:clean(label?.textContent || container?.querySelector('label')?.textContent || e.getAttribute('aria-label')),
      selected:clean(e.value,100), disabled:!!e.disabled,
      choices:[...e.options].slice(0,250).map(o=>({value:clean(o.value,100),label:clean(o.textContent),disabled:!!o.disabled}))};
  });
  const prices = [...root.querySelectorAll('.price, [data-price-type], [class*="price-value"]')]
    .filter(visible).map(e=>clean(e.textContent,100)).filter(Boolean).slice(0,20);
  const actions=[...root.querySelectorAll('button, input[type="number"], input[type="text"]')]
    .filter(visible).slice(0,40).map(e=>({tag:e.tagName.toLowerCase(),type:clean(e.type,30),
      name:clean(e.name,80),label:clean(e.getAttribute('aria-label') || e.textContent,100),
      required:!!e.required,disabled:!!e.disabled,
      form_action:clean(e.form?.getAttribute('action'),160)}));
  const tables=[...root.querySelectorAll('table')].filter(visible)
    .slice(0,10).map(e=>clean(e.textContent,500));
  const quantity_sections=[...root.querySelectorAll('label, span, strong, div')]
    .filter(e=>visible(e) && /^quantity\s*[:*]?$/i.test(clean(e.textContent,40)))
    .slice(0,10).map(e=>clean(e.parentElement?.textContent,300));
  const login_prompt = [...root.querySelectorAll('a,button')].filter(visible)
    .some(e=>/^(?:sign in|log in|login to view price)$/i.test(clean(e.textContent)));
  const childSku=clean(document.querySelector('[data-bind*="child-sku"]')?.textContent,160);
  const selectedChild=clean(document.querySelector('input[name="selected_configurable_option"]')?.value,100);
  return {title:clean(root.querySelector('h1')?.textContent,300),fields,prices,actions,tables,
    quantity_sections,login_prompt,
    child_sku_text:childSku,selected_child_id:selectedChild};
}"""


def json_shape(value, depth=0):
    """Report route structure without copying account or session data."""
    if depth >= 4:
        return {'type': type(value).__name__}
    if isinstance(value, dict):
        return {'type': 'object', 'count': len(value),
                'fields': {str(key)[:60]: json_shape(item, depth + 1)
                           for key, item in list(value.items())[:20]}}
    if isinstance(value, list):
        return {'type': 'array', 'count': len(value),
                'first': json_shape(value[0], depth + 1) if value else None}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {'type': 'number', 'example': value}
    return {'type': type(value).__name__}


def price_tiers(value):
    """Extract only finite numeric quantity/price pairs from the price route."""
    import math
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return None
    rows = value[0].get('packagePrices')
    if not isinstance(rows, list) or len(rows) > 1000:
        return None
    result = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get('qty')) not in (int,float) or type(row.get('price')) not in (int,float):
            return None
        quantity, price = row['qty'], row['price']
        if not math.isfinite(quantity) or quantity < 1 or int(quantity) != quantity or not math.isfinite(price) or price < 0:
            return None
        result.append({'quantity': int(quantity), 'price': price})
    if len({item['quantity'] for item in result}) != len(result):
        return None
    return result


def area_tiers(value):
    """Retain volume discount tiers separately from fixed package prices."""
    import math
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return None
    rows = value[0].get('tierPrices')
    if not isinstance(rows, list) or len(rows) > 1000:
        return None
    result=[]
    for row in rows:
        if not isinstance(row, dict) or type(row.get('qty')) not in (int,float) or type(row.get('price')) not in (int,float):
            return None
        quantity, price = row['qty'], row['price']
        if not math.isfinite(quantity) or quantity < 1 or int(quantity) != quantity or not math.isfinite(price) or price < 0:
            return None
        result.append({'quantity':int(quantity),'price':price})
    return result if len({item['quantity'] for item in result}) == len(result) else None
