import pytest

from crawler.soardist_availability import available_production_choices, production_quantity_bounds


def test_quantity_scoped_production_filters_unavailable_choices():
    fields=[{'label':'Turnaround Time','choices':[
        {'label':'2 Business Day 1-5pcs','disabled':False},
        {'label':'3 Business Day 6-10pcs','disabled':False},
        {'label':'4 Business Day 11-15pcs','disabled':False},
        {'label':'5 Business Day 16-20pcs','disabled':False}]}]
    assert [choice['label'] for choice in available_production_choices(fields,6)]==['3 Business Day 6-10pcs']
    assert available_production_choices(fields,21)==[]


def test_overlapping_source_ranges_are_preserved_without_inventing_a_choice():
    fields=[{'label':'Production Time','choices':[
        {'label':'4 Business Days Qty 6-20pcs','disabled':False},
        {'label':'5 Business Days Qty 10-15pcs','disabled':False}]}]
    assert len(available_production_choices(fields,12))==2


def test_ambiguous_or_invalid_source_scope_fails_closed():
    assert production_quantity_bounds('2 Business Days') is None
    assert production_quantity_bounds('4 Business Days Qty 21+pcs')==(21,None)
    with pytest.raises(ValueError,match='unclear'):
        production_quantity_bounds('3 Business Days Qty varies')
    with pytest.raises(ValueError,match='invalid'):
        production_quantity_bounds('3 Business Days 20-10pcs')
