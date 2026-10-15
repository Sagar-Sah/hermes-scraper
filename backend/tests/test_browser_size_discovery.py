"""Orientation revealed by the first ordinary finite size selection."""

import pytest

from test_browser_progress import durable_store, journal_for, commit_rows
from test_browser_traversal import local_page, coverage


def late_orientation_page(product_type, *, shapes=False):
    shape = (
        '<label for="shape">Shape</label><select id="shape"><option value="" selected>Select Shape</option><option value="5">Rectangle</option><option value="4">Square</option></select>'
        if shapes
        else ""
    )
    return f"""<!doctype html><h1>Late orientation fixture</h1><div id="order-form" data-printproducttype="{product_type}">{shape}<div id="dimensions"></div></div>
<div id="subtotal">$0.00</div><script>
const root=document.querySelector('#order-form'),dimensions=document.querySelector('#dimensions');
let mode='1';
function square(){{return document.querySelector('#shape')?.value==='4';}}
function values(){{return square()?['448','449']:mode==='1'?['101','103']:['102','104'];}}
function options(placeholder){{return (placeholder?'<option value="" selected>Select Size</option>':'')+values().map(value=>'<option value="'+value+'">Preset '+value+'</option>').join('');}}
function quote(){{const size=document.querySelector('#size');document.querySelector('#subtotal').textContent=size.value?'$'+(Number(size.value)/10).toFixed(2):'$0.00';}}
function selectSize(){{
 if(!square()&&!document.querySelector('#orientation')){{
  root.insertAdjacentHTML('beforeend','<fieldset id="orientation"><legend>Orientation</legend><input type="radio" id="horizontal" name="orientation" value="1" checked><label for="horizontal">Horizontal</label><input type="radio" id="vertical" name="orientation" value="2"><label for="vertical">Vertical</label></fieldset>');
  document.querySelectorAll('[name=orientation]').forEach(input=>input.addEventListener('change',()=>{{mode=input.value;document.querySelector('#size').innerHTML=options(false);quote();}}));
 }}
 if(!document.querySelector('#quantity'))root.insertAdjacentHTML('beforeend','<label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option></select>');
 quote();
}}
function resetShape(){{mode='1';document.querySelector('#orientation')?.remove();dimensions.innerHTML='<label for="size">Size</label><select id="size">'+options(true)+'</select>';document.querySelector('#size').addEventListener('change',selectSize);document.querySelector('#subtotal').textContent='$0.00';}}
const shape=document.querySelector('#shape');if(shape)shape.addEventListener('change',resetShape);else resetShape();
</script>"""


@pytest.mark.asyncio
async def test_late_orientation_preserves_all_rectangular_and_square_sizes_with_journal(
    local_page, durable_store
):
    journal = await journal_for(durable_store)
    html = late_orientation_page("36", shapes=True)
    rows, events = await local_page(
        html, journal=journal, on_row=commit_rows(durable_store, journal)
    )
    actual = {
        (
            row["configuration"]["shape"]["value"],
            row["configuration"].get("orientation", {}).get("value"),
            row["configuration"]["size"]["value"],
        )
        for row in rows
    }
    assert actual == {
        ("5", "1", "101"),
        ("5", "1", "103"),
        ("5", "2", "102"),
        ("5", "2", "104"),
        ("4", None, "448"),
        ("4", None, "449"),
    }
    assert len(rows) == 6
    warmups = [payload for kind, payload in events if kind == "size_dependency_warmup"]
    assert len(warmups) == 2  # One per shape; square does not recurse forever.
    assert all(payload["published_rows"] == 0 for payload in warmups)
    assert coverage(events)["status"] == "visible_domain_exhausted"
    assert coverage(events)["count_is_exact"]
    assert not coverage(events)["reasons"]

    # Warm-up re-entry uses the same journal scope and does not lose a sibling.
    resumed = await journal_for(durable_store)
    repeated, resumed_events = await local_page(
        html, journal=resumed, on_row=commit_rows(durable_store, resumed)
    )
    assert repeated == []
    assert not any(kind == "size_dependency_warmup" for kind, _ in resumed_events)
    assert coverage(resumed_events)["resumed_completed_branches"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("product_type", ["1", "27"])
async def test_late_orientation_reveals_requested_vertical_size_without_publishing_seed(
    local_page, product_type
):
    rows, events = await local_page(
        late_orientation_page(product_type),
        filters={"size": "104", "orientation": "2"},
    )
    assert len(rows) == 1
    assert rows[0]["configuration"]["size"]["value"] == "104"
    assert rows[0]["configuration"]["orientation"]["value"] == "2"
    warmups = [payload for kind, payload in events if kind == "size_dependency_warmup"]
    assert len(warmups) == 1 and warmups[0]["size"] == "101"
    assert warmups[0]["published_rows"] == 0
    assert coverage(events)["count_is_exact"]
    assert not coverage(events)["reasons"]
