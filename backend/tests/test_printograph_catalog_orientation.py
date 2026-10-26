"""Type6 source-proven size/orientation dependency, local Chromium only."""
import ast,hashlib,json
from pathlib import Path
import pytest
from test_browser_traversal import local_page,coverage
from test_browser_size_discovery import late_orientation_page
from crawler.adapters.printograph import AdapterError
FIXTURES=Path(__file__).resolve().parent/'fixtures/printograph_catalog_orientation'

def page():
 return '''<!doctype html><h1>Catalog dependency fixture</h1><div id=order-form data-printproducttype=6>
 <label for=size>Size</label><select id=size><option value=114>5.5 x8.5 Vertical</option></select>
 <label for=quantity>Quantity</label><select id=quantity><option value=50>50</option><option value=250>250</option></select>
 <fieldset><legend>Orientation</legend><input id=landscape name=orientation type=radio value=1><label for=landscape>Landscape</label><input id=portrait name=orientation type=radio value=2 checked><label for=portrait>Portrait</label></fieldset>
 </div><div id=subtotal>$5.00</div><script>
 function show(){const orientation=document.querySelector('[name=orientation]:checked').value;const q=document.querySelector('#quantity').value;document.querySelector('#subtotal').textContent='$'+(Number(q)/10+Number(orientation)).toFixed(2);}
 document.querySelectorAll('[name=orientation]').forEach(e=>e.addEventListener('change',()=>{document.querySelector('#size').innerHTML=e.value==='1'?'<option value=115>5.5 x8.5 Horizontal</option>':'<option value=114>5.5 x8.5 Vertical</option>';show();}));
 document.querySelector('#quantity').addEventListener('change',show);show();</script>'''

@pytest.mark.asyncio
async def test_catalog_orientation_precedes_size_without_losing_either_branch(local_page):
 rows,events=await local_page(page());assert {(r['configuration']['orientation']['value'],r['configuration']['size']['value'],r['quantity']) for r in rows}=={('1','115',50),('1','115',250),('2','114',50),('2','114',250)}
 c=coverage(events);assert c['count_is_exact'] and c['status']=='visible_domain_exhausted';assert not c['reasons'];assert not any(k=='browser_restore_reset' for k,_ in events)

@pytest.mark.asyncio
async def test_explicit_landscape_size_retains_its_orientation(local_page):
 rows,events=await local_page(page(),filters={'size':'115','orientation':'1'});assert len(rows)==2
 assert all(r['configuration']['size']['value']=='115' and r['configuration']['orientation']['value']=='1' for r in rows)
 assert coverage(events)['count_is_exact'] and not coverage(events)['reasons']

@pytest.mark.asyncio
async def test_impossible_portrait_horizontal_pair_is_never_published(local_page):
 rows=[]
 with pytest.raises(AdapterError,match='No fully selected source configuration produced a quote'):
  await local_page(page(),filters={'size':'115','orientation':'2'},rows_out=rows)
 assert rows==[]

@pytest.mark.asyncio
async def test_late_native_orientation_is_revealed_before_filtered_size(local_page):
 rows,events=await local_page(late_orientation_page('6'),filters={'size':'104','orientation':'2'});assert len(rows)==1
 assert rows[0]['configuration']['size']['value']=='104' and rows[0]['configuration']['orientation']['value']=='2'
 warm=[v for k,v in events if k=='size_dependency_warmup'];assert len(warm)==1 and warm[0]['published_rows']==0
 assert coverage(events)['count_is_exact'] and not coverage(events)['reasons']

def test_source_type_and_size_orientation_binding_proved_by_existing_records():
 types=json.loads((FIXTURES/'type-proof.json').read_bytes());assert types['private_capture_identifiers_omitted'] is True;assert 'run_id' not in types;assert len(types['records'])==5
 assert all(r['status']==200 and r['product_type_query']==[['productType','6']] for r in types['records'])
 value=json.loads((FIXTURES/'native-catalog-size-orientation.json').read_bytes());assert {str(s['id']):str(s['orientation']['id']) for s in value['sizes']}=={'114':'2','115':'1'}
 raw=(FIXTURES/value['original_response_path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==value['original_response_sha256']
