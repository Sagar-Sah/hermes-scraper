"""Read-only, scoped checks of rendered Soardist prices in a signed-in browser."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

ORIGIN = 'https://soardistusa.com'

ACCOUNT_CHECK_JS = """() => ({signed_in: [...document.querySelectorAll('button')]
  .some(e => e.getClientRects().length && /^logout$/i.test((e.innerText||'').trim()))})"""

PRODUCT_PRICE_JS = """() => {
  const visible = e => !!e && e.getClientRects().length > 0 &&
    getComputedStyle(e).visibility !== 'hidden';
  const root = document.querySelector('.product-single__meta');
  const title = root?.querySelector('h1.product-single__title')?.textContent?.trim() || '';
  const prices = [...(root?.querySelectorAll('span.product__price') || [])]
    .filter(visible).map(e => (e.innerText || e.textContent || '').trim());
  const variant = root?.querySelector('input[name="id"],select[name="id"]')?.value || null;
  const login_prompt = [...(root?.querySelectorAll('a.talon-price-hide-btn') || [])]
    .some(visible);
  return {title: title.slice(0, 300), prices: prices.slice(0, 4),
    variant_id: variant && /^\\d+$/.test(variant) ? variant : null,
    login_prompt};
}"""


def observed_price(value):
    """Accept one USD price, never a range or mixed sale/regular display."""
    if not isinstance(value, dict) or value.get('login_prompt') is True:
        return None
    prices = value.get('prices')
    if not isinstance(prices, list) or len(prices) != 1:
        return None
    text = prices[0]
    if not isinstance(text, str) or len(text) > 80:
        return None
    match = re.fullmatch(r'\s*(?:Regular price\s*)?\$\s*([0-9][0-9,]*\.[0-9]{2})\s*', text, re.I)
    if not match:
        return None
    try:
        amount = Decimal(match[1].replace(',', ''))
    except InvalidOperation:
        return None
    return format(amount, '.2f') if amount >= 0 else None


def exact_product_page(actual, expected):
    a, b = urlsplit(actual), urlsplit(expected)
    return (a.scheme == b.scheme == 'https' and a.hostname in {'soardistusa.com', 'www.soardistusa.com'}
            and b.hostname in {'soardistusa.com', 'www.soardistusa.com'}
            and a.path.rstrip('/') == b.path.rstrip('/') and not a.query and not a.fragment)
