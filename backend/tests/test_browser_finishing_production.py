"""Recover a finishing sibling from an inherited minimum-quantity Rush state."""

import pytest

from test_browser_traversal import local_page, coverage


def finishing_page(key, *, remove_control):
    paid = "135" if key == "raisedprint" else "3"
    return f"""<!doctype html><h1>Production-dependent finishing</h1>
<div id="order-form" data-printproducttype="1"><label for="paper">Paper</label><select id="paper"><option value="10">Matte</option></select>
<div id="finishing"><label for="{key}">Finishing</label><select id="{key}"><option value="">None</option><option value="{paid}">Paid finish</option></select></div>
<label for="quantity">Quantity</label><select id="quantity"><option value="100">100</option></select></div>
<div id="radioBtn"><a class="turnAround active" data-toggle="turnaround" data-title="1">Regular 2-4 Business Days</a><a id="rush" class="turnAround notActive" data-toggle="turnaround" data-title="3">Rush 2 Business Days</a></div><div id="subtotal">$5.00</div>
<script>
const finish=document.querySelector('#{key}'),regular=document.querySelector('[data-title="1"]'),rush=document.querySelector('#rush');
function update(){{
 const isRush=rush.classList.contains('active');
 if({str(remove_control).lower()})document.querySelector('#finishing').style.display=isRush?'none':'';
 else {{const value=finish.value;finish.innerHTML=isRush?'<option value="">None</option>':'<option value="">None</option><option value="{paid}">Paid finish</option>';if([...finish.options].some(o=>o.value===value))finish.value=value;}}
 rush.textContent=finish.value==='{paid}'?'Rush Not Available':'Rush 2 Business Days';
 document.querySelector('#subtotal').textContent=finish.value==='{paid}'?'$8.00':isRush?'$6.00':'$5.00';
}}
document.querySelectorAll('.turnAround').forEach(button=>button.addEventListener('click',()=>{{
 document.querySelectorAll('.turnAround').forEach(other=>other.className='turnAround notActive');button.className='turnAround active';update();
}}));finish.addEventListener('change',update);update();
</script>"""


def regular_actions(monkeypatch):
    from playwright.async_api import Locator

    original, actions = Locator.click, []

    async def track(locator, *args, **kwargs):
        if await locator.get_attribute("data-title") == "1":
            actions.append("regular")
        return await original(locator, *args, **kwargs)

    monkeypatch.setattr(Locator, "click", track)
    return actions


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["raisedprint", "spotcoating"])
async def test_regular_domain_recovers_paid_finish_after_inherited_rush(
    local_page, key, monkeypatch
):
    actions = regular_actions(monkeypatch)
    paid = "135" if key == "raisedprint" else "3"
    rows, events = await local_page(finishing_page(key, remove_control=True), bulk=True)
    selected = {
        (
            row["configuration"].get(key, {}).get("value"),
            row["production"]["value"],
            row["subtotal"],
        )
        for row in rows
    }
    assert (paid, "1", "8.00") in selected
    assert not any(value == paid and mode == "3" for value, mode, _ in selected)
    assert all(row["quantity"] == 100 for row in rows)
    # Absence after production is fixed remains an explicit unsupported branch;
    # this regression does not pretend that missing-control Rush is enumerated.
    assert coverage(events)["status"] == "partial"
    assert not any(
        kind == "branch_skipped"
        and payload.get("key") == key
        and payload.get("choice_value") == paid
        for kind, payload in events
    )
    # The fixed Rush prefix fails conservatively; only the subsequent unfixed
    # sibling restoration may select Regular once.
    assert actions == ["regular"]


@pytest.mark.asyncio
async def test_explicit_rush_filter_does_not_publish_regular_finish(
    local_page, monkeypatch
):
    actions = regular_actions(monkeypatch)
    rows, events = await local_page(
        finishing_page("raisedprint", remove_control=False),
        filters={"productionTime": "3"},
        bulk=True,
    )
    assert len(rows) == 1
    assert rows[0]["production"]["value"] == "3"
    assert rows[0]["configuration"]["raisedprint"]["value"] == ""
    assert rows[0]["subtotal"] == "6.00"
    assert not any(
        row["configuration"]["raisedprint"]["value"] == "135" for row in rows
    )
    assert actions == []
