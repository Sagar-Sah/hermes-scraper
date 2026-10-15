"""Join exact browser option paths with account price tables, without guessing rows."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def package_rows(title, source_url, option_map, price_tables, *, image_url='', production_reference=''):
    if not option_map.get('complete_child_mapping') or price_tables.get('coverage') != 'all_source_child_routes':
        raise ValueError('Complete option and price tables required')
    mappings=option_map['rows']
    observations={item['child_id']:item for item in price_tables['observations']}
    if len(observations)!=len(price_tables['observations']) or set(observations)!={item['child_id'] for item in mappings}:
        raise ValueError('Option and price child sets differ')
    rows=[]; signatures=set(); columns=[]
    for mapping in mappings:
        options={}
        for field in mapping['fields']:
            label=field['label'].strip().rstrip('*').strip()
            choice=field['choice'].strip()
            if not label or not choice or label in options:
                raise ValueError('Incomplete or duplicate option label')
            options[label]=choice
            if label not in columns: columns.append(label)
        if not options:
            raise ValueError('No customer-facing options for child')
        price=observations[mapping['child_id']]
        tiers=price.get('package_prices')
        if price.get('status')!='priced' or not isinstance(tiers,list) or not tiers:
            raise ValueError('Open quantity or unpriced child requires separate calculator')
        for tier in tiers:
            quantity=tier.get('quantity')
            try: amount=Decimal(str(tier.get('price')))
            except (InvalidOperation,ValueError,TypeError): raise ValueError('Invalid price') from None
            if type(quantity) is not int or quantity<1 or not amount.is_finite() or amount<0:
                raise ValueError('Invalid package tier')
            signature=(tuple(sorted(options.items())),quantity)
            if signature in signatures:
                raise ValueError('Duplicate customer option and quantity row')
            signatures.add(signature)
            rows.append({'Product':title,**options,'Quantity':quantity,
                         'Supplier Table Price':str(amount),'Currency':'USD',
                         'Production Time (product page reference)':production_reference,
                         'Image URL':image_url,
                         'Verification':'Pending full-selection availability, production, and price checks',
                         'Source URL':source_url})
    if not rows:
        raise ValueError('No finite package rows')
    return rows,['Product',*columns,'Quantity','Supplier Table Price','Currency',
                 'Production Time (product page reference)','Image URL','Verification','Source URL']


def area_rows(title, source_url, option_map, price_tables, dimension_grid):
    """Expand only rendered enabled dimensions at the source's one-set area rate."""
    if not option_map.get('complete_child_mapping') or price_tables.get('coverage') != 'all_source_child_routes':
        raise ValueError('Complete source child mapping required')
    mappings={row['child_id']:row for row in option_map['rows']}
    observations={row['child_id']:row for row in price_tables['observations']}
    grids={row['child_id']:row for row in dimension_grid}
    if (not mappings or len(mappings)!=len(option_map['rows'])
            or len(observations)!=len(price_tables['observations'])
            or len(grids)!=len(dimension_grid)
            or set(mappings)!=set(observations) or set(mappings)!=set(grids)):
        raise ValueError('Dimension, option, and rate child sets differ')
    columns=[]; rows=[]; signatures=set()
    for child_id,mapping in mappings.items():
        rate_tiers=observations[child_id].get('tier_prices') or []
        if len(rate_tiers)!=1 or rate_tiers[0].get('quantity')!=1 or observations[child_id].get('package_prices'):
            raise ValueError('Expected a single one-set area rate')
        try:rate=Decimal(str(rate_tiers[0]['price']))
        except (InvalidOperation,ValueError,TypeError):raise ValueError('Invalid area rate') from None
        if not rate.is_finite() or rate<=0:raise ValueError('Invalid area rate')
        options={}
        for field in mapping['fields']:
            label=field['label'].strip().rstrip('*').strip()
            choice=field['choice'].strip()
            if not label or not choice or label in options:raise ValueError('Invalid customer option')
            options[label]=choice
            if label not in columns:columns.append(label)
        grid=grids[child_id]
        if grid.get('source_url')!=source_url or not isinstance(grid.get('heights_by_width'),dict):
            raise ValueError('Dimension grid source mismatch')
        pairs=0
        for width,heights in grid['heights_by_width'].items():
            if not isinstance(heights,list) or len(heights)!=len(set(heights)):
                raise ValueError('Invalid enabled dimension list')
            for height in heights:
                if not (isinstance(width,str) and width.isdecimal() and isinstance(height,str) and height.isdecimal()
                        and 1<=int(width)<=1000 and 1<=int(height)<=1000):
                    raise ValueError('Invalid enabled dimension')
                signature=(tuple(sorted(options.items())),width,height)
                if signature in signatures:raise ValueError('Duplicate dimension row')
                signatures.add(signature);pairs+=1
                price=(rate*Decimal(width)*Decimal(height)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)
                rows.append({'Product':title,**options,'Width (in)':width,'Height (in)':height,
                             'Quantity':1,'Calculated Product Price':format(price,'.2f'),
                             'Currency':'USD','Verification':'Pending full-selection availability, production, and price checks',
                             'Source URL':source_url})
        if pairs!=grid.get('valid_dimension_pairs') or pairs<1:
            raise ValueError('Dimension grid count mismatch')
    return rows,['Product',*columns,'Width (in)','Height (in)','Quantity','Calculated Product Price',
                 'Currency','Verification','Source URL']
