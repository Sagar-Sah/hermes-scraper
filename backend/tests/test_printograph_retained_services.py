"""Pure synthetic page tests: no browser, source, application or store imports."""
from copy import deepcopy
import pytest

from crawler import printograph_modern_browser as m
URL='https://www.printograph.com/products/cardboards/order'

def raw(pdf=False, design=True):
    names={'proofType':('100','Manually Processed'), 'proofTypeForManual':('1' if pdf else '36','PDF Proof' if pdf else 'No Proof'), 'fileDeliveryForManual':('5','Upload Files Now')}
    if design:names['designServices']=('42','No Design Services')
    groups=[]
    for key in m.SERVICE_KEYS:
        choices=[]
        if key in names:
            value,label=names[key]
            choices=[{'id':key+value,'name':key,'value':value,'label':label,'checked':True,'disabled':False,'visible':True}]
        groups.append({'key':key,'control_count':len(choices),'choices':choices})
    services={'proofType':'100','manualProofType':'1' if pdf else '36','fileDelivery':'5','designServices':'42'}
    return {'observed':True,'open':True,'source_url':URL,'product_type':'47','groups':groups,'model_services':services,
            'source_item_type':{'observed':True,'is_blank_product':False,'value':'5'},
            'native_total':{'observed':True,'controller_total':'13.00','applied_product_price':'13.00'},
            'base_context':{'product_id':'900012','specs':{'size':'11'},'production':'1'},
            'selection_context':{'specs':{'size':'11'},'options':{},'services':deepcopy(services),'quantity':'100','production':'1'}}

def group(r,name):return next(g for g in r['groups'] if g['key']==name)

def choice(r,name):return group(r,name)['choices'][0]

class Page:
    def __init__(self,module,state,on_read=None):
        self.module,self.state,self.on_read=module,deepcopy(state),on_read
        self.reads=0;self.actions=[];self.marks=0;self.settles=0;self.checks=0
    async def evaluate(self,script,args):
        if script==self.module.SERVICE_READ_JS:
            self.reads+=1
            if self.on_read:self.on_read(self,self.reads)
            return deepcopy(self.state)
        assert script==self.module.SERVICE_ACTION_JS
        self.actions.append(deepcopy(args))
        if args['action']=='open':self.state['open']=True;return True
        target=args['target'];name=target['name']
        for c in group(self.state,name)['choices']:c['checked']=c['value']==target['value']
        model={'proofType':'proofType','proofTypeForManual':'manualProofType','fileDeliveryForManual':'fileDelivery','designServices':'designServices'}[name]
        self.state['model_services'][model]=target['value']
        self.state['selection_context']['services'][model]=target['value']
        return True
    async def check(self):self.checks+=1
    async def mark(self):self.marks+=1
    async def settle(self):self.settles+=1

async def run(module,state,pdf=False,on_read=None):
    p=Page(module,state,on_read)
    try:
        value,policy=await module.prepare_services(p,URL,p.check,p.mark,p.settle,service_policy=module.PDF_SERVICE_POLICY if pdf else None)
        outcome=('ok',value,policy)
    except module.ModernHeld as e:outcome=('held',e.reason,e.details)
    return p,outcome

@pytest.mark.asyncio
@pytest.mark.parametrize('pdf,design',[(False,True),(False,False),(True,True),(True,False)])
async def test_open_retained_policy_uses_one_atomic_read_and_preserves_result(pdf,design):
    state=raw(pdf,design)
    p,actual=await run(m,state,pdf)
    assert actual[0]=='ok' and actual[1]==m.bind_service_policy(state,m.PDF_SERVICE_POLICY if pdf else None,URL)
    assert actual[2]=={'proofType':'100','proofTypeForManual':'1' if pdf else '36','fileDeliveryForManual':'5',**({'designServices':'42'} if design else {})}
    assert p.reads==1 and p.actions==[] and p.marks==p.settles==0
    assert m.modern_bound(m.service_signature(actual[1]))

@pytest.mark.asyncio
async def test_closed_panel_uses_original_open_and_reread_path():
    p,a=await run(m,{**raw(),'open':False})
    assert a[0]=='ok' and p.reads==7
    assert [x['action'] for x in p.actions]==['open'] and p.marks==p.settles==1

@pytest.mark.asyncio
async def test_needed_radio_action_follows_original_sequential_path():
    r=raw();g=group(r,'proofTypeForManual');g['choices'][0]['checked']=False
    g['choices'].append({**deepcopy(g['choices'][0]),'id':'other','value':'1','label':'PDF Proof','checked':True});g['control_count']=2
    r['model_services']['manualProofType']='1';r['selection_context']['services']['manualProofType']='1'
    p,a=await run(m,r)
    assert a[0]=='ok' and a[2]['proofTypeForManual']=='36' and len(p.actions)==1 and p.reads==6
    assert p.actions[0]['action']=='radio' and p.actions[0]['target']['value']=='36'
    assert p.marks==p.settles==1

@pytest.mark.asyncio
@pytest.mark.parametrize('drift',['duplicate_eligible','different_checked','hidden','disabled','wrong_label','model_value','two_checked','control_count','duplicate_group','invalid_model','missing_group','wrong_pdf_id'])
async def test_uncertainty_never_takes_shortcut_and_preserves_original_outcome(drift):
    pdf=drift=='wrong_pdf_id';r=raw(pdf)
    c=choice(r,'proofTypeForManual');g=group(r,'proofTypeForManual')
    if drift=='duplicate_eligible':g['choices'].append({**deepcopy(c),'id':'duplicate','value':'38','checked':False});g['control_count']=2
    if drift=='different_checked':c['checked']=False;g['choices'].append({**deepcopy(c),'id':'other','value':'1','label':'PDF Proof','checked':True});g['control_count']=2
    if drift=='hidden':c['visible']=False
    if drift=='disabled':c['disabled']=True
    if drift=='wrong_label':c['label']='Paid Artwork Service'
    if drift=='model_value':r['model_services']['manualProofType']='38'
    if drift=='two_checked':g['choices'].append({**deepcopy(c),'id':'other','value':'37','label':'Other','checked':True});g['control_count']=2
    if drift=='control_count':g['control_count']=99
    if drift=='duplicate_group':r['groups'][-1]=deepcopy(r['groups'][0])
    if drift=='invalid_model':r['model_services']['manualProofType']=True
    if drift=='missing_group':r['groups'].pop()
    if drift=='wrong_pdf_id':c['value']='9';r['model_services']['manualProofType']='9'
    assert m._retained_service_policy(m.bind_service_policy(r,m.PDF_SERVICE_POLICY if pdf else None,URL)) is None
    # The original sequential route remains the fallback for every uncertain
    # first observation; it alone owns native actions and failure reasons.
    p,a=await run(m,r,pdf)
    assert p.reads>=3
    if drift in {'hidden','disabled','wrong_label','duplicate_eligible'}:
        assert a[0]=='held' and a[1]=='modern_safe_service_choice_unavailable' and not p.actions
    elif drift=='wrong_pdf_id':
        assert a[0]=='held' and a[1]=='modern_pdf_proof_identity_unproven' and not p.actions
    elif drift in {'two_checked','duplicate_group'}:
        assert a[0]=='held' and a[1]=='modern_service_policy_reset'
    elif drift in {'control_count','invalid_model','missing_group'}:
        assert a[0]=='held' and a[1]=='modern_service_identity_unbound'
    else:
        assert a[0]=='ok'

@pytest.mark.asyncio
@pytest.mark.parametrize('reason',['private_or_special_context','modern_source_scope_unproven','modern_process_header_unproven'])
async def test_source_guard_failures_keep_existing_machine_reason(reason):
    state={'observed':False,'reason':reason}
    p,a=await run(m,state)
    assert a==('held',reason,None) and p.reads==1 and not p.actions

@pytest.mark.asyncio
async def test_no_cache_retained_between_calls_and_later_drift_uses_native_path():
    p=Page(m,raw());a=await m.prepare_services(p,URL,p.check,p.mark,p.settle)
    assert p.reads==1
    c=choice(p.state,'proofTypeForManual');c['visible']=False
    with pytest.raises(m.ModernHeld,match='safe_service_choice_unavailable'):
        await m.prepare_services(p,URL,p.check,p.mark,p.settle)
    assert p.reads>1 and not p.actions

@pytest.mark.asyncio
async def test_mutation_during_needed_action_still_caught_by_original_final_read():
    state=raw();state['open']=False
    def drift(p,n):
        if n==7:choice(p.state,'proofTypeForManual')['checked']=False
    p,a=await run(m,state,on_read=drift)
    assert a[0]=='held' and a[1]=='modern_service_policy_reset' and p.reads==7

def test_fast_policy_does_not_replace_exact_price_or_production_gates():
    import json
    r=raw();g=m.FreshQuoteGate()
    price_url='https://www.printograph.com/service/rest/v1/products/900012/prices'
    production_url='https://www.printograph.com/service/rest/v1/rules/turnarounds'
    body=json.dumps({'quantity':100,'specifications':{'printProduct':{'productType':47,'size':11,'turnarounds':[1]},'other':{}},'productOptions':[36],'itemType':5})
    g.observe(price_url,200,{'items':[{'quantity':100,'markupPrice':'13.00'}]},r,r,'price',1,1,1,1)
    g.observe(production_url,200,{'items':[{'id':1,'range':{'min':3,'max':4}}]},r,r,'production',1,1,1,1,'POST',body)
    control={'value':'1','selected_label':'Regular 3–4 Business Days'}
    assert g.verify(r,control,displayed_subtotal='13.00')['current_quantity_applied_base_matches_table']
    with pytest.raises(m.ModernHeld,match='subtotal_unproven'):g.verify(r,control,displayed_subtotal='14.00')
    with pytest.raises(m.ModernHeld,match='production_stale'):g.verify(r,{'value':'1','selected_label':'Regular 27–31 Business Days'},displayed_subtotal='13.00')
    with pytest.raises(m.ModernHeld,match='evidence_missing'):m.FreshQuoteGate().verify(r,control,displayed_subtotal='13.00')
