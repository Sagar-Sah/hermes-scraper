"""Conservative quantity gating for supplier-labelled production choices."""
from __future__ import annotations

import re


_RANGE = re.compile(r'(?<!\d)(\d{1,6})\s*(?:-|–|—|to)\s*(\d{1,6})\s*(?:pcs?|pieces?)\b', re.I)
_PLUS = re.compile(r'(?<!\d)(\d{1,6})\s*\+\s*(?:pcs?|pieces?)\b', re.I)
_QUANTITY_HINT = re.compile(r'\b(?:qty|quantity|pcs?|pieces?)\b', re.I)


def production_quantity_bounds(label: str):
    """Return inclusive bounds, None for unscoped, or raise for unclear scope."""
    if not isinstance(label,str) or not label.strip():
        raise ValueError('Production choice label is missing')
    matches=list(_RANGE.finditer(label))
    plus=list(_PLUS.finditer(label))
    if len(matches)+len(plus)>1:
        raise ValueError('Production choice has multiple quantity scopes')
    if matches:
        lower,upper=map(int,matches[0].groups())
        if lower<1 or upper<lower:
            raise ValueError('Production quantity range is invalid')
        return lower,upper
    if plus:
        lower=int(plus[0].group(1))
        if lower<1:
            raise ValueError('Production quantity minimum is invalid')
        return lower,None
    if _QUANTITY_HINT.search(label):
        raise ValueError('Production quantity scope is unclear')
    return None


def available_production_choices(fields: list[dict], quantity: int):
    """Keep source choices whose explicit quantity range includes this quantity."""
    if type(quantity) is not int or quantity<1:
        raise ValueError('Quantity must be a positive integer')
    production=[field for field in fields if re.search(r'\b(?:turnaround|production time)\b',
                str(field.get('label','')),re.I)]
    if len(production)!=1:
        raise ValueError('One source production field is required')
    allowed=[]
    for choice in production[0].get('choices',[]):
        if choice.get('disabled'):
            continue
        bounds=production_quantity_bounds(choice.get('label',''))
        if bounds is None or (quantity>=bounds[0] and (bounds[1] is None or quantity<=bounds[1])):
            allowed.append(choice)
    return allowed
