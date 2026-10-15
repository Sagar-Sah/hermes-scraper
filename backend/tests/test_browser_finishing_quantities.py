"""Source-observed finishing domains, including a minimum that hides finishes."""

import pytest

from test_browser_traversal import coverage, local_page


def finishing_quantities_page(key="raisedprint", *, restrict_paid=True):
    paid = "135" if key == "raisedprint" else "3"
    return f"""<!doctype html><h1>Finite finishing domain</h1>
<div id="order-form" data-printproducttype="27">
<label for="paper">Paper</label><select id="paper"><option value="10">Matte</option></select>
<div id="finishing"><label for="{key}">Finishing</label><select id="{key}"><option value="">None</option><option value="{paid}">Paid finish</option></select></div>
<label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option><option value="100" selected>100</option><option value="250">250</option><option value="5000">5000</option><option value="10000">10000</option></select>
</div><div id="radioBtn"><a class="turnAround active" data-toggle="turnaround" data-title="1">Regular 2-4 Business Days</a></div><div id="subtotal">$5.00</div>
<script>
const finish=document.querySelector('#{key}'),quantity=document.querySelector('#quantity');
function quote(){{
 const q=Number(quantity.value),visible=q>=100&&q<=250;
 document.querySelector('#finishing').style.display=visible?'':'none';
 document.querySelector('#subtotal').textContent='$'+(q/20+(visible&&finish.value==='{paid}'?3:0)).toFixed(2);
}}
finish.addEventListener('change',()=>{{
 const previous=quantity.value,tiers={str(restrict_paid).lower()}&&finish.value==='{paid}'?[100,250]:[50,100,250,5000,10000];
 quantity.innerHTML=tiers.map(q=>'<option value="'+q+'">'+q+'</option>').join('');
 if(tiers.includes(Number(previous)))quantity.value=previous;quote();
}});
quantity.addEventListener('change',quote);quote();
</script>"""


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["raisedprint", "spotcoating"])
async def test_supporting_quantity_recovers_finish_without_per_tier_reload(local_page, key):
    visits = []
    rows, events = await local_page(
        finishing_quantities_page(key), bulk=True, document_visits=visits
    )
    paid = "135" if key == "raisedprint" else "3"
    selected = {
        (row["configuration"].get(key, {}).get("value"), row["quantity"])
        for row in rows
    }
    assert selected == {
        (None, 50), ("", 100), ("", 250), (None, 5000), (None, 10000),
        (paid, 100), (paid, 250),
    }
    assert len(rows) == len(selected)
    assert visits == ["/product"]
    assert coverage(events)["restore_failures"] == 0
    assert not any(kind == "browser_restore_reset" for kind, _ in events)
    assert not any(kind == "branch_skipped" for kind, _ in events)


@pytest.mark.asyncio
async def test_paid_finish_filter_never_turns_hidden_finish_into_valid_paid_row(local_page):
    rows, events = await local_page(
        finishing_quantities_page(restrict_paid=False),
        filters={"raisedprint": "135"}, bulk=True,
    )
    assert {row["quantity"] for row in rows} == {100, 250}
    assert all(row["configuration"]["raisedprint"]["value"] == "135" for row in rows)
    assert {row["subtotal"] for row in rows} == {"8.00", "15.50"}
    assert coverage(events)["status"] == "partial"
    summaries = [payload for kind, payload in events if kind == "browser_traversal_diagnostics"]
    assert len(summaries) == 1
    assert sum(item["occurrences"] for item in summaries[0]["restore_failures"]) > 0
    assert all("prefix" in payload and "effective_configuration" in payload
               for kind, payload in events if kind == "browser_restore_reset")


@pytest.mark.asyncio
async def test_explicit_none_filter_requires_visible_selected_none(local_page):
    rows, events = await local_page(
        finishing_quantities_page(), filters={"raisedprint": ""}, bulk=True,
    )
    assert {row["quantity"] for row in rows} == {100, 250}
    assert all(row["configuration"]["raisedprint"]["value"] == "" for row in rows)
    assert coverage(events)["status"] == "partial"
