import pytest

from crawler.fourover_rows import package_rows, area_rows


def fixture():
    mapping={'complete_child_mapping':True,'rows':[{'child_id':'7','fields':[
        {'label':'Size *','choice':'2 x 3.5'}, {'label':'Colorspec *','choice':'4/0'}]}]}
    prices={'coverage':'all_source_child_routes','observations':[
        {'child_id':'7','status':'priced','package_prices':[
            {'quantity':100,'price':14.289415},{'quantity':250,'price':15.872616}]}]}
    return mapping,prices


def test_joins_exact_source_child_and_preserves_price_precision():
    mapping,prices=fixture()
    rows,columns=package_rows('Cards','https://4over.com/cards',mapping,prices,
        image_url='https://4over.com/card.jpg',production_reference='4 Business Days')
    assert len(rows)==2
    assert columns==['Product','Size','Colorspec','Quantity','Supplier Table Price','Currency',
                     'Production Time (product page reference)','Image URL','Verification','Source URL']
    assert rows[0]['Supplier Table Price']=='14.289415'
    assert rows[0]['Production Time (product page reference)']=='4 Business Days'
    assert rows[0]['Image URL']=='https://4over.com/card.jpg'
    assert rows[0]['Size']=='2 x 3.5'
    assert 'child_id' not in rows[0]


def test_holds_open_quantity_and_duplicate_rows():
    mapping,prices=fixture()
    prices['observations'][0]['package_prices']=None
    with pytest.raises(ValueError,match='Open quantity'): package_rows('Cards','https://4over.com/cards',mapping,prices)
    mapping,prices=fixture()
    prices['observations'][0]['package_prices'].append({'quantity':100,'price':14.289415})
    with pytest.raises(ValueError,match='Duplicate'): package_rows('Cards','https://4over.com/cards',mapping,prices)


def test_area_rows_use_only_rendered_enabled_dimensions():
    mapping={'complete_child_mapping':True,'rows':[{'child_id':'7','fields':[
        {'label':'Stock *','choice':'Black Gator Board'}]}]}
    prices={'coverage':'all_source_child_routes','observations':[
        {'child_id':'7','package_prices':[],'tier_prices':[{'quantity':1,'price':0.0534027778}]}]}
    grid=[{'child_id':'7','source_url':'https://4over.com/gator-board-signs',
           'valid_dimension_pairs':2,'heights_by_width':{'6':['6'],'24':['36']}}]
    rows,columns=area_rows('Gator Board','https://4over.com/gator-board-signs',mapping,prices,grid)
    assert len(rows)==2
    assert rows[0]['Calculated Product Price']=='1.92'
    assert rows[1]['Calculated Product Price']=='46.14'
    assert 'child_id' not in columns
    grid[0]['valid_dimension_pairs']=3
    with pytest.raises(ValueError,match='count mismatch'):
        area_rows('Gator Board','https://4over.com/gator-board-signs',mapping,prices,grid)
